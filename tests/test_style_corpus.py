from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from colmat_x.research_fetch import FetchedResearchDocument, ResearchFetcher, ResearchUrlError
from colmat_x.style_corpus import (
    CORPUS_USER_AGENT,
    DEFAULT_SOURCES_PATH,
    MIN_DOCUMENT_CHARACTERS,
    CorpusManifestError,
    CorpusRobotsError,
    CorpusSourcesError,
    PoliteFetcher,
    build_corpus,
    corpus_slug,
    extract_document_text,
    load_corpus_manifest,
    load_corpus_sources,
    parse_corpus_manifest,
    parse_corpus_sources,
    parse_index,
    read_corpus_texts,
    verify_corpus,
    write_corpus_manifest,
)
from colmat_x.yaml_utils import load_yaml_unique

SOURCES_PATH = Path("config/gustavo-bueno-corpus-sources.yaml")
LONG_PARAGRAPH = (
    "La filosofía no es un saber de primer grado, sino un saber de segundo grado que "
    "presupone las ciencias, las técnicas y la política; es decir, no es lo mismo la "
    "filosofía académica que la filosofía mundana. "
) * 4


@dataclass
class FakeResponse:
    status_code: int = 200
    headers: dict[str, str] = field(
        default_factory=lambda: {"Content-Type": "text/html; charset=utf-8"}
    )
    chunks: list[bytes] = field(default_factory=list)

    def iter_content(self, *, chunk_size: int) -> list[bytes]:
        return self.chunks

    def close(self) -> None:
        return None


class FakeSession:
    """Sirve rutas fijas; registra cada petición para comprobar ritmo y cabeceras."""

    def __init__(self, pages: dict[str, FakeResponse]) -> None:
        self.pages = pages
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        try:
            return self.pages[url]
        except KeyError:
            return FakeResponse(status_code=404, chunks=[b"no"])


def html_page(body: str, *, title: str = "Gustavo Bueno, Texto") -> FakeResponse:
    document = f"<html><head><title>{title}</title></head><body>{body}</body></html>"
    return FakeResponse(chunks=[document.encode("utf-8")])


def text_page(body: str, *, mime: str = "text/plain") -> FakeResponse:
    return FakeResponse(headers={"Content-Type": mime}, chunks=[body.encode("utf-8")])


def make_document(body: str, *, url: str = "https://nodulo.org/ec/2002/n001p02.htm"):
    payload = body.encode("utf-8")
    return FetchedResearchDocument(
        url=url,
        body=payload,
        mime_type="text/html",
        charset="utf-8",
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def make_polite_fetcher(
    pages: dict[str, FakeResponse], *, default_delay: float = 1.5
) -> tuple[PoliteFetcher, FakeSession, list[float]]:
    session = FakeSession(pages)
    sleeps: list[float] = []
    clock = {"now": 100.0}

    def sleep(seconds: float) -> None:
        sleeps.append(round(seconds, 3))
        clock["now"] += seconds

    fetcher = PoliteFetcher(
        fetcher=ResearchFetcher(
            session=session,  # type: ignore[arg-type]
            max_response_bytes=64 * 1024,
            max_text_characters=64 * 1024,
            user_agent=CORPUS_USER_AGENT,
        ),
        default_delay_seconds=default_delay,
        sleep=sleep,
        clock=lambda: clock["now"],
        now=lambda: datetime(2026, 9, 23, 12, 0, tzinfo=UTC),
    )
    return fetcher, session, sleeps


def sources_with_single_source(source_id: str):
    raw = load_yaml_unique(SOURCES_PATH.read_text(encoding="utf-8"))
    raw["sources"] = [item for item in raw["sources"] if item["id"] == source_id]
    return parse_corpus_sources(raw, sha256="a" * 64)


# --------------------------------------------------------------------------------------
# Fuentes declaradas
# --------------------------------------------------------------------------------------


def test_canonical_sources_only_use_allowlisted_hosts_with_declared_conditions() -> None:
    sources = load_corpus_sources(DEFAULT_SOURCES_PATH)

    assert {source.id for source in sources.sources} == {
        "nodulo-catoblepas",
        "filosofia-textos",
        "fgbueno-articulos-prensa",
    }
    assert {condition.host for condition in sources.conditions} == {
        "www.fgbueno.es",
        "www.filosofia.org",
        "nodulo.org",
    }
    filosofia = sources.source("filosofia-textos")
    assert filosofia.min_delay_seconds >= 60  # Crawl-delay declarado por robots.txt
    assert all(source.link_pattern.pattern.startswith("^https://") for source in sources.sources)
    assert "derechos" in next(
        condition.aviso_url or ""
        for condition in sources.conditions
        if condition.host == "www.filosofia.org"
    )
    assert len(sources.sha256) == 64


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda raw: raw["sources"][0].update(index_url="https://example.com/index.htm"),
            "autorizada",
        ),
        (lambda raw: raw["metadata"]["condiciones_de_uso"].pop(2), "sin condiciones de uso"),
        (lambda raw: raw["sources"][0].update(link_pattern="nodulo"), "anclarse"),
        (lambda raw: raw["sources"].append(deepcopy(raw["sources"][0])), "duplicada"),
        (lambda raw: raw["sources"][0].update(min_delay_seconds=0), "min_delay_seconds"),
        (
            lambda raw: raw["sources"][0]["tabla"].update(columna_autor=None),
            "columna_autor y autor",
        ),
        (lambda raw: raw["sources"][2].update(seccion=None), "seccion"),
        (lambda raw: raw.update(version=2), "versión"),
    ],
)
def test_sources_reject_unsafe_or_inconsistent_declarations(mutation, message: str) -> None:
    raw = load_yaml_unique(SOURCES_PATH.read_text(encoding="utf-8"))
    mutation(raw)

    with pytest.raises(CorpusSourcesError, match=message):
        parse_corpus_sources(raw)


# --------------------------------------------------------------------------------------
# Extracción
# --------------------------------------------------------------------------------------


def test_extraction_keeps_prose_and_drops_navigation_notes_and_markers() -> None:
    body = (
        "<html><head><title>Gustavo Bueno, Texto</title></head><body>"
        '<div class="nov">Separata de la revista El Catoblepas</div>'
        "<nav>Inicio · Autores</nav>"
        '<article><h1 class="ac">Título centrado</h1>'
        '<p class="ad">Entradilla editorial que no es de Bueno y debería omitirse.</p>'
        f"<p>{LONG_PARAGRAPH}Página [17] con nota{{1}} y símbolo &amp;c.</p>"
        "<p>Corto.</p><p>Encabezado sin punto final</p>"
        '<p class="nt">1. Nota al pie muy larga que tampoco debe entrar en el corpus de prosa.</p>'
        "<sup>2</sup><script>var x = 1;</script></article>"
        '<p class="ac c1 k5">© 2002 nodulo.org</p></body></html>'
    )

    extracted = extract_document_text(make_document(body))

    assert extracted.title == "Gustavo Bueno, Texto"
    assert len(extracted.paragraphs) == 2
    assert extracted.paragraphs[0].startswith("La filosofía no es un saber de primer grado")
    assert "[17]" not in extracted.paragraphs[0]
    assert "{1}" not in extracted.paragraphs[0]
    assert extracted.paragraphs[0].endswith("con nota y símbolo &c.")
    assert extracted.paragraphs[1] == "Corto."
    assert "Entradilla" not in extracted.text
    assert "Nota al pie" not in extracted.text
    assert "Separata" not in extracted.text


def test_extraction_survives_unclosed_paragraphs_of_legacy_html() -> None:
    body = (
        '<table><tr><td><p class="sm">Diario 16<p>Madrid, 15 de noviembre de 1992</td></tr></table>'
        f'<p align="justify">1. {LONG_PARAGRAPH}<p align="justify">2. {LONG_PARAGRAPH}'
    )

    extracted = extract_document_text(
        make_document(body, url="https://www.fgbueno.es/hem/1992r15.htm")
    )

    assert len(extracted.paragraphs) == 2
    assert extracted.paragraphs[0].startswith("1. La filosofía")
    assert extracted.paragraphs[1].startswith("2. La filosofía")
    assert "Madrid" not in extracted.text


def test_extraction_applies_source_specific_boilerplate_and_plain_text() -> None:
    document = FetchedResearchDocument(
        url="https://www.filosofia.org/aut/gbm/x.htm",
        body=f"Textos de Gustavo Bueno\n\n{LONG_PARAGRAPH}\n\n{LONG_PARAGRAPH}".encode(),
        mime_type="text/plain",
        charset="utf-8",
        sha256="0" * 64,
    )

    extracted = extract_document_text(document, boilerplate_patterns=("^textos de gustavo bueno",))

    assert extracted.title == ""
    assert len(extracted.paragraphs) == 2


# --------------------------------------------------------------------------------------
# Índices
# --------------------------------------------------------------------------------------


def catoblepas_row(author: str, date: str, href: str, title: str) -> str:
    return (
        f"<tr><td>{author}</td><td>{date}</td><td>1.02</td><td>ras</td>"
        f'<td><a href="{href}">{title}</a></td></tr>'
    )


def test_table_index_filters_by_author_column_link_pattern_and_year() -> None:
    sources = sources_with_single_source("nodulo-catoblepas")
    index = (
        "<table>"
        + catoblepas_row("gbm", "2002.03", "../2002/n001p03.htm", "Noetología")
        + catoblepas_row("otro", "2002.03", "../2002/n001p04.htm", "Ajeno")
        + catoblepas_row("gbm", "2003.01", "../2003/n011p02.htm#x", "Memoria")
        + catoblepas_row("gbm", "2003.02", "https://evil.example/n012p02.htm", "Fuera")
        + catoblepas_row("gbm", "2003.01", "../2003/n011p02.htm", "Repetida")
        + "</table>"
    )

    entries = parse_index(
        make_document(index, url="https://nodulo.org/ec/aut/gbm.htm"),
        sources.source("nodulo-catoblepas"),
    )

    assert [entry.url for entry in entries] == [
        "https://nodulo.org/ec/2002/n001p03.htm",
        "https://nodulo.org/ec/2003/n011p02.htm",
    ]
    assert entries[0].title == "Noetología"
    assert [entry.year for entry in entries] == [2002, 2003]


def test_section_index_takes_links_between_markers_and_year_from_path() -> None:
    sources = sources_with_single_source("fgbueno-articulos-prensa")
    index = (
        '<a href="hem/1979g07.htm">Entrevista temprana</a>'
        "<b>Artículos</b>"
        '• <a href="hem/1983c14.htm">El materialismo dialéctico</a> / El País / 14 marzo 1983<br>'
        '• <a href="gbm/gb1996mc.htm">Libro (no es hemeroteca)</a><br>'
        '• <a href="hem/2002b23.htm">Telebasura</a> / ABC / 2002<br>'
        "<b>Entrevistas</b>"
        '• <a href="hem/1993r01.htm">La igualdad es imposible</a>'
    )

    entries = parse_index(
        make_document(index, url="https://www.fgbueno.es/fgb80.htm"),
        sources.source("fgbueno-articulos-prensa"),
    )

    assert [(entry.url, entry.year) for entry in entries] == [
        ("https://www.fgbueno.es/hem/1983c14.htm", 1983),
        ("https://www.fgbueno.es/hem/2002b23.htm", 2002),
    ]


def test_section_index_without_start_marker_fails_loudly() -> None:
    sources = sources_with_single_source("fgbueno-articulos-prensa")

    with pytest.raises(CorpusSourcesError, match="no contiene la sección"):
        parse_index(
            make_document("<p>Nada</p>", url="https://www.fgbueno.es/fgb80.htm"),
            sources.source("fgbueno-articulos-prensa"),
        )


# --------------------------------------------------------------------------------------
# Descarga cortés
# --------------------------------------------------------------------------------------


def test_polite_fetcher_honours_robots_disallow_and_crawl_delay() -> None:
    pages = {
        "https://www.filosofia.org/robots.txt": text_page(
            "User-agent: *\nCrawl-delay: 60\ndisallow: /ne.htm\n"
        ),
        "https://www.filosofia.org/aut/gbm/1995qf.htm": html_page("<p>uno</p>"),
        "https://www.filosofia.org/aut/gbm/1995qc.htm": html_page("<p>dos</p>"),
    }
    fetcher, session, sleeps = make_polite_fetcher(pages)

    first = fetcher.fetch("https://www.filosofia.org/aut/gbm/1995qf.htm", min_delay_seconds=2)
    second = fetcher.fetch("https://www.filosofia.org/aut/gbm/1995qc.htm", min_delay_seconds=2)
    with pytest.raises(CorpusRobotsError, match="prohíbe"):
        fetcher.fetch("https://www.filosofia.org/ne.htm")

    assert first.mime_type == "text/html" and second.body.endswith(b"</html>")
    assert [call["url"] for call in session.calls] == [
        "https://www.filosofia.org/robots.txt",
        "https://www.filosofia.org/aut/gbm/1995qf.htm",
        "https://www.filosofia.org/aut/gbm/1995qc.htm",
    ]
    assert all(call["headers"]["User-Agent"] == CORPUS_USER_AGENT for call in session.calls)
    assert sleeps == [60.0, 60.0]
    record = fetcher.robots_records[0]
    assert record.host == "www.filosofia.org"
    assert record.crawl_delay_seconds == 60.0
    assert record.applied_delay_seconds == 60.0


def test_polite_fetcher_applies_default_delay_without_robots_and_rejects_foreign_hosts() -> None:
    pages = {
        "https://nodulo.org/ec/2002/n001p03.htm": html_page("<p>uno</p>"),
        "https://nodulo.org/ec/2002/n002p03.htm": html_page("<p>dos</p>"),
    }
    fetcher, session, sleeps = make_polite_fetcher(pages, default_delay=2.0)

    fetcher.fetch("https://nodulo.org/ec/2002/n001p03.htm")
    fetcher.fetch("https://nodulo.org/ec/2002/n002p03.htm")
    with pytest.raises(ResearchUrlError):
        fetcher.fetch("https://example.com/texto.htm")

    assert [call["url"] for call in session.calls][0] == "https://nodulo.org/robots.txt"
    assert len(session.calls) == 3
    assert sleeps == [2.0, 2.0]
    assert fetcher.robots_records[0].sha256 is None


# --------------------------------------------------------------------------------------
# Construcción, manifiesto y verificación
# --------------------------------------------------------------------------------------


def nodulo_pages() -> dict[str, FakeResponse]:
    index = (
        "<table>"
        + catoblepas_row("gbm", "2002.03", "../2002/n001p03.htm", "Noetología")
        + catoblepas_row("gbm", "2002.04", "../2002/n002p03.htm", "Breve")
        + catoblepas_row("gbm", "2002.05", "../2002/n003p02.htm", "Ausente")
        + "</table>"
    )
    return {
        "https://nodulo.org/robots.txt": text_page("User-agent: *\ndisallow: /log\n"),
        "https://nodulo.org/ec/aut/gbm.htm": html_page(index),
        "https://nodulo.org/ec/2002/n001p03.htm": html_page(
            f"<article><p>{LONG_PARAGRAPH}</p><p>{LONG_PARAGRAPH}</p></article>"
        ),
        "https://nodulo.org/ec/2002/n002p03.htm": html_page("<p>Demasiado corto para contar.</p>"),
    }


def test_build_corpus_writes_texts_outside_git_and_a_verifiable_manifest(tmp_path: Path) -> None:
    sources = sources_with_single_source("nodulo-catoblepas")
    fetcher, session, _ = make_polite_fetcher(nodulo_pages())
    corpus_dir = tmp_path / "corpus"
    manifest_path = tmp_path / "manifest.yaml"
    progress: list[str] = []

    manifest = build_corpus(
        sources,
        corpus_dir=corpus_dir,
        fetcher=fetcher,
        progress=progress.append,
        now=lambda: datetime(2026, 9, 23, 12, 0, tzinfo=UTC),
    )
    write_corpus_manifest(manifest, manifest_path)

    assert [item.id for item in manifest.items] == ["nodulo-catoblepas/ec-2002-n001p03"]
    item = manifest.items[0]
    assert item.title == "Noetología" and item.year == 2002
    assert item.path == "nodulo-catoblepas/ec-2002-n001p03.txt"
    stored = (corpus_dir / item.path).read_bytes()
    assert hashlib.sha256(stored).hexdigest() == item.text_sha256
    assert (
        item.body_sha256
        == hashlib.sha256(
            session.pages["https://nodulo.org/ec/2002/n001p03.htm"].chunks[0]
        ).hexdigest()
    )
    assert item.words > 0 and item.characters >= MIN_DOCUMENT_CHARACTERS and item.paragraphs == 2
    assert manifest.user_agent == CORPUS_USER_AGENT
    assert manifest.sources_sha256 == sources.sha256
    assert {entry["motivo"] for entry in manifest.skipped} == {
        "texto insuficiente",
        "La fuente respondió con un estado HTTP no válido",
    }
    assert manifest.robots[0].host == "nodulo.org"
    assert any("texto insuficiente" in line for line in progress)

    loaded = load_corpus_manifest(manifest_path)
    assert loaded == manifest
    assert verify_corpus(loaded, corpus_dir).ok is True
    assert read_corpus_texts(loaded, corpus_dir) == {item.id: stored.decode("utf-8")}

    (corpus_dir / item.path).write_text("texto alterado", encoding="utf-8")
    verification = verify_corpus(loaded, corpus_dir)
    assert verification.mismatched == (item.path,)
    with pytest.raises(CorpusManifestError, match="no coincide"):
        read_corpus_texts(loaded, corpus_dir)


def test_build_corpus_resumes_from_previous_manifest_without_refetching(tmp_path: Path) -> None:
    sources = sources_with_single_source("nodulo-catoblepas")
    corpus_dir = tmp_path / "corpus"
    fetcher, session, _ = make_polite_fetcher(nodulo_pages())
    first = build_corpus(sources, corpus_dir=corpus_dir, fetcher=fetcher)

    fetcher2, session2, _ = make_polite_fetcher(nodulo_pages())
    second = build_corpus(sources, corpus_dir=corpus_dir, fetcher=fetcher2, previous=first)

    assert second.items == first.items
    assert "https://nodulo.org/ec/2002/n001p03.htm" not in [call["url"] for call in session2.calls]
    assert "https://nodulo.org/ec/2002/n001p03.htm" in [call["url"] for call in session.calls]

    with pytest.raises(CorpusSourcesError, match="desconocidas"):
        build_corpus(sources, corpus_dir=corpus_dir, fetcher=fetcher2, only_sources=["otra"])


def test_manifest_rejects_tampering_and_unsafe_paths(tmp_path: Path) -> None:
    sources = sources_with_single_source("nodulo-catoblepas")
    fetcher, _, _ = make_polite_fetcher(nodulo_pages())
    manifest = build_corpus(sources, corpus_dir=tmp_path / "corpus", fetcher=fetcher)
    raw = manifest.to_mapping()

    broken_totals = deepcopy(raw)
    broken_totals["totales"]["palabras"] += 1
    with pytest.raises(CorpusManifestError, match="totales"):
        parse_corpus_manifest(broken_totals)

    bad_hash = deepcopy(raw)
    bad_hash["items"][0]["text_sha256"] = "zz"
    with pytest.raises(CorpusManifestError, match="SHA-256"):
        parse_corpus_manifest(bad_hash)

    traversal = deepcopy(raw)
    traversal["items"][0]["path"] = "../fuera.txt"
    with pytest.raises(CorpusManifestError, match="ruta relativa"):
        parse_corpus_manifest(traversal)

    foreign = deepcopy(raw)
    foreign["items"][0]["url"] = "https://example.com/texto.htm"
    with pytest.raises(CorpusManifestError, match="autorizada"):
        parse_corpus_manifest(foreign)


def test_corpus_slug_is_ascii_and_stable() -> None:
    assert corpus_slug("https://www.filosofia.org/rev/bas/bas10101.htm") == "rev-bas-bas10101"
    assert corpus_slug("https://nodulo.org/ec/2002/n001p03.htm#ras") == "ec-2002-n001p03"
    assert corpus_slug("https://www.fgbueno.es/") == "documento"
