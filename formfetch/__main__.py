"""Command line: one JSON config expresses the whole choice.

    python -m formfetch CONFIG.json
    python -m formfetch --preview CONFIG.json

Config schema (only ``page_url`` and ``form_id`` are required)::

    {
      "page_url":     "http://127.0.0.1:8000/export",
      "form_id":      "export-form",
      "submitter_id": "btn-download",        // optional; omit to submit
                                             // with no button value
      "modifications": {                     // optional
        "region":  "north",                  // string  -> text-like
        "agree":   true,                     // boolean -> checkbox/radio
        "kinds":   ["opt-temp", "opt-rain"], // list    -> select options
        "upload":  ["/data/query-ids.txt"]   // list    -> file paths
      },
      "output":       "/data/export.bin",    // required unless preview
      "preview_only": false,                 // stop before submitting
      "timeout": 30,
      "limits": {"page_bytes": 16777216,
                 "upload_bytes": 1073741824,
                 "result_bytes": 268435456},
      "boundary": "----formfetch-demo"       // optional, for tests
    }

The preview (ordered successful controls, target, method, enctype --
password values masked) is printed before any submission.  Exit codes:
0 ok, 2 known formfetch error, 1 unexpected failure.
"""

from __future__ import annotations

import argparse
import json
import sys

from .api import (Limits, fetch_page, prepare_submission, submit)
from .errors import ConfigError, FormFetchError
from .session import FormSession

_LIMIT_KEYS = ("page_bytes", "upload_bytes", "result_bytes")


def _limits_from_config(config):
    raw = config.get("limits", {})
    if not isinstance(raw, dict):
        raise ConfigError("'limits' must be an object")
    unknown = sorted(set(raw) - set(_LIMIT_KEYS))
    if unknown:
        raise ConfigError(f"unknown limits keys: {unknown}")
    values = {}
    for key, value in raw.items():
        if not isinstance(value, int) or isinstance(value, bool) \
                or value <= 0:
            raise ConfigError(f"limits.{key} must be a positive integer")
        values[key] = value
    return Limits(**values)


def run_from_config(config, *, preview_only=False):
    if not isinstance(config, dict):
        raise ConfigError("config must be a JSON object")
    page_url = config.get("page_url")
    if not isinstance(page_url, str) or not page_url:
        raise ConfigError("config 'page_url' (string) is required")
    form_id = config.get("form_id")
    if not isinstance(form_id, str) or not form_id:
        raise ConfigError("config 'form_id' (string) is required")
    submitter_id = config.get("submitter_id")
    if submitter_id is not None and not isinstance(submitter_id, str):
        raise ConfigError("config 'submitter_id' must be a string")
    timeout = config.get("timeout", 30.0)
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ConfigError("config 'timeout' must be a positive number")
    boundary = config.get("boundary")
    if boundary is not None and not isinstance(boundary, str):
        raise ConfigError("config 'boundary' must be a string")

    limits = _limits_from_config(config)
    session = FormSession(timeout=float(timeout))
    page = fetch_page(session, page_url, limits=limits)
    prepared = prepare_submission(
        page, form_id,
        modifications=config.get("modifications"),
        submitter_id=submitter_id,
        limits=limits,
        boundary=boundary)
    out = {"preview": prepared.describe()}
    if preview_only or config.get("preview_only"):
        out["submitted"] = False
        return out
    output = config.get("output")
    if not isinstance(output, str) or not output:
        raise ConfigError(
            "config 'output' (string) is required unless preview_only")
    result = submit(session, prepared, output, limits=limits)
    out["submitted"] = True
    out["result"] = result.to_dict()
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="formfetch",
        description="Form-based export fetching (see module docstring).")
    parser.add_argument("config", help="path to the JSON config")
    parser.add_argument("--preview", action="store_true",
                        help="stop before submitting (no submit request "
                             "is sent)")
    args = parser.parse_args(argv)
    try:
        with open(args.config, "r", encoding="utf-8") as fh:
            config = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": f"cannot read config: {exc}"}),
              file=sys.stderr)
        return 2
    try:
        result = run_from_config(config, preview_only=args.preview)
    except FormFetchError as exc:
        print(json.dumps({"error": str(exc),
                          "error_type": type(exc).__name__},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
