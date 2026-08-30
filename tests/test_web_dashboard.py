from __future__ import annotations

import re
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

import colmat_x.platform_store as platform_store_module
from colmat_x.platform_store import PlatformStore
from colmat_x.rbac import Role
from colmat_x.web import CSRF_COOKIE, LOGIN_CSRF_COOKIE, SESSION_COOKIE, create_app

ORIGIN = "https://testserver"
PEPPER = "test-web-auth-pepper-which-is-longer-than-32-characters"


@dataclass
class RecordingTelegramClient:
    sent: list[tuple[int, str, int | None]] = field(default_factory=list)

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
    ) -> object:
        self.sent.append((chat_id, text, reply_to_message_id))
        return {"ok": True}


@pytest.fixture
def dashboard_stack():
    store = PlatformStore("sqlite+pysqlite:///:memory:")
    owner, _membership = store.bootstrap_owner(
        email="owner@colmat.test",
        username="sirhegel",
        display_name="SirHegel",
    )
    store.bind_telegram_chat(
        7084929277,
        telegram_user_id=7084929277,
        actor_id=owner.id,
        user_id=owner.id,
        purpose="control",
    )
    telegram = RecordingTelegramClient()
    app = create_app(
        environ={"WEB_AUTH_PEPPER": PEPPER},
        store=store,
        processor=object(),
        telegram_client=telegram,
    )
    client = TestClient(app, base_url=ORIGIN, follow_redirects=False)
    try:
        yield client, store, owner, telegram
    finally:
        client.close()
        store.close()


def _hidden(response, name: str) -> str:
    match = re.search(rf'name="{re.escape(name)}" value="([^"]+)"', response.text)
    assert match is not None
    return match.group(1)


def _login(
    client: TestClient,
    telegram: RecordingTelegramClient,
    *,
    identifier: str = "SirHegel",
):
    login = client.get("/login")
    assert login.status_code == 200
    login_csrf = _hidden(login, "csrf_token")
    assert client.cookies.get(LOGIN_CSRF_COOKIE) == login_csrf

    challenge = client.post(
        "/auth/code",
        headers={"Origin": ORIGIN},
        data={"csrf_token": login_csrf, "identifier": identifier},
    )
    assert challenge.status_code == 200
    assert "Telegram vinculado" in challenge.text
    challenge_id = _hidden(challenge, "challenge_id")
    code_match = re.search(r"\b([0-9]{8})\b", telegram.sent[-1][1])
    assert code_match is not None

    verified = client.post(
        "/auth/verify",
        headers={"Origin": ORIGIN},
        data={
            "csrf_token": login_csrf,
            "challenge_id": challenge_id,
            "code": code_match.group(1),
        },
    )
    assert verified.status_code == 303
    assert verified.headers["location"] == "/app"
    return verified


def test_passwordless_login_sets_hardened_cookies_and_renders_dashboard(
    dashboard_stack,
) -> None:
    client, _store, _owner, telegram = dashboard_stack

    verified = _login(client, telegram)
    set_cookie = verified.headers.get_list("set-cookie")
    assert any(
        SESSION_COOKIE in value and "HttpOnly" in value and "Secure" in value
        for value in set_cookie
    )
    assert any(
        CSRF_COOKIE in value and "HttpOnly" in value and "Secure" in value for value in set_cookie
    )

    panel = client.get("/app")

    assert panel.status_code == 200
    assert "Buen trabajo, SirHegel" in panel.text
    assert "Encargar a MiniMax" in panel.text
    assert panel.headers["content-security-policy"].startswith("default-src 'self'")
    assert panel.headers["strict-transport-security"].startswith("max-age=")
    assert panel.headers["x-frame-options"] == "DENY"


def test_unknown_login_is_generic_and_never_sends_a_code(dashboard_stack) -> None:
    client, _store, _owner, telegram = dashboard_stack
    login = client.get("/login")
    csrf_token = _hidden(login, "csrf_token")

    response = client.post(
        "/auth/code",
        headers={"Origin": ORIGIN},
        data={"csrf_token": csrf_token, "identifier": "unknown@colmat.test"},
    )

    assert response.status_code == 200
    assert "Si la cuenta está habilitada" in response.text
    assert "unknown@colmat.test" not in response.text
    assert telegram.sent == []


def test_login_post_requires_same_origin_and_csrf(dashboard_stack) -> None:
    client, _store, _owner, telegram = dashboard_stack
    login = client.get("/login")
    csrf_token = _hidden(login, "csrf_token")

    missing_origin = client.post(
        "/auth/code",
        data={"csrf_token": csrf_token, "identifier": "sirhegel"},
    )
    wrong_csrf = client.post(
        "/auth/code",
        headers={"Origin": ORIGIN},
        data={"csrf_token": "wrong", "identifier": "sirhegel"},
    )

    assert missing_origin.status_code == 403
    assert wrong_csrf.status_code == 403
    assert telegram.sent == []


def test_owner_can_create_team_member_and_enqueue_minimax_work(dashboard_stack) -> None:
    client, store, owner, telegram = dashboard_stack
    _login(client, telegram)
    panel = client.get("/app")
    csrf_token = client.cookies.get(CSRF_COOKIE)
    assert csrf_token

    created = client.post(
        "/app/team",
        headers={"Origin": ORIGIN},
        data={
            "csrf_token": csrf_token,
            "email": "editor@colmat.test",
            "username": "editor.colmat",
            "display_name": "Editora Colmat",
            "role": "editor",
        },
    )
    generated = client.post(
        "/app/generate",
        headers={"Origin": ORIGIN},
        data={
            "csrf_token": csrf_token,
            "request_id": _hidden(panel, "request_id"),
            "brief": ("9 poderes articulados en 3 capas. Fuente: TIERRA FIRME, capítulos 1 y 5."),
            "generate_image": "true",
        },
    )

    assert created.status_code == 303
    assert created.headers["location"] == "/app?notice=team_created"
    memberships = store.list_memberships(actor_id=owner.id)
    assert any(store.get_user(item.user_id).username == "editor.colmat" for item in memberships)
    assert generated.status_code == 303
    assert generated.headers["location"] == "/app?notice=generation_queued"
    requests = store.list_generation_requests(actor_id=owner.id)
    assert len(requests) == 1
    assert requests[0].requested_by == owner.id
    assert requests[0].generate_image is True


def test_human_review_automation_update_is_versioned_and_direct_stays_closed(
    dashboard_stack,
) -> None:
    client, store, owner, telegram = dashboard_stack
    _login(client, telegram)
    csrf_token = client.cookies.get(CSRF_COOKIE)
    assert csrf_token

    updated = client.post(
        "/app/automation",
        headers={"Origin": ORIGIN},
        data={
            "csrf_token": csrf_token,
            "mode": "human_review",
            "expected_version": "1",
            "max_posts_per_day": "4",
        },
    )
    blocked_direct = client.post(
        "/app/automation",
        headers={"Origin": ORIGIN},
        data={
            "csrf_token": csrf_token,
            "enabled": "true",
            "mode": "direct",
            "expected_version": "2",
            "max_posts_per_day": "4",
        },
    )

    settings = store.get_automation_settings(actor_id=owner.id)
    assert updated.headers["location"] == "/app?notice=automation_updated"
    assert settings.enabled is False
    assert settings.mode == "human_review"
    assert settings.max_posts_per_day == 4
    assert settings.version == 2
    assert blocked_direct.headers["location"] == "/app?error=automation_failed"


def test_web_post_cannot_decide_or_publish_media_without_a_preview(dashboard_stack) -> None:
    client, store, owner, telegram = dashboard_stack
    editor, _membership = store.create_team_member(
        actor_id=owner.id,
        email="media-editor@colmat.test",
        username="media.editor",
        display_name="Editor de imagen",
        role=Role.EDITOR,
    )
    draft, revision = store.create_draft(
        actor_id=editor.id,
        text="Nueve poderes territoriales requieren una lectura articulada.",
        category="dato_semana",
        publish_at="2026-09-01T14:00:00+00:00",
        evidence={"figure": "9", "source": "TIERRA FIRME, capítulo 1"},
        image_sha256="a" * 64,
    )
    store.submit_for_review(
        draft.id,
        actor_id=editor.id,
        expected_snapshot_hash=revision.snapshot_hash,
    )
    _login(client, telegram)
    csrf_token = client.cookies.get(CSRF_COOKIE)
    assert csrf_token
    form = {"csrf_token": csrf_token, "snapshot_hash": revision.snapshot_hash}

    approve = client.post(
        f"/app/drafts/{draft.id}/approve",
        headers={"Origin": ORIGIN},
        data=form,
    )
    reject = client.post(
        f"/app/drafts/{draft.id}/reject",
        headers={"Origin": ORIGIN},
        data=form,
    )

    assert approve.headers["location"] == "/app?error=review_failed"
    assert reject.headers["location"] == "/app?error=review_failed"
    assert store.get_draft(draft.id, actor_id=owner.id).status == "in_review"

    # Simula la decisión efectuada desde Telegram, que sí muestra la imagen.
    store.approve_draft(
        draft.id,
        actor_id=owner.id,
        expected_snapshot_hash=revision.snapshot_hash,
    )
    publish = client.post(
        f"/app/drafts/{draft.id}/publish",
        headers={"Origin": ORIGIN},
        data=form,
    )

    assert publish.headers["location"] == "/app?error=publication_failed"
    assert store.list_publication_requests(actor_id=owner.id) == []


def test_web_post_cannot_approve_truncated_evidence(dashboard_stack) -> None:
    client, store, owner, telegram = dashboard_stack
    editor, _membership = store.create_team_member(
        actor_id=owner.id,
        email="evidence-editor@colmat.test",
        username="evidence.editor",
        display_name="Editor de evidencia",
        role=Role.EDITOR,
    )
    draft, revision = store.create_draft(
        actor_id=editor.id,
        text="La trazabilidad editorial protege la lectura territorial.",
        category="correccion_publica",
        publish_at="2026-09-01T15:00:00+00:00",
        evidence={"source": "TIERRA FIRME", "detail": "x" * 2_100},
    )
    store.submit_for_review(
        draft.id,
        actor_id=editor.id,
        expected_snapshot_hash=revision.snapshot_hash,
    )
    _login(client, telegram)
    csrf_token = client.cookies.get(CSRF_COOKIE)
    assert csrf_token

    response = client.post(
        f"/app/drafts/{draft.id}/approve",
        headers={"Origin": ORIGIN},
        data={"csrf_token": csrf_token, "snapshot_hash": revision.snapshot_hash},
    )

    assert response.headers["location"] == "/app?error=review_failed"
    assert store.get_draft(draft.id, actor_id=owner.id).status == "in_review"


def test_scheduler_can_edit_schedule_but_cannot_change_mode(dashboard_stack) -> None:
    client, store, owner, telegram = dashboard_stack
    scheduler, _membership = store.create_team_member(
        actor_id=owner.id,
        email="scheduler@colmat.test",
        username="schedule.bot",
        display_name="Agenda Colmat",
        role=Role.SCHEDULER,
    )
    store.bind_telegram_chat(
        7000000001,
        telegram_user_id=7000000001,
        actor_id=owner.id,
        user_id=scheduler.id,
        purpose="control",
    )
    _login(client, telegram, identifier="schedule.bot")
    csrf_token = client.cookies.get(CSRF_COOKIE)
    assert csrf_token

    response = client.post(
        "/app/automation",
        headers={"Origin": ORIGIN},
        data={
            "csrf_token": csrf_token,
            "expected_version": "1",
            "mode": "direct",  # Un campo fabricado no amplía los permisos del rol.
            "max_posts_per_day": "5",
        },
    )

    settings = store.get_automation_settings(actor_id=scheduler.id)
    assert response.headers["location"] == "/app?notice=automation_updated"
    assert settings.mode == "human_review"
    assert settings.max_posts_per_day == 5
    assert settings.version == 2


def test_generation_endpoint_enforces_durable_daily_quota(
    dashboard_stack,
    monkeypatch,
) -> None:
    monkeypatch.setattr(platform_store_module, "MAX_GENERATION_REQUESTS_PER_USER_PER_DAY", 2)
    client, store, owner, telegram = dashboard_stack
    _login(client, telegram)
    csrf_token = client.cookies.get(CSRF_COOKIE)
    assert csrf_token

    responses = [
        client.post(
            "/app/generate",
            headers={"Origin": ORIGIN},
            data={
                "csrf_token": csrf_token,
                "request_id": f"quota-request-{index:02d}-abcdefgh",
                "brief": "9 poderes territoriales. Fuente: TIERRA FIRME, capítulo 1.",
            },
        )
        for index in range(3)
    ]

    assert [response.headers["location"] for response in responses] == [
        "/app?notice=generation_queued",
        "/app?notice=generation_queued",
        "/app?error=generation_failed",
    ]
    assert len(store.list_generation_requests(actor_id=owner.id)) == 2


def test_dashboard_queries_stay_bounded_with_many_drafts(dashboard_stack) -> None:
    client, store, owner, telegram = dashboard_stack
    for index in range(35):
        store.create_draft(
            actor_id=owner.id,
            text=f"Dato territorial número {index} con trazabilidad editorial.",
            category="dato_semana",
            publish_at="2026-09-02T14:00:00+00:00",
            evidence={"figure": str(index), "source": "TIERRA FIRME, capítulo 1"},
        )
    _login(client, telegram)
    statements: list[str] = []

    def record_statement(_connection, _cursor, statement, _parameters, _context, _many) -> None:
        statements.append(statement)

    event.listen(store.engine, "before_cursor_execute", record_statement)
    try:
        response = client.get("/app")
    finally:
        event.remove(store.engine, "before_cursor_execute", record_statement)

    assert response.status_code == 200
    assert len(statements) <= 10
    assert "Dato territorial número 34" in response.text
    assert "Dato territorial número 0" not in response.text
