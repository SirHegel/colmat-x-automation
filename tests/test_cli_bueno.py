from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

import colmat_x.cli as cli_module
from colmat_x.cli import app
from colmat_x.style_analysis import compute_style_evidence, write_style_evidence
from colmat_x.style_corpus import CorpusItem, CorpusManifest, write_corpus_manifest

runner = CliRunner()
TEXT = (
    "La democracia no es un fin, sino un medio; es decir, un procedimiento. "
    "Por ejemplo, el Estado no es la «sociedad civil», sino la sociedad política. "
) * 6


def write_fake_corpus(tmp_path: Path) -> tuple[Path, Path]:
    corpus_dir = tmp_path / "corpus"
    (corpus_dir / "nodulo-catoblepas").mkdir(parents=True)
    payload = TEXT.encode("utf-8")
    (corpus_dir / "nodulo-catoblepas" / "doc.txt").write_bytes(payload)
    manifest = CorpusManifest(
        version=1,
        generated_at=datetime(2026, 9, 23, tzinfo=UTC).isoformat(),
        user_agent="colmat-x-automation/0.2",
        sources_sha256="a" * 64,
        robots=(),
        items=(
            CorpusItem(
                id="nodulo-catoblepas/doc",
                source="nodulo-catoblepas",
                url="https://nodulo.org/ec/2002/n001p03.htm",
                title="Doc",
                year=2002,
                fetched_at=datetime(2026, 9, 23, tzinfo=UTC).isoformat(),
                body_sha256="b" * 64,
                text_sha256=hashlib.sha256(payload).hexdigest(),
                characters=len(TEXT),
                words=len(TEXT.split()),
                paragraphs=1,
                path="nodulo-catoblepas/doc.txt",
            ),
        ),
        skipped=(),
    )
    manifest_path = tmp_path / "config" / "gustavo-bueno-corpus-manifest.yaml"
    manifest_path.parent.mkdir()
    write_corpus_manifest(manifest, manifest_path)
    return corpus_dir, manifest_path


def write_minimal_guide(config_dir: Path) -> Path:
    guide_path = config_dir / "estilo-gustavo-bueno.yaml"
    guide_path.write_text(
        """
version: 1
metadata:
  nombre: Guía mínima
  finalidad: Probar la CLI.
  evidencia: config/estilo-gustavo-bueno-evidencia.yaml
  manifiesto: config/gustavo-bueno-corpus-manifest.yaml
  derivada_el: "2026-09-23"
  advertencia: No reproduce textos.
norma_ortografica:
  autoridad: Real Academia Española.
  variedad: español peninsular neutro
  reglas: ["Comillas angulares «»."]
  prohibiciones: ["Toda norma distinta de la RAE."]
lexico:
  - {termino: Estado, clave: lexico_tecnico.Estado, uso: la sociedad política.}
directivas:
  - id: distinguir
    regla: Abre distinguiendo.
    evidencia: [{clave: figuras_por_10k.no X sino Y.por_10k, minimo: 1}]
    ejemplos: null
    ejemplos_en: null
limites: ["Nunca relaja cifra, fuente, 280 caracteres ni identidad visual."]
""".strip(),
        encoding="utf-8",
    )
    return guide_path


def test_help_exposes_bueno_corpus_and_style_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in ("bueno-corpus-build", "bueno-corpus-verify", "bueno-style-derive"):
        assert command in result.output


def test_corpus_verify_reports_tampering_with_nonzero_exit(tmp_path: Path) -> None:
    corpus_dir, manifest_path = write_fake_corpus(tmp_path)

    ok = runner.invoke(
        app,
        ["bueno-corpus-verify", "--corpus-dir", str(corpus_dir), "--manifest", str(manifest_path)],
    )
    assert ok.exit_code == 0
    assert json.loads(ok.output) == {"alterados": [], "faltantes": [], "ok": True, "verificados": 1}

    (corpus_dir / "nodulo-catoblepas" / "doc.txt").write_text("alterado", encoding="utf-8")
    tampered = runner.invoke(
        app,
        ["bueno-corpus-verify", "--corpus-dir", str(corpus_dir), "--manifest", str(manifest_path)],
    )
    assert tampered.exit_code == 1
    assert json.loads(tampered.output)["alterados"] == ["nodulo-catoblepas/doc.txt"]


def test_style_derive_writes_evidence_then_check_detects_drift(tmp_path: Path) -> None:
    corpus_dir, manifest_path = write_fake_corpus(tmp_path)
    config_dir = manifest_path.parent
    guide_path = write_minimal_guide(config_dir)
    evidence_path = config_dir / "estilo-gustavo-bueno-evidencia.yaml"
    common = [
        "--corpus-dir",
        str(corpus_dir),
        "--manifest",
        str(manifest_path),
        "--evidence",
        str(evidence_path),
        "--guide",
        str(guide_path),
    ]

    written = runner.invoke(app, ["bueno-style-derive", *common])
    assert written.exit_code == 0, written.output
    payload = json.loads(written.output)
    assert payload["modo"] == "write"
    assert payload["documentos"] == 1
    assert payload["manifiesto_sha256"] == hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    assert evidence_path.is_file()

    checked = runner.invoke(app, ["bueno-style-derive", *common, "--check"])
    assert checked.exit_code == 0, checked.output
    assert json.loads(checked.output)["modo"] == "check"

    drifted = compute_style_evidence(
        {"otro": TEXT + " Frase añadida al corpus."}, manifest_sha256="c" * 64
    )
    write_style_evidence(drifted, evidence_path)
    stale = runner.invoke(app, ["bueno-style-derive", *common, "--check"])
    assert stale.exit_code == 1
    assert "no coincide con el corpus" in stale.output


def test_corpus_build_never_touches_the_network_when_sources_are_invalid(
    tmp_path: Path, monkeypatch
) -> None:
    class ExplodingFetcher:
        def __init__(self) -> None:
            raise AssertionError("no debe construirse un fetcher con fuentes inválidas")

    monkeypatch.setattr(cli_module, "PoliteFetcher", ExplodingFetcher)
    sources = tmp_path / "sources.yaml"
    sources.write_text("version: 2\n", encoding="utf-8")

    result = runner.invoke(app, ["bueno-corpus-build", "--sources", str(sources)])

    assert result.exit_code == 1
    assert "Error:" in result.output
