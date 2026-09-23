from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

import colmat_x.style_guide as style_guide_module
from colmat_x.style_analysis import compute_style_evidence, write_style_evidence
from colmat_x.style_guide import (
    DEFAULT_STYLE_GUIDE_PATH,
    MAX_STYLE_PROMPT_CHARACTERS,
    StyleGuide,
    StyleGuideError,
    load_gustavo_bueno_style,
    load_style_guide,
)
from colmat_x.yaml_utils import load_yaml_unique

SYNTHETIC_TEXT = (
    "La democracia no es un fin, sino un medio; es decir, un procedimiento. "
    "Por ejemplo, el Estado no es la «sociedad civil», sino la sociedad política. "
    "Acaso el llamado «consenso» sea un mito; por tanto, cabe distinguir. "
) * 4 + "Hay symploké entre las partes."  # «symploké» queda por debajo del mínimo léxico


@pytest.fixture
def guide_bundle(tmp_path: Path) -> tuple[Path, dict]:
    """Guía mínima, evidencia y manifiesto coherentes entre sí en un directorio temporal."""

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    manifest_path = config_dir / "gustavo-bueno-corpus-manifest.yaml"
    manifest_path.write_text("version: 1\nitems: []\n", encoding="utf-8")
    evidence = compute_style_evidence(
        {"a": SYNTHETIC_TEXT, "b": SYNTHETIC_TEXT},
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    )
    write_style_evidence(evidence, config_dir / "estilo-gustavo-bueno-evidencia.yaml")
    guide = {
        "version": 1,
        "metadata": {
            "nombre": "Guía mínima",
            "finalidad": "Probar el cargador.",
            "evidencia": "config/estilo-gustavo-bueno-evidencia.yaml",
            "manifiesto": "config/gustavo-bueno-corpus-manifest.yaml",
            "derivada_el": "2026-09-23",
            "advertencia": "No reproduce textos.",
        },
        "norma_ortografica": {
            "autoridad": "Real Academia Española (Ortografía 2010).",
            "variedad": "español peninsular neutro",
            "reglas": ["Comillas angulares «» como primera opción."],
            "prohibiciones": ["Cualquier norma distinta de la RAE."],
        },
        "lexico": [
            {
                "termino": "Estado",
                "clave": "lexico_tecnico.Estado",
                "uso": "la sociedad política.",
            }
        ],
        "directivas": [
            {
                "id": "distinguir",
                "regla": "Abre distinguiendo con «no… sino».",
                "evidencia": [{"clave": "figuras_por_10k.no X sino Y.por_10k", "minimo": 1}],
                "ejemplos": ["es decir", "por ejemplo"],
                "ejemplos_en": "conectores_por_10k",
            }
        ],
        "limites": [
            "Modula el registro; nunca relaja cifra, fuente, el máximo de 280 caracteres "
            "ni la identidad visual."
        ],
    }
    guide_path = config_dir / "estilo-gustavo-bueno.yaml"
    guide_path.write_text(
        yaml.safe_dump(guide, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return guide_path, guide


def write_guide(guide_path: Path, guide: dict) -> Path:
    guide_path.write_text(
        yaml.safe_dump(guide, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return guide_path


def test_minimal_guide_loads_and_renders_bounded_prompt(guide_bundle) -> None:
    guide_path, _ = guide_bundle

    guide = load_style_guide(guide_path)

    assert isinstance(guide, StyleGuide)
    assert guide.corpus_documents == 2
    assert guide.lexicon[0].term == "Estado"
    assert guide.directives[0].examples == ("es decir", "por ejemplo")
    prompt = guide.generation_prompt()
    assert prompt.startswith("Registro de redacción")
    assert "Real Academia Española" in prompt
    assert "Prohibido: Cualquier norma distinta de la RAE." in prompt
    assert "«es decir»" in prompt
    assert "280" in prompt
    assert len(prompt) <= MAX_STYLE_PROMPT_CHARACTERS


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda g: g.update(version=2), "versión"),
        (
            lambda g: g["directivas"][0]["evidencia"][0].update(
                clave="figuras_por_10k.inventada.por_10k"
            ),
            "no contiene la clave",
        ),
        (lambda g: g["directivas"][0]["evidencia"][0].update(minimo=10_000), "por debajo"),
        (
            lambda g: g["directivas"][0].update(ejemplos=["conector inventado"]),
            "no aparece en la evidencia",
        ),
        (lambda g: g["directivas"][0].update(evidencia=[]), "al menos una clave"),
        (lambda g: g["lexico"][0].update(clave="lexico_tecnico.symploké"), "menos de"),
        (lambda g: g["lexico"][0].update(clave="conectores_por_10k.es decir"), "lexico_tecnico"),
        (
            lambda g: g["norma_ortografica"].update(autoridad="Cambridge Style Guide"),
            "Real Academia",
        ),
        (lambda g: g["norma_ortografica"].update(prohibiciones=["Nada."]), "distinta de la RAE"),
        (lambda g: g.update(limites=["Solo modula el registro."]), "cifra"),
        (lambda g: g["metadata"].update(evidencia="../fuera.yaml"), "bajo config/"),
        (lambda g: g.update(extra=True), "desconocidos"),
    ],
)
def test_guide_rejects_claims_without_evidence_or_outside_rae(
    guide_bundle, mutation, message: str
) -> None:
    guide_path, guide = guide_bundle
    broken = deepcopy(guide)
    mutation(broken)
    write_guide(guide_path, broken)

    with pytest.raises(StyleGuideError, match=message):
        load_style_guide(guide_path)


def test_guide_is_bound_to_the_versioned_manifest_hash(guide_bundle) -> None:
    guide_path, _ = guide_bundle
    manifest_path = guide_path.parent / "gustavo-bueno-corpus-manifest.yaml"
    manifest_path.write_text("version: 1\nitems: [otro]\n", encoding="utf-8")

    with pytest.raises(StyleGuideError, match="no corresponde al manifiesto"):
        load_style_guide(guide_path)


def test_guide_rejects_duplicate_yaml_keys(guide_bundle) -> None:
    guide_path, _ = guide_bundle
    guide_path.write_text(
        guide_path.read_text(encoding="utf-8") + "\nversion: 1\n", encoding="utf-8"
    )

    with pytest.raises(StyleGuideError, match="No se pudo leer"):
        load_style_guide(guide_path)


def test_prompt_length_is_capped(guide_bundle) -> None:
    guide_path, guide = guide_bundle
    padded = deepcopy(guide)
    padded["norma_ortografica"]["reglas"] = [f"Regla {index} " + "x" * 900 for index in range(8)]
    write_guide(guide_path, padded)

    with pytest.raises(StyleGuideError, match="máximo"):
        load_style_guide(guide_path)


def test_canonical_guide_is_backed_by_the_versioned_corpus_evidence() -> None:
    load_gustavo_bueno_style.cache_clear()
    try:
        guide = load_gustavo_bueno_style()
    finally:
        load_gustavo_bueno_style.cache_clear()
    raw = load_yaml_unique(DEFAULT_STYLE_GUIDE_PATH.read_text(encoding="utf-8"))

    assert guide.corpus_documents >= 150
    assert guide.corpus_words >= 1_000_000
    assert (
        guide.manifest_sha256
        == hashlib.sha256(
            Path("config/gustavo-bueno-corpus-manifest.yaml").read_bytes()
        ).hexdigest()
    )
    assert {entry.term for entry in guide.lexicon} >= {"Idea", "concepto", "Estado", "symploké"}
    assert {directive.id for directive in guide.directives} >= {
        "distinguir-antes-de-afirmar",
        "reformular-y-ejemplificar",
        "ironia-por-comillas",
        "critica-como-trituracion",
        "puntuacion-densa",
    }
    assert all(directive.evidence for directive in guide.directives)
    assert "Real Academia Española" in guide.norm.authority
    assert any("Cambridge" in item for item in guide.norm.prohibitions)
    assert any("&c." in item and "etc." in item for item in guide.limits)
    assert len(raw["directivas"]) == len(guide.directives)
    assert len(guide.generation_prompt()) <= MAX_STYLE_PROMPT_CHARACTERS


def test_packaged_resource_fallback_serves_the_three_style_files(
    monkeypatch, tmp_path: Path
) -> None:
    payloads = {
        name: Path("config", name).read_bytes()
        for name in (
            "estilo-gustavo-bueno.yaml",
            "estilo-gustavo-bueno-evidencia.yaml",
            "gustavo-bueno-corpus-manifest.yaml",
        )
    }

    class PackagedResource:
        def __init__(self, name: str = "") -> None:
            self.name = name

        def joinpath(self, *parts: str) -> PackagedResource:
            return PackagedResource(parts[-1])

        def read_bytes(self) -> bytes:
            return payloads[self.name]

    monkeypatch.setattr(style_guide_module, "_checkout_config_path", lambda name: tmp_path / name)
    monkeypatch.setattr(style_guide_module.resources, "files", lambda _package: PackagedResource())
    load_gustavo_bueno_style.cache_clear()
    try:
        packaged = load_gustavo_bueno_style()
    finally:
        load_gustavo_bueno_style.cache_clear()

    assert packaged.corpus_documents >= 150
