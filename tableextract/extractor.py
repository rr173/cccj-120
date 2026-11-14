"""Offline HTML table extraction (Python standard library only).

One call -- :func:`extract_table` -- takes raw HTML bytes, the id of the
target ``<table>`` and a non-negative ``header_rows`` count, and returns
a :class:`TableExtraction` with the rectangular grid, the numbered list
of real source cells, per-column header paths and the expanded data
rows.  No network access, no browser, no third-party HTML parser.

Supported HTML subset (deliberately strict, no browser-style repair):

* tags and attribute names are case-insensitive; attribute values are
  compared as-is;
* the document must be explicitly closed: every non-void start tag needs
  its matching end tag (void elements such as ``br``/``img``/``meta``
  never need closing);
* inside the selected table only ``thead``/``tbody``/``tfoot``/``tr``
  may sit directly under ``<table>``, only ``tr`` under a row group,
  and only ``td``/``th`` under ``tr``; consecutive direct ``tr``
  children form one implicit row group;
* cell content is inline markup plus optionally nested ``<table>``
  elements (a nested table is a table of its own and never contributes
  to the outer cell);
* anything outside this subset -- missing close tags, illegal nesting,
  misplaced text -- is located and rejected, never silently repaired.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

from .errors import (
    DuplicateTableIdError,
    HeaderBoundaryError,
    HeaderRowsError,
    HtmlStructureError,
    InputTooLargeError,
    InvalidUtf8Error,
    LayoutConflictError,
    ResultLimitError,
    RowspanOutOfGroupError,
    SpanAttributeError,
    TableNotFoundError,
)

__all__ = [
    "MAX_INPUT_BYTES", "MAX_ROWS", "MAX_COLS", "MAX_POSITIONS",
    "SourceCell", "RowGroupInfo", "HeaderEntry", "DataRow",
    "TableExtraction", "extract_table",
]

#: hard caps, all checked before any large grid is allocated
MAX_INPUT_BYTES = 4 * 1024 * 1024  # 4 MiB of raw HTML
MAX_ROWS = 10_000
MAX_COLS = 256
MAX_POSITIONS = 2_000_000

_ASCII_WS = " \t\n\r\f"
_WS_RUN_RE = re.compile(r"[ \t\n\r\f]+")
_SPAN_RE = re.compile(r"[0-9]+")

_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
})
_RAW_TEXT_TAGS = frozenset({"script", "style"})
_ROW_GROUP_TAGS = frozenset({"thead", "tbody", "tfoot"})
_CELL_TAGS = frozenset({"td", "th"})
_STRUCTURAL_TAGS = _ROW_GROUP_TAGS | _CELL_TAGS | frozenset({"tr", "table"})


# ---------------------------------------------------------------------------
# document tree (strict, explicitly closed subset)
# ---------------------------------------------------------------------------

@dataclass
class Text:
    """A character-data node; positions are 1-based line / 0-based col."""
    value: str
    line: int
    col: int


@dataclass
class Element:
    """An element node; ``line``/``col`` point at its start tag."""
    tag: str
    attrs: list  # list of (name, value|None), names already lowercased
    line: int
    col: int
    children: list  # Element | Text


class _StrictTreeBuilder(HTMLParser):
    """Build a simple element tree, rejecting any unclosed/mismatched tag.

    html.parser already lowercases tag and attribute names, decodes
    character references in text and attribute values, and hands us the
    raw text of script/style (which we drop).  What it does *not* do is
    any error recovery -- every end tag must match the innermost open
    element, which is exactly the supported subset.
    """

    def __init__(self, table_id):
        super().__init__(convert_charrefs=True)
        self._table_id = table_id
        self.root = Element("#document", [], 1, 0, [])
        self._stack = [self.root]

    def handle_starttag(self, tag, attrs):
        line, col = self.getpos()
        el = Element(tag, list(attrs), line, col, [])
        self._stack[-1].children.append(el)
        if tag not in _VOID_TAGS:
            self._stack.append(el)

    def handle_startendtag(self, tag, attrs):
        # explicit ``<tag/>``: a complete element with no children
        line, col = self.getpos()
        self._stack[-1].children.append(Element(tag, list(attrs), line, col, []))

    def handle_endtag(self, tag):
        line, col = self.getpos()
        if tag in _VOID_TAGS:
            return  # a stray "</br>" etc. carries no information
        if len(self._stack) == 1:
            raise HtmlStructureError(
                f"结束标签 </{tag}> 没有对应的开始标签",
                table_id=self._table_id, line=line, col=col)
        top = self._stack[-1]
        if top.tag != tag:
            raise HtmlStructureError(
                f"标签闭合错位：<{top.tag}>（第 {top.line} 行，第 {top.col} 列）"
                f"尚未闭合就遇到 </{tag}>；本工具不猜测补全，请修正源文件",
                table_id=self._table_id, line=line, col=col)
        self._stack.pop()

    def handle_data(self, data):
        if not data:
            return
        if self._stack[-1].tag in _RAW_TEXT_TAGS:
            return  # script/style content is not data
        line, col = self.getpos()
        self._stack[-1].children.append(Text(data, line, col))

    def handle_comment(self, data):
        pass  # comments are not data

    def handle_decl(self, decl):
        pass  # doctype carries no table data

    def handle_pi(self, data):
        pass

    def unknown_decl(self, data):
        pass

    def close(self):
        super().close()
        if len(self._stack) != 1:
            top = self._stack[-1]
            raise HtmlStructureError(
                f"文档结束时 <{top.tag}>（第 {top.line} 行，第 {top.col} 列）仍未闭合",
                table_id=self._table_id, line=top.line, col=top.col)


def _parse_document(html_bytes, table_id):
    """Decode UTF-8 and build the element tree (whole document, strict)."""
    if len(html_bytes) > MAX_INPUT_BYTES:
        raise InputTooLargeError(
            f"输入 {len(html_bytes)} 字节，超过上限 {MAX_INPUT_BYTES} 字节（4 MiB）",
            table_id=table_id)
    try:
        text = html_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        line = html_bytes.count(b"\n", 0, exc.start) + 1
        last_nl = html_bytes.rfind(b"\n", 0, exc.start)
        col = exc.start - (last_nl + 1)
        raise InvalidUtf8Error(
            f"非法 UTF-8 字节序列（{exc.reason}），无法按 UTF-8 解码",
            table_id=table_id, line=line, col=col) from exc
    builder = _StrictTreeBuilder(table_id)
    builder.feed(text)
    builder.close()
    return builder.root


# ---------------------------------------------------------------------------
# table selection
# ---------------------------------------------------------------------------

def _iter_elements(root):
    """Yield every element in document order (iterative, deep-nesting safe)."""
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        for child in reversed(node.children):
            if isinstance(child, Element):
                stack.append(child)


def _select_table(root, table_id):
    """The requested id must occur on exactly one <table> in the document."""
    matches = []
    available = []
    for el in _iter_elements(root):
        if el.tag != "table":
            continue
        ids = [value for name, value in el.attrs if name == "id"]
        available.extend(v for v in ids if v is not None)
        if any(v == table_id for v in ids):
            matches.append(el)
    if not matches:
        if available:
            hint = "；文档中现有的表格 id：" + "、".join(
                repr(v) for v in sorted(set(available))[:8])
        else:
            hint = "；文档中没有任何带 id 的 <table>"
        raise TableNotFoundError(
            f"找不到 id 为 {table_id!r} 的表格{hint}", table_id=table_id)
    if len(matches) > 1:
        locs = "、".join(f"第 {el.line} 行" for el in matches)
        raise DuplicateTableIdError(
            f"id {table_id!r} 同时出现在 {len(matches)} 个 <table> 上（{locs}），"
            f"无法唯一确定目标表格",
            table_id=table_id, line=matches[1].line, col=matches[1].col)
    return matches[0]


# ---------------------------------------------------------------------------
# structural validation of the selected table
# ---------------------------------------------------------------------------

@dataclass
class _Group:
    kind: str   # "thead" | "tbody" | "tfoot" | "implicit"
    rows: list  # list of rows; a row is a list of td/th Elements


def _reject_text(text_node, context, table_id):
    if text_node.value.strip(_ASCII_WS):
        raise HtmlStructureError(
            f"{context} 下不允许直接出现文本（只允许空白），"
            f"文本必须位于 td/th 单元格内",
            table_id=table_id, line=text_node.line, col=text_node.col)


def _validate_cell_content(cell_el, table_id):
    """No structural tag inside a cell unless a nested <table> intervenes."""
    stack = list(cell_el.children)
    while stack:
        node = stack.pop()
        if isinstance(node, Text):
            continue
        if node.tag == "table":
            continue  # a nested table is a separate table, not cell structure
        if node.tag in _STRUCTURAL_TAGS:
            raise HtmlStructureError(
                f"单元格 <{cell_el.tag}>（第 {cell_el.line} 行）内非法嵌套 "
                f"<{node.tag}>；行/单元格结构只能出现在表格层",
                table_id=table_id, line=node.line, col=node.col)
        stack.extend(node.children)


def _build_row(tr_el, table_id):
    cells = []
    for child in tr_el.children:
        if isinstance(child, Text):
            _reject_text(child, "<tr>", table_id)
            continue
        if child.tag not in _CELL_TAGS:
            raise HtmlStructureError(
                f"<tr> 下不允许直接出现 <{child.tag}>（只允许 td/th）",
                table_id=table_id, line=child.line, col=child.col)
        _validate_cell_content(child, table_id)
        cells.append(child)
    return cells


def _build_groups(table_el, table_id):
    """Split the table into row groups, validating structure on the way."""
    groups = []
    implicit = None
    for child in table_el.children:
        if isinstance(child, Text):
            _reject_text(child, "<table>", table_id)
            continue
        tag = child.tag
        if tag == "tr":
            if implicit is None:
                implicit = _Group("implicit", [])
                groups.append(implicit)
            implicit.rows.append(_build_row(child, table_id))
        elif tag in _ROW_GROUP_TAGS:
            implicit = None  # a row group breaks any run of direct <tr>
            group = _Group(tag, [])
            for sub in child.children:
                if isinstance(sub, Text):
                    _reject_text(sub, f"<{tag}>", table_id)
                    continue
                if sub.tag != "tr":
                    raise HtmlStructureError(
                        f"<{tag}> 下不允许直接出现 <{sub.tag}>（只允许 tr）",
                        table_id=table_id, line=sub.line, col=sub.col)
                group.rows.append(_build_row(sub, table_id))
            groups.append(group)
        else:
            raise HtmlStructureError(
                f"<table> 下不允许直接出现 <{tag}>"
                f"（只允许 thead/tbody/tfoot/tr）",
                table_id=table_id, line=child.line, col=child.col)
    return groups


# ---------------------------------------------------------------------------
# cell text
# ---------------------------------------------------------------------------

def _cell_text(cell_el):
    """Normalized text of one cell.

    Inline tags contribute only their text; character references are
    already decoded by the parser; each <br> counts as one space; nested
    tables contribute nothing; runs of ASCII whitespace collapse to one
    space and the result is stripped.  (U+00A0 from &nbsp; is *not*
    ASCII whitespace and is preserved.)
    """
    parts = []
    stack = list(reversed(cell_el.children))
    while stack:
        node = stack.pop()
        if isinstance(node, Text):
            parts.append(node.value)
        elif node.tag == "table":
            continue  # nested table content never enters the outer cell
        elif node.tag == "br":
            parts.append(" ")
        else:
            stack.extend(reversed(node.children))
    return _WS_RUN_RE.sub(" ", "".join(parts)).strip(_ASCII_WS)


# ---------------------------------------------------------------------------
# span attributes
# ---------------------------------------------------------------------------

def _parse_span(cell_el, attr, table_id):
    """Parse rowspan/colspan: default 1; decimal non-negative integer only."""
    values = [v for k, v in cell_el.attrs if k == attr]
    if len(values) > 1:
        raise SpanAttributeError(
            f"<{cell_el.tag}> 上 {attr} 属性重复出现 {len(values)} 次",
            table_id=table_id, line=cell_el.line, col=cell_el.col)
    if not values:
        return 1
    raw = values[0]
    if raw is None:
        raise SpanAttributeError(
            f"{attr} 属性缺少属性值",
            table_id=table_id, line=cell_el.line, col=cell_el.col)
    stripped = raw.strip(_ASCII_WS)
    if not _SPAN_RE.fullmatch(stripped):
        raise SpanAttributeError(
            f"{attr}={raw!r} 非法：去掉首尾 ASCII 空白后只允许十进制非负整数",
            table_id=table_id, line=cell_el.line, col=cell_el.col)
    value = int(stripped)
    if attr == "colspan" and value == 0:
        raise SpanAttributeError(
            "colspan 必须大于 0（rowspan 才可以为 0）",
            table_id=table_id, line=cell_el.line, col=cell_el.col)
    return value


# ---------------------------------------------------------------------------
# result model
# ---------------------------------------------------------------------------

@dataclass
class SourceCell:
    """One real td/th from the HTML, numbered in document order.

    ``row``/``col`` is the top-left position on the expanded grid
    (0-based); ``span_rows``/``span_cols`` is the *actual* expanded span
    (rowspan="0" already resolved against its row group); ``line``/
    ``col_offset`` locate the start tag in the original HTML (1-based
    line, 0-based column offset).
    """
    index: int
    kind: str           # "td" | "th"
    text: str
    row_group: int      # 0-based row group number
    row_group_kind: str # "thead" | "tbody" | "tfoot" | "implicit"
    row: int
    col: int
    span_rows: int
    span_cols: int
    line: int
    col_offset: int

    def to_dict(self):
        return {
            "index": self.index,
            "kind": self.kind,
            "text": self.text,
            "row_group": self.row_group,
            "row_group_kind": self.row_group_kind,
            "row": self.row,
            "col": self.col,
            "span_rows": self.span_rows,
            "span_cols": self.span_cols,
            "line": self.line,
            "col_offset": self.col_offset,
        }


@dataclass
class RowGroupInfo:
    """One row group of the table, in document order."""
    index: int
    kind: str      # "thead" | "tbody" | "tfoot" | "implicit"
    rows: int      # number of rows in this group
    first_row: int # 0-based expanded row index of the group's first row

    def to_dict(self):
        return {"index": self.index, "kind": self.kind,
                "rows": self.rows, "first_row": self.first_row}


@dataclass
class HeaderEntry:
    """One source cell contributing to a column's header path."""
    cell: int  # source cell index
    text: str

    def to_dict(self):
        return {"cell": self.cell, "text": self.text}


@dataclass
class DataRow:
    """One expanded data row.

    ``values[i]``/``cells[i]`` describe grid column i: a real (possibly
    empty-text) cell gives its text and source cell index; a padded gap
    gives None/None -- the two are always distinguishable.
    """
    row: int           # 0-based expanded row index
    values: list       # list of str | None
    cells: list        # list of int | None (source cell indices)

    def to_dict(self):
        return {"row": self.row, "values": list(self.values),
                "cells": list(self.cells)}


@dataclass
class TableExtraction:
    """Complete result of one :func:`extract_table` call.

    ``grid[r][c]`` is the source cell index covering expanded position
    (r, c) -- positions covered by a span reference the same original
    cell -- or None for a padded gap.  Numbering and ordering are stable
    for identical input.
    """
    table_id: str
    header_rows: int
    n_rows: int
    n_cols: int
    row_groups: list    # list of RowGroupInfo
    source_cells: list  # list of SourceCell, in document order
    grid: list          # list of rows of int | None
    header_paths: list  # one list of HeaderEntry per column
    data_rows: list     # list of DataRow

    def to_dict(self):
        return {
            "table_id": self.table_id,
            "header_rows": self.header_rows,
            "rows": self.n_rows,
            "cols": self.n_cols,
            "row_groups": [g.to_dict() for g in self.row_groups],
            "source_cells": [c.to_dict() for c in self.source_cells],
            "grid": [list(row) for row in self.grid],
            "header_paths": [[e.to_dict() for e in path]
                             for path in self.header_paths],
            "data_rows": [d.to_dict() for d in self.data_rows],
        }


# ---------------------------------------------------------------------------
# layout
# ---------------------------------------------------------------------------

def _layout(groups, n_rows, table_id):
    """Place every source cell on the expanded grid, per row group.

    Returns (cells, row_new, n_cols) where row_new[r] holds the sparse
    intervals (start, end, cell_index, span_rows) of cells *starting* in
    row r.  Only intervals are tracked here -- the dense grid is
    allocated later, after the size caps are checked.
    """
    cells = []
    row_new = [[] for _ in range(n_rows)]
    n_cols = 0
    base = 0
    for gi, group in enumerate(groups):
        g_len = len(group.rows)
        carries = []  # [start, end, remaining_future_rows, cell_index]
        for ri, row_els in enumerate(group.rows):
            r = base + ri
            carries = [iv for iv in carries if iv[2] > 0]
            active = sorted(carries, key=lambda iv: iv[0])
            width = 0
            for iv in active:
                if iv[1] > width:
                    width = iv[1]
            cursor = 0
            for el in row_els:
                rowspan = _parse_span(el, "rowspan", table_id)
                colspan = _parse_span(el, "colspan", table_id)
                if rowspan == 0:
                    span_rows = g_len - ri  # to the end of *this* group
                else:
                    span_rows = rowspan
                    if ri + span_rows > g_len:
                        raise RowspanOutOfGroupError(
                            f"rowspan={rowspan} 超出所在行组"
                            f"（{group.kind}，本组共 {g_len} 行）：单元格从组内第 "
                            f"{ri} 行起最多只能再跨 {g_len - ri} 行；"
                            f"跨行绝不流入下一行组",
                            table_id=table_id, line=el.line, col=el.col)
                # leftmost free column: skip whole carried intervals,
                # never split this cell's colspan around them
                for iv in active:
                    if iv[1] <= cursor:
                        continue
                    if iv[0] <= cursor:
                        cursor = iv[1]
                    else:
                        break
                start = cursor
                end = start + colspan
                for iv in active:
                    if iv[0] >= end:
                        break
                    if iv[1] > start:
                        conflict_col = max(start, iv[0])
                        other = cells[iv[3]]
                        raise LayoutConflictError(
                            f"单元格占位冲突：<{el.tag}>（第 {el.line} 行）声明的"
                            f"跨度与先前跨行单元格 <{other.kind}>（第 "
                            f"{other.line} 行）在展开坐标 (行 {r}, 列 "
                            f"{conflict_col}) 重叠；已拒绝，不会用后者覆盖前者",
                            table_id=table_id, line=el.line, col=el.col,
                            conflict_row=r, conflict_col=conflict_col,
                            existing_cell_line=other.line)
                cell = SourceCell(
                    index=len(cells), kind=el.tag, text=_cell_text(el),
                    row_group=gi, row_group_kind=group.kind,
                    row=r, col=start, span_rows=span_rows, span_cols=colspan,
                    line=el.line, col_offset=el.col)
                cells.append(cell)
                row_new[r].append((start, end, cell.index, span_rows))
                if span_rows > 1:
                    carries.append([start, end, span_rows - 1, cell.index])
                cursor = end
                if end > width:
                    width = end
            if width > n_cols:
                n_cols = width
            for iv in active:
                iv[2] -= 1
        base += g_len
    return cells, row_new, n_cols


def _fill_grid(groups, row_new, n_rows, n_cols):
    """Materialize the dense reference matrix (called after cap checks)."""
    grid = [[None] * n_cols for _ in range(n_rows)]
    base = 0
    for group in groups:
        carries = []
        for ri in range(len(group.rows)):
            r = base + ri
            carries = [iv for iv in carries if iv[2] > 0]
            active = list(carries)
            row = grid[r]
            for iv in active:
                for c in range(iv[0], iv[1]):
                    row[c] = iv[3]
            for (start, end, ci, span_rows) in row_new[r]:
                for c in range(start, end):
                    row[c] = ci
                if span_rows > 1:
                    carries.append([start, end, span_rows - 1, ci])
            for iv in active:
                iv[2] -= 1
        base += len(group.rows)
    return grid


def _header_paths(grid, cells, header_rows, n_cols):
    """Per column, top-down over the header rows: collect each source
    cell's text once (a rowspan repeating in the column is not
    duplicated; two different cells with equal text both stay)."""
    paths = []
    for c in range(n_cols):
        seen = set()
        path = []
        for r in range(header_rows):
            ci = grid[r][c]
            if ci is None or ci in seen:
                continue
            seen.add(ci)
            text = cells[ci].text
            if text == "":
                continue
            path.append(HeaderEntry(cell=ci, text=text))
        paths.append(path)
    return paths


def _data_rows(grid, cells, header_rows, n_rows, n_cols):
    rows = []
    for r in range(header_rows, n_rows):
        values = []
        refs = []
        for c in range(n_cols):
            ci = grid[r][c]
            if ci is None:
                values.append(None)
                refs.append(None)
            else:
                values.append(cells[ci].text)
                refs.append(ci)
        rows.append(DataRow(row=r, values=values, cells=refs))
    return rows


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------

def extract_table(html_bytes, table_id, header_rows=0):
    """Extract one <table> from raw UTF-8 HTML bytes into a rectangle.

    Args:
        html_bytes:  raw HTML (bytes, at most 4 MiB).
        table_id:    id of the target <table>; must occur on exactly one
                     table element in the whole document.
        header_rows: how many of the first rows (document order) are
                     column headers; 0 means "no header".

    Returns:
        A :class:`TableExtraction`.  Repeating the call with identical
        input yields identical numbering and ordering.

    Raises:
        TableExtractError: any rejection (bad UTF-8, missing/duplicate
            table id, structure/span/layout violations, header
            inconsistencies, exceeded caps) with table id, source line
            and concrete reason attached.
    """
    if not isinstance(html_bytes, (bytes, bytearray, memoryview)):
        raise TypeError("html_bytes 必须是 bytes（原始字节，不是 str）")
    if not isinstance(table_id, str):
        raise TypeError("table_id 必须是 str")
    if isinstance(header_rows, bool) or not isinstance(header_rows, int):
        raise ValueError("header_rows 必须是非负整数")
    if header_rows < 0:
        raise ValueError("header_rows 必须是非负整数")

    root = _parse_document(bytes(html_bytes), table_id)
    table_el = _select_table(root, table_id)
    groups = _build_groups(table_el, table_id)

    n_rows = sum(len(g.rows) for g in groups)
    if n_rows > MAX_ROWS:
        raise ResultLimitError(
            f"表格共 {n_rows} 行，超过上限 {MAX_ROWS} 行",
            table_id=table_id, line=table_el.line, col=table_el.col)

    cells, row_new, n_cols = _layout(groups, n_rows, table_id)

    if n_cols > MAX_COLS:
        raise ResultLimitError(
            f"表格展开后共 {n_cols} 列，超过上限 {MAX_COLS} 列",
            table_id=table_id, line=table_el.line, col=table_el.col)
    if n_rows * n_cols > MAX_POSITIONS:
        raise ResultLimitError(
            f"表格展开后共 {n_rows}×{n_cols}={n_rows * n_cols} 个网格位置，"
            f"超过上限 {MAX_POSITIONS}",
            table_id=table_id, line=table_el.line, col=table_el.col)

    if header_rows > n_rows:
        raise HeaderRowsError(
            f"header_rows={header_rows} 超过表格实际行数（共 {n_rows} 行）",
            table_id=table_id, line=table_el.line, col=table_el.col)
    for cell in cells:
        last = cell.row + cell.span_rows - 1
        if cell.row < header_rows <= last:
            raise HeaderBoundaryError(
                f"单元格 <{cell.kind}>（第 {cell.line} 行）向下跨 "
                f"{cell.span_rows} 行，同时覆盖标题行与数据行"
                f"（header_rows={header_rows}，边界在第 {header_rows} 行）",
                table_id=table_id, line=cell.line, col=cell.col_offset)

    grid = _fill_grid(groups, row_new, n_rows, n_cols)
    row_groups = []
    base = 0
    for gi, group in enumerate(groups):
        row_groups.append(RowGroupInfo(
            index=gi, kind=group.kind, rows=len(group.rows), first_row=base))
        base += len(group.rows)

    return TableExtraction(
        table_id=table_id,
        header_rows=header_rows,
        n_rows=n_rows,
        n_cols=n_cols,
        row_groups=row_groups,
        source_cells=cells,
        grid=grid,
        header_paths=_header_paths(grid, cells, header_rows, n_cols),
        data_rows=_data_rows(grid, cells, header_rows, n_rows, n_cols),
    )
