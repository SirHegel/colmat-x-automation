from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path
from urllib.parse import urlsplit

from yaml import YAMLError

from colmat_x.yaml_utils import load_yaml_unique

MAX_REGISTRY_BYTES = 512 * 1024
MAX_SELECTED_REFERENCES = 3
RESEARCH_ONLY_BRIEF_PREFIX = "[COLMAT:RESEARCH_ONLY:v1]"
_ALLOWED_SOURCE_HOSTS = frozenset(
    {
        "datos.bne.es",
        "fgbueno.es",
        "filosofia.org",
        "helicon.es",
        "nodulo.org",
        "www.fgbueno.es",
        "www.filosofia.org",
        "www.helicon.es",
        "www.nodulo.org",
    }
)
_STOPWORDS = frozenset(
    {
        "con",
        "contra",
        "del",
        "desde",
        "donde",
        "el",
        "ella",
        "en",
        "entre",
        "esta",
        "este",
        "filosofico",
        "hacia",
        "las",
        "los",
        "para",
        "por",
        "que",
        "sin",
        "sobre",
        "sus",
        "una",
        "uno",
        "unos",
        "y",
    }
)
_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")


class ResearchRegistryError(ValueError):
    """El registro bibliográfico no tiene el formato o la ubicación autorizados."""


@dataclass(frozen=True, slots=True)
class ResearchReference:
    id: str
    title: str
    year: int
    url: str
    disputed: bool = False
    status_url: str | None = None
    dispute_note: str | None = None


@dataclass(frozen=True, slots=True)
class ResearchRegistry:
    entries: tuple[ResearchReference, ...]
    canonical_urls: tuple[str, ...]

    def select(
        self,
        query: str,
        *,
        limit: int = MAX_SELECTED_REFERENCES,
    ) -> tuple[ResearchReference, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 3:
            raise ValueError("limit debe estar entre 1 y 3")
        query_tokens = _significant_tokens(query)
        if not query_tokens:
            return ()
        ranked: list[tuple[int, int, ResearchReference]] = []
        for ordinal, entry in enumerate(self.entries):
            title_tokens = _significant_tokens(entry.title)
            score, matches = _match_score(query_tokens, title_tokens)
            exact_title = bool(title_tokens) and title_tokens.issubset(query_tokens)
            if entry.disputed:
                # Una palabra genérica nunca convierte una atribución negada en fuente.
                # El año y al menos dos términos del título hacen explícito el caso.
                if str(entry.year) not in query_tokens or matches < 2:
                    continue
            elif matches < 2 and len(query_tokens) > 1 and not exact_title:
                continue
            if score:
                ranked.append((score + (8 if exact_title else 0), ordinal, entry))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return tuple(item[2] for item in ranked[:limit])


@lru_cache(maxsize=1)
def load_gustavo_bueno_registry() -> ResearchRegistry:
    try:
        content = _read_registry_content()
        raw = load_yaml_unique(content)
    except (OSError, UnicodeError, ValueError, YAMLError) as exc:
        if isinstance(exc, ResearchRegistryError):
            raise
        raise ResearchRegistryError("No se pudo leer el registro bibliográfico") from exc
    return _parse_registry(raw)


def _parse_registry(raw: object) -> ResearchRegistry:
    root = _mapping(raw, "registro")
    if root.get("version") != 1:
        raise ResearchRegistryError("La versión del registro bibliográfico no es compatible")
    metadata = _mapping(root.get("metadata"), "metadata")
    counts = _mapping(metadata.get("conteos"), "metadata.conteos")
    documented_count = counts.get("autoria_o_participacion_documentada")
    disputed_count = counts.get("atribuciones_disputadas")
    total_count = counts.get("titulos_o_series_total")
    raw_entries = _sequence(root.get("titulos_documentados"), "titulos_documentados")
    raw_disputed = _sequence(root.get("atribuciones_disputadas"), "atribuciones_disputadas")
    if (
        documented_count != 44
        or len(raw_entries) != documented_count
        or disputed_count != 1
        or len(raw_disputed) != disputed_count
        or total_count != documented_count + disputed_count
    ):
        raise ResearchRegistryError("El registro no contiene los 44 títulos y 1 disputa esperados")

    entries: list[ResearchReference] = []
    seen_ids: set[str] = set()
    for field_name, values, disputed in (
        ("titulos_documentados", raw_entries, False),
        ("atribuciones_disputadas", raw_disputed, True),
    ):
        for index, value in enumerate(values):
            item = _mapping(value, f"{field_name}[{index}]")
            identifier = _short_text(item.get("id"), f"{field_name}[{index}].id", 100)
            title = _short_text(item.get("titulo"), f"{field_name}[{index}].titulo", 300)
            year = item.get("anio")
            if isinstance(year, bool) or not isinstance(year, int) or not 1900 <= year <= 2100:
                raise ResearchRegistryError(f"{field_name}[{index}].anio no es válido")
            url = _canonical_url(item.get("url"), f"{field_name}[{index}].url")
            if identifier in seen_ids:
                raise ResearchRegistryError(f"ID bibliográfico duplicado: {identifier}")
            seen_ids.add(identifier)
            status_url = None
            dispute_note = None
            if disputed:
                status_url = _canonical_url(
                    item.get("fuente_estatuto"),
                    f"{field_name}[{index}].fuente_estatuto",
                )
                dispute_note = _short_text(
                    item.get("nota"),
                    f"{field_name}[{index}].nota",
                    600,
                )
            entries.append(
                ResearchReference(
                    identifier,
                    title,
                    year,
                    url,
                    disputed,
                    status_url,
                    dispute_note,
                )
            )

    complete_works = _mapping(root.get("obras_completas"), "obras_completas")
    complete_metadata = _mapping(complete_works.get("metadata"), "obras_completas.metadata")
    complete_volumes = _sequence(complete_works.get("tomos"), "obras_completas.tomos")
    if complete_metadata.get("total_tomos_publicados") != 9 or len(complete_volumes) != 9:
        raise ResearchRegistryError("Obras Completas debe conservar sus 9 tomos separados")

    raw_sources = _sequence(metadata.get("fuentes_canonicas"), "metadata.fuentes_canonicas")
    canonical_urls = tuple(
        _canonical_url(
            _mapping(source, f"metadata.fuentes_canonicas[{index}]").get("url"),
            f"metadata.fuentes_canonicas[{index}].url",
        )
        for index, source in enumerate(raw_sources)
    )
    if not canonical_urls or len(set(canonical_urls)) != len(canonical_urls):
        raise ResearchRegistryError("Las fuentes maestras del registro no son válidas")
    return ResearchRegistry(tuple(entries), canonical_urls)


def _read_registry_content() -> str:
    project_root = Path(__file__).resolve().parents[2]
    requested = _checkout_registry_path()
    if requested.exists():
        registry_path = requested.resolve(strict=True)
        if not registry_path.is_relative_to(project_root):
            raise ResearchRegistryError("El registro bibliográfico sale de la raíz confiable")
        payload = registry_path.read_bytes()
    else:
        try:
            payload = (
                resources.files("colmat_x")
                .joinpath("data", "gustavo-bueno-books.yaml")
                .read_bytes()
            )
        except (FileNotFoundError, ModuleNotFoundError, OSError) as exc:
            raise ResearchRegistryError(
                "No está disponible el registro bibliográfico canónico"
            ) from exc
    if not 1 <= len(payload) <= MAX_REGISTRY_BYTES:
        raise ResearchRegistryError("El registro bibliográfico tiene un tamaño inválido")
    return payload.decode("utf-8")


def _checkout_registry_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "gustavo-bueno-books.yaml"


def _significant_tokens(value: str) -> frozenset[str]:
    if not isinstance(value, str):
        return frozenset()
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    ascii_text = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    return frozenset(
        token
        for token in _TOKEN_PATTERN.findall(ascii_text)
        if len(token) >= 3 and token not in _STOPWORDS
    )


def _match_score(
    query_tokens: frozenset[str],
    title_tokens: frozenset[str],
) -> tuple[int, int]:
    score = 0
    matches = 0
    for title_token in title_tokens:
        if title_token in query_tokens:
            score += 4
            matches += 1
            continue
        if len(title_token) < 8:
            continue
        if any(
            len(query_token) >= 8 and query_token[:8] == title_token[:8]
            for query_token in query_tokens
        ):
            score += 2
            matches += 1
    return score, matches


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ResearchRegistryError(f"{field_name} debe ser un objeto")
    return value


def _sequence(value: object, field_name: str) -> Sequence[object]:
    if not isinstance(value, list):
        raise ResearchRegistryError(f"{field_name} debe ser una lista")
    return value


def _short_text(value: object, field_name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ResearchRegistryError(f"{field_name} debe ser texto")
    normalized = " ".join(value.split())
    if (
        not normalized
        or len(normalized) > maximum
        or any(ord(character) < 32 for character in normalized)
    ):
        raise ResearchRegistryError(f"{field_name} no es válido")
    return normalized


def _canonical_url(value: object, field_name: str) -> str:
    normalized = _short_text(value, field_name, 500)
    parsed = urlsplit(normalized)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _ALLOWED_SOURCE_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or not parsed.path
    ):
        raise ResearchRegistryError(f"{field_name} no es una URL canónica autorizada")
    return normalized
