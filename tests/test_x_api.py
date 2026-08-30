from __future__ import annotations

import pytest
import requests

from colmat_x.config import XCredentials
from colmat_x.x_api import (
    AmbiguousMediaError,
    AmbiguousPublishError,
    XApiClient,
    XApiError,
    XIdentityMismatchError,
)


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


def test_create_post_sends_media_and_ai_disclosure(monkeypatch) -> None:
    api = client()
    observed = {}

    def post(*args, **kwargs):
        observed.update(kwargs["json"])
        return FakeResponse(201, {"data": {"id": "123", "text": "Hola"}})

    monkeypatch.setattr(api.session, "post", post)

    api.create_post("Hola", media_ids=["987"], made_with_ai=True)

    assert observed == {
        "text": "Hola",
        "media": {"media_ids": ["987"]},
        "made_with_ai": True,
    }


def test_create_post_rejects_ai_flag_without_media() -> None:
    with pytest.raises(ValueError, match="media"):
        client().create_post("Hola", made_with_ai=True)


def test_create_post_rejects_more_than_four_media_items() -> None:
    with pytest.raises(ValueError, match="cuatro"):
        client().create_post("Hola", media_ids=["1", "2", "3", "4", "5"])


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


@pytest.mark.parametrize("status", [408, 409, 425, 500, 503])
def test_create_post_treats_uncertain_http_status_as_ambiguous(monkeypatch, status) -> None:
    api = client()
    monkeypatch.setattr(api.session, "post", lambda *args, **kwargs: FakeResponse(status, {}))

    with pytest.raises(AmbiguousPublishError, match=str(status)):
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


def test_upload_image_sets_alt_text_before_returning(monkeypatch) -> None:
    api = client()
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        if url == api.media_upload_endpoint:
            return FakeResponse(
                200,
                {"data": {"id": "777", "media_key": "3_777", "expires_after_secs": 99}},
            )
        return FakeResponse(200, {"data": {"id": "777"}})

    monkeypatch.setattr(api.session, "post", post)

    result = api.upload_image(
        b"image-bytes", filename="dato.png", mime_type="image/png", alt_text="Gráfica"
    )

    assert result.id == "777"
    assert calls[0][1]["data"] == {"media_category": "tweet_image"}
    assert calls[1][1]["json"] == {
        "id": "777",
        "metadata": {"alt_text": {"text": "Gráfica"}},
    }


def test_upload_image_blocks_when_alt_text_is_not_confirmed(monkeypatch) -> None:
    api = client()

    def post(url, **kwargs):
        if url == api.media_upload_endpoint:
            return FakeResponse(200, {"data": {"id": "777"}})
        raise requests.Timeout("late")

    monkeypatch.setattr(api.session, "post", post)

    with pytest.raises(AmbiguousMediaError, match="bloqueada"):
        api.upload_image(
            b"image-bytes", filename="dato.png", mime_type="image/png", alt_text="Gráfica"
        )


@pytest.mark.parametrize(
    ("content", "mime_type", "alt_text", "message"),
    [
        (b"", "image/png", "Gráfica", "vacía"),
        (b"ok", "image/svg+xml", "Gráfica", "JPEG"),
        (b"ok", "image/png", "", "obligatorio"),
        (b"ok", "image/png", "x" * 1001, "1000"),
    ],
)
def test_upload_image_validates_input(content, mime_type, alt_text, message) -> None:
    with pytest.raises(ValueError, match=message):
        client().upload_image(
            content,
            filename="dato.png",
            mime_type=mime_type,
            alt_text=alt_text,
        )


def test_verify_identity_checks_expected_account(monkeypatch) -> None:
    api = client()
    monkeypatch.setattr(
        api.session,
        "get",
        lambda *args, **kwargs: FakeResponse(
            200, {"data": {"id": "123", "username": "colmat", "name": "COLMAT"}}
        ),
    )

    identity = api.verify_identity(expected_user_id="123", expected_username="@Colmat")

    assert identity.username == "colmat"


def test_verify_identity_rejects_other_account(monkeypatch) -> None:
    api = client()
    monkeypatch.setattr(
        api.session,
        "get",
        lambda *args, **kwargs: FakeResponse(
            200, {"data": {"id": "123", "username": "otra", "name": "Otra"}}
        ),
    )

    with pytest.raises(XIdentityMismatchError, match="usuario esperado"):
        api.verify_identity(expected_username="colmat")
