from __future__ import annotations

import pytest
import requests

from colmat_x.config import XCredentials
from colmat_x.x_api import AmbiguousPublishError, XApiClient, XApiError


class FakeResponse:
    def __init__(self, status_code, payload=None, text="", headers=None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def client() -> XApiClient:
    return XApiClient(
        XCredentials(
            consumer_key="key",
            consumer_secret="secret",
            access_token="token",
            access_token_secret="secret",
        )
    )


def test_create_post_reads_201_response(monkeypatch) -> None:
    api = client()
    monkeypatch.setattr(
        api.session,
        "post",
        lambda *args, **kwargs: FakeResponse(201, {"data": {"id": "123", "text": "Hola"}}),
    )

    result = api.create_post("Hola")

    assert result.id == "123"


def test_create_post_reports_definite_4xx(monkeypatch) -> None:
    api = client()
    monkeypatch.setattr(
        api.session,
        "post",
        lambda *args, **kwargs: FakeResponse(401, {"detail": "Unauthorized"}),
    )

    with pytest.raises(XApiError, match="401"):
        api.create_post("Hola")


def test_create_post_treats_timeout_as_ambiguous(monkeypatch) -> None:
    api = client()

    def timeout(*args, **kwargs):
        raise requests.Timeout("late")

    monkeypatch.setattr(api.session, "post", timeout)

    with pytest.raises(AmbiguousPublishError, match="revisa la cuenta"):
        api.create_post("Hola")


def test_create_post_treats_broken_response_body_as_ambiguous(monkeypatch) -> None:
    api = client()

    def broken_body(*args, **kwargs):
        raise requests.exceptions.ChunkedEncodingError("response ended early")

    monkeypatch.setattr(api.session, "post", broken_body)

    with pytest.raises(AmbiguousPublishError, match="revisa la cuenta"):
        api.create_post("Hola")


def test_create_post_rejects_null_success_id_as_ambiguous(monkeypatch) -> None:
    api = client()
    monkeypatch.setattr(
        api.session,
        "post",
        lambda *args, **kwargs: FakeResponse(201, {"data": {"id": None, "text": "Hola"}}),
    )

    with pytest.raises(AmbiguousPublishError, match="leer el ID"):
        api.create_post("Hola")


@pytest.mark.parametrize("invalid_x_id", ["0123", "１２３", "0", "1" * 21])
def test_create_post_rejects_noncanonical_success_id_as_ambiguous(
    monkeypatch, invalid_x_id: str
) -> None:
    api = client()
    monkeypatch.setattr(
        api.session,
        "post",
        lambda *args, **kwargs: FakeResponse(
            201,
            {"data": {"id": invalid_x_id, "text": "Hola"}},
        ),
    )

    with pytest.raises(AmbiguousPublishError, match="leer el ID"):
        api.create_post("Hola")
