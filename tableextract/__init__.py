"""tableextract — offline HTML table extractor (standard library only).

Reads local UTF-8 HTML bytes and restores one explicitly-closed
``<table>`` -- selected by an id that must be unique among all table
elements in the document -- into rectangular data, keeping for every
value a reference to the real source cell it came from (td/th kind,
text, row group, top-left grid coordinate, actual expanded span, and
the start tag's line/column offset in the original HTML).

The module never touches the network and is not connected to the
archive store, the WARC fact base, the event ledger, or the robots
cache; it is a pure local-bytes-in, structured-data-out component.

Public API:
    extract_table        html_bytes + table id + header_rows -> TableExtraction
    TableExtraction      dimensions, source cells, reference grid,
                         header paths, expanded data rows (see .to_dict())
    SourceCell           one real td/th, numbered in document order
    RowGroupInfo         one thead/tbody/tfoot/implicit row group
    HeaderEntry          one cell contributing to a column's header path
    DataRow              one expanded data row (values + cell references)
    TableExtractError    base class of every rejection (see errors.py)

CLI:
    python -m tableextract FILE --table-id ID [--header-rows N]
"""

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
    TableExtractError,
    TableNotFoundError,
)
from .extractor import (
    MAX_COLS,
    MAX_INPUT_BYTES,
    MAX_POSITIONS,
    MAX_ROWS,
    DataRow,
    HeaderEntry,
    RowGroupInfo,
    SourceCell,
    TableExtraction,
    extract_table,
)

__all__ = [
    "extract_table",
    "TableExtraction",
    "SourceCell",
    "RowGroupInfo",
    "HeaderEntry",
    "DataRow",
    "MAX_INPUT_BYTES",
    "MAX_ROWS",
    "MAX_COLS",
    "MAX_POSITIONS",
    "TableExtractError",
    "InputTooLargeError",
    "InvalidUtf8Error",
    "TableNotFoundError",
    "DuplicateTableIdError",
    "HtmlStructureError",
    "SpanAttributeError",
    "LayoutConflictError",
    "RowspanOutOfGroupError",
    "HeaderRowsError",
    "HeaderBoundaryError",
    "ResultLimitError",
]

__version__ = "1.0.0"
