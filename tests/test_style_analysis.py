from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from colmat_x.style_analysis import (
    StyleAnalysisError,
    compute_style_evidence,
    load_style_evidence,
    resolve_evidence_key,
    split_sentences,
    tokenize_words,
    validate_style_evidence,
    write_style_evidence,
)

DOC_A = (
    "La democracia no es un fin, sino un medio; es decir, un procedimiento. "
    "¿Qué es entonces la Idea de Estado? Por ejemplo, el Estado no es la «sociedad civil». "
    "Acaso el llamado «consenso» sea un mito, &c.\n\n"
    "2. Por tanto, cabe distinguir tres acepciones (1) política, (2) económica y (3) moral. "
    "Pero la distinción decisiva es otra."
)
DOC_B = (
    "El materialismo filosófico distingue entre ideas y conceptos. "
    "Sin embargo, la Idea de Cultura no es un concepto categorial, sino una Idea. "
    "Precisamente por ello cabría hablar de symploké: no todo está conectado con todo."
)


@pytest.fixture
def evidence() -> dict:
    return compute_style_evidence({"b": DOC_B, "a": DOC_A}, manifest_sha256="f" * 64)


def test_tokenizer_and_sentence_splitter_follow_spanish_punctuation() -> None:
    assert tokenize_words("¿Qué es la «Idea»? Sí, &c.") == ["Qué", "es", "la", "Idea", "Sí", "c"]
    sentences = split_sentences(DOC_A)
    assert sentences[0] == "La democracia no es un fin, sino un medio; es decir, un procedimiento."
    assert sentences[1].startswith("¿Qué es entonces")
    assert len(sentences) == 6


def test_evidence_counts_are_exact_and_deterministic(evidence: dict) -> None:
    assert evidence["version"] == 1
    assert evidence["manifiesto_sha256"] == "f" * 64
    assert evidence["corpus"]["documentos"] == 2
    assert evidence["corpus"]["oraciones"] == 9
    assert evidence["corpus"]["parrafos"] == 3
    assert evidence["corpus"]["palabras"] == len(tokenize_words(DOC_A)) + len(tokenize_words(DOC_B))

    connectors = evidence["conectores_por_10k"]
    assert connectors["es decir"]["ocurrencias"] == 1
    assert connectors["por ejemplo"]["documentos_pct"] == 50.0
    assert connectors["&c."]["ocurrencias"] == 1
    assert connectors["cabría / cabe"]["ocurrencias"] == 2

    lexicon = evidence["lexico_tecnico"]
    assert lexicon["Idea (mayúscula)"]["ocurrencias"] == 3
    assert lexicon["idea (minúscula)"]["ocurrencias"] == 1
    assert lexicon["Estado"]["ocurrencias"] == 2
    assert lexicon["symploké"]["ocurrencias"] == 1
    assert lexicon["materialismo filosófico"]["documentos_pct"] == 50.0

    figures = evidence["figuras_por_10k"]
    assert figures["no X sino Y"]["ocurrencias"] == 2
    assert figures["comillas irónicas de una palabra"]["ocurrencias"] == 1
    assert figures["enumeración numerada (1) (2)"]["ocurrencias"] == 3
    assert figures["los llamados / la llamada"]["ocurrencias"] == 1

    assert evidence["oracion"]["interrogativas_pct"] == pytest.approx(100 / 9, abs=0.01)
    assert evidence["aperturas"]["pregunta_inicial_pct"] == 0.0
    assert evidence["cierres"]["primeras_palabras_ultima_oracion"] == {"pero": 1, "precisamente": 1}
    assert evidence["parrafo"]["numerados_pct"] == pytest.approx(100 / 3, abs=0.01)
    assert evidence["puntuacion_por_10k"]["comillas angulares «»"] > 0
    assert evidence["vocabulario"]["contenido_top"][0]["palabra"] == "idea"
    assert evidence == compute_style_evidence({"a": DOC_A, "b": DOC_B}, manifest_sha256="f" * 64)


def test_evidence_round_trips_through_yaml_and_validates(tmp_path: Path, evidence: dict) -> None:
    path = tmp_path / "evidencia.yaml"
    write_style_evidence(evidence, path)

    loaded = load_style_evidence(path)

    assert loaded["corpus"] == evidence["corpus"]
    assert loaded["conectores_por_10k"]["es decir"] == evidence["conectores_por_10k"]["es decir"]
    assert path.read_text(encoding="utf-8").startswith("# Generado por")
    assert resolve_evidence_key(loaded, "lexico_tecnico.Idea (mayúscula).ocurrencias") == 3
    with pytest.raises(StyleAnalysisError, match="no contiene"):
        resolve_evidence_key(loaded, "lexico_tecnico.inexistente.ocurrencias")


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda raw: raw.update(version=2), "versión"),
        (lambda raw: raw.update(manifiesto_sha256="abc"), "SHA-256"),
        (lambda raw: raw.pop("cierres"), "faltan"),
        (lambda raw: raw.update(extra=1), "sobran"),
        (lambda raw: raw["corpus"].update(palabras=0), "positivos"),
        (lambda raw: raw.update(conectores_por_10k={}), "no vacío"),
    ],
)
def test_evidence_validation_rejects_malformed_documents(
    evidence: dict, mutation, message: str
) -> None:
    raw = deepcopy(evidence)
    mutation(raw)

    with pytest.raises(StyleAnalysisError, match=message):
        validate_style_evidence(raw)


def test_empty_corpus_is_rejected() -> None:
    with pytest.raises(StyleAnalysisError, match="vacío"):
        compute_style_evidence({})
    with pytest.raises(StyleAnalysisError, match="analizable"):
        compute_style_evidence({"a": "   "})
