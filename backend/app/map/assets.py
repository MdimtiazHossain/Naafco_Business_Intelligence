"""Uploaded marker artwork.

SVG is the preferred format and the only one stored as markup — sanitised
markup, produced by :mod:`app.map.svg_safety`, never the bytes that arrived.
PNG and WebP are accepted, verified against their magic headers, and stored as
base64 data URIs so that serving a marker never becomes a filesystem read with a
user-influenced path.

The size limit is configuration, not a constant: ``MAP_MARKER_MAX_UPLOAD_KB``.
"""

from __future__ import annotations

import base64
import hashlib
import struct
import uuid
from dataclasses import dataclass
from typing import BinaryIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database.models_map import MapMarkerAsset
from .svg_safety import SvgRejected, sanitise_svg

MEDIA_SVG = "image/svg+xml"
MEDIA_PNG = "image/png"
MEDIA_WEBP = "image/webp"

#: Extension -> media type. SVG first: it is the format to prefer.
ALLOWED_UPLOADS: dict[str, str] = {
    ".svg": MEDIA_SVG,
    ".png": MEDIA_PNG,
    ".webp": MEDIA_WEBP,
}

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_RIFF_MAGIC = b"RIFF"
_WEBP_MAGIC = b"WEBP"


class AssetRejected(ValueError):
    """An upload that cannot be accepted, with a user-safe message."""


def max_upload_bytes() -> int:
    """The configured ceiling, defaulting to 512 KB.

    A marker is a small glyph; a megabyte of artwork would be a mistake, and
    every byte is re-sent to every map client that loads the configuration.
    """
    import os

    try:
        kilobytes = int(os.getenv("MAP_MARKER_MAX_UPLOAD_KB", "512"))
    except ValueError:
        kilobytes = 512
    return max(8, min(kilobytes, 4096)) * 1024


@dataclass
class StoredAsset:
    asset: MapMarkerAsset
    #: True when an identical file was already present and was reused.
    reused: bool = False


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    """Width and height from the IHDR chunk."""
    if len(data) < 24 or not data.startswith(_PNG_MAGIC):
        return None
    try:
        width, height = struct.unpack(">II", data[16:24])
    except struct.error:  # pragma: no cover - guarded by the length check
        return None
    return (width, height)


def _webp_dimensions(data: bytes) -> tuple[int, int] | None:
    """Width and height for the common WebP variants (VP8, VP8L, VP8X)."""
    if len(data) < 30 or data[:4] != _RIFF_MAGIC or data[8:12] != _WEBP_MAGIC:
        return None
    fourcc = data[12:16]
    try:
        if fourcc == b"VP8X":
            width = int.from_bytes(data[24:27], "little") + 1
            height = int.from_bytes(data[27:30], "little") + 1
            return (width, height)
        if fourcc == b"VP8 ":
            return (
                int.from_bytes(data[26:28], "little") & 0x3FFF,
                int.from_bytes(data[28:30], "little") & 0x3FFF,
            )
        if fourcc == b"VP8L":
            bits = int.from_bytes(data[21:25], "little")
            return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    except (IndexError, ValueError):  # pragma: no cover
        return None
    return None


def store_asset(session: Session, source: BinaryIO, file_name: str,
                content_type: str | None, uploaded_by: str | None) -> StoredAsset:
    """Validate, sanitise and persist an uploaded marker image."""
    from pathlib import Path

    extension = Path(file_name or "").suffix.lower()
    media_type = ALLOWED_UPLOADS.get(extension)
    if media_type is None:
        raise AssetRejected(
            f"'{extension or file_name}' is not a supported image. Upload an SVG "
            "(preferred), PNG or WebP file."
        )

    limit = max_upload_bytes()
    data = source.read(limit + 1)
    if not data:
        raise AssetRejected("The file is empty.")
    if len(data) > limit:
        raise AssetRejected(
            f"The image is larger than the {limit // 1024} KB limit. Simplify the "
            "artwork or export it at a smaller size."
        )

    width = height = None
    report = None

    if media_type == MEDIA_SVG:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise AssetRejected(
                "The SVG file is not valid UTF-8 text."
            ) from exc
        try:
            sanitised = sanitise_svg(text)
        except SvgRejected as exc:
            # The sanitiser's message is already written for the uploader.
            raise AssetRejected(str(exc)) from exc
        content = sanitised.markup
        width, height = sanitised.width, sanitised.height
        report = sanitised.report()
    else:
        dimensions = (_png_dimensions(data) if media_type == MEDIA_PNG
                      else _webp_dimensions(data))
        if dimensions is None:
            raise AssetRejected(
                f"The file is named {extension} but its contents are not a valid "
                f"{media_type.split('/')[-1].upper()} image."
            )
        width, height = dimensions
        if width > 2048 or height > 2048:
            raise AssetRejected(
                f"The image is {width}×{height}px. A marker image may be at most "
                "2048px on a side."
            )
        encoded = base64.b64encode(data).decode("ascii")
        content = f"data:{media_type};base64,{encoded}"

    checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
    existing = session.execute(
        select(MapMarkerAsset).where(MapMarkerAsset.checksum == checksum)
    ).scalars().first()
    if existing is not None:
        # The same artwork uploaded twice is one asset: designs that share a
        # glyph then share its storage and its cache entry.
        return StoredAsset(asset=existing, reused=True)

    asset = MapMarkerAsset(
        asset_uuid=str(uuid.uuid4()),
        file_name=file_name[-255:],
        media_type=media_type,
        byte_size=len(content.encode("utf-8")),
        checksum=checksum,
        content=content,
        sanitised_report=report,
        width=int(width) if width else None,
        height=int(height) if height else None,
        uploaded_by=uploaded_by,
    )
    session.add(asset)
    session.flush()
    return StoredAsset(asset=asset)


def asset_payload(asset: MapMarkerAsset) -> dict:
    """The asset as the designer sees it. Content is safe to embed."""
    return {
        "asset_id": asset.asset_id,
        "asset_uuid": asset.asset_uuid,
        "file_name": asset.file_name,
        "media_type": asset.media_type,
        "byte_size": asset.byte_size,
        "width": asset.width,
        "height": asset.height,
        "content": asset.content,
        "sanitised": asset.sanitised_report,
        "uploaded_by": asset.uploaded_by,
        "created_at": asset.created_at,
    }


__all__ = [
    "store_asset",
    "asset_payload",
    "AssetRejected",
    "StoredAsset",
    "ALLOWED_UPLOADS",
    "MEDIA_SVG",
    "MEDIA_PNG",
    "MEDIA_WEBP",
    "max_upload_bytes",
]
