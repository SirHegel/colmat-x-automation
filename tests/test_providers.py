from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
import requests

from colmat_x.editorial import DRAFT_TOOL_NAME, load_editorial_policy
from colmat_x.providers import (
    ANTHROPIC_MESSAGES_ENDPOINT,
    ANTHROPIC_MODELS_ENDPOINT,
    GEMINI_API_BASE,
    IMAGE_PROVIDER_NAMES,
    OPENAI_CHAT_COMPLETIONS_ENDPOINT,
    OPENAI_IMAGE_GENERATIONS_ENDPOINT,
    OPENAI_MODELS_ENDPOINT,
    PROVIDER_CATALOG,
    TEXT_PROVIDER_NAMES,
    AIEngine,
    AnthropicAPIError,
    AnthropicCapabilityError,
    AnthropicClient,
    AnthropicConfigurationError,
    AnthropicResponseError,
    GeminiClient,
    GeminiConfigurationError,
    GeminiResponseError,
    MiniMaxClient,
    OpenAIClient,
    OpenAIResponseError,
    ProviderCapabilityError,
    ProviderConfigurationError,
    build_ai_engine,
    check_provider_api_key,
    normalize_engine_selection,
    normalize_provider_name,
)
from colmat_x.providers.gemini import generate_content_endpoint, sanitize_schema
from tests.factories import ONE_PIXEL_PNG

POLICY = load_editorial_policy(Path("config/editorial-policy.yaml"))
SECRET = "test-secret-injected-value"


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
    def __init__(self, *responses, error: Exception | None = None) -> None:
        self.responses = list(responses)
        self.error = error
        self.calls: list[dict] = []

    def _next(self):
        if self.error is not None:
            raise self.error
        # La última respuesta se repite: el bucle de borrador reintenta una vez.
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]

    def post(self, url, **kwargs):
        self.calls.append({"method": "POST", "url": url, **kwargs})
        return self._next()

    def get(self, url, **kwargs):
        self.calls.append({"method": "GET", "url": url, **kwargs})
        return self._next()


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


def anthropic_message(arguments, *, tool_use_id="toolu-1", stop_reason="tool_use", prefix=()):
    return {
        "id": "msg-1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5",
        "content": [
            *prefix,
            {"type": "tool_use", "id": tool_use_id, "name": DRAFT_TOOL_NAME, "input": arguments},
        ],
        "stop_reason": stop_reason,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def openai_completion(arguments: str, *, call_id="call-1", finish_reason="tool_calls"):
    return {
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": DRAFT_TOOL_NAME, "arguments": arguments},
                        }
                    ],
                },
            }
        ]
    }


def gemini_candidate(arguments, *, finish_reason="STOP", name=DRAFT_TOOL_NAME):
    return {
        "responseId": "resp-1",
        "candidates": [
            {
                "finishReason": finish_reason,
                "content": {
                    "role": "model",
                    "parts": [
                        {"functionCall": {"name": name, "args": arguments}, "thoughtSignature": "s"}
                    ],
                },
            }
        ],
    }


# --- catálogo cerrado ------------------------------------------------------------------


def test_catalog_is_closed_and_anthropic_never_offers_images() -> None:
    assert TEXT_PROVIDER_NAMES == ("minimax", "anthropic", "openai", "gemini")
    assert IMAGE_PROVIDER_NAMES == ("minimax", "openai", "gemini")
    assert PROVIDER_CATALOG["anthropic"].supports_images is False
    assert PROVIDER_CATALOG["anthropic"].default_text_model == "claude-opus-5"
    with pytest.raises(ProviderConfigurationError, match="uno de"):
        normalize_provider_name("https://attacker.invalid", capability="text", field_name="x")
    with pytest.raises(ProviderConfigurationError, match="uno de"):
        normalize_provider_name("anthropic", capability="image", field_name="image_provider")
    with pytest.raises(ProviderConfigurationError, match="modelo de texto"):
        normalize_engine_selection(
            text_provider="anthropic",
            text_model="claude-imaginary-9",
            image_provider="openai",
            image_model="gpt-image-2.5-sunburst",
        )


# --- Anthropic ---------------------------------------------------------------------------


def test_anthropic_requires_its_own_key_and_never_reads_minimax(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("MINIMAX_API_KEY", "must-not-be-used")
    with pytest.raises(AnthropicConfigurationError, match="ANTHROPIC_API_KEY"):
        AnthropicClient(session=RecordingSession())


def test_anthropic_draft_uses_fixed_endpoint_forced_tool_and_no_sampling(
    monkeypatch, draft_payload
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    session = RecordingSession(FakeResponse(200, anthropic_message(draft_payload)))
    client = AnthropicClient(session=session, model="claude-sonnet-5")

    draft = client.generate_draft("Dato entregado", POLICY, category="dato_semana")

    assert draft.figure == "10 %"
    assert draft.publication_authorized is False
    call = session.calls[0]
    assert call["url"] == ANTHROPIC_MESSAGES_ENDPOINT
    assert call["headers"]["x-api-key"] == SECRET
    assert call["headers"]["anthropic-version"] == "2023-06-01"
    assert "Authorization" not in call["headers"]
    body = call["json"]
    assert body["model"] == "claude-sonnet-5"
    assert "temperature" not in body
    assert "thinking" not in body
    assert body["tool_choice"] == {"type": "tool", "name": DRAFT_TOOL_NAME}
    assert body["tools"][0]["input_schema"]["additionalProperties"] is False
    assert all(message["role"] != "system" for message in body["messages"])
    assert "visual.tipo='ninguna'" in body["system"]


def test_anthropic_repair_echoes_thinking_blocks_and_fails_closed(
    monkeypatch, draft_payload
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    invalid = json.loads(json.dumps(draft_payload))
    invalid["visual"]["tipo"] = "ninguna"
    thinking = {"type": "thinking", "thinking": "", "signature": "sig"}
    redacted = {"type": "redacted_thinking", "data": "opaque"}
    first = anthropic_message(invalid, tool_use_id="toolu-bad", prefix=(thinking, redacted))
    second = anthropic_message(draft_payload, tool_use_id="toolu-ok")
    session = RecordingSession(FakeResponse(200, first), FakeResponse(200, second))

    draft = AnthropicClient(session=session).generate_draft("Dato", POLICY)

    assert draft.figure == "10 %"
    repair = session.calls[1]["json"]["messages"]
    assert repair[-2]["content"][0] == thinking
    assert repair[-2]["content"][1] == redacted
    assert repair[-1]["content"][0]["tool_use_id"] == "toolu-bad"
    assert repair[-1]["content"][0]["is_error"] is True

    stuck = RecordingSession(
        FakeResponse(200, anthropic_message(invalid, tool_use_id="a")),
        FakeResponse(200, anthropic_message(invalid, tool_use_id="b")),
    )
    with pytest.raises(AnthropicResponseError, match="borrador inválido"):
        AnthropicClient(session=stuck).generate_draft("Dato", POLICY)


def test_anthropic_refusal_and_errors_never_echo_the_key(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    refusal = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "No."}],
        "stop_reason": "refusal",
    }
    with pytest.raises(AnthropicResponseError, match="rechazó el encargo"):
        AnthropicClient(session=RecordingSession(FakeResponse(200, refusal))).generate_draft(
            "Dato", POLICY
        )
    leaking = FakeResponse(429, {"error": {"type": "rate_limit_error", "message": f"bad {SECRET}"}})
    with pytest.raises(AnthropicAPIError, match="429") as captured:
        AnthropicClient(session=RecordingSession(leaking)).generate_draft("Dato", POLICY)
    assert SECRET not in str(captured.value)
    assert "[REDACTADO]" in str(captured.value)


def test_anthropic_declares_no_image_capability(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    client = AnthropicClient(session=RecordingSession())
    assert client.supports_images is False
    with pytest.raises(AnthropicCapabilityError, match="no genera imágenes"):
        client.generate_image("Mapa abstracto", POLICY, aspect_ratio="16:9")
    assert isinstance(AnthropicCapabilityError("x"), ProviderCapabilityError)


def test_anthropic_key_check_uses_models_endpoint_without_generating() -> None:
    session = RecordingSession(FakeResponse(200, {"data": []}))
    check = AnthropicClient.check_api_key(SECRET, session=session)
    assert check.status == "valid"
    call = session.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == ANTHROPIC_MODELS_ENDPOINT
    assert call["headers"]["x-api-key"] == SECRET
    assert SECRET not in check.detail

    assert (
        AnthropicClient.check_api_key(
            SECRET, session=RecordingSession(FakeResponse(401, {}))
        ).status
        == "invalid"
    )
    timeout = RecordingSession(error=requests.Timeout("late"))
    assert AnthropicClient.check_api_key(SECRET, session=timeout).status == "error"


# --- OpenAI ------------------------------------------------------------------------------


def test_openai_draft_forces_the_closed_function_without_temperature(
    monkeypatch, draft_payload
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    session = RecordingSession(FakeResponse(200, openai_completion(json.dumps(draft_payload))))
    client = OpenAIClient(session=session)

    draft = client.generate_draft("Dato entregado", POLICY)

    assert draft.figure == "10 %"
    call = session.calls[0]
    assert call["url"] == OPENAI_CHAT_COMPLETIONS_ENDPOINT
    assert call["headers"]["Authorization"] == f"Bearer {SECRET}"
    body = call["json"]
    assert body["model"] == "gpt-6-astra"
    assert "temperature" not in body
    assert body["tool_choice"] == {"type": "function", "function": {"name": DRAFT_TOOL_NAME}}
    assert body["parallel_tool_calls"] is False
    assert "max_completion_tokens" in body and "max_tokens" not in body


def test_openai_repairs_with_tool_message_and_rejects_refusal(monkeypatch, draft_payload) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    invalid = json.loads(json.dumps(draft_payload))
    invalid["cifra"] = "sin digitos"
    session = RecordingSession(
        FakeResponse(200, openai_completion(json.dumps(invalid), call_id="call-bad")),
        FakeResponse(200, openai_completion(json.dumps(draft_payload), call_id="call-ok")),
    )
    assert OpenAIClient(session=session).generate_draft("Dato", POLICY).figure == "10 %"
    repair = session.calls[1]["json"]["messages"]
    assert repair[-1]["role"] == "tool"
    assert repair[-1]["tool_call_id"] == "call-bad"
    assert json.loads(repair[-1]["content"])["accepted"] is False

    refusal = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "refusal": "No puedo", "content": None},
            }
        ]
    }
    with pytest.raises(OpenAIResponseError, match="rechazó el encargo"):
        OpenAIClient(session=RecordingSession(FakeResponse(200, refusal))).generate_draft(
            "Dato", POLICY
        )
    plain = {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]}
    with pytest.raises(OpenAIResponseError, match="no hizo la llamada"):
        OpenAIClient(session=RecordingSession(FakeResponse(200, plain))).generate_draft(
            "Dato", POLICY
        )


def test_openai_image_maps_landscape_to_supported_size_and_validates_bytes(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    encoded = base64.b64encode(ONE_PIXEL_PNG).decode("ascii")
    session = RecordingSession(FakeResponse(200, {"data": [{"b64_json": encoded}]}))
    client = OpenAIClient(session=session)

    image = client.generate_image(
        "Mapa abstracto sobrio", POLICY, aspect_ratio="16:9", alt_text="Mapa"
    )

    assert image.provider == "openai"
    assert image.model == "gpt-image-2.5-sunburst"
    assert (image.width, image.height) == (1536, 1024)
    assert image.mime_type == "image/png"
    call = session.calls[0]
    assert call["url"] == OPENAI_IMAGE_GENERATIONS_ENDPOINT
    assert call["json"]["size"] == "1536x1024"
    assert call["json"]["n"] == 1
    assert "No reconstruyas" in call["json"]["prompt"]

    with pytest.raises(OpenAIResponseError, match="exactamente una imagen"):
        OpenAIClient(session=RecordingSession(FakeResponse(200, {"data": []}))).generate_image(
            "Mapa", POLICY
        )
    broken = RecordingSession(FakeResponse(200, {"data": [{"b64_json": "not-base64!"}]}))
    with pytest.raises(OpenAIResponseError, match="base64"):
        OpenAIClient(session=broken).generate_image("Mapa", POLICY)


def test_openai_key_check_uses_models_endpoint() -> None:
    session = RecordingSession(FakeResponse(200, {"data": []}))
    assert OpenAIClient.check_api_key(SECRET, session=session).status == "valid"
    assert session.calls[0]["url"] == OPENAI_MODELS_ENDPOINT
    assert (
        OpenAIClient.check_api_key(SECRET, session=RecordingSession(FakeResponse(403, {}))).status
        == "invalid"
    )


# --- Gemini ------------------------------------------------------------------------------


def test_gemini_endpoint_is_fixed_and_model_name_cannot_escape_the_path() -> None:
    assert generate_content_endpoint("gemini-3.8-flash") == (
        f"{GEMINI_API_BASE}/models/gemini-3.8-flash:generateContent"
    )
    for hostile in ("../other", "x:generateAnswer", "a?key=b", "m#f", "evil/path"):
        with pytest.raises(GeminiConfigurationError):
            generate_content_endpoint(hostile)


def test_gemini_draft_uses_function_calling_config_and_native_repair(
    monkeypatch, draft_payload
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    invalid = json.loads(json.dumps(draft_payload))
    invalid["visual"]["tipo"] = "ninguna"
    session = RecordingSession(
        FakeResponse(200, gemini_candidate(invalid)),
        FakeResponse(200, gemini_candidate(draft_payload)),
    )
    client = GeminiClient(session=session, model="gemini-3.5-flash")

    draft = client.generate_draft("Dato entregado", POLICY, institution="colmat")

    assert draft.figure == "10 %"
    first = session.calls[0]
    assert first["url"] == f"{GEMINI_API_BASE}/models/gemini-3.5-flash:generateContent"
    assert first["headers"]["x-goog-api-key"] == SECRET
    assert SECRET not in first["url"]
    body = first["json"]
    assert body["toolConfig"]["functionCallingConfig"] == {
        "mode": "ANY",
        "allowedFunctionNames": [DRAFT_TOOL_NAME],
    }
    parameters = body["tools"][0]["functionDeclarations"][0]["parameters"]
    assert "additionalProperties" not in parameters
    assert "enum" not in parameters["properties"]["visual"]["properties"]["usa_simbolos"]
    assert "uniqueItems" not in parameters["properties"]["visual"]["properties"]["colores"]
    repair = session.calls[1]["json"]["contents"]
    assert repair[-2]["role"] == "model"
    assert repair[-2]["parts"][0]["thoughtSignature"] == "s"
    assert repair[-1]["parts"][0]["functionResponse"]["name"] == DRAFT_TOOL_NAME
    assert repair[-1]["parts"][0]["functionResponse"]["response"]["accepted"] is False


def test_gemini_blocks_and_wrong_tools_fail_closed(monkeypatch, draft_payload) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    blocked = {"promptFeedback": {"blockReason": "SAFETY"}, "candidates": []}
    with pytest.raises(GeminiResponseError, match="bloqueó el encargo"):
        GeminiClient(session=RecordingSession(FakeResponse(200, blocked))).generate_draft(
            "Dato", POLICY
        )
    with pytest.raises(GeminiResponseError, match="truncó"):
        GeminiClient(
            session=RecordingSession(
                FakeResponse(200, gemini_candidate(draft_payload, finish_reason="MAX_TOKENS"))
            )
        ).generate_draft("Dato", POLICY)
    with pytest.raises(GeminiResponseError, match="no autorizada"):
        GeminiClient(
            session=RecordingSession(
                FakeResponse(200, gemini_candidate(draft_payload, name="publicar_en_x"))
            )
        ).generate_draft("Dato", POLICY)


def test_gemini_image_uses_inline_data_and_nominal_dimensions(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    encoded = base64.b64encode(ONE_PIXEL_PNG).decode("ascii")
    payload = {
        "responseId": "img-1",
        "candidates": [
            {
                "finishReason": "STOP",
                "content": {
                    "role": "model",
                    "parts": [
                        {"text": "Listo"},
                        {"inlineData": {"mimeType": "image/png", "data": encoded}},
                    ],
                },
            }
        ],
    }
    session = RecordingSession(FakeResponse(200, payload))
    image = GeminiClient(session=session).generate_image(
        "Mapa abstracto", POLICY, aspect_ratio="16:9"
    )

    assert image.provider == "gemini"
    assert image.model == "gemini-3.1-flash-image"
    assert image.request_id == "img-1"
    assert (image.width, image.height) == (1344, 768)
    body = session.calls[0]["json"]
    assert body["generationConfig"]["responseModalities"] == ["IMAGE"]
    assert body["generationConfig"]["imageConfig"] == {"aspectRatio": "16:9"}

    mismatched = json.loads(json.dumps(payload))
    mismatched["candidates"][0]["content"]["parts"][1]["inlineData"]["mimeType"] = "image/jpeg"
    with pytest.raises(GeminiResponseError, match="MIME declarado"):
        GeminiClient(session=RecordingSession(FakeResponse(200, mismatched))).generate_image(
            "Mapa", POLICY
        )


def test_gemini_schema_sanitizer_keeps_string_enums_and_required() -> None:
    cleaned = sanitize_schema(
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["a"],
            "properties": {
                "a": {"type": "string", "enum": ["x"]},
                "b": {"type": "boolean", "enum": [False]},
                "c": {"type": "array", "uniqueItems": True, "items": {"type": "string"}},
            },
        }
    )
    assert cleaned["required"] == ["a"]
    assert cleaned["properties"]["a"]["enum"] == ["x"]
    assert "enum" not in cleaned["properties"]["b"]
    assert "uniqueItems" not in cleaned["properties"]["c"]


def test_gemini_key_check_treats_400_as_invalid() -> None:
    assert (
        GeminiClient.check_api_key(SECRET, session=RecordingSession(FakeResponse(400, {}))).status
        == "invalid"
    )
    session = RecordingSession(FakeResponse(200, {"models": []}))
    assert GeminiClient.check_api_key(SECRET, session=session).status == "valid"
    assert session.calls[0]["headers"]["x-goog-api-key"] == SECRET
    assert SECRET not in session.calls[0]["url"]


# --- fábrica -----------------------------------------------------------------------------


def test_engine_defaults_to_minimax_and_shares_one_client(monkeypatch, draft_payload) -> None:
    monkeypatch.delenv("COLMAT_TEXT_PROVIDER", raising=False)
    monkeypatch.delenv("COLMAT_IMAGE_PROVIDER", raising=False)
    monkeypatch.setenv("MINIMAX_API_KEY", SECRET)
    engine = build_ai_engine(session=RecordingSession())

    assert engine.provider_name == "minimax"
    assert engine.model == "MiniMax-M2.7"
    assert engine.image_provider_name == "minimax"
    assert engine.image_model == "image-01"
    assert engine.describe() == {
        "text": {"provider": "minimax", "model": "MiniMax-M2.7"},
        "image": {"provider": "minimax", "model": "image-01"},
    }
    assert engine._client("minimax") is engine._client("minimax")
    assert isinstance(engine._client("minimax"), MiniMaxClient)


def test_engine_combines_claude_text_with_openai_images(monkeypatch, draft_payload) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    session = RecordingSession(
        FakeResponse(200, anthropic_message(draft_payload)),
        FakeResponse(200, {"data": [{"b64_json": base64.b64encode(ONE_PIXEL_PNG).decode()}]}),
    )
    engine = build_ai_engine(text_provider="anthropic", image_provider="openai", session=session)

    draft = engine.generate_draft("Dato", POLICY)
    image = engine.generate_image(draft.visual.descripcion, POLICY, aspect_ratio="16:9")

    assert engine.describe()["text"]["model"] == "claude-opus-5"
    assert image.provider == "openai"
    assert session.calls[0]["url"] == ANTHROPIC_MESSAGES_ENDPOINT
    assert session.calls[0]["headers"]["x-api-key"] == "anthropic-secret"
    assert session.calls[1]["url"] == OPENAI_IMAGE_GENERATIONS_ENDPOINT
    assert session.calls[1]["headers"]["Authorization"] == "Bearer openai-secret"


def test_engine_with_claude_only_fails_clearly_on_images(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    engine = AIEngine(text_provider="anthropic", session=RecordingSession())
    assert engine.supports_images is False
    assert engine.describe()["image"] is None
    with pytest.raises(ProviderCapabilityError, match="no genera imágenes"):
        engine.generate_image("Mapa", POLICY, aspect_ratio="16:9")


def test_engine_is_lazy_and_missing_key_surfaces_at_first_use(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    engine = AIEngine(text_provider="gemini", session=RecordingSession())
    with pytest.raises(ProviderConfigurationError, match="GEMINI_API_KEY"):
        engine.generate_draft("Dato", POLICY)


def test_engine_prefers_injected_key_over_environment(monkeypatch, draft_payload) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "environment-key")
    session = RecordingSession(FakeResponse(200, openai_completion(json.dumps(draft_payload))))
    engine = AIEngine(
        text_provider="openai",
        api_key_resolver=lambda name: "stored-key" if name == "openai" else None,
        session=session,
    )
    engine.generate_draft("Dato", POLICY)
    assert session.calls[0]["headers"]["Authorization"] == "Bearer stored-key"

    fallback = RecordingSession(FakeResponse(200, openai_completion(json.dumps(draft_payload))))
    AIEngine(
        text_provider="openai", api_key_resolver=lambda _name: None, session=fallback
    ).generate_draft("Dato", POLICY)
    assert fallback.calls[0]["headers"]["Authorization"] == "Bearer environment-key"


def test_engine_rejects_names_outside_the_closed_list() -> None:
    with pytest.raises(ProviderConfigurationError, match="uno de"):
        AIEngine(text_provider="https://evil.invalid/v1")
    with pytest.raises(ProviderConfigurationError, match="uno de"):
        AIEngine(text_provider="openai", image_provider="anthropic")
    with pytest.raises(ProviderConfigurationError):
        check_provider_api_key("no-existe", SECRET, session=RecordingSession())
    # `codex` es un alias coloquial aceptado que resuelve a openai, no una
    # ampliación de la lista cerrada: sigue sin admitir URLs ni nombres libres.
    assert AIEngine(text_provider="codex").provider_name == "openai"
