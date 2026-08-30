from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
import requests

from colmat_x.editorial import DRAFT_TOOL_NAME, load_editorial_policy
from colmat_x.minimax import (
    IMAGE_ASPECT_RATIO_DIMENSIONS,
    MINIMAX_CHAT_COMPLETIONS_ENDPOINT,
    MINIMAX_IMAGE_GENERATION_ENDPOINT,
    MiniMaxAPIError,
    MiniMaxClient,
    MiniMaxConfigurationError,
    MiniMaxResponseError,
    MiniMaxTransportError,
)

POLICY_PATH = Path("config/editorial-policy.yaml")
ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class RecordingSession:
    def __init__(self, response=None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict] = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self.error is not None:
            raise self.error
        return self.response


class SequenceSession(RecordingSession):
    def __init__(self, *responses: FakeResponse) -> None:
        super().__init__()
        self.responses = list(responses)

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)


@pytest.fixture
def policy():
    return load_editorial_policy(POLICY_PATH)


@pytest.fixture
def draft_payload() -> dict:
    return {
        "categoria": "dato_semana",
        "institucion": "colmat",
        "texto": "El dato es 10 %. Fuente: DANE 2026.",
        "cifra": "10 %",
        "fuente": "DANE 2026",
        "visual": {
            "tipo": "tipografica",
            "descripcion": "Cifra central sobre fondo azul.",
            "colores": ["azul_cortical", "tinta"],
            "tipografia": "Arial",
            "incluye_retrato_persona_viva": False,
            "usa_simbolos": False,
            "serie_completa": False,
            "eje_truncado": False,
        },
    }


def tool_response(arguments: str) -> dict:
    return {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "type": "function",
                            "function": {
                                "name": DRAFT_TOOL_NAME,
                                "arguments": arguments,
                            },
                        }
                    ],
                },
            }
        ],
        "base_resp": {"status_code": 0, "status_msg": "success"},
    }


def make_client(monkeypatch, session: RecordingSession, **kwargs) -> MiniMaxClient:
    monkeypatch.setenv("MINIMAX_API_KEY", "test-secret-from-env")
    return MiniMaxClient(session=session, **kwargs)


def test_api_key_is_required_from_environment(monkeypatch) -> None:
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)

    with pytest.raises(MiniMaxConfigurationError, match="MINIMAX_API_KEY"):
        MiniMaxClient(session=RecordingSession())
    with pytest.raises(TypeError):
        MiniMaxClient(api_key="forbidden")  # type: ignore[call-arg]


def test_generate_draft_uses_official_endpoint_and_closed_tool(
    monkeypatch, policy, draft_payload
) -> None:
    response = FakeResponse(200, tool_response(json.dumps(draft_payload)))
    session = RecordingSession(response)
    client = make_client(
        monkeypatch,
        session,
        model="MiniMax-M2.7",
        connect_timeout_seconds=3,
        read_timeout_seconds=17,
    )

    draft = client.generate_draft(
        "Usa el dato entregado.",
        policy,
        category="dato_semana",
        institution="colmat",
    )

    assert draft.publication_authorized is False
    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["url"] == MINIMAX_CHAT_COMPLETIONS_ENDPOINT
    assert call["headers"]["Authorization"] == "Bearer test-secret-from-env"
    assert call["timeout"] == (3.0, 17.0)
    assert call["json"]["stream"] is False
    assert call["json"]["tools"][0]["function"]["parameters"]["additionalProperties"] is False
    assert call["json"]["tool_choice"] == {
        "type": "function",
        "function": {"name": DRAFT_TOOL_NAME},
    }
    assert "visual.tipo='ninguna'" in call["json"]["messages"][0]["content"]


def test_generate_draft_repairs_one_locally_invalid_tool_call(
    monkeypatch, policy, draft_payload
) -> None:
    invalid_payload = json.loads(json.dumps(draft_payload))
    invalid_payload["visual"]["tipo"] = "ninguna"
    first = tool_response(json.dumps(invalid_payload))
    first_message = first["choices"][0]["message"]
    first_message["tool_calls"][0]["id"] = "call-invalid-1"
    first_message["reasoning_details"] = [{"type": "reasoning.text", "text": "revisión"}]
    second = tool_response(json.dumps(draft_payload))
    session = SequenceSession(FakeResponse(200, first), FakeResponse(200, second))
    client = make_client(monkeypatch, session)

    draft = client.generate_draft("Dato entregado", policy)

    assert draft.figure == "10 %"
    assert len(session.calls) == 2
    repair_messages = session.calls[1]["json"]["messages"]
    assert repair_messages[-2] == first_message
    assert repair_messages[-1]["role"] == "tool"
    assert repair_messages[-1]["tool_call_id"] == "call-invalid-1"
    feedback = json.loads(repair_messages[-1]["content"])
    assert feedback["accepted"] is False
    assert "colores=[]" in feedback["validation_error"]
    assert feedback["texto_debe_contener_literalmente"] == {
        "cifra": draft_payload["cifra"],
        "fuente": draft_payload["fuente"],
    }


def test_generate_draft_repairs_malformed_tool_arguments(
    monkeypatch, policy, draft_payload
) -> None:
    first = tool_response('{"categoria":"dato_semana",')
    first["choices"][0]["message"]["tool_calls"][0]["id"] = "call-broken-json"
    second = tool_response(json.dumps(draft_payload))
    session = SequenceSession(FakeResponse(200, first), FakeResponse(200, second))
    client = make_client(monkeypatch, session)

    draft = client.generate_draft("Dato entregado", policy)

    assert draft.figure == "10 %"
    feedback = json.loads(session.calls[1]["json"]["messages"][-1]["content"])
    assert "JSON inválidos" in feedback["validation_error"]


def test_generate_draft_repairs_without_inventing_a_missing_tool_call_id(
    monkeypatch, policy, draft_payload
) -> None:
    invalid_payload = json.loads(json.dumps(draft_payload))
    invalid_payload["visual"]["tipo"] = "ninguna"
    first = tool_response(json.dumps(invalid_payload))
    second = tool_response(json.dumps(draft_payload))
    session = SequenceSession(FakeResponse(200, first), FakeResponse(200, second))
    client = make_client(monkeypatch, session)

    assert client.generate_draft("Dato entregado", policy).figure == "10 %"
    repair_message = session.calls[1]["json"]["messages"][-1]
    assert repair_message["role"] == "user"
    assert "colores=[]" in repair_message["content"]
    assert "tool_call_id" not in repair_message


def test_generate_draft_fails_closed_after_one_repair(monkeypatch, policy, draft_payload) -> None:
    invalid_payload = json.loads(json.dumps(draft_payload))
    invalid_payload["visual"]["tipo"] = "ninguna"
    first = tool_response(json.dumps(invalid_payload))
    first["choices"][0]["message"]["tool_calls"][0]["id"] = "call-invalid-1"
    second = tool_response(json.dumps(invalid_payload))
    session = SequenceSession(FakeResponse(200, first), FakeResponse(200, second))
    client = make_client(monkeypatch, session)

    with pytest.raises(MiniMaxResponseError, match=r"colores=\[\]"):
        client.generate_draft("Dato entregado", policy)

    assert len(session.calls) == 2


def test_generate_draft_accepts_strict_fenced_json_fallback(
    monkeypatch, policy, draft_payload
) -> None:
    content = (
        f"<think>razonamiento separado tardío</think>\n```json\n{json.dumps(draft_payload)}\n```"
    )
    response = FakeResponse(
        200,
        {
            "choices": [{"finish_reason": "stop", "message": {"content": content}}],
            "base_resp": {"status_code": 0},
        },
    )
    client = make_client(monkeypatch, RecordingSession(response))

    assert client.generate_draft("Dato entregado", policy).figure == "10 %"


def test_generate_draft_reports_plain_text_refusal_without_reflecting_it(
    monkeypatch, policy
) -> None:
    refusal = "No puedo crear la pieza sin una cifra y una fuente verificables."
    response = FakeResponse(
        200,
        {
            "choices": [{"finish_reason": "stop", "message": {"content": refusal}}],
            "base_resp": {"status_code": 0},
        },
    )
    client = make_client(monkeypatch, RecordingSession(response))

    with pytest.raises(MiniMaxResponseError, match="llamada de herramienta requerida") as captured:
        client.generate_draft("Encargo conceptual sin evidencia", policy)

    assert refusal not in str(captured.value)


def test_generate_draft_rejects_duplicate_json_keys(monkeypatch, policy, draft_payload) -> None:
    raw = json.dumps(draft_payload)
    raw = raw[:-1] + ', "cifra": "11 %"}'
    client = make_client(monkeypatch, RecordingSession(FakeResponse(200, tool_response(raw))))

    with pytest.raises(MiniMaxResponseError, match="JSON inválidos"):
        client.generate_draft("Dato", policy)


def test_generate_draft_rejects_unvalidated_output(monkeypatch, policy, draft_payload) -> None:
    draft_payload["publicar"] = True
    client = make_client(
        monkeypatch,
        RecordingSession(FakeResponse(200, tool_response(json.dumps(draft_payload)))),
    )

    with pytest.raises(MiniMaxResponseError, match="borrador inválido"):
        client.generate_draft("Dato", policy)


def test_clear_http_and_transport_errors(monkeypatch, policy) -> None:
    unauthorized = make_client(
        monkeypatch,
        RecordingSession(FakeResponse(401, {"message": "bad key"})),
    )
    with pytest.raises(MiniMaxAPIError, match="autenticación"):
        unauthorized.generate_draft("Dato", policy)

    timeout = make_client(
        monkeypatch,
        RecordingSession(error=requests.Timeout("late")),
    )
    with pytest.raises(MiniMaxTransportError, match="tiempo de espera") as captured:
        timeout.generate_draft("Dato", policy)
    assert captured.value.__context__ is None


def test_base_response_error_is_not_treated_as_success(monkeypatch, policy) -> None:
    response = FakeResponse(
        200,
        {"base_resp": {"status_code": 1004, "status_msg": "insufficient balance"}},
    )
    client = make_client(monkeypatch, RecordingSession(response))

    with pytest.raises(MiniMaxAPIError, match="1004"):
        client.generate_draft("Dato", policy)


def test_base_response_never_echoes_environment_secret(monkeypatch, policy) -> None:
    response = FakeResponse(
        200,
        {
            "base_resp": {
                "status_code": 1004,
                "status_msg": "rejected test-secret-from-env",
            }
        },
    )
    client = make_client(monkeypatch, RecordingSession(response))

    with pytest.raises(MiniMaxAPIError) as captured:
        client.generate_draft("Dato", policy)
    assert "test-secret-from-env" not in str(captured.value)
    assert "[REDACTADO]" in str(captured.value)


def test_generate_image_returns_memory_bytes_mime_and_hash(monkeypatch, policy) -> None:
    response = FakeResponse(
        200,
        {
            "id": "image-request-1",
            "data": {"image_base64": [base64.b64encode(ONE_PIXEL_PNG).decode("ascii")]},
            "metadata": {"failed_count": "0", "success_count": "1"},
            "base_resp": {"status_code": 0},
        },
    )
    session = RecordingSession(response)
    client = make_client(monkeypatch, session, image_model="image-01")

    image = client.generate_image(
        "Gráfica territorial abstracta y sobria.",
        policy,
        size="16:9",
        seed=7,
        alt_text="Gráfica del territorio colombiano en tonos sobrios.",
    )

    assert image.bytes == ONE_PIXEL_PNG
    assert image.mime_type == "image/png"
    assert image.sha256 == hashlib.sha256(ONE_PIXEL_PNG).hexdigest()
    assert (image.width, image.height) == IMAGE_ASPECT_RATIO_DIMENSIONS["16:9"]
    assert image.alt_text == "Gráfica del territorio colombiano en tonos sobrios."
    call = session.calls[0]
    assert call["url"] == MINIMAX_IMAGE_GENERATION_ENDPOINT
    assert call["json"]["response_format"] == "base64"
    assert call["json"]["prompt_optimizer"] is False
    assert call["json"]["n"] == 1
    assert "alt_text" not in call["json"]
    for color in policy.visual.palette.values():
        assert color in call["json"]["prompt"]
    assert "No reconstruyas" in call["json"]["prompt"]


def test_generate_image_supports_official_custom_dimensions(monkeypatch, policy) -> None:
    response = FakeResponse(
        200,
        {
            "data": {"image_base64": [base64.b64encode(ONE_PIXEL_PNG).decode("ascii")]},
            "base_resp": {"status_code": 0},
        },
    )
    session = RecordingSession(response)
    client = make_client(monkeypatch, session)

    image = client.generate_image("Mapa abstracto", policy, width=1024, height=768)

    assert (image.width, image.height) == (1024, 768)
    assert session.calls[0]["json"]["width"] == 1024
    assert session.calls[0]["json"]["height"] == 768
    assert "aspect_ratio" not in session.calls[0]["json"]


def test_custom_dimensions_require_image_01(monkeypatch, policy) -> None:
    session = RecordingSession()
    client = make_client(monkeypatch, session, image_model="image-01-live")

    with pytest.raises(MiniMaxConfigurationError, match="solo.*image-01"):
        client.generate_image("Mapa", policy, width=1024, height=768)
    assert session.calls == []


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"aspect_ratio": "5:4"}, "aspect_ratio"),
        ({"width": 1025, "height": 768}, "divisible por 8"),
        ({"width": 504, "height": 768}, "entre 512 y 2048"),
        ({"width": 1024}, "juntos"),
        ({"size": "1:1", "aspect_ratio": "1:1"}, "Usa solo"),
    ],
)
def test_invalid_image_size_is_rejected_before_network(
    monkeypatch, policy, kwargs: dict, message: str
) -> None:
    session = RecordingSession()
    client = make_client(monkeypatch, session)

    with pytest.raises(MiniMaxConfigurationError, match=message):
        client.generate_image("Mapa", policy, **kwargs)
    assert session.calls == []


def test_generate_image_rejects_url_or_broken_base64(monkeypatch, policy) -> None:
    url_client = make_client(
        monkeypatch,
        RecordingSession(
            FakeResponse(200, {"data": {"image_urls": ["https://expires.invalid/image"]}})
        ),
    )
    with pytest.raises(MiniMaxResponseError, match="base64"):
        url_client.generate_image("Mapa", policy)

    broken_client = make_client(
        monkeypatch,
        RecordingSession(FakeResponse(200, {"data": {"image_base64": ["not-base64!"]}})),
    )
    with pytest.raises(MiniMaxResponseError, match="base64.*inválido"):
        broken_client.generate_image("Mapa", policy)


def test_alt_text_is_validated_locally_before_network(monkeypatch, policy) -> None:
    session = RecordingSession()
    client = make_client(monkeypatch, session)

    with pytest.raises(MiniMaxConfigurationError, match="alt_text"):
        client.generate_image("Mapa", policy, alt_text="x" * 1_001)
    assert session.calls == []


def test_visual_prompt_limit_is_checked_before_network(monkeypatch, policy) -> None:
    session = RecordingSession()
    client = make_client(monkeypatch, session)

    with pytest.raises(MiniMaxConfigurationError, match="1500"):
        client.generate_image("x" * 1_500, policy)
    assert session.calls == []


def test_prohibited_visual_prompt_is_rejected_before_network(monkeypatch, policy) -> None:
    session = RecordingSession()
    client = make_client(monkeypatch, session)

    with pytest.raises(MiniMaxConfigurationError, match="política visual"):
        client.generate_image("Retrato con laureles y un logo improvisado", policy)
    assert session.calls == []
