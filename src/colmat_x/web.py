from __future__ import annotations

import json
import os
import threading
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from json import JSONDecodeError
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import inspect, text

from colmat_x.platform_store import (
    CANONICAL_AUTOMATION_WEEKDAYS,
    AutomationMode,
    ConflictError,
    DraftStatus,
    NotFoundError,
    PlatformStore,
    StaleSnapshotError,
)
from colmat_x.rbac import AuthorizationError
from colmat_x.telegram_api import (
    TelegramApiClient,
    TelegramConfigurationError,
    TelegramCredentials,
)
from colmat_x.telegram_bot import (
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

MAX_TELEGRAM_UPDATE_BYTES = 1_048_576
TELEGRAM_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
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
    "approvals": frozenset({"revision_id", "snapshot_hash"}),
    "automation_review_notifications": frozenset(
        {
            "automation_run_id",
            "claim_fence",
            "claim_token_hash",
            "lease_expires_at",
            "status",
        }
    ),
    "automation_runs": frozenset({"settings_version", "slot_hash", "status"}),
    "automation_settings": frozenset({"enabled", "mode", "slots", "version"}),
    "callback_intents": frozenset({"nonce_hash", "snapshot_hash"}),
    "drafts": frozenset({"approved_revision_id", "current_revision_id", "status"}),
    "generation_notifications": frozenset(
        {
            "generation_request_id",
            "claim_fence",
            "claim_token_hash",
            "lease_expires_at",
            "status",
        }
    ),
    "generation_requests": frozenset(
        {
            "claim_fence",
            "claim_token_hash",
            "idempotency_key",
            "lease_expires_at",
            "status",
        }
    ),
    "media_assets": frozenset({"byte_size", "sha256"}),
    "memberships": frozenset({"role", "user_id", "workspace_id"}),
    "publication_requests": frozenset(
        {
            "claim_fence",
            "claim_token_hash",
            "lease_expires_at",
            "publish_attempt_id",
            "snapshot_hash",
            "status",
        }
    ),
    "publish_attempts": frozenset({"snapshot_hash", "status"}),
    "revisions": frozenset({"image_sha256", "snapshot_hash"}),
    "telegram_bindings": frozenset({"chat_id", "telegram_user_id", "user_id"}),
    "telegram_updates": frozenset(
        {
            "business_result",
            "claim_fence",
            "claim_token_hash",
            "lease_expires_at",
            "prepared_actions",
        }
    ),
    "users": frozenset({"username"}),
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
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        if not isinstance(generate_images, bool):
            raise TypeError("generate_images debe ser booleano")
        self._generate_images = generate_images
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
            generation_request = self.store.enqueue_generation_request(
                brief,
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
                "No se encoló la generación. Revisa permisos y el vínculo de Telegram; "
                "no se llamó a MiniMax ni se realizó ninguna publicación.",
                accepted=False,
            )
        return CommandResult(
            f"Generación encolada como {generation_request.id}. OpenClaw la asignará a "
            "MiniMax fuera del webhook; el resultado siempre exigirá revisión humana"
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


def _generation_default_image(environ: Mapping[str, str]) -> bool:
    raw_value = environ.get("COLMAT_GENERATION_DEFAULT_IMAGE", "true")
    value = raw_value.strip().casefold() if isinstance(raw_value, str) else ""
    if value not in {"true", "false"}:
        raise WebConfigurationError(
            "COLMAT_GENERATION_DEFAULT_IMAGE debe ser true o false exactamente"
        )
    return value == "true"


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
    ) -> None:
        self.environ = os.environ if environ is None else environ
        self._injected = WebRuntime(store, processor, telegram_client)
        self._runtime: WebRuntime | None = None
        self._lock = threading.Lock()

    def get(self) -> WebRuntime:
        if self._runtime is not None:
            return self._runtime
        with self._lock:
            if self._runtime is None:
                self._runtime = self._build()
        return self._runtime

    def readiness(self) -> tuple[bool, dict[str, str]]:
        checks: dict[str, str] = {}
        if self.environ.get("VERCEL"):
            checks["worker_secrets"] = (
                "error"
                if any(self.environ.get(name, "") for name in PRIVILEGED_WORKER_ENVIRONMENT)
                else "ok"
            )

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
                        for table_name, expected_columns in REQUIRED_DATABASE_COLUMNS.items():
                            if not inspector.has_table(table_name):
                                raise RuntimeError("El esquema operativo está incompleto")
                            actual_columns = {
                                column["name"] for column in inspector.get_columns(table_name)
                            }
                            if not expected_columns.issubset(actual_columns):
                                raise RuntimeError("El esquema operativo está incompleto")
                checks["database"] = "ok"
            except Exception:
                checks["database"] = "error"

        return all(result == "ok" for result in checks.values()), checks

    def _build(self) -> WebRuntime:
        store = self._injected.store
        if store is None:
            database_url = self.environ.get("DATABASE_URL", "").strip() or None
            if self.environ.get("VERCEL") and database_url is None:
                raise WebConfigurationError("DATABASE_URL es obligatorio en Vercel")
            store = PlatformStore(
                database_url=database_url,
                create_schema=not bool(self.environ.get("VERCEL")),
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
                ),
                callback_nonces=store,
            )

        telegram_client = self._injected.telegram_client
        if telegram_client is None:
            credentials = TelegramCredentials.from_environment(environ=self.environ)
            telegram_client = TelegramApiClient(credentials)
        return WebRuntime(store=store, processor=processor, telegram_client=telegram_client)


def create_app(
    *,
    environ: Mapping[str, str] | None = None,
    store: Any | None = None,
    processor: Any | None = None,
    telegram_client: Any | None = None,
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
    )
    app = FastAPI(
        title="Colmat X Automation",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.runtime_provider = provider

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

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
        payload = await _read_json_object(request, maximum_bytes=max_update_bytes)
        try:
            runtime = provider.get()
            result = runtime.processor.process_update(
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
                _finish_failed_update(provider, payload, exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="service unavailable",
            ) from exc
        except ClaimedTelegramUpdateError as exc:
            _finish_failed_update(
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
            _finish_failed_update(provider, payload, exc)
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
            execute_bot_actions(
                runtime.telegram_client,
                result.actions,
                replay=result.replayed,
            )
            _finish_update(runtime.store, result.update_id, result=result)
        except Exception as exc:
            _finish_failed_update(
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
