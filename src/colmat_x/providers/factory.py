"""Fábrica de motores de IA sobre una lista cerrada de proveedores.

`AIEngine` compone un proveedor de texto y otro de imagen (pueden ser
distintos), construye los clientes de forma perezosa y resuelve la clave de
cada proveedor en el momento de usarla: primero la del almacén cifrado, si se
inyecta un resolutor, y después la del entorno del proceso. Ninguna ruta acepta
endpoints: los adaptadores los fijan en constantes.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from colmat_x.editorial import EditorialCategory, EditorialDraft, EditorialPolicy, Institution
from colmat_x.providers.anthropic import AnthropicClient
from colmat_x.providers.base import (
    CredentialCheck,
    GeneratedImage,
    ProviderCapabilityError,
    ProviderConfigurationError,
)
from colmat_x.providers.catalog import (
    DEFAULT_PROVIDER,
    IMAGE_PROVIDER_ENV,
    PROVIDER_ANTHROPIC,
    PROVIDER_CATALOG,
    PROVIDER_GEMINI,
    PROVIDER_MINIMAX,
    PROVIDER_OPENAI,
    TEXT_PROVIDER_ENV,
    normalize_any_provider_name,
    normalize_model_name,
    normalize_provider_name,
)
from colmat_x.providers.gemini import GeminiClient
from colmat_x.providers.minimax import MiniMaxClient
from colmat_x.providers.openai import OpenAIClient

ApiKeyResolver = Callable[[str], "str | None"]

_CLIENT_CLASSES: Mapping[str, type] = {
    PROVIDER_MINIMAX: MiniMaxClient,
    PROVIDER_ANTHROPIC: AnthropicClient,
    PROVIDER_OPENAI: OpenAIClient,
    PROVIDER_GEMINI: GeminiClient,
}


@dataclass(frozen=True)
class EngineSelection:
    """Elección validada de proveedor y modelo para texto e imagen."""

    text_provider: str
    text_model: str
    image_provider: str
    image_model: str

    def to_mapping(self) -> dict[str, str]:
        return {
            "text_provider": self.text_provider,
            "text_model": self.text_model,
            "image_provider": self.image_provider,
            "image_model": self.image_model,
        }


def normalize_engine_selection(
    *,
    text_provider: object,
    text_model: object,
    image_provider: object,
    image_model: object,
) -> EngineSelection:
    """Valida los cuatro valores contra el catálogo cerrado."""

    text_name = normalize_provider_name(
        text_provider, capability="text", field_name="text_provider"
    )
    image_name = normalize_provider_name(
        image_provider, capability="image", field_name="image_provider"
    )
    return EngineSelection(
        text_provider=text_name,
        text_model=normalize_model_name(text_name, text_model, capability="text"),
        image_provider=image_name,
        image_model=normalize_model_name(image_name, image_model, capability="image"),
    )


def default_engine_selection() -> EngineSelection:
    entry = PROVIDER_CATALOG[DEFAULT_PROVIDER]
    assert entry.default_image_model is not None
    return EngineSelection(
        text_provider=DEFAULT_PROVIDER,
        text_model=entry.default_text_model,
        image_provider=DEFAULT_PROVIDER,
        image_model=entry.default_image_model,
    )


class AIEngine:
    """Motor compuesto: texto e imagen pueden venir de proveedores distintos.

    Nunca aprueba, programa ni publica: delega en adaptadores cuya única salida
    es un `EditorialDraft` validado o una `GeneratedImage` en memoria.
    """

    def __init__(
        self,
        *,
        text_provider: str,
        text_model: str | None = None,
        image_provider: str | None = None,
        image_model: str | None = None,
        api_key_resolver: ApiKeyResolver | None = None,
        session: Any | None = None,
    ) -> None:
        self.provider_name = normalize_provider_name(
            text_provider, capability="text", field_name="text_provider"
        )
        self.model = (
            normalize_model_name(self.provider_name, text_model, capability="text")
            if text_model is not None
            else PROVIDER_CATALOG[self.provider_name].default_text_model
        )
        if image_provider is None:
            self.image_provider_name: str | None = (
                self.provider_name if PROVIDER_CATALOG[self.provider_name].supports_images else None
            )
        else:
            self.image_provider_name = normalize_provider_name(
                image_provider, capability="image", field_name="image_provider"
            )
        self.image_model: str | None
        if self.image_provider_name is None:
            if image_model is not None:
                raise ProviderConfigurationError(
                    "image_model requiere un proveedor de imagen de la lista cerrada"
                )
            self.image_model = None
        elif image_model is not None:
            self.image_model = normalize_model_name(
                self.image_provider_name, image_model, capability="image"
            )
        else:
            self.image_model = PROVIDER_CATALOG[self.image_provider_name].default_image_model
        self._api_key_resolver = api_key_resolver
        self._session = session
        self._clients: dict[str, Any] = {}
        self._lock = threading.Lock()

    @property
    def supports_images(self) -> bool:
        return self.image_provider_name is not None

    def describe(self) -> dict[str, Any]:
        return {
            "text": {"provider": self.provider_name, "model": self.model},
            "image": (
                {"provider": self.image_provider_name, "model": self.image_model}
                if self.image_provider_name is not None
                else None
            ),
        }

    def generate_draft(
        self,
        brief: str,
        policy: EditorialPolicy,
        *,
        category: EditorialCategory | str | None = None,
        institution: Institution | str | None = None,
    ) -> EditorialDraft:
        client = self._client(self.provider_name)
        return client.generate_draft(brief, policy, category=category, institution=institution)

    def generate_image(
        self,
        prompt: str,
        policy: EditorialPolicy,
        *,
        aspect_ratio: str | None = None,
        alt_text: str | None = None,
        **extra: Any,
    ) -> GeneratedImage:
        if self.image_provider_name is None:
            label = PROVIDER_CATALOG[self.provider_name].label
            raise ProviderCapabilityError(
                f"{label} no genera imágenes; configura un proveedor de imagen "
                f"({IMAGE_PROVIDER_ENV} o la selección persistida)"
            )
        client = self._client(self.image_provider_name)
        return client.generate_image(
            prompt, policy, aspect_ratio=aspect_ratio, alt_text=alt_text, **extra
        )

    def _client(self, provider: str) -> Any:
        with self._lock:
            client = self._clients.get(provider)
            if client is None:
                client = self._build(provider)
                self._clients[provider] = client
            return client

    def _build(self, provider: str) -> Any:
        api_key = self._api_key_resolver(provider) if self._api_key_resolver else None
        kwargs: dict[str, Any] = {"api_key": api_key, "session": self._session}
        if provider == self.provider_name:
            kwargs["model"] = self.model
        if provider == self.image_provider_name and self.image_model is not None:
            kwargs["image_model"] = self.image_model
        return _CLIENT_CLASSES[provider](**kwargs)


def build_ai_engine(
    *,
    text_provider: str | None = None,
    text_model: str | None = None,
    image_provider: str | None = None,
    image_model: str | None = None,
    api_key_resolver: ApiKeyResolver | None = None,
    environ: Mapping[str, str] | None = None,
    session: Any | None = None,
) -> AIEngine:
    """Resuelve la selección desde argumentos o del entorno; MiniMax por defecto."""

    source = os.environ if environ is None else environ
    text_name = text_provider or source.get(TEXT_PROVIDER_ENV) or DEFAULT_PROVIDER
    image_name = image_provider or source.get(IMAGE_PROVIDER_ENV) or None
    return AIEngine(
        text_provider=text_name,
        text_model=text_model,
        image_provider=image_name,
        image_model=image_model,
        api_key_resolver=api_key_resolver,
        session=session,
    )


def engine_from_selection(
    selection: EngineSelection,
    *,
    api_key_resolver: ApiKeyResolver | None = None,
    session: Any | None = None,
) -> AIEngine:
    return AIEngine(
        text_provider=selection.text_provider,
        text_model=selection.text_model,
        image_provider=selection.image_provider,
        image_model=selection.image_model,
        api_key_resolver=api_key_resolver,
        session=session,
    )


def check_provider_api_key(
    provider: str,
    api_key: str,
    *,
    session: Any | None = None,
) -> CredentialCheck:
    """Comprueba una clave contra el proveedor con la llamada mínima documentada."""

    name = normalize_any_provider_name(provider)
    return _CLIENT_CLASSES[name].check_api_key(api_key, session=session)
