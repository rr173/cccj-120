"""sitemapdiscover — discover fetchable pages from sitemap roots (offline).

Given root sitemap URLs and a mapping of already-downloaded URL -> local
file, walk sitemap indexes depth-first to the real pages and produce a
deduplicated, source-annotated fetch list.  Nothing is downloaded,
robots.txt is never consulted, and page contents are never parsed.

Quick start::

    from sitemapdiscover import discover_to_report

    summary = discover_to_report(
        roots=["https://example.com/sitemap_index.xml"],
        mapping_path="mapping.json",
        report_path="report.jsonl",
    )
    print(summary["complete"], summary["counts"]["pages"])

Memory: bulk state (maps, pages, sources, edges) lives in a temporary
SQLite file created for this run and deleted on close.  Python holds the
active DFS stack, one parser buffer and one staged entry at a time;
results are streamed with ``Discovery.iter_pages()``.
"""

from .engine import (
    Discovery,
    DiscoveryReport,
    MapEdge,
    MapNode,
    PageResult,
    PageSource,
)
from .errors import (
    ConfigurationError,
    LimitsError,
    MapRejection,
    MappingConflictError,
    SitemapDiscoverError,
    URLRuleError,
)
from .mapping import DownloadMapping, MappingEntry
from .profile import Limits
from .reader import EntryRecord, MapSummary, parse_map_file
from .urls import ParsedURL, canonicalize, parse_origin, parse_url

__all__ = [
    "Discovery",
    "DiscoveryReport",
    "MapNode",
    "MapEdge",
    "PageResult",
    "PageSource",
    "DownloadMapping",
    "MappingEntry",
    "Limits",
    "parse_url",
    "parse_origin",
    "canonicalize",
    "ParsedURL",
    "parse_map_file",
    "EntryRecord",
    "MapSummary",
    "SitemapDiscoverError",
    "ConfigurationError",
    "MappingConflictError",
    "URLRuleError",
    "MapRejection",
    "LimitsError",
    "discover_to_report",
]

__version__ = "1.0.0"


def discover_to_report(roots, mapping_path, report_path,
                       limits=None, *, format="jsonl",
                       include_sources=True):
    """One-call convenience wrapper used by the CLI and by callers.

    Opens the mapping, runs the discovery, streams the report to
    ``report_path``, and returns the summary dictionary (the same one
    stored under ``"report"`` in the report file).  The temporary SQLite
    scratch directory is always removed before returning.
    """
    mapping = DownloadMapping.from_file(mapping_path)
    discovery = Discovery(roots, mapping, limits)
    try:
        discovery.run()
        summary = discovery.report.to_dict()
        with open(report_path, "w", encoding="utf-8") as fh:
            discovery.write_report(fh, format=format,
                                   include_sources=include_sources)
        summary["report_path"] = report_path
        return summary
    finally:
        discovery.close()
