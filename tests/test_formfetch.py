"""Unit tests for formfetch's pure parts (parser, model, encoding).

The end-to-end story lives in demo/run_formfetch_drill.py; these tests
pin down the individual rules.
"""

from __future__ import annotations

import pytest

from formfetch import (FormPage, FormSession, Limits, fetch_page,
                       prepare_submission, submit)
from formfetch.api import PASSWORD_MASK
from formfetch.encode import (build_multipart, normalize_newlines,
                              urlencode_entries)
from formfetch.errors import (EncodeError, FormStructureError,
                              HtmlSubsetError, ModificationError,
                              OutputFileError, UnsupportedFeatureError)
from formfetch.forms import (FileEntry, FormDocument, TextEntry,
                             successful_entries)
from formfetch.htmldoc import parse_html
from formfetch.testsite import DATA_SHA256, ExportSite

PAGE = "http://example.test/export"


def model(html, url=PAGE):
    return FormDocument(parse_html(html), url)


def page_of(html, url=PAGE):
    return FormPage(url, None, model(html, url))


def entries_of(html, form_id="f", **kwargs):
    prepared = prepare_submission(page_of(html), form_id, **kwargs)
    return [(e.name, e.value) for e in prepared.entries
            if isinstance(e, TextEntry)]


# ---------------------------------------------------------------------------
# parser


def test_entities_in_text_and_attributes():
    doc = model('<form id="f"><input id="i" name="n" value="a&amp;b&#33;">'
                '<textarea id="t" name="t">x&amp;y</textarea></form>')
    assert doc.control_by_id("i").value == "a&b!"
    assert doc.control_by_id("t").value == "x&y"


def test_mismatched_end_tag():
    with pytest.raises(HtmlSubsetError):
        parse_html("<form><div></form></div>")


def test_unclosed_element():
    with pytest.raises(HtmlSubsetError):
        parse_html("<form><select></form>")


def test_void_element_end_tag():
    with pytest.raises(HtmlSubsetError):
        parse_html("<form><input></input></form>")


def test_nested_form():
    with pytest.raises(HtmlSubsetError):
        parse_html("<form id='a'><form id='b'></form></form>")


def test_textarea_must_contain_text():
    with pytest.raises(HtmlSubsetError):
        parse_html("<form><textarea><b>x</b></textarea></form>")


def test_select_structure():
    with pytest.raises(HtmlSubsetError):
        model("<form id='f'><select name='s'><div></div></select></form>")
    with pytest.raises(HtmlSubsetError):
        model("<form id='f'><select name='s'>text</select></form>")


# ---------------------------------------------------------------------------
# ownership


def test_ownership_form_attribute_before_and_after():
    doc = model(
        '<input id="pre" name="pre" form="f">'
        '<form id="f"><input id="in" name="in"></form>'
        '<textarea id="post" name="post" form="f"></textarea>')
    owners = {c.control_id: c.owner for c in doc.controls}
    form = doc.form_by_id("f")
    assert owners["pre"] is form and owners["post"] is form
    assert owners["in"] is form


def test_ownership_form_attribute_unknown_or_nonform():
    doc = model(
        '<form id="f"></form>'
        '<input id="a" name="a" form="nope">'
        '<div id="notaform"></div>'
        '<input id="b" name="b" form="notaform">'
        '<input id="c" name="c" form="">')
    owners = {c.control_id: c.owner for c in doc.controls}
    assert owners["a"] is None
    assert owners["b"] is None
    assert owners["c"] is None


def test_ownership_explicit_beats_ancestor():
    doc = model(
        '<form id="a"><input id="x" name="x" form="b"></form>'
        '<form id="b"></form>')
    assert doc.control_by_id("x").owner is doc.form_by_id("b")


def test_ownership_nearest_form_ancestor():
    doc = model('<form id="a"><div><input id="x" name="x"></div></form>')
    assert doc.control_by_id("x").owner is doc.form_by_id("a")


def test_duplicate_form_id():
    doc = model('<form id="f"></form><form id="f"></form>')
    with pytest.raises(FormStructureError):
        doc.form_by_id("f")


def test_duplicate_control_id():
    doc = model('<form id="f"><input id="d" name="a">'
                '<input id="d" name="b"></form>')
    with pytest.raises(FormStructureError):
        doc.control_by_id("d")


# ---------------------------------------------------------------------------
# disabled / fieldset


def test_fieldset_disabled_and_first_legend_exception():
    doc = model(
        '<form id="f">'
        '<fieldset disabled>'
        '<legend>l<input id="inlegend" name="a"></legend>'
        '<input id="plain" name="b">'
        '<fieldset><input id="nested" name="c"></fieldset>'
        '</fieldset>'
        '<fieldset disabled>'
        '<legend>first</legend>'
        '<legend>second<input id="secondleg" name="d"></legend>'
        '</fieldset>'
        '<fieldset disabled>'
        '<legend>o<fieldset disabled>'
        '<input id="innerdis" name="e"></fieldset></legend>'
        '</fieldset>'
        '</form>')
    disabled = {c.control_id: c.disabled for c in doc.controls}
    assert disabled["inlegend"] is False   # first legend exempts its own
    assert disabled["plain"] is True
    assert disabled["nested"] is True      # outer fieldset still applies
    assert disabled["secondleg"] is True   # only the *first* legend
    assert disabled["innerdis"] is True    # nested fieldset judged itself


def test_legend_must_be_direct_child():
    doc = model(
        '<form id="f"><fieldset disabled>'
        '<div><legend><input id="x" name="x"></legend></div>'
        '</fieldset></form>')
    assert doc.control_by_id("x").disabled is True


# ---------------------------------------------------------------------------
# successful controls


def test_radio_last_checked_wins_initially():
    got = entries_of(
        '<form id="f">'
        '<input type="radio" name="r" value="1" checked>'
        '<input type="radio" name="r" value="2" checked>'
        '</form>')
    assert got == [("r", "2")]


def test_checkbox_defaults_and_empty_value():
    got = entries_of(
        '<form id="f">'
        '<input type="checkbox" name="a" checked>'
        '<input type="checkbox" name="b" value="" checked>'
        '<input type="checkbox" name="c">'
        '</form>')
    assert got == [("a", "on"), ("b", "")]


def test_select_single_defaults_to_first_available():
    got = entries_of(
        '<form id="f"><select name="s">'
        '<option value="no" disabled>x</option>'
        '<option value="yes">y</option>'
        '</select></form>')
    assert got == [("s", "yes")]


def test_select_single_multiple_selected_last_available():
    got = entries_of(
        '<form id="f"><select name="s">'
        '<option value="1" selected>a</option>'
        '<option value="2" selected>b</option>'
        '</select></form>')
    assert got == [("s", "2")]


def test_select_multiple_none_selected_sends_nothing():
    got = entries_of(
        '<form id="f"><select name="s" multiple>'
        '<option value="1">a</option></select></form>')
    assert got == []


def test_select_optgroup_disabled():
    got = entries_of(
        '<form id="f"><select name="s" multiple>'
        '<optgroup disabled><option value="1" selected>a</option>'
        '</optgroup>'
        '<option value="2" selected>b</option>'
        '</select></form>')
    assert got == [("s", "2")]


def test_option_without_value_uses_text():
    got = entries_of(
        '<form id="f"><select name="s">'
        '<option selected>  hello  </option></select></form>')
    assert got == [("s", "hello")]


def test_document_order_and_skips():
    html = ('<input type="text" name="pre" value="0" form="f">'
            '<form id="f">'
            '<input type="text" name="a" value="1">'
            '<input type="text" value="skip">'
            '<input type="text" name="" value="skip">'
            '<input type="text" name="b" value="2" disabled>'
            '<input type="submit" id="go" name="go" value="g">'
            '<input type="reset" name="rst" value="r">'
            '</form>'
            '<input type="text" name="post" value="3" form="f">')
    # no clicked submitter: the submit button contributes nothing
    assert entries_of(html) == [("pre", "0"), ("a", "1"), ("post", "3")]
    # clicked: the button joins at its own document position
    assert entries_of(html, submitter_id="go") == [
        ("pre", "0"), ("a", "1"), ("go", "g"), ("post", "3")]


def test_submitter_none_means_no_button_value():
    got = entries_of(
        '<form id="f"><input type="submit" name="go" value="g"></form>')
    assert got == []


def test_duplicate_names_kept_in_order():
    got = entries_of(
        '<form id="f"><input name="t" value="a"><input name="t" value="b">'
        '</form>')
    assert got == [("t", "a"), ("t", "b")]


# ---------------------------------------------------------------------------
# modifications


def mod_page():
    return page_of(
        '<form id="f">'
        '<input type="text" id="t" name="t" value="d">'
        '<input type="checkbox" id="c" name="c">'
        '<input type="radio" id="r1" name="r" value="1">'
        '<input type="radio" id="r2" name="r" value="2" checked>'
        '<select id="s" name="s">'
        '<option id="o1" value="1" selected>a</option>'
        '<option id="o2" value="2">b</option>'
        '</select>'
        '<select id="m" name="m" multiple>'
        '<option id="m1" value="x">x</option>'
        '<option id="m2" value="y">y</option>'
        '</select>'
        '<input type="file" id="file" name="file">'
        '<input type="text" id="dis" name="dis" disabled>'
        '</form>'
        '<form id="g"><input id="other" name="t"></form>')


def test_modification_effects():
    page = mod_page()
    prepared = prepare_submission(
        page, "f",
        modifications={"t": "new", "c": True, "r1": True, "s": ["o2"],
                       "m": ["m2", "m1"]})
    got = [(e.name, e.value) for e in prepared.entries
           if isinstance(e, TextEntry)]
    # multi-select values come out in the options' document order,
    # not in the order the modification listed them
    assert got == [("t", "new"), ("c", "on"), ("r", "1"), ("s", "2"),
                   ("m", "x"), ("m", "y")]


def test_modification_radio_false_only_unchecks_itself():
    page = mod_page()
    prepared = prepare_submission(page, "f", modifications={"r2": False})
    assert [(e.name, e.value) for e in prepared.entries
            if isinstance(e, TextEntry) and e.name == "r"] == []


def test_modification_errors():
    page = mod_page()
    with pytest.raises(ModificationError):
        prepare_submission(page, "f", modifications={"other": "x"})
    with pytest.raises(ModificationError):
        prepare_submission(page, "f", modifications={"dis": "x"})
    with pytest.raises(ModificationError):
        prepare_submission(page, "f",
                           modifications={"r1": True, "r2": True})
    with pytest.raises(ModificationError):
        prepare_submission(page, "f", modifications={"s": ["o1", "o2"]})
    with pytest.raises(ModificationError):
        prepare_submission(page, "f", modifications={"s": ["nope"]})
    with pytest.raises(ModificationError):
        prepare_submission(page, "f",
                           modifications={"file": ["/a", "/b"]})
    with pytest.raises(ModificationError):
        prepare_submission(page, "f",
                           modifications={"file": ["/no/such/file"]})
    with pytest.raises(FormStructureError):
        prepare_submission(page, "f", modifications={"nope": "x"})


def test_modification_of_failed_prepare_leaves_no_trace():
    page = mod_page()
    with pytest.raises(FormStructureError):
        prepare_submission(page, "f",
                           modifications={"t": "changed", "nope": "x"})
    # the valid-looking part must not have been applied either
    prepared = prepare_submission(page, "f")
    assert [(e.name, e.value) for e in prepared.entries
            if isinstance(e, TextEntry) and e.name == "t"] == [("t", "d")]


# ---------------------------------------------------------------------------
# target resolution


def test_base_first_wins_and_action_resolution():
    page = page_of(
        '<html><head><base href="http://example.test/a/">'
        '<base href="http://example.test/ignored/"></head>'
        '<body><form id="f" action="go" method="post"></form></body>'
        '</html>')
    prepared = prepare_submission(page, "f")
    assert prepared.url == "http://example.test/a/go"


def test_empty_action_returns_to_page_url():
    page = page_of('<form id="f" action="" method="post"></form>')
    assert prepare_submission(page, "f").url == PAGE


def test_get_replaces_query_keeps_path():
    page = page_of(
        '<form id="f" action="http://example.test/x/y?old=1" method="get">'
        '<input name="a" value="1"></form>')
    prepared = prepare_submission(page, "f")
    assert prepared.url == "http://example.test/x/y?a=1"


def test_post_keeps_action_query():
    page = page_of(
        '<form id="f" action="http://example.test/x?keep=1" method="post">'
        '<input name="a" value="1"></form>')
    prepared = prepare_submission(page, "f")
    assert prepared.url == "http://example.test/x?keep=1"
    assert prepared.body == b"a=1"


def test_submitter_overrides():
    page = page_of(
        '<form id="f" action="a" method="post">'
        '<input type="submit" id="s" name="go" value="g" '
        'formaction="b?x=1" formmethod="get"></form>')
    prepared = prepare_submission(page, "f", submitter_id="s")
    assert prepared.method == "GET"
    assert prepared.url == "http://example.test/b?go=g"


def test_unsupported_method_and_enctype():
    with pytest.raises(UnsupportedFeatureError):
        prepare_submission(
            page_of('<form id="f" method="dialog"></form>'), "f")
    with pytest.raises(UnsupportedFeatureError):
        prepare_submission(
            page_of('<form id="f" method="post" '
                    'enctype="text/plain"></form>'), "f")


def test_files_require_post_multipart(tmp_path):
    upload = tmp_path / "u.bin"
    upload.write_bytes(b"x")
    with pytest.raises(UnsupportedFeatureError):
        prepare_submission(
            page_of('<form id="f" method="get">'
                    '<input type="file" id="u" name="u"></form>'),
            "f", modifications={"u": [str(upload)]})
    with pytest.raises(UnsupportedFeatureError):
        prepare_submission(
            page_of('<form id="f" method="post">'
                    '<input type="file" id="u" name="u"></form>'),
            "f", modifications={"u": [str(upload)]})


# ---------------------------------------------------------------------------
# encoding


def test_normalize_newlines():
    assert normalize_newlines("a\nb\r\nc\rd") == "a\r\nb\r\nc\r\nd"


def test_urlencode_utf8_plus_duplicates():
    body = urlencode_entries([
        TextEntry("a", "x y", None),
        TextEntry("a", "z", None),
        TextEntry("名", "值\n", None),
    ])
    assert body == ("a=x+y&a=z&%E5%90%8D=%E5%80%BC%0D%0A").encode("ascii")


def test_multipart_exact_bytes(tmp_path):
    upload = tmp_path / "a.bin"
    upload.write_bytes(b"\x00\r\nx")
    entries = [
        TextEntry("t", "v1\nv2", None),
        FileEntry("f", "a.bin", str(upload), 3, None),
        FileEntry("empty", "", None, 0, None),
    ]
    body = build_multipart(entries, "X")
    expected = (
        b'--X\r\nContent-Disposition: form-data; name="t"\r\n\r\n'
        b'v1\r\nv2\r\n'
        b'--X\r\nContent-Disposition: form-data; name="f"; '
        b'filename="a.bin"\r\n'
        b'Content-Type: application/octet-stream\r\n\r\n'
        b'\x00\r\nx\r\n'
        b'--X\r\nContent-Disposition: form-data; name="empty"; '
        b'filename=""\r\n'
        b'Content-Type: application/octet-stream\r\n\r\n'
        b'\r\n'
        b'--X--\r\n')
    assert body.content_length == len(expected)
    assert b"".join(body.iter_chunks()) == expected


def test_multipart_escaping_and_crlf_rejection(tmp_path):
    upload = tmp_path / 'we"ird\\name.bin'
    upload.write_bytes(b"")
    entries = [FileEntry("f", upload.name, str(upload), 0, None)]
    body = build_multipart(entries, "X")
    assert b'filename="we\\"ird\\\\name.bin"' in \
        b"".join(body.iter_chunks())
    with pytest.raises(EncodeError):
        build_multipart([TextEntry("bad\rname", "v", None)], "X")


def test_password_masked_in_describe():
    page = page_of(
        '<form id="f" method="get">'
        '<input type="password" id="p" name="pw" value=""></form>')
    prepared = prepare_submission(page, "f",
                                  modifications={"p": "topsecret"})
    described = prepared.describe()
    assert "topsecret" not in str(described)
    assert described["entries"][0]["value"] == PASSWORD_MASK
    assert "topsecret" not in described["url"]


def test_output_exists_refused_before_network(tmp_path):
    page = page_of('<form id="f" method="get"></form>')
    prepared = prepare_submission(page, "f")
    existing = tmp_path / "out.bin"
    existing.write_bytes(b"old")
    with pytest.raises(OutputFileError):
        submit(None, prepared, existing)   # session never touched
    assert existing.read_bytes() == b"old"


# ---------------------------------------------------------------------------
# one integration round trip (the full story is the drill)


def test_integration_round_trip(tmp_path):
    site = ExportSite(tmp_path / "site").start()
    try:
        session = FormSession()
        page = fetch_page(session, site.url + "/export")
        prepared = prepare_submission(
            page, "export-form", modifications={"agree": True},
            submitter_id="btn-download", boundary="pytest-boundary")
        result = submit(session, prepared, tmp_path / "out.bin")
        assert result.status == 200
        assert result.sha256 == DATA_SHA256
        assert site.receipts[-1]["kind"] == "multipart"
    finally:
        site.stop()
