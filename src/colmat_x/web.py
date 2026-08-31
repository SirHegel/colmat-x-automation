from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import secrets
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from json import JSONDecodeError
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import inspect, text
from starlette.concurrency import run_in_threadpool

from colmat_x.platform_store import (
    CANONICAL_AUTOMATION_WEEKDAYS,
    AutomationMode,
    Base,
    ConflictError,
    DraftStatus,
    NotFoundError,
    PlatformStore,
    StaleSnapshotError,
)
from colmat_x.rbac import AuthorizationError, Permission, Role, can_assign_role, has_permission
from colmat_x.research_registry import (
    RESEARCH_ONLY_BRIEF_PREFIX,
    ResearchRegistry,
    load_gustavo_bueno_registry,
)
from colmat_x.telegram_api import (
    TelegramApiClient,
    TelegramConfigurationError,
    TelegramCredentials,
)
from colmat_x.telegram_bot import (
    RESEARCH_PATTERN_STEPS,
    BotAutomationMode,
    ClaimedTelegramUpdateError,
    CommandResult,
    DecisionResult,
    MalformedTelegramUpdate,
    TelegramWebhookProcessor,
    TelegramWebhookSecret,
    WebhookAuthenticationError,
    execute_bot_actions,
)
from colmat_x.web_auth import (
    CsrfError,
    InvalidChallengeError,
    InvalidSessionError,
    RateLimitError,
    WebAuthConfigurationError,
    WebAuthService,
)

MAX_TELEGRAM_UPDATE_BYTES = 1_048_576
MAX_WEB_FORM_BYTES = 16_384
TELEGRAM_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
SESSION_COOKIE = "__Host-colmat_session"
CSRF_COOKIE = "__Host-colmat_csrf"
LOGIN_CSRF_COOKIE = "__Host-colmat_login_csrf"
TEMPLATES_DIRECTORY = Path(__file__).with_name("templates")
STATIC_DIRECTORY = Path(__file__).with_name("static")
SESSION_MAX_AGE_SECONDS = 12 * 60 * 60
READINESS_CACHE_SECONDS = 5.0
PRIVILEGED_WORKER_ENVIRONMENT = frozenset(
    {
        "EXPECTED_X_USER_ID",
        "EXPECTED_X_USERNAME",
        "MINIMAX_API_KEY",
        "X_ACCESS_TOKEN",
        "X_ACCESS_TOKEN_SECRET",
        "X_CONSUMER_KEY",
        "X_CONSUMER_SECRET",
    }
)
REQUIRED_DATABASE_COLUMNS: dict[str, frozenset[str]] = {
    table_name: frozenset(column.name for column in table.columns)
    for table_name, table in Base.metadata.tables.items()
}


class WebConfigurationError(RuntimeError):
    """La función web no tiene una configuración de producción utilizable."""


class PlatformTelegramOperations:
    """Operaciones editoriales permitidas desde Telegram; nunca publica contenido."""

    def __init__(
        self,
        store: PlatformStore,
        *,
        generate_images: bool = True,
        research_registry: ResearchRegistry | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        if not isinstance(generate_images, bool):
            raise TypeError("generate_images debe ser booleano")
        self._generate_images = generate_images
        if research_registry is not None and not isinstance(research_registry, ResearchRegistry):
            raise TypeError("research_registry debe ser ResearchRegistry")
        self._research_registry = research_registry
        self._now = now or (lambda: datetime.now(UTC))

    def get_status(self, *, telegram_user_id: int, chat_id: int) -> str:
        actor = self.store.resolve_telegram_actor(
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
        )
        drafts = self.store.list_drafts(actor_id=actor.id)
        counts = Counter(draft.status for draft in drafts)
        labels = (
            (DraftStatus.DRAFT.value, "borradores"),
            (DraftStatus.IN_REVIEW.value, "en revisión"),
            (DraftStatus.APPROVED.value, "aprobados"),
            (DraftStatus.REJECTED.value, "rechazados"),
            (DraftStatus.PUBLISHED.value, "publicados"),
        )
        summary = "; ".join(f"{counts[key]} {label}" for key, label in labels)
        return f"Estado editorial: {summary}. Total: {len(drafts)}."

    def get_team(self, *, telegram_user_id: int, chat_id: int) -> str:
        actor = self.store.resolve_telegram_actor(
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
        )
        memberships = self.store.list_memberships(actor_id=actor.id)
        lines = ["Equipo Colmat:"]
        for membership in memberships:
            user = self.store.get_user(membership.user_id)
            state = "activo" if user.is_active else "inactivo"
            lines.append(f"• {user.display_name} — {membership.role} ({state})")
        return "\n".join(lines)

    def list_telegram_users(self, *, telegram_user_id: int, chat_id: int) -> str:
        actor = self.store.resolve_telegram_actor(
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
        )
        try:
            memberships = self.store.list_memberships(actor_id=actor.id)
            bindings = self.store.list_telegram_bindings(actor_id=actor.id)
        except (AuthorizationError, NotFoundError, ValueError):
            return "No se pudieron consultar los vínculos; la operación requiere al owner."
        bindings_by_user: dict[str, list[str]] = {}
        for binding in bindings:
            if binding.purpose != "control":
                continue
            state = "activo" if binding.is_active else "inactivo"
            private = "privado" if binding.chat_id == binding.telegram_user_id else "otro chat"
            bindings_by_user.setdefault(binding.user_id, []).append(
                f"{binding.telegram_user_id} ({state}, {private})"
            )
        lines = ["Usuarios y acceso Telegram (IDs numéricos; no @username):"]
        for index, membership in enumerate(memberships):
            user = self.store.get_user(membership.user_id)
            telegram_ids = ", ".join(bindings_by_user.get(user.id, ())) or "sin vínculo"
            state = "activo" if user.is_active else "inactivo"
            entry = (
                f"• {user.display_name} — {membership.role}, {state}\n"
                f"  user_id={user.id}; Telegram={telegram_ids}"
            )
            if len("\n".join((*lines, entry))) > 3_800:
                lines.append(
                    f"… {len(memberships) - index} usuario(s) omitidos; consulta la auditoría "
                    "o el panel para el listado completo."
                )
                break
            lines.append(entry)
        return "\n".join(lines)

    def invite_telegram_user(
        self,
        target_telegram_user_id: int,
        role: str,
        email: str,
        display_name: str,
        *,
        request_id: str,
        telegram_user_id: int,
        chat_id: int,
    ) -> CommandResult:
        del request_id  # El ID privado + los datos exactos hacen el alta idempotente.
        actor = self.store.resolve_telegram_actor(
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
        )
        try:
            user, membership, _binding = self.store.provision_telegram_team_member(
                actor_id=actor.id,
                telegram_user_id=target_telegram_user_id,
                email=email,
                display_name=display_name,
                role=Role(role),
            )
        except (AuthorizationError, ConflictError, NotFoundError, ValueError):
            return CommandResult(
                "No se completó el alta: debe ejecutarla el owner con un ID numérico no "
                "vinculado, un correo nuevo y un rol delegable.",
                accepted=False,
            )
        return CommandResult(
            f"Alta lista: {user.display_name} ({membership.role}), user_id={user.id}, "
            f"Telegram={target_telegram_user_id}. Debe abrir el bot en privado y usar /start."
        )

    def bind_telegram_user(
        self,
        target_telegram_user_id: int,
        user_id: str,
        *,
        request_id: str,
        telegram_user_id: int,
        chat_id: int,
    ) -> CommandResult:
        del request_id  # bind_telegram_chat es un upsert auditado por identidad exacta.
        actor = self.store.resolve_telegram_actor(
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
        )
        try:
            actor_membership = next(
                membership
                for membership in self.store.list_memberships(actor_id=actor.id)
                if membership.user_id == actor.id
            )
            if Role(actor_membership.role) is not Role.OWNER:
                raise AuthorizationError("Solo el owner puede vincular otras identidades")
            self.store.bind_telegram_chat(
                target_telegram_user_id,
                telegram_user_id=target_telegram_user_id,
                actor_id=actor.id,
                user_id=user_id,
                purpose="control",
            )
        except (AuthorizationError, ConflictError, NotFoundError, StopIteration, ValueError):
            return CommandResult(
                "No se creó el vínculo: comprueba el user_id, el ID numérico y que la "
                "identidad no pertenezca a otra cuenta.",
                accepted=False,
            )
        return CommandResult(
            f"Telegram {target_telegram_user_id} quedó vinculado a user_id={user_id} en chat "
            "privado. El rol de la membresía limita sus comandos."
        )

    def get_editorial_line(
        self,
        month: str | None,
        *,
        telegram_user_id: int,
        chat_id: int,
    ) -> str:
        actor = self.store.resolve_telegram_actor(
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
        )
        selected_month = month or self._editorial_month()
        line = self.store.get_editorial_line(selected_month, actor_id=actor.id)
        if line is None:
            return f"No hay línea editorial fijada para {selected_month}."
        return f"Línea editorial {line.month} (v{line.version}): {line.line_text}"

    def set_editorial_line(
        self,
        month: str,
        text: str,
        *,
        request_id: str,
        telegram_user_id: int,
        chat_id: int,
    ) -> CommandResult:
        del request_id  # El upsert idéntico no crea una versión adicional.
        actor = self.store.resolve_telegram_actor(
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
        )
        try:
            line = self.store.set_editorial_line(month, text, actor_id=actor.id)
        except (AuthorizationError, ConflictError, NotFoundError, ValueError):
            return CommandResult(
                "No se fijó la línea: solo el owner puede modificar un mes AAAA-MM válido.",
                accepted=False,
            )
        return CommandResult(
            f"Línea editorial {line.month} fijada en versión {line.version}. "
            "No se generó ni publicó contenido."
        )

    def research_topic(
        self,
        topic: str,
        *,
        request_id: str,
        telegram_user_id: int,
        chat_id: int,
    ) -> CommandResult:
        actor = self.store.resolve_telegram_actor(
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
        )
        month = self._editorial_month()
        try:
            line = self.store.get_editorial_line(month, actor_id=actor.id)
            if line is None:
                raise ConflictError("No hay línea editorial mensual")
            registry = self._research_registry or load_gustavo_bueno_registry()
            # La directriz mensual contextualiza la síntesis, pero no debe contaminar
            # la selección bibliográfica por título del tema solicitado.
            selected_references = registry.select(topic)
            pattern = " ".join(
                f"{index}) {step}" for index, step in enumerate(RESEARCH_PATTERN_STEPS, start=1)
            )
            brief_prefix = (
                f"{RESEARCH_ONLY_BRIEF_PREFIX} Tema: {topic}. Línea {line.month} "
                f"v{line.version}: {line.line_text} Patrón obligatorio: {pattern}"
            )
            if selected_references:
                suffix = (
                    " Catálogo por título, no cita: "
                    + " | ".join(
                        _research_reference_text(reference) for reference in selected_references
                    )
                    + ". URLs acotadas; verifica pertinencia y hechos."
                )
            else:
                suffix = (
                    " Sin obra pertinente: no inventes. URLs maestras autorizadas: "
                    + " ".join(registry.canonical_urls[:2])
                    + ". El worker recuperará solo esas fuentes; inferencias POR VERIFICAR."
                )
            brief = brief_prefix + suffix
            while len(brief) > 1_000 and len(selected_references) > 1:
                selected_references = selected_references[:-1]
                suffix = (
                    " Catálogo por título, no cita: "
                    + " | ".join(
                        _research_reference_text(reference) for reference in selected_references
                    )
                    + ". URLs acotadas; verifica pertinencia y hechos."
                )
                brief = brief_prefix + suffix
            if len(brief) > 1_000:
                raise ValueError("Tema, línea y referencias superan el brief permitido")
            generation_request = self.store.enqueue_generation_request(
                brief,
                actor_id=actor.id,
                telegram_user_id=telegram_user_id,
                chat_id=chat_id,
                idempotency_key=request_id,
                generate_image=False,
                category="dato_semana",
                institution="colmat",
            )
        except (AuthorizationError, ConflictError, NotFoundError, ValueError):
            return CommandResult(
                "No se encoló la investigación: se requiere owner/editor, línea del mes y "
                "capacidad disponible.",
                accepted=False,
            )
        return CommandResult(
            f"Síntesis exploratoria encolada como {generation_request.id} con la línea "
            f"{line.month} v{line.version} y {len(selected_references)} coincidencia(s) de "
            "catálogo por título. OpenClaw recuperará solo las URLs oficiales autorizadas y "
            "asignará la síntesis a MiniMax; no es navegación web abierta ni es publicable. "
            "Tras verificarla, copia sus hallazgos manualmente a /generar para crear una pieza "
            "nueva sujeta a revisión humana."
        )

    def get_research_patterns(self, *, telegram_user_id: int, chat_id: int) -> str:
        actor = self.store.resolve_telegram_actor(
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
        )
        # Revalida owner/editor en el store; no depende solo del permiso del procesador.
        self.store.get_editorial_line(self._editorial_month(), actor_id=actor.id)
        registry = self._research_registry or load_gustavo_bueno_registry()
        documented_count = sum(not reference.disputed for reference in registry.entries)
        disputed_count = sum(reference.disputed for reference in registry.entries)
        lines = [
            "Patrón de investigación Colmat:",
            f"Registro canónico cargado: {documented_count} títulos o series documentados + "
            f"{disputed_count} atribución "
            "disputada; se envían como máximo 3 coincidencias por título, nunca el catálogo "
            "completo, y siempre se verifica su pertinencia.",
        ]
        lines.extend(
            f"{index}. {step}" for index, step in enumerate(RESEARCH_PATTERN_STEPS, start=1)
        )
        lines.append(
            "MiniMax solo sintetiza las fuentes del encargo o del registro canónico. La "
            "verificación externa sigue siendo humana; sin fuente identificable, todo queda "
            "POR VERIFICAR y nunca pasa a publicación automática."
        )
        return "\n".join(lines)

    def _editorial_month(self) -> str:
        return self._now().astimezone(ZoneInfo("America/Bogota")).strftime("%Y-%m")

    def get_calendar(
        self,
        *,
        days: int,
        telegram_user_id: int,
        chat_id: int,
    ) -> str:
        actor = self.store.resolve_telegram_actor(
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
        )
        settings = self.store.get_automation_settings(actor_id=actor.id)
        zone = ZoneInfo(settings.timezone)
        first_day = self._now().astimezone(zone).date()
        lines = [
            "Agenda Colmat "
            f"({settings.timezone}; {'activa' if settings.enabled else 'pausada'}; "
            f"modo {settings.mode}):"
        ]
        slots = tuple(settings.slots or ())
        if not slots:
            lines.append("• No hay slots diarios configurados.")
            return "\n".join(lines)
        for offset in range(days):
            local_day = first_day + timedelta(days=offset)
            for raw_slot in slots:
                if not isinstance(raw_slot, Mapping):
                    continue
                weekdays = raw_slot.get("weekdays")
                if weekdays is not None and (
                    not isinstance(weekdays, list)
                    or CANONICAL_AUTOMATION_WEEKDAYS[local_day.weekday()] not in weekdays
                ):
                    continue
                slot_id = str(raw_slot.get("id") or "slot")
                at = str(raw_slot.get("at") or "--:--")
                mode = str(raw_slot.get("mode") or settings.mode)
                lines.append(f"• {local_day.isoformat()} {at} — {slot_id} [{mode}]")
        return "\n".join(lines)

    def get_mode(self, *, telegram_user_id: int, chat_id: int) -> str:
        actor = self.store.resolve_telegram_actor(
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
        )
        settings = self.store.get_automation_settings(actor_id=actor.id)
        state = "activa" if settings.enabled else "pausada"
        return f"Modo actual: {settings.mode}; automatización {state}; versión {settings.version}."

    def set_mode(
        self,
        mode: BotAutomationMode,
        *,
        request_id: str,
        telegram_user_id: int,
        chat_id: int,
    ) -> CommandResult:
        del request_id  # La CAS persistida y el update_id deduplicado protegen este cambio.
        actor = self.store.resolve_telegram_actor(
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
        )
        try:
            current = self.store.get_automation_settings(actor_id=actor.id)
            requested_mode = AutomationMode(mode.value)
            slots = _automation_slots_for_mode(current.slots, requested_mode)
            updated = self.store.update_automation_settings(
                actor_id=actor.id,
                expected_version=current.version,
                mode=requested_mode,
                slots=slots,
            )
        except (AuthorizationError, ConflictError, NotFoundError, ValueError):
            return CommandResult(
                "No se cambió el modo: faltan permisos, la versión cambió o el kill switch "
                "direct sigue cerrado.",
                accepted=False,
            )
        return CommandResult(
            f"Modo configurado: {updated.mode}; versión {updated.version}. "
            "Este cambio no publica contenido por sí solo."
        )

    def generate_draft(
        self,
        brief: str,
        *,
        request_id: str,
        telegram_user_id: int,
        chat_id: int,
    ) -> CommandResult:
        actor = self.store.resolve_telegram_actor(
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
        )
        try:
            month = self._editorial_month()
            line = self.store.get_editorial_line(month, actor_id=actor.id)
            if line is None:
                raise ConflictError("No hay línea editorial mensual")
            guided_brief = (
                f"Línea editorial {line.month} v{line.version}: {line.line_text} Encargo: {brief}"
            )
            if len(guided_brief) > 1_000:
                raise ValueError("La línea y el encargo superan el brief permitido")
            generation_request = self.store.enqueue_generation_request(
                guided_brief,
                actor_id=actor.id,
                telegram_user_id=telegram_user_id,
                chat_id=chat_id,
                idempotency_key=request_id,
                generate_image=self._generate_images,
            )
        except (
            AuthorizationError,
            ConflictError,
            NotFoundError,
            ValueError,
        ):
            return CommandResult(
                "No se encoló la generación. Se requiere owner/editor, una línea editorial "
                "del mes y un encargo que junto con ella no supere 1000 caracteres; no se "
                "llamó a MiniMax ni se realizó ninguna publicación.",
                accepted=False,
            )
        return CommandResult(
            f"Generación encolada como {generation_request.id}. OpenClaw la asignará a "
            f"MiniMax con la línea {line.month} v{line.version} fuera del webhook; el resultado "
            "siempre exigirá revisión humana"
            f"{' e incluirá imagen' if generation_request.generate_image else ''}."
        )

    def request_publication(
        self,
        post_id: str,
        *,
        request_id: str,
        telegram_user_id: int,
        chat_id: int,
    ) -> CommandResult:
        actor = self.store.resolve_telegram_actor(
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
        )
        try:
            revision = self.store.get_current_revision(post_id, actor_id=actor.id)
            publication_request = self.store.enqueue_publication_request(
                post_id,
                actor_id=actor.id,
                expected_snapshot_hash=revision.snapshot_hash,
                idempotency_key=request_id,
            )
        except (
            AuthorizationError,
            ConflictError,
            NotFoundError,
            StaleSnapshotError,
            ValueError,
        ):
            return CommandResult(
                "No se encoló: el borrador debe estar aprobado, conservar el snapshot y ser "
                "solicitado por un publisher autorizado.",
                accepted=False,
            )
        return CommandResult(
            f"Solicitud encolada como {publication_request.id}; no se publicó durante el webhook."
        )

    def approve_post(
        self,
        *,
        post_id: str,
        snapshot_hash: str,
        telegram_user_id: int,
        chat_id: int,
    ) -> DecisionResult:
        actor = self.store.resolve_telegram_actor(
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
        )
        try:
            self.store.approve_draft(
                post_id,
                actor_id=actor.id,
                expected_snapshot_hash=snapshot_hash,
            )
        except (AuthorizationError, ConflictError, NotFoundError, StaleSnapshotError):
            return DecisionResult(
                "No se registró la aprobación: el contenido o sus permisos cambiaron.",
                accepted=False,
            )
        return DecisionResult(
            f"El borrador {post_id} fue aprobado; todavía no se ha publicado.",
        )

    def reject_post(
        self,
        *,
        post_id: str,
        snapshot_hash: str,
        telegram_user_id: int,
        chat_id: int,
    ) -> DecisionResult:
        actor = self.store.resolve_telegram_actor(
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
        )
        try:
            self.store.reject_draft(
                post_id,
                actor_id=actor.id,
                expected_snapshot_hash=snapshot_hash,
                reason="Rechazado mediante el control editorial de Telegram.",
            )
        except (AuthorizationError, ConflictError, NotFoundError, StaleSnapshotError):
            return DecisionResult(
                "No se registró el rechazo: el contenido o sus permisos cambiaron.",
                accepted=False,
            )
        return DecisionResult(f"El borrador {post_id} fue rechazado.")


def _automation_slots_for_mode(
    slots: object,
    mode: AutomationMode,
) -> list[dict[str, object]]:
    if not isinstance(slots, list):
        raise ValueError("Los slots persistidos no tienen formato de lista")
    aligned: list[dict[str, object]] = []
    for index, raw_slot in enumerate(slots):
        if not isinstance(raw_slot, Mapping):
            raise ValueError(f"El slot persistido {index} no tiene formato de objeto")
        aligned.append({**raw_slot, "mode": mode.value})
    return aligned


def _automation_slots_from_form(
    slots: object,
    mode: AutomationMode,
    form: Mapping[str, str],
) -> list[dict[str, object]]:
    """Actualiza solo las horas visibles; identidad, política y evidencia quedan intactas."""

    aligned = _automation_slots_for_mode(slots, mode)
    for index, slot in enumerate(aligned[:20]):
        field_name = f"slot_at_{index}"
        if field_name in form:
            slot["at"] = form[field_name]
    return aligned


def _generation_default_image(environ: Mapping[str, str]) -> bool:
    raw_value = environ.get("COLMAT_GENERATION_DEFAULT_IMAGE", "true")
    value = raw_value.strip().casefold() if isinstance(raw_value, str) else ""
    if value not in {"true", "false"}:
        raise WebConfigurationError(
            "COLMAT_GENERATION_DEFAULT_IMAGE debe ser true o false exactamente"
        )
    return value == "true"


def _web_telegram_timeout(environ: Mapping[str, str]) -> float:
    raw_value = environ.get("COLMAT_WEB_TELEGRAM_TIMEOUT_SECONDS", "3")
    try:
        timeout = float(raw_value)
    except (TypeError, ValueError) as exc:
        raise WebConfigurationError(
            "COLMAT_WEB_TELEGRAM_TIMEOUT_SECONDS debe ser un número entre 1 y 5"
        ) from exc
    if not 1 <= timeout <= 5:
        raise WebConfigurationError("COLMAT_WEB_TELEGRAM_TIMEOUT_SECONDS debe estar entre 1 y 5")
    return timeout


@dataclass(frozen=True, slots=True)
class WebRuntime:
    store: Any
    processor: Any
    telegram_client: Any


class RuntimeProvider:
    """Construye una sola runtime por instancia caliente, sin abrir recursos al importar."""

    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        store: Any | None = None,
        processor: Any | None = None,
        telegram_client: Any | None = None,
        web_auth: Any | None = None,
    ) -> None:
        self.environ = os.environ if environ is None else environ
        self._injected = WebRuntime(store, processor, telegram_client)
        self._injected_web_auth = web_auth
        self._runtime: WebRuntime | None = None
        self._web_auth: Any | None = None
        self._lock = threading.Lock()
        self._web_auth_lock = threading.Lock()
        self._readiness_lock = threading.Lock()
        self._readiness_cache: tuple[float, bool, dict[str, str]] | None = None

    def get(self) -> WebRuntime:
        if self._runtime is not None:
            return self._runtime
        with self._lock:
            if self._runtime is None:
                self._runtime = self._build()
        return self._runtime

    def get_web_auth(self) -> Any:
        if self._injected_web_auth is not None:
            return self._injected_web_auth
        if self._web_auth is not None:
            return self._web_auth
        with self._web_auth_lock:
            if self._web_auth is None:
                pepper = self.environ.get("WEB_AUTH_PEPPER")
                self._web_auth = WebAuthService(self.get().store, pepper=pepper)
        return self._web_auth

    def readiness(self) -> tuple[bool, dict[str, str]]:
        with self._readiness_lock:
            cached = self._readiness_cache
            now = time.monotonic()
            if cached is not None and now - cached[0] < READINESS_CACHE_SECONDS:
                return cached[1], dict(cached[2])
            ready, checks = self._readiness_uncached()
            self._readiness_cache = (time.monotonic(), ready, dict(checks))
            return ready, checks

    def _readiness_uncached(self) -> tuple[bool, dict[str, str]]:
        checks: dict[str, str] = {}
        if self.environ.get("VERCEL"):
            checks["worker_secrets"] = (
                "error"
                if any(self.environ.get(name, "") for name in PRIVILEGED_WORKER_ENVIRONMENT)
                else "ok"
            )
            if self._injected_web_auth is not None:
                checks["web_auth_pepper"] = "ok"
            else:
                pepper = self.environ.get("WEB_AUTH_PEPPER", "")
                checks["web_auth_pepper"] = "ok" if len(pepper) >= 32 else "missing"

        if self._injected.processor is None:
            try:
                TelegramWebhookSecret.from_environment(environ=self.environ)
            except ValueError:
                checks["telegram_webhook_secret"] = "missing"
            else:
                checks["telegram_webhook_secret"] = "ok"
        else:
            checks["telegram_webhook_secret"] = "ok"

        if self._injected.telegram_client is None:
            try:
                TelegramCredentials.from_environment(environ=self.environ)
            except TelegramConfigurationError:
                checks["telegram_bot_token"] = "missing"
            else:
                checks["telegram_bot_token"] = "ok"
        else:
            checks["telegram_bot_token"] = "ok"

        database_url = self.environ.get("DATABASE_URL", "").strip()
        if self._injected.store is None and self.environ.get("VERCEL") and not database_url:
            checks["database"] = "missing"
        else:
            try:
                runtime = self.get()
                engine = getattr(runtime.store, "engine", None)
                if engine is not None:
                    with engine.connect() as connection:
                        connection.execute(text("SELECT 1"))
                        inspector = inspect(connection)
                        multi_columns = inspector.get_multi_columns(
                            filter_names=list(REQUIRED_DATABASE_COLUMNS)
                        )
                        columns_by_table = {
                            table_name: {column["name"] for column in columns}
                            for (_schema, table_name), columns in multi_columns.items()
                        }
                        for table_name, expected_columns in REQUIRED_DATABASE_COLUMNS.items():
                            actual_columns = columns_by_table.get(table_name, set())
                            if not expected_columns.issubset(actual_columns):
                                raise RuntimeError("El esquema operativo está incompleto")
                checks["database"] = "ok"
            except Exception:
                checks["database"] = "error"

        return all(result == "ok" for result in checks.values()), checks

    def _build(self) -> WebRuntime:
        # Se carga durante el arranque, no al recibir /investigar: un artefacto
        # instalado sin el registro canónico queda no-ready y falla cerrado.
        research_registry = load_gustavo_bueno_registry()
        store = self._injected.store
        if store is None:
            database_url = self.environ.get("DATABASE_URL", "").strip() or None
            if self.environ.get("VERCEL") and database_url is None:
                raise WebConfigurationError("DATABASE_URL es obligatorio en Vercel")
            store = PlatformStore(
                database_url=database_url,
                create_schema=not bool(self.environ.get("VERCEL")),
                serverless=bool(self.environ.get("VERCEL")),
            )

        processor = self._injected.processor
        if processor is None:
            webhook_secret = TelegramWebhookSecret.from_environment(environ=self.environ)
            processor = TelegramWebhookProcessor(
                webhook_secret=webhook_secret,
                update_store=store,
                authorizer=store,
                operations=PlatformTelegramOperations(
                    store,
                    generate_images=_generation_default_image(self.environ),
                    research_registry=research_registry,
                ),
                callback_nonces=store,
            )

        telegram_client = self._injected.telegram_client
        if telegram_client is None:
            credentials = TelegramCredentials.from_environment(environ=self.environ)
            telegram_client = TelegramApiClient(
                credentials,
                timeout_seconds=_web_telegram_timeout(self.environ),
            )
        return WebRuntime(store=store, processor=processor, telegram_client=telegram_client)


def _research_reference_text(reference: Any) -> str:
    if reference.disputed:
        return (
            f"{reference.title} ({reference.year}) {reference.url}; atribución editorial "
            "disputada; Bueno negó participación: "
            f"{reference.status_url}"
        )
    return f"{reference.title} ({reference.year}) {reference.url}"


def create_app(
    *,
    environ: Mapping[str, str] | None = None,
    store: Any | None = None,
    processor: Any | None = None,
    telegram_client: Any | None = None,
    web_auth: Any | None = None,
    max_update_bytes: int = MAX_TELEGRAM_UPDATE_BYTES,
) -> FastAPI:
    if isinstance(max_update_bytes, bool) or not isinstance(max_update_bytes, int):
        raise ValueError("max_update_bytes debe ser un entero positivo")
    if max_update_bytes <= 0:
        raise ValueError("max_update_bytes debe ser un entero positivo")

    provider = RuntimeProvider(
        environ=environ,
        store=store,
        processor=processor,
        telegram_client=telegram_client,
        web_auth=web_auth,
    )
    app = FastAPI(
        title="Colmat X Automation",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.runtime_provider = provider
    templates = Jinja2Templates(directory=str(TEMPLATES_DIRECTORY))
    app.state.templates = templates
    app.mount("/static", StaticFiles(directory=str(STATIC_DIRECTORY)), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
            "form-action 'self'; img-src 'self' data:; object-src 'none'; "
            "script-src 'self'; style-src 'self'"
        )
        if _request_scheme(request) == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    @app.get("/", response_class=HTMLResponse)
    def landing(request: Request) -> Any:
        session_token = request.cookies.get(SESSION_COOKIE)
        if session_token:
            try:
                provider.get_web_auth().authenticate(session_token)
            except (InvalidSessionError, WebAuthConfigurationError):
                response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
                _clear_auth_cookies(response)
                return response
            return RedirectResponse("/app", status_code=status.HTTP_303_SEE_OTHER)
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/login", response_class=HTMLResponse)
    def login(request: Request) -> Any:
        session_token = request.cookies.get(SESSION_COOKIE)
        if session_token:
            try:
                provider.get_web_auth().authenticate(session_token)
            except (InvalidSessionError, WebAuthConfigurationError):
                pass
            else:
                return RedirectResponse("/app", status_code=status.HTTP_303_SEE_OTHER)
        response = _render_login(templates, request)
        _clear_session_cookies(response)
        return response

    @app.post("/auth/code", response_class=HTMLResponse)
    async def request_access_code(request: Request) -> Any:
        request_started = time.monotonic()
        try:
            form = await _read_web_form(request)
            _require_same_origin(request)
            csrf_token = _require_login_csrf(request, form)
            auth = await run_in_threadpool(provider.get_web_auth)
            issued = await run_in_threadpool(
                auth.request_challenge,
                form.get("identifier", ""),
                _client_ip(request, provider.environ),
            )
        except RateLimitError:
            return _render_login(
                templates,
                request,
                error="Se alcanzó el límite temporal. Inténtalo de nuevo en unos minutos.",
                response_status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        except HTTPException as exc:
            return _render_error(
                templates,
                request,
                status_code=exc.status_code,
                title="Solicitud no válida",
                message="Vuelve al inicio e intenta de nuevo.",
                back_url="/login",
            )
        except (WebAuthConfigurationError, ValueError):
            return _render_error(
                templates,
                request,
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                title="Acceso temporalmente no disponible",
                message="El servicio de acceso no está listo. Inténtalo de nuevo más tarde.",
                back_url="/login",
            )

        if issued.deliverable:
            try:
                runtime = await run_in_threadpool(provider.get)
                await run_in_threadpool(
                    runtime.telegram_client.send_message,
                    int(issued.chat_id),
                    "Código de acceso a Colmat: "
                    f"{issued.code}\nCaduca en 5 minutos y solo funciona una vez.",
                )
            except Exception:
                await run_in_threadpool(auth.cancel_challenge, issued.challenge_id)

        await _uniform_login_delay(request_started)

        return _render_verify(
            templates,
            request,
            csrf_token=csrf_token,
            challenge_id=auth.seal_challenge_id(issued.challenge_id),
            message="Si la cuenta está habilitada, el código llegó a su Telegram vinculado.",
        )

    @app.post("/auth/verify", response_class=HTMLResponse)
    async def verify_access_code(request: Request) -> Any:
        try:
            form = await _read_web_form(request)
            _require_same_origin(request)
            csrf_token = _require_login_csrf(request, form)
            auth = await run_in_threadpool(provider.get_web_auth)
            challenge_id = auth.open_challenge_state(form.get("challenge_id", ""))
            issued = await run_in_threadpool(
                auth.verify_challenge,
                challenge_id,
                form.get("code", ""),
                _client_ip(request, provider.environ),
            )
        except InvalidChallengeError:
            return _render_verify(
                templates,
                request,
                csrf_token=request.cookies.get(LOGIN_CSRF_COOKIE, ""),
                challenge_id=form.get("challenge_id", "") if "form" in locals() else "",
                error="El código no es válido o ya caducó.",
                response_status=status.HTTP_400_BAD_REQUEST,
            )
        except HTTPException as exc:
            return _render_error(
                templates,
                request,
                status_code=exc.status_code,
                title="Solicitud no válida",
                message="Vuelve al inicio e intenta de nuevo.",
                back_url="/login",
            )
        except (WebAuthConfigurationError, ValueError):
            return _render_error(
                templates,
                request,
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                title="Acceso temporalmente no disponible",
                message="El servicio de acceso no está listo. Inténtalo de nuevo más tarde.",
                back_url="/login",
            )

        response = RedirectResponse("/app", status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(
            SESSION_COOKIE,
            issued.token,
            max_age=SESSION_MAX_AGE_SECONDS,
            secure=True,
            httponly=True,
            samesite="lax",
            path="/",
        )
        response.set_cookie(
            CSRF_COOKIE,
            issued.csrf_token,
            max_age=SESSION_MAX_AGE_SECONDS,
            secure=True,
            httponly=True,
            samesite="lax",
            path="/",
        )
        response.delete_cookie(LOGIN_CSRF_COOKIE, path="/", secure=True, httponly=True)
        return response

    @app.post("/auth/logout")
    async def logout(request: Request) -> Any:
        try:
            form, _principal, session_token = await _authenticated_web_post(provider, request)
            del form
            auth = await run_in_threadpool(provider.get_web_auth)
            await run_in_threadpool(auth.logout, session_token)
        except InvalidSessionError:
            response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
            _clear_auth_cookies(response)
            return response
        except (CsrfError, HTTPException):
            return _render_error(
                templates,
                request,
                status_code=status.HTTP_403_FORBIDDEN,
                title="Solicitud rechazada",
                message="La sesión o la solicitud ya no es válida.",
                back_url="/app",
            )
        response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        _clear_auth_cookies(response)
        return response

    @app.get("/app", response_class=HTMLResponse)
    def dashboard(request: Request) -> Any:
        try:
            principal, csrf_token = _authenticated_web_get(provider, request)
            return _render_dashboard(
                templates,
                request,
                provider,
                principal,
                csrf_token,
                notice=request.query_params.get("notice"),
                error=request.query_params.get("error"),
            )
        except InvalidSessionError:
            response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
            _clear_auth_cookies(response)
            return response
        except (WebAuthConfigurationError, AuthorizationError, NotFoundError, ValueError):
            return _render_error(
                templates,
                request,
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                title="Panel temporalmente no disponible",
                message="No fue posible cargar el estado del espacio. Inténtalo de nuevo.",
                back_url="/login",
            )

    @app.post("/app/generate")
    async def generate_from_dashboard(request: Request) -> Any:
        try:
            form, principal, _session_token = await _authenticated_web_post(provider, request)
            request_nonce = form.get("request_id", "")
            if not _valid_request_nonce(request_nonce):
                raise ValueError("request_id inválido")
            runtime = await run_in_threadpool(provider.get)
            await run_in_threadpool(
                runtime.store.enqueue_generation_request,
                form.get("brief", ""),
                actor_id=principal.user_id,
                telegram_user_id=principal.telegram_user_id,
                chat_id=principal.chat_id,
                idempotency_key=f"web:{principal.user_id}:{request_nonce}",
                generate_image=form.get("generate_image") == "true",
            )
        except InvalidSessionError:
            return _redirect_to_login()
        except (CsrfError, HTTPException):
            return _app_redirect(error="request_rejected")
        except (AuthorizationError, ConflictError, NotFoundError, ValueError):
            return _app_redirect(error="generation_failed")
        return _app_redirect(notice="generation_queued")

    @app.post("/app/automation")
    async def update_automation_from_dashboard(request: Request) -> Any:
        try:
            form, principal, _session_token = await _authenticated_web_post(provider, request)
            await run_in_threadpool(
                _update_web_automation,
                provider,
                principal,
                form,
            )
        except InvalidSessionError:
            return _redirect_to_login()
        except (CsrfError, HTTPException):
            return _app_redirect(error="request_rejected")
        except (AuthorizationError, ConflictError, NotFoundError, ValueError):
            return _app_redirect(error="automation_failed")
        return _app_redirect(notice="automation_updated")

    @app.post("/app/team")
    async def create_team_member(request: Request) -> Any:
        try:
            form, principal, _session_token = await _authenticated_web_post(provider, request)
            role = Role(form.get("role", ""))
            if not can_assign_role(principal.role, role):
                raise AuthorizationError("Rol no delegable")
            runtime = await run_in_threadpool(provider.get)
            await run_in_threadpool(
                runtime.store.create_team_member,
                actor_id=principal.user_id,
                email=form.get("email", ""),
                username=form.get("username", ""),
                display_name=form.get("display_name", ""),
                role=role,
            )
        except InvalidSessionError:
            return _redirect_to_login()
        except (CsrfError, HTTPException):
            return _app_redirect(error="request_rejected")
        except (AuthorizationError, ConflictError, NotFoundError, ValueError):
            return _app_redirect(error="team_failed")
        return _app_redirect(notice="team_created")

    @app.post("/app/drafts/{draft_id}/approve")
    async def approve_from_dashboard(draft_id: str, request: Request) -> Any:
        try:
            form, principal, _session_token = await _authenticated_web_post(provider, request)
            runtime = await run_in_threadpool(provider.get)
            store = runtime.store
            snapshot_hash = form.get("snapshot_hash", "")
            await run_in_threadpool(
                _approve_web_draft,
                store,
                draft_id=draft_id,
                actor_id=principal.user_id,
                expected_snapshot_hash=snapshot_hash,
            )
        except InvalidSessionError:
            return _redirect_to_login()
        except (CsrfError, HTTPException):
            return _app_redirect(error="request_rejected")
        except (AuthorizationError, ConflictError, NotFoundError, StaleSnapshotError, ValueError):
            return _app_redirect(error="review_failed")
        return _app_redirect(notice="draft_approved")

    @app.post("/app/drafts/{draft_id}/reject")
    async def reject_from_dashboard(draft_id: str, request: Request) -> Any:
        try:
            form, principal, _session_token = await _authenticated_web_post(provider, request)
            runtime = await run_in_threadpool(provider.get)
            store = runtime.store
            snapshot_hash = form.get("snapshot_hash", "")
            await run_in_threadpool(
                _reject_web_draft,
                store,
                draft_id=draft_id,
                actor_id=principal.user_id,
                expected_snapshot_hash=snapshot_hash,
            )
        except InvalidSessionError:
            return _redirect_to_login()
        except (CsrfError, HTTPException):
            return _app_redirect(error="request_rejected")
        except (AuthorizationError, ConflictError, NotFoundError, StaleSnapshotError, ValueError):
            return _app_redirect(error="review_failed")
        return _app_redirect(notice="draft_rejected")

    @app.post("/app/drafts/{draft_id}/publish")
    async def publish_from_dashboard(draft_id: str, request: Request) -> Any:
        try:
            form, principal, _session_token = await _authenticated_web_post(provider, request)
            snapshot_hash = form.get("snapshot_hash", "")
            runtime = await run_in_threadpool(provider.get)
            await run_in_threadpool(
                _publish_web_draft,
                runtime.store,
                draft_id=draft_id,
                actor_id=principal.user_id,
                expected_snapshot_hash=snapshot_hash,
            )
        except InvalidSessionError:
            return _redirect_to_login()
        except (CsrfError, HTTPException):
            return _app_redirect(error="request_rejected")
        except (AuthorizationError, ConflictError, NotFoundError, StaleSnapshotError, ValueError):
            return _app_redirect(error="publication_failed")
        return _app_redirect(notice="publication_queued")

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "colmat-x-automation"}

    @app.get("/api/ready")
    def ready() -> JSONResponse:
        is_ready, checks = provider.readiness()
        response_status = status.HTTP_200_OK if is_ready else status.HTTP_503_SERVICE_UNAVAILABLE
        return JSONResponse(
            status_code=response_status,
            content={"status": "ready" if is_ready else "not_ready", "checks": checks},
        )

    @app.post("/api/telegram/webhook")
    async def telegram_webhook(request: Request) -> dict[str, object]:
        _require_configured_webhook_header(provider, request)
        payload = await _read_json_object(request, maximum_bytes=max_update_bytes)
        try:
            runtime = await run_in_threadpool(provider.get)
            result = await run_in_threadpool(
                runtime.processor.process_update,
                payload,
                secret_token=request.headers.get(TELEGRAM_SECRET_HEADER),
            )
        except WebhookAuthenticationError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden") from exc
        except MalformedTelegramUpdate as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid update",
            ) from exc
        except (TelegramConfigurationError, ValueError) as exc:
            if isinstance(exc, ValueError) and not isinstance(exc, TelegramConfigurationError):
                await run_in_threadpool(_finish_failed_update, provider, payload, exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="service unavailable",
            ) from exc
        except ClaimedTelegramUpdateError as exc:
            await run_in_threadpool(
                _finish_failed_update,
                provider,
                payload,
                exc.__cause__ or exc,
                claim_token=exc.claim_token,
                claim_fence=exc.claim_fence,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="service unavailable",
            ) from exc
        except Exception as exc:
            await run_in_threadpool(_finish_failed_update, provider, payload, exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="service unavailable",
            ) from exc

        if result.duplicate:
            if result.retryable:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="service unavailable",
                )
            return {"ok": True, "update_id": result.update_id, "duplicate": True}

        try:
            await run_in_threadpool(_deliver_and_finish_webhook, runtime, result)
        except Exception as exc:
            await run_in_threadpool(
                _finish_failed_update,
                provider,
                payload,
                exc,
                claim_token=result.claim_token,
                claim_fence=result.claim_fence,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="service unavailable",
            ) from exc
        return {"ok": True, "update_id": result.update_id, "duplicate": False}

    return app


_NOTICE_MESSAGES = {
    "automation_updated": "La agenda y el modo editorial quedaron actualizados.",
    "draft_approved": "La revisión exacta quedó aprobada; aún no se publicó.",
    "draft_rejected": "La revisión exacta quedó rechazada.",
    "generation_queued": "OpenClaw recibió la pieza y MiniMax la preparará para revisión.",
    "publication_queued": "La publicación aprobada quedó en la cola; el worker decide cuándo sale.",
    "team_created": (
        "La cuenta quedó creada. Para ingresar, un administrador debe vincular su Telegram "
        "de control."
    ),
}
_ERROR_MESSAGES = {
    "automation_failed": "No se cambió la agenda. Recarga el panel y revisa tus permisos.",
    "generation_failed": "No se pudo encargar la pieza. Revisa la instrucción y vuelve a intentar.",
    "publication_failed": "No se encoló: el borrador debe seguir aprobado y sin cambios.",
    "request_rejected": "La sesión o el formulario ya no son válidos. Recarga el panel.",
    "review_failed": "No se registró la decisión: el borrador o los permisos cambiaron.",
    "team_failed": "No se creó la cuenta. Revisa los datos, duplicados y jerarquía del rol.",
}
_ROLE_LABELS = {
    Role.OWNER: "Propietario",
    Role.ADMIN: "Administrador",
    Role.EDITOR: "Editor",
    Role.REVIEWER: "Revisor",
    Role.PUBLISHER: "Publicador",
    Role.SCHEDULER: "Programador",
    Role.AUDITOR: "Auditor",
}


def _render_login(
    templates: Jinja2Templates,
    request: Request,
    *,
    error: str | None = None,
    message: str | None = None,
    response_status: int = status.HTTP_200_OK,
) -> HTMLResponse:
    csrf_token = secrets.token_urlsafe(32)
    response = templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"csrf_token": csrf_token, "error": error, "message": message},
        status_code=response_status,
    )
    response.set_cookie(
        LOGIN_CSRF_COOKIE,
        csrf_token,
        max_age=5 * 60,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


def _render_verify(
    templates: Jinja2Templates,
    request: Request,
    *,
    csrf_token: str,
    challenge_id: str,
    error: str | None = None,
    message: str | None = None,
    response_status: int = status.HTTP_200_OK,
) -> HTMLResponse:
    response = templates.TemplateResponse(
        request=request,
        name="verify.html",
        context={
            "csrf_token": csrf_token,
            "challenge_id": challenge_id,
            "error": error,
            "message": message,
        },
        status_code=response_status,
    )
    if csrf_token:
        response.set_cookie(
            LOGIN_CSRF_COOKIE,
            csrf_token,
            max_age=5 * 60,
            secure=True,
            httponly=True,
            samesite="lax",
            path="/",
        )
    return response


def _render_error(
    templates: Jinja2Templates,
    request: Request,
    *,
    status_code: int,
    title: str,
    message: str,
    back_url: str,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="error.html",
        context={
            "status": status_code,
            "title": title,
            "message": message,
            "back_url": back_url,
        },
        status_code=status_code,
    )


def _update_web_automation(
    provider: RuntimeProvider,
    principal: Any,
    form: Mapping[str, str],
) -> None:
    runtime = provider.get()
    current = runtime.store.get_automation_settings(actor_id=principal.user_id)
    can_manage_mode = has_permission(
        principal.role,
        Permission.MANAGE_AUTOMATION_MODE,
    )
    mode = AutomationMode(form.get("mode", "")) if can_manage_mode else AutomationMode(current.mode)
    if (
        can_manage_mode
        and mode is AutomationMode.DIRECT
        and not _direct_mode_available(provider.environ)
    ):
        raise ValueError("direct no está habilitado")
    expected_version = int(form.get("expected_version", ""))
    if current.version != expected_version:
        raise ConflictError("La configuración cambió")
    changes: dict[str, object] = {
        "actor_id": principal.user_id,
        "expected_version": expected_version,
        "enabled": form.get("enabled") == "true",
        "slots": _automation_slots_from_form(current.slots, mode, form),
        "max_posts_per_day": int(form.get("max_posts_per_day", "")),
    }
    if can_manage_mode:
        changes["mode"] = mode
    runtime.store.update_automation_settings(**changes)


def _approve_web_draft(
    store: PlatformStore,
    *,
    draft_id: str,
    actor_id: str,
    expected_snapshot_hash: str,
) -> None:
    _require_complete_web_review_material(
        store,
        draft_id=draft_id,
        actor_id=actor_id,
        expected_snapshot_hash=expected_snapshot_hash,
    )
    store.approve_draft(
        draft_id,
        actor_id=actor_id,
        expected_snapshot_hash=expected_snapshot_hash,
        reason="Aprobado desde el panel web de Colmat.",
    )


def _reject_web_draft(
    store: PlatformStore,
    *,
    draft_id: str,
    actor_id: str,
    expected_snapshot_hash: str,
) -> None:
    _require_complete_web_review_material(
        store,
        draft_id=draft_id,
        actor_id=actor_id,
        expected_snapshot_hash=expected_snapshot_hash,
    )
    store.reject_draft(
        draft_id,
        actor_id=actor_id,
        expected_snapshot_hash=expected_snapshot_hash,
        reason="Rechazado desde el panel web de Colmat.",
    )


def _publish_web_draft(
    store: PlatformStore,
    *,
    draft_id: str,
    actor_id: str,
    expected_snapshot_hash: str,
) -> None:
    _require_complete_web_review_material(
        store,
        draft_id=draft_id,
        actor_id=actor_id,
        expected_snapshot_hash=expected_snapshot_hash,
    )
    store.enqueue_publication_request(
        draft_id,
        actor_id=actor_id,
        expected_snapshot_hash=expected_snapshot_hash,
        idempotency_key=f"web-publish:{draft_id}:{expected_snapshot_hash[:16]}",
    )


def _require_complete_web_review_material(
    store: PlatformStore,
    *,
    draft_id: str,
    actor_id: str,
    expected_snapshot_hash: str,
) -> None:
    """Impide decidir desde web material que el panel no puede mostrar completo."""

    revision = store.get_current_revision(draft_id, actor_id=actor_id)
    if revision.snapshot_hash != expected_snapshot_hash:
        raise StaleSnapshotError("El contenido ya no coincide con el snapshot mostrado")
    if revision.image_sha256 is not None:
        raise ConflictError("Las piezas con imagen requieren revisión con vista previa en Telegram")
    evidence_text = json.dumps(
        revision.evidence,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ": "),
    )
    if len(evidence_text) > 2_000:
        raise ConflictError("La evidencia requiere revisión completa en Telegram")


def _render_dashboard(
    templates: Jinja2Templates,
    request: Request,
    provider: RuntimeProvider,
    principal: Any,
    csrf_token: str,
    *,
    notice: str | None,
    error: str | None,
) -> HTMLResponse:
    runtime = provider.get()
    store = runtime.store
    snapshot = store.get_dashboard_snapshot(
        actor_id=principal.user_id,
        draft_limit=25,
    )
    settings = snapshot.settings
    zone = ZoneInfo(settings.timezone)
    draft_rows = snapshot.drafts
    counts = Counter(draft.status for draft, _revision in draft_rows)
    can_review = has_permission(principal.role, Permission.REVIEW_DRAFTS)
    can_publish = has_permission(principal.role, Permission.PUBLISH_DRAFTS)
    drafts: list[dict[str, object]] = []
    for draft, revision in draft_rows:
        has_image = revision.image_sha256 is not None
        research_only = (
            isinstance(revision.evidence, Mapping)
            and revision.evidence.get("research_only") is True
        )
        evidence_text = json.dumps(
            revision.evidence,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ": "),
        )
        evidence_truncated = len(evidence_text) > 2_000
        review_material_complete = not has_image and not evidence_truncated
        drafts.append(
            {
                "id": draft.id,
                "status": draft.status,
                "text": revision.text,
                "category": revision.category,
                "publish_at": revision.publish_at.astimezone(zone).strftime("%Y-%m-%d %H:%M"),
                "snapshot_hash": revision.snapshot_hash,
                "evidence": evidence_text[:2_000],
                "evidence_truncated": evidence_truncated,
                "image_fingerprint": (
                    revision.image_sha256[:16] if revision.image_sha256 is not None else None
                ),
                "requires_telegram_review": has_image,
                "research_only": research_only,
                "can_approve": (
                    can_review
                    and draft.status == DraftStatus.IN_REVIEW.value
                    and revision.created_by != principal.user_id
                    and review_material_complete
                    and not research_only
                ),
                "can_reject": (
                    can_review
                    and draft.status == DraftStatus.IN_REVIEW.value
                    and revision.created_by != principal.user_id
                    and review_material_complete
                ),
                "can_publish": (
                    can_publish
                    and draft.status == DraftStatus.APPROVED.value
                    and review_material_complete
                    and not research_only
                ),
            }
        )

    team: list[dict[str, object]] = []
    for membership, user, login_ready in snapshot.team:
        team.append(
            {
                "id": user.id,
                "display_name": user.display_name,
                "username": user.username or "sin-usuario",
                "email": user.email,
                "role": membership.role,
                "active": user.is_active,
                "login_ready": (not user.email.endswith(".internal") and login_ready),
            }
        )

    can_manage_team = has_permission(principal.role, Permission.MANAGE_USERS) and has_permission(
        principal.role, Permission.MANAGE_MEMBERSHIPS
    )
    can_manage_automation = has_permission(principal.role, Permission.MANAGE_SCHEDULE)
    can_manage_automation_mode = has_permission(
        principal.role,
        Permission.MANAGE_AUTOMATION_MODE,
    )
    assignable_roles = [
        {"value": role.value, "label": _ROLE_LABELS[role]}
        for role in Role
        if can_assign_role(principal.role, role)
    ]
    context = {
        "principal": {
            "display_name": principal.display_name,
            "username": principal.username,
            "email": principal.email,
            "role": principal.role.value,
        },
        "csrf_token": csrf_token,
        "generation_request_id": secrets.token_urlsafe(24),
        "automation": {
            "enabled": settings.enabled,
            "mode": settings.mode,
            "version": settings.version,
            "timezone": settings.timezone,
            "max_posts_per_day": settings.max_posts_per_day,
            "slots": [
                {
                    "id": str(raw_slot.get("id") or f"slot-{index + 1}"),
                    "at": str(raw_slot.get("at") or ""),
                    "category": str(raw_slot.get("category") or ""),
                }
                for index, raw_slot in enumerate(
                    settings.slots if isinstance(settings.slots, list) else []
                )
                if isinstance(raw_slot, Mapping) and index < 20
            ],
        },
        "draft_counts": dict(counts),
        "drafts": drafts,
        "team": team,
        "calendar": _dashboard_calendar(settings),
        "can_manage_team": can_manage_team,
        "can_manage_automation": can_manage_automation,
        "can_manage_automation_mode": can_manage_automation_mode,
        "can_generate": has_permission(principal.role, Permission.CREATE_DRAFTS),
        "assignable_roles": assignable_roles,
        "direct_available": _direct_mode_available(provider.environ),
        "flash": _NOTICE_MESSAGES.get(notice or ""),
        "error": _ERROR_MESSAGES.get(error or ""),
    }
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context=context,
    )


def _dashboard_calendar(settings: Any, *, days: int = 7) -> list[dict[str, str]]:
    zone = ZoneInfo(settings.timezone)
    first_day = datetime.now(UTC).astimezone(zone).date()
    entries: list[dict[str, str]] = []
    slots = settings.slots if isinstance(settings.slots, list) else []
    for offset in range(days):
        local_day = first_day + timedelta(days=offset)
        weekday = CANONICAL_AUTOMATION_WEEKDAYS[local_day.weekday()]
        for raw_slot in slots:
            if not isinstance(raw_slot, Mapping):
                continue
            weekdays = raw_slot.get("weekdays")
            if isinstance(weekdays, list) and weekday not in weekdays:
                continue
            entries.append(
                {
                    "date": local_day.isoformat(),
                    "time": str(raw_slot.get("at") or "--:--"),
                    "slot": str(raw_slot.get("id") or "slot"),
                    "mode": str(raw_slot.get("mode") or settings.mode),
                    "category": str(raw_slot.get("category") or ""),
                }
            )
    return entries


def _authenticated_web_get(provider: RuntimeProvider, request: Request) -> tuple[Any, str]:
    session_token = request.cookies.get(SESSION_COOKIE, "")
    csrf_token = request.cookies.get(CSRF_COOKIE, "")
    if not session_token or not csrf_token:
        raise InvalidSessionError("La sesión no es válida")
    try:
        principal = provider.get_web_auth().verify_csrf(session_token, csrf_token)
    except CsrfError as exc:
        raise InvalidSessionError("La sesión no es válida") from exc
    return principal, csrf_token


async def _authenticated_web_post(
    provider: RuntimeProvider,
    request: Request,
) -> tuple[dict[str, str], Any, str]:
    form = await _read_web_form(request)
    _require_same_origin(request)
    session_token = request.cookies.get(SESSION_COOKIE, "")
    csrf_cookie = request.cookies.get(CSRF_COOKIE, "")
    csrf_form = form.get("csrf_token", "")
    if (
        not session_token
        or not csrf_cookie
        or not csrf_form
        or not secrets.compare_digest(csrf_cookie, csrf_form)
    ):
        raise CsrfError("CSRF inválido")
    auth = await run_in_threadpool(provider.get_web_auth)
    principal = await run_in_threadpool(auth.verify_csrf, session_token, csrf_form)
    return form, principal, session_token


def _require_login_csrf(request: Request, form: Mapping[str, str]) -> str:
    cookie_token = request.cookies.get(LOGIN_CSRF_COOKIE, "")
    form_token = form.get("csrf_token", "")
    if not cookie_token or not form_token or not secrets.compare_digest(cookie_token, form_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
    return cookie_token


async def _read_web_form(
    request: Request,
    *,
    maximum_bytes: int = MAX_WEB_FORM_BYTES,
) -> dict[str, str]:
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().casefold()
    if content_type != "application/x-www-form-urlencoded":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid form")
    raw_length = request.headers.get("content-length")
    if raw_length is not None:
        try:
            content_length = int(raw_length)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid body",
            ) from exc
        if content_length < 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid body")
        if content_length > maximum_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="body too large",
            )
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > maximum_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="body too large",
            )
    try:
        parsed = parse_qs(
            body.decode("utf-8"),
            keep_blank_values=True,
            max_num_fields=40,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid form") from exc
    if any(len(values) != 1 for values in parsed.values()):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid form")
    return {key: values[0] for key, values in parsed.items()}


def _require_same_origin(request: Request) -> None:
    origin = request.headers.get("origin", "")
    host = request.headers.get("host", "").strip().casefold()
    if not origin or not host:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
    try:
        parsed = urlsplit(origin)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden") from exc
    if (
        parsed.scheme.casefold() != _request_scheme(request)
        or parsed.netloc.casefold() != host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")


def _request_scheme(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-proto", "").partition(",")[0].strip().casefold()
    return forwarded if forwarded in {"http", "https"} else request.url.scheme.casefold()


def _client_ip(request: Request, environ: Mapping[str, str]) -> str:
    candidates: list[str] = []
    if environ.get("VERCEL"):
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            candidates.append(forwarded.partition(",")[0].strip())
    if request.client is not None:
        candidates.append(request.client.host)
    for candidate in candidates:
        try:
            return ipaddress.ip_address(candidate).compressed
        except ValueError:
            continue
    return "127.0.0.1"


async def _uniform_login_delay(started_at: float) -> None:
    """Reduce la señal temporal cuenta-existente sin bloquear el event loop."""

    target_seconds = 1.0 + (secrets.randbelow(251) / 1_000)
    remaining = target_seconds - (time.monotonic() - started_at)
    if remaining > 0:
        await asyncio.sleep(remaining)


def _direct_mode_available(environ: Mapping[str, str]) -> bool:
    return all(
        environ.get(name, "").strip().casefold() == "true"
        for name in ("COLMAT_DIRECT_PUBLISH_ENABLED", "COLMAT_LIVE_ENABLED")
    )


def _valid_request_nonce(value: str) -> bool:
    return bool(
        isinstance(value, str)
        and 20 <= len(value) <= 80
        and value.isascii()
        and all(character.isalnum() or character in {"-", "_"} for character in value)
    )


def _app_redirect(*, notice: str | None = None, error: str | None = None) -> RedirectResponse:
    query = {key: value for key, value in (("notice", notice), ("error", error)) if value}
    target = f"/app?{urlencode(query)}" if query else "/app"
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)


def _redirect_to_login() -> RedirectResponse:
    response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    _clear_auth_cookies(response)
    return response


def _clear_session_cookies(response: Any) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True)
    response.delete_cookie(CSRF_COOKIE, path="/", secure=True, httponly=True)


def _clear_auth_cookies(response: Any) -> None:
    _clear_session_cookies(response)
    response.delete_cookie(LOGIN_CSRF_COOKIE, path="/", secure=True, httponly=True)


def _require_configured_webhook_header(provider: RuntimeProvider, request: Request) -> None:
    """Rechaza tráfico no autenticado antes de leer el cuerpo del webhook."""

    configured = provider.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    if not isinstance(configured, str) or not configured.strip():
        # Los runtimes inyectados conservan la validación interna; producción
        # declara siempre la credencial y toma la ruta temprana.
        return
    try:
        secret = TelegramWebhookSecret.from_environment(environ=provider.environ)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="service unavailable",
        ) from exc
    if not secret.matches(request.headers.get(TELEGRAM_SECRET_HEADER)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")


async def _read_json_object(request: Request, *, maximum_bytes: int) -> dict[str, object]:
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().casefold()
    if content_type != "application/json":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid JSON")
    raw_length = request.headers.get("content-length")
    if raw_length is not None:
        try:
            content_length = int(raw_length)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid body",
            ) from exc
        if content_length < 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid body")
        if content_length > maximum_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="body too large",
            )

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > maximum_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="body too large",
            )
    try:
        payload = json.loads(body.decode("utf-8"))
    except (JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid update")
    return payload


def _deliver_and_finish_webhook(runtime: WebRuntime, result: Any) -> None:
    """Mantiene entrega y cierre fuera del event loop y en el orden cercado."""

    execute_bot_actions(
        runtime.telegram_client,
        result.actions,
        replay=result.replayed,
    )
    _finish_update(runtime.store, result.update_id, result=result)


def _finish_failed_update(
    provider: RuntimeProvider,
    payload: Mapping[str, object],
    exc: Exception,
    *,
    claim_token: str | None = None,
    claim_fence: int | None = None,
) -> None:
    update_id = payload.get("update_id")
    if isinstance(update_id, bool) or not isinstance(update_id, int) or update_id < 0:
        return
    try:
        runtime = provider.get()
        _finish_update(
            runtime.store,
            update_id,
            error=f"webhook_failed:{type(exc).__name__}",
            claim_token=claim_token,
            claim_fence=claim_fence,
        )
    except Exception:
        return


def _finish_update(
    store: object,
    update_id: int,
    *,
    result: object | None = None,
    error: str | None = None,
    claim_token: str | None = None,
    claim_fence: int | None = None,
) -> object:
    """Finaliza stores cercados sin romper fakes/implementaciones heredadas."""

    if result is not None:
        claim_token = getattr(result, "claim_token", None)
        claim_fence = getattr(result, "claim_fence", None)
    kwargs: dict[str, object] = {}
    if error is not None:
        kwargs["error"] = error
    if claim_token is not None and claim_fence is not None:
        kwargs["claim_token"] = claim_token
        kwargs["claim_fence"] = claim_fence
    finish = store.finish_telegram_update  # type: ignore[attr-defined]
    return finish(update_id, **kwargs)
