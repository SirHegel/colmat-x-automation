"""Corpus local de textos de Gustavo Bueno para derivar una guía de estilo.

El corpus se descarga únicamente desde los sitios de la Fundación Gustavo Bueno y de su
escuela (lista blanca de ``research_fetch``), respetando ``robots.txt`` y su ``Crawl-delay``,
con un ``User-Agent`` identificable y a ritmo cortés. Los textos quedan fuera de Git; lo que
se versiona es el manifiesto con la procedencia (URL, título, año, fecha de descarga y
SHA-256 del cuerpo recibido y del texto extraído) para que cualquier rasgo de estilo que se
afirme sea trazable.
"""

from __future__ import annotations

import hashlib
import re
import time
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import yaml

from colmat_x.research_fetch import (
    FetchedResearchDocument,
    ResearchFetcher,
    ResearchFetchError,
    ResearchUrlError,
    decode_document_body,
    validate_research_url,
)
from colmat_x.yaml_utils import load_yaml_unique

DEFAULT_SOURCES_PATH = Path("config/gustavo-bueno-corpus-sources.yaml")
DEFAULT_MANIFEST_PATH = Path("config/gustavo-bueno-corpus-manifest.yaml")
DEFAULT_CORPUS_DIR = Path(".state/corpus/gustavo-bueno")
CORPUS_USER_AGENT = (
    "colmat-x-automation/0.2 (+https://github.com/SirHegel/colmat-x-automation; "
    "corpus de estilo; uso docente y de investigacion)"
)
MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
MAX_INDEX_ITEMS = 1_000
MIN_DOCUMENT_CHARACTERS = 400
MIN_BLOCK_CHARACTERS = 80
DEFAULT_MIN_DELAY_SECONDS = 1.5
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
ROBOTS_USER_AGENT = "colmat-x-automation"

_EXCLUDED_TAGS = frozenset(
    {"script", "style", "nav", "aside", "object", "iframe", "head", "title", "sup", "noscript"}
)
_EXCLUDED_CLASS_TOKENS = frozenset(
    {"nov", "noi", "sm", "nt", "ad", "pie", "rec", "ytb", "avisos", "ac", "enc"}
)
_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "dt",
        "dd",
        "blockquote",
        "tr",
        "td",
        "th",
        "section",
        "article",
        "header",
        "footer",
        "table",
        "br",
        "hr",
        "pre",
    }
)
_GLOBAL_BOILERPLATE_PATTERNS = (
    r"^separata de la revista",
    r"^el catoblepas\s*[·•]",
    r"^©",
    r"^\(c\)",
    r"^filosof[ií]a en espa[ñn]ol\b",
    r"^proyecto filosof[ií]a en espa[ñn]ol",
    r"^fundaci[oó]n gustavo bueno\b",
    r"^hemeroteca\b",
    r"^bibliograf[ií]a cronol[oó]gica",
    r"^sumario\b",
    r"^[ií]ndice\b",
    r"^gustavo bueno$",
    r"document\.write",
    r"^issn\b",
    r"^tesela n",
    r"^p[aá]gina\s+\d",
    r"^p[aá]ginas\s+\d",
)
_PAGE_MARKER_PATTERN = re.compile(r"\s*\[\d{1,4}\]\s*")
_FOOTNOTE_MARKER_PATTERN = re.compile(r"\{\d{1,3}\}")
_SENTENCE_END_CHARACTERS = ".!?»”)…:"
_YEAR_PATTERN = re.compile(r"(?<!\d)(1[89]\d{2}|20\d{2})(?!\d)")
_WORD_PATTERN = re.compile(r"[^\W\d_]+(?:[-'’][^\W\d_]+)*", re.UNICODE)
_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


class CorpusError(ValueError):
    """El corpus de estilo no cumple su contrato de procedencia o de uso."""


class CorpusSourcesError(CorpusError):
    """La lista de fuentes del corpus no es válida."""


class CorpusManifestError(CorpusError):
    """El manifiesto del corpus no es válido o no coincide con los archivos locales."""


class CorpusRobotsError(CorpusError):
    """El sitio prohíbe recuperar esa ruta; nunca se descarga."""


@dataclass(frozen=True, slots=True)
class UseConditions:
    host: str
    robots_url: str
    aviso_url: str | None
    resumen: str
    verificado: str


@dataclass(frozen=True, slots=True)
class TableIndexRule:
    author_column: int | None
    author_value: str | None
    date_column: int | None


@dataclass(frozen=True, slots=True)
class SectionIndexRule:
    start: str
    end: str


@dataclass(frozen=True, slots=True)
class CorpusSource:
    id: str
    nombre: str
    index_url: str
    link_pattern: re.Pattern[str]
    table_rule: TableIndexRule | None
    section_rule: SectionIndexRule | None
    min_delay_seconds: float
    boilerplate_patterns: tuple[str, ...]

    @property
    def host(self) -> str:
        return urlsplit(self.index_url).hostname or ""


@dataclass(frozen=True, slots=True)
class CorpusSources:
    version: int
    finalidad: str
    conditions: tuple[UseConditions, ...]
    sources: tuple[CorpusSource, ...]
    sha256: str

    def source(self, source_id: str) -> CorpusSource:
        for item in self.sources:
            if item.id == source_id:
                return item
        raise CorpusSourcesError(f"Fuente desconocida: {source_id}")


@dataclass(frozen=True, slots=True)
class IndexEntry:
    url: str
    title: str
    year: int | None


@dataclass(frozen=True, slots=True)
class ExtractedText:
    title: str
    paragraphs: tuple[str, ...]

    @property
    def text(self) -> str:
        return "\n\n".join(self.paragraphs)


@dataclass(frozen=True, slots=True)
class CorpusItem:
    id: str
    source: str
    url: str
    title: str
    year: int | None
    fetched_at: str
    body_sha256: str
    text_sha256: str
    characters: int
    words: int
    paragraphs: int
    path: str

    def to_mapping(self) -> dict[str, object]:
        return {
            "id": self.id,
            "source": self.source,
            "url": self.url,
            "title": self.title,
            "year": self.year,
            "fetched_at": self.fetched_at,
            "body_sha256": self.body_sha256,
            "text_sha256": self.text_sha256,
            "characters": self.characters,
            "words": self.words,
            "paragraphs": self.paragraphs,
            "path": self.path,
        }


@dataclass(frozen=True, slots=True)
class RobotsRecord:
    host: str
    fetched_at: str
    sha256: str | None
    crawl_delay_seconds: float | None
    applied_delay_seconds: float


@dataclass(frozen=True, slots=True)
class CorpusManifest:
    version: int
    generated_at: str
    user_agent: str
    sources_sha256: str
    robots: tuple[RobotsRecord, ...]
    items: tuple[CorpusItem, ...]
    skipped: tuple[Mapping[str, object], ...]

    @property
    def total_words(self) -> int:
        return sum(item.words for item in self.items)

    @property
    def total_characters(self) -> int:
        return sum(item.characters for item in self.items)

    def to_mapping(self) -> dict[str, object]:
        by_source: dict[str, dict[str, int]] = {}
        for item in self.items:
            bucket = by_source.setdefault(
                item.source, {"documentos": 0, "palabras": 0, "caracteres": 0}
            )
            bucket["documentos"] += 1
            bucket["palabras"] += item.words
            bucket["caracteres"] += item.characters
        return {
            "version": self.version,
            "generated_at": self.generated_at,
            "user_agent": self.user_agent,
            "sources_sha256": self.sources_sha256,
            "totales": {
                "documentos": len(self.items),
                "palabras": self.total_words,
                "caracteres": self.total_characters,
                "por_fuente": by_source,
            },
            "robots": [
                {
                    "host": record.host,
                    "fetched_at": record.fetched_at,
                    "sha256": record.sha256,
                    "crawl_delay_seconds": record.crawl_delay_seconds,
                    "applied_delay_seconds": record.applied_delay_seconds,
                }
                for record in self.robots
            ],
            "omitidos": [dict(entry) for entry in self.skipped],
            "items": [item.to_mapping() for item in self.items],
        }


# --------------------------------------------------------------------------------------
# Fuentes
# --------------------------------------------------------------------------------------


def load_corpus_sources(path: str | Path = DEFAULT_SOURCES_PATH) -> CorpusSources:
    sources_path = Path(path)
    try:
        payload = sources_path.read_bytes()
        raw = load_yaml_unique(payload.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
        raise CorpusSourcesError(f"No se pudo leer la lista de fuentes: {sources_path}") from exc
    return parse_corpus_sources(raw, sha256=hashlib.sha256(payload).hexdigest())


def parse_corpus_sources(raw: object, *, sha256: str = "") -> CorpusSources:
    root = _strict_mapping(raw, {"version", "metadata", "sources"}, "raíz", CorpusSourcesError)
    if root["version"] != 1:
        raise CorpusSourcesError("La única versión de fuentes admitida es 1")
    metadata = _strict_mapping(
        root["metadata"],
        {"nombre", "finalidad", "condiciones_de_uso"},
        "metadata",
        CorpusSourcesError,
    )
    _nonempty_text(metadata["nombre"], "metadata.nombre", CorpusSourcesError)
    finalidad = _nonempty_text(metadata["finalidad"], "metadata.finalidad", CorpusSourcesError)
    conditions = tuple(
        _parse_conditions(item, f"metadata.condiciones_de_uso[{index}]")
        for index, item in enumerate(
            _sequence(metadata["condiciones_de_uso"], "metadata.condiciones_de_uso")
        )
    )
    hosts_with_conditions = {item.host for item in conditions}
    if len(hosts_with_conditions) != len(conditions):
        raise CorpusSourcesError("Las condiciones de uso repiten un host")

    sources: list[CorpusSource] = []
    seen: set[str] = set()
    for index, item in enumerate(_sequence(root["sources"], "sources")):
        source = _parse_source(item, f"sources[{index}]")
        if source.id in seen:
            raise CorpusSourcesError(f"Fuente duplicada: {source.id}")
        if source.host not in hosts_with_conditions:
            raise CorpusSourcesError(
                f"sources[{index}] usa el host {source.host} sin condiciones de uso declaradas"
            )
        seen.add(source.id)
        sources.append(source)
    if not sources:
        raise CorpusSourcesError("La lista de fuentes está vacía")
    return CorpusSources(
        version=1,
        finalidad=finalidad,
        conditions=conditions,
        sources=tuple(sources),
        sha256=sha256,
    )


def _parse_conditions(value: object, location: str) -> UseConditions:
    data = _strict_mapping(
        value,
        {"host", "robots_url", "aviso_url", "resumen", "verificado"},
        location,
        CorpusSourcesError,
    )
    host = _nonempty_text(data["host"], f"{location}.host", CorpusSourcesError)
    robots_url = _allowed_https_url(data["robots_url"], f"{location}.robots_url")
    if urlsplit(robots_url).hostname != host or not robots_url.endswith("/robots.txt"):
        raise CorpusSourcesError(f"{location}.robots_url debe ser el robots.txt de {host}")
    aviso_url = data["aviso_url"]
    if aviso_url is not None:
        aviso_url = _allowed_https_url(aviso_url, f"{location}.aviso_url")
    verificado = _nonempty_text(data["verificado"], f"{location}.verificado", CorpusSourcesError)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", verificado) is None:
        raise CorpusSourcesError(f"{location}.verificado debe ser una fecha AAAA-MM-DD")
    return UseConditions(
        host=host,
        robots_url=robots_url,
        aviso_url=aviso_url,
        resumen=_nonempty_text(data["resumen"], f"{location}.resumen", CorpusSourcesError),
        verificado=verificado,
    )


def _parse_source(value: object, location: str) -> CorpusSource:
    data = _strict_mapping(
        value,
        {
            "id",
            "nombre",
            "index_url",
            "link_pattern",
            "index_format",
            "tabla",
            "seccion",
            "min_delay_seconds",
            "boilerplate_patterns",
        },
        location,
        CorpusSourcesError,
    )
    identifier = _nonempty_text(data["id"], f"{location}.id", CorpusSourcesError)
    if _SOURCE_ID_PATTERN.fullmatch(identifier) is None:
        raise CorpusSourcesError(f"{location}.id debe ser un identificador en minúsculas")
    index_url = _allowed_https_url(data["index_url"], f"{location}.index_url")
    pattern_text = _nonempty_text(
        data["link_pattern"], f"{location}.link_pattern", CorpusSourcesError
    )
    try:
        link_pattern = re.compile(pattern_text)
    except re.error as exc:
        raise CorpusSourcesError(f"{location}.link_pattern no es una expresión válida") from exc
    if not pattern_text.startswith("^https://"):
        raise CorpusSourcesError(f"{location}.link_pattern debe anclarse a ^https://")
    index_format = data["index_format"]
    table_rule: TableIndexRule | None = None
    section_rule: SectionIndexRule | None = None
    if index_format == "tabla":
        table = _strict_mapping(
            data["tabla"],
            {"columna_autor", "autor", "columna_fecha"},
            f"{location}.tabla",
            CorpusSourcesError,
        )
        if data["seccion"] is not None:
            raise CorpusSourcesError(f"{location}.seccion debe ser null en formato tabla")
        author_column = _optional_index(table["columna_autor"], f"{location}.tabla.columna_autor")
        author_value = table["autor"]
        if (author_column is None) != (author_value is None):
            raise CorpusSourcesError(f"{location}.tabla exige columna_autor y autor juntos")
        if author_value is not None:
            author_value = _nonempty_text(
                author_value, f"{location}.tabla.autor", CorpusSourcesError
            )
        table_rule = TableIndexRule(
            author_column=author_column,
            author_value=author_value,
            date_column=_optional_index(table["columna_fecha"], f"{location}.tabla.columna_fecha"),
        )
    elif index_format == "seccion":
        section = _strict_mapping(
            data["seccion"], {"inicio", "fin"}, f"{location}.seccion", CorpusSourcesError
        )
        if data["tabla"] is not None:
            raise CorpusSourcesError(f"{location}.tabla debe ser null en formato seccion")
        section_rule = SectionIndexRule(
            start=_nonempty_text(
                section["inicio"], f"{location}.seccion.inicio", CorpusSourcesError
            ),
            end=_nonempty_text(section["fin"], f"{location}.seccion.fin", CorpusSourcesError),
        )
    else:
        raise CorpusSourcesError(f"{location}.index_format debe ser 'tabla' o 'seccion'")
    delay = data["min_delay_seconds"]
    if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not 1 <= delay <= 3600:
        raise CorpusSourcesError(f"{location}.min_delay_seconds debe estar entre 1 y 3600")
    boilerplate = data["boilerplate_patterns"]
    if not isinstance(boilerplate, list):
        raise CorpusSourcesError(f"{location}.boilerplate_patterns debe ser una lista")
    patterns: list[str] = []
    for index, item in enumerate(boilerplate):
        text = _nonempty_text(item, f"{location}.boilerplate_patterns[{index}]", CorpusSourcesError)
        try:
            re.compile(text)
        except re.error as exc:
            raise CorpusSourcesError(
                f"{location}.boilerplate_patterns[{index}] no es una expresión válida"
            ) from exc
        patterns.append(text)
    return CorpusSource(
        id=identifier,
        nombre=_nonempty_text(data["nombre"], f"{location}.nombre", CorpusSourcesError),
        index_url=index_url,
        link_pattern=link_pattern,
        table_rule=table_rule,
        section_rule=section_rule,
        min_delay_seconds=float(delay),
        boilerplate_patterns=tuple(patterns),
    )


# --------------------------------------------------------------------------------------
# Extracción de texto
# --------------------------------------------------------------------------------------


_VOID_TAGS = frozenset({"br", "hr", "img", "meta", "link", "input", "wbr", "source"})
# Elementos que el HTML antiguo deja sin cerrar; un nuevo bloque los cierra implícitamente.
_IMPLICITLY_CLOSED_BY: dict[str, frozenset[str]] = {
    "p": frozenset({"p"}),
    "li": frozenset({"li"}),
    "dt": frozenset({"dt", "dd"}),
    "dd": frozenset({"dt", "dd"}),
    "td": frozenset({"td", "th"}),
    "th": frozenset({"td", "th"}),
    "tr": frozenset({"td", "th", "tr"}),
}


class _BlockTextParser(HTMLParser):
    """Convierte HTML en bloques de texto, omitiendo navegación, avisos y notas."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._excluded_depth = 0
        self._stack: list[tuple[str, bool]] = []
        self._current: list[str] = []
        self.blocks: list[str] = []
        self.title_parts: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag == "title":
            self._in_title = True
            return
        if tag in _VOID_TAGS:
            if tag in {"br", "hr"}:
                self._flush()
            return
        class_tokens: set[str] = set()
        identifier = ""
        for name, value in attrs:
            if name == "class" and value:
                class_tokens.update(value.casefold().split())
            elif name == "id" and value:
                identifier = value.casefold()
        excluded = (
            tag in _EXCLUDED_TAGS
            or bool(class_tokens & _EXCLUDED_CLASS_TOKENS)
            or identifier in _EXCLUDED_CLASS_TOKENS
        )
        if tag in _BLOCK_TAGS:
            self._flush()
            closes = _IMPLICITLY_CLOSED_BY.get(tag, frozenset({"p"}))
            while self._stack and self._stack[-1][0] in closes:
                self._pop()
        self._stack.append((tag, excluded))
        if excluded:
            self._excluded_depth += 1

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag == "title":
            self._in_title = False
            return
        if tag in _VOID_TAGS:
            return
        if tag in _BLOCK_TAGS:
            self._flush()
        if any(open_tag == tag for open_tag, _ in self._stack):
            while self._stack:
                open_tag, _ = self._stack[-1]
                self._pop()
                if open_tag == tag:
                    break

    def _pop(self) -> None:
        _, excluded = self._stack.pop()
        if excluded and self._excluded_depth:
            self._excluded_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
            return
        if not self._excluded_depth:
            self._current.append(data)

    def close(self) -> None:
        super().close()
        self._flush()

    def _flush(self) -> None:
        if self._current:
            text = " ".join("".join(self._current).split())
            if text:
                self.blocks.append(text)
            self._current = []


def extract_document_text(
    document: FetchedResearchDocument,
    *,
    boilerplate_patterns: Sequence[str] = (),
) -> ExtractedText:
    """Extrae título y párrafos de prosa de un documento HTML o de texto plano."""

    decoded = decode_document_body(document.body, charset=document.charset)
    if document.mime_type != "text/html":
        blocks = [" ".join(part.split()) for part in re.split(r"\n\s*\n", decoded)]
        title = ""
    else:
        parser = _BlockTextParser()
        parser.feed(decoded)
        parser.close()
        blocks = parser.blocks
        title = " ".join("".join(parser.title_parts).split())
    patterns = [
        re.compile(pattern, re.IGNORECASE)
        for pattern in (*_GLOBAL_BOILERPLATE_PATTERNS, *boilerplate_patterns)
    ]
    paragraphs: list[str] = []
    for block in blocks:
        cleaned = _clean_block(block)
        if not cleaned:
            continue
        if any(pattern.search(cleaned) for pattern in patterns):
            continue
        if len(cleaned) < MIN_BLOCK_CHARACTERS and cleaned[-1] not in _SENTENCE_END_CHARACTERS:
            continue
        paragraphs.append(cleaned)
    return ExtractedText(title=title, paragraphs=tuple(paragraphs))


def _clean_block(block: str) -> str:
    text = _PAGE_MARKER_PATTERN.sub(" ", block)
    text = _FOOTNOTE_MARKER_PATTERN.sub("", text)
    text = unicodedata.normalize("NFC", text)
    return " ".join(text.split())


# --------------------------------------------------------------------------------------
# Índices
# --------------------------------------------------------------------------------------


class _TableIndexParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[tuple[str, str | None]]] = []
        self._row: list[tuple[str, str | None]] | None = None
        self._cell: list[str] | None = None
        self._href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []
            self._href = None
        elif tag == "a" and self._cell is not None and self._href is None:
            for name, value in attrs:
                if name == "href" and value:
                    self._href = value.strip()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append((" ".join("".join(self._cell).split()), self._href))
            self._cell = None
            self._href = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


class _SectionLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text = ""
        self.links: list[tuple[int, str, str]] = []
        self._href: str | None = None
        self._link_text: list[str] = []
        self._link_start = 0
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag in {"script", "style"}:
            self._skip += 1
        elif tag == "a":
            for name, value in attrs:
                if name == "href" and value:
                    self._href = value.strip()
                    self._link_text = []
                    self._link_start = len(self.text)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in {"script", "style"} and self._skip:
            self._skip -= 1
        elif tag == "a" and self._href is not None:
            self.links.append(
                (self._link_start, self._href, " ".join("".join(self._link_text).split()))
            )
            self._href = None

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        self.text += data
        if self._href is not None:
            self._link_text.append(data)


def parse_index(document: FetchedResearchDocument, source: CorpusSource) -> tuple[IndexEntry, ...]:
    """Obtiene las entradas de un índice de autor según la regla declarada de la fuente."""

    decoded = decode_document_body(document.body, charset=document.charset)
    entries: list[IndexEntry] = []
    if source.table_rule is not None:
        parser = _TableIndexParser()
        parser.feed(decoded)
        parser.close()
        rule = source.table_rule
        for row in parser.rows:
            if rule.author_column is not None:
                if len(row) <= rule.author_column:
                    continue
                if row[rule.author_column][0] != rule.author_value:
                    continue
            link = next(((text, href) for text, href in row if href), None)
            if link is None:
                continue
            year = None
            if rule.date_column is not None and len(row) > rule.date_column:
                year = _year_from_text(row[rule.date_column][0])
            entries.append(IndexEntry(url=link[1], title=link[0], year=year))
    else:
        assert source.section_rule is not None
        parser = _SectionLinkParser()
        parser.feed(decoded)
        parser.close()
        start = parser.text.find(source.section_rule.start)
        if start < 0:
            raise CorpusSourcesError(
                f"El índice de {source.id} no contiene la sección {source.section_rule.start!r}"
            )
        end = parser.text.find(source.section_rule.end, start + len(source.section_rule.start))
        if end < 0:
            end = len(parser.text)
        for position, href, text in parser.links:
            if start <= position < end:
                entries.append(IndexEntry(url=href, title=text, year=None))

    selected: list[IndexEntry] = []
    seen: set[str] = set()
    for entry in entries:
        absolute = urljoin(document.url, entry.url).split("#", 1)[0]
        if source.link_pattern.fullmatch(absolute) is None or absolute in seen:
            continue
        seen.add(absolute)
        year = entry.year if entry.year is not None else _year_from_text(urlsplit(absolute).path)
        selected.append(IndexEntry(url=absolute, title=entry.title or absolute, year=year))
        if len(selected) >= MAX_INDEX_ITEMS:
            break
    return tuple(selected)


def _year_from_text(value: str) -> int | None:
    match = _YEAR_PATTERN.search(value or "")
    return int(match.group(1)) if match else None


# --------------------------------------------------------------------------------------
# Descarga cortés
# --------------------------------------------------------------------------------------


class PoliteFetcher:
    """Descarga documentos de la lista blanca respetando robots.txt y un retardo por host."""

    def __init__(
        self,
        *,
        fetcher: ResearchFetcher | None = None,
        default_delay_seconds: float = DEFAULT_MIN_DELAY_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.fetcher = fetcher or ResearchFetcher(
            max_response_bytes=MAX_DOCUMENT_BYTES,
            max_text_characters=MAX_DOCUMENT_BYTES,
            user_agent=CORPUS_USER_AGENT,
        )
        self.default_delay_seconds = float(default_delay_seconds)
        self._sleep = sleep
        self._clock = clock
        self._now = now
        self._robots: dict[str, tuple[RobotFileParser, RobotsRecord]] = {}
        self._last_request: dict[str, float] = {}

    @property
    def robots_records(self) -> tuple[RobotsRecord, ...]:
        return tuple(record for _, record in self._robots.values())

    def fetch(self, url: str, *, min_delay_seconds: float = 0.0) -> FetchedResearchDocument:
        # La lista blanca se comprueba antes de consultar robots.txt o de esperar nada.
        url = validate_research_url(url)
        host = urlsplit(url).hostname or ""
        parser, record = self._robots_for(host)
        if not parser.can_fetch(ROBOTS_USER_AGENT, url):
            raise CorpusRobotsError(f"robots.txt de {host} prohíbe recuperar {url}")
        delay = max(self.default_delay_seconds, min_delay_seconds, record.applied_delay_seconds)
        self._wait(host, delay)
        return self.fetcher.fetch_document(url)

    def _robots_for(self, host: str) -> tuple[RobotFileParser, RobotsRecord]:
        cached = self._robots.get(host)
        if cached is not None:
            return cached
        parser = RobotFileParser()
        fetched_at = self._now().isoformat(timespec="seconds")
        sha256: str | None = None
        self._wait(host, self.default_delay_seconds)
        try:
            document = self.fetcher.fetch_document(f"https://{host}/robots.txt")
        except ResearchFetchError:
            # Sin robots.txt legible se aplica el retardo por defecto; nunca se asume nada más.
            parser.parse([])
        else:
            sha256 = document.sha256
            parser.parse(decode_document_body(document.body, charset=document.charset).splitlines())
        crawl_delay = parser.crawl_delay(ROBOTS_USER_AGENT)
        crawl_delay_seconds = float(crawl_delay) if crawl_delay is not None else None
        record = RobotsRecord(
            host=host,
            fetched_at=fetched_at,
            sha256=sha256,
            crawl_delay_seconds=crawl_delay_seconds,
            applied_delay_seconds=max(self.default_delay_seconds, crawl_delay_seconds or 0.0),
        )
        self._robots[host] = (parser, record)
        return parser, record

    def _wait(self, host: str, delay: float) -> None:
        last = self._last_request.get(host)
        if last is not None:
            remaining = delay - (self._clock() - last)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request[host] = self._clock()


# --------------------------------------------------------------------------------------
# Construcción, escritura y verificación del corpus
# --------------------------------------------------------------------------------------


def build_corpus(
    sources: CorpusSources,
    *,
    corpus_dir: Path,
    fetcher: PoliteFetcher,
    only_sources: Iterable[str] | None = None,
    limit_per_source: int | None = None,
    previous: CorpusManifest | None = None,
    progress: Callable[[str], None] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> CorpusManifest:
    """Descarga las fuentes y devuelve el manifiesto; los textos se escriben en corpus_dir."""

    corpus_dir = Path(corpus_dir)
    corpus_dir.mkdir(parents=True, exist_ok=True)
    selected_ids = set(only_sources) if only_sources is not None else None
    if selected_ids is not None:
        unknown = selected_ids - {source.id for source in sources.sources}
        if unknown:
            raise CorpusSourcesError("Fuentes desconocidas: " + ", ".join(sorted(unknown)))
    reusable: dict[str, CorpusItem] = {}
    if previous is not None and previous.sources_sha256 == sources.sha256:
        for item in previous.items:
            path = corpus_dir / item.path
            if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == item.text_sha256:
                reusable[item.url] = item

    items: list[CorpusItem] = []
    skipped: list[Mapping[str, object]] = []
    for source in sources.sources:
        if selected_ids is not None and source.id not in selected_ids:
            continue
        _report(progress, f"[{source.id}] índice {source.index_url}")
        index_document = fetcher.fetch(source.index_url, min_delay_seconds=source.min_delay_seconds)
        entries = parse_index(index_document, source)
        if limit_per_source is not None:
            entries = entries[:limit_per_source]
        _report(progress, f"[{source.id}] {len(entries)} entradas seleccionadas")
        for entry in entries:
            reused = reusable.get(entry.url)
            if reused is not None:
                items.append(reused)
                continue
            try:
                document = fetcher.fetch(entry.url, min_delay_seconds=source.min_delay_seconds)
            except (CorpusRobotsError, ResearchFetchError) as exc:
                skipped.append({"source": source.id, "url": entry.url, "motivo": str(exc)})
                _report(progress, f"[{source.id}] omitido {entry.url}: {exc}")
                continue
            extracted = extract_document_text(
                document, boilerplate_patterns=source.boilerplate_patterns
            )
            text = extracted.text
            if len(text) < MIN_DOCUMENT_CHARACTERS:
                skipped.append(
                    {"source": source.id, "url": entry.url, "motivo": "texto insuficiente"}
                )
                _report(progress, f"[{source.id}] omitido {entry.url}: texto insuficiente")
                continue
            relative = Path(source.id) / f"{corpus_slug(entry.url)}.txt"
            target = corpus_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = text.encode("utf-8")
            target.write_bytes(payload)
            item = CorpusItem(
                id=f"{source.id}/{corpus_slug(entry.url)}",
                source=source.id,
                url=document.url,
                title=entry.title or extracted.title or document.url,
                year=entry.year,
                fetched_at=now().isoformat(timespec="seconds"),
                body_sha256=document.sha256,
                text_sha256=hashlib.sha256(payload).hexdigest(),
                characters=len(text),
                words=count_words(text),
                paragraphs=len(extracted.paragraphs),
                path=relative.as_posix(),
            )
            items.append(item)
            _report(progress, f"[{source.id}] {item.words} palabras ← {entry.url}")
    items.sort(key=lambda item: (item.source, item.url))
    return CorpusManifest(
        version=1,
        generated_at=now().isoformat(timespec="seconds"),
        user_agent=fetcher.fetcher.user_agent or "",
        sources_sha256=sources.sha256,
        robots=tuple(sorted(fetcher.robots_records, key=lambda record: record.host)),
        items=tuple(items),
        skipped=tuple(skipped),
    )


def corpus_slug(url: str) -> str:
    path = urlsplit(url).path.strip("/")
    path = re.sub(r"\.html?$", "", path)
    ascii_path = "".join(
        character
        for character in unicodedata.normalize("NFKD", path.casefold())
        if not unicodedata.combining(character)
    )
    return _SLUG_PATTERN.sub("-", ascii_path).strip("-") or "documento"


def count_words(text: str) -> int:
    return len(_WORD_PATTERN.findall(text))


def write_corpus_manifest(manifest: CorpusManifest, path: str | Path) -> None:
    Path(path).write_text(
        yaml.safe_dump(manifest.to_mapping(), allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )


def load_corpus_manifest(path: str | Path = DEFAULT_MANIFEST_PATH) -> CorpusManifest:
    manifest_path = Path(path)
    try:
        payload = manifest_path.read_bytes()
    except OSError as exc:
        raise CorpusManifestError(f"No se pudo leer el manifiesto: {manifest_path}") from exc
    if not 1 <= len(payload) <= MAX_MANIFEST_BYTES:
        raise CorpusManifestError("El manifiesto tiene un tamaño inválido")
    try:
        raw = load_yaml_unique(payload.decode("utf-8"))
    except (UnicodeError, ValueError, yaml.YAMLError) as exc:
        raise CorpusManifestError("El manifiesto no es YAML válido") from exc
    return parse_corpus_manifest(raw)


def parse_corpus_manifest(raw: object) -> CorpusManifest:
    root = _strict_mapping(
        raw,
        {
            "version",
            "generated_at",
            "user_agent",
            "sources_sha256",
            "totales",
            "robots",
            "omitidos",
            "items",
        },
        "manifiesto",
        CorpusManifestError,
    )
    if root["version"] != 1:
        raise CorpusManifestError("La única versión de manifiesto admitida es 1")
    robots: list[RobotsRecord] = []
    for index, value in enumerate(_sequence(root["robots"], "robots", CorpusManifestError)):
        data = _strict_mapping(
            value,
            {"host", "fetched_at", "sha256", "crawl_delay_seconds", "applied_delay_seconds"},
            f"robots[{index}]",
            CorpusManifestError,
        )
        robots.append(
            RobotsRecord(
                host=_nonempty_text(data["host"], f"robots[{index}].host", CorpusManifestError),
                fetched_at=_nonempty_text(
                    data["fetched_at"], f"robots[{index}].fetched_at", CorpusManifestError
                ),
                sha256=_optional_sha256(data["sha256"], f"robots[{index}].sha256"),
                crawl_delay_seconds=_optional_number(
                    data["crawl_delay_seconds"], f"robots[{index}].crawl_delay_seconds"
                ),
                applied_delay_seconds=float(
                    _optional_number(
                        data["applied_delay_seconds"], f"robots[{index}].applied_delay_seconds"
                    )
                    or 0.0
                ),
            )
        )
    items: list[CorpusItem] = []
    seen_paths: set[str] = set()
    for index, value in enumerate(_sequence(root["items"], "items", CorpusManifestError)):
        location = f"items[{index}]"
        data = _strict_mapping(
            value,
            {
                "id",
                "source",
                "url",
                "title",
                "year",
                "fetched_at",
                "body_sha256",
                "text_sha256",
                "characters",
                "words",
                "paragraphs",
                "path",
            },
            location,
            CorpusManifestError,
        )
        path = _nonempty_text(data["path"], f"{location}.path", CorpusManifestError)
        if path.startswith("/") or ".." in Path(path).parts or path in seen_paths:
            raise CorpusManifestError(f"{location}.path no es una ruta relativa única")
        seen_paths.add(path)
        year = data["year"]
        if year is not None and (isinstance(year, bool) or not isinstance(year, int)):
            raise CorpusManifestError(f"{location}.year debe ser un entero o null")
        items.append(
            CorpusItem(
                id=_nonempty_text(data["id"], f"{location}.id", CorpusManifestError),
                source=_nonempty_text(data["source"], f"{location}.source", CorpusManifestError),
                url=_allowed_https_url(data["url"], f"{location}.url", CorpusManifestError),
                title=_nonempty_text(data["title"], f"{location}.title", CorpusManifestError),
                year=year,
                fetched_at=_nonempty_text(
                    data["fetched_at"], f"{location}.fetched_at", CorpusManifestError
                ),
                body_sha256=_required_sha256(data["body_sha256"], f"{location}.body_sha256"),
                text_sha256=_required_sha256(data["text_sha256"], f"{location}.text_sha256"),
                characters=_positive_int(data["characters"], f"{location}.characters"),
                words=_positive_int(data["words"], f"{location}.words"),
                paragraphs=_positive_int(data["paragraphs"], f"{location}.paragraphs"),
                path=path,
            )
        )
    skipped = tuple(
        dict(
            _strict_mapping(
                value, {"source", "url", "motivo"}, f"omitidos[{index}]", CorpusManifestError
            )
        )
        for index, value in enumerate(_sequence(root["omitidos"], "omitidos", CorpusManifestError))
    )
    manifest = CorpusManifest(
        version=1,
        generated_at=_nonempty_text(root["generated_at"], "generated_at", CorpusManifestError),
        user_agent=str(root["user_agent"] or ""),
        sources_sha256=_required_sha256(root["sources_sha256"], "sources_sha256"),
        robots=tuple(robots),
        items=tuple(items),
        skipped=skipped,
    )
    totals = _strict_mapping(
        root["totales"],
        {"documentos", "palabras", "caracteres", "por_fuente"},
        "totales",
        CorpusManifestError,
    )
    if (
        totals["documentos"] != len(manifest.items)
        or totals["palabras"] != manifest.total_words
        or totals["caracteres"] != manifest.total_characters
    ):
        raise CorpusManifestError("Los totales del manifiesto no coinciden con sus items")
    return manifest


@dataclass(frozen=True, slots=True)
class CorpusVerification:
    verified: int
    missing: tuple[str, ...]
    mismatched: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.missing and not self.mismatched


def verify_corpus(manifest: CorpusManifest, corpus_dir: str | Path) -> CorpusVerification:
    """Comprueba que cada texto local coincide con el SHA-256 declarado en el manifiesto."""

    base = Path(corpus_dir)
    missing: list[str] = []
    mismatched: list[str] = []
    verified = 0
    for item in manifest.items:
        path = base / item.path
        if not path.is_file():
            missing.append(item.path)
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != item.text_sha256:
            mismatched.append(item.path)
            continue
        verified += 1
    return CorpusVerification(verified, tuple(missing), tuple(mismatched))


def read_corpus_texts(manifest: CorpusManifest, corpus_dir: str | Path) -> dict[str, str]:
    """Devuelve {item.id: texto} tras verificar las huellas; falla ante cualquier desvío."""

    verification = verify_corpus(manifest, corpus_dir)
    if not verification.ok:
        problems = ", ".join((*verification.missing, *verification.mismatched)[:5])
        raise CorpusManifestError(f"El corpus local no coincide con el manifiesto: {problems}")
    base = Path(corpus_dir)
    return {item.id: (base / item.path).read_text(encoding="utf-8") for item in manifest.items}


# --------------------------------------------------------------------------------------
# Utilidades de validación
# --------------------------------------------------------------------------------------


def _report(progress: Callable[[str], None] | None, message: str) -> None:
    if progress is not None:
        progress(message)


def _strict_mapping(
    value: object, expected: set[str], location: str, error: type[CorpusError]
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise error(f"'{location}' debe ser un objeto")
    actual = set(value)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing:
        raise error(f"Faltan campos en '{location}': " + ", ".join(missing))
    if unknown:
        raise error(f"Campos desconocidos en '{location}': " + ", ".join(unknown))
    return value


def _sequence(
    value: object, location: str, error: type[CorpusError] = CorpusSourcesError
) -> Sequence[object]:
    if not isinstance(value, list):
        raise error(f"'{location}' debe ser una lista")
    return value


def _nonempty_text(value: object, location: str, error: type[CorpusError]) -> str:
    if not isinstance(value, str) or not value.strip():
        raise error(f"'{location}' debe ser texto no vacío")
    normalized = " ".join(value.split())
    if any(ord(character) < 32 for character in normalized) or len(normalized) > 1_000:
        raise error(f"'{location}' no es válido")
    return normalized


def _allowed_https_url(
    value: object, location: str, error: type[CorpusError] = CorpusSourcesError
) -> str:
    text = _nonempty_text(value, location, error)
    try:
        return validate_research_url(text)
    except ResearchUrlError as exc:
        raise error(f"'{location}' no es una URL autorizada: {exc}") from exc


def _optional_index(value: object, location: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 20:
        raise CorpusSourcesError(f"'{location}' debe ser un índice de columna entre 0 y 20")
    return value


def _optional_number(value: object, location: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise CorpusManifestError(f"'{location}' debe ser un número no negativo")
    return float(value)


def _optional_sha256(value: object, location: str) -> str | None:
    if value is None:
        return None
    return _required_sha256(value, location)


def _required_sha256(value: object, location: str) -> str:
    if not isinstance(value, str) or _HEX_SHA256.fullmatch(value) is None:
        raise CorpusManifestError(f"'{location}' debe ser un SHA-256 hexadecimal")
    return value


def _positive_int(value: object, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CorpusManifestError(f"'{location}' debe ser un entero no negativo")
    return value
