from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from enum import StrEnum
from typing import Protocol

from colmat_x.config import ProjectSettings
from colmat_x.domain import PostStatus
from colmat_x.state import (
    DailyLimitReached,
    PublicationInProgress,
    QueuePost,
    ReconciliationRequired,
    StateStore,
)
from colmat_x.x_api import AmbiguousPublishError, XApiError, XPostResponse


class Publisher(Protocol):
    def create_post(self, text: str) -> XPostResponse: ...


class Outcome(StrEnum):
    DRY_RUN = "dry-run"
    PUBLISHED = "published"
    FAILED = "failed"
    UNKNOWN = "unknown"
    DAILY_LIMIT = "daily-limit"
    RECONCILIATION_REQUIRED = "reconciliation-required"
    BUSY = "busy"


@dataclass(frozen=True)
class PublishResult:
    post_id: str | None
    outcome: Outcome
    detail: str
    x_post_id: str | None = None


def run_due_posts(
    store: StateStore,
    settings: ProjectSettings,
    *,
    live: bool,
    publisher: Publisher | None = None,
    now: datetime | None = None,
    limit: int | None = None,
) -> list[PublishResult]:
    selection_time = _clock(now)
    effective_limit = settings.safety.max_posts_per_run
    if limit is not None:
        if limit <= 0:
            raise ValueError("El límite debe ser mayor que cero")
        effective_limit = min(limit, effective_limit)

    due = store.due_posts(selection_time, effective_limit)
    if not live:
        return [
            PublishResult(
                post_id=post.id,
                outcome=Outcome.DRY_RUN,
                detail=post.rendered_text,
            )
            for post in due
        ]

    if publisher is None:
        raise ValueError("La publicación real necesita un cliente de X")

    unknown = store.list_posts(PostStatus.UNKNOWN)
    if unknown:
        return [
            PublishResult(
                post_id=unknown[0].id,
                outcome=Outcome.RECONCILIATION_REQUIRED,
                detail=(
                    "Hay resultados unknown. Revisa la cuenta y usa reconcile-published o "
                    "retry --confirm-not-published antes de continuar"
                ),
            )
        ]

    if not due:
        return []

    results: list[PublishResult] = []
    for queued in due:
        claim_time = _clock(now)
        day_start, day_end = _local_day_bounds(claim_time, settings)
        try:
            claimed = store.claim(
                queued.id,
                claim_time,
                settings.safety.lease_minutes,
                day_start=day_start,
                day_end=day_end,
                max_daily=settings.safety.max_posts_per_day,
            )
        except DailyLimitReached as exc:
            results.append(PublishResult(None, Outcome.DAILY_LIMIT, str(exc)))
            break
        except ReconciliationRequired as exc:
            results.append(PublishResult(queued.id, Outcome.RECONCILIATION_REQUIRED, str(exc)))
            break
        except PublicationInProgress as exc:
            results.append(PublishResult(queued.id, Outcome.BUSY, str(exc)))
            break
        if claimed is None:
            continue
        result = _publish_claimed(store, publisher, claimed, fixed_now=now)
        results.append(result)
        if result.outcome in {Outcome.FAILED, Outcome.UNKNOWN}:
            break
    return results


def _publish_claimed(
    store: StateStore,
    publisher: Publisher,
    post: QueuePost,
    fixed_now: datetime | None,
) -> PublishResult:
    try:
        response = publisher.create_post(post.rendered_text)
    except AmbiguousPublishError as exc:
        store.mark_unknown(
            post.id,
            str(exc),
            expected_attempt=post.attempts,
            now=_clock(fixed_now),
        )
        return PublishResult(post.id, Outcome.UNKNOWN, str(exc))
    except XApiError as exc:
        store.mark_failed(
            post.id,
            str(exc),
            expected_attempt=post.attempts,
            now=_clock(fixed_now),
        )
        return PublishResult(post.id, Outcome.FAILED, str(exc))

    store.mark_published(
        post.id,
        response.id,
        expected_attempt=post.attempts,
        now=_clock(fixed_now),
    )
    return PublishResult(
        post.id,
        Outcome.PUBLISHED,
        "Publicación creada correctamente",
        x_post_id=response.id,
    )


def _local_day_bounds(now_utc: datetime, settings: ProjectSettings) -> tuple[datetime, datetime]:
    local_now = now_utc.astimezone(settings.brand.zoneinfo)
    local_start = datetime.combine(local_now.date(), time.min, tzinfo=settings.brand.zoneinfo)
    local_end = local_start + timedelta(days=1)
    return local_start.astimezone(UTC), local_end.astimezone(UTC)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("La fecha de ejecución debe incluir zona horaria")
    return value.astimezone(UTC)


def _clock(fixed_now: datetime | None) -> datetime:
    return _ensure_utc(fixed_now or datetime.now(UTC))
