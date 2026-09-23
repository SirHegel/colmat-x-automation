from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from colmat_x.providers.base import ProviderConfigurationError

PROVIDER_MINIMAX = "minimax"
PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OPENAI = "openai"
PROVIDER_GEMINI = "gemini"
DEFAULT_PROVIDER = PROVIDER_MINIMAX

TEXT_PROVIDER_ENV = "COLMAT_TEXT_PROVIDER"
IMAGE_PROVIDER_ENV = "COLMAT_IMAGE_PROVIDER"


@dataclass(frozen=True)
class ProviderCatalogEntry:
    """Descripción cerrada de un proveedor; ninguna variable puede ampliarla."""

    name: str
    label: str
    api_key_variable: str
    env_file_variable: str
    text_models: tuple[str, ...]
    image_models: tuple[str, ...]

    @property
    def default_text_model(self) -> str:
        return self.text_models[0]

    @property
    def default_image_model(self) -> str | None:
        return self.image_models[0] if self.image_models else None

    @property
    def supports_images(self) -> bool:
        return bool(self.image_models)


# Los modelos listados se contrastaron contra la documentación oficial el
# 2026-09-23 (developers.openai.com/api/docs/models y ai.google.dev). Ampliar o
# actualizar el catálogo es un cambio de código revisado, no una variable: un
# identificador escrito de memoria caduca y rompe la generación en producción.
PROVIDER_CATALOG: Mapping[str, ProviderCatalogEntry] = MappingProxyType(
    {
        PROVIDER_MINIMAX: ProviderCatalogEntry(
            name=PROVIDER_MINIMAX,
            label="MiniMax",
            api_key_variable="MINIMAX_API_KEY",
            env_file_variable="COLMAT_MINIMAX_ENV_FILE",
            text_models=("MiniMax-M2.7",),
            image_models=("image-01",),
        ),
        PROVIDER_ANTHROPIC: ProviderCatalogEntry(
            name=PROVIDER_ANTHROPIC,
            label="Anthropic (Claude)",
            api_key_variable="ANTHROPIC_API_KEY",
            env_file_variable="COLMAT_ANTHROPIC_ENV_FILE",
            text_models=(
                "claude-opus-5",
                "claude-sonnet-5",
                "claude-haiku-4-5",
                "claude-haiku-4-5-20251001",
            ),
            image_models=(),
        ),
        PROVIDER_OPENAI: ProviderCatalogEntry(
            name=PROVIDER_OPENAI,
            label="OpenAI",
            api_key_variable="OPENAI_API_KEY",
            env_file_variable="COLMAT_OPENAI_ENV_FILE",
            text_models=("gpt-6-astra", "gpt-6-sol", "gpt-6-luna"),
            image_models=("gpt-image-2.5-sunburst", "gpt-image-2.5-flare"),
        ),
        PROVIDER_GEMINI: ProviderCatalogEntry(
            name=PROVIDER_GEMINI,
            label="Google Gemini",
            api_key_variable="GEMINI_API_KEY",
            env_file_variable="COLMAT_GEMINI_ENV_FILE",
            # Los `gemini-2.5-*` quedaron como legacy de acceso limitado; el
            # catálogo usa los estables vigentes para no depender de ese acceso.
            text_models=("gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"),
            image_models=("gemini-3.1-flash-image", "gemini-3-pro-image"),
        ),
    }
)

PROVIDER_NAMES: tuple[str, ...] = tuple(PROVIDER_CATALOG)
TEXT_PROVIDER_NAMES: tuple[str, ...] = tuple(
    name for name, entry in PROVIDER_CATALOG.items() if entry.text_models
)
IMAGE_PROVIDER_NAMES: tuple[str, ...] = tuple(
    name for name, entry in PROVIDER_CATALOG.items() if entry.supports_images
)
API_KEY_VARIABLES: tuple[str, ...] = tuple(
    entry.api_key_variable for entry in PROVIDER_CATALOG.values()
)


# Alias coloquiales admitidos en la entrada. No amplían la lista cerrada: se
# resuelven a un nombre canónico antes de validar, y nunca aceptan una URL.
PROVIDER_ALIASES: Mapping[str, str] = MappingProxyType(
    {
        "codex": PROVIDER_OPENAI,
        "chatgpt": PROVIDER_OPENAI,
        "gpt": PROVIDER_OPENAI,
        "claude": PROVIDER_ANTHROPIC,
        "google": PROVIDER_GEMINI,
    }
)


def _canonical_provider_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().casefold()
    return PROVIDER_ALIASES.get(normalized, normalized)


def normalize_provider_name(value: object, *, capability: str, field_name: str) -> str:
    """Valida un nombre contra la lista cerrada; acepta alias, nunca URLs."""

    allowed = TEXT_PROVIDER_NAMES if capability == "text" else IMAGE_PROVIDER_NAMES
    canonical = _canonical_provider_name(value)
    if canonical is None or canonical not in allowed:
        raise ProviderConfigurationError(f"{field_name} debe ser uno de: {', '.join(allowed)}")
    return canonical


def normalize_any_provider_name(value: object, *, field_name: str = "provider") -> str:
    canonical = _canonical_provider_name(value)
    if canonical is None or canonical not in PROVIDER_NAMES:
        raise ProviderConfigurationError(
            f"{field_name} debe ser uno de: {', '.join(PROVIDER_NAMES)}"
        )
    return canonical


def normalize_model_name(provider: str, value: object, *, capability: str) -> str:
    """Valida un modelo contra la lista cerrada del proveedor indicado."""

    entry = PROVIDER_CATALOG[provider]
    allowed = entry.text_models if capability == "text" else entry.image_models
    label = "texto" if capability == "text" else "imagen"
    if not allowed:
        raise ProviderConfigurationError(f"{entry.label} no ofrece modelos de {label}")
    if not isinstance(value, str) or value.strip() not in allowed:
        raise ProviderConfigurationError(
            f"El modelo de {label} de {entry.label} debe ser uno de: {', '.join(allowed)}"
        )
    return value.strip()


def default_model(provider: str, *, capability: str) -> str:
    entry = PROVIDER_CATALOG[provider]
    if capability == "text":
        return entry.default_text_model
    model = entry.default_image_model
    if model is None:
        raise ProviderConfigurationError(f"{entry.label} no genera imágenes")
    return model


def api_key_configured(provider: str, environ: Mapping[str, str]) -> bool:
    """Informa presencia de la credencial sin leer ni propagar su valor."""

    entry = PROVIDER_CATALOG[provider]
    return bool(environ.get(entry.api_key_variable, "").strip())


def provider_credentials_status(environ: Mapping[str, str]) -> dict[str, bool]:
    """Mapa proveedor -> credencial presente en el entorno del proceso."""

    return {name: api_key_configured(name, environ) for name in PROVIDER_NAMES}
