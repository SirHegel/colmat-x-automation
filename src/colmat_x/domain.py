from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TypeGuard

DOMAIN_CHARACTER = r"[^\W_]"
DOMAIN_LETTER = r"[^\W\d_]"
DOMAIN_LABEL = rf"{DOMAIN_CHARACTER}(?:(?:{DOMAIN_CHARACTER}|-){{0,61}}{DOMAIN_CHARACTER})?"
DOMAIN_SUFFIX = (
    rf"(?:xn--[a-z0-9-]{{2,59}}|{DOMAIN_LETTER}"
    rf"(?:(?:{DOMAIN_LETTER}|-){{0,61}}{DOMAIN_LETTER})?)"
)
URL_PATTERN = re.compile(
    rf"(?:(?:https?://|www\.)[^\s]+|(?<![@\w])(?:{DOMAIN_LABEL}\.)+"
    rf"{DOMAIN_SUFFIX}(?::\d+)?[^\s]*)",
    re.IGNORECASE,
)
URL_CORE_PATTERN = re.compile(
    rf"^(?:https?://|www\.)?(?:{DOMAIN_LABEL}\.)+{DOMAIN_SUFFIX}(?::\d+)?",
    re.IGNORECASE,
)
CASHTAG_PATTERN = re.compile(r"(?<!\w)\$[A-Za-z][A-Za-z0-9_]*")
POST_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{2,79}$")
X_POST_ID_PATTERN = re.compile(r"^[1-9][0-9]{0,19}$")


class ContentError(ValueError):
    """Una pieza de contenido no cumple las reglas editoriales."""


class PostStatus(StrEnum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class RenderedPost:
    id: str
    template: str
    publish_at: datetime
    text: str
    content_hash: str
    source_path: Path

    @property
    def publish_at_utc(self) -> datetime:
        return self.publish_at.astimezone(UTC)

    @property
    def approval_snapshot_hash(self) -> str:
        scheduled = self.publish_at_utc.isoformat(timespec="seconds").replace("+00:00", "Z")
        return approval_hash_from_values(self.id, scheduled, self.text, self.content_hash)


def parse_publish_at(value: object, *, field_name: str = "publish_at") -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ContentError(f"'{field_name}' debe ser una fecha ISO 8601 con zona horaria")
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ContentError(f"'{field_name}' no es una fecha ISO 8601 válida: {value}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContentError(f"'{field_name}' debe incluir zona horaria, por ejemplo -05:00")
    try:
        parsed_utc = parsed.astimezone(UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise ContentError(f"'{field_name}' queda fuera del rango de fechas admitido") from exc
    if not 2000 <= parsed_utc.year <= 9998:
        raise ContentError(f"'{field_name}' debe quedar entre los años 2000 y 9998 en UTC")
    return parsed


def validate_post_id(value: object) -> str:
    if not isinstance(value, str) or not POST_ID_PATTERN.fullmatch(value):
        raise ContentError(
            "'id' debe tener 3-80 caracteres: minúsculas, números, guion o guion bajo"
        )
    return value


def is_canonical_x_post_id(value: object) -> TypeGuard[str]:
    """Acepta únicamente el formato decimal canónico de un ID de publicación de X."""
    return isinstance(value, str) and X_POST_ID_PATTERN.fullmatch(value) is not None


def normalized_content_hash(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    normalized = " ".join(normalized.casefold().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def approval_hash_from_values(
    post_id: str, scheduled_at_utc: str, text: str, content_hash: str
) -> str:
    payload = "\0".join((post_id, scheduled_at_utc, text, content_hash))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def weighted_length(text: str) -> int:
    """Aproxima twitter-text v3: las URL pesan 23 y ciertos caracteres pesan 2."""
    text = unicodedata.normalize("NFC", text)
    total = 0
    cursor = 0
    for match in URL_PATTERN.finditer(text):
        total += _weighted_plain_text(text[cursor : match.start()])
        matched_url = match.group(0)
        core = URL_CORE_PATTERN.match(matched_url)
        # Solo el dominio inequívoco recibe peso 23. El resto se suma también: esto puede
        # sobrecontar rutas válidas, pero evita aceptar por error texto fuera del enlace.
        trailing = matched_url[core.end() :] if core else matched_url
        total += 23
        total += _weighted_plain_text(trailing)
        cursor = match.end()
    return total + _weighted_plain_text(text[cursor:])


def validate_rendered_text(
    text: str,
    *,
    max_weighted_length: int,
    allow_urls: bool,
) -> None:
    if not text.strip():
        raise ContentError("La plantilla produjo una publicación vacía")
    if "\x00" in text:
        raise ContentError("La publicación contiene un carácter nulo")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in text):
        raise ContentError("La publicación contiene Unicode no válido (surrogate)")
    normalized_text = unicodedata.normalize("NFC", text)
    if not allow_urls and URL_PATTERN.search(normalized_text):
        raise ContentError(
            "La publicación contiene una URL, pero safety.allow_urls está desactivado"
        )
    cashtags = CASHTAG_PATTERN.findall(normalized_text)
    if len(cashtags) > 1:
        raise ContentError("X permite como máximo un cashtag por publicación")
    length = weighted_length(normalized_text)
    if length > max_weighted_length:
        raise ContentError(
            f"La publicación pesa {length} caracteres; el máximo es {max_weighted_length}"
        )


def _weighted_plain_text(text: str) -> int:
    return sum(_character_weight(character) for character in text)


def _character_weight(character: str) -> int:
    codepoint = ord(character)
    single_weight_ranges = (
        (0x0000, 0x10FF),
        (0x2000, 0x200D),
        (0x2010, 0x201F),
        (0x2032, 0x2037),
    )
    return 1 if any(start <= codepoint <= end for start, end in single_weight_ranges) else 2
