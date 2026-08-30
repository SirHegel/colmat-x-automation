from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from colmat_x.automation import AutomationError, PublicationAmbiguousError
from colmat_x.media_paths import require_trusted_media_root
from colmat_x.platform_store import (
    AutomationRun,
    AutomationRunStatus,
    PlatformStore,
    PublicationClaim,
    PublicationRequestStatus,
    PublishStatus,
)
from colmat_x.x_api import (
    AmbiguousMediaError,
    AmbiguousPublishError,
    XApiClient,
    XApiError,
)

LIVE_PUBLISH_ENV = "COLMAT_LIVE_ENABLED"
EXPECTED_X_USER_ID_ENV = "EXPECTED_X_USER_ID"
EXPECTED_X_USERNAME_ENV = "EXPECTED_X_USERNAME"
DEFAULT_QUEUE_LIMIT = 5
MAX_QUEUE_LIMIT = 20
_ALLOWED_MEDIA_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
_SAFE_MEDIA_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")


class QueuePublicationStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class QueuePublicationResult:
    request_id: str
    draft_id: str
    status: QueuePublicationStatus
    provider_post_id: str | None = None
    detail: str = ""


class QueuedPublicationWorker:
    """Consume solicitudes humanas una sola vez, con fencing y sin reintento ambiguo."""

    def __init__(
        self,
        *,
        store: PlatformStore,
        x_client: XApiClient,
        publisher_actor_id: str,
        scheduler_actor_id: str,
        media_root: Path,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.x_client = x_client
        self.publisher_actor_id = _identifier(publisher_actor_id, "publisher_actor_id")
        self.scheduler_actor_id = _identifier(scheduler_actor_id, "scheduler_actor_id")
        if self.publisher_actor_id == self.scheduler_actor_id:
            raise ValueError("publisher_actor_id y scheduler_actor_id deben ser distintos")
        self.media_root = require_trusted_media_root(media_root)
        self.environ = os.environ if environ is None else environ
        self.clock = clock or (lambda: datetime.now(UTC))

    def run(self, *, limit: int = DEFAULT_QUEUE_LIMIT) -> tuple[QueuePublicationResult, ...]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_QUEUE_LIMIT
        ):
            raise ValueError(f"limit debe estar entre 1 y {MAX_QUEUE_LIMIT}")
        self._require_live_gate()
        if not self.store.has_queued_publication_request(
            actor_id=self.publisher_actor_id,
            now=self._now(),
        ):
            return ()
        results: list[QueuePublicationResult] = []
        for _ in range(limit):
            # El gate puede cambiar durante una carga de media. Si ya procesamos
            # una solicitud, detener el lote conserva las restantes en QUEUED;
            # si todavía no empezamos, el error explícito evita cualquier claim.
            try:
                self._require_live_gate()
            except AutomationError:
                if results:
                    break
                raise
            claim = self.store.claim_publication_request(
                actor_id=self.publisher_actor_id,
                now=self._now(),
            )
            if claim is None:
                break
            results.append(self._publish_claim(claim))
        return tuple(results)

    def _publish_claim(self, claim: PublicationClaim) -> QueuePublicationResult:
        request = claim.request
        attempt = None
        linked_run: AutomationRun | None = None
        try:
            self._validate_claim(claim)
            revision = self.store.get_current_revision(
                request.draft_id,
                actor_id=self.publisher_actor_id,
            )
            if (
                revision.id != request.revision_id
                or revision.snapshot_hash != request.snapshot_hash
            ):
                raise AutomationError("El snapshot encolado ya no coincide con el draft")
            attempt = self.store.create_publish_attempt(
                request.draft_id,
                actor_id=self.publisher_actor_id,
                expected_snapshot_hash=request.snapshot_hash,
                idempotency_key=claim.publish_attempt_idempotency_key,
                channel=request.channel,
                now=self._now(),
            )
            if PublishStatus(attempt.status) is not PublishStatus.PENDING:
                return self._reconcile_terminal_attempt(claim, attempt)
            linked_run = self._begin_linked_automation_run(request.draft_id)
            self._verify_x_identity()
            media_ids = self._upload_revision_media(revision)
            self._require_live_gate()
            self._validate_claim(claim)
            current_revision = self.store.get_current_revision(
                request.draft_id,
                actor_id=self.publisher_actor_id,
            )
            if (
                current_revision.id != request.revision_id
                or current_revision.snapshot_hash != request.snapshot_hash
                or current_revision.text != revision.text
            ):
                raise AutomationError("El contenido cambió antes de contactar a X")
            response = self.x_client.create_post(
                revision.text,
                media_ids=media_ids,
                made_with_ai=bool(media_ids),
            )
        except AmbiguousPublishError as exc:
            return self._finish_failure(
                claim,
                attempt,
                linked_run,
                status=QueuePublicationStatus.UNKNOWN,
                detail="X no confirmó si creó la publicación; no se reintentará.",
                cause=exc,
            )
        except PublicationAmbiguousError as exc:
            return self._finish_failure(
                claim,
                attempt,
                linked_run,
                status=QueuePublicationStatus.UNKNOWN,
                detail="El resultado persistido es inconcluso; no se reintentará.",
                cause=exc,
            )
        except (AmbiguousMediaError, XApiError, AutomationError, OSError, ValueError) as exc:
            return self._finish_failure(
                claim,
                attempt,
                linked_run,
                status=QueuePublicationStatus.FAILED,
                detail="La solicitud se cerró de forma segura antes de confirmar el post.",
                cause=exc,
            )
        except Exception as exc:
            return self._finish_failure(
                claim,
                attempt,
                linked_run,
                status=QueuePublicationStatus.UNKNOWN,
                detail="El estado externo es inconcluso; no se reintentará automáticamente.",
                cause=exc,
            )

        try:
            finished_attempt = self.store.finish_publish_attempt(
                attempt.id,
                PublishStatus.SUCCEEDED,
                actor_id=self.publisher_actor_id,
                provider_post_id=response.id,
                now=self._now(),
            )
        except Exception as exc:
            if linked_run is not None:
                self._finish_linked_run(linked_run, QueuePublicationStatus.UNKNOWN)
            raise PublicationAmbiguousError(
                "X confirmó el post, pero su recibo local quedó inconcluso"
            ) from exc
        self.store.finish_publication_request(
            request.id,
            PublicationRequestStatus.SUCCEEDED,
            actor_id=self.publisher_actor_id,
            claim_token=claim.claim_token,
            claim_fence=claim.claim_fence,
            publish_attempt_id=finished_attempt.id,
            now=self._now(),
        )
        if linked_run is not None:
            self._finish_linked_run(linked_run, QueuePublicationStatus.SUCCEEDED)
        return QueuePublicationResult(
            request_id=request.id,
            draft_id=request.draft_id,
            status=QueuePublicationStatus.SUCCEEDED,
            provider_post_id=response.id,
            detail="Publicación humana confirmada y persistida.",
        )

    def _upload_revision_media(self, revision: object) -> list[str]:
        digest = getattr(revision, "image_sha256", None)
        if digest is None:
            return []
        asset = self.store.get_media_asset_by_sha256(
            digest,
            actor_id=self.publisher_actor_id,
        )
        if asset is None:
            raise AutomationError("El snapshot aprobado referencia una imagen inexistente")
        metadata = asset.asset_metadata
        if not isinstance(metadata, dict):
            raise AutomationError("La imagen no tiene metadatos verificables")
        alt_text = metadata.get("alt_text")
        filename = metadata.get("filename")
        if not isinstance(alt_text, str) or not alt_text.strip():
            raise AutomationError("La imagen aprobada no tiene texto alternativo")
        if (
            not isinstance(filename, str)
            or _SAFE_MEDIA_FILENAME.fullmatch(filename.strip()) is None
        ):
            raise AutomationError("La imagen aprobada no tiene nombre seguro")
        filename = filename.strip()
        if asset.mime_type not in _ALLOWED_MEDIA_MIME_TYPES:
            raise AutomationError("La imagen aprobada usa un MIME no permitido")
        path = _trusted_media_path(asset.url, root=self.media_root)
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != digest:
            raise AutomationError("La imagen aprobada no coincide con su hash")
        if asset.byte_size is not None and asset.byte_size != len(content):
            raise AutomationError("El tamaño de la imagen aprobada no coincide")
        uploaded = self.x_client.upload_image(
            content,
            filename=filename,
            mime_type=asset.mime_type,
            alt_text=alt_text,
        )
        return [uploaded.id]

    def _validate_claim(self, claim: PublicationClaim) -> None:
        self.store.validate_publication_claim(
            claim.request.id,
            actor_id=self.publisher_actor_id,
            claim_token=claim.claim_token,
            claim_fence=claim.claim_fence,
            now=self._now(),
        )

    def _begin_linked_automation_run(self, draft_id: str) -> AutomationRun | None:
        run = next(
            (
                item
                for item in self.store.list_automation_runs(
                    actor_id=self.scheduler_actor_id,
                    status=AutomationRunStatus.READY,
                    limit=1000,
                )
                if item.draft_id == draft_id
            ),
            None,
        )
        if run is None:
            return None
        return self.store.finish_automation_run(
            run.id,
            AutomationRunStatus.PUBLISHING,
            actor_id=self.scheduler_actor_id,
            draft_id=draft_id,
            now=self._now(),
        )

    def _finish_linked_run(
        self,
        run: AutomationRun,
        status: QueuePublicationStatus,
    ) -> None:
        target = {
            QueuePublicationStatus.SUCCEEDED: AutomationRunStatus.SUCCEEDED,
            QueuePublicationStatus.FAILED: AutomationRunStatus.FAILED,
            QueuePublicationStatus.UNKNOWN: AutomationRunStatus.UNKNOWN,
        }[status]
        kwargs: dict[str, object] = {"draft_id": run.draft_id, "now": self._now()}
        if target in {AutomationRunStatus.FAILED, AutomationRunStatus.UNKNOWN}:
            kwargs["error"] = "La solicitud de publicación terminó sin éxito confirmado."
        self.store.finish_automation_run(
            run.id,
            target,
            actor_id=self.scheduler_actor_id,
            **kwargs,
        )

    def _finish_failure(
        self,
        claim: PublicationClaim,
        attempt: Any | None,
        linked_run: AutomationRun | None,
        *,
        status: QueuePublicationStatus,
        detail: str,
        cause: Exception,
    ) -> QueuePublicationResult:
        del cause  # Nunca se serializa texto de excepciones de proveedores o secretos.
        if attempt is None:
            # Sin PublishAttempt no existe prueba de contacto con X. El lease vencerá
            # a UNKNOWN y conservará el fencing; no se reencola automáticamente.
            return QueuePublicationResult(
                request_id=claim.request.id,
                draft_id=claim.request.draft_id,
                status=QueuePublicationStatus.UNKNOWN,
                detail="El claim quedó cercado para conciliación; no se reintentará.",
            )
        publish_status = (
            PublishStatus.UNKNOWN
            if status is QueuePublicationStatus.UNKNOWN
            else PublishStatus.FAILED
        )
        finished_attempt = self.store.finish_publish_attempt(
            attempt.id,
            publish_status,
            actor_id=self.publisher_actor_id,
            error=detail,
            now=self._now(),
        )
        request_status = (
            PublicationRequestStatus.UNKNOWN
            if status is QueuePublicationStatus.UNKNOWN
            else PublicationRequestStatus.FAILED
        )
        self.store.finish_publication_request(
            claim.request.id,
            request_status,
            actor_id=self.publisher_actor_id,
            claim_token=claim.claim_token,
            claim_fence=claim.claim_fence,
            publish_attempt_id=finished_attempt.id,
            now=self._now(),
        )
        if linked_run is not None:
            self._finish_linked_run(linked_run, status)
        return QueuePublicationResult(
            request_id=claim.request.id,
            draft_id=claim.request.draft_id,
            status=status,
            detail=detail,
        )

    def _reconcile_terminal_attempt(
        self,
        claim: PublicationClaim,
        attempt: Any,
    ) -> QueuePublicationResult:
        status = PublishStatus(attempt.status)
        mapped = {
            PublishStatus.SUCCEEDED: QueuePublicationStatus.SUCCEEDED,
            PublishStatus.FAILED: QueuePublicationStatus.FAILED,
            PublishStatus.UNKNOWN: QueuePublicationStatus.UNKNOWN,
        }.get(status)
        if mapped is None:
            raise PublicationAmbiguousError("Existe un intento pendiente previo")
        request_status = PublicationRequestStatus(mapped.value)
        self.store.finish_publication_request(
            claim.request.id,
            request_status,
            actor_id=self.publisher_actor_id,
            claim_token=claim.claim_token,
            claim_fence=claim.claim_fence,
            publish_attempt_id=attempt.id,
            now=self._now(),
        )
        return QueuePublicationResult(
            request_id=claim.request.id,
            draft_id=claim.request.draft_id,
            status=mapped,
            provider_post_id=getattr(attempt, "provider_post_id", None),
            detail="Se reconcilió un intento terminal ya persistido.",
        )

    def _require_live_gate(self) -> None:
        if self.environ.get(LIVE_PUBLISH_ENV, "").strip().casefold() != "true":
            raise AutomationError(f"Publicación bloqueada: {LIVE_PUBLISH_ENV} debe ser true")

    def _verify_x_identity(self) -> None:
        expected_user_id = _required_environment_identifier(
            self.environ,
            EXPECTED_X_USER_ID_ENV,
        )
        expected_username = _required_environment_identifier(
            self.environ,
            EXPECTED_X_USERNAME_ENV,
        )
        self.x_client.verify_identity(
            expected_user_id=expected_user_id,
            expected_username=expected_username,
        )

    def _now(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("El reloj del worker debe incluir zona horaria")
        return value.astimezone(UTC)


def _trusted_media_path(url: object, *, root: Path) -> Path:
    if not isinstance(url, str):
        raise AutomationError("La URL del media aprobado no es válida")
    parsed = urlsplit(url)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        raise AutomationError("El worker solo admite media local persistida")
    candidate = Path(unquote(parsed.path))
    try:
        metadata = candidate.lstat()
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise AutomationError("La ruta del media aprobado no pertenece al almacén seguro") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise AutomationError("La ruta del media aprobado no es un archivo regular")
    return resolved


def _identifier(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 120:
        raise ValueError(f"{field_name} no es válido")
    return value.strip()


def _required_environment_identifier(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 120:
        raise AutomationError(f"Publicación bloqueada: falta {name}")
    return value.strip()
