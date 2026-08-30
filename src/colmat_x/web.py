from __future__ import annotations

import json
import os
import threading
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from json import JSONDecodeError
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import text

from colmat_x.platform_store import (
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
    DecisionResult,
    MalformedTelegramUpdate,
    TelegramWebhookProcessor,
    TelegramWebhookSecret,
    WebhookAuthenticationError,
    execute_bot_actions,
)

MAX_TELEGRAM_UPDATE_BYTES = 1_048_576
TELEGRAM_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


class WebConfigurationError(RuntimeError):
    """La función web no tiene una configuración de producción utilizable."""


class PlatformTelegramOperations:
    """Operaciones editoriales permitidas desde Telegram; nunca publica contenido."""

    def __init__(self, store: PlatformStore) -> None:
        self.store = store

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
            store = PlatformStore(database_url=database_url)

        processor = self._injected.processor
        if processor is None:
            webhook_secret = TelegramWebhookSecret.from_environment(environ=self.environ)
            processor = TelegramWebhookProcessor(
                webhook_secret=webhook_secret,
                update_store=store,
                authorizer=store,
                operations=PlatformTelegramOperations(store),
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
        except Exception as exc:
            _finish_failed_update(provider, payload, exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="service unavailable",
            ) from exc

        if result.duplicate:
            return {"ok": True, "update_id": result.update_id, "duplicate": True}

        try:
            execute_bot_actions(runtime.telegram_client, result.actions)
            runtime.store.finish_telegram_update(result.update_id)
        except Exception as exc:
            _finish_failed_update(provider, payload, exc)
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
) -> None:
    update_id = payload.get("update_id")
    if isinstance(update_id, bool) or not isinstance(update_id, int) or update_id < 0:
        return
    try:
        runtime = provider.get()
        runtime.store.finish_telegram_update(
            update_id,
            error=f"webhook_failed:{type(exc).__name__}",
        )
    except Exception:
        return
