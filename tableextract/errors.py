"""Exception hierarchy for tableextract.

Every failure carries the target table id plus, where known, the source
position (1-based line, 0-based column offset) and a concrete reason, so
a rejected page can be traced back to the offending tag in the original
HTML.  Extraction is all-or-nothing: the caller either gets a complete
:class:`~tableextract.extractor.TableExtraction` or one of these errors;
nothing partial is ever produced.
"""

from __future__ import annotations


class TableExtractError(Exception):
    """Base class for all tableextract failures.

    Attributes:
        message:  concrete reason (what rule was violated).
        table_id: the requested target table id, when a call context exists.
        line:     1-based source line of the offending tag/bytes, if known.
        col:      0-based column offset within that line, if known.
    """

    def __init__(self, message, *, table_id=None, line=None, col=None):
        super().__init__(message)
        self.message = message
        self.table_id = table_id
        self.line = line
        self.col = col

    def __str__(self):
        where = []
        if self.table_id is not None:
            where.append(f"table {self.table_id!r}")
        if self.line is not None:
            loc = f"line {self.line}"
            if self.col is not None:
                loc += f", col {self.col}"
            where.append(loc)
        prefix = "; ".join(where)
        return f"[{prefix}] {self.message}" if prefix else self.message


class InputTooLargeError(TableExtractError):
    """The raw HTML input exceeds the 4 MiB cap (checked before decoding)."""


class InvalidUtf8Error(TableExtractError):
    """The input bytes are not valid UTF-8."""


class TableNotFoundError(TableExtractError):
    """No <table> element in the whole document carries the requested id."""


class DuplicateTableIdError(TableExtractError):
    """More than one <table> element in the document carries the id."""


class HtmlStructureError(TableExtractError):
    """The explicitly-closed HTML subset is violated.

    Covers: mismatched/missing close tags anywhere in the document, and
    inside the selected table: text or tags directly under table/row
    group/tr where only structural children are allowed, and structural
    tags (tr/td/th/row groups) nested inside a cell without an
    intervening <table>.
    """


class SpanAttributeError(TableExtractError):
    """A rowspan/colspan attribute is duplicated, has no value, or is not
    a decimal non-negative integer after stripping ASCII whitespace
    (colspan must additionally be > 0)."""


class LayoutConflictError(TableExtractError):
    """A cell's declared span would cover a grid position already held by
    an earlier row-spanning cell.

    Extra attributes: ``conflict_row``/``conflict_col`` (0-based expanded
    grid coordinate of the first overlapped position) and
    ``existing_cell_line`` (source line of the cell already holding it).
    The incoming cell is never allowed to overwrite the earlier one.
    """

    def __init__(self, message, *, conflict_row=None, conflict_col=None,
                 existing_cell_line=None, **kwargs):
        super().__init__(message, **kwargs)
        self.conflict_row = conflict_row
        self.conflict_col = conflict_col
        self.existing_cell_line = existing_cell_line


class RowspanOutOfGroupError(TableExtractError):
    """A positive rowspan reaches past the last row of its own row group."""


class HeaderRowsError(TableExtractError):
    """header_rows exceeds the actual number of rows in the table."""


class HeaderBoundaryError(TableExtractError):
    """A row-spanning cell would cover both header and data rows."""


class ResultLimitError(TableExtractError):
    """The expanded grid would exceed the row / column / grid-position
    caps (reported before the grid is allocated)."""
