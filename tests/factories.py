from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path

from colmat_x.domain import RenderedPost, normalized_content_hash

NOW = datetime(2026, 8, 15, 15, 0, tzinfo=UTC)

ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
ONE_PIXEL_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAMCAgICAgMCAgIDAwMDBAYEBAQEBAgGBgUGCQgKCgkI"
    "CQkKDA8MCgsOCwkJDRENDg8QEBEQCgwSExIQEw8QEBD/2wBDAQMDAwQDBAgEBAgQCwkLEBAQEBAQ"
    "EBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBD/wAARCAABAAEDAREAAhEB"
    "AxEB/8QAFAABAAAAAAAAAAAAAAAAAAAAB//EABQQAQAAAAAAAAAAAAAAAAAAAAD/xAAVAQEBAAAAAAAA"
    "AAAAAAAAAAAGB//EABQRAQAAAAAAAAAAAAAAAAAAAAD/2gAMAwEAAhEDEQA/ACxPzh//2Q=="
)
ONE_PIXEL_WEBP = base64.b64decode(
    "UklGRjIAAABXRUJQVlA4ICYAAACQAQCdASoBAAEAAgA0JZACdLoAA5gA/u2QP7oMs4cHvqHFIeAAAA=="
)


def make_post(
    *,
    post_id: str = "colmat-test-001",
    text: str = "Una idea de Colmat",
    publish_at: datetime = NOW - timedelta(minutes=5),
) -> RenderedPost:
    return RenderedPost(
        id=post_id,
        template="idea",
        publish_at=publish_at,
        text=text,
        content_hash=normalized_content_hash(text),
        source_path=Path(f"{post_id}.yaml"),
    )
