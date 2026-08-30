from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import func, select
from typer.testing import CliRunner

from colmat_x.cli import app
from colmat_x.editorial import load_editorial_policy, validate_ai_draft
from colmat_x.platform_store import Membership, PlatformStore, User
from colmat_x.x_api import XUserResponse

runner = CliRunner()


def database_url(tmp_path: Path) -> str:
    return f"sqlite+pysqlite:///{tmp_path / 'platform.db'}"


def bootstrap_owner(url: str, *, user_id: str = "owner-1") -> None:
    result = runner.invoke(
        app,
        [
            "team-bootstrap",
            "--email",
            "owner@example.org",
            "--display",
            "Owner principal",
            "--user-id",
            user_id,
            "--database-url",
            url,
        ],
    )
    assert result.exit_code == 0, result.output


def test_team_bootstrap_uses_database_url_env_and_only_accepts_empty_database(
    tmp_path: Path, monkeypatch
) -> None:
    url = database_url(tmp_path)
    monkeypatch.setenv("DATABASE_URL", url)

    created = runner.invoke(
        app,
        [
            "team-bootstrap",
            "--email",
            "Owner@Example.org",
            "--display",
            "Owner principal",
            "--user-id",
            "owner-1",
        ],
    )
    repeated = runner.invoke(
        app,
        [
            "team-bootstrap",
            "--email",
            "second@example.org",
            "--display",
            "Otro owner",
        ],
    )

    assert created.exit_code == 0
    assert "id=owner-1" in created.output
    assert "email=owner@example.org" in created.output
    assert "role=owner" in created.output
    assert repeated.exit_code == 1
    assert "base de plataforma vacía" in repeated.output


def test_team_add_and_list_show_role_and_active_state(tmp_path: Path) -> None:
    url = database_url(tmp_path)
    bootstrap_owner(url)

    added = runner.invoke(
        app,
        [
            "team-add",
            "--actor-id",
            "owner-1",
            "--email",
            "editora@example.org",
            "--display",
            "Editora territorial",
            "--role",
            "editor",
            "--user-id",
            "editor-1",
            "--database-url",
            url,
        ],
    )
    listed = runner.invoke(
        app,
        ["team-list", "--actor-id", "owner-1", "--database-url", url],
    )

    assert added.exit_code == 0, added.output
    assert "id=editor-1" in added.output
    assert "role=editor" in added.output
    assert listed.exit_code == 0, listed.output
    assert "DISPLAY" in listed.output
    assert "owner@example.org" in listed.output
    assert "Editora territorial" in listed.output
    assert "editor" in listed.output
    assert "sí" in listed.output


def test_team_add_removes_orphan_if_membership_grant_fails(tmp_path: Path, monkeypatch) -> None:
    url = database_url(tmp_path)
    bootstrap_owner(url)

    def fail_grant(*args, **kwargs):
        del args, kwargs
        raise ValueError("fallo inducido al conceder el rol")

    monkeypatch.setattr(PlatformStore, "grant_membership", fail_grant)
    result = runner.invoke(
        app,
        [
            "team-add",
            "--actor-id",
            "owner-1",
            "--email",
            "incompleta@example.org",
            "--display",
            "Cuenta incompleta",
            "--role",
            "reviewer",
            "--user-id",
            "orphan-1",
            "--database-url",
            url,
        ],
    )

    assert result.exit_code == 1
    assert "fallo inducido" in result.output
    with PlatformStore(url) as store, store.session() as session:
        assert session.scalar(select(func.count(User.id))) == 1
        assert session.scalar(select(func.count(Membership.id))) == 1
        assert session.get(User, "orphan-1") is None


def test_telegram_bind_uses_from_user_and_chat_as_separate_identities(tmp_path: Path) -> None:
    url = database_url(tmp_path)
    bootstrap_owner(url)

    result = runner.invoke(
        app,
        [
            "telegram-bind",
            "--actor-id",
            "owner-1",
            "--user-id",
            "owner-1",
            "--telegram-user-id",
            "7084929277",
            "--chat-id",
            "-1001234567890",
            "--purpose",
            "control",
            "--database-url",
            url,
        ],
    )

    assert result.exit_code == 0, result.output
    assert "telegram_user_id=7084929277" in result.output
    assert "chat_id=-1001234567890" in result.output
    with PlatformStore(url) as store:
        resolved = store.resolve_telegram_actor(
            telegram_user_id=7084929277,
            chat_id=-1001234567890,
        )
    assert resolved.id == "owner-1"


def test_x_whoami_prints_identity_only_and_honors_expected_account(
    configured_project, monkeypatch
) -> None:
    _, config_path = configured_project
    secret_marker = "never-print-this-x-secret"
    for name in (
        "X_CONSUMER_KEY",
        "X_CONSUMER_SECRET",
        "X_ACCESS_TOKEN",
        "X_ACCESS_TOKEN_SECRET",
    ):
        monkeypatch.setenv(name, f"{secret_marker}-{name}")
    received: dict[str, str | None] = {}

    def fake_identity(self, *, expected_user_id=None, expected_username=None):
        del self
        received["user_id"] = expected_user_id
        received["username"] = expected_username
        return XUserResponse(id="123456789", username="NuevaGrandaCo", name="Nueva Granada")

    monkeypatch.setattr("colmat_x.cli.XApiClient.verify_identity", fake_identity)
    result = runner.invoke(
        app,
        [
            "x-whoami",
            "--config",
            str(config_path),
            "--expected-username",
            "@NuevaGrandaCo",
            "--expected-user-id",
            "123456789",
        ],
    )

    assert result.exit_code == 0, result.output
    assert received == {"user_id": "123456789", "username": "@NuevaGrandaCo"}
    assert json.loads(result.output) == {
        "id": "123456789",
        "name": "Nueva Granada",
        "username": "NuevaGrandaCo",
    }
    assert secret_marker not in result.output


def test_database_errors_never_echo_database_url_secrets() -> None:
    secret_marker = "never-print-this-db-secret"
    result = runner.invoke(
        app,
        [
            "team-list",
            "--actor-id",
            "owner-1",
            "--database-url",
            f"missing_driver://user:{secret_marker}@db.example/colmat",
        ],
    )

    assert result.exit_code == 1
    assert "revisa DATABASE_URL" in result.output
    assert secret_marker not in result.output


def test_ai_draft_outputs_validated_non_publishable_json(configured_project, monkeypatch) -> None:
    _, config_path = configured_project
    policy_path = Path("config/editorial-policy.yaml").resolve()
    policy = load_editorial_policy(policy_path)
    draft = validate_ai_draft(
        {
            "categoria": "dato_semana",
            "institucion": "escuela_colombiana_de_filosofia",
            "texto": "Bogotá aporta 25,2 % del PIB nacional. Fuente: DANE 2024.",
            "cifra": "25,2 %",
            "fuente": "DANE 2024",
            "visual": {
                "tipo": "tipografica",
                "descripcion": "La cifra ocupa el centro sobre fondo ocre.",
                "colores": ["ocre_basal", "tinta"],
                "tipografia": "Arial",
                "incluye_retrato_persona_viva": False,
                "usa_simbolos": False,
                "serie_completa": False,
                "eje_truncado": False,
            },
        },
        policy,
    )
    monkeypatch.setenv("MINIMAX_API_KEY", "test-key-not-for-network")
    received: dict[str, object] = {}

    def fake_generate(self, brief, received_policy, *, category=None, institution=None):
        del self
        received.update(
            brief=brief,
            policy=received_policy,
            category=category,
            institution=institution,
        )
        return draft

    monkeypatch.setattr("colmat_x.cli.MiniMaxClient.generate_draft", fake_generate)
    result = runner.invoke(
        app,
        [
            "ai-draft",
            "Una cifra semanal comprobable",
            "--config",
            str(config_path),
            "--policy",
            str(policy_path),
            "--category",
            "dato_semana",
            "--institution",
            "escuela_colombiana_de_filosofia",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "draft"
    assert payload["publication_authorized"] is False
    assert payload["requires_human_approval"] is True
    assert "no se publicó" in payload["notice"]
    assert payload["categoria"] == "dato_semana"
    assert 0 <= payload["assessment"]["score"] <= 100
    assert payload["assessment"]["publication_authorized"] is False
    assert "no predice ni garantiza" in payload["assessment"]["disclaimer"]
    assert received["brief"] == "Una cifra semanal comprobable"
    assert str(received["category"]) == "dato_semana"
    assert str(received["institution"]) == "escuela_colombiana_de_filosofia"
