"""Form and control model: ownership, disabled state, successful controls.

The rules implemented here follow the HTML form-submission algorithm,
with the task-specific clarifications noted in the module README:

* ownership: an explicit ``form`` attribute wins; if it names no form
  (missing id, or an element that is not a form) the control belongs to
  no form; otherwise the nearest ``form`` ancestor owns the control;
* parameter order is the document order of the controls, wherever they
  physically appear (associated controls may sit before or after the
  form);
* disabled: own ``disabled`` attribute, or a disabled ``fieldset``
  ancestor -- except controls inside that fieldset's *first direct*
  ``legend`` child; nested fieldsets are judged independently;
* radio groups are per (owning form, name); if several start out
  checked, the last one in document order wins;
* select: single without an explicit selection submits the first
  non-disabled option; multiple without a selection submits nothing; a
  single select with several selected options submits the last
  non-disabled one; an option without ``value`` submits its text.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urldefrag, urljoin

from .errors import (FormStructureError, HtmlSubsetError, ModificationError,
                     UnsupportedFeatureError)
from .htmldoc import Document, Element

URLENCODED = "application/x-www-form-urlencoded"
MULTIPART = "multipart/form-data"

_BUTTON_INPUT_TYPES = frozenset({"submit", "reset", "button", "image"})
_SPECIAL_INPUT_TYPES = _BUTTON_INPUT_TYPES | {"checkbox", "radio", "file"}
_CONTROL_TAGS = frozenset({"input", "textarea", "select", "button"})


# ---------------------------------------------------------------------------
# model


class Option:
    def __init__(self, element, disabled):
        self.element = element
        self.disabled = disabled
        self.selected = "selected" in element.attrs
        if "value" in element.attrs:
            self.value = element.attrs["value"]
        else:
            # option without value submits its text (stripped)
            self.value = element.text_content().strip()

    @property
    def option_id(self):
        return self.element.attrs.get("id")


class Control:
    """One input/textarea/select/button element plus its live state."""

    def __init__(self, element):
        self.element = element
        self.kind = element.tag            # input|textarea|select|button
        self.owner = None                  # Form | None
        self.disabled = False
        self.value = ""
        self.checked = False
        self.multiple = False
        self.options = []                  # select only: list[Option]
        self.files = []                    # file input: selected local paths
        self.explicit_selection = None     # select: option ids from a
                                           # modification, else None
        if self.kind == "input":
            self.input_type = (element.attrs.get("type", "text")
                               .strip().lower() or "text")
            self.value = element.attrs.get("value", "")
            self.checked = "checked" in element.attrs
            self.multiple = "multiple" in element.attrs
        elif self.kind == "textarea":
            self.input_type = None
            self.value = element.text_content()
        elif self.kind == "button":
            self.input_type = None
            self.button_type = (element.attrs.get("type", "submit")
                                .strip().lower())
            if self.button_type not in ("submit", "reset", "button"):
                self.button_type = "submit"
            self.value = element.attrs.get("value", "")
        else:  # select
            self.input_type = None
            self.multiple = "multiple" in element.attrs

    # -- identity -----------------------------------------------------

    @property
    def control_id(self):
        return self.element.attrs.get("id")

    @property
    def name(self):
        return self.element.attrs.get("name")

    # -- classification -------------------------------------------------

    @property
    def is_password(self):
        return self.kind == "input" and self.input_type == "password"

    @property
    def is_text_like(self):
        # Unknown/invalid input types behave as type=text (HTML default).
        if self.kind == "textarea":
            return True
        return (self.kind == "input"
                and self.input_type not in _SPECIAL_INPUT_TYPES)

    @property
    def is_checkbox(self):
        return self.kind == "input" and self.input_type == "checkbox"

    @property
    def is_radio(self):
        return self.kind == "input" and self.input_type == "radio"

    @property
    def is_file(self):
        return self.kind == "input" and self.input_type == "file"

    @property
    def is_button_like(self):
        return (self.kind == "button"
                or (self.kind == "input"
                    and self.input_type in _BUTTON_INPUT_TYPES))

    @property
    def is_submitter(self):
        if self.kind == "button":
            return self.button_type == "submit"
        return self.kind == "input" and self.input_type == "submit"

    def describe_kind(self):
        if self.kind == "input":
            return f"input type={self.input_type}"
        if self.kind == "button":
            return f"button type={self.button_type}"
        return self.kind


class Form:
    def __init__(self, element):
        self.element = element

    @property
    def form_id(self):
        return self.element.attrs.get("id")


def _is_ancestor(ancestor, element):
    node = element.parent
    while node is not None:
        if node is ancestor:
            return True
        node = node.parent
    return False


class FormDocument:
    """The form-relevant model of one parsed page."""

    def __init__(self, document: Document, page_url: str):
        self.document = document
        self.page_url = page_url
        self.base_url = self._find_base_url()
        self._id_map = {}
        for el in document.elements:
            el_id = el.attrs.get("id")
            if el_id:
                self._id_map.setdefault(el_id, []).append(el)
        self.forms = [Form(el) for el in document.elements
                      if el.tag == "form"]
        self._form_by_element = {id(f.element): f for f in self.forms}
        self.controls = []
        for el in document.elements:          # document order
            if el.tag in _CONTROL_TAGS:
                ctl = Control(el)
                ctl.owner = self._resolve_owner(el)
                ctl.disabled = self._compute_disabled(el)
                if el.tag == "select":
                    ctl.options = self._build_options(el)
                self.controls.append(ctl)
        self._control_by_element = {id(c.element): c for c in self.controls}
        self._normalize_radio_groups()

    # -- construction helpers ------------------------------------------

    def _find_base_url(self):
        # The first <base href> in document order wins; a relative href
        # is resolved against the page URL.
        for el in self.document.elements:
            if el.tag == "base" and "href" in el.attrs:
                return urljoin(self.page_url, el.attrs["href"])
        return self.page_url

    def _resolve_owner(self, el):
        if "form" in el.attrs:
            # Explicit association wins.  It must name a form element;
            # anything else (missing id, non-form element, empty value)
            # means the control belongs to no form.
            fid = el.attrs["form"]
            if fid:
                targets = self._id_map.get(fid)
                if targets and targets[0].tag == "form":
                    return self._form_by_element[id(targets[0])]
            return None
        node = el.parent
        while node is not None:
            if node.tag == "form":
                return self._form_by_element[id(node)]
            node = node.parent
        return None

    def _compute_disabled(self, el):
        if "disabled" in el.attrs:
            return True
        node = el.parent
        while node is not None:
            if node.tag == "fieldset" and "disabled" in node.attrs:
                legend = None
                for child in node.children:
                    if isinstance(child, Element) and child.tag == "legend":
                        legend = child
                        break
                if legend is None or not _is_ancestor(legend, el):
                    return True
            node = node.parent
        return False

    def _build_options(self, select_el):
        options = []

        def add_option(opt_el, group_disabled):
            disabled = group_disabled or "disabled" in opt_el.attrs
            options.append(Option(opt_el, disabled))

        for child in select_el.children:
            if isinstance(child, Element):
                if child.tag == "option":
                    add_option(child, False)
                elif child.tag == "optgroup":
                    group_disabled = "disabled" in child.attrs
                    for sub in child.children:
                        if isinstance(sub, Element):
                            if sub.tag != "option":
                                raise HtmlSubsetError(
                                    f"<{sub.tag}> inside <optgroup> is not "
                                    "supported; optgroup may contain option "
                                    "elements only")
                            add_option(sub, group_disabled)
                        elif sub.data.strip():
                            raise HtmlSubsetError(
                                "text inside <optgroup> is not supported")
                else:
                    raise HtmlSubsetError(
                        f"<{child.tag}> inside <select> is not supported; "
                        "select may contain option/optgroup only")
            elif child.data.strip():
                raise HtmlSubsetError(
                    "non-whitespace text directly inside <select> is not "
                    "supported")
        return options

    def _normalize_radio_groups(self):
        groups = {}
        for ctl in self.controls:
            if ctl.is_radio:
                key = (id(ctl.owner) if ctl.owner is not None else None,
                       ctl.name)
                groups.setdefault(key, []).append(ctl)
        for group in groups.values():
            checked = [c for c in group if c.checked]
            # Document order: the last checked radio of the group wins.
            for ctl in checked[:-1]:
                ctl.checked = False

    # -- lookups ---------------------------------------------------------

    def form_by_id(self, form_id):
        matches = [f for f in self.forms if f.form_id == form_id]
        if not matches:
            present = sorted({f.form_id for f in self.forms if f.form_id})
            raise FormStructureError(
                f"no form with id {form_id!r} on this page "
                f"(forms present: {present or 'none'})")
        if len(matches) > 1:
            raise FormStructureError(
                f"form id {form_id!r} is not unique: {len(matches)} forms "
                "share it")
        return matches[0]

    def control_by_id(self, control_id):
        matches = self._id_map.get(control_id, [])
        if not matches:
            raise FormStructureError(
                f"no element with id {control_id!r} on this page")
        if len(matches) > 1:
            raise FormStructureError(
                f"id {control_id!r} is not unique: {len(matches)} elements "
                "share it")
        ctl = self._control_by_element.get(id(matches[0]))
        if ctl is None:
            raise FormStructureError(
                f"element {control_id!r} is a <{matches[0].tag}>, not a "
                "form control")
        return ctl


# ---------------------------------------------------------------------------
# successful controls


@dataclass
class TextEntry:
    name: str
    value: str
    control: Control


@dataclass
class FileEntry:
    name: str
    filename: str          # "" when the control has no file selected
    path: str | None       # None when unselected
    size: int
    control: Control


def _selected_options(ctl):
    if ctl.explicit_selection is not None:
        chosen = [o for o in ctl.options
                  if o.option_id in ctl.explicit_selection]
    else:
        chosen = [o for o in ctl.options if o.selected]
    if ctl.multiple:
        return [o for o in chosen if not o.disabled]
    enabled = [o for o in chosen if not o.disabled]
    if enabled:
        return [enabled[-1]]
    if chosen:
        return []           # explicit selection exists but is disabled
    for opt in ctl.options:  # no explicit selection: first available
        if not opt.disabled:
            return [opt]
    return []


def successful_entries(doc: FormDocument, form: Form, submitter=None):
    """Ordered successful controls of ``form`` (document order).

    ``submitter`` is the single clicked submit button (or None); every
    other button-type control is skipped.
    """
    entries = []
    for ctl in doc.controls:
        if ctl.owner is not form:
            continue
        if ctl.disabled:
            continue
        if ctl.is_button_like:
            if ctl is submitter and ctl.name:
                entries.append(TextEntry(ctl.name, ctl.value, ctl))
            continue
        name = ctl.name
        if not name:
            continue
        if ctl.is_text_like:
            entries.append(TextEntry(name, ctl.value, ctl))
        elif ctl.is_checkbox or ctl.is_radio:
            if ctl.checked:
                entries.append(
                    TextEntry(name, ctl.element.attrs.get("value", "on"),
                              ctl))
        elif ctl.kind == "select":
            for opt in _selected_options(ctl):
                entries.append(TextEntry(name, opt.value, ctl))
        elif ctl.is_file:
            if ctl.files:
                for path in ctl.files:
                    entries.append(
                        FileEntry(name, os.path.basename(path), path,
                                  os.stat(path).st_size, ctl))
            else:
                # "no file selected" is its own state: an empty-named,
                # empty-bodied entry, distinct from a zero-byte file.
                entries.append(FileEntry(name, "", None, 0, ctl))
    return entries


# ---------------------------------------------------------------------------
# modifications (validated as a whole, then applied -- never partially)


def apply_modifications(doc: FormDocument, form: Form, modifications):
    if not isinstance(modifications, dict):
        raise ModificationError(
            "modifications must be a mapping of control id to new state")
    actions = []
    radio_true = {}
    for cid, spec in modifications.items():
        if not isinstance(cid, str):
            raise ModificationError(
                f"control id must be a string, got {cid!r}")
        ctl = doc.control_by_id(cid)
        if ctl.owner is not form:
            raise ModificationError(
                f"control {cid!r} does not belong to form "
                f"{form.form_id!r}; modifications cannot cross form "
                "boundaries")
        if ctl.disabled:
            raise ModificationError(
                f"control {cid!r} is disabled; a disabled control cannot "
                "be modified or submitted")
        if ctl.is_button_like:
            raise ModificationError(
                f"control {cid!r} is a {ctl.describe_kind()}; buttons are "
                "clicked (submitter_id), not modified")
        if isinstance(spec, bool):
            actions.append(_bool_action(doc, ctl, cid, spec, radio_true))
        elif isinstance(spec, str):
            if not ctl.is_text_like:
                raise ModificationError(
                    f"control {cid!r} is a {ctl.describe_kind()}; a string "
                    "modification only applies to text-like inputs and "
                    "textarea")
            actions.append(_set_value(ctl, spec))
        elif isinstance(spec, list):
            if ctl.kind == "select":
                actions.append(_select_action(ctl, cid, spec))
            elif ctl.is_file:
                actions.append(_file_action(ctl, cid, spec))
            else:
                raise ModificationError(
                    f"control {cid!r} is a {ctl.describe_kind()}; a list "
                    "modification only applies to select (option ids) or "
                    "file (paths) controls")
        else:
            raise ModificationError(
                f"unsupported modification for control {cid!r}: "
                f"{type(spec).__name__} (expected string, boolean, or "
                "list of strings)")
    for action in actions:
        action()


def _set_value(ctl, value):
    def apply():
        ctl.value = value
    return apply


def _bool_action(doc, ctl, cid, spec, radio_true):
    if ctl.is_checkbox:
        return _set_checked(ctl, spec)
    if ctl.is_radio:
        if spec:
            key = (id(ctl.owner), ctl.name)
            other = radio_true.get(key)
            if other is not None:
                raise ModificationError(
                    f"radio controls {other!r} and {cid!r} share group "
                    f"name={ctl.name!r}; only one radio of a group may be "
                    "set true")
            radio_true[key] = cid

            def apply():
                for other_ctl in doc.controls:
                    if (other_ctl.is_radio
                            and other_ctl.owner is ctl.owner
                            and other_ctl.name == ctl.name):
                        other_ctl.checked = other_ctl is ctl
            return apply
        return _set_checked(ctl, False)
    raise ModificationError(
        f"control {cid!r} is a {ctl.describe_kind()}; a boolean "
        "modification only applies to checkbox/radio")


def _set_checked(ctl, value):
    def apply():
        ctl.checked = value
    return apply


def _select_action(ctl, cid, spec):
    if not ctl.multiple and len(spec) > 1:
        raise ModificationError(
            f"select {cid!r} is single-select but {len(spec)} option ids "
            "were given")
    for oid in spec:
        if not isinstance(oid, str):
            raise ModificationError(
                f"option ids for select {cid!r} must be strings, got "
                f"{oid!r}")
    for oid in spec:
        matches = [o for o in ctl.options if o.option_id == oid]
        if not matches:
            raise ModificationError(
                f"select {cid!r} has no option with id {oid!r}")
        if len(matches) > 1:
            raise ModificationError(
                f"option id {oid!r} is not unique within select {cid!r}")
        if matches[0].disabled:
            raise ModificationError(
                f"option {oid!r} of select {cid!r} is disabled and cannot "
                "be selected")

    def apply():
        ctl.explicit_selection = list(spec)
    return apply


def _file_action(ctl, cid, spec):
    if not ctl.multiple and len(spec) > 1:
        raise ModificationError(
            f"file control {cid!r} is not multiple but {len(spec)} paths "
            "were given")
    for path in spec:
        if not isinstance(path, str):
            raise ModificationError(
                f"file paths for control {cid!r} must be strings, got "
                f"{path!r}")
        if not os.path.isfile(path):
            raise ModificationError(
                f"upload path for control {cid!r} is not a regular file: "
                f"{path!r}")
        if not os.access(path, os.R_OK):
            raise ModificationError(
                f"upload path for control {cid!r} is not readable: "
                f"{path!r}")

    def apply():
        ctl.files = list(spec)
    return apply


# ---------------------------------------------------------------------------
# submitter and target resolution


def resolve_submitter(doc: FormDocument, form: Form, submitter_id):
    if submitter_id is None:
        return None
    ctl = doc.control_by_id(submitter_id)
    if ctl.owner is not form:
        raise FormStructureError(
            f"submit button {submitter_id!r} does not belong to form "
            f"{form.form_id!r}")
    if not ctl.is_button_like:
        raise FormStructureError(
            f"control {submitter_id!r} is a {ctl.describe_kind()}, not a "
            "button")
    if ctl.kind == "input" and ctl.input_type == "image":
        raise UnsupportedFeatureError(
            f"image button {submitter_id!r} is not supported: it would "
            "require click coordinates (x/y)")
    if not ctl.is_submitter:
        raise FormStructureError(
            f"control {submitter_id!r} is a {ctl.describe_kind()}; only "
            "submit buttons can be clicked")
    if ctl.disabled:
        raise FormStructureError(
            f"submit button {submitter_id!r} is disabled and cannot be "
            "clicked")
    return ctl


@dataclass
class Target:
    method: str            # "GET" | "POST"
    url: str               # absolute, fragment stripped
    enctype: str           # URLENCODED | MULTIPART


def _pick_attr(submitter, form, name):
    if submitter is not None:
        value = submitter.element.attrs.get(f"form{name}")
        if value is not None:
            return value
    return form.element.attrs.get(name)


def resolve_target(doc: FormDocument, form: Form, submitter) -> Target:
    raw_method = _pick_attr(submitter, form, "method")
    if raw_method is None or raw_method.strip() == "":
        method = "GET"
    else:
        value = raw_method.strip().lower()
        if value == "get":
            method = "GET"
        elif value == "post":
            method = "POST"
        else:
            raise UnsupportedFeatureError(
                f"form method {raw_method!r} is not supported; only GET "
                "and POST are")

    action = None
    raw_action = _pick_attr(submitter, form, "action")
    if raw_action is not None and raw_action.strip() != "":
        action = raw_action
    if action is None:
        # Missing or empty action: submit back to the page URL itself.
        url = doc.page_url
    else:
        url = urljoin(doc.base_url, action)
    url = urldefrag(url)[0]

    raw_enctype = _pick_attr(submitter, form, "enctype")
    if raw_enctype is None or raw_enctype.strip() == "":
        enctype = URLENCODED
    else:
        value = raw_enctype.strip().lower()
        if value == URLENCODED:
            enctype = URLENCODED
        elif value == MULTIPART:
            enctype = MULTIPART
        elif value == "text/plain":
            raise UnsupportedFeatureError(
                "enctype text/plain is not supported; use "
                "application/x-www-form-urlencoded or multipart/form-data")
        else:
            raise UnsupportedFeatureError(
                f"enctype {raw_enctype!r} is not supported")
    return Target(method, url, enctype)
