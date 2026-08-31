from __future__ import annotations

import hashlib
import ipaddress
import math
import re
from contextlib import suppress
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

MAX_RESEARCH_SOURCES = 3
MAX_RESPONSE_BYTES = 512 * 1024
MAX_SOURCE_TEXT_CHARACTERS = 4_000
MAX_REDIRECTS = 3

# Hostnames are compared for exact equality. In particular, arbitrary subdomains of
# these institutions are not trusted merely because they share a suffix.
ALLOWED_RESEARCH_HOSTS = frozenset(
    {
        "archivogeneral.gov.co",
        "babel.banrepcultural.org",
        "banrepcultural.org",
        "bne.es",
        "catalogo.bne.es",
        "datos.bne.es",
        "enciclopedia.banrepcultural.org",
        "fgbueno.es",
        "filosofia.org",
        "helicon.es",
        "museonacional.gov.co",
        "nodulo.org",
        "www.archivogeneral.gov.co",
        "www.banrepcultural.org",
        "www.bne.es",
        "www.fgbueno.es",
        "www.filosofia.org",
        "www.helicon.es",
        "www.museonacional.gov.co",
    }
)
ALLOWED_TEXT_MIME_TYPES = frozenset({"text/html", "text/plain"})

_URL_PATTERN = re.compile(r"https://[^\s<>\[\]\"']+", re.IGNORECASE)
_CONTENT_LENGTH_PATTERN = re.compile(r"^[0-9]+$")
_CHARSET_PATTERN = re.compile(
    r"(?:^|;)\s*charset\s*=\s*[\"']?([A-Za-z0-9._-]{1,40})",
    re.IGNORECASE,
)
_TRAILING_URL_PUNCTUATION = ".,;:!?)]}"
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class ResearchFetchError(RuntimeError):
    """Una fuente no se pudo recuperar de forma segura."""


class ResearchUrlError(ResearchFetchError, ValueError):
    """Una URL no pertenece a las fuentes web expresamente autorizadas."""


class ResearchTransportError(ResearchFetchError):
    """La fuente no produjo una respuesta HTTP concluyente."""


class ResearchResponseError(ResearchFetchError):
    """La respuesta de la fuente no cumple los límites de investigación."""


@dataclass(frozen=True, slots=True)
class FetchedResearchSource:
    """Texto acotado y huella del cuerpo recibido desde una fuente oficial."""

    url: str
    text: str
    sha256: str


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._excluded_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.casefold() in {"script", "style"}:
            self._excluded_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style"} and self._excluded_depth:
            self._excluded_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._excluded_depth:
            self.parts.append(data)


def extract_research_urls(
    brief: str,
    *,
    limit: int = MAX_RESEARCH_SOURCES,
) -> tuple[str, ...]:
    """Return up to ``limit`` distinct, authorized HTTPS URLs found in a brief."""

    if not isinstance(brief, str):
        raise TypeError("brief debe ser texto")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 3:
        raise ValueError("limit debe estar entre 1 y 3")

    selected: list[str] = []
    seen: set[str] = set()
    for match in _URL_PATTERN.finditer(brief):
        candidate = match.group(0).rstrip(_TRAILING_URL_PUNCTUATION)
        normalized, _ = _validate_source_url(candidate)
        if normalized in seen:
            continue
        seen.add(normalized)
        selected.append(normalized)
        if len(selected) == limit:
            break
    return tuple(selected)


class ResearchFetcher:
    """Synchronous, bounded fetcher for evidence URLs embedded in research briefs."""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        connect_timeout_seconds: float = 5.0,
        read_timeout_seconds: float = 15.0,
    ) -> None:
        self.connect_timeout_seconds = _positive_timeout(
            connect_timeout_seconds,
            field_name="connect_timeout_seconds",
        )
        self.read_timeout_seconds = _positive_timeout(
            read_timeout_seconds,
            field_name="read_timeout_seconds",
        )
        self.session = requests.Session() if session is None else session

    def fetch_brief(self, brief: str) -> tuple[FetchedResearchSource, ...]:
        return tuple(self.fetch(url) for url in extract_research_urls(brief))

    def fetch(self, url: str) -> FetchedResearchSource:
        current_url, original_host = _validate_source_url(url)
        visited: set[str] = set()

        for redirect_count in range(MAX_REDIRECTS + 1):
            if current_url in visited:
                raise ResearchResponseError("La fuente produjo un ciclo de redirecciones")
            visited.add(current_url)
            response = self._request(current_url)
            redirect_url: str | None = None
            body_result: tuple[bytes, str, str | None] | None = None
            transport_failed = False
            try:
                status_code = response.status_code
                if status_code in _REDIRECT_STATUSES:
                    if redirect_count == MAX_REDIRECTS:
                        raise ResearchResponseError("La fuente excedió el límite de redirecciones")
                    location = response.headers.get("Location")
                    if not isinstance(location, str) or not location.strip():
                        raise ResearchResponseError("La fuente devolvió una redirección inválida")
                    redirected_url, redirected_host = _validate_source_url(
                        urljoin(current_url, location.strip())
                    )
                    if redirected_host != original_host:
                        raise ResearchResponseError(
                            "La fuente intentó redirigir hacia un host diferente"
                        )
                    redirect_url = redirected_url
                elif not isinstance(status_code, int) or not 200 <= status_code < 300:
                    raise ResearchResponseError("La fuente respondió con un estado HTTP no válido")
                else:
                    body_result = _read_bounded_body(response)
            except requests.RequestException:
                transport_failed = True
            finally:
                with suppress(Exception):
                    response.close()

            # This is deliberately raised outside the exception handler so a request URL
            # (including a sensitive query) is not retained in __context__.
            if transport_failed:
                raise ResearchTransportError("La conexión terminó antes de completar la fuente")
            if redirect_url is not None:
                current_url = redirect_url
                continue
            if body_result is None:
                raise ResearchResponseError("La fuente devolvió un cuerpo no válido")
            body, mime_type, charset = body_result
            text = _body_to_text(body, mime_type=mime_type, charset=charset)
            return FetchedResearchSource(
                url=current_url,
                text=text,
                sha256=hashlib.sha256(body).hexdigest(),
            )

        raise ResearchResponseError("La fuente excedió el límite de redirecciones")

    def _request(self, url: str) -> requests.Response:
        response: requests.Response | None = None
        with suppress(requests.RequestException):
            response = self.session.get(
                url,
                allow_redirects=False,
                headers={"Accept": "text/html, text/plain;q=0.9"},
                stream=True,
                timeout=(self.connect_timeout_seconds, self.read_timeout_seconds),
            )
        if response is None:
            raise ResearchTransportError("No se pudo obtener una respuesta de la fuente")
        return response


def _validate_source_url(url: str) -> tuple[str, str]:
    if not isinstance(url, str) or not url or any(ord(character) < 32 for character in url):
        raise ResearchUrlError("La URL de investigación no es válida")
    if "\\" in url or chr(127) in url:
        raise ResearchUrlError("La URL de investigación no es válida")
    parsed = None
    invalid_parts = False
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        invalid_parts = True
    if invalid_parts or parsed is None:
        raise ResearchUrlError("La URL de investigación no es válida")
    if parsed.scheme.casefold() != "https" or not parsed.netloc or hostname is None:
        raise ResearchUrlError("La fuente debe usar HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise ResearchUrlError("La URL de investigación no puede contener credenciales")
    if port not in {None, 443}:
        raise ResearchUrlError("La URL de investigación usa un puerto no autorizado")

    host = hostname.casefold()
    non_ascii_host = False
    try:
        host.encode("ascii")
    except UnicodeEncodeError:
        non_ascii_host = True
    if non_ascii_host:
        raise ResearchUrlError("El host de investigación no es válido")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ResearchUrlError("Las direcciones IP no son fuentes autorizadas")
    if host not in ALLOWED_RESEARCH_HOSTS:
        raise ResearchUrlError("El dominio de investigación no está autorizado")

    # Fragments are client-side only and must not become part of the fetched evidence URL.
    normalized = urlunsplit(("https", host, parsed.path or "/", parsed.query, ""))
    return normalized, host


def _read_bounded_body(response: requests.Response) -> tuple[bytes, str, str | None]:
    content_type = response.headers.get("Content-Type")
    if not isinstance(content_type, str):
        raise ResearchResponseError("La fuente no declaró un tipo de contenido permitido")
    mime_type = content_type.partition(";")[0].strip().casefold()
    if mime_type not in ALLOWED_TEXT_MIME_TYPES:
        raise ResearchResponseError("La fuente devolvió un tipo de contenido no permitido")

    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        normalized_length = content_length.strip()
        if _CONTENT_LENGTH_PATTERN.fullmatch(normalized_length) is None:
            raise ResearchResponseError("La fuente declaró un tamaño inválido")
        if int(normalized_length) > MAX_RESPONSE_BYTES:
            raise ResearchResponseError("La fuente excede el tamaño máximo permitido")

    chunks: list[bytes] = []
    received = 0
    for chunk in response.iter_content(chunk_size=16 * 1024):
        if not chunk:
            continue
        if not isinstance(chunk, bytes):
            raise ResearchResponseError("La fuente devolvió un cuerpo no válido")
        received += len(chunk)
        if received > MAX_RESPONSE_BYTES:
            raise ResearchResponseError("La fuente excede el tamaño máximo permitido")
        chunks.append(chunk)

    charset_match = _CHARSET_PATTERN.search(content_type)
    charset = charset_match.group(1) if charset_match is not None else None
    return b"".join(chunks), mime_type, charset


def _body_to_text(body: bytes, *, mime_type: str, charset: str | None) -> str:
    try:
        decoded = body.decode(charset or "utf-8", errors="replace")
    except LookupError:
        decoded = body.decode("utf-8", errors="replace")
    decoded = decoded.lstrip("\ufeff")
    if mime_type == "text/html":
        parser = _VisibleTextParser()
        parser.feed(decoded)
        parser.close()
        decoded = " ".join(parser.parts)
    normalized = " ".join(decoded.split())
    return normalized[:MAX_SOURCE_TEXT_CHARACTERS].rstrip()


def _positive_timeout(value: float, *, field_name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{field_name} debe ser un número positivo")
    return float(value)
