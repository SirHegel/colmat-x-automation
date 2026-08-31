from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

import yaml

from colmat_x.editorial import (
    EditorialCategory,
    EditorialDraft,
    EditorialPolicy,
    EngagementAssessment,
    Institution,
    assess_engagement,
    validate_ai_draft,
)
from colmat_x.image_validation import (
    SUPPORTED_IMAGE_MIME_TYPES,
    sniff_supported_image_mime,
)
from colmat_x.yaml_utils import load_yaml_unique

AUTOMATION_TIMEZONE = "America/Bogota"
DIRECT_PUBLISH_ENV = "COLMAT_DIRECT_PUBLISH_ENABLED"
MIN_DIRECT_ENGAGEMENT_SCORE = 80
MAX_DAILY_LIMIT = 10
MAX_GENERATED_IMAGE_BYTES = 5 * 1024 * 1024
DEFAULT_AUTOMATION_CONFIG_PATH = Path("config/automation.yaml")

_SLOT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{1,49}$")
_TIME_PATTERN = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
_EDITORIAL_MONTH_PATTERN = re.compile(r"^[0-9]{4}-(?:0[1-9]|1[0-2])$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SAFE_FILENAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_ALLOWED_IMAGE_MIME_TYPES = SUPPORTED_IMAGE_MIME_TYPES


class AutomationError(RuntimeError):
    """Error controlado del núcleo de programación."""


class AutomationConfigurationError(AutomationError, ValueError):
    """La programación no es cerrada o contradice los gates de seguridad."""


class AutomationGenerationError(AutomationError):
    """El generador no produjo un artefacto editorial utilizable."""


class PublicationAmbiguousError(AutomationError):
    """El publisher no puede confirmar si el proveedor llegó a publicar."""


class AutomationMode(StrEnum):
    HUMAN_REVIEW = "human_review"
    DIRECT = "direct"


class AutomationWeekday(StrEnum):
    """Días locales canónicos, ordenados como ``date.weekday()``."""

    MONDAY = "lunes"
    TUESDAY = "martes"
    WEDNESDAY = "miércoles"
    THURSDAY = "jueves"
    FRIDAY = "viernes"
    SATURDAY = "sábado"
    SUNDAY = "domingo"


ALL_AUTOMATION_WEEKDAYS = tuple(AutomationWeekday)


class ClaimDecision(StrEnum):
    CLAIMED = "claimed"
    DUPLICATE = "duplicate"
    DUPLICATE_FAILED = "duplicate_failed"
    DUPLICATE_UNKNOWN = "duplicate_unknown"
    DAILY_LIMIT = "daily_limit"


class AutomationStatus(StrEnum):
    REVIEW_REQUIRED = "review_required"
    PUBLISHED = "published"
    SKIPPED_DUPLICATE = "skipped_duplicate"
    SKIPPED_DAILY_LIMIT = "skipped_daily_limit"
    DIRECT_BLOCKED = "direct_blocked"
    FAILED = "failed"
    UNKNOWN = "unknown"


class AutomationEventKind(StrEnum):
    REVIEW_REQUIRED = "review_required"
    PUBLISHED = "published"
    DIRECT_BLOCKED = "direct_blocked"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AutomationSlot:
    id: str
    at: time
    mode: AutomationMode
    category: EditorialCategory
    institution: Institution
    brief: str
    weekdays: tuple[AutomationWeekday, ...] = ALL_AUTOMATION_WEEKDAYS
    generate_image: bool = False
    evidence_verified: bool = False
    evidence_reference: str | None = None
    evidence_expected_figure: str | None = None
    evidence_expected_source: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or _SLOT_ID_PATTERN.fullmatch(self.id) is None:
            raise AutomationConfigurationError(
                "slot.id debe usar 2-50 caracteres: minúsculas, números, _ o -"
            )
        if not isinstance(self.at, time) or self.at.tzinfo is not None:
            raise AutomationConfigurationError("slot.at debe ser una hora local sin zona")
        if not isinstance(self.mode, AutomationMode):
            raise AutomationConfigurationError("slot.mode debe ser human_review o direct")
        if not isinstance(self.category, EditorialCategory):
            raise AutomationConfigurationError("slot.category no pertenece a la taxonomía")
        if not isinstance(self.institution, Institution):
            raise AutomationConfigurationError("slot.institution no es canónica")
        if not isinstance(self.brief, str):
            raise AutomationConfigurationError("slot.brief debe ser texto")
        normalized_brief = " ".join(self.brief.split())
        if not 10 <= len(normalized_brief) <= 1_000:
            raise AutomationConfigurationError("slot.brief debe tener entre 10 y 1000 caracteres")
        if not isinstance(self.weekdays, tuple) or not self.weekdays:
            raise AutomationConfigurationError("slot.weekdays debe ser una lista no vacía")
        if any(not isinstance(day, AutomationWeekday) for day in self.weekdays):
            raise AutomationConfigurationError("slot.weekdays contiene un día no canónico")
        if len(set(self.weekdays)) != len(self.weekdays):
            raise AutomationConfigurationError("slot.weekdays no admite duplicados")
        canonical_weekdays = tuple(day for day in ALL_AUTOMATION_WEEKDAYS if day in self.weekdays)
        if not isinstance(self.generate_image, bool):
            raise AutomationConfigurationError("slot.generate_image debe ser booleano")
        if not isinstance(self.evidence_verified, bool):
            raise AutomationConfigurationError("slot.evidence_verified debe ser booleano")
        reference = self.evidence_reference
        if reference is not None:
            if not isinstance(reference, str):
                raise AutomationConfigurationError("slot.evidence_reference debe ser texto o null")
            reference = " ".join(reference.split())
            if not 3 <= len(reference) <= 500:
                raise AutomationConfigurationError(
                    "slot.evidence_reference debe tener entre 3 y 500 caracteres"
                )
            object.__setattr__(self, "evidence_reference", reference)
        if self.evidence_verified and reference is None:
            raise AutomationConfigurationError(
                "slot.evidence_verified exige una referencia externa auditable"
            )
        expected_figure = _normalize_optional_slot_evidence(
            self.evidence_expected_figure,
            field="slot.evidence_expected_figure",
            minimum=1,
            maximum=80,
        )
        expected_source = _normalize_optional_slot_evidence(
            self.evidence_expected_source,
            field="slot.evidence_expected_source",
            minimum=3,
            maximum=200,
        )
        if (expected_figure is None) != (expected_source is None):
            raise AutomationConfigurationError(
                "slot.evidence requiere expected_figure y expected_source juntos"
            )
        if self.evidence_verified and expected_figure is None:
            raise AutomationConfigurationError(
                "slot.evidence_verified exige cifra y fuente esperadas concretas"
            )
        object.__setattr__(self, "evidence_expected_figure", expected_figure)
        object.__setattr__(self, "evidence_expected_source", expected_source)
        object.__setattr__(self, "brief", normalized_brief)
        object.__setattr__(self, "weekdays", canonical_weekdays)

    def runs_on(self, local_date: date) -> bool:
        """Indica si el slot está autorizado para esa fecha local."""

        if isinstance(local_date, datetime) or not isinstance(local_date, date):
            raise TypeError("local_date debe ser date")
        return ALL_AUTOMATION_WEEKDAYS[local_date.weekday()] in self.weekdays


@dataclass(frozen=True)
class AutomationConfig:
    version: int
    timezone: str
    daily_limit: int
    direct_enabled: bool
    direct_min_engagement_score: int
    slots: tuple[AutomationSlot, ...]

    def __post_init__(self) -> None:
        if self.version != 1:
            raise AutomationConfigurationError("La única versión de automatización admitida es 1")
        if self.timezone != AUTOMATION_TIMEZONE:
            raise AutomationConfigurationError(
                f"La automatización debe usar exclusivamente {AUTOMATION_TIMEZONE}"
            )
        if type(self.daily_limit) is not int or not 1 <= self.daily_limit <= MAX_DAILY_LIMIT:
            raise AutomationConfigurationError(
                f"daily_limit debe ser un entero entre 1 y {MAX_DAILY_LIMIT}"
            )
        if not isinstance(self.direct_enabled, bool):
            raise AutomationConfigurationError("direct.enabled debe ser booleano")
        if (
            type(self.direct_min_engagement_score) is not int
            or not MIN_DIRECT_ENGAGEMENT_SCORE <= self.direct_min_engagement_score <= 100
        ):
            raise AutomationConfigurationError(
                "direct.minimum_engagement_score debe estar entre "
                f"{MIN_DIRECT_ENGAGEMENT_SCORE} y 100"
            )
        if not isinstance(self.slots, tuple) or not self.slots:
            raise AutomationConfigurationError("slots debe contener al menos un slot programado")
        peak_daily_slots = max(
            sum(day in slot.weekdays for slot in self.slots) for day in ALL_AUTOMATION_WEEKDAYS
        )
        if peak_daily_slots > self.daily_limit:
            raise AutomationConfigurationError("La cantidad de slots supera daily_limit")
        if any(not isinstance(slot, AutomationSlot) for slot in self.slots):
            raise AutomationConfigurationError("slots contiene un valor inválido")
        slot_ids = [slot.id for slot in self.slots]
        if len(set(slot_ids)) != len(slot_ids):
            raise AutomationConfigurationError("slot.id no admite duplicados")

    @property
    def zoneinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass(frozen=True)
class GeneratedMedia:
    content: bytes = field(repr=False)
    filename: str
    mime_type: str
    sha256: str
    alt_text: str

    def __post_init__(self) -> None:
        if not isinstance(self.content, bytes) or not self.content:
            raise AutomationGenerationError("La imagen generada debe contener bytes")
        if len(self.content) > MAX_GENERATED_IMAGE_BYTES:
            raise AutomationGenerationError("La imagen generada supera el máximo seguro de 5 MB")
        if (
            not isinstance(self.filename, str)
            or _SAFE_FILENAME_PATTERN.fullmatch(self.filename) is None
        ):
            raise AutomationGenerationError("El nombre de imagen no es seguro")
        if self.mime_type not in _ALLOWED_IMAGE_MIME_TYPES:
            raise AutomationGenerationError("La imagen debe ser JPEG, PNG o WebP")
        detected_mime_type = sniff_supported_image_mime(self.content)
        if detected_mime_type is None:
            raise AutomationGenerationError(
                "Los bytes generados no tienen una firma JPEG, PNG o WebP válida"
            )
        if detected_mime_type != self.mime_type:
            raise AutomationGenerationError(
                "El MIME declarado no coincide con la firma de la imagen generada"
            )
        if not isinstance(self.sha256, str) or _SHA256_PATTERN.fullmatch(self.sha256) is None:
            raise AutomationGenerationError("La imagen requiere un SHA-256 canónico")
        calculated = hashlib.sha256(self.content).hexdigest()
        if calculated != self.sha256:
            raise AutomationGenerationError("El SHA-256 no coincide con la imagen generada")
        if not isinstance(self.alt_text, str):
            raise AutomationGenerationError("La imagen requiere texto alternativo")
        normalized_alt = " ".join(self.alt_text.split())
        if not 1 <= len(normalized_alt) <= 1_000:
            raise AutomationGenerationError(
                "El texto alternativo debe tener entre 1 y 1000 caracteres"
            )
        object.__setattr__(self, "alt_text", normalized_alt)


@dataclass(frozen=True)
class DraftCandidate:
    draft: EditorialDraft
    evidence_verified: bool = False
    evidence_reference: str | None = None
    editorial_line_month: str | None = None
    editorial_line_version: int | None = None
    editorial_line_sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.draft, EditorialDraft):
            raise AutomationGenerationError("El candidato debe contener un EditorialDraft")
        if not isinstance(self.evidence_verified, bool):
            raise AutomationGenerationError("evidence_verified debe ser booleano")
        reference = self.evidence_reference
        if reference is not None:
            if not isinstance(reference, str):
                raise AutomationGenerationError("evidence_reference debe ser texto")
            reference = " ".join(reference.split())
            if not 3 <= len(reference) <= 500:
                raise AutomationGenerationError(
                    "evidence_reference debe tener entre 3 y 500 caracteres"
                )
            object.__setattr__(self, "evidence_reference", reference)
        if self.evidence_verified and reference is None:
            raise AutomationGenerationError(
                "La verificación de evidencia requiere una referencia auditable"
            )
        editorial_values = (
            self.editorial_line_month,
            self.editorial_line_version,
            self.editorial_line_sha256,
        )
        if any(value is not None for value in editorial_values):
            if not all(value is not None for value in editorial_values):
                raise AutomationGenerationError(
                    "La línea editorial exige mes, versión y SHA-256 juntos"
                )
            if (
                not isinstance(self.editorial_line_month, str)
                or _EDITORIAL_MONTH_PATTERN.fullmatch(self.editorial_line_month) is None
            ):
                raise AutomationGenerationError("El mes de la línea editorial no es canónico")
            if (
                isinstance(self.editorial_line_version, bool)
                or not isinstance(self.editorial_line_version, int)
                or self.editorial_line_version < 1
            ):
                raise AutomationGenerationError(
                    "La versión de la línea editorial debe ser un entero positivo"
                )
            if (
                not isinstance(self.editorial_line_sha256, str)
                or _SHA256_PATTERN.fullmatch(self.editorial_line_sha256) is None
            ):
                raise AutomationGenerationError("La línea editorial requiere un SHA-256 canónico")


@dataclass(frozen=True)
class SlotClaim:
    idempotency_key: str
    local_date: date
    scheduled_for: datetime
    slot: AutomationSlot


@dataclass(frozen=True)
class GenerationRequest:
    claim: SlotClaim
    brief: str
    category: EditorialCategory
    institution: Institution


@dataclass(frozen=True)
class ImageGenerationRequest:
    claim: SlotClaim
    draft: EditorialDraft


@dataclass(frozen=True)
class PreparedAutomation:
    claim: SlotClaim
    candidate: DraftCandidate
    assessment: EngagementAssessment
    media: GeneratedMedia | None

    @property
    def draft(self) -> EditorialDraft:
        return self.candidate.draft


@dataclass(frozen=True)
class PublicationReceipt:
    provider_post_id: str
    channel: str = "x"

    def __post_init__(self) -> None:
        for field_name, value, maximum in (
            ("provider_post_id", self.provider_post_id, 100),
            ("channel", self.channel, 30),
        ):
            if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
                raise AutomationError(f"{field_name} de publicación no es válido")
            if any(ord(character) < 32 for character in value):
                raise AutomationError(f"{field_name} contiene caracteres de control")
            object.__setattr__(self, field_name, value.strip())


@dataclass(frozen=True)
class AutomationEvent:
    kind: AutomationEventKind
    slot_id: str
    idempotency_key: str
    detail: str
    engagement_score: int | None = None
    provider_post_id: str | None = None


@dataclass(frozen=True)
class AutomationResult:
    slot_id: str
    idempotency_key: str
    status: AutomationStatus
    detail: str
    scheduled_for: datetime
    assessment: EngagementAssessment | None = None
    media_generated: bool = False
    notification_delivered: bool = False
    receipt: PublicationReceipt | None = None


class AutomationGenerator(Protocol):
    """Adaptador de IA; no recibe publisher ni puede autorizar publicación."""

    def generate_draft(self, request: GenerationRequest) -> DraftCandidate: ...

    def generate_image(self, request: ImageGenerationRequest) -> GeneratedMedia: ...


class AutomationRepository(Protocol):
    """Persistencia: claim_slot debe deduplicar y aplicar el límite de forma atómica."""

    def claim_slot(self, claim: SlotClaim, *, daily_limit: int) -> ClaimDecision: ...

    def claim_retry_slot(self, claim: SlotClaim, *, daily_limit: int) -> ClaimDecision: ...

    def save_prepared(self, prepared: PreparedAutomation) -> None: ...

    def mark_review_required(self, idempotency_key: str, *, reason: str) -> None: ...

    def mark_direct_blocked(self, idempotency_key: str, *, reason: str) -> None: ...

    def mark_published(self, idempotency_key: str, *, receipt: PublicationReceipt) -> None: ...

    def mark_failed(self, idempotency_key: str, *, reason: str, ambiguous: bool) -> None: ...


class AutomationNotifier(Protocol):
    def notify(self, event: AutomationEvent) -> None: ...


class AutomationPublisher(Protocol):
    """Único puerto capaz de publicar; solo se invoca después de todos los gates."""

    def publish(self, prepared: PreparedAutomation) -> PublicationReceipt: ...


class DailyAutomation:
    """Ejecuta slots autorizados y vencidos de la fecha local, sin llamadas de red."""

    def __init__(
        self,
        *,
        config: AutomationConfig,
        policy: EditorialPolicy,
        generator: AutomationGenerator,
        repository: AutomationRepository,
        notifier: AutomationNotifier,
        publisher: AutomationPublisher | None = None,
    ) -> None:
        self.config = config
        self.policy = policy
        self.generator = generator
        self.repository = repository
        self.notifier = notifier
        self.publisher = publisher
        self._validate_policy_references()

    def run_due(
        self,
        *,
        now: datetime | None = None,
        environ: Mapping[str, str] | None = None,
        progress: Callable[[str, str, str | None], None] | None = None,
    ) -> tuple[AutomationResult, ...]:
        instant = now or datetime.now(UTC)
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("now debe incluir zona horaria")
        local_now = instant.astimezone(self.config.zoneinfo)
        environment = os.environ if environ is None else environ
        due_slots = sorted(
            (
                slot
                for slot in self.config.slots
                if slot.runs_on(local_now.date())
                and slot.at <= local_now.time().replace(tzinfo=None)
            ),
            key=lambda slot: (slot.at, slot.id),
        )
        results: list[AutomationResult] = []
        for slot in due_slots:
            if progress is not None:
                with suppress(Exception):
                    progress("started", slot.id, None)
            result = self._run_slot(
                slot,
                local_date=local_now.date(),
                environment=environment,
            )
            results.append(result)
            if progress is not None:
                with suppress(Exception):
                    progress("finished", slot.id, result.status.value)
        return tuple(results)

    def run_retry(
        self,
        *,
        slot_id: str,
        local_date: date,
        idempotency_key: str,
        environ: Mapping[str, str] | None = None,
        progress: Callable[[str, str, str | None], None] | None = None,
    ) -> AutomationResult:
        """Ejecuta únicamente un retry persistido; el repositorio debe cercarlo por run id."""

        if isinstance(local_date, datetime) or not isinstance(local_date, date):
            raise TypeError("local_date debe ser date")
        slot = next((item for item in self.config.slots if item.id == slot_id), None)
        if slot is None:
            raise AutomationConfigurationError("El retry no pertenece a un slot vigente")
        if not slot.runs_on(local_date):
            raise AutomationConfigurationError(
                "El retry no coincide con un día autorizado para el slot"
            )
        expected_key = slot_idempotency_key(local_date, slot.id)
        if idempotency_key != expected_key:
            raise AutomationConfigurationError(
                "El retry no coincide con la fecha local y el slot persistidos"
            )
        environment = os.environ if environ is None else environ
        if progress is not None:
            with suppress(Exception):
                progress("retry_started", slot.id, None)
        result = self._run_slot(
            slot,
            local_date=local_date,
            environment=environment,
            retry=True,
        )
        if progress is not None:
            with suppress(Exception):
                progress("retry_finished", slot.id, result.status.value)
        return result

    def _run_slot(
        self,
        slot: AutomationSlot,
        *,
        local_date: date,
        environment: Mapping[str, str],
        retry: bool = False,
    ) -> AutomationResult:
        scheduled_for = datetime.combine(local_date, slot.at, tzinfo=self.config.zoneinfo)
        key = slot_idempotency_key(local_date, slot.id)
        claim = SlotClaim(
            idempotency_key=key,
            local_date=local_date,
            scheduled_for=scheduled_for,
            slot=slot,
        )

        try:
            claim_slot = self.repository.claim_retry_slot if retry else self.repository.claim_slot
            decision = claim_slot(claim, daily_limit=self.config.daily_limit)
        except Exception as exc:  # una frontera de adaptador nunca debe romper el scheduler
            detail = _safe_failure("El claim del repositorio", exc)
            delivered = self._notify_failure(claim, detail, ambiguous=False)
            return self._result(
                claim,
                AutomationStatus.FAILED,
                detail,
                notification_delivered=delivered,
            )

        if decision is ClaimDecision.DUPLICATE:
            return self._result(
                claim,
                AutomationStatus.SKIPPED_DUPLICATE,
                "El slot ya fue reclamado para esta fecha local.",
            )
        if decision is ClaimDecision.DUPLICATE_FAILED:
            return self._result(
                claim,
                AutomationStatus.FAILED,
                (
                    "El run persistido de este slot terminó en failed; "
                    "requiere un reintento explícito y auditado."
                ),
            )
        if decision is ClaimDecision.DUPLICATE_UNKNOWN:
            return self._result(
                claim,
                AutomationStatus.UNKNOWN,
                (
                    "El run persistido de este slot tiene resultado unknown; "
                    "requiere conciliación y no se reintentará automáticamente."
                ),
            )
        if decision is ClaimDecision.DAILY_LIMIT:
            return self._result(
                claim,
                AutomationStatus.SKIPPED_DAILY_LIMIT,
                "El límite diario atómico impidió reclamar el slot.",
            )
        if decision is not ClaimDecision.CLAIMED:
            detail = "El repositorio devolvió una decisión de claim desconocida."
            self._safe_mark_failed(claim.idempotency_key, detail, ambiguous=False)
            delivered = self._notify_failure(claim, detail, ambiguous=False)
            return self._result(
                claim,
                AutomationStatus.FAILED,
                detail,
                notification_delivered=delivered,
            )

        block_reason = self._direct_block_reason(slot, environment)
        if block_reason is not None:
            try:
                self.repository.mark_direct_blocked(key, reason=block_reason)
            except Exception as exc:
                detail = _safe_failure("El registro del bloqueo direct", exc)
                self._safe_mark_failed(key, detail, ambiguous=False)
                delivered = self._notify_failure(claim, detail, ambiguous=False)
                return self._result(
                    claim,
                    AutomationStatus.FAILED,
                    detail,
                    notification_delivered=delivered,
                )
            delivered = self._notify(
                AutomationEvent(
                    kind=AutomationEventKind.DIRECT_BLOCKED,
                    slot_id=slot.id,
                    idempotency_key=key,
                    detail=block_reason,
                )
            )
            return self._result(
                claim,
                AutomationStatus.DIRECT_BLOCKED,
                block_reason,
                notification_delivered=delivered,
            )

        try:
            prepared = self._prepare(claim)
            self.repository.save_prepared(prepared)
        except Exception as exc:  # sanitiza fallos de IA, media o persistencia inyectada
            detail = _safe_failure("La preparación del borrador", exc)
            self._safe_mark_failed(claim.idempotency_key, detail, ambiguous=False)
            delivered = self._notify_failure(claim, detail, ambiguous=False)
            return self._result(
                claim,
                AutomationStatus.FAILED,
                detail,
                notification_delivered=delivered,
            )

        if slot.mode is AutomationMode.HUMAN_REVIEW:
            return self._hold_for_review(
                prepared,
                reason="El slot exige revisión y aprobación humana.",
            )

        if prepared.draft.category is EditorialCategory.FICHA_TERRITORIO:
            return self._hold_for_review(
                prepared,
                reason=(
                    "La ficha de territorio exige validar autoría local mediante revisión humana."
                ),
            )
        if prepared.media is not None:
            return self._hold_for_review(
                prepared,
                reason=("La media generada exige revisión visual humana antes de publicarse en X."),
            )

        if slot.evidence_verified and not draft_matches_expected_evidence(prepared.draft, slot):
            return self._hold_for_review(
                prepared,
                reason=(
                    "La cifra y la fuente del borrador no coinciden literalmente con "
                    "la evidencia concreta autorizada en el slot."
                ),
            )
        if not prepared.candidate.evidence_verified:
            return self._hold_for_review(
                prepared,
                reason="La evidencia no tiene una verificación externa auditable.",
            )
        if prepared.assessment.score < self.config.direct_min_engagement_score:
            return self._hold_for_review(
                prepared,
                reason=(
                    f"El puntaje {prepared.assessment.score} no alcanza el umbral directo "
                    f"{self.config.direct_min_engagement_score}; no predice alcance."
                ),
            )
        return self._publish_direct(prepared)

    def _prepare(self, claim: SlotClaim) -> PreparedAutomation:
        request = GenerationRequest(
            claim=claim,
            brief=claim.slot.brief,
            category=claim.slot.category,
            institution=claim.slot.institution,
        )
        candidate = self.generator.generate_draft(request)
        if not isinstance(candidate, DraftCandidate):
            raise AutomationGenerationError("generate_draft debe devolver DraftCandidate")
        validated = validate_ai_draft(candidate.draft.to_mapping(), self.policy)
        if validated.category is not claim.slot.category:
            raise AutomationGenerationError("El borrador cambió la categoría solicitada")
        if validated.institution is not claim.slot.institution:
            raise AutomationGenerationError("El borrador cambió la institución solicitada")
        evidence_matches = draft_matches_expected_evidence(validated, claim.slot)
        candidate = DraftCandidate(
            draft=validated,
            evidence_verified=(
                claim.slot.evidence_verified
                and candidate.evidence_verified
                and candidate.evidence_reference == claim.slot.evidence_reference
                and evidence_matches
            ),
            evidence_reference=claim.slot.evidence_reference,
            editorial_line_month=candidate.editorial_line_month,
            editorial_line_version=candidate.editorial_line_version,
            editorial_line_sha256=candidate.editorial_line_sha256,
        )
        assessment = assess_engagement(validated)
        media = None
        if claim.slot.generate_image:
            media = self.generator.generate_image(
                ImageGenerationRequest(claim=claim, draft=validated)
            )
            if not isinstance(media, GeneratedMedia):
                raise AutomationGenerationError("generate_image debe devolver GeneratedMedia")
        return PreparedAutomation(
            claim=claim,
            candidate=candidate,
            assessment=assessment,
            media=media,
        )

    def _hold_for_review(
        self,
        prepared: PreparedAutomation,
        *,
        reason: str,
    ) -> AutomationResult:
        try:
            self.repository.mark_review_required(
                prepared.claim.idempotency_key,
                reason=reason,
            )
        except Exception as exc:
            detail = _safe_failure("El registro de revisión", exc)
            self._safe_mark_failed(prepared.claim.idempotency_key, detail, ambiguous=False)
            delivered = self._notify_failure(prepared.claim, detail, ambiguous=False)
            return self._result(
                prepared.claim,
                AutomationStatus.FAILED,
                detail,
                assessment=prepared.assessment,
                media_generated=prepared.media is not None,
                notification_delivered=delivered,
            )
        delivered = self._notify(
            AutomationEvent(
                kind=AutomationEventKind.REVIEW_REQUIRED,
                slot_id=prepared.claim.slot.id,
                idempotency_key=prepared.claim.idempotency_key,
                detail=reason,
                engagement_score=prepared.assessment.score,
            )
        )
        return self._result(
            prepared.claim,
            AutomationStatus.REVIEW_REQUIRED,
            reason,
            assessment=prepared.assessment,
            media_generated=prepared.media is not None,
            notification_delivered=delivered,
        )

    def _publish_direct(self, prepared: PreparedAutomation) -> AutomationResult:
        if self.publisher is None:  # protegido también por _direct_block_reason
            raise AutomationConfigurationError("El modo direct requiere un publisher inyectado")
        try:
            receipt = self.publisher.publish(prepared)
            if not isinstance(receipt, PublicationReceipt):
                raise AutomationError("publish debe devolver PublicationReceipt")
        except PublicationAmbiguousError as exc:
            detail = _safe_failure("La publicación directa", exc)
            self._safe_mark_failed(prepared.claim.idempotency_key, detail, ambiguous=True)
            delivered = self._notify_failure(prepared.claim, detail, ambiguous=True)
            return self._result(
                prepared.claim,
                AutomationStatus.UNKNOWN,
                detail,
                assessment=prepared.assessment,
                media_generated=prepared.media is not None,
                notification_delivered=delivered,
            )
        except Exception as exc:
            detail = _safe_failure("La publicación directa", exc)
            self._safe_mark_failed(prepared.claim.idempotency_key, detail, ambiguous=False)
            delivered = self._notify_failure(prepared.claim, detail, ambiguous=False)
            return self._result(
                prepared.claim,
                AutomationStatus.FAILED,
                detail,
                assessment=prepared.assessment,
                media_generated=prepared.media is not None,
                notification_delivered=delivered,
            )

        try:
            self.repository.mark_published(
                prepared.claim.idempotency_key,
                receipt=receipt,
            )
        except Exception as exc:
            detail = _safe_failure("La confirmación persistente de publicación", exc)
            self._safe_mark_failed(prepared.claim.idempotency_key, detail, ambiguous=True)
            delivered = self._notify_failure(prepared.claim, detail, ambiguous=True)
            return self._result(
                prepared.claim,
                AutomationStatus.UNKNOWN,
                detail,
                assessment=prepared.assessment,
                media_generated=prepared.media is not None,
                notification_delivered=delivered,
                receipt=receipt,
            )

        delivered = self._notify(
            AutomationEvent(
                kind=AutomationEventKind.PUBLISHED,
                slot_id=prepared.claim.slot.id,
                idempotency_key=prepared.claim.idempotency_key,
                detail="Publicación directa confirmada y persistida.",
                engagement_score=prepared.assessment.score,
                provider_post_id=receipt.provider_post_id,
            )
        )
        return self._result(
            prepared.claim,
            AutomationStatus.PUBLISHED,
            "Publicación directa confirmada y persistida.",
            assessment=prepared.assessment,
            media_generated=prepared.media is not None,
            notification_delivered=delivered,
            receipt=receipt,
        )

    def _direct_block_reason(
        self,
        slot: AutomationSlot,
        environment: Mapping[str, str],
    ) -> str | None:
        if slot.mode is not AutomationMode.DIRECT:
            return None
        if not self.config.direct_enabled:
            return "Modo direct bloqueado: direct.enabled no está activo en la configuración."
        if environment.get(DIRECT_PUBLISH_ENV, "").strip().casefold() != "true":
            return f"Modo direct bloqueado: {DIRECT_PUBLISH_ENV} debe valer exactamente true."
        if self.publisher is None:
            return "Modo direct bloqueado: no hay publisher inyectado."
        return None

    def _validate_policy_references(self) -> None:
        for slot in self.config.slots:
            if slot.category not in self.policy.taxonomy:
                raise AutomationConfigurationError(
                    f"El slot '{slot.id}' usa una categoría ausente de la política"
                )
            if slot.institution not in self.policy.institutions:
                raise AutomationConfigurationError(
                    f"El slot '{slot.id}' usa una institución ausente de la política"
                )

    def _safe_mark_failed(self, key: str, detail: str, *, ambiguous: bool) -> None:
        with suppress(Exception):
            self.repository.mark_failed(key, reason=detail, ambiguous=ambiguous)

    def _notify_failure(self, claim: SlotClaim, detail: str, *, ambiguous: bool) -> bool:
        return self._notify(
            AutomationEvent(
                kind=(AutomationEventKind.UNKNOWN if ambiguous else AutomationEventKind.FAILED),
                slot_id=claim.slot.id,
                idempotency_key=claim.idempotency_key,
                detail=detail,
            )
        )

    def _notify(self, event: AutomationEvent) -> bool:
        try:
            self.notifier.notify(event)
        except Exception:
            return False
        return True

    @staticmethod
    def _result(
        claim: SlotClaim,
        status: AutomationStatus,
        detail: str,
        *,
        assessment: EngagementAssessment | None = None,
        media_generated: bool = False,
        notification_delivered: bool = False,
        receipt: PublicationReceipt | None = None,
    ) -> AutomationResult:
        return AutomationResult(
            slot_id=claim.slot.id,
            idempotency_key=claim.idempotency_key,
            status=status,
            detail=detail,
            scheduled_for=claim.scheduled_for,
            assessment=assessment,
            media_generated=media_generated,
            notification_delivered=notification_delivered,
            receipt=receipt,
        )


def slot_idempotency_key(local_date: date, slot_id: str) -> str:
    """Clave estable por fecha local/slot, independiente de reintentos y procesos."""

    if isinstance(local_date, datetime) or not isinstance(local_date, date):
        raise ValueError("local_date debe ser date")
    if not isinstance(slot_id, str) or _SLOT_ID_PATTERN.fullmatch(slot_id) is None:
        raise ValueError("slot_id no tiene un formato canónico")
    return f"colmat:auto:v1:{local_date.isoformat()}:{slot_id}"


def draft_matches_expected_evidence(
    draft: EditorialDraft,
    slot: AutomationSlot,
) -> bool:
    """Liga el material generado a la cifra/fuente verificadas por un humano."""

    if not isinstance(draft, EditorialDraft) or not isinstance(slot, AutomationSlot):
        raise TypeError("draft y slot deben ser valores editoriales validados")
    if slot.evidence_expected_figure is None or slot.evidence_expected_source is None:
        return False
    text = _evidence_comparison_text(draft.text)
    expected_figure = _evidence_comparison_text(slot.evidence_expected_figure)
    expected_source = _evidence_comparison_text(slot.evidence_expected_source)
    return (
        _evidence_comparison_text(draft.figure) == expected_figure
        and _evidence_comparison_text(draft.source) == expected_source
        and _contains_evidence_literal(text, expected_figure)
        and _contains_evidence_literal(text, expected_source)
    )


def automation_slot_mapping(slot: AutomationSlot) -> dict[str, object]:
    """Representación canónica que se autoriza, reclama y audita como una unidad."""

    if not isinstance(slot, AutomationSlot):
        raise TypeError("slot debe ser AutomationSlot")
    mapped: dict[str, object] = {
        "id": slot.id,
        "at": slot.at.strftime("%H:%M"),
        "mode": slot.mode.value,
        "category": slot.category.value,
        "institution": slot.institution.value,
        "brief": slot.brief,
        "generate_image": slot.generate_image,
        "evidence": {
            "verified": slot.evidence_verified,
            "reference": slot.evidence_reference,
            "expected_figure": slot.evidence_expected_figure,
            "expected_source": slot.evidence_expected_source,
        },
    }
    if slot.weekdays != ALL_AUTOMATION_WEEKDAYS:
        mapped["weekdays"] = [day.value for day in slot.weekdays]
    return mapped


def load_automation_config(
    path: str | Path = DEFAULT_AUTOMATION_CONFIG_PATH,
    *,
    policy: EditorialPolicy,
) -> AutomationConfig:
    config_path = Path(path)
    try:
        raw = config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise AutomationConfigurationError(
            f"No se pudo leer la configuración de automatización: {config_path}"
        ) from exc
    try:
        document = load_yaml_unique(raw)
    except yaml.YAMLError as exc:
        raise AutomationConfigurationError("YAML de automatización inválido") from exc
    return parse_automation_config(document, policy=policy)


def parse_automation_config(
    document: object,
    *,
    policy: EditorialPolicy,
) -> AutomationConfig:
    """Valida una agenda ya cargada, incluida la copia persistida en plataforma."""

    root = _strict_mapping(
        document,
        {"version", "timezone", "daily_limit", "direct", "slots"},
        "raíz",
    )
    direct = _strict_mapping(
        root["direct"],
        {"enabled", "minimum_engagement_score"},
        "direct",
    )
    slots_raw = root["slots"]
    if not isinstance(slots_raw, list):
        raise AutomationConfigurationError("slots debe ser una lista")
    slots = tuple(
        _load_slot(value, index=index, policy=policy) for index, value in enumerate(slots_raw)
    )
    return AutomationConfig(
        version=_strict_int(root["version"], "version"),
        timezone=_strict_text(root["timezone"], "timezone", maximum=80),
        daily_limit=_strict_int(root["daily_limit"], "daily_limit"),
        direct_enabled=_strict_bool(direct["enabled"], "direct.enabled"),
        direct_min_engagement_score=_strict_int(
            direct["minimum_engagement_score"],
            "direct.minimum_engagement_score",
        ),
        slots=slots,
    )


def _load_slot(value: object, *, index: int, policy: EditorialPolicy) -> AutomationSlot:
    field = f"slots[{index}]"
    data = _strict_mapping(
        value,
        {
            "id",
            "at",
            "mode",
            "category",
            "institution",
            "brief",
            "generate_image",
            "evidence",
        },
        field,
        optional={"weekdays"},
    )
    evidence = _strict_mapping(
        data["evidence"],
        {"verified", "reference", "expected_figure", "expected_source"},
        f"{field}.evidence",
    )
    raw_time = _strict_text(data["at"], f"{field}.at", maximum=5)
    if _TIME_PATTERN.fullmatch(raw_time) is None:
        raise AutomationConfigurationError(f"{field}.at debe usar HH:MM en formato 24 horas")
    hour, minute = (int(part) for part in raw_time.split(":"))
    try:
        mode = AutomationMode(_strict_text(data["mode"], f"{field}.mode", maximum=20))
    except ValueError as exc:
        raise AutomationConfigurationError(f"{field}.mode debe ser human_review o direct") from exc
    try:
        category = EditorialCategory(
            _strict_text(data["category"], f"{field}.category", maximum=80)
        )
    except ValueError as exc:
        raise AutomationConfigurationError(
            f"{field}.category no pertenece a la taxonomía canónica"
        ) from exc
    try:
        institution = Institution(
            _strict_text(data["institution"], f"{field}.institution", maximum=80)
        )
    except ValueError as exc:
        raise AutomationConfigurationError(f"{field}.institution no es canónica") from exc
    if category not in policy.taxonomy:
        raise AutomationConfigurationError(f"{field}.category no está habilitada por la política")
    if institution not in policy.institutions:
        raise AutomationConfigurationError(
            f"{field}.institution no está habilitada por la política"
        )
    return AutomationSlot(
        id=_strict_text(data["id"], f"{field}.id", maximum=50),
        at=time(hour, minute),
        mode=mode,
        category=category,
        institution=institution,
        brief=_strict_text(data["brief"], f"{field}.brief", maximum=1_000),
        weekdays=_load_weekdays(data["weekdays"], field=field)
        if "weekdays" in data
        else ALL_AUTOMATION_WEEKDAYS,
        generate_image=_strict_bool(data["generate_image"], f"{field}.generate_image"),
        evidence_verified=_strict_bool(evidence["verified"], f"{field}.evidence.verified"),
        evidence_reference=_optional_strict_text(
            evidence["reference"],
            f"{field}.evidence.reference",
            maximum=500,
        ),
        evidence_expected_figure=_optional_strict_text(
            evidence["expected_figure"],
            f"{field}.evidence.expected_figure",
            maximum=80,
        ),
        evidence_expected_source=_optional_strict_text(
            evidence["expected_source"],
            f"{field}.evidence.expected_source",
            maximum=200,
        ),
    )


def _strict_mapping(
    value: object,
    required: set[str],
    field: str,
    *,
    optional: set[str] | None = None,
) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise AutomationConfigurationError(f"{field} debe ser un objeto YAML")
    if any(not isinstance(key, str) for key in value):
        raise AutomationConfigurationError(f"{field} solo admite claves de texto")
    allowed = required | (optional or set())
    unknown = sorted(set(value) - allowed)
    missing = sorted(required - set(value))
    if unknown:
        raise AutomationConfigurationError(f"Campos desconocidos en {field}: {', '.join(unknown)}")
    if missing:
        raise AutomationConfigurationError(f"Faltan campos en {field}: {', '.join(missing)}")
    return value


def _load_weekdays(value: object, *, field: str) -> tuple[AutomationWeekday, ...]:
    weekday_field = f"{field}.weekdays"
    if not isinstance(value, list) or not value:
        raise AutomationConfigurationError(f"{weekday_field} debe ser una lista no vacía")
    if len(value) > len(ALL_AUTOMATION_WEEKDAYS):
        raise AutomationConfigurationError(f"{weekday_field} no admite más de siete días")
    weekdays: list[AutomationWeekday] = []
    for raw_day in value:
        try:
            day = AutomationWeekday(raw_day)
        except (TypeError, ValueError) as exc:
            allowed = ", ".join(day.value for day in ALL_AUTOMATION_WEEKDAYS)
            raise AutomationConfigurationError(
                f"{weekday_field} solo admite valores canónicos: {allowed}"
            ) from exc
        if day in weekdays:
            raise AutomationConfigurationError(f"{weekday_field} no admite duplicados")
        weekdays.append(day)
    return tuple(day for day in ALL_AUTOMATION_WEEKDAYS if day in weekdays)


def _strict_text(value: object, field: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise AutomationConfigurationError(f"{field} debe ser texto")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > maximum:
        raise AutomationConfigurationError(f"{field} debe tener entre 1 y {maximum} caracteres")
    return normalized


def _optional_strict_text(value: object, field: str, *, maximum: int) -> str | None:
    if value is None:
        return None
    return _strict_text(value, field, maximum=maximum)


def _normalize_optional_slot_evidence(
    value: object,
    *,
    field: str,
    minimum: int,
    maximum: int,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AutomationConfigurationError(f"{field} debe ser texto o null")
    if "\x00" in value or any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise AutomationConfigurationError(f"{field} contiene Unicode no válido")
    normalized = " ".join(unicodedata.normalize("NFC", value).split())
    if not minimum <= len(normalized) <= maximum:
        raise AutomationConfigurationError(
            f"{field} debe tener entre {minimum} y {maximum} caracteres"
        )
    return normalized


def _evidence_comparison_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _contains_evidence_literal(text: str, expected: str) -> bool:
    """Evita aceptar una cifra/fuente como subcadena de otro token."""

    return re.search(rf"(?<!\w){re.escape(expected)}(?!\w)", text) is not None


def _strict_int(value: object, field: str) -> int:
    if type(value) is not int:
        raise AutomationConfigurationError(f"{field} debe ser un entero")
    return value


def _strict_bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise AutomationConfigurationError(f"{field} debe ser booleano")
    return value


def _safe_failure(stage: str, error: Exception) -> str:
    if isinstance(error, AutomationError):
        return str(error)
    return f"{stage} falló de forma segura ({type(error).__name__})."
