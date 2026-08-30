from __future__ import annotations

SUPPORTED_IMAGE_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})


def sniff_supported_image_mime(content: bytes) -> str | None:
    """Detecta por firma los formatos de imagen admitidos por la plataforma."""

    if not isinstance(content, bytes):
        return None
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    return None
