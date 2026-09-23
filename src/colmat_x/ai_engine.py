"""Puerto de servicio para el motor de IA: selección, credenciales y estado.

Lo usan el panel web, la CLI y el bot de Telegram sin duplicar lógica. Todas
las decisiones de autorización viven en `PlatformStore`; aquí solo se compone:
catálogo cerrado, sobre sellado, comprobación mínima contra el proveedor y la
resolución de la clave que usará el worker.

Precedencia documentada: si existe una credencial sellada en la base para el
proveedor, gana sobre la variable del entorno del worker; si no existe, el
adaptador usa la variable de su archivo `0600`. Si existe pero el proceso no
puede abrirla, el uso falla cerrado con un detalle claro y nunca cae en
silencio a la variable del entorno.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from colmat_x.credential_cipher import (
    PRIVATE_KEY_ENV,
    PUBLIC_KEY_ENV,
    CredentialCipher,
    CredentialCipherUnavailable,
    CredentialEnvelopeError,
    credential_fingerprint,
)
from colmat_x.platform_store import (
    DEFAULT_WORKSPACE_ID,
    UNSET,
    AIEngineSettings,
    AIProviderCredential,
    AIProviderStatus,
    PlatformStore,
)
from colmat_x.providers import (
    PROVIDER_CATALOG,
    PROVIDER_NAMES,
    AIEngine,
    CredentialCheck,
    EngineSelection,
    ProviderConfigurationError,
    check_provider_api_key,
    engine_from_selection,
    provider_credentials_status,
)
from colmat_x.rbac import Permission, Role, has_permission

_UNSET = UNSET
MIN_API_KEY_CHARACTERS = 8
MAX_API_KEY_CHARACTERS = 4_096


class AICredentialError(ValueError):
    """La clave recibida no tiene un formato admisible; el mensaje nunca la incluye."""


@dataclass(frozen=True)
class ProviderOverview:
    name: str
    label: str
    text_models: tuple[str, ...]
    image_models: tuple[str, ...]
    api_key_variable: str
    stored: dict[str, Any] | None
    worker_status: dict[str, Any] | None

    @property
    def supports_images(self) -> bool:
        return bool(self.image_models)

    @property
    def stored_configured(self) -> bool:
        return self.stored is not None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "text_models": list(self.text_models),
            "image_models": list(self.image_models),
            "supports_images": self.supports_images,
            "api_key_variable": self.api_key_variable,
            "stored": self.stored,
            "stored_configured": self.stored_configured,
            "worker_status": self.worker_status,
        }


@dataclass(frozen=True)
class AIEngineOverview:
    selection: dict[str, Any]
    providers: tuple[ProviderOverview, ...]
    encryption: dict[str, Any]
    can_manage_engine: bool
    can_manage_credentials: bool

    def provider(self, name: str) -> ProviderOverview:
        for entry in self.providers:
            if entry.name == name:
                return entry
        raise KeyError(name)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "selection": self.selection,
            "providers": [entry.to_mapping() for entry in self.providers],
            "encryption": self.encryption,
            "can_manage_engine": self.can_manage_engine,
            "can_manage_credentials": self.can_manage_credentials,
        }


@dataclass(frozen=True)
class CredentialStoreResult:
    provider: str
    replaced: bool
    check: CredentialCheck | None
    view: dict[str, Any]


@dataclass(frozen=True)
class CredentialVerification:
    provider: str
    outcome: str  # verified | requested
    check: CredentialCheck | None
    view: dict[str, Any]


class AIEngineService:
    """Fachada única sobre el store, el cifrador y el catálogo cerrado."""

    def __init__(
        self,
        store: PlatformStore,
        *,
        cipher: CredentialCipher | None = None,
        environ: Mapping[str, str] | None = None,
        http_session: Any | None = None,
        clock: Callable[[], datetime] | None = None,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> None:
        self.store = store
        self.environ = os.environ if environ is None else environ
        self.cipher = (
            cipher if cipher is not None else CredentialCipher.from_environment(self.environ)
        )
        self.http_session = http_session
        self._clock = clock
        self.workspace_id = workspace_id

    # --- lectura -------------------------------------------------------------------

    def encryption_status(self) -> dict[str, Any]:
        return {
            "can_encrypt": self.cipher.can_encrypt,
            "can_decrypt": self.cipher.can_decrypt,
            "key_id": self.cipher.key_id,
            "public_key_variable": PUBLIC_KEY_ENV,
            "private_key_variable": PRIVATE_KEY_ENV,
        }

    def selection(self, actor_id: str) -> AIEngineSettings:
        return self.store.get_ai_engine_settings(actor_id=actor_id, workspace_id=self.workspace_id)

    def overview(self, actor_id: str, *, role: Role | str | None = None) -> AIEngineOverview:
        """Estado completo para panel o bot; nunca contiene un sobre ni una clave."""

        settings = self.selection(actor_id)
        statuses = self.store.list_ai_provider_status(
            actor_id=actor_id, workspace_id=self.workspace_id
        )
        effective_role = Role(role) if role is not None else None
        credentials: list[AIProviderCredential] = []
        if effective_role is not None and has_permission(
            effective_role, Permission.MANAGE_AI_ENGINE
        ):
            credentials = self.store.list_ai_credentials(
                actor_id=actor_id, workspace_id=self.workspace_id
            )
        return self.overview_from_rows(settings, statuses, credentials, role=effective_role)

    def overview_from_rows(
        self,
        settings: AIEngineSettings | None,
        statuses: Any,
        credentials: Any,
        *,
        role: Role | str | None,
    ) -> AIEngineOverview:
        """Compone la vista desde filas ya cargadas (el panel usa su snapshot)."""

        if settings is None:
            raise ValueError("El workspace no tiene motor de IA configurado")
        effective_role = Role(role) if role is not None else None
        detailed = effective_role is not None and has_permission(
            effective_role, Permission.MANAGE_AI_ENGINE
        )
        status_map = {row.provider: row.to_mapping() for row in statuses}
        stored: dict[str, dict[str, Any] | None] = {name: None for name in PROVIDER_NAMES}
        if detailed:
            for row in credentials:
                stored[row.provider] = row.public_view()
        statuses = status_map
        providers = tuple(
            ProviderOverview(
                name=entry.name,
                label=entry.label,
                text_models=entry.text_models,
                image_models=entry.image_models,
                api_key_variable=entry.api_key_variable,
                stored=stored.get(entry.name),
                worker_status=statuses.get(entry.name),
            )
            for entry in PROVIDER_CATALOG.values()
        )
        return AIEngineOverview(
            selection=settings.to_mapping(),
            providers=providers,
            encryption=self.encryption_status(),
            can_manage_engine=detailed,
            can_manage_credentials=(
                effective_role is not None
                and has_permission(effective_role, Permission.MANAGE_AI_CREDENTIALS)
            ),
        )

    # --- selección con CAS --------------------------------------------------------------

    def select_engine(
        self,
        actor_id: str,
        *,
        expected_version: int,
        text_provider: object = _UNSET,
        text_model: object = _UNSET,
        image_provider: object = _UNSET,
        image_model: object = _UNSET,
    ) -> AIEngineSettings:
        return self.store.update_ai_engine_settings(
            actor_id=actor_id,
            expected_version=expected_version,
            text_provider=text_provider,
            text_model=text_model,
            image_provider=image_provider,
            image_model=image_model,
            workspace_id=self.workspace_id,
            now=self._now(),
        )

    # --- credenciales -------------------------------------------------------------------

    def store_credential(
        self,
        actor_id: str,
        provider: str,
        api_key: str,
        *,
        verify: bool = True,
    ) -> CredentialStoreResult:
        """Comprueba (opcional), sella y persiste; la clave vive solo en esta llamada."""

        name = _provider(provider)
        secret = normalize_api_key(api_key)
        if not self.cipher.can_encrypt:
            raise CredentialCipherUnavailable(
                f"El almacén cifrado no está disponible: falta {PUBLIC_KEY_ENV}"
            )
        check = check_provider_api_key(name, secret, session=self.http_session) if verify else None
        envelope = self.cipher.seal(secret, context=self._context(name))
        key_id = self.cipher.key_id
        assert key_id is not None
        existing = self.store.ai_credentials_configured(
            actor_id=actor_id, workspace_id=self.workspace_id
        ).get(name, False)
        row = self.store.store_ai_credential(
            name,
            actor_id=actor_id,
            envelope=envelope,
            key_id=key_id,
            fingerprint=credential_fingerprint(secret),
            last_four=secret[-4:],
            byte_length=len(secret.encode("utf-8")),
            verification=(check.status, check.detail) if check is not None else None,
            workspace_id=self.workspace_id,
            now=self._now(),
        )
        return CredentialStoreResult(
            provider=name, replaced=existing, check=check, view=row.public_view()
        )

    def delete_credential(self, actor_id: str, provider: str) -> bool:
        return self.store.delete_ai_credential(
            _provider(provider),
            actor_id=actor_id,
            workspace_id=self.workspace_id,
            now=self._now(),
        )

    def verify_credential(self, actor_id: str, provider: str) -> CredentialVerification:
        """Verifica en el acto si este proceso puede abrir el sobre; si no, lo encola."""

        name = _provider(provider)
        if self.cipher.can_decrypt:
            row = self.store.get_ai_credential_envelope(
                name, actor_id=actor_id, workspace_id=self.workspace_id
            )
            if row is None:
                check = CredentialCheck("error", "No hay credencial sellada para este proveedor.")
                raise AICredentialError(check.detail)
            check = self._check_envelope(row)
            updated = self.store.record_ai_credential_verification(
                name,
                actor_id=actor_id,
                status=check.status,
                detail=check.detail,
                workspace_id=self.workspace_id,
                now=self._now(),
            )
            return CredentialVerification(name, "verified", check, updated.public_view())
        requested = self.store.request_ai_credential_verification(
            name,
            actor_id=actor_id,
            workspace_id=self.workspace_id,
            now=self._now(),
        )
        return CredentialVerification(name, "requested", None, requested.public_view())

    # --- worker ------------------------------------------------------------------------

    def resolve_api_key(self, provider: str, *, actor_id: str) -> str | None:
        """Base gana; None deja que el adaptador use la variable del entorno."""

        name = _provider(provider)
        row = self.store.get_ai_credential_envelope(
            name, actor_id=actor_id, workspace_id=self.workspace_id
        )
        if row is None:
            return None
        label = PROVIDER_CATALOG[name].label
        if not self.cipher.can_decrypt:
            raise ProviderConfigurationError(
                f"La credencial de {label} está sellada en la base, pero este proceso no puede "
                f"abrirla: falta {PRIVATE_KEY_ENV}"
            )
        try:
            return self.cipher.open(row.envelope, context=self._context(name))
        except CredentialEnvelopeError as exc:
            raise ProviderConfigurationError(
                f"La credencial sellada de {label} no se pudo abrir con la clave privada de este "
                f"worker (sobre {row.key_id})"
            ) from exc

    def build_engine(self, actor_id: str, *, session: Any | None = None) -> AIEngine:
        settings = self.selection(actor_id)
        selection = EngineSelection(
            text_provider=settings.text_provider,
            text_model=settings.text_model,
            image_provider=settings.image_provider,
            image_model=settings.image_model,
        )
        return engine_from_selection(
            selection,
            api_key_resolver=lambda name: self.resolve_api_key(name, actor_id=actor_id),
            session=session,
        )

    def report_worker_status(self, actor_id: str) -> list[AIProviderStatus]:
        """Publica presencia de claves en el entorno del worker; nunca sus valores."""

        return self.store.report_ai_provider_status(
            actor_id=actor_id,
            statuses=provider_credentials_status(self.environ),
            can_decrypt_credentials=self.cipher.can_decrypt,
            workspace_id=self.workspace_id,
            now=self._now(),
        )

    def run_pending_verifications(self, actor_id: str) -> list[tuple[str, CredentialCheck]]:
        """Ejecuta las verificaciones solicitadas desde el panel; requiere la clave privada."""

        results: list[tuple[str, CredentialCheck]] = []
        pending = self.store.list_pending_ai_credential_verifications(
            actor_id=actor_id, workspace_id=self.workspace_id
        )
        for row in pending:
            if not self.cipher.can_decrypt:
                check = CredentialCheck(
                    "error", f"El worker no puede abrir el sobre: falta {PRIVATE_KEY_ENV}."
                )
            else:
                check = self._check_envelope(row)
            self.store.record_ai_credential_verification(
                row.provider,
                actor_id=actor_id,
                status=check.status,
                detail=check.detail,
                workspace_id=self.workspace_id,
                now=self._now(),
            )
            results.append((row.provider, check))
        return results

    # --- internos -----------------------------------------------------------------------

    def _check_envelope(self, row: AIProviderCredential) -> CredentialCheck:
        try:
            secret = self.cipher.open(row.envelope, context=self._context(row.provider))
        except CredentialEnvelopeError:
            return CredentialCheck(
                "error", f"El sobre sellado no se pudo abrir con esta clave ({row.key_id})."
            )
        return check_provider_api_key(row.provider, secret, session=self.http_session)

    def _context(self, provider: str) -> str:
        return f"colmat:ai-credential:{self.workspace_id}:{provider}"

    def _now(self) -> datetime | None:
        if self._clock is None:
            return None
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("El reloj del servicio debe devolver un datetime con zona horaria")
        return value.astimezone(UTC)


def normalize_api_key(value: object) -> str:
    """Formato mínimo defensivo; el mensaje de error nunca refleja el valor."""

    if not isinstance(value, str):
        raise AICredentialError("La clave debe ser texto")
    secret = value.strip()
    if not MIN_API_KEY_CHARACTERS <= len(secret) <= MAX_API_KEY_CHARACTERS:
        raise AICredentialError(
            f"La clave debe tener entre {MIN_API_KEY_CHARACTERS} y {MAX_API_KEY_CHARACTERS} "
            "caracteres"
        )
    if (
        not secret.isascii()
        or not secret.isprintable()
        or any(character.isspace() for character in secret)
    ):
        raise AICredentialError("La clave contiene caracteres no admitidos")
    return secret


def _provider(value: object) -> str:
    try:
        from colmat_x.providers import normalize_any_provider_name

        return normalize_any_provider_name(value)
    except ProviderConfigurationError as exc:
        raise ValueError(str(exc)) from exc
