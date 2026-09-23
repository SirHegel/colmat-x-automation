"""Guía de estilo de Gustavo Bueno: carga, validación y texto para el prompt.

La guía (`config/estilo-gustavo-bueno.yaml`) es configuración versionada con la misma
disciplina que la política editorial: claves cerradas, versión fija y validación
fallida ante cualquier desvío. Cada directiva y cada término del léxico citan claves de
la evidencia estilométrica (`config/estilo-gustavo-bueno-evidencia.yaml`), que a su vez
lleva el SHA-256 del manifiesto del corpus. Así, ningún rasgo de estilo que llegue al
prompt carece de respaldo trazable en el corpus descargado.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from colmat_x.style_analysis import (
    StyleAnalysisError,
    resolve_evidence_key,
    validate_style_evidence,
)
from colmat_x.yaml_utils import load_yaml_unique

DEFAULT_STYLE_GUIDE_PATH = Path("config/estilo-gustavo-bueno.yaml")
STYLE_GUIDE_VERSION = 1
MIN_LEXICON_OCCURRENCES = 5
MAX_STYLE_PROMPT_CHARACTERS = 6_000
MAX_CONFIG_BYTES = 2 * 1024 * 1024
REQUIRED_LIMIT_TERMS = ("cifra", "fuente", "280", "identidad visual")
RAE_MARKERS = ("real academia española", "rae")


class StyleGuideError(ValueError):
    """La guía de estilo no coincide con su evidencia o no cumple el contrato."""


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    key: str
    minimum: float | None


@dataclass(frozen=True, slots=True)
class LexiconEntry:
    term: str
    key: str
    usage: str


@dataclass(frozen=True, slots=True)
class StyleDirective:
    id: str
    rule: str
    evidence: tuple[EvidenceReference, ...]
    examples: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OrthographicNorm:
    authority: str
    variety: str
    rules: tuple[str, ...]
    prohibitions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StyleGuide:
    version: int
    name: str
    purpose: str
    derived_on: str
    evidence_path: str
    manifest_path: str
    manifest_sha256: str
    corpus_documents: int
    corpus_words: int
    norm: OrthographicNorm
    lexicon: tuple[LexiconEntry, ...]
    directives: tuple[StyleDirective, ...]
    limits: tuple[str, ...]

    def generation_prompt(self) -> str:
        """Bloque de registro para el prompt; modula el tono y reitera que no relaja reglas."""

        lines = [
            "Registro de redacción: guía de estilo derivada del corpus público de Gustavo "
            f"Bueno ({self.corpus_documents} textos, {self.corpus_words} palabras; manifiesto "
            f"SHA-256 {self.manifest_sha256[:12]}). Escribe como él, no como un ensayo "
            "académico anglosajón:"
        ]
        for directive in self.directives:
            line = f"- {directive.rule}"
            if directive.examples:
                line += " Ejemplos del corpus: " + ", ".join(
                    f"«{example}»" for example in directive.examples
                )
                line += "."
            lines.append(line)
        lines.append(
            "Léxico técnico admisible, solo con propiedad y sin pedantería: "
            + "; ".join(f"{entry.term}: {entry.usage}" for entry in self.lexicon)
        )
        lines.append(
            f"Norma ortográfica y de estilo: {self.norm.authority}. Variedad: {self.norm.variety}. "
            + " ".join(self.norm.rules)
        )
        lines.append("Prohibido: " + " ".join(self.norm.prohibitions))
        lines.append("Límites del estilo: " + " ".join(self.limits))
        return "\n".join(lines)


@lru_cache(maxsize=1)
def load_gustavo_bueno_style() -> StyleGuide:
    """Carga la guía canónica del checkout o del paquete instalado; falla cerrado."""

    try:
        guide_raw = load_yaml_unique(_read_config_text(DEFAULT_STYLE_GUIDE_PATH.name))
    except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
        if isinstance(exc, StyleGuideError):
            raise
        raise StyleGuideError("No se pudo leer la guía de estilo canónica") from exc
    return parse_style_guide(guide_raw, config_reader=_read_config_bytes)


def load_style_guide(path: str | Path = DEFAULT_STYLE_GUIDE_PATH) -> StyleGuide:
    """Carga una guía desde una ruta explícita; sus referencias se resuelven junto a ella."""

    guide_path = Path(path)
    try:
        guide_raw = load_yaml_unique(guide_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
        raise StyleGuideError(f"No se pudo leer la guía de estilo: {guide_path}") from exc

    def reader(name: str) -> bytes:
        candidate = guide_path.parent / name
        try:
            return candidate.read_bytes()
        except OSError as exc:
            raise StyleGuideError(f"No se pudo leer el archivo referido: {candidate}") from exc

    return parse_style_guide(guide_raw, config_reader=reader)


def parse_style_guide(raw: object, *, config_reader: Any) -> StyleGuide:
    root = _strict_mapping(
        raw,
        {"version", "metadata", "norma_ortografica", "lexico", "directivas", "limites"},
        "raíz",
    )
    if root["version"] != STYLE_GUIDE_VERSION:
        raise StyleGuideError("La única versión de guía de estilo admitida es 1")
    metadata = _strict_mapping(
        root["metadata"],
        {"nombre", "finalidad", "evidencia", "manifiesto", "derivada_el", "advertencia"},
        "metadata",
    )
    evidence_name = _config_file_name(metadata["evidencia"], "metadata.evidencia")
    manifest_name = _config_file_name(metadata["manifiesto"], "metadata.manifiesto")
    derived_on = _nonempty_text(metadata["derivada_el"], "metadata.derivada_el")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", derived_on) is None:
        raise StyleGuideError("metadata.derivada_el debe ser una fecha AAAA-MM-DD")
    _nonempty_text(metadata["advertencia"], "metadata.advertencia")

    try:
        evidence = validate_style_evidence(
            load_yaml_unique(config_reader(evidence_name).decode("utf-8"))
        )
    except (UnicodeError, ValueError, yaml.YAMLError) as exc:
        if isinstance(exc, StyleGuideError):
            raise
        raise StyleGuideError(f"La evidencia {evidence_name} no es válida: {exc}") from exc
    manifest_bytes = config_reader(manifest_name)
    if not 1 <= len(manifest_bytes) <= MAX_CONFIG_BYTES:
        raise StyleGuideError("El manifiesto del corpus tiene un tamaño inválido")
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if manifest_sha256 != evidence["manifiesto_sha256"]:
        raise StyleGuideError(
            "La evidencia no corresponde al manifiesto del corpus versionado; "
            "vuelve a ejecutar bueno-style-derive"
        )

    norm = _parse_norm(root["norma_ortografica"])
    lexicon = _parse_lexicon(root["lexico"], evidence)
    directives = _parse_directives(root["directivas"], evidence)
    limits = _text_sequence(root["limites"], "limites")
    joined_limits = " ".join(limits).casefold()
    for term in REQUIRED_LIMIT_TERMS:
        if term not in joined_limits:
            raise StyleGuideError(f"limites debe reiterar explícitamente «{term}»")

    guide = StyleGuide(
        version=STYLE_GUIDE_VERSION,
        name=_nonempty_text(metadata["nombre"], "metadata.nombre"),
        purpose=_nonempty_text(metadata["finalidad"], "metadata.finalidad"),
        derived_on=derived_on,
        evidence_path=evidence_name,
        manifest_path=manifest_name,
        manifest_sha256=manifest_sha256,
        corpus_documents=int(evidence["corpus"]["documentos"]),
        corpus_words=int(evidence["corpus"]["palabras"]),
        norm=norm,
        lexicon=lexicon,
        directives=directives,
        limits=limits,
    )
    prompt_length = len(guide.generation_prompt())
    if prompt_length > MAX_STYLE_PROMPT_CHARACTERS:
        raise StyleGuideError(
            f"El bloque de estilo mide {prompt_length} caracteres; el máximo es "
            f"{MAX_STYLE_PROMPT_CHARACTERS}"
        )
    return guide


def _parse_norm(value: object) -> OrthographicNorm:
    data = _strict_mapping(
        value, {"autoridad", "variedad", "reglas", "prohibiciones"}, "norma_ortografica"
    )
    authority = _nonempty_text(data["autoridad"], "norma_ortografica.autoridad")
    folded = authority.casefold()
    if not any(marker in folded for marker in RAE_MARKERS):
        raise StyleGuideError("norma_ortografica.autoridad debe ser la Real Academia Española")
    prohibitions = _text_sequence(data["prohibiciones"], "norma_ortografica.prohibiciones")
    if not any(
        "rae" in item.casefold() or "real academia" in item.casefold() for item in prohibitions
    ):
        raise StyleGuideError(
            "norma_ortografica.prohibiciones debe excluir toda norma distinta de la RAE"
        )
    return OrthographicNorm(
        authority=authority,
        variety=_nonempty_text(data["variedad"], "norma_ortografica.variedad"),
        rules=_text_sequence(data["reglas"], "norma_ortografica.reglas"),
        prohibitions=prohibitions,
    )


def _parse_lexicon(value: object, evidence: Mapping[str, Any]) -> tuple[LexiconEntry, ...]:
    items = _sequence(value, "lexico")
    entries: list[LexiconEntry] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        location = f"lexico[{index}]"
        data = _strict_mapping(item, {"termino", "clave", "uso"}, location)
        term = _nonempty_text(data["termino"], f"{location}.termino")
        key = _nonempty_text(data["clave"], f"{location}.clave")
        if not key.startswith("lexico_tecnico."):
            raise StyleGuideError(f"{location}.clave debe apuntar a lexico_tecnico")
        record = _resolve(evidence, key, location)
        if not isinstance(record, Mapping) or not isinstance(record.get("ocurrencias"), int):
            raise StyleGuideError(f"{location}.clave no resuelve a un término con ocurrencias")
        if record["ocurrencias"] < MIN_LEXICON_OCCURRENCES:
            raise StyleGuideError(
                f"{location}: «{term}» aparece menos de {MIN_LEXICON_OCCURRENCES} veces"
            )
        if term.casefold() in seen:
            raise StyleGuideError(f"lexico repite el término «{term}»")
        seen.add(term.casefold())
        entries.append(
            LexiconEntry(term=term, key=key, usage=_nonempty_text(data["uso"], f"{location}.uso"))
        )
    if not entries:
        raise StyleGuideError("lexico no puede estar vacío")
    return tuple(entries)


def _parse_directives(value: object, evidence: Mapping[str, Any]) -> tuple[StyleDirective, ...]:
    items = _sequence(value, "directivas")
    directives: list[StyleDirective] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        location = f"directivas[{index}]"
        data = _strict_mapping(
            item, {"id", "regla", "evidencia", "ejemplos", "ejemplos_en"}, location
        )
        identifier = _nonempty_text(data["id"], f"{location}.id")
        if re.fullmatch(r"[a-z0-9-]{3,60}", identifier) is None:
            raise StyleGuideError(f"{location}.id debe ser un identificador en minúsculas")
        if identifier in seen:
            raise StyleGuideError(f"directivas repite el id {identifier}")
        seen.add(identifier)
        references: list[EvidenceReference] = []
        for ref_index, reference in enumerate(
            _sequence(data["evidencia"], f"{location}.evidencia")
        ):
            ref_location = f"{location}.evidencia[{ref_index}]"
            ref_data = _strict_mapping(reference, {"clave", "minimo"}, ref_location)
            key = _nonempty_text(ref_data["clave"], f"{ref_location}.clave")
            resolved = _resolve(evidence, key, ref_location)
            minimum = ref_data["minimo"]
            if minimum is not None:
                if isinstance(minimum, bool) or not isinstance(minimum, (int, float)):
                    raise StyleGuideError(f"{ref_location}.minimo debe ser numérico o null")
                if isinstance(resolved, bool) or not isinstance(resolved, (int, float)):
                    raise StyleGuideError(f"{ref_location}.clave no resuelve a un número")
                if resolved < minimum:
                    raise StyleGuideError(
                        f"{ref_location}: {key} vale {resolved}, por debajo de {minimum}"
                    )
            references.append(EvidenceReference(key=key, minimum=minimum))
        if not references:
            raise StyleGuideError(f"{location} debe citar al menos una clave de evidencia")
        examples_raw = data["ejemplos"]
        examples: tuple[str, ...] = ()
        if examples_raw is not None:
            examples = _text_sequence(examples_raw, f"{location}.ejemplos")
            section = data["ejemplos_en"]
            if section is None:
                raise StyleGuideError(f"{location}.ejemplos exige ejemplos_en")
            section_map = _resolve(
                evidence, _nonempty_text(section, f"{location}.ejemplos_en"), location
            )
            if not isinstance(section_map, Mapping):
                raise StyleGuideError(
                    f"{location}.ejemplos_en debe ser una sección de la evidencia"
                )
            for example in examples:
                if example not in section_map:
                    raise StyleGuideError(
                        f"{location}: el ejemplo «{example}» no aparece en la evidencia {section}"
                    )
        elif data["ejemplos_en"] is not None:
            raise StyleGuideError(f"{location}.ejemplos_en exige ejemplos")
        directives.append(
            StyleDirective(
                id=identifier,
                rule=_nonempty_text(data["regla"], f"{location}.regla"),
                evidence=tuple(references),
                examples=examples,
            )
        )
    if not directives:
        raise StyleGuideError("directivas no puede estar vacío")
    return tuple(directives)


def _resolve(evidence: Mapping[str, Any], key: str, location: str) -> Any:
    try:
        return resolve_evidence_key(evidence, key)
    except StyleAnalysisError as exc:
        raise StyleGuideError(f"{location}: {exc}") from exc


def _read_config_text(name: str) -> str:
    return _read_config_bytes(name).decode("utf-8")


def _read_config_bytes(name: str) -> bytes:
    project_root = Path(__file__).resolve().parents[2]
    requested = _checkout_config_path(name)
    if requested.exists():
        config_path = requested.resolve(strict=True)
        if not config_path.is_relative_to(project_root):
            raise StyleGuideError("La configuración de estilo sale de la raíz confiable")
        payload = config_path.read_bytes()
    else:
        try:
            payload = resources.files("colmat_x").joinpath("data", name).read_bytes()
        except (FileNotFoundError, ModuleNotFoundError, OSError) as exc:
            raise StyleGuideError(f"No está disponible la configuración de estilo {name}") from exc
    if not 1 <= len(payload) <= MAX_CONFIG_BYTES:
        raise StyleGuideError(f"La configuración de estilo {name} tiene un tamaño inválido")
    return payload


def _checkout_config_path(name: str) -> Path:
    return Path(__file__).resolve().parents[2] / "config" / name


def _config_file_name(value: object, location: str) -> str:
    text = _nonempty_text(value, location)
    name = Path(text).name
    if text != f"config/{name}" or re.fullmatch(r"[a-z0-9-]+\.yaml", name) is None:
        raise StyleGuideError(f"{location} debe ser un archivo YAML bajo config/")
    return name


def _strict_mapping(value: object, expected: set[str], location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise StyleGuideError(f"'{location}' debe ser un objeto")
    actual = set(value)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing:
        raise StyleGuideError(f"Faltan campos en '{location}': " + ", ".join(missing))
    if unknown:
        raise StyleGuideError(f"Campos desconocidos en '{location}': " + ", ".join(unknown))
    return value


def _sequence(value: object, location: str) -> Sequence[object]:
    if not isinstance(value, list):
        raise StyleGuideError(f"'{location}' debe ser una lista")
    return value


def _nonempty_text(value: object, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StyleGuideError(f"'{location}' debe ser texto no vacío")
    normalized = " ".join(value.split())
    if any(ord(character) < 32 for character in normalized) or len(normalized) > 1_000:
        raise StyleGuideError(f"'{location}' no es válido")
    return normalized


def _text_sequence(value: object, location: str) -> tuple[str, ...]:
    items = _sequence(value, location)
    if not items:
        raise StyleGuideError(f"'{location}' debe ser una lista no vacía")
    result = tuple(_nonempty_text(item, f"{location}[{index}]") for index, item in enumerate(items))
    if len(set(result)) != len(result):
        raise StyleGuideError(f"'{location}' no admite valores duplicados")
    return result
