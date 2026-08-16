from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests
from requests_oauthlib import OAuth1Session

from colmat_x.config import XCredentials
from colmat_x.domain import is_canonical_x_post_id


class XApiError(RuntimeError):
    """X rechazó la solicitud y no creó la publicación."""


class XApiRateLimitError(XApiError):
    """X rechazó la solicitud por límite de uso."""


class AmbiguousPublishError(RuntimeError):
    """No es seguro afirmar si X alcanzó a crear la publicación."""


@dataclass(frozen=True)
class XPostResponse:
    id: str
    text: str


class XApiClient:
    def __init__(self, credentials: XCredentials, *, timeout_seconds: float = 20.0) -> None:
        self.endpoint = "https://api.x.com/2/tweets"
        self.timeout_seconds = timeout_seconds
        self.session = OAuth1Session(
            client_key=credentials.consumer_key,
            client_secret=credentials.consumer_secret,
            resource_owner_key=credentials.access_token,
            resource_owner_secret=credentials.access_token_secret,
        )

    def create_post(self, text: str) -> XPostResponse:
        try:
            response = self.session.post(
                self.endpoint,
                json={"text": text},
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise AmbiguousPublishError(
                "La conexión terminó sin una respuesta concluyente; revisa la cuenta de X "
                "antes de reintentar"
            ) from exc

        if response.status_code == 429:
            reset = response.headers.get("x-rate-limit-reset")
            detail = _error_detail(response)
            suffix = f"; reset={reset}" if reset else ""
            raise XApiRateLimitError(f"X respondió 429: {detail}{suffix}")
        if response.status_code >= 500:
            raise AmbiguousPublishError(
                f"X respondió {response.status_code}; revisa la cuenta antes de reintentar"
            )
        if response.status_code != 201:
            raise XApiError(f"X respondió {response.status_code}: {_error_detail(response)}")

        try:
            payload: Any = response.json()
            data = payload["data"]
            raw_post_id = data["id"]
            if not is_canonical_x_post_id(raw_post_id):
                raise TypeError("data.id no tiene el formato decimal canónico")
            post_id = raw_post_id
            raw_returned_text = data.get("text")
            returned_text = raw_returned_text if isinstance(raw_returned_text, str) else text
        except (ValueError, KeyError, TypeError) as exc:
            raise AmbiguousPublishError(
                "X respondió con éxito, pero no fue posible leer el ID de la publicación"
            ) from exc
        return XPostResponse(id=post_id, text=returned_text)


def _error_detail(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip()[:500] or "sin detalle"
    if isinstance(payload, dict):
        detail = payload.get("detail") or payload.get("title")
        if detail:
            return str(detail)[:500]
        errors = payload.get("errors")
        if errors:
            return str(errors)[:500]
    return str(payload)[:500]
