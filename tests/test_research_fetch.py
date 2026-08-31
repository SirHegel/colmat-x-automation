from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

import pytest
import requests

from colmat_x.research_fetch import (
    MAX_RESPONSE_BYTES,
    MAX_SOURCE_TEXT_CHARACTERS,
    ResearchFetcher,
    ResearchResponseError,
    ResearchTransportError,
    ResearchUrlError,
    extract_research_urls,
)


@dataclass
class FakeResponse:
    status_code: int = 200
    headers: dict[str, str] = field(
        default_factory=lambda: {"Content-Type": "text/plain; charset=utf-8"}
    )
    chunks: list[bytes] = field(default_factory=lambda: [b"evidence"])
    closed: bool = False

    def iter_content(self, *, chunk_size: int) -> list[bytes]:
        assert chunk_size == 16 * 1024
        return self.chunks

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, responses: list[FakeResponse | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def make_fetcher(
    responses: list[FakeResponse | Exception],
) -> tuple[ResearchFetcher, FakeSession]:
    session = FakeSession(responses)
    return (
        ResearchFetcher(
            session=session,  # type: ignore[arg-type]
            connect_timeout_seconds=2.5,
            read_timeout_seconds=7.0,
        ),
        session,
    )


def test_extracts_at_most_three_distinct_allowed_https_urls() -> None:
    brief = """
    Fuentes: https://www.fgbueno.es/gbm/gb0bibl.htm,
    https://www.fgbueno.es/gbm/gb0bibl.htm y
    https://www.museonacional.gov.co/noticias/boyaca.
    Más: https://babel.banrepcultural.org/item/7 y
    https://www.archivogeneral.gov.co/cuarta.
    """

    assert extract_research_urls(brief) == (
        "https://www.fgbueno.es/gbm/gb0bibl.htm",
        "https://www.museonacional.gov.co/noticias/boyaca",
        "https://babel.banrepcultural.org/item/7",
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://www.fgbueno.es/gbm/gb0bibl.htm",
        "https://127.0.0.1/admin",
        "https://[::1]/admin",
        "https://evil.fgbueno.es/",
        "https://www.fgbueno.es.evil.example/",
        "https://user:password@www.fgbueno.es/",
        "https://www.fgbueno.es:444/",
        "https://www.fgbueno.es\\@evil.example/",
        "https://example.com/",
    ],
)
def test_rejects_ssrf_and_non_allowlisted_url_forms_without_network(url: str) -> None:
    fetcher, session = make_fetcher([])

    with pytest.raises(ResearchUrlError):
        fetcher.fetch(url)

    assert session.calls == []


def test_fetch_uses_streaming_no_automatic_redirects_and_explicit_timeouts() -> None:
    response = FakeResponse(chunks=[b"materialismo"])
    fetcher, session = make_fetcher([response])

    result = fetcher.fetch("https://www.filosofia.org/tema.htm#fragment")

    assert result.url == "https://www.filosofia.org/tema.htm"
    assert result.text == "materialismo"
    assert session.calls == [
        {
            "url": "https://www.filosofia.org/tema.htm",
            "allow_redirects": False,
            "headers": {"Accept": "text/html, text/plain;q=0.9"},
            "stream": True,
            "timeout": (2.5, 7.0),
        }
    ]
    assert response.closed is True


def test_follows_relative_redirect_only_on_the_same_exact_host() -> None:
    redirect = FakeResponse(status_code=302, headers={"Location": "/final"}, chunks=[])
    final = FakeResponse(chunks=[b"fuente final"])
    fetcher, session = make_fetcher([redirect, final])

    result = fetcher.fetch("https://www.fgbueno.es/start")

    assert result.url == "https://www.fgbueno.es/final"
    assert result.text == "fuente final"
    assert [call["url"] for call in session.calls] == [
        "https://www.fgbueno.es/start",
        "https://www.fgbueno.es/final",
    ]
    assert redirect.closed is True
    assert final.closed is True


def test_rejects_cross_host_redirect_even_when_both_hosts_are_allowlisted() -> None:
    redirect = FakeResponse(
        status_code=301,
        headers={"Location": "https://filosofia.org/elsewhere"},
        chunks=[],
    )
    fetcher, session = make_fetcher([redirect])

    with pytest.raises(ResearchResponseError, match="host diferente"):
        fetcher.fetch("https://www.fgbueno.es/start")

    assert len(session.calls) == 1
    assert redirect.closed is True


def test_rejects_content_length_above_limit_before_reading_body() -> None:
    response = FakeResponse(
        headers={
            "Content-Type": "text/plain",
            "Content-Length": str(MAX_RESPONSE_BYTES + 1),
        },
        chunks=[b"must not be consumed"],
    )
    fetcher, _ = make_fetcher([response])

    with pytest.raises(ResearchResponseError, match="tamaño máximo"):
        fetcher.fetch("https://www.helicon.es/source")

    assert response.closed is True


def test_rejects_streamed_body_above_limit_without_content_length() -> None:
    response = FakeResponse(
        headers={"Content-Type": "text/plain"},
        chunks=[b"x" * MAX_RESPONSE_BYTES, b"y"],
    )
    fetcher, _ = make_fetcher([response])

    with pytest.raises(ResearchResponseError, match="tamaño máximo"):
        fetcher.fetch("https://datos.bne.es/source")


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Content-Type": "application/pdf"},
        {"Content-Type": "application/json"},
        {"Content-Type": "text/plain", "Content-Length": "unknown"},
    ],
)
def test_rejects_missing_or_unsupported_mime_and_invalid_length(
    headers: dict[str, str],
) -> None:
    fetcher, _ = make_fetcher([FakeResponse(headers=headers)])

    with pytest.raises(ResearchResponseError):
        fetcher.fetch("https://www.bne.es/source")


def test_html_parser_removes_script_and_style_and_normalizes_visible_text() -> None:
    body = (
        b"<html><head><style>.secret {display:none}</style>"
        b"<script>token = 'do-not-copy'</script></head>"
        b"<body><h1>Batalla &amp; territorio</h1><p>Apoyo\n popular</p></body></html>"
    )
    response = FakeResponse(
        headers={"Content-Type": "TEXT/HTML; charset=utf-8"},
        chunks=[body[:40], body[40:]],
    )
    fetcher, _ = make_fetcher([response])

    result = fetcher.fetch("https://www.museonacional.gov.co/historia")

    assert result.text == "Batalla & territorio Apoyo popular"
    assert "secret" not in result.text
    assert "token" not in result.text
    assert result.sha256 == hashlib.sha256(body).hexdigest()


def test_plain_text_is_normalized_and_truncated_to_four_thousand_characters() -> None:
    body = ("a " * 3_000).encode()
    fetcher, _ = make_fetcher([FakeResponse(chunks=[body])])

    result = fetcher.fetch("https://enciclopedia.banrepcultural.org/independencia")

    assert 0 < len(result.text) <= MAX_SOURCE_TEXT_CHARACTERS
    assert len(result.text) == MAX_SOURCE_TEXT_CHARACTERS - 1
    assert "  " not in result.text
    assert result.sha256 == hashlib.sha256(body).hexdigest()


def test_fetch_brief_returns_sources_in_brief_order() -> None:
    fetcher, _ = make_fetcher([FakeResponse(chunks=[b"uno"]), FakeResponse(chunks=[b"dos"])])

    sources = fetcher.fetch_brief(
        "Consultar https://filosofia.org/a y https://www.archivogeneral.gov.co/b"
    )

    assert [source.text for source in sources] == ["uno", "dos"]


def test_allows_exact_dispute_status_source_from_canonical_registry() -> None:
    fetcher, session = make_fetcher([FakeResponse(chunks=[b"estatuto de atribucion"])])

    result = fetcher.fetch("https://nodulo.org/ec/2010/n099p02.htm")

    assert result.text == "estatuto de atribucion"
    assert session.calls[0]["url"] == "https://nodulo.org/ec/2010/n099p02.htm"


def test_transport_exception_is_sanitized() -> None:
    secret = "super-secret-query-value"
    fetcher, _ = make_fetcher(
        [requests.Timeout(f"timeout at https://www.fgbueno.es/?token={secret}")]
    )

    with pytest.raises(ResearchTransportError) as captured:
        fetcher.fetch(f"https://www.fgbueno.es/?token={secret}")

    assert secret not in str(captured.value)
    assert "https://" not in str(captured.value)


@pytest.mark.parametrize("value", [0, -1, float("inf"), True])
def test_timeout_configuration_must_be_finite_and_positive(value: float) -> None:
    with pytest.raises(ValueError, match="número positivo"):
        ResearchFetcher(connect_timeout_seconds=value)
