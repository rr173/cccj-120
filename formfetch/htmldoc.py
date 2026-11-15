"""Strict, deterministic parser for the supported HTML subset.

The subset exists so that *control boundaries are never ambiguous*:

* tags must nest correctly; every non-void element needs an explicit end
  tag (``<option>`` must be closed with ``</option>`` and so on);
* void elements (``input``, ``base``, ``br``, ``meta``, ...) never have
  an end tag and never have children; both ``<input ...>`` and
  ``<input ... />`` are accepted;
* ``<form>`` elements never nest;
* ``<textarea>`` contains text only -- no elements, no comments;
* comments and the doctype are ignored everywhere else.

Anything outside this subset raises :class:`HtmlSubsetError` with the
position instead of guessing a meaning.  Character references in text
and attribute values are decoded by the parser (``convert_charrefs``).
"""

from __future__ import annotations

from html.parser import HTMLParser

from .errors import HtmlSubsetError

VOID_ELEMENTS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
})


class Element:
    """One element node.  ``order`` is the start-tag position in the
    document (0 is the artificial root)."""

    __slots__ = ("tag", "attrs", "children", "parent", "order", "line",
                 "col")

    def __init__(self, tag, attrs, parent, order, line, col):
        self.tag = tag
        self.attrs = attrs          # dict, first occurrence wins
        self.children = []          # Element | Text, in document order
        self.parent = parent
        self.order = order
        self.line = line
        self.col = col

    def iter_elements(self):
        for child in self.children:
            if isinstance(child, Element):
                yield child
                yield from child.iter_elements()

    def text_content(self):
        parts = []
        for node in self.children:
            if isinstance(node, Text):
                parts.append(node.data)
            else:
                parts.append(node.text_content())
        return "".join(parts)

    def __repr__(self):
        return f"<{self.tag} #{self.order}>"


class Text:
    __slots__ = ("data", "parent")

    def __init__(self, data, parent):
        self.data = data
        self.parent = parent


class Document:
    """Parsed page: element tree plus the flat start-tag ordered list."""

    def __init__(self, root, elements):
        self.root = root
        self.elements = elements    # every element, in document order


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Element("#document", {}, None, 0, 0, 0)
        self.stack = [self.root]
        self.elements = []
        self._order = 0

    # -- helpers ------------------------------------------------------

    def _in_textarea(self):
        return any(el.tag == "textarea" for el in self.stack)

    def _add(self, tag, attrs, push):
        self._order += 1
        line, col = self.getpos()
        attr_dict = {}
        for key, value in attrs:
            if key not in attr_dict:
                attr_dict[key] = value if value is not None else ""
        el = Element(tag, attr_dict, self.stack[-1], self._order, line, col)
        self.stack[-1].children.append(el)
        self.elements.append(el)
        if push:
            self.stack.append(el)

    def _check_nesting(self, tag):
        line, col = self.getpos()
        if self._in_textarea():
            raise HtmlSubsetError(
                f"line {line} col {col}: element <{tag}> inside <textarea>; "
                "textarea must contain text only")
        if tag == "form" and any(el.tag == "form" for el in self.stack):
            raise HtmlSubsetError(
                f"line {line} col {col}: nested <form> is not supported; "
                "close the outer form first")

    # -- HTMLParser callbacks -----------------------------------------

    def handle_starttag(self, tag, attrs):
        self._check_nesting(tag)
        self._add(tag, attrs, push=(tag not in VOID_ELEMENTS))

    def handle_startendtag(self, tag, attrs):
        self._check_nesting(tag)
        self._add(tag, attrs, push=False)

    def handle_endtag(self, tag):
        line, col = self.getpos()
        if tag in VOID_ELEMENTS:
            raise HtmlSubsetError(
                f"line {line} col {col}: </{tag}> is an end tag for a void "
                "element; void elements never have end tags")
        if len(self.stack) == 1:
            raise HtmlSubsetError(
                f"line {line} col {col}: unmatched end tag </{tag}>")
        top = self.stack[-1]
        if top.tag != tag:
            raise HtmlSubsetError(
                f"line {line} col {col}: mismatched end tag </{tag}>; "
                f"<{top.tag}> opened at line {top.line} is still open")
        self.stack.pop()

    def handle_data(self, data):
        if data:
            self.stack[-1].children.append(Text(data, self.stack[-1]))

    def handle_comment(self, data):
        if self._in_textarea():
            line, col = self.getpos()
            raise HtmlSubsetError(
                f"line {line} col {col}: comment inside <textarea>; "
                "textarea must contain text only")


def parse_html(source: str) -> Document:
    """Parse ``source`` into a :class:`Document` or raise
    :class:`HtmlSubsetError`."""
    builder = _Builder()
    builder.feed(source)
    builder.close()
    if len(builder.stack) != 1:
        top = builder.stack[-1]
        raise HtmlSubsetError(
            f"unclosed element <{top.tag}> (opened at line {top.line}) "
            "at end of document")
    return Document(builder.root, builder.elements)
