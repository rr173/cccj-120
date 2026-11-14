"""Regression tests for tableextract.  Run: python3 -m pytest tests/ -q"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tableextract import (
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
    extract_table,
)

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "demo" / "table_fixtures"


def load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# ----------------------------------------------------------------------
# regional_stats: rowspan region column + two-level colspan header
# ----------------------------------------------------------------------

def test_regional_grid_and_dimensions():
    res = extract_table(load("regional_stats.html"), "regional-stats", 2)
    assert (res.n_rows, res.n_cols) == (5, 6)
    assert len(res.source_cells) == 25
    assert res.grid == [
        [0, 1, 2, 2, 3, 3],
        [0, 1, 4, 5, 6, 7],
        [8, 9, 10, 11, 12, 13],
        [8, 14, 15, 16, 17, 18],
        [19, 20, 21, 22, 23, 24],
    ]
    assert [(g.kind, g.rows) for g in res.row_groups] == [("thead", 2),
                                                          ("tbody", 3)]


def test_regional_header_paths():
    res = extract_table(load("regional_stats.html"), "regional-stats", 2)
    assert [[e.text for e in p] for p in res.header_paths] == [
        ["地区"], ["省份"],
        ["2023年", "GDP （亿元）"], ["2023年", "人口（万人）"],
        ["2024年", "GDP （亿元）"], ["2024年", "人口（万人）"],
    ]
    # 跨两列的“2023年”进入列 2、3 两列的路径；跨行的“地区”只收一次
    assert [e.cell for e in res.header_paths[2]] == [2, 4]
    assert [e.cell for e in res.header_paths[0]] == [0]


def test_regional_data_rows_and_traceability():
    res = extract_table(load("regional_stats.html"), "regional-stats", 2)
    assert res.data_rows[1].values == ["华东", "浙江", "77715.4", "6577",
                                       "80100.0", "6620"]
    assert res.data_rows[1].cells == [8, 14, 15, 16, 17, 18]
    east = res.source_cells[8]
    assert (east.kind, east.text, east.row, east.col,
            east.span_rows, east.span_cols) == ("td", "华东", 2, 0, 2, 1)
    assert (east.line, east.col_offset) == (30, 8)
    # tbody 内的 th 保留为数据，不进标题路径
    assert res.source_cells[9].kind == "th"
    assert all(e.cell < 8 for p in res.header_paths for e in p)


def test_repeated_extraction_is_stable():
    raw = load("regional_stats.html")
    a = extract_table(raw, "regional-stats", 2)
    b = extract_table(raw, "regional-stats", 2)
    assert json.dumps(a.to_dict(), sort_keys=True) == \
        json.dumps(b.to_dict(), sort_keys=True)


# ----------------------------------------------------------------------
# rowspan=0 / row groups
# ----------------------------------------------------------------------

def test_rowspan_zero_stops_at_tbody_boundary():
    res = extract_table(load("rowspan_zero.html"), "zero-span", 0)
    assert res.grid == [[0, 1], [0, 2], [0, 3], [4, 5], [6, 7]]
    assert res.source_cells[0].span_rows == 3
    assert res.source_cells[res.grid[3][0]].text == "乙一"
    assert [g.kind for g in res.row_groups] == ["tbody", "tbody"]
    assert all(path == [] for path in res.header_paths)


def test_rowspan_zero_in_last_row_spans_one_row():
    res = extract_table(
        b'<table id="x"><tr><td>a</td><td rowspan="0">z</td></tr></table>',
        "x", 0)
    assert res.source_cells[1].span_rows == 1


def test_positive_rowspan_beyond_group_rejected():
    with pytest.raises(RowspanOutOfGroupError):
        extract_table(
            b'<table id="x"><tbody><tr><td rowspan="3">a</td></tr>'
            b'<tr><td>b</td></tr></tbody></table>', "x", 0)


def test_rowspan_never_flows_into_next_group():
    # 隐式行组 1 行 + tbody 1 行：rowspan=2 越出隐式组即拒绝
    with pytest.raises(RowspanOutOfGroupError):
        extract_table(
            b'<table id="x"><tr><td rowspan="2">a</td></tr>'
            b'<tbody><tr><td>b</td></tr></tbody></table>', "x", 0)


# ----------------------------------------------------------------------
# nested tables / text semantics / gaps
# ----------------------------------------------------------------------

def test_nested_tables_extract_independently():
    raw = load("nested_tables.html")
    outer = extract_table(raw, "outer-report", 1)
    assert (outer.n_rows, outer.n_cols) == (2, 2)
    assert outer.source_cells[3].text == "见下表： （完）"
    inner = extract_table(raw, "inner-detail", 1)
    assert [d.values for d in inner.data_rows] == [["甲", "10"], ["乙", "20"]]


def test_text_semantics():
    res = extract_table(load("text_semantics.html"), "text-cells", 1)
    assert res.source_cells[3].text == "煤&电 合计： 1234 吨"
    assert res.source_cells[5].text == "a b  c"
    assert res.source_cells[7].text == "前后尾"


def test_empty_cell_vs_null_gap():
    res = extract_table(load("gaps.html"), "gaps", 0)
    assert res.grid == [[0, 1, 2], [3, 4, None], [5, None, None]]
    assert res.data_rows[0].values == ["A", "", "C"]
    assert res.data_rows[0].cells == [0, 1, 2]
    assert res.data_rows[1].values == ["D", "E", None]
    assert res.data_rows[1].cells == [3, 4, None]
    assert res.source_cells[1].text == ""


def test_same_text_different_cells_both_kept_in_header_path():
    res = extract_table(
        '<table id="d"><tr><th>名称</th></tr><tr><th>名称</th></tr>'
        '<tr><td>v</td></tr></table>'.encode("utf-8"), "d", 2)
    assert [[e.text for e in p] for p in res.header_paths] == [["名称", "名称"]]
    assert [e.cell for e in res.header_paths[0]] == [0, 1]


# ----------------------------------------------------------------------
# layout conflicts
# ----------------------------------------------------------------------

def test_colspan_hitting_rowspan_placeholder_rejected():
    with pytest.raises(LayoutConflictError) as excinfo:
        extract_table(load("conflict.html"), "conflict", 0)
    exc = excinfo.value
    assert (exc.conflict_row, exc.conflict_col) == (1, 1)
    assert exc.existing_cell_line == 6


def test_no_partial_result_on_conflict():
    try:
        extract_table(load("conflict.html"), "conflict", 0)
    except LayoutConflictError:
        return
    pytest.fail("冲突未被拒绝")


# ----------------------------------------------------------------------
# structure / spans / ids / utf-8 rejections
# ----------------------------------------------------------------------

@pytest.mark.parametrize("html", [
    b'<table id="x"><tr><td>a</tr></table>',           # missing </td>
    b'<table id="x"><tr><td>a</td></tr>',              # unclosed at EOF
    b'<table id="x"><td>a</td></table>',               # td under table
    b'<table id="x"><tr><tr><td>a</td></tr></tr></table>',  # tr in tr
    b'<table id="x">hello<tr><td>a</td></tr></table>',  # text under table
    b'<table id="x"><tr><td><span><td>b</td></span></td></tr></table>',
    b'<table id="x"><thead><td>a</td></thead></table>',  # td under thead
])
def test_structure_rejections(html):
    with pytest.raises(HtmlStructureError):
        extract_table(html, "x", 0)


@pytest.mark.parametrize("attr", [
    'rowspan="-1"', 'rowspan="1.5"', 'rowspan="２"', 'rowspan=""',
    'rowspan', 'rowspan="1" rowspan="2"',
    'colspan="0"', 'colspan="-2"', 'colspan="2."',
])
def test_span_rejections(attr):
    html = ("<table id='x'><tr><td " + attr
            + ">a</td></tr></table>").encode("utf-8")
    with pytest.raises(SpanAttributeError):
        extract_table(html, "x", 0)


def test_span_with_surrounding_ascii_whitespace_accepted():
    res = extract_table(
        b'<table id="x"><tr><td rowspan=" 2 ">a</td></tr>'
        b'<tr><td>b</td></tr></table>', "x", 0)
    assert res.source_cells[0].span_rows == 2
    assert res.grid == [[0, None], [0, 1]]


def test_table_id_must_be_unique():
    html = (b'<table id="x"><tr><td>1</td></tr></table>'
            b'<table id="x"><tr><td>2</td></tr></table>')
    with pytest.raises(DuplicateTableIdError):
        extract_table(html, "x", 0)


def test_table_id_not_found_lists_available():
    with pytest.raises(TableNotFoundError) as excinfo:
        extract_table(b'<table id="a"><tr><td>1</td></tr></table>', "ghost", 0)
    assert "'a'" in str(excinfo.value)


def test_table_id_value_is_case_sensitive():
    with pytest.raises(TableNotFoundError):
        extract_table(b'<table id="UP"><tr><td>a</td></tr></table>', "up", 0)


def test_tags_and_attribute_names_are_case_insensitive():
    res = extract_table(
        b'<TABLE ID="x"><TR><TD ROWSPAN="2">a</TD></TR>'
        b'<TR><TD>b</TD></TR></TABLE>', "x", 0)
    assert res.source_cells[0].span_rows == 2


def test_invalid_utf8_rejected_with_position():
    with pytest.raises(InvalidUtf8Error) as excinfo:
        extract_table(b'<table id="x">\n<tr><td>\xff</td></tr></table>', "x", 0)
    assert excinfo.value.line == 2


def test_header_rows_exceeding_rows_rejected():
    with pytest.raises(HeaderRowsError):
        extract_table(b'<table id="x"><tr><td>a</td></tr></table>', "x", 2)


def test_rowspan_crossing_header_data_boundary_rejected():
    with pytest.raises(HeaderBoundaryError):
        extract_table(
            b'<table id="x"><tr><th rowspan="2">h</th></tr>'
            b'<tr><td>d</td></tr></table>', "x", 1)


def test_rowspan_within_header_allowed():
    res = extract_table(
        b'<table id="x"><tr><th rowspan="2">h</th><th>a</th></tr>'
        b'<tr><th>b</th></tr><tr><td>c</td><td>d</td></tr></table>', "x", 2)
    assert res.source_cells[0].span_rows == 2
    assert len(res.data_rows) == 1


@pytest.mark.parametrize("bad", [-1, 2.5, True, "1"])
def test_header_rows_argument_validation(bad):
    with pytest.raises((ValueError, TypeError)):
        extract_table(b"<table id='x'/>", "x", bad)


# ----------------------------------------------------------------------
# caps
# ----------------------------------------------------------------------

def test_input_size_cap():
    with pytest.raises(InputTooLargeError):
        extract_table(b"x" * (4 * 1024 * 1024 + 1), "x", 0)


def test_row_cap():
    html = ("<table id='x'>" + "<tr><td>a</td></tr>" * 10001
            + "</table>").encode()
    with pytest.raises(ResultLimitError):
        extract_table(html, "x", 0)


def test_col_cap():
    html = ("<table id='x'><tr>" + "<td>a</td>" * 257
            + "</tr></table>").encode()
    with pytest.raises(ResultLimitError):
        extract_table(html, "x", 0)


def test_grid_position_cap():
    html = ("<table id='x'>" + "<tr><td colspan='201'>a</td></tr>" * 10000
            + "</table>").encode()
    with pytest.raises(ResultLimitError):
        extract_table(html, "x", 0)


def test_caps_boundary_accepted():
    html = ("<table id='x'>" + "<tr><td>a</td></tr>" * 10000
            + "</table>").encode()
    res = extract_table(html, "x", 0)
    assert (res.n_rows, res.n_cols) == (10000, 1)


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def run_cli(*args):
    return subprocess.run([sys.executable, "-m", "tableextract", *args],
                          cwd=REPO, capture_output=True, text=True)


def test_cli_success_outputs_json():
    proc = run_cli(str(FIXTURES / "regional_stats.html"),
                   "--table-id", "regional-stats", "--header-rows", "2")
    assert proc.returncode == 0
    doc = json.loads(proc.stdout)
    assert (doc["rows"], doc["cols"]) == (5, 6)
    assert doc["data_rows"][0]["values"][0] == "华东"


def test_cli_failure_leaves_stdout_empty():
    proc = run_cli(str(FIXTURES / "conflict.html"), "--table-id", "conflict")
    assert proc.returncode != 0
    assert proc.stdout == ""
    assert "冲突" in proc.stderr


def test_cli_unknown_id_fails():
    proc = run_cli(str(FIXTURES / "gaps.html"), "--table-id", "ghost")
    assert proc.returncode != 0
    assert proc.stdout == ""
