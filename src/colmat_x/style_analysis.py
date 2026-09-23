"""Análisis estilométrico reproducible del corpus de Gustavo Bueno.

Todo rasgo que la guía de estilo afirme debe apoyarse en una clave de la evidencia que
produce este módulo: frecuencias léxicas, n-gramas, longitud de frase y de párrafo,
conectores, vocabulario técnico del materialismo filosófico, puntuación, aperturas,
cierres y figuras argumentales. El resultado es determinista para un mismo corpus.
"""

from __future__ import annotations

import hashlib
import re
import statistics
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from colmat_x.style_corpus import CorpusManifest, read_corpus_texts
from colmat_x.yaml_utils import load_yaml_unique

DEFAULT_EVIDENCE_PATH = Path("config/estilo-gustavo-bueno-evidencia.yaml")
EVIDENCE_VERSION = 1
TOP_VOCABULARY = 60
TOP_NGRAMS = 30
TOP_OPENINGS = 15
PER = 10_000

_WORD_PATTERN = re.compile(r"[^\W\d_]+(?:[-'’][^\W\d_]+)*", re.UNICODE)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+(?=[«“\"¿¡(\[]?[A-ZÁÉÍÓÚÜÑ0-9])")
_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")
_ENUMERATION_PREFIX = re.compile(r"^(?:\(?\d{1,2}[.)]|[IVX]{1,4}\.|§\s*\d*\.?)\s+")

_STOPWORD_TEXT = (
    "a al algo alguna algunas alguno algunos ante aquel aquella aquellas aquellos aquí así aun "
    "aunque bajo bien cada como con contra cual cuales cuando cuanto cuyo cuya cuyos cuyas de del "
    "desde donde dos e el ella ellas ello ellos en entre era eran es esa esas ese eso esos esta "
    "estas este esto estos fue fueron ha habría había han hasta hay la las le les lo los más me "
    "mi mis mismo misma mismos mismas mucho muchos muy nada ni no nos nosotros nuestra nuestro o "
    "otra otras otro otros para pero poco por porque pues que qué quien quienes se sea sean según "
    "ser si sí sido siempre sin sino sobre su sus tal tales también tan tanto te tiene tienen "
    "toda todas todo todos tras tu un una unas uno unos vez y ya yo él está están estar he hemos "
    "puede pueden sería serían fuera fuese sólo solo allí cuál cuáles"
)
STOPWORDS = frozenset(_STOPWORD_TEXT.split())

CONNECTORS: tuple[tuple[str, str], ...] = (
    ("es decir", r"\bes decir\b"),
    ("ahora bien", r"\bahora bien\b"),
    ("en efecto", r"\ben efecto\b"),
    ("por tanto", r"\bpor tanto\b"),
    ("por consiguiente", r"\bpor consiguiente\b"),
    ("sin embargo", r"\bsin embargo\b"),
    ("en cualquier caso", r"\ben cualquier caso\b"),
    ("en todo caso", r"\ben todo caso\b"),
    ("por ejemplo", r"\bpor ejemplo\b"),
    ("a saber", r"\ba saber\b"),
    ("en rigor", r"\ben rigor\b"),
    ("desde luego", r"\bdesde luego\b"),
    ("precisamente", r"\bprecisamente\b"),
    ("sin duda", r"\bsin duda\b"),
    ("por lo demás", r"\bpor lo dem[aá]s\b"),
    ("en resolución", r"\ben resoluci[oó]n\b"),
    ("en resumen", r"\ben resumen\b"),
    ("en suma", r"\ben suma\b"),
    ("dicho de otro modo", r"\bdicho de otro modo\b"),
    ("lo que quiere decir", r"\blo que quiere decir\b"),
    ("según esto", r"\bseg[uú]n esto\b"),
    ("de hecho", r"\bde hecho\b"),
    ("en este sentido", r"\ben este sentido\b"),
    ("sin perjuicio de", r"\bsin perjuicio de\b"),
    ("en modo alguno", r"\ben modo alguno\b"),
    ("ni siquiera", r"\bni siquiera\b"),
    ("tan solo", r"\btan s[oó]lo\b"),
    ("esto es", r"\besto es\b"),
    ("o sea", r"\bo sea\b"),
    ("en consecuencia", r"\ben consecuencia\b"),
    ("así pues", r"\bas[ií] pues\b"),
    ("pues bien", r"\bpues bien\b"),
    ("es cierto que", r"\bes cierto que\b"),
    ("en realidad", r"\ben realidad\b"),
    ("acaso", r"\bacaso\b"),
    ("cabría / cabe", r"\bcabr[ií]a\b|\bcabe\b"),
    ("por decirlo así", r"\bpor decirlo as[ií]\b"),
    ("en cuanto tal", r"\ben cuanto tal(?:es)?\b"),
    ("ante todo", r"\bante todo\b"),
    ("a lo sumo", r"\ba lo sumo\b"),
    ("hasta el punto de", r"\bhasta el punto de\b"),
    ("&c.", r"&c\."),
)

TECHNICAL_LEXICON: tuple[tuple[str, str, bool], ...] = (
    # (etiqueta, patrón, sensible a mayúsculas)
    ("symploké", r"\bsymplok[eé]s?\b", False),
    ("holización", r"\bholiza(?:ci[oó]n|ciones|r|d[oa]s?)\b", False),
    ("diamérico", r"\bdiam[eé]ric[oa]s?\b", False),
    ("metamérico", r"\bmetam[eé]ric[oa]s?\b", False),
    ("anamnesis", r"\ban[aá]mnesis\b", False),
    ("Ego trascendental", r"\bego trascendental\b", False),
    ("M1/M2/M3", r"\bM[123]\b", True),
    ("materia ontológico-general", r"\bmateria ontol[oó]gico[- ]general\b", False),
    ("ciencias categoriales", r"\bciencias? categorial(?:es)?\b", False),
    ("cierre categorial", r"\bcierre categorial\b", False),
    ("materialismo filosófico", r"\bmaterialismo filos[oó]fico\b", False),
    ("Idea (mayúscula)", r"\bIdeas?\b", True),
    ("idea (minúscula)", r"\bideas?\b", True),
    ("concepto", r"\bconceptos?\b", False),
    ("categoría", r"\bcategor[ií]as?\b", False),
    ("dialéctica", r"\bdial[eé]ctic[oa]s?\b", False),
    ("gnoseología", r"\bgnoseol[oó]gic[oa]s?\b|\bgnoseolog[ií]a\b", False),
    ("ontología", r"\bontol[oó]gic[oa]s?\b|\bontolog[ií]a\b", False),
    ("totalidad", r"\btotalidad(?:es)?\b", False),
    ("partes", r"\bpartes\b", False),
    ("todo atributivo / distributivo", r"\b(?:atributiv|distributiv)[oa]s?\b", False),
    ("eutaxia", r"\beutaxia\b", False),
    ("nematología", r"\bnematol[oó]gic[oa]s?\b|\bnematolog[ií]a\b", False),
    ("morfología", r"\bmorfolog[ií]as?\b|\bmorfol[oó]gic[oa]s?\b", False),
    ("ejercicio / representación", r"\bejercid[oa]s?\b|\bejercicio\b|\brepresentad[oa]s?\b", False),
    ("operatorio", r"\boperatori[oa]s?\b", False),
    ("apotético / paratético", r"\b(?:apot[eé]tic|parat[eé]tic)[oa]s?\b", False),
    ("regressus / progressus", r"\b(?:regressus|progressus)\b", False),
    ("ortograma", r"\bortogramas?\b", False),
    ("sociedad política", r"\bsociedad(?:es)? pol[ií]ticas?\b", False),
    ("sociedad civil", r"\bsociedad civil\b", False),
    ("Estado", r"\bEstados?\b", True),
    ("nación política", r"\bnaci[oó]n(?:es)? pol[ií]ticas?\b", False),
    ("Imperio", r"\bImperios?\b", True),
    ("fundamentalismo democrático", r"\bfundamentalismo democr[aá]tico\b", False),
    ("realmente existente", r"\brealmente existente\b", False),
    ("Pensamiento Alicia", r"\bpensamiento alicia\b", False),
    ("trituración", r"\btritura(?:ci[oó]n|ciones|r|d[oa]s?)\b", False),
    ("mito", r"\bmitos?\b", False),
    ("ideología", r"\bideolog[ií]as?\b", False),
    ("ceremonia", r"\bceremonias?\b", False),
    ("numen / númenes", r"\bn[uú]men(?:es)?\b", False),
    ("espacio antropológico", r"\bespacio antropol[oó]gico\b", False),
    ("ejes circular / radial / angular", r"\beje (?:circular|radial|angular)\b", False),
    ("institución", r"\binstituci(?:[oó]n|ones)\b", False),
    ("filosofía mundana / académica", r"\bfilosof[ií]a (?:mundana|acad[eé]mica)\b", False),
    ("saber de segundo grado", r"\bde segundo grado\b", False),
    ("conceptos conjugados", r"\bconceptos conjugados\b", False),
    (
        "primogenérico / segundogenérico / terciogenérico",
        r"\b(?:primo|segundo|tercio)gen[eé]ric[oa]s?\b",
        False,
    ),
    ("holótico", r"\bhol[oó]tic[oa]s?\b", False),
    ("lisológico", r"\blisol[oó]gic[oa]s?\b", False),
    ("emic / etic", r"\b(?:emic|etic)\b", False),
    ("monismo / pluralismo", r"\b(?:monismo|pluralismo)\b", False),
    ("hiperrealismo", r"\bhiperrealis(?:mo|ta)s?\b", False),
    ("sustancialismo", r"\bsustancialis(?:mo|ta)s?\b", False),
    ("norma / normas", r"\bnormas?\b", False),
    ("cultura (mayúscula)", r"\bCultura\b", True),
    ("Nación (mayúscula)", r"\bNaci[oó]n(?:es)?\b", True),
    ("Historia (mayúscula)", r"\bHistoria\b", True),
)

RHETORICAL_FIGURES: tuple[tuple[str, str], ...] = (
    ("no X sino Y", r"\bno\b[^.;:?!]{1,80}?\bsino\b"),
    ("no tanto X cuanto Y", r"\bno tanto\b[^.;:?!]{1,80}?\bcuanto\b"),
    ("no ya X sino Y", r"\bno ya\b[^.;:?!]{1,80}?\bsino\b"),
    ("los llamados / la llamada", r"\b(?:el|la|los|las) llamad[oa]s?\b"),
    ("supuesto / pretendido / presunto", r"\b(?:supuest|pretendid|presunt)[oa]s?\b"),
    ("comillas irónicas de una palabra", r"«[^\W\d_]+»|“[^\W\d_]+”"),
    ("enumeración numerada (1) (2)", r"\(\d{1,2}\)"),
    ("en primer / segundo / tercer lugar", r"\ben (?:primer|segundo|tercer) lugar\b"),
    ("sencillamente", r"\bsencillamente\b"),
    ("ni más ni menos", r"\bni m[aá]s ni menos\b"),
    (
        "latinismos",
        r"\b(?:sui generis|in actu|in fieri|a fortiori|a priori|a posteriori|per se|de facto"
        r"|de iure|ad hoc|mutatis mutandis|ex hypothesi|in medias res|quid pro quo|grosso modo)\b",
    ),
    ("confusión / oscuridad (crítica)", r"\b(?:confus[oa]s?|confusi[oó]n|oscur[oa]s?|oscuridad)\b"),
    ("absurdo / disparate", r"\b(?:absurd[oa]s?|disparates?)\b"),
    ("reconstrucción / reconstruir", r"\breconstru(?:cci[oó]n|ir|ye|id[oa]s?)\b"),
    ("distinguir / distinción", r"\bdistin(?:guir|ci[oó]n|ciones|gue|guimos)\b"),
    ("clasificación / clasificar", r"\bclasifica(?:ci[oó]n|ciones|r|mos)\b"),
    ("dialelo", r"\bdialelos?\b"),
    ("sólo con tilde (ortografía anterior a 2010)", r"\bs[oó]lo\b"),
)

PUNCTUATION_MARKS: tuple[tuple[str, str], ...] = (
    ("comillas angulares «»", r"«"),
    ("comillas inglesas “”", r"“"),
    ('comillas rectas "', r"\""),
    ("raya —", r"—"),
    ("semirraya –", r"–"),
    ("paréntesis (", r"\("),
    ("punto y coma ;", r";"),
    ("dos puntos :", r":"),
    ("interrogación de apertura ¿", r"¿"),
    ("exclamación de apertura ¡", r"¡"),
    ("puntos suspensivos", r"…|\.\.\."),
    ("etcétera abreviado etc.", r"\betc\."),
    ("etcétera &c.", r"&c\."),
)


class StyleAnalysisError(ValueError):
    """La evidencia estilométrica no es válida o no coincide con el corpus."""


@dataclass(frozen=True, slots=True)
class DocumentStats:
    words: int
    sentences: int
    paragraphs: int


def tokenize_words(text: str) -> list[str]:
    return _WORD_PATTERN.findall(text)


def split_sentences(text: str) -> list[str]:
    sentences: list[str] = []
    for paragraph in _PARAGRAPH_SPLIT.split(text):
        # Un número de sección («2.», «(1)», «§3.») no es una oración por sí mismo.
        paragraph = _ENUMERATION_PREFIX.sub("", paragraph.strip())
        if not paragraph:
            continue
        for sentence in _SENTENCE_SPLIT.split(paragraph):
            sentence = sentence.strip()
            if sentence:
                sentences.append(sentence)
    return sentences


def split_paragraphs(text: str) -> list[str]:
    return [part.strip() for part in _PARAGRAPH_SPLIT.split(text) if part.strip()]


def compute_style_evidence(
    texts: Mapping[str, str],
    *,
    manifest_sha256: str | None = None,
) -> dict[str, Any]:
    """Calcula la evidencia estilométrica de un corpus {id: texto}; determinista."""

    if not texts:
        raise StyleAnalysisError("El corpus está vacío")
    document_ids = sorted(texts)
    all_words: list[str] = []
    sentence_lengths: list[int] = []
    paragraph_word_lengths: list[int] = []
    paragraph_sentence_counts: list[int] = []
    total_sentences = 0
    total_paragraphs = 0
    total_characters = 0
    question_sentences = 0
    exclamation_sentences = 0
    opening_questions = 0
    closing_questions = 0
    opening_first_words: Counter[str] = Counter()
    opening_first_bigrams: Counter[str] = Counter()
    closing_first_words: Counter[str] = Counter()
    closing_first_bigrams: Counter[str] = Counter()
    connector_counts: Counter[str] = Counter()
    connector_docs: Counter[str] = Counter()
    lexicon_counts: Counter[str] = Counter()
    lexicon_docs: Counter[str] = Counter()
    figure_counts: Counter[str] = Counter()
    figure_docs: Counter[str] = Counter()
    punctuation_counts: Counter[str] = Counter()
    numbered_paragraphs = 0
    long_words = 0
    connector_patterns = [
        (label, re.compile(pattern, re.IGNORECASE)) for label, pattern in CONNECTORS
    ]
    lexicon_patterns = [
        (label, re.compile(pattern, 0 if case_sensitive else re.IGNORECASE))
        for label, pattern, case_sensitive in TECHNICAL_LEXICON
    ]
    figure_patterns = [
        (label, re.compile(pattern, re.IGNORECASE)) for label, pattern in RHETORICAL_FIGURES
    ]
    punctuation_patterns = [(label, re.compile(pattern)) for label, pattern in PUNCTUATION_MARKS]

    for document_id in document_ids:
        text = unicodedata.normalize("NFC", texts[document_id])
        total_characters += len(text)
        words = tokenize_words(text)
        all_words.extend(words)
        long_words += sum(1 for word in words if len(word) >= 10)
        sentences = split_sentences(text)
        total_sentences += len(sentences)
        for sentence in sentences:
            sentence_words = tokenize_words(sentence)
            sentence_lengths.append(len(sentence_words))
            if sentence.endswith("?"):
                question_sentences += 1
            if sentence.endswith("!"):
                exclamation_sentences += 1
        paragraphs = split_paragraphs(text)
        total_paragraphs += len(paragraphs)
        for paragraph in paragraphs:
            paragraph_word_lengths.append(len(tokenize_words(paragraph)))
            paragraph_sentence_counts.append(len(split_sentences(paragraph)))
            if _ENUMERATION_PREFIX.match(paragraph):
                numbered_paragraphs += 1
        if sentences:
            first = sentences[0]
            last = sentences[-1]
            if first.endswith("?"):
                opening_questions += 1
            if last.endswith("?"):
                closing_questions += 1
            first_words = [word.casefold() for word in tokenize_words(first)]
            last_words = [word.casefold() for word in tokenize_words(last)]
            if first_words:
                opening_first_words[first_words[0]] += 1
                if len(first_words) > 1:
                    opening_first_bigrams[" ".join(first_words[:2])] += 1
            if last_words:
                closing_first_words[last_words[0]] += 1
                if len(last_words) > 1:
                    closing_first_bigrams[" ".join(last_words[:2])] += 1
        for label, pattern in connector_patterns:
            hits = len(pattern.findall(text))
            if hits:
                connector_counts[label] += hits
                connector_docs[label] += 1
        for label, pattern in lexicon_patterns:
            hits = len(pattern.findall(text))
            if hits:
                lexicon_counts[label] += hits
                lexicon_docs[label] += 1
        for label, pattern in figure_patterns:
            hits = len(pattern.findall(text))
            if hits:
                figure_counts[label] += hits
                figure_docs[label] += 1
        for label, pattern in punctuation_patterns:
            punctuation_counts[label] += len(pattern.findall(text))

    total_words = len(all_words)
    if total_words == 0 or total_sentences == 0:
        raise StyleAnalysisError("El corpus no contiene texto analizable")
    document_count = len(document_ids)
    folded = [word.casefold() for word in all_words]
    content = [word for word in folded if word not in STOPWORDS and len(word) > 2]
    vocabulary = Counter(content)
    bigrams = Counter(zip(folded, folded[1:], strict=False))
    trigrams = Counter(zip(folded, folded[1:], folded[2:], strict=False))
    content_trigrams = Counter(
        gram for gram in trigrams.elements() if any(token not in STOPWORDS for token in gram)
    )

    def per_10k(count: int) -> float:
        return round(count * PER / total_words, 2)

    def doc_pct(count: int) -> float:
        return round(count * 100 / document_count, 1)

    def distribution(values: Sequence[int]) -> dict[str, float]:
        buckets = {"1-15": 0, "16-30": 0, "31-50": 0, "51+": 0}
        for value in values:
            if value <= 15:
                buckets["1-15"] += 1
            elif value <= 30:
                buckets["16-30"] += 1
            elif value <= 50:
                buckets["31-50"] += 1
            else:
                buckets["51+"] += 1
        return {key: round(count * 100 / len(values), 1) for key, count in buckets.items()}

    sorted_lengths = sorted(sentence_lengths)
    p90_index = min(len(sorted_lengths) - 1, int(round(0.9 * len(sorted_lengths))))
    evidence: dict[str, Any] = {
        "version": EVIDENCE_VERSION,
        "manifiesto_sha256": manifest_sha256,
        "corpus": {
            "documentos": document_count,
            "palabras": total_words,
            "oraciones": total_sentences,
            "parrafos": total_paragraphs,
            "caracteres": total_characters,
            "palabras_largas_pct": round(long_words * 100 / total_words, 2),
        },
        "oracion": {
            "media_palabras": round(statistics.fmean(sentence_lengths), 2),
            "mediana_palabras": statistics.median(sentence_lengths),
            "p90_palabras": sorted_lengths[p90_index],
            "distribucion_pct": distribution(sentence_lengths),
            "interrogativas_pct": round(question_sentences * 100 / total_sentences, 2),
            "exclamativas_pct": round(exclamation_sentences * 100 / total_sentences, 2),
        },
        "parrafo": {
            "media_palabras": round(statistics.fmean(paragraph_word_lengths), 2),
            "mediana_palabras": statistics.median(paragraph_word_lengths),
            "media_oraciones": round(statistics.fmean(paragraph_sentence_counts), 2),
            "numerados_pct": round(numbered_paragraphs * 100 / total_paragraphs, 2),
        },
        "conectores_por_10k": {
            label: {
                "por_10k": per_10k(count),
                "ocurrencias": count,
                "documentos_pct": doc_pct(connector_docs[label]),
            }
            for label, count in sorted(
                connector_counts.items(), key=lambda item: (-item[1], item[0])
            )
        },
        "lexico_tecnico": {
            label: {
                "ocurrencias": count,
                "por_10k": per_10k(count),
                "documentos_pct": doc_pct(lexicon_docs[label]),
            }
            for label, count in sorted(lexicon_counts.items(), key=lambda item: (-item[1], item[0]))
        },
        "figuras_por_10k": {
            label: {
                "por_10k": per_10k(count),
                "ocurrencias": count,
                "documentos_pct": doc_pct(figure_docs[label]),
            }
            for label, count in sorted(figure_counts.items(), key=lambda item: (-item[1], item[0]))
        },
        "puntuacion_por_10k": {
            label: per_10k(count)
            for label, count in sorted(
                punctuation_counts.items(), key=lambda item: (-item[1], item[0])
            )
        },
        "aperturas": {
            "pregunta_inicial_pct": doc_pct(opening_questions),
            "primeras_palabras": _top(opening_first_words, TOP_OPENINGS),
            "primeros_bigramas": _top(opening_first_bigrams, TOP_OPENINGS),
        },
        "cierres": {
            "pregunta_final_pct": doc_pct(closing_questions),
            "primeras_palabras_ultima_oracion": _top(closing_first_words, TOP_OPENINGS),
            "primeros_bigramas_ultima_oracion": _top(closing_first_bigrams, TOP_OPENINGS),
        },
        "vocabulario": {
            "contenido_top": [
                {"palabra": word, "ocurrencias": count, "por_10k": per_10k(count)}
                for word, count in sorted(vocabulary.items(), key=lambda item: (-item[1], item[0]))[
                    :TOP_VOCABULARY
                ]
            ],
        },
        "ngramas": {
            "bigramas_top": [
                {"texto": " ".join(gram), "ocurrencias": count}
                for gram, count in sorted(bigrams.items(), key=lambda item: (-item[1], item[0]))[
                    :TOP_NGRAMS
                ]
            ],
            "trigramas_contenido_top": [
                {"texto": " ".join(gram), "ocurrencias": count}
                for gram, count in sorted(
                    content_trigrams.items(), key=lambda item: (-item[1], item[0])
                )[:TOP_NGRAMS]
            ],
        },
    }
    return evidence


def _top(counter: Counter[str], limit: int) -> dict[str, int]:
    return {
        key: count
        for key, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:limit]
    }


def compute_evidence_from_corpus(
    manifest: CorpusManifest, corpus_dir: str | Path, *, manifest_path: str | Path
) -> dict[str, Any]:
    texts = read_corpus_texts(manifest, corpus_dir)
    manifest_sha256 = hashlib.sha256(Path(manifest_path).read_bytes()).hexdigest()
    return compute_style_evidence(texts, manifest_sha256=manifest_sha256)


def write_style_evidence(evidence: Mapping[str, Any], path: str | Path) -> None:
    Path(path).write_text(
        "# Generado por `colmat-x bueno-style-derive`; no editar a mano.\n"
        + yaml.safe_dump(dict(evidence), allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )


def load_style_evidence(path: str | Path = DEFAULT_EVIDENCE_PATH) -> dict[str, Any]:
    evidence_path = Path(path)
    try:
        raw = load_yaml_unique(evidence_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
        raise StyleAnalysisError(f"No se pudo leer la evidencia: {evidence_path}") from exc
    return validate_style_evidence(raw)


def validate_style_evidence(raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or any(not isinstance(key, str) for key in raw):
        raise StyleAnalysisError("La evidencia debe ser un objeto")
    expected = {
        "version",
        "manifiesto_sha256",
        "corpus",
        "oracion",
        "parrafo",
        "conectores_por_10k",
        "lexico_tecnico",
        "figuras_por_10k",
        "puntuacion_por_10k",
        "aperturas",
        "cierres",
        "vocabulario",
        "ngramas",
    }
    missing = sorted(expected - set(raw))
    unknown = sorted(set(raw) - expected)
    if missing or unknown:
        raise StyleAnalysisError(
            "La evidencia tiene campos inesperados: faltan "
            + ", ".join(missing)
            + "; sobran "
            + ", ".join(unknown)
        )
    if raw["version"] != EVIDENCE_VERSION:
        raise StyleAnalysisError("La versión de la evidencia no es compatible")
    manifest_sha256 = raw["manifiesto_sha256"]
    if (
        not isinstance(manifest_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) is None
    ):
        raise StyleAnalysisError("manifiesto_sha256 debe ser un SHA-256 hexadecimal")
    corpus = raw["corpus"]
    if not isinstance(corpus, Mapping) or any(
        isinstance(corpus.get(key), bool)
        or not isinstance(corpus.get(key), int)
        or corpus[key] <= 0
        for key in ("documentos", "palabras", "oraciones", "parrafos", "caracteres")
    ):
        raise StyleAnalysisError("corpus debe declarar conteos enteros positivos")
    for section in (
        "conectores_por_10k",
        "lexico_tecnico",
        "figuras_por_10k",
        "puntuacion_por_10k",
    ):
        if not isinstance(raw[section], Mapping) or not raw[section]:
            raise StyleAnalysisError(f"{section} debe ser un objeto no vacío")
    return dict(raw)


def resolve_evidence_key(evidence: Mapping[str, Any], dotted: str) -> Any:
    """Resuelve una referencia `seccion.clave[.subclave]` dentro de la evidencia."""

    current: Any = evidence
    for part in dotted.split("."):
        if not isinstance(current, Mapping) or part not in current:
            raise StyleAnalysisError(f"La evidencia no contiene la clave {dotted!r}")
        current = current[part]
    return current
