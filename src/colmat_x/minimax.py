"""Compatibilidad: el adaptador MiniMax vive ahora en `colmat_x.providers.minimax`.

Este módulo conserva los nombres públicos históricos para que el código y las
pruebas existentes sigan importando desde `colmat_x.minimax`.
"""

from __future__ import annotations

from colmat_x.providers._shared import (
    MAX_ALT_TEXT_CHARACTERS,
    MAX_DRAFT_VALIDATION_ATTEMPTS,
    MAX_IMAGE_BYTES,
    MAX_IMAGE_PROMPT_CHARACTERS,
)
from colmat_x.providers.base import GeneratedImage
from colmat_x.providers.minimax import (
    DEFAULT_MINIMAX_API_STYLE,
    DEFAULT_MINIMAX_IMAGE_MODEL,
    DEFAULT_MINIMAX_MODEL,
    IMAGE_ASPECT_RATIO_DIMENSIONS,
    MINIMAX_ANTHROPIC_MESSAGES_ENDPOINT,
    MINIMAX_ANTHROPIC_VERSION,
    MINIMAX_CHAT_COMPLETIONS_ENDPOINT,
    MINIMAX_IMAGE_GENERATION_ENDPOINT,
    MiniMaxAPIError,
    MiniMaxClient,
    MiniMaxConfigurationError,
    MiniMaxError,
    MiniMaxResponseError,
    MiniMaxTransportError,
    validate_alt_text,
)

__all__ = [
    "DEFAULT_MINIMAX_API_STYLE",
    "DEFAULT_MINIMAX_IMAGE_MODEL",
    "DEFAULT_MINIMAX_MODEL",
    "GeneratedImage",
    "IMAGE_ASPECT_RATIO_DIMENSIONS",
    "MAX_ALT_TEXT_CHARACTERS",
    "MAX_DRAFT_VALIDATION_ATTEMPTS",
    "MAX_IMAGE_BYTES",
    "MAX_IMAGE_PROMPT_CHARACTERS",
    "MINIMAX_ANTHROPIC_MESSAGES_ENDPOINT",
    "MINIMAX_ANTHROPIC_VERSION",
    "MINIMAX_CHAT_COMPLETIONS_ENDPOINT",
    "MINIMAX_IMAGE_GENERATION_ENDPOINT",
    "MiniMaxAPIError",
    "MiniMaxClient",
    "MiniMaxConfigurationError",
    "MiniMaxError",
    "MiniMaxResponseError",
    "MiniMaxTransportError",
    "validate_alt_text",
]
