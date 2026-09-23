from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from colmat_x.editorial import EditorialCategory, EditorialDraft, EditorialPolicy, Institution


class ProviderError(RuntimeError):
    """Error base de una solicitud a un proveedor de IA."""


class ProviderConfigurationError(ProviderError):
    """Falta una configuración local o contiene valores inseguros."""


class ProviderCapabilityError(ProviderConfigurationError):
    """El proveedor seleccionado no ofrece la capacidad solicitada."""


class ProviderTransportError(ProviderError):
    """La solicitud no obtuvo una respuesta HTTP concluyente."""


class ProviderAPIError(ProviderError):
    """El proveedor rechazó una solicitud con una respuesta explícita."""


class ProviderResponseError(ProviderError):
    """El proveedor respondió, pero el cuerpo no es seguro para consumir."""


@dataclass(frozen=True)
class ProviderErrorFamily:
    """Etiqueta y jerarquía de errores de un proveedor concreto.

    El código compartido formatea sus mensajes con `label` y levanta las clases
    del proveedor, de modo que `except MiniMaxError` sigue funcionando y una
    auditoría distingue qué proveedor produjo cada fallo.
    """

    label: str
    configuration: type[ProviderConfigurationError]
    transport: type[ProviderTransportError]
    api: type[ProviderAPIError]
    response: type[ProviderResponseError]


@dataclass(frozen=True)
class GeneratedImage:
    content: bytes = field(repr=False)
    mime_type: str
    sha256: str
    width: int
    height: int
    model: str
    request_id: str | None
    alt_text: str | None
    provider: str = "minimax"

    @property
    def data(self) -> bytes:
        return self.content

    @property
    def bytes(self) -> bytes:
        return self.content


@dataclass(frozen=True)
class CredentialCheck:
    """Resultado de comprobar una clave contra el proveedor; nunca contiene la clave."""

    status: str  # valid | invalid | error
    detail: str

    @property
    def is_valid(self) -> bool:
        return self.status == "valid"


CREDENTIAL_CHECK_STATUSES = ("valid", "invalid", "error")


@runtime_checkable
class TextProvider(Protocol):
    """Puerto de texto: propone borradores; nunca aprueba, programa ni publica."""

    provider_name: str
    model: str

    def generate_draft(
        self,
        brief: str,
        policy: EditorialPolicy,
        *,
        category: EditorialCategory | str | None = None,
        institution: Institution | str | None = None,
    ) -> EditorialDraft: ...


@runtime_checkable
class ImageProvider(Protocol):
    """Puerto de imagen: devuelve bytes en memoria bajo la política visual."""

    provider_name: str
    image_model: str

    def generate_image(
        self,
        prompt: str,
        policy: EditorialPolicy,
        *,
        aspect_ratio: str | None = None,
        alt_text: str | None = None,
    ) -> GeneratedImage: ...


def text_provenance(client: object) -> dict[str, str]:
    """Extrae proveedor y modelo de texto para la evidencia del borrador."""

    provider = getattr(client, "provider_name", None)
    model = getattr(client, "model", None)
    if not isinstance(provider, str) or not provider or not isinstance(model, str) or not model:
        raise TypeError("El cliente de texto debe declarar provider_name y model")
    return {"provider": provider, "model": model}


def image_provenance(client: object) -> dict[str, str] | None:
    """Extrae proveedor y modelo de imagen; None si el cliente no genera imágenes."""

    provider = getattr(client, "image_provider_name", None)
    if provider is None:
        provider = getattr(client, "provider_name", None)
    model = getattr(client, "image_model", None)
    if model is None:
        return None
    if not isinstance(provider, str) or not provider or not isinstance(model, str) or not model:
        raise TypeError("El cliente de imagen debe declarar provider_name e image_model")
    return {"provider": provider, "model": model}


def engine_evidence(client: object) -> dict[str, Any]:
    """Bloque `ai_engine` que la evidencia persiste para auditoría posterior."""

    return {"text": text_provenance(client), "image": image_provenance(client)}
