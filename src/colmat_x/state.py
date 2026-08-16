from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from colmat_x.domain import (
    PostStatus,
    RenderedPost,
    approval_hash_from_values,
    is_canonical_x_post_id,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id TEXT PRIMARY KEY,
    scheduled_at_utc TEXT NOT NULL,
    rendered_text TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    source_path TEXT NOT NULL,
    approved_by TEXT,
    approved_at TEXT,
    approval_hash TEXT,
    status TEXT NOT NULL CHECK (
        status IN (
            'draft', 'scheduled', 'publishing', 'published',
            'failed', 'unknown', 'cancelled'
        )
    ),
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_until TEXT,
    x_post_id TEXT UNIQUE,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    published_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_posts_due
ON posts(status, scheduled_at_utc);

CREATE TABLE IF NOT EXISTS audit_log (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id TEXT NOT NULL,
    event TEXT NOT NULL,
    detail TEXT,
    occurred_at TEXT NOT NULL,
    FOREIGN KEY(post_id) REFERENCES posts(id)
);
"""


class StateError(RuntimeError):
    """La operación solicitada no es válida para el estado de la cola."""


class DailyLimitReached(StateError):
    """No quedan cupos de publicación reservables para el día local."""


class ReconciliationRequired(StateError):
    """Existe un resultado ambiguo que debe conciliarse antes de publicar."""


class PublicationInProgress(StateError):
    """Otro proceso ya tiene una publicación en vuelo."""


@dataclass(frozen=True)
class QueuePost:
    id: str
    scheduled_at_utc: datetime
    rendered_text: str
    content_hash: str
    source_path: str
    approved_by: str | None
    approved_at: datetime | None
    approval_hash: str | None
    status: PostStatus
    attempts: int
    lease_until: datetime | None
    x_post_id: str | None
    last_error: str | None
    published_at: datetime | None


@dataclass(frozen=True)
class SyncSummary:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    protected: int = 0
    cancelled: int = 0


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        parent_created = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=False, mode=0o700)
            parent_created = True
        except FileExistsError as exc:
            if not self.path.parent.is_dir():
                raise StateError(
                    f"La ruta de estado no es un directorio: {self.path.parent}"
                ) from exc
        except OSError as exc:
            raise StateError(
                f"No se pudo preparar el directorio de estado {self.path.parent}: {exc}"
            ) from exc
        if parent_created:
            with suppress(OSError):
                os.chmod(self.path.parent, 0o700)
        try:
            parent_mode = stat.S_IMODE(self.path.parent.stat().st_mode)
        except OSError as exc:
            raise StateError(
                f"No se pudieron comprobar los permisos de {self.path.parent}: {exc}"
            ) from exc
        if parent_mode & 0o077:
            raise StateError(
                f"El directorio de estado {self.path.parent} debe tener permisos 0700; "
                f"actualmente tiene {parent_mode:04o}"
            )
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            self._migrate(connection)
        with suppress(OSError):
            os.chmod(self.path, 0o600)

    def sync_posts(
        self,
        posts: Iterable[RenderedPost],
        *,
        now: datetime | None = None,
    ) -> SyncSummary:
        current_time = _ensure_utc(now or datetime.now(UTC))
        incoming = list(posts)
        incoming_ids = {post.id for post in incoming}
        counts = {
            "inserted": 0,
            "updated": 0,
            "unchanged": 0,
            "protected": 0,
            "cancelled": 0,
        }

        with self._connect() as connection:
            # Serializa la lectura y las actualizaciones para que sync nunca pueda
            # devolver a scheduled una fila reclamada por otro proceso.
            connection.execute("BEGIN IMMEDIATE")
            for post in incoming:
                existing_hash = connection.execute(
                    "SELECT id FROM posts WHERE content_hash = ? AND id <> ?",
                    (post.content_hash, post.id),
                ).fetchone()
                if existing_hash:
                    raise StateError(
                        f"El texto de '{post.id}' ya está registrado como '{existing_hash['id']}'"
                    )

                row = connection.execute("SELECT * FROM posts WHERE id = ?", (post.id,)).fetchone()
                scheduled = _format_time(post.publish_at_utc)
                source = str(post.source_path)

                if row is None:
                    connection.execute(
                        """
                        INSERT INTO posts (
                            id, scheduled_at_utc, rendered_text, content_hash,
                            source_path, status, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            post.id,
                            scheduled,
                            post.text,
                            post.content_hash,
                            source,
                            PostStatus.DRAFT.value,
                            _format_time(current_time),
                            _format_time(current_time),
                        ),
                    )
                    self._audit(
                        connection,
                        post.id,
                        "synced",
                        f"status={PostStatus.DRAFT.value}",
                        current_time,
                    )
                    counts["inserted"] += 1
                    continue

                status = PostStatus(row["status"])
                material_fields_match = (
                    row["scheduled_at_utc"] == scheduled
                    and row["rendered_text"] == post.text
                    and row["content_hash"] == post.content_hash
                )
                if material_fields_match:
                    if row["source_path"] != source:
                        connection.execute(
                            "UPDATE posts SET source_path = ?, updated_at = ? WHERE id = ?",
                            (source, _format_time(current_time), post.id),
                        )
                        self._audit(connection, post.id, "source_moved", source, current_time)
                        counts["updated"] += 1
                    elif status in {
                        PostStatus.PUBLISHING,
                        PostStatus.PUBLISHED,
                        PostStatus.FAILED,
                        PostStatus.UNKNOWN,
                        PostStatus.CANCELLED,
                    }:
                        counts["protected"] += 1
                    else:
                        counts["unchanged"] += 1
                    continue

                protected = {
                    PostStatus.PUBLISHING,
                    PostStatus.PUBLISHED,
                    PostStatus.UNKNOWN,
                    PostStatus.CANCELLED,
                }
                if status in protected:
                    raise StateError(
                        f"'{post.id}' está en estado {status.value}; no se puede cambiar "
                        "su texto ni programación"
                    )

                connection.execute(
                    """
                    UPDATE posts
                    SET scheduled_at_utc = ?, rendered_text = ?, content_hash = ?,
                        source_path = ?, approved_by = NULL, approved_at = NULL,
                        approval_hash = NULL, status = ?,
                        lease_until = NULL, last_error = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        scheduled,
                        post.text,
                        post.content_hash,
                        source,
                        PostStatus.DRAFT.value,
                        _format_time(current_time),
                        post.id,
                    ),
                )
                self._audit(
                    connection,
                    post.id,
                    "approval_invalidated" if row["approval_hash"] else "resynced",
                    f"status={PostStatus.DRAFT.value}",
                    current_time,
                )
                counts["updated"] += 1

            active_rows = connection.execute(
                "SELECT id FROM posts WHERE status IN (?, ?)",
                (PostStatus.DRAFT.value, PostStatus.SCHEDULED.value),
            ).fetchall()
            for row in active_rows:
                if row["id"] in incoming_ids:
                    continue
                connection.execute(
                    """
                    UPDATE posts
                    SET status = ?, approved_by = NULL, approved_at = NULL,
                        approval_hash = NULL, lease_until = NULL, updated_at = ?
                    WHERE id = ? AND status IN (?, ?)
                    """,
                    (
                        PostStatus.CANCELLED.value,
                        _format_time(current_time),
                        row["id"],
                        PostStatus.DRAFT.value,
                        PostStatus.SCHEDULED.value,
                    ),
                )
                self._audit(
                    connection,
                    row["id"],
                    "cancelled_missing_source",
                    "El archivo YAML ya no forma parte del contenido sincronizado",
                    current_time,
                )
                counts["cancelled"] += 1

        return SyncSummary(**counts)

    def approve(
        self,
        post_id: str,
        reviewer: str,
        expected_snapshot_hash: str,
        *,
        now: datetime | None = None,
    ) -> str:
        normalized_reviewer = " ".join(reviewer.split())
        if (
            not normalized_reviewer
            or len(normalized_reviewer) > 100
            or any(ord(character) < 32 for character in normalized_reviewer)
        ):
            raise StateError("El responsable debe tener entre 1 y 100 caracteres")
        current_time = _ensure_utc(now or datetime.now(UTC))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
            if row is None:
                raise StateError(f"No existe '{post_id}' en la cola; ejecuta sync primero")
            if PostStatus(row["status"]) != PostStatus.DRAFT:
                raise StateError("Solo se puede aprobar una publicación en estado draft")
            approval_hash = _approval_hash(row)
            if approval_hash != expected_snapshot_hash:
                raise StateError(
                    "El snapshot cambió desde la vista previa; vuelve a ejecutar preview"
                )
            connection.execute(
                """
                UPDATE posts
                SET status = ?, approved_by = ?, approved_at = ?, approval_hash = ?,
                    updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    PostStatus.SCHEDULED.value,
                    normalized_reviewer,
                    _format_time(current_time),
                    approval_hash,
                    _format_time(current_time),
                    post_id,
                    PostStatus.DRAFT.value,
                ),
            )
            self._audit(
                connection,
                post_id,
                "approved",
                f"by={normalized_reviewer}; snapshot={approval_hash}",
                current_time,
            )
        return approval_hash

    def withdraw_approval(self, post_id: str, *, now: datetime | None = None) -> None:
        current_time = _ensure_utc(now or datetime.now(UTC))
        self._clear_approval(
            post_id,
            from_status=PostStatus.SCHEDULED,
            event="approval_withdrawn",
            now=current_time,
        )

    def restore_cancelled(self, post: RenderedPost, *, now: datetime | None = None) -> None:
        current_time = _ensure_utc(now or datetime.now(UTC))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT status FROM posts WHERE id = ?", (post.id,)).fetchone()
            if row is None or PostStatus(row["status"]) != PostStatus.CANCELLED:
                raise StateError(
                    f"'{post.id}' debe estar en estado {PostStatus.CANCELLED.value} "
                    "para esta operación"
                )
            duplicate = connection.execute(
                "SELECT id FROM posts WHERE content_hash = ? AND id <> ?",
                (post.content_hash, post.id),
            ).fetchone()
            if duplicate:
                raise StateError(
                    f"El texto de '{post.id}' ya está registrado como '{duplicate['id']}'"
                )
            connection.execute(
                """
                UPDATE posts
                SET scheduled_at_utc = ?, rendered_text = ?, content_hash = ?,
                    source_path = ?, approved_by = NULL, approved_at = NULL,
                    approval_hash = NULL, status = ?,
                    lease_until = NULL, x_post_id = NULL, last_error = NULL,
                    published_at = NULL, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    _format_time(post.publish_at_utc),
                    post.text,
                    post.content_hash,
                    str(post.source_path),
                    PostStatus.DRAFT.value,
                    _format_time(current_time),
                    post.id,
                    PostStatus.CANCELLED.value,
                ),
            )
            self._audit(
                connection,
                post.id,
                "restored_as_draft",
                "Se cargó el snapshot vigente y se requiere una nueva aprobación",
                current_time,
            )

    def due_posts(self, now: datetime, limit: int) -> list[QueuePost]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM posts
                WHERE status = ? AND scheduled_at_utc <= ?
                ORDER BY scheduled_at_utc, id
                LIMIT ?
                """,
                (PostStatus.SCHEDULED.value, _format_time(now), limit),
            ).fetchall()
        return [_queue_post(row) for row in rows]

    def claim(
        self,
        post_id: str,
        now: datetime,
        lease_minutes: int,
        *,
        day_start: datetime | None = None,
        day_end: datetime | None = None,
        max_daily: int | None = None,
    ) -> QueuePost | None:
        current_time = _ensure_utc(now)
        lease_until = current_time + timedelta(minutes=lease_minutes)
        daily_values = (day_start, day_end, max_daily)
        if any(value is not None for value in daily_values) and any(
            value is None for value in daily_values
        ):
            raise StateError("El límite diario requiere inicio, fin y máximo")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            unknown = connection.execute(
                "SELECT id FROM posts WHERE status = ? LIMIT 1",
                (PostStatus.UNKNOWN.value,),
            ).fetchone()
            if unknown:
                raise ReconciliationRequired(
                    f"'{unknown['id']}' está en unknown y requiere conciliación"
                )
            publishing = connection.execute(
                "SELECT id FROM posts WHERE status = ? LIMIT 1",
                (PostStatus.PUBLISHING.value,),
            ).fetchone()
            if publishing:
                raise PublicationInProgress(
                    f"'{publishing['id']}' ya está siendo publicada por otro proceso"
                )
            if day_start is not None and day_end is not None and max_daily is not None:
                reserved = self._count_daily_slots(connection, day_start, day_end)
                if reserved >= max_daily:
                    raise DailyLimitReached(
                        f"Ya se reservaron {reserved} de {max_daily} publicaciones del día"
                    )
            candidate = connection.execute(
                "SELECT * FROM posts WHERE id = ? AND status = ? AND scheduled_at_utc <= ?",
                (post_id, PostStatus.SCHEDULED.value, _format_time(current_time)),
            ).fetchone()
            if candidate is None:
                return None
            stored_approval = candidate["approval_hash"]
            if not stored_approval or stored_approval != _approval_hash(candidate):
                connection.execute(
                    """
                    UPDATE posts
                    SET status = ?, approved_by = NULL, approved_at = NULL,
                        approval_hash = NULL, lease_until = NULL, updated_at = ?
                    WHERE id = ? AND status = ?
                    """,
                    (
                        PostStatus.DRAFT.value,
                        _format_time(current_time),
                        post_id,
                        PostStatus.SCHEDULED.value,
                    ),
                )
                self._audit(
                    connection,
                    post_id,
                    "approval_invalidated_on_claim",
                    "El hash aprobado no coincide con el snapshot almacenado",
                    current_time,
                )
                return None
            cursor = connection.execute(
                """
                UPDATE posts
                SET status = ?, attempts = attempts + 1, lease_until = ?, updated_at = ?
                WHERE id = ? AND status = ? AND scheduled_at_utc <= ?
                    AND approval_hash = ?
                """,
                (
                    PostStatus.PUBLISHING.value,
                    _format_time(lease_until),
                    _format_time(current_time),
                    post_id,
                    PostStatus.SCHEDULED.value,
                    _format_time(current_time),
                    stored_approval,
                ),
            )
            if cursor.rowcount != 1:
                return None
            self._audit(connection, post_id, "claimed", None, current_time)
            row = connection.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
        return _queue_post(row)

    def mark_published(
        self,
        post_id: str,
        x_post_id: str,
        *,
        expected_attempt: int,
        now: datetime | None = None,
    ) -> None:
        if not is_canonical_x_post_id(x_post_id):
            raise StateError("El ID de X debe usar de 1 a 20 dígitos ASCII, sin ceros iniciales")
        current_time = _ensure_utc(now or datetime.now(UTC))
        self._transition_from_publishing(
            post_id,
            PostStatus.PUBLISHED,
            expected_attempt=expected_attempt,
            now=current_time,
            x_post_id=x_post_id,
            published_at=current_time,
            event="published",
        )

    def mark_failed(
        self,
        post_id: str,
        error: str,
        *,
        expected_attempt: int,
        now: datetime | None = None,
    ) -> None:
        current_time = _ensure_utc(now or datetime.now(UTC))
        self._transition_from_publishing(
            post_id,
            PostStatus.FAILED,
            expected_attempt=expected_attempt,
            now=current_time,
            error=error,
            event="failed",
        )

    def mark_unknown(
        self,
        post_id: str,
        error: str,
        *,
        expected_attempt: int,
        now: datetime | None = None,
    ) -> None:
        current_time = _ensure_utc(now or datetime.now(UTC))
        self._transition_from_publishing(
            post_id,
            PostStatus.UNKNOWN,
            expected_attempt=expected_attempt,
            now=current_time,
            error=error,
            event="outcome_unknown",
        )

    def recover_expired_leases(self, now: datetime | None = None) -> int:
        current_time = _ensure_utc(now or datetime.now(UTC))
        recovered = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT id FROM posts
                WHERE status = ? AND lease_until IS NOT NULL AND lease_until <= ?
                """,
                (PostStatus.PUBLISHING.value, _format_time(current_time)),
            ).fetchall()
            for row in rows:
                cursor = connection.execute(
                    """
                    UPDATE posts
                    SET status = ?, lease_until = NULL, last_error = ?, updated_at = ?
                    WHERE id = ? AND status = ?
                    """,
                    (
                        PostStatus.UNKNOWN.value,
                        "El proceso perdió el lease; el resultado de publicación es desconocido",
                        _format_time(current_time),
                        row["id"],
                        PostStatus.PUBLISHING.value,
                    ),
                )
                if cursor.rowcount == 1:
                    self._audit(
                        connection,
                        row["id"],
                        "lease_expired",
                        "Requiere conciliación manual",
                        current_time,
                    )
                    recovered += 1
        return recovered

    def count_published_between(self, start: datetime, end: datetime) -> int:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS amount FROM posts
                WHERE status = ? AND published_at >= ? AND published_at < ?
                """,
                (
                    PostStatus.PUBLISHED.value,
                    _format_time(start),
                    _format_time(end),
                ),
            ).fetchone()
        return int(row["amount"])

    def count_daily_slots(self, start: datetime, end: datetime) -> int:
        with self._connect() as connection:
            return self._count_daily_slots(connection, start, end)

    def list_posts(self, status: PostStatus | None = None) -> list[QueuePost]:
        with self._connect() as connection:
            if status is None:
                rows = connection.execute(
                    "SELECT * FROM posts ORDER BY scheduled_at_utc, id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM posts WHERE status = ? ORDER BY scheduled_at_utc, id",
                    (status.value,),
                ).fetchall()
        return [_queue_post(row) for row in rows]

    def retry(self, post_id: str, *, confirm_not_published: bool = False) -> PostStatus:
        current_time = datetime.now(UTC)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
            if row is None:
                raise StateError(f"No existe '{post_id}' en la cola")
            status = PostStatus(row["status"])
            allowed = status == PostStatus.FAILED or (
                status == PostStatus.UNKNOWN and confirm_not_published
            )
            if not allowed:
                raise StateError(
                    "Solo se puede reintentar un fallo aprobado; para estado unknown confirma "
                    "primero que no se publicó"
                )
            target = PostStatus.SCHEDULED if row["approval_hash"] else PostStatus.DRAFT
            connection.execute(
                """
                UPDATE posts
                SET status = ?, lease_until = NULL, last_error = NULL, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (target.value, _format_time(current_time), post_id, status.value),
            )
            event = "retried" if target == PostStatus.SCHEDULED else "retry_requires_reapproval"
            self._audit(connection, post_id, event, None, current_time)
        return target

    def reconcile_as_published(self, post_id: str, x_post_id: str) -> None:
        normalized_x_id = x_post_id.strip()
        if not is_canonical_x_post_id(normalized_x_id):
            raise StateError("El ID de X debe usar de 1 a 20 dígitos ASCII, sin ceros iniciales")
        current_time = datetime.now(UTC)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE posts
                SET status = ?, x_post_id = ?, published_at = ?, lease_until = NULL,
                    last_error = NULL, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    PostStatus.PUBLISHED.value,
                    normalized_x_id,
                    _format_time(current_time),
                    _format_time(current_time),
                    post_id,
                    PostStatus.UNKNOWN.value,
                ),
            )
            if cursor.rowcount != 1:
                raise StateError("Solo se puede conciliar una publicación en estado unknown")
            self._audit(
                connection,
                post_id,
                "reconciled_published",
                f"x_post_id={normalized_x_id}",
                current_time,
            )

    def _transition_from_publishing(
        self,
        post_id: str,
        status: PostStatus,
        *,
        expected_attempt: int,
        now: datetime,
        event: str,
        error: str | None = None,
        x_post_id: str | None = None,
        published_at: datetime | None = None,
    ) -> None:
        safe_error = error[:1000] if error else None
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE posts
                SET status = ?, lease_until = NULL, x_post_id = ?, last_error = ?,
                    published_at = ?, updated_at = ?
                WHERE id = ? AND status = ? AND attempts = ?
                """,
                (
                    status.value,
                    x_post_id,
                    safe_error,
                    _format_time(published_at) if published_at else None,
                    _format_time(now),
                    post_id,
                    PostStatus.PUBLISHING.value,
                    expected_attempt,
                ),
            )
            if cursor.rowcount != 1:
                raise StateError(
                    f"El intento {expected_attempt} de '{post_id}' ya no es la publicación activa"
                )
            self._audit(connection, post_id, event, safe_error, now)

    def _clear_approval(
        self,
        post_id: str,
        *,
        from_status: PostStatus,
        event: str,
        now: datetime,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE posts
                SET status = ?, approved_by = NULL, approved_at = NULL,
                    approval_hash = NULL, lease_until = NULL, last_error = NULL,
                    updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    PostStatus.DRAFT.value,
                    _format_time(now),
                    post_id,
                    from_status.value,
                ),
            )
            if cursor.rowcount != 1:
                raise StateError(
                    f"'{post_id}' debe estar en estado {from_status.value} para esta operación"
                )
            self._audit(connection, post_id, event, None, now)

    def _audit(
        self,
        connection: sqlite3.Connection,
        post_id: str,
        event: str,
        detail: str | None,
        occurred_at: datetime,
    ) -> None:
        connection.execute(
            "INSERT INTO audit_log(post_id, event, detail, occurred_at) VALUES (?, ?, ?, ?)",
            (post_id, event, detail, _format_time(occurred_at)),
        )

    def _count_daily_slots(
        self, connection: sqlite3.Connection, start: datetime, end: datetime
    ) -> int:
        row = connection.execute(
            """
            SELECT COUNT(*) AS amount FROM posts
            WHERE (
                status = ? AND published_at >= ? AND published_at < ?
            ) OR status = ?
            """,
            (
                PostStatus.PUBLISHED.value,
                _format_time(start),
                _format_time(end),
                PostStatus.PUBLISHING.value,
            ),
        ).fetchone()
        return int(row["amount"])

    def _migrate(self, connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(posts)").fetchall()}
        if "approved_by" not in columns:
            connection.execute("ALTER TABLE posts ADD COLUMN approved_by TEXT")
        if "approved_at" not in columns:
            connection.execute("ALTER TABLE posts ADD COLUMN approved_at TEXT")
        if "approval_hash" not in columns:
            connection.execute("ALTER TABLE posts ADD COLUMN approval_hash TEXT")
        # Una base previa puede no ligar la aprobación al snapshot, o contener un hash
        # inconsistente. Nunca se conserva como publicable sin una nueva revisión.
        current_time = datetime.now(UTC)
        rows = connection.execute(
            "SELECT * FROM posts WHERE status IN (?, ?)",
            (PostStatus.SCHEDULED.value, PostStatus.FAILED.value),
        ).fetchall()
        for row in rows:
            if row["approval_hash"] and row["approval_hash"] == _approval_hash(row):
                continue
            connection.execute(
                """
                UPDATE posts
                SET status = ?, approved_by = NULL, approved_at = NULL,
                    approval_hash = NULL, lease_until = NULL, last_error = NULL,
                    updated_at = ?
                WHERE id = ? AND status IN (?, ?)
                """,
                (
                    PostStatus.DRAFT.value,
                    _format_time(current_time),
                    row["id"],
                    PostStatus.SCHEDULED.value,
                    PostStatus.FAILED.value,
                ),
            )
            self._audit(
                connection,
                row["id"],
                "legacy_approval_invalidated",
                "Se requiere aprobar un snapshot verificable",
                current_time,
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.path, timeout=10)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA busy_timeout = 10000")
            with connection:
                yield connection
        except sqlite3.Error as exc:
            raise StateError(f"Error SQLite en {self.path}: {exc}") from exc
        finally:
            if connection is not None:
                connection.close()


def _queue_post(row: sqlite3.Row) -> QueuePost:
    return QueuePost(
        id=row["id"],
        scheduled_at_utc=_parse_time(row["scheduled_at_utc"]),
        rendered_text=row["rendered_text"],
        content_hash=row["content_hash"],
        source_path=row["source_path"],
        approved_by=row["approved_by"],
        approved_at=_parse_time(row["approved_at"]) if row["approved_at"] else None,
        approval_hash=row["approval_hash"],
        status=PostStatus(row["status"]),
        attempts=int(row["attempts"]),
        lease_until=_parse_time(row["lease_until"]) if row["lease_until"] else None,
        x_post_id=row["x_post_id"],
        last_error=row["last_error"],
        published_at=_parse_time(row["published_at"]) if row["published_at"] else None,
    )


def _format_time(value: datetime) -> str:
    normalized = _ensure_utc(value)
    return normalized.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise StateError("Las fechas internas deben incluir zona horaria")
    return value.astimezone(UTC)


def _approval_hash(row: sqlite3.Row) -> str:
    return approval_hash_from_values(
        row["id"],
        row["scheduled_at_utc"],
        row["rendered_text"],
        row["content_hash"],
    )
