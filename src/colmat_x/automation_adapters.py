from __future__ import annotations

import hashlib
import os
import re
import stat
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from colmat_x.automation import (
    DIRECT_PUBLISH_ENV,
    AutomationError,
    AutomationEvent,
    AutomationEventKind,
    AutomationGenerationError,
    AutomationMode,
    ClaimDecision,
    DraftCandidate,
    GeneratedMedia,
    GenerationRequest,
    ImageGenerationRequest,
    PreparedAutomation,
    PublicationAmbiguousError,
    PublicationReceipt,
    SlotClaim,
    automation_slot_mapping,
    draft_matches_expected_evidence,
)
from colmat_x.editorial import EditorialPolicy
from colmat_x.image_validation import sniff_supported_image_mime
from colmat_x.media_paths import configured_worker_media_root, require_trusted_media_root
from colmat_x.minimax import MiniMaxClient
from colmat_x.platform_store import (
    AutomationReviewNotificationClaim,
    AutomationReviewNotificationStatus,
    AutomationRunStatus,
    ConflictError,
    DraftStatus,
    PlatformStore,
    PlatformStoreError,
    PublishStatus,
)
from colmat_x.telegram_api import (
    TelegramApiClient,
    TelegramApiError,
    TelegramProtocolError,
    TelegramTransportError,
)
from colmat_x.telegram_bot import approval_callback_data, rejection_callback_data
from colmat_x.x_api import (
    AmbiguousMediaError,
    AmbiguousPublishError,
    XApiClient,
    XApiError,
)

LIVE_PUBLISH_ENV = "COLMAT_LIVE_ENABLED"
DEFAULT_MEDIA_ROOT = configured_worker_media_root(None)
DEFAULT_REVIEW_NOTIFICATION_LIMIT = 5
MAX_REVIEW_NOTIFICATION_LIMIT = 20
MAX_TELEGRAM_PHOTO_BYTES = 10 * 1024 * 1024

_MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]+")
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+\-/=]{6,}"),
    re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{6,}"),
    re.compile(r"\b[0-9]{6,}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(
        r"(?i)\b(api[_ -]?key|access[_ -]?token|token|secret|password|authorization)"
        r"\s*[:=]\s*[^\s,;]+"
    ),
)


@dataclass(frozen=True)
class StoredAutomation:
    """Referencias persistidas para un slot reclamado en este proceso."""

    run_id: str
    claim: SlotClaim
    draft_id: str | None = None
    revision_id: str | None = None
    snapshot_hash: str | None = None
    text: str | None = None
    engagement_score: int | None = None
    media_sha256: str | None = None
    media_path: Path | None = None
    media_filename: str | None = None
    media_mime_type: str | None = None


class _UniqueUtcClock:
    """Produce marcas UTC estrictamente crecientes incluso con un reloj congelado."""

    def __init__(self, clock: Callable[[], datetime]) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._last: datetime | None = None

    def next(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("El reloj inyectado debe devolver un datetime con zona horaria")
        value = value.astimezone(UTC)
        with self._lock:
            if self._last is not None and value <= self._last:
                value = self._last + timedelta(microseconds=1)
            self._last = value
        return value


class MiniMaxAutomationGenerator:
    """Adapta MiniMax al puerto de generación sin concederle autoridad editorial."""

    def __init__(
        self,
        client: MiniMaxClient,
        *,
        policy: EditorialPolicy,
        editorial_line_resolver: Callable[[str], tuple[str, int] | None] | None = None,
    ) -> None:
        if not isinstance(policy, EditorialPolicy):
            raise TypeError("policy debe ser una EditorialPolicy validada")
        if editorial_line_resolver is not None and not callable(editorial_line_resolver):
            raise TypeError("editorial_line_resolver debe ser invocable")
        self.client = client
        self.policy = policy
        self.editorial_line_resolver = editorial_line_resolver

    def generate_draft(self, request: GenerationRequest) -> DraftCandidate:
        if not isinstance(request, GenerationRequest):
            raise TypeError("request debe ser GenerationRequest")
        model_brief = request.brief
        editorial_month: str | None = None
        editorial_version: int | None = None
        editorial_sha256: str | None = None
        if self.editorial_line_resolver is not None:
            editorial_month = f"{request.claim.local_date:%Y-%m}"
            resolved_line = self.editorial_line_resolver(editorial_month)
            if resolved_line is None:
                raise AutomationGenerationError(
                    f"No existe línea editorial para el mes {editorial_month}"
                )
            if not isinstance(resolved_line, tuple) or len(resolved_line) != 2:
                raise AutomationGenerationError("La línea editorial resuelta no es válida")
            line_text, editorial_version = resolved_line
            if not isinstance(line_text, str) or any(
                ord(character) < 32 and character not in "\n\t" for character in line_text
            ):
                raise AutomationGenerationError("El texto de la línea editorial no es válido")
            line_text = " ".join(line_text.split())
            if not 1 <= len(line_text) <= 600:
                raise AutomationGenerationError(
                    "La línea editorial debe tener entre 1 y 600 caracteres"
                )
            if (
                isinstance(editorial_version, bool)
                or not isinstance(editorial_version, int)
                or editorial_version < 1
            ):
                raise AutomationGenerationError("La versión de la línea editorial no es válida")
            editorial_sha256 = hashlib.sha256(line_text.encode("utf-8")).hexdigest()
            model_brief = (
                "Línea editorial mensual humana obligatoria "
                f"(mes={editorial_month}; versión={editorial_version}; "
                "contextualiza el encargo y no autoriza publicación):\n"
                f"{line_text}\n\n"
                f"Encargo autónomo del slot:\n{request.brief}"
            )
        if request.claim.slot.evidence_verified:
            model_brief = (
                f"{model_brief}\n\n"
                "Evidencia humana ya verificada. Usa literalmente estos valores tanto en "
                "los campos estructurados como en el texto: "
                f"cifra={request.claim.slot.evidence_expected_figure!r}; "
                f"fuente={request.claim.slot.evidence_expected_source!r}."
            )
        draft = self.client.generate_draft(
            model_brief,
            self.policy,
            category=request.category,
            institution=request.institution,
        )

        # La verificación viene exclusivamente de la configuración del slot. Nunca
        # se infiere a partir del texto, la cifra o la fuente redactados por MiniMax.
        verified = request.claim.slot.evidence_verified and draft_matches_expected_evidence(
            draft, request.claim.slot
        )
        reference = request.claim.slot.evidence_reference
        return DraftCandidate(
            draft=draft,
            evidence_verified=verified,
            evidence_reference=reference,
            editorial_line_month=editorial_month,
            editorial_line_version=editorial_version,
            editorial_line_sha256=editorial_sha256,
        )

    def generate_image(self, request: ImageGenerationRequest) -> GeneratedMedia:
        if not isinstance(request, ImageGenerationRequest):
            raise TypeError("request debe ser ImageGenerationRequest")
        description = request.draft.visual.descripcion
        alt_text = _image_alt_text(request)
        image = self.client.generate_image(
            description,
            self.policy,
            aspect_ratio="16:9",
            alt_text=alt_text,
        )
        extension = _MIME_EXTENSIONS.get(image.mime_type)
        if extension is None:
            raise AutomationGenerationError("MiniMax devolvió un tipo de imagen no admitido")
        filename = f"{request.claim.slot.id}-{request.claim.local_date.isoformat()}{extension}"
        return GeneratedMedia(
            content=image.content,
            filename=filename,
            mime_type=image.mime_type,
            sha256=image.sha256,
            alt_text=image.alt_text or alt_text,
        )


class PlatformAutomationRepository:
    """Persistencia operativa del scheduler, con media local por contenido."""

    def __init__(
        self,
        store: PlatformStore,
        *,
        scheduler_actor_id: str,
        author_actor_id: str,
        reviewer_telegram_user_id: int | str | None = None,
        review_chat_id: int | str | None = None,
        media_root: Path = DEFAULT_MEDIA_ROOT,
        workspace_id: str = "colmat",
        clock: Callable[[], datetime] | None = None,
        retry_run_ids: Mapping[str, str] | None = None,
    ) -> None:
        self.store = store
        self.scheduler_actor_id = _required_identifier(scheduler_actor_id, "scheduler_actor_id")
        self.author_actor_id = _required_identifier(author_actor_id, "author_actor_id")
        if self.scheduler_actor_id == self.author_actor_id:
            raise ValueError("scheduler_actor_id debe ser distinto del autor automático")
        if (reviewer_telegram_user_id is None) is not (review_chat_id is None):
            raise ValueError("reviewer_telegram_user_id y review_chat_id deben configurarse juntos")
        self.reviewer_telegram_user_id = reviewer_telegram_user_id
        self.review_chat_id = review_chat_id
        self.workspace_id = _required_identifier(workspace_id, "workspace_id")
        self.media_root = require_trusted_media_root(media_root)
        self._clock = _UniqueUtcClock(clock or (lambda: datetime.now(UTC)))
        self.retry_run_ids = {
            _required_identifier(key, "retry idempotency_key"): _required_identifier(
                run_id, "retry run_id"
            )
            for key, run_id in (retry_run_ids or {}).items()
        }
        self._records: dict[str, StoredAutomation] = {}
        self._records_lock = threading.RLock()

    def configure_review_destination(
        self,
        *,
        telegram_user_id: int | str,
        chat_id: int | str,
    ) -> None:
        """Fija una sola vez el destino que quedará dentro de la transacción outbox."""

        with self._records_lock:
            if self.reviewer_telegram_user_id is None and self.review_chat_id is None:
                self.reviewer_telegram_user_id = telegram_user_id
                self.review_chat_id = chat_id
                return
            if self.reviewer_telegram_user_id != telegram_user_id or self.review_chat_id != chat_id:
                raise ValueError("El destino de revisión ya fue configurado con otra identidad")

    def claim_slot(self, claim: SlotClaim, *, daily_limit: int) -> ClaimDecision:
        return self._claim_slot(
            claim,
            daily_limit=daily_limit,
            expected_retry_run_id=self.retry_run_ids.get(claim.idempotency_key),
        )

    def claim_retry_slot(self, claim: SlotClaim, *, daily_limit: int) -> ClaimDecision:
        expected_run_id = self.retry_run_ids.get(claim.idempotency_key)
        if expected_run_id is None:
            raise AutomationError("El retry no fue autorizado por un run persistido")
        return self._claim_slot(
            claim,
            daily_limit=daily_limit,
            expected_retry_run_id=expected_run_id,
        )

    def _claim_slot(
        self,
        claim: SlotClaim,
        *,
        daily_limit: int,
        expected_retry_run_id: str | None,
    ) -> ClaimDecision:
        if not isinstance(claim, SlotClaim):
            raise TypeError("claim debe ser SlotClaim")
        if isinstance(daily_limit, bool) or not isinstance(daily_limit, int) or daily_limit < 1:
            raise ValueError("daily_limit debe ser un entero positivo")
        claim_now = self._clock.next()
        try:
            run = self.store.claim_automation_run(
                actor_id=self.scheduler_actor_id,
                idempotency_key=claim.idempotency_key,
                slot_id=claim.slot.id,
                scheduled_for=claim.scheduled_for,
                slot_snapshot=automation_slot_mapping(claim.slot),
                mode=claim.slot.mode.value,
                workspace_id=self.workspace_id,
                now=claim_now,
                expected_retry_run_id=expected_retry_run_id,
            )
        except ConflictError as exc:
            if "max_posts_per_day" in str(exc):
                return ClaimDecision.DAILY_LIMIT
            raise

        if not _same_instant(run.claimed_at, claim_now):
            if run.status_value is AutomationRunStatus.FAILED:
                return ClaimDecision.DUPLICATE_FAILED
            if run.status_value is AutomationRunStatus.UNKNOWN:
                return ClaimDecision.DUPLICATE_UNKNOWN
            return ClaimDecision.DUPLICATE
        record = StoredAutomation(run_id=run.id, claim=claim)
        with self._records_lock:
            self._records[claim.idempotency_key] = record
        return ClaimDecision.CLAIMED

    def save_prepared(self, prepared: PreparedAutomation) -> None:
        if not isinstance(prepared, PreparedAutomation):
            raise TypeError("prepared debe ser PreparedAutomation")
        key = prepared.claim.idempotency_key
        record = self.get_record(key)
        if record.draft_id is not None:
            raise AutomationError("El slot ya tiene un borrador persistido")

        evidence = {
            "automation": {
                "idempotency_key": key,
                "slot_id": prepared.claim.slot.id,
            },
            "engagement": prepared.assessment.to_mapping(),
            "institution": prepared.draft.institution.value,
            "reported_figure": prepared.draft.figure,
            "reported_source": prepared.draft.source,
            "verification": {
                "reference": prepared.candidate.evidence_reference,
                "verified": prepared.candidate.evidence_verified,
                "expected_figure": prepared.claim.slot.evidence_expected_figure,
                "expected_source": prepared.claim.slot.evidence_expected_source,
            },
        }
        if prepared.candidate.editorial_line_month is not None:
            evidence["editorial_line"] = {
                "month": prepared.candidate.editorial_line_month,
                "version": prepared.candidate.editorial_line_version,
                "sha256": prepared.candidate.editorial_line_sha256,
            }
        media_path: Path | None = None
        image: dict[str, object] | None = None
        if prepared.media is not None:
            media_path = self._persist_media(prepared.media)
            image = {
                "url": media_path.as_uri(),
                "sha256": prepared.media.sha256,
                "mime_type": prepared.media.mime_type,
                "byte_size": len(prepared.media.content),
                "metadata": {
                    "alt_text": prepared.media.alt_text,
                    "filename": prepared.media.filename,
                    "source": "minimax",
                },
            }

        _run, draft, revision = self.store.persist_automation_prepared(
            record.run_id,
            actor_id=self.scheduler_actor_id,
            author_actor_id=self.author_actor_id,
            text=prepared.draft.text,
            category=prepared.draft.category.value,
            publish_at=prepared.claim.scheduled_for,
            evidence=evidence,
            image=image,
            now=self._clock.next(),
        )
        record = replace(
            record,
            draft_id=draft.id,
            revision_id=revision.id,
            snapshot_hash=revision.snapshot_hash,
            text=prepared.draft.text,
            engagement_score=prepared.assessment.score,
            media_sha256=prepared.media.sha256 if prepared.media is not None else None,
            media_path=media_path,
            media_filename=(prepared.media.filename if prepared.media is not None else None),
            media_mime_type=(prepared.media.mime_type if prepared.media is not None else None),
        )
        self._set_record(key, record)
        # El run permanece CLAIMED hasta conocer la disposición final. En modo
        # humano, mark_review_required enlaza AWAITING_REVIEW y su outbox en un
        # único commit; en modo direct avanza directamente a PUBLISHING.

    def mark_review_required(self, idempotency_key: str, *, reason: str) -> None:
        record = self.get_record(idempotency_key)
        if (
            record.draft_id is None
            or record.snapshot_hash is None
            or record.engagement_score is None
        ):
            raise AutomationError("El slot no tiene un snapshot revisable persistido")
        if self.reviewer_telegram_user_id is None or self.review_chat_id is None:
            raise AutomationError("El destino de revisión Telegram no está configurado")
        self.store.hold_automation_run_for_review(
            record.run_id,
            actor_id=self.scheduler_actor_id,
            draft_id=record.draft_id,
            expected_snapshot_hash=record.snapshot_hash,
            telegram_user_id=self.reviewer_telegram_user_id,
            chat_id=self.review_chat_id,
            detail=reason,
            engagement_score=record.engagement_score,
            now=self._clock.next(),
        )

    def mark_direct_blocked(self, idempotency_key: str, *, reason: str) -> None:
        record = self.get_record(idempotency_key)
        self.store.finish_automation_run(
            record.run_id,
            AutomationRunStatus.FAILED,
            actor_id=self.scheduler_actor_id,
            error=_safe_text(reason, maximum=1_000),
            now=self._clock.next(),
        )

    def begin_publishing(self, idempotency_key: str) -> None:
        record = self.get_record(idempotency_key)
        self.store.finish_automation_run(
            record.run_id,
            AutomationRunStatus.PUBLISHING,
            actor_id=self.scheduler_actor_id,
            draft_id=record.draft_id,
            now=self._clock.next(),
        )

    def mark_published(self, idempotency_key: str, *, receipt: PublicationReceipt) -> None:
        if not isinstance(receipt, PublicationReceipt):
            raise TypeError("receipt debe ser PublicationReceipt")
        record = self.get_record(idempotency_key)
        self.store.finish_automation_run(
            record.run_id,
            AutomationRunStatus.SUCCEEDED,
            actor_id=self.scheduler_actor_id,
            draft_id=record.draft_id,
            now=self._clock.next(),
        )

    def mark_failed(
        self,
        idempotency_key: str,
        *,
        reason: str,
        ambiguous: bool,
    ) -> None:
        record = self.get_record(idempotency_key)
        self.store.finish_automation_run(
            record.run_id,
            AutomationRunStatus.UNKNOWN if ambiguous else AutomationRunStatus.FAILED,
            actor_id=self.scheduler_actor_id,
            draft_id=record.draft_id,
            error=_safe_text(reason, maximum=1_000),
            now=self._clock.next(),
        )

    def get_record(self, idempotency_key: str) -> StoredAutomation:
        key = _required_identifier(idempotency_key, "idempotency_key")
        with self._records_lock:
            record = self._records.get(key)
        if record is None:
            raise AutomationError("El slot no está reclamado en este proceso")
        return record

    def _set_record(self, key: str, record: StoredAutomation) -> None:
        with self._records_lock:
            self._records[key] = record

    def _persist_media(self, media: GeneratedMedia) -> Path:
        self.media_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.media_root, 0o700)
        extension = _MIME_EXTENSIONS[media.mime_type]
        destination = self.media_root / f"{media.sha256}{extension}"
        if destination.parent != self.media_root:
            raise AutomationError("La ruta persistente de media no es segura")

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(destination, flags, 0o600)
        except FileExistsError:
            info = destination.lstat()
            if not stat.S_ISREG(info.st_mode) or destination.is_symlink():
                raise AutomationError(
                    "El destino de media existente no es un archivo seguro"
                ) from None
            existing = destination.read_bytes()
            if hashlib.sha256(existing).hexdigest() != media.sha256:
                raise AutomationError(
                    "El archivo de media existente no coincide con su hash"
                ) from None
            os.chmod(destination, 0o600)
            return destination
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(media.content)
                output.flush()
                os.fsync(output.fileno())
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return destination


class PlatformXPublisher:
    """Publisher directo con aprobación separada, intento idempotente y kill switches."""

    def __init__(
        self,
        *,
        store: PlatformStore,
        repository: PlatformAutomationRepository,
        x_client: XApiClient,
        reviewer_actor_id: str,
        publisher_actor_id: str,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.repository = repository
        self.x_client = x_client
        self.reviewer_actor_id = _required_identifier(reviewer_actor_id, "reviewer_actor_id")
        self.publisher_actor_id = _required_identifier(publisher_actor_id, "publisher_actor_id")
        service_identities = {
            repository.scheduler_actor_id,
            repository.author_actor_id,
            self.reviewer_actor_id,
            self.publisher_actor_id,
        }
        if len(service_identities) != 4:
            raise ValueError(
                "scheduler, autor, reviewer y publisher deben ser identidades distintas"
            )
        self._environ = environ
        self._clock = _UniqueUtcClock(clock or (lambda: datetime.now(UTC)))

    def publish(self, prepared: PreparedAutomation) -> PublicationReceipt:
        if not isinstance(prepared, PreparedAutomation):
            raise TypeError("prepared debe ser PreparedAutomation")
        self._require_runtime_gates(prepared)

        key = prepared.claim.idempotency_key
        record = self.repository.get_record(key)
        if record.draft_id is None or record.snapshot_hash is None:
            raise AutomationError("El borrador directo no está persistido")
        if record.text != prepared.draft.text or record.media_sha256 != (
            prepared.media.sha256 if prepared.media is not None else None
        ):
            raise AutomationError("El contenido preparado no coincide con el snapshot persistido")

        draft = self.store.get_draft(record.draft_id, actor_id=self.publisher_actor_id)
        status = DraftStatus(draft.status)
        if status is DraftStatus.IN_REVIEW:
            self.store.approve_draft(
                record.draft_id,
                actor_id=self.reviewer_actor_id,
                expected_snapshot_hash=record.snapshot_hash,
                reason=(
                    "Aprobación de servicio para modo direct autorizado; "
                    "snapshot e idempotencia verificados."
                ),
                now=self._clock.next(),
            )
        elif status not in {DraftStatus.APPROVED, DraftStatus.PUBLISHED}:
            raise AutomationError("El borrador no está listo para una publicación directa")

        attempt_now = self._clock.next()
        attempt = self.store.create_publish_attempt(
            record.draft_id,
            actor_id=self.publisher_actor_id,
            expected_snapshot_hash=record.snapshot_hash,
            idempotency_key=f"{key}:x",
            channel="x",
            now=attempt_now,
        )
        attempt_status = PublishStatus(attempt.status)
        is_new_attempt = attempt_status is PublishStatus.PENDING and _same_instant(
            attempt.started_at, attempt_now
        )
        if not is_new_attempt:
            return self._resolve_existing_attempt(attempt)

        try:
            self.repository.begin_publishing(key)
        except Exception as exc:
            self._finish_attempt(
                attempt.id,
                PublishStatus.FAILED,
                error=_error_label("inicio de publicación", exc),
            )
            raise AutomationError(
                "El claim directo no superó la validación previa; no se contactó a X"
            ) from exc

        media_ids: list[str] = []
        if prepared.media is not None:
            try:
                uploaded = self.x_client.upload_image(
                    prepared.media.content,
                    filename=prepared.media.filename,
                    mime_type=prepared.media.mime_type,
                    alt_text=prepared.media.alt_text,
                )
                media_ids.append(uploaded.id)
            except (AmbiguousMediaError, XApiError) as exc:
                self._finish_attempt(
                    attempt.id,
                    PublishStatus.FAILED,
                    error=_error_label("carga de media", exc),
                )
                raise AutomationError("La imagen no se confirmó; no se envió el post a X") from exc
            except Exception as exc:
                self._finish_attempt(
                    attempt.id,
                    PublishStatus.FAILED,
                    error=_error_label("carga de media", exc),
                )
                raise AutomationError("La imagen no se confirmó; no se envió el post a X") from exc

        # La carga de media es una frontera temporal: configuración, kill switches,
        # autorización y hash del slot se vuelven a comprobar antes de crear el post.
        try:
            self._require_runtime_gates(prepared)
            self.repository.begin_publishing(key)
        except Exception as exc:
            self._finish_attempt(
                attempt.id,
                PublishStatus.FAILED,
                error=_error_label("revalidación previa al post", exc),
            )
            raise AutomationError(
                "La autorización cambió después de preparar la media; no se envió el post a X"
            ) from exc

        try:
            response = self.x_client.create_post(
                prepared.draft.text,
                media_ids=media_ids,
                made_with_ai=bool(media_ids),
            )
        except AmbiguousPublishError as exc:
            self._finish_attempt(
                attempt.id,
                PublishStatus.UNKNOWN,
                error="X no confirmó si llegó a crear la publicación.",
            )
            raise PublicationAmbiguousError(
                "X no confirmó la publicación; se bloqueó cualquier reintento automático"
            ) from exc
        except XApiError as exc:
            self._finish_attempt(
                attempt.id,
                PublishStatus.FAILED,
                error=_error_label("creación del post", exc),
            )
            raise AutomationError("X rechazó la publicación de forma explícita") from exc
        except Exception as exc:
            self._finish_attempt(
                attempt.id,
                PublishStatus.UNKNOWN,
                error=_error_label("respuesta del post", exc),
            )
            raise PublicationAmbiguousError(
                "La respuesta de X fue inconclusa; se bloqueó el reintento automático"
            ) from exc

        try:
            self.store.finish_publish_attempt(
                attempt.id,
                PublishStatus.SUCCEEDED,
                actor_id=self.publisher_actor_id,
                provider_post_id=response.id,
                now=self._clock.next(),
            )
        except Exception as exc:
            raise PublicationAmbiguousError(
                "X confirmó el post, pero falló su confirmación persistente; requiere conciliación"
            ) from exc
        return PublicationReceipt(provider_post_id=response.id, channel="x")

    def _require_runtime_gates(self, prepared: PreparedAutomation) -> None:
        environment = os.environ if self._environ is None else self._environ
        if environment.get(LIVE_PUBLISH_ENV, "").strip().casefold() != "true":
            raise AutomationError(f"Publicación bloqueada: {LIVE_PUBLISH_ENV} no está activo")
        if environment.get(DIRECT_PUBLISH_ENV, "").strip().casefold() != "true":
            raise AutomationError(f"Publicación bloqueada: {DIRECT_PUBLISH_ENV} no está activo")
        if prepared.claim.slot.mode is not AutomationMode.DIRECT:
            raise AutomationError("El publisher directo solo admite slots en modo direct")
        if not prepared.candidate.evidence_verified:
            raise AutomationError("La publicación directa requiere evidencia verificada")
        if not draft_matches_expected_evidence(prepared.draft, prepared.claim.slot):
            raise AutomationError(
                "La cifra o fuente del borrador no coincide con la evidencia autorizada"
            )

    def _resolve_existing_attempt(self, attempt: Any) -> PublicationReceipt:
        status = PublishStatus(attempt.status)
        if status is PublishStatus.SUCCEEDED and attempt.provider_post_id:
            return PublicationReceipt(provider_post_id=attempt.provider_post_id, channel="x")
        if status in {PublishStatus.PENDING, PublishStatus.UNKNOWN}:
            raise PublicationAmbiguousError(
                "Existe un intento previo pendiente o desconocido; no se reintentará"
            )
        raise AutomationError("Existe un intento previo fallido; requiere intervención humana")

    def _finish_attempt(
        self,
        attempt_id: str,
        status: PublishStatus,
        *,
        error: str,
    ) -> None:
        try:
            self.store.finish_publish_attempt(
                attempt_id,
                status,
                actor_id=self.publisher_actor_id,
                error=_safe_text(error, maximum=1_000),
                now=self._clock.next(),
            )
        except Exception:
            # El intento queda PENDING, que impide un reintento automático posterior.
            return


class ReviewNotificationDeliveryStatus(StrEnum):
    SENT = "sent"
    FAILED = "failed"
    UNKNOWN = "unknown"
    BUSY = "busy"


@dataclass(frozen=True, slots=True)
class ReviewNotificationDeliveryResult:
    notification_id: str
    automation_run_id: str
    status: ReviewNotificationDeliveryStatus
    detail: str


class AutomationReviewNotificationWorker:
    """Consume la outbox de revisión sin acceso a MiniMax ni a credenciales de X."""

    def __init__(
        self,
        *,
        store: PlatformStore,
        telegram_client: TelegramApiClient,
        actor_id: str,
        media_root: Path = DEFAULT_MEDIA_ROOT,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.telegram_client = telegram_client
        self.actor_id = _required_identifier(actor_id, "actor_id")
        self.media_root = require_trusted_media_root(media_root)
        self._clock = clock or (lambda: datetime.now(UTC))

    def drain(
        self,
        *,
        limit: int = DEFAULT_REVIEW_NOTIFICATION_LIMIT,
    ) -> tuple[ReviewNotificationDeliveryResult, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("limit debe estar entre 1 y 20")
        results = [
            ReviewNotificationDeliveryResult(
                notification_id=item.id,
                automation_run_id=item.automation_run_id,
                status=ReviewNotificationDeliveryStatus.UNKNOWN,
                detail="La lease venció; la entrega requiere conciliación y no se reenviará.",
            )
            for item in self.store.expire_automation_review_notification_claims(
                actor_id=self.actor_id,
                now=self._now(),
            )
        ]
        for _ in range(limit):
            claim = self.store.claim_automation_review_notification(
                actor_id=self.actor_id,
                now=self._now(),
            )
            if claim is None:
                break
            results.append(self._deliver(claim))
        return tuple(results)

    def deliver_one(self, notification_id: str) -> ReviewNotificationDeliveryResult:
        notification = self.store.get_automation_review_notification(
            notification_id,
            actor_id=self.actor_id,
        )
        status = notification.status_value
        if status is AutomationReviewNotificationStatus.SENT:
            return ReviewNotificationDeliveryResult(
                notification.id,
                notification.automation_run_id,
                ReviewNotificationDeliveryStatus.SENT,
                "La revisión ya estaba entregada.",
            )
        if status is AutomationReviewNotificationStatus.FAILED:
            return ReviewNotificationDeliveryResult(
                notification.id,
                notification.automation_run_id,
                ReviewNotificationDeliveryStatus.FAILED,
                "Telegram rechazó previamente la entrega.",
            )
        if status is AutomationReviewNotificationStatus.UNKNOWN:
            return ReviewNotificationDeliveryResult(
                notification.id,
                notification.automation_run_id,
                ReviewNotificationDeliveryStatus.UNKNOWN,
                "La entrega previa es ambigua y no se reenviará.",
            )
        claim = self.store.claim_automation_review_notification(
            actor_id=self.actor_id,
            notification_id=notification.id,
            now=self._now(),
        )
        if claim is None:
            return ReviewNotificationDeliveryResult(
                notification.id,
                notification.automation_run_id,
                ReviewNotificationDeliveryStatus.BUSY,
                "Otro worker conserva el claim de la notificación.",
            )
        return self._deliver(claim)

    def _deliver(
        self,
        claim: AutomationReviewNotificationClaim,
    ) -> ReviewNotificationDeliveryResult:
        notification = claim.notification
        external_effect_started = False
        try:
            current = self._validate_claim(claim)
            photo: tuple[bytes, str, str] | None = None
            if current.media_sha256 is not None and current.photo_message_id is None:
                photo = self._load_media(current.media_sha256)
            callback_now = self._now()
            approve, reject = self.store.prepare_automation_review_notification_callbacks(
                current.id,
                actor_id=self.actor_id,
                claim_token=claim.claim_token,
                claim_fence=claim.claim_fence,
                expires_at=callback_now + timedelta(hours=23),
                now=callback_now,
            )
            keyboard = {
                "inline_keyboard": [
                    [
                        {
                            "text": "✅ Aprobar snapshot",
                            "callback_data": approval_callback_data(approve.nonce),
                        },
                        {
                            "text": "❌ Rechazar",
                            "callback_data": rejection_callback_data(reject.nonce),
                        },
                    ]
                ]
            }
            if photo is not None:
                content, filename, mime_type = photo
                self._validate_claim(claim)
                external_effect_started = True
                photo_result = self.telegram_client.send_photo_bytes(
                    current.chat_id,
                    content,
                    filename=filename,
                    mime_type=mime_type,
                    caption=(
                        "COLMAT · VISTA PREVIA DE IMAGEN\n"
                        f"Borrador: {current.draft_id}\n"
                        f"Snapshot: {current.snapshot_hash}"
                    )[:1024],
                )
                photo_message_id = _telegram_message_id(photo_result)
                self.store.record_automation_review_notification_photo(
                    current.id,
                    actor_id=self.actor_id,
                    claim_token=claim.claim_token,
                    claim_fence=claim.claim_fence,
                    telegram_message_id=photo_message_id,
                    now=self._now(),
                )
            current = self._validate_claim(claim)
            external_effect_started = True
            message_result = self.telegram_client.send_message(
                current.chat_id,
                _automation_review_message(current),
                reply_markup=keyboard,
            )
            review_message_id = _telegram_message_id(message_result)
        except TelegramApiError as exc:
            ambiguous = isinstance(exc, (TelegramTransportError, TelegramProtocolError))
            return self._finish_failure(claim, ambiguous=ambiguous)
        except (AutomationError, OSError, PlatformStoreError, ValueError):
            return self._finish_failure(claim, ambiguous=external_effect_started)
        except Exception:
            return self._finish_failure(claim, ambiguous=external_effect_started)

        try:
            self.store.finish_automation_review_notification(
                notification.id,
                AutomationReviewNotificationStatus.SENT,
                actor_id=self.actor_id,
                claim_token=claim.claim_token,
                claim_fence=claim.claim_fence,
                review_message_id=review_message_id,
                now=self._now(),
            )
        except Exception:
            return self._finish_failure(claim, ambiguous=True)
        return ReviewNotificationDeliveryResult(
            notification.id,
            notification.automation_run_id,
            ReviewNotificationDeliveryStatus.SENT,
            "Revisión entregada con callbacks ligados al snapshot.",
        )

    def _finish_failure(
        self,
        claim: AutomationReviewNotificationClaim,
        *,
        ambiguous: bool,
    ) -> ReviewNotificationDeliveryResult:
        target = (
            AutomationReviewNotificationStatus.UNKNOWN
            if ambiguous
            else AutomationReviewNotificationStatus.FAILED
        )
        try:
            self.store.finish_automation_review_notification(
                claim.notification.id,
                target,
                actor_id=self.actor_id,
                claim_token=claim.claim_token,
                claim_fence=claim.claim_fence,
                error=(
                    "La entrega Telegram es ambigua; no se reenviará automáticamente."
                    if ambiguous
                    else "Telegram rechazó la entrega o la media local no fue válida."
                ),
                now=self._now(),
            )
        except Exception:
            ambiguous = True
        return ReviewNotificationDeliveryResult(
            claim.notification.id,
            claim.notification.automation_run_id,
            (
                ReviewNotificationDeliveryStatus.UNKNOWN
                if ambiguous
                else ReviewNotificationDeliveryStatus.FAILED
            ),
            (
                "La entrega requiere conciliación y no se reenviará."
                if ambiguous
                else "La entrega fue rechazada de forma explícita antes de confirmarse."
            ),
        )

    def _validate_claim(
        self,
        claim: AutomationReviewNotificationClaim,
    ) -> Any:
        return self.store.validate_automation_review_notification_claim(
            claim.notification.id,
            actor_id=self.actor_id,
            claim_token=claim.claim_token,
            claim_fence=claim.claim_fence,
            now=self._now(),
        )

    def _load_media(self, digest: str) -> tuple[bytes, str, str]:
        asset = self.store.get_media_asset_by_sha256(digest, actor_id=self.actor_id)
        if asset is None or asset.mime_type not in _MIME_EXTENSIONS:
            raise AutomationError("La revisión referencia una imagen inexistente o inválida")
        metadata = asset.asset_metadata
        if not isinstance(metadata, dict):
            raise AutomationError("La imagen no tiene metadatos verificables")
        filename = metadata.get("filename")
        if not isinstance(filename, str) or _SAFE_FILENAME.fullmatch(filename) is None:
            raise AutomationError("La imagen no tiene un nombre seguro")
        parsed = urlsplit(asset.url)
        if (
            parsed.scheme != "file"
            or parsed.netloc not in {"", "localhost"}
            or parsed.query
            or parsed.fragment
        ):
            raise AutomationError("El worker solo admite media local persistida")
        candidate = Path(unquote(parsed.path))
        try:
            info = candidate.lstat()
            path = candidate.resolve(strict=True)
            path.relative_to(self.media_root)
        except (OSError, ValueError) as exc:
            raise AutomationError("La media no pertenece al almacén seguro") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise AutomationError("La media no es un archivo regular")
        content = path.read_bytes()
        if not content or len(content) > MAX_TELEGRAM_PHOTO_BYTES:
            raise AutomationError("La imagen excede los límites de Telegram")
        if hashlib.sha256(content).hexdigest() != digest:
            raise AutomationError("La imagen no coincide con el snapshot")
        if asset.byte_size is not None and asset.byte_size != len(content):
            raise AutomationError("El tamaño de la imagen no coincide")
        if sniff_supported_image_mime(content) != asset.mime_type:
            raise AutomationError("La firma binaria de la imagen no coincide con su MIME")
        return content, filename, asset.mime_type

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("El reloj del worker debe incluir zona horaria")
        return value.astimezone(UTC)


class TelegramAutomationNotifier:
    """Entrega alertas y controles de revisión ligados al snapshot persistido."""

    def __init__(
        self,
        client: TelegramApiClient,
        *,
        chat_id: int | str,
        store: PlatformStore | None = None,
        repository: PlatformAutomationRepository | None = None,
        reviewer_telegram_user_id: int | str | None = None,
        actor_id: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.client = client
        if isinstance(chat_id, bool) or not isinstance(chat_id, (int, str)):
            raise ValueError("chat_id no es válido")
        self.chat_id = chat_id
        review_options = (store, repository, reviewer_telegram_user_id, actor_id)
        if any(value is not None for value in review_options) and not all(
            value is not None for value in review_options
        ):
            raise ValueError(
                "Los controles de revisión exigen store, repository, reviewer y actor_id"
            )
        self.store = store
        self.repository = repository
        self.reviewer_telegram_user_id = reviewer_telegram_user_id
        self.actor_id = _required_identifier(actor_id, "actor_id") if actor_id is not None else None
        self._clock = clock or (lambda: datetime.now(UTC))
        self.review_worker: AutomationReviewNotificationWorker | None = None
        if (
            store is not None
            and repository is not None
            and reviewer_telegram_user_id is not None
            and self.actor_id is not None
        ):
            repository.configure_review_destination(
                telegram_user_id=reviewer_telegram_user_id,
                chat_id=chat_id,
            )
            self.review_worker = AutomationReviewNotificationWorker(
                store=store,
                telegram_client=client,
                actor_id=self.actor_id,
                media_root=repository.media_root,
                clock=self._clock,
            )

    def notify(self, event: AutomationEvent) -> None:
        if not isinstance(event, AutomationEvent):
            raise TypeError("event debe ser AutomationEvent")
        if event.kind is AutomationEventKind.REVIEW_REQUIRED and self.store is not None:
            self._notify_review(event)
            return
        labels = {
            AutomationEventKind.REVIEW_REQUIRED: "REVISIÓN REQUERIDA",
            AutomationEventKind.PUBLISHED: "PUBLICADO",
            AutomationEventKind.DIRECT_BLOCKED: "DIRECT BLOQUEADO",
            AutomationEventKind.FAILED: "FALLO SEGURO",
            AutomationEventKind.UNKNOWN: "RESULTADO DESCONOCIDO",
        }
        lines = [
            f"COLMAT · {labels[event.kind]}",
            f"Slot: {event.slot_id}",
            f"Clave: {event.idempotency_key}",
            f"Detalle: {_safe_text(event.detail, maximum=2_800)}",
        ]
        if event.engagement_score is not None:
            lines.append(f"Puntaje editorial: {event.engagement_score}/100 (no predice alcance)")
        if event.provider_post_id is not None:
            lines.append(f"ID de X: {event.provider_post_id}")
        self.client.send_message(self.chat_id, "\n".join(lines)[:4096])

    def _notify_review(self, event: AutomationEvent) -> None:
        if (
            self.store is None
            or self.repository is None
            or self.reviewer_telegram_user_id is None
            or self.actor_id is None
        ):  # pragma: no cover - protegido por __init__
            raise AutomationError("Los controles Telegram no están configurados")
        record = self.repository.get_record(event.idempotency_key)
        if record.draft_id is None or record.snapshot_hash is None or record.text is None:
            raise AutomationError("El borrador no tiene un snapshot notificable")
        notification = self.store.get_automation_review_notification_for_run(
            record.run_id,
            actor_id=self.actor_id,
        )
        if (
            notification.draft_id != record.draft_id
            or notification.snapshot_hash != record.snapshot_hash
            or notification.text != record.text
            or notification.detail != _safe_text(event.detail, maximum=1_000)
            or notification.engagement_score != event.engagement_score
        ):
            raise AutomationError("La outbox no coincide con el evento de revisión")
        if self.review_worker is None:  # pragma: no cover - protegido por __init__
            raise AutomationError("El worker de revisión Telegram no está configurado")
        result = self.review_worker.deliver_one(notification.id)
        if result.status is not ReviewNotificationDeliveryStatus.SENT:
            raise AutomationError(result.detail)


def _automation_review_message(notification: Any) -> str:
    return "\n".join(
        (
            "COLMAT · REVISIÓN HUMANA REQUERIDA",
            f"Borrador: {notification.draft_id}",
            f"Snapshot: {notification.snapshot_hash}",
            f"Run: {notification.automation_run_id}",
            f"Detalle: {notification.detail}",
            (
                f"Puntaje editorial: {notification.engagement_score}/100 "
                "(no predice ni garantiza alcance)"
            ),
            "Cifra y fuente: requieren verificación humana antes de aprobar.",
            "",
            "Texto propuesto:",
            notification.text,
        )
    )[:4096]


def _telegram_message_id(value: object) -> int:
    if not isinstance(value, Mapping):
        raise TelegramProtocolError("Telegram no devolvió un mensaje verificable")
    message_id = value.get("message_id")
    if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
        raise TelegramProtocolError("Telegram no devolvió message_id válido")
    return message_id


def _image_alt_text(request: ImageGenerationRequest) -> str:
    description = " ".join(request.draft.visual.descripcion.split())
    value = f"{request.draft.figure}. {description} Fuente: {request.draft.source}."
    return value[:1_000].rstrip()


def _required_identifier(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} debe identificar un actor")
    normalized = value.strip()
    if len(normalized) > 120 or _CONTROL_CHARACTERS.search(normalized):
        raise ValueError(f"{field_name} no es válido")
    return normalized


def _same_instant(left: datetime, right: datetime) -> bool:
    return (
        isinstance(left, datetime)
        and left.tzinfo is not None
        and left.utcoffset() is not None
        and left.astimezone(UTC) == right.astimezone(UTC)
    )


def _safe_text(value: object, *, maximum: int) -> str:
    normalized = _CONTROL_CHARACTERS.sub(" ", str(value))
    for pattern in _SECRET_PATTERNS:
        normalized = pattern.sub("[REDACTED]", normalized)
    normalized = " ".join(normalized.split()) or "Sin detalle operativo."
    if len(normalized) > maximum:
        normalized = normalized[: maximum - 1].rstrip() + "…"
    return normalized


def _error_label(stage: str, error: Exception) -> str:
    return f"{stage} falló de forma segura ({type(error).__name__})."
