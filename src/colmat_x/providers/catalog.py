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


# Los modelos listados provienen de la documentación oficial vigente al escribir
# este catálogo. Ampliarlo es un cambio de código revisado, no una variable.
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
            text_models=("gpt-5", "gpt-5-mini", "gpt-4.1"),
            image_models=("gpt-image-1",),
        ),
        PROVIDER_GEMINI: ProviderCatalogEntry(
            name=PROVIDER_GEMINI,
            label="Google Gemini",
            api_key_variable="GEMINI_API_KEY",
            env_file_variable="COLMAT_GEMINI_ENV_FILE",
            text_models=("gemini-2.5-pro", "gemini-2.5-flash"),
            image_models=("gemini-2.5-flash-image",),
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


def normalize_provider_name(value: object, *, capability: str, field_name: str) -> str:
    """Valida un nombre contra la lista cerrada; nunca acepta URLs ni alias."""

    allowed = TEXT_PROVIDER_NAMES if capability == "text" else IMAGE_PROVIDER_NAMES
    if not isinstance(value, str) or value.strip().casefold() not in allowed:
        raise ProviderConfigurationError(f"{field_name} debe ser uno de: {', '.join(allowed)}")
    return value.strip().casefold()


def normalize_any_provider_name(value: object, *, field_name: str = "provider") -> str:
    if not isinstance(value, str) or value.strip().casefold() not in PROVIDER_NAMES:
        raise ProviderConfigurationError(
            f"{field_name} debe ser uno de: {', '.join(PROVIDER_NAMES)}"
        )
    return value.strip().casefold()


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
