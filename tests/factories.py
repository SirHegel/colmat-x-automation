from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from colmat_x.domain import RenderedPost, normalized_content_hash

NOW = datetime(2026, 8, 15, 15, 0, tzinfo=UTC)


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
