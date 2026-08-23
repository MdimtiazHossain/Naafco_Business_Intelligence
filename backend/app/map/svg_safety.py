"""SVG sanitisation.

An SVG is a document that a browser executes, so an uploaded one is untrusted
input in the same way an uploaded HTML file would be. This module reduces it to
drawing instructions and nothing else.

The approach is a **whitelist**: unknown elements and unknown attributes are
dropped rather than inspected for badness, because a blacklist can only ever
exclude the attacks that were known when it was written. What survives is
re-serialised from the parsed tree, so the bytes that are stored are bytes this
module produced — an upload's original markup is never persisted or served.

Specifically defeated:

* ``<script>``, event handlers (``onload``, ``onclick``, …) and
  ``javascript:`` / ``data:text/html`` URLs — script execution;
* ``<foreignObject>`` — an escape hatch that embeds arbitrary HTML;
* ``<use href="http://…">``, ``<image href="http://…">`` and CSS ``url(…)`` —
  requests to third-party hosts, which leak viewer IPs and can be swapped for
  hostile content later;
* ``<!DOCTYPE>`` / ``<!ENTITY>`` — XXE file disclosure and billion-laughs
  expansion, rejected before parsing;
* ``<style>`` and style attributes containing anything but a small set of
  presentation properties.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"

#: Elements that draw. Anything else is removed with its subtree.
ALLOWED_ELEMENTS: frozenset[str] = frozenset({
    "svg", "g", "defs", "title", "desc", "symbol",
    "path", "rect", "circle", "ellipse", "line", "polyline", "polygon",
    "text", "tspan",
    "linearGradient", "radialGradient", "stop", "clipPath", "mask", "pattern",
    "use",
})

#: Attributes that describe geometry or presentation, and nothing else.
ALLOWED_ATTRIBUTES: frozenset[str] = frozenset({
    # structure / geometry
    "id", "class", "viewBox", "width", "height", "x", "y", "x1", "y1", "x2", "y2",
    "cx", "cy", "r", "rx", "ry", "d", "points", "transform", "preserveAspectRatio",
    "clip-path", "mask", "clip-rule", "offset", "gradientUnits",
    "gradientTransform", "patternUnits", "spreadMethod", "maskUnits",
    "clipPathUnits", "patternContentUnits",
    # presentation
    "fill", "fill-opacity", "fill-rule", "stroke", "stroke-width", "stroke-opacity",
    "stroke-linecap", "stroke-linejoin", "stroke-dasharray", "stroke-dashoffset",
    "stroke-miterlimit", "opacity", "color", "stop-color", "stop-opacity",
    "vector-effect", "paint-order",
    # text
    "font-size", "font-family", "font-weight", "font-style", "text-anchor",
    "dominant-baseline", "letter-spacing", "dx", "dy",
    # a tightly filtered style attribute; see ``_clean_style``
    "style",
})

#: CSS properties permitted inside a ``style`` attribute.
ALLOWED_STYLE_PROPERTIES: frozenset[str] = frozenset({
    "fill", "fill-opacity", "fill-rule", "stroke", "stroke-width", "stroke-opacity",
    "stroke-linecap", "stroke-linejoin", "stroke-dasharray", "opacity", "color",
    "font-size", "font-family", "font-weight", "font-style", "text-anchor",
    "stop-color", "stop-opacity", "display", "visibility",
})

#: Attributes naming a resource. Only same-document ``#fragment`` is allowed.
REFERENCE_ATTRIBUTES: frozenset[str] = frozenset({
    "href", "clip-path", "mask", "fill", "stroke", "filter",
})

#: Rejected before parsing — these are document-level attacks, not markup.
_FORBIDDEN_PROLOGUE = re.compile(r"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)
_SCRIPT_LIKE = re.compile(r"<\s*(script|foreignObject)\b", re.IGNORECASE)
#: Any URL scheme that is not a same-document fragment.
_UNSAFE_URL = re.compile(
    r"(javascript|vbscript|data|file|about|blob)\s*:", re.IGNORECASE
)
_CSS_URL = re.compile(r"url\s*\(", re.IGNORECASE)
_EXPRESSION = re.compile(r"expression\s*\(|@import|behaviou?r\s*:", re.IGNORECASE)

MAX_SVG_ELEMENTS = 3000
MAX_SVG_DEPTH = 40


class SvgRejected(ValueError):
    """The upload cannot be made safe, with a message fit to show a user."""


@dataclass
class SanitisedSvg:
    """The result of sanitising one document."""

    markup: str
    width: float | None = None
    height: float | None = None
    view_box: str | None = None
    #: What was removed, so the uploader can be told why it looks different.
    removed_elements: list[str] = field(default_factory=list)
    removed_attributes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.removed_elements or self.removed_attributes)

    def report(self) -> dict[str, Any]:
        return {
            "removed_elements": sorted(set(self.removed_elements))[:50],
            "removed_attributes": sorted(set(self.removed_attributes))[:50],
            "changed": self.changed,
        }


def _local(tag: Any) -> str:
    """``{http://www.w3.org/2000/svg}path`` -> ``path``."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _is_safe_reference(value: str) -> bool:
    """Only ``#fragment`` references stay: never a URL to another host."""
    stripped = value.strip()
    if stripped.startswith("#"):
        return True
    if stripped.lower().startswith("url(#") and stripped.endswith(")"):
        return True
    return not (_UNSAFE_URL.search(stripped) or _CSS_URL.search(stripped)
                or "//" in stripped)


def _clean_style(value: str, removed: list[str]) -> str | None:
    """Keep only presentation declarations with safe values."""
    if _EXPRESSION.search(value) or _CSS_URL.search(value) or _UNSAFE_URL.search(value):
        removed.append("style")
        return None
    kept = []
    for declaration in value.split(";"):
        if ":" not in declaration:
            continue
        name, _, raw = declaration.partition(":")
        name = name.strip().lower()
        raw = raw.strip()
        if name in ALLOWED_STYLE_PROPERTIES and raw and _is_safe_reference(raw):
            kept.append(f"{name}:{raw}")
        elif name:
            removed.append(f"style:{name}")
    return ";".join(kept) or None


def _clean_element(element: ET.Element, result: SanitisedSvg, depth: int) -> bool:
    """Strip one element in place. Returns False when it must be removed."""
    tag = _local(element.tag)
    if tag not in ALLOWED_ELEMENTS:
        result.removed_elements.append(tag or "<unknown>")
        return False
    if depth > MAX_SVG_DEPTH:
        result.removed_elements.append(tag)
        return False

    for name in list(element.attrib):
        local = _local(name) if "}" in name else name
        value = element.attrib[name]

        # Event handlers are the classic vector and are never presentational.
        if local.lower().startswith("on"):
            del element.attrib[name]
            result.removed_attributes.append(local)
            continue
        if local not in ALLOWED_ATTRIBUTES:
            del element.attrib[name]
            result.removed_attributes.append(local)
            continue
        if local == "style":
            cleaned = _clean_style(value, result.removed_attributes)
            if cleaned is None:
                del element.attrib[name]
            else:
                element.attrib[name] = cleaned
            continue
        if local in REFERENCE_ATTRIBUTES and not _is_safe_reference(value):
            del element.attrib[name]
            result.removed_attributes.append(local)
            continue
        if _UNSAFE_URL.search(value) or _EXPRESSION.search(value):
            del element.attrib[name]
            result.removed_attributes.append(local)

        # ``xlink:href`` is the legacy spelling of the same escape hatch.
        if name.startswith(f"{{{XLINK_NS}}}"):
            del element.attrib[name]
            result.removed_attributes.append(f"xlink:{local}")

    for child in list(element):
        if not _clean_element(child, result, depth + 1):
            element.remove(child)
    return True


def _dimension(value: str | None) -> float | None:
    """``"24"`` / ``"24px"`` -> ``24.0``; percentages and junk -> ``None``."""
    if not value:
        return None
    match = re.match(r"^\s*([0-9]*\.?[0-9]+)\s*(px|pt)?\s*$", value)
    return float(match.group(1)) if match else None


def sanitise_svg(markup: str) -> SanitisedSvg:
    """Reduce an SVG document to safe drawing instructions.

    Raises :class:`SvgRejected` when the document cannot be made safe — a
    malformed file, a document-level attack, or markup that is not an SVG at all.
    """
    if not markup or not markup.strip():
        raise SvgRejected("The SVG file is empty.")
    if len(markup) > 2_000_000:
        raise SvgRejected("The SVG markup is too large to process safely.")

    # Rejected before the parser sees them: an entity attack is an attack on the
    # parser itself, so it must not reach one.
    if _FORBIDDEN_PROLOGUE.search(markup):
        raise SvgRejected(
            "The SVG contains a DOCTYPE or ENTITY declaration, which is not "
            "allowed. Export a plain SVG without a document type declaration."
        )
    if _SCRIPT_LIKE.search(markup):
        raise SvgRejected(
            "The SVG contains a <script> or <foreignObject> element. Remove any "
            "interactivity and export a plain vector graphic."
        )

    try:
        parser = ET.XMLParser()
        root = ET.fromstring(markup, parser=parser)
    except ET.ParseError as exc:
        raise SvgRejected(
            "The SVG file could not be parsed. Re-export it from your drawing tool."
        ) from exc

    if _local(root.tag) != "svg":
        raise SvgRejected("The file is not an SVG document.")

    count = sum(1 for _ in root.iter())
    if count > MAX_SVG_ELEMENTS:
        raise SvgRejected(
            f"The SVG has {count} elements, more than the {MAX_SVG_ELEMENTS} a "
            "marker may contain. Simplify the artwork before uploading."
        )

    result = SanitisedSvg(markup="")
    if not _clean_element(root, result, 0):
        raise SvgRejected("The SVG root element could not be sanitised.")

    result.view_box = root.get("viewBox")
    result.width = _dimension(root.get("width"))
    result.height = _dimension(root.get("height"))
    if not result.view_box and result.width and result.height:
        result.view_box = f"0 0 {result.width:g} {result.height:g}"
    if not result.view_box:
        # Without a viewBox a marker cannot be scaled predictably, so one is
        # supplied rather than letting the browser guess.
        result.view_box = "0 0 24 24"

    root.set("viewBox", result.view_box)
    root.attrib.pop("width", None)
    root.attrib.pop("height", None)

    ET.register_namespace("", SVG_NS)
    body = ET.tostring(root, encoding="unicode")
    # A parsed-and-reserialised document cannot carry a processing instruction
    # or comment through, but strip defensively in case the tree grows one.
    body = re.sub(r"<\?.*?\?>|<!--.*?-->", "", body, flags=re.DOTALL)

    if not body.strip():
        raise SvgRejected("Nothing drawable remained after sanitisation.")
    result.markup = body.strip()
    return result


__all__ = [
    "sanitise_svg",
    "SanitisedSvg",
    "SvgRejected",
    "ALLOWED_ELEMENTS",
    "ALLOWED_ATTRIBUTES",
    "ALLOWED_STYLE_PROPERTIES",
    "MAX_SVG_ELEMENTS",
]
