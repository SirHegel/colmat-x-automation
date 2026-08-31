from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
import requests

from colmat_x.editorial import DRAFT_TOOL_NAME, load_editorial_policy
from colmat_x.minimax import (
    DEFAULT_MINIMAX_API_STYLE,
    IMAGE_ASPECT_RATIO_DIMENSIONS,
    MINIMAX_ANTHROPIC_MESSAGES_ENDPOINT,
    MINIMAX_ANTHROPIC_VERSION,
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


def anthropic_tool_response(
    arguments: object,
    *,
    tool_use_id: str | None = "toolu-draft-1",
    tool_name: str = DRAFT_TOOL_NAME,
    prefix_blocks: list[dict] | None = None,
    stop_reason: str = "tool_use",
) -> dict:
    tool_use = {
        "type": "tool_use",
        "name": tool_name,
        "input": arguments,
    }
    if tool_use_id is not None:
        tool_use["id"] = tool_use_id
    return {
        "id": "msg-draft-1",
        "type": "message",
        "role": "assistant",
        "model": "MiniMax-M2.7",
        "content": [*(prefix_blocks or []), tool_use],
        "stop_reason": stop_reason,
        "usage": {"input_tokens": 10, "output_tokens": 20},
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


def test_api_style_is_allowlisted_and_can_be_selected_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("MINIMAX_API_KEY", "test-secret-from-env")
    monkeypatch.setenv("MINIMAX_API_STYLE", " OpenAI ")

    assert MiniMaxClient(session=RecordingSession()).api_style == "openai"

    monkeypatch.setenv("MINIMAX_API_STYLE", "https://attacker.invalid/messages")
    with pytest.raises(MiniMaxConfigurationError, match="anthropic u openai"):
        MiniMaxClient(session=RecordingSession())


def test_openai_style_remains_an_explicit_fixed_endpoint(
    monkeypatch, policy, draft_payload
) -> None:
    session = RecordingSession(FakeResponse(200, tool_response(json.dumps(draft_payload))))
    client = make_client(monkeypatch, session, api_style="openai")

    assert client.generate_draft("Dato entregado", policy).figure == "10 %"

    call = session.calls[0]
    assert call["url"] == MINIMAX_CHAT_COMPLETIONS_ENDPOINT
    assert call["headers"] == {
        "Content-Type": "application/json",
        "Authorization": "Bearer test-secret-from-env",
    }
    assert call["json"]["tool_choice"] == {
        "type": "function",
        "function": {"name": DRAFT_TOOL_NAME},
    }
    assert call["json"]["reasoning_split"] is True
    assert call["json"]["max_completion_tokens"] == 2_048


def test_generate_draft_uses_official_endpoint_and_closed_tool(
    monkeypatch, policy, draft_payload
) -> None:
    response = FakeResponse(200, anthropic_tool_response(draft_payload))
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
    assert client.api_style == DEFAULT_MINIMAX_API_STYLE == "anthropic"
    assert call["url"] == MINIMAX_ANTHROPIC_MESSAGES_ENDPOINT
    assert call["headers"]["x-api-key"] == "test-secret-from-env"
    assert call["headers"]["anthropic-version"] == MINIMAX_ANTHROPIC_VERSION
    assert "Authorization" not in call["headers"]
    assert call["timeout"] == (3.0, 17.0)
    body = call["json"]
    assert body["stream"] is False
    assert body["thinking"] == {"type": "disabled"}
    assert body["max_tokens"] == 2_048
    assert "max_completion_tokens" not in body
    assert "reasoning_split" not in body
    assert body["tools"][0]["name"] == DRAFT_TOOL_NAME
    assert body["tools"][0]["input_schema"]["additionalProperties"] is False
    assert "function" not in body["tools"][0]
    assert body["tool_choice"] == {"type": "tool", "name": DRAFT_TOOL_NAME}
    assert "visual.tipo='ninguna'" in body["system"]
    assert all(message["role"] != "system" for message in body["messages"])
    assert body["messages"][0]["content"][0]["type"] == "text"
    assert "Usa el dato entregado." in body["messages"][0]["content"][0]["text"]


def test_generate_draft_repairs_one_locally_invalid_tool_call(
    monkeypatch, policy, draft_payload
) -> None:
    invalid_payload = json.loads(json.dumps(draft_payload))
    invalid_payload["visual"]["tipo"] = "ninguna"
    thinking = {"type": "thinking", "thinking": "revisión", "signature": "firma"}
    first = anthropic_tool_response(
        invalid_payload,
        tool_use_id="toolu-invalid-1",
        prefix_blocks=[thinking],
    )
    second = anthropic_tool_response(draft_payload, tool_use_id="toolu-valid-2")
    session = SequenceSession(FakeResponse(200, first), FakeResponse(200, second))
    client = make_client(monkeypatch, session)

    draft = client.generate_draft("Dato entregado", policy)

    assert draft.figure == "10 %"
    assert len(session.calls) == 2
    repair_messages = session.calls[1]["json"]["messages"]
    assert repair_messages[-2] == {"role": "assistant", "content": first["content"]}
    assert repair_messages[-2]["content"][0] == thinking
    assert repair_messages[-1]["role"] == "user"
    tool_result = repair_messages[-1]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_use_id"] == "toolu-invalid-1"
    assert tool_result["is_error"] is True
    feedback = json.loads(tool_result["content"])
    assert feedback["accepted"] is False
    assert "colores=[]" in feedback["validation_error"]
    assert feedback["texto_debe_contener_literalmente"] == {
        "cifra": draft_payload["cifra"],
        "fuente": draft_payload["fuente"],
    }


def test_generate_draft_repairs_malformed_tool_arguments(
    monkeypatch, policy, draft_payload
) -> None:
    first = anthropic_tool_response(
        '{"categoria":"dato_semana",',
        tool_use_id="toolu-broken-input",
    )
    second = anthropic_tool_response(draft_payload, tool_use_id="toolu-valid-2")
    session = SequenceSession(FakeResponse(200, first), FakeResponse(200, second))
    client = make_client(monkeypatch, session)

    draft = client.generate_draft("Dato entregado", policy)

    assert draft.figure == "10 %"
    result_block = session.calls[1]["json"]["messages"][-1]["content"][0]
    feedback = json.loads(result_block["content"])
    assert "no son un objeto JSON" in feedback["validation_error"]


def test_generate_draft_repairs_without_inventing_a_missing_tool_call_id(
    monkeypatch, policy, draft_payload
) -> None:
    first = anthropic_tool_response(draft_payload, tool_use_id=None)
    second = anthropic_tool_response(draft_payload, tool_use_id="toolu-valid-2")
    session = SequenceSession(FakeResponse(200, first), FakeResponse(200, second))
    client = make_client(monkeypatch, session)

    assert client.generate_draft("Dato entregado", policy).figure == "10 %"
    repair_message = session.calls[1]["json"]["messages"][-1]
    assert repair_message["role"] == "user"
    assert "sin id válido" in repair_message["content"][0]["text"]
    assert "tool_use_id" not in repair_message["content"][0]


def test_generate_draft_fails_closed_after_one_repair(monkeypatch, policy, draft_payload) -> None:
    invalid_payload = json.loads(json.dumps(draft_payload))
    invalid_payload["visual"]["tipo"] = "ninguna"
    first = anthropic_tool_response(invalid_payload, tool_use_id="toolu-invalid-1")
    second = anthropic_tool_response(invalid_payload, tool_use_id="toolu-invalid-2")
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
    client = make_client(monkeypatch, RecordingSession(response), api_style="openai")

    assert client.generate_draft("Dato entregado", policy).figure == "10 %"


def test_generate_draft_reports_plain_text_refusal_without_reflecting_it(
    monkeypatch, policy
) -> None:
    refusal = "No puedo crear la pieza sin una cifra y una fuente verificables."
    response = FakeResponse(
        200,
        {
            "id": "msg-refusal",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": refusal}],
            "stop_reason": "end_turn",
            "base_resp": {"status_code": 0},
        },
    )
    client = make_client(monkeypatch, RecordingSession(response))

    with pytest.raises(MiniMaxResponseError, match="exactamente una llamada") as captured:
        client.generate_draft("Encargo conceptual sin evidencia", policy)

    assert refusal not in str(captured.value)


def test_generate_draft_rejects_duplicate_json_keys(monkeypatch, policy, draft_payload) -> None:
    raw = json.dumps(draft_payload)
    raw = raw[:-1] + ', "cifra": "11 %"}'
    client = make_client(
        monkeypatch,
        RecordingSession(FakeResponse(200, tool_response(raw))),
        api_style="openai",
    )

    with pytest.raises(MiniMaxResponseError, match="JSON inválidos"):
        client.generate_draft("Dato", policy)


def test_generate_draft_rejects_unvalidated_output(monkeypatch, policy, draft_payload) -> None:
    draft_payload["publicar"] = True
    client = make_client(
        monkeypatch,
        RecordingSession(FakeResponse(200, anthropic_tool_response(draft_payload))),
    )

    with pytest.raises(MiniMaxResponseError, match="borrador inválido"):
        client.generate_draft("Dato", policy)


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (
            {
                "type": "message",
                "role": "assistant",
                "content": [],
                "stop_reason": "end_turn",
            },
            "bloques de contenido",
        ),
        (
            anthropic_tool_response(
                {},
                tool_name="publicar_en_x",
            ),
            "herramienta no autorizada",
        ),
        (
            {
                **anthropic_tool_response({}),
                "content": [
                    *anthropic_tool_response({})["content"],
                    {
                        "type": "tool_use",
                        "id": "toolu-extra",
                        "name": DRAFT_TOOL_NAME,
                        "input": {},
                    },
                ],
            },
            "exactamente una llamada",
        ),
        (
            anthropic_tool_response([], tool_use_id="toolu-not-object"),
            "no son un objeto JSON",
        ),
        (
            {
                **anthropic_tool_response({}),
                "content": [
                    {"type": "server_tool_use", "id": "server-tool"},
                    *anthropic_tool_response({})["content"],
                ],
            },
            "bloque Anthropic no autorizado",
        ),
        (
            anthropic_tool_response({}, stop_reason="end_turn"),
            "no terminó con la llamada",
        ),
        (
            anthropic_tool_response({}, stop_reason="max_tokens"),
            "truncó",
        ),
    ],
)
def test_anthropic_response_shape_fails_closed(
    monkeypatch, policy, response: dict, message: str
) -> None:
    client = make_client(monkeypatch, RecordingSession(FakeResponse(200, response)))

    with pytest.raises(MiniMaxResponseError, match=message):
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


def test_anthropic_http_error_redacts_nested_message(monkeypatch, policy) -> None:
    response = FakeResponse(
        429,
        {
            "type": "error",
            "error": {
                "type": "rate_limit_error",
                "message": "Token Plan rejected test-secret-from-env",
            },
        },
    )
    client = make_client(monkeypatch, RecordingSession(response))

    with pytest.raises(MiniMaxAPIError, match="429") as captured:
        client.generate_draft("Dato", policy)
    assert "test-secret-from-env" not in str(captured.value)
    assert "[REDACTADO]" in str(captured.value)


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
    assert call["headers"] == {
        "Content-Type": "application/json",
        "Authorization": "Bearer test-secret-from-env",
    }
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
