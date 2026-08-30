from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from typer.testing import CliRunner

from colmat_x.cli import _load_trusted_worker_environment, app
from colmat_x.domain import PostStatus
from colmat_x.platform_store import AutomationRunStatus, PlatformStore
from colmat_x.rbac import Role
from colmat_x.state import StateStore
from tests.factories import make_post

runner = CliRunner()
POLICY_PATH = Path("config/editorial-policy.yaml").resolve()
AUTOMATION_PATH = Path("config/automation.yaml").resolve()


def platform_database_url(tmp_path: Path) -> str:
    return f"sqlite+pysqlite:///{tmp_path / 'platform.db'}"


def persisted_test_slot(*, mode: str = "human_review") -> dict[str, object]:
    return {
        "id": "dato-manana",
        "at": "08:30",
        "mode": mode,
        "category": "dato_semana",
        "institution": "colmat",
        "brief": "Explica una cifra territorial con una fuente primaria verificable.",
        "generate_image": True,
        "evidence": {
            "verified": False,
            "reference": None,
            "expected_figure": None,
            "expected_source": None,
        },
    }


def add_due_post(project) -> None:
    (project.paths.content_dir / "due.yaml").write_text(
        """
id: colmat-cli-001
template: idea
publish_at: "2020-01-01T09:00:00-05:00"
data:
  titulo: Una idea
  desarrollo: Un texto listo para revisar.
  cierre: Colmat
""".strip(),
        encoding="utf-8",
    )


def test_cli_editorial_flow_stays_in_dry_run(configured_project, monkeypatch) -> None:
    project, config_path = configured_project
    add_due_post(project)
    monkeypatch.setenv("COLMAT_LIVE_ENABLED", "false")

    validated = runner.invoke(app, ["validate", "--config", str(config_path)])
    previewed = runner.invoke(
        app,
        ["preview", "--config", str(config_path), "--id", "colmat-cli-001"],
    )
    snapshot = next(
        line.split(": ", 1)[1]
        for line in previewed.output.splitlines()
        if line.startswith("snapshot de aprobación:")
    )
    approved = runner.invoke(
        app,
        [
            "approve",
            "colmat-cli-001",
            "--by",
            "Equipo editorial",
            "--snapshot",
            snapshot,
            "--config",
            str(config_path),
        ],
    )
    simulated = runner.invoke(app, ["run-due", "--config", str(config_path)])
    status = runner.invoke(app, ["status", "--config", str(config_path)])

    assert validated.exit_code == 0
    assert "1 contenido(s) fuente válidos" in validated.output
    assert previewed.exit_code == 0
    assert "Un texto listo para revisar" in previewed.output
    assert approved.exit_code == 0
    assert "aprobado por Equipo editorial" in approved.output
    assert simulated.exit_code == 0
    assert "SIMULACIÓN" in simulated.output
    assert status.exit_code == 0
    assert "scheduled" in status.output


def test_cli_live_mode_needs_both_safety_gates(configured_project, monkeypatch) -> None:
    project, config_path = configured_project
    add_due_post(project)
    monkeypatch.setenv("COLMAT_LIVE_ENABLED", "false")

    result = runner.invoke(app, ["run-due", "--config", str(config_path), "--live"])

    assert result.exit_code == 1
    assert "Publicación bloqueada" in result.output


def test_doctor_can_verify_credentials_while_live_mode_stays_disabled(
    configured_project, monkeypatch
) -> None:
    _, config_path = configured_project
    monkeypatch.setenv("COLMAT_LIVE_ENABLED", "false")
    names = (
        "X_CONSUMER_KEY",
        "X_CONSUMER_SECRET",
        "X_ACCESS_TOKEN",
        "X_ACCESS_TOKEN_SECRET",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)

    missing = runner.invoke(
        app,
        ["doctor", "--credentials", "--config", str(config_path)],
    )
    for name in names:
        monkeypatch.setenv(name, f"test-{name.casefold()}")
    present = runner.invoke(
        app,
        ["doctor", "--credentials", "--config", str(config_path)],
    )

    assert missing.exit_code == 1
    assert "Faltan credenciales" in missing.output
    assert present.exit_code == 0
    assert "credenciales presentes; publicación real deshabilitada" in present.output


def test_cli_deleting_last_yaml_cancels_queued_post(configured_project) -> None:
    project, config_path = configured_project
    add_due_post(project)
    previewed = runner.invoke(
        app,
        ["preview", "--config", str(config_path), "--id", "colmat-cli-001"],
    )
    snapshot = next(
        line.split(": ", 1)[1]
        for line in previewed.output.splitlines()
        if line.startswith("snapshot de aprobación:")
    )
    approved = runner.invoke(
        app,
        [
            "approve",
            "colmat-cli-001",
            "--by",
            "Equipo",
            "--snapshot",
            snapshot,
            "--config",
            str(config_path),
        ],
    )
    assert approved.exit_code == 0
    (project.paths.content_dir / "due.yaml").unlink()

    synced = runner.invoke(app, ["sync", "--config", str(config_path)])
    status = runner.invoke(app, ["status", "--config", str(config_path)])

    assert synced.exit_code == 0
    assert "1 canceladas" in synced.output
    assert "cancelled" in status.output


def test_cli_restore_accepts_a_changed_snapshot_as_draft(configured_project) -> None:
    project, config_path = configured_project
    post = make_post(post_id="colmat-cli-restore", text="Texto anterior")
    store = StateStore(project.paths.state_db)
    store.sync_posts([post])
    store.sync_posts([])
    (project.paths.content_dir / "restored.yaml").write_text(
        """
id: colmat-cli-restore
template: idea
publish_at: "2026-08-20T09:00:00-05:00"
data:
  titulo: Texto restaurado
  desarrollo: Esta versión cambió y necesita una aprobación nueva.
  cierre: Colmat
""".strip(),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["restore", "colmat-cli-restore", "--config", str(config_path)],
    )

    assert result.exit_code == 0
    restored = store.list_posts()[0]
    assert restored.status == PostStatus.DRAFT
    assert restored.rendered_text.startswith("Texto restaurado")
    assert restored.approval_hash is None


def test_cli_recovers_expired_lease_before_syncing_changed_yaml(configured_project) -> None:
    project, config_path = configured_project
    add_due_post(project)
    claim_time = datetime.now(UTC) - timedelta(hours=1)
    post = make_post(
        post_id="colmat-cli-001",
        text="Snapshot distinto al YAML vigente",
        publish_at=claim_time - timedelta(minutes=1),
    )
    store = StateStore(project.paths.state_db)
    store.sync_posts([post], now=claim_time)
    store.approve(post.id, "Equipo", post.approval_snapshot_hash, now=claim_time)
    assert store.claim(post.id, claim_time, lease_minutes=10) is not None

    result = runner.invoke(app, ["run-due", "--config", str(config_path)])

    assert result.exit_code == 2
    assert "pasaron a unknown" in result.output
    assert "concílialas manualmente" in result.output
    assert store.list_posts()[0].status == PostStatus.UNKNOWN


def test_help_exposes_scheduler_surface() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "scheduler seguro" in result.output
    for command in (
        "automation-validate",
        "automation-calendar",
        "automation-status",
        "automation-sync",
        "automation-mode",
        "automation-enable",
        "automation-disable",
        "generation-run",
    ):
        assert command in result.output


def test_generation_worker_env_file_overrides_inherited_gate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    env_file = tmp_path / "generation-worker.env"
    env_file.write_text("COLMAT_GENERATION_ENABLED=false\n", encoding="utf-8")
    env_file.chmod(0o600)
    monkeypatch.setenv("COLMAT_GENERATION_ENABLED", "true")

    result = runner.invoke(app, ["generation-run", "--env-file", str(env_file)])

    assert result.exit_code == 1
    assert "COLMAT_GENERATION_ENABLED=true" in result.output
    assert "MINIMAX_API_KEY" not in result.output


def test_worker_env_loader_preserves_literals_without_interpolation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    env_file = tmp_path / "literal-worker.env"
    env_file.write_text(
        "COLMAT_TEST_DOLLAR='cash$and${HOME}'\n"
        "COLMAT_TEST_QUOTE='single\\'quote'\n"
        "COLMAT_TEST_BACKSLASH='back\\\\slash'\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    monkeypatch.setenv("COLMAT_TEST_DOLLAR", "inherited")

    _load_trusted_worker_environment(env_file)

    assert os.environ["COLMAT_TEST_DOLLAR"] == "cash$and${HOME}"
    assert os.environ["COLMAT_TEST_QUOTE"] == "single'quote"
    assert os.environ["COLMAT_TEST_BACKSLASH"] == "back\\slash"


def test_worker_env_loader_rejects_missing_symlink_and_broad_permissions(tmp_path: Path) -> None:
    missing = runner.invoke(
        app,
        ["generation-run", "--env-file", str(tmp_path / "missing.env")],
    )
    target = tmp_path / "target.env"
    target.write_text("COLMAT_GENERATION_ENABLED=false\n", encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "linked.env"
    link.symlink_to(target)
    linked = runner.invoke(app, ["generation-run", "--env-file", str(link)])
    target.chmod(0o640)
    broad = runner.invoke(app, ["generation-run", "--env-file", str(target)])

    assert missing.exit_code == 1
    assert "No se pudo abrir" in missing.output
    assert linked.exit_code == 1
    assert "no un enlace" in linked.output
    assert broad.exit_code == 1
    assert "permisos 0600" in broad.output


def test_telegram_commands_registers_and_verifies_private_menu(
    tmp_path: Path,
    monkeypatch,
) -> None:
    env_file = tmp_path / "telegram-worker.env"
    env_file.write_text("TELEGRAM_BOT_TOKEN='123456:test-token'\n", encoding="utf-8")
    env_file.chmod(0o600)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "inherited-token")
    captured: dict[str, object] = {}

    class FakeTelegramClient:
        def __init__(self, credentials) -> None:
            captured["token"] = credentials.token

        def set_my_commands(self, commands, *, scope):
            captured["commands"] = [command.as_payload() for command in commands]
            captured["scope"] = scope
            return True

        def get_my_commands(self, *, scope):
            assert scope == captured["scope"]
            return captured["commands"]

    monkeypatch.setattr("colmat_x.cli.TelegramApiClient", FakeTelegramClient)

    result = runner.invoke(app, ["telegram-commands", "--env-file", str(env_file)])

    assert result.exit_code == 0, result.output
    assert captured["token"] == "123456:test-token"
    assert captured["scope"] == {"type": "all_private_chats"}
    assert json.loads(result.output) == {
        "commands": [
            "estado",
            "equipo",
            "calendario",
            "modo",
            "generar",
            "publicar",
            "ayuda",
        ],
        "registered": True,
    }


def test_generation_worker_rejects_any_x_credentials(monkeypatch) -> None:
    monkeypatch.setenv("COLMAT_GENERATION_ENABLED", "true")
    monkeypatch.setenv("X_CONSUMER_KEY", "must-not-enter-generation-worker")

    result = runner.invoke(app, ["generation-run"])

    assert result.exit_code == 1
    assert "rechaza credenciales de X" in result.output
    assert "must-not-enter-generation-worker" not in result.output


def test_publication_worker_rejects_any_minimax_credentials(monkeypatch) -> None:
    monkeypatch.setenv("MINIMAX_API_KEY", "must-not-enter-publication-worker")

    result = runner.invoke(app, ["publication-run", "--live"])

    assert result.exit_code == 1
    assert "rechaza credenciales de MiniMax" in result.output
    assert "must-not-enter-publication-worker" not in result.output


def test_team_commands_accept_show_and_persist_usernames_and_scheduler_role(tmp_path: Path) -> None:
    url = platform_database_url(tmp_path)
    owner = runner.invoke(
        app,
        [
            "team-bootstrap",
            "--email",
            "owner@example.org",
            "--display",
            "Owner",
            "--username",
            "owner_colmat",
            "--user-id",
            "owner-1",
            "--database-url",
            url,
        ],
    )
    scheduler = runner.invoke(
        app,
        [
            "team-add",
            "--actor-id",
            "owner-1",
            "--email",
            "scheduler@example.org",
            "--display",
            "Agenda editorial",
            "--username",
            "agenda_colmat",
            "--role",
            "scheduler",
            "--user-id",
            "scheduler-1",
            "--database-url",
            url,
        ],
    )
    listed = runner.invoke(
        app,
        ["team-list", "--actor-id", "owner-1", "--database-url", url],
    )

    assert owner.exit_code == 0, owner.output
    assert "username=@owner_colmat" in owner.output
    assert scheduler.exit_code == 0, scheduler.output
    assert "username=@agenda_colmat" in scheduler.output
    assert "role=scheduler" in scheduler.output
    assert listed.exit_code == 0
    assert "USERNAME" in listed.output
    assert "@owner_colmat" in listed.output
    assert "@agenda_colmat" in listed.output
    with PlatformStore(url) as store:
        assert store.get_user("owner-1").username == "owner_colmat"
        assert store.get_user("scheduler-1").username == "agenda_colmat"


def test_automation_validate_and_calendar_are_read_only_views() -> None:
    validated = runner.invoke(
        app,
        [
            "automation-validate",
            "--automation-config",
            str(AUTOMATION_PATH),
            "--policy",
            str(POLICY_PATH),
        ],
    )
    calendar = runner.invoke(
        app,
        [
            "automation-calendar",
            "--automation-config",
            str(AUTOMATION_PATH),
            "--policy",
            str(POLICY_PATH),
            "--days",
            "2",
            "--start-date",
            "2026-08-30",
        ],
    )

    assert validated.exit_code == 0, validated.output
    assert "timezone=America/Bogota" in validated.output
    assert "slots=2" in validated.output
    assert "no se publicó nada" in validated.output
    assert calendar.exit_code == 0, calendar.output
    assert calendar.output.count("colmat:auto:v1:2026-08-30") == 1
    assert calendar.output.count("colmat:auto:v1:2026-08-31") == 2
    assert "colmat:auto:v1:2026-08-30:dato-manana" not in calendar.output
    assert "colmat:auto:v1:2026-08-31:dato-manana" in calendar.output
    assert "dato-manana" in calendar.output
    assert "territorio-tarde" in calendar.output
    assert "human_review" in calendar.output


def test_automation_calendar_rejects_unsafe_horizon_and_noncanonical_date() -> None:
    too_long = runner.invoke(
        app,
        ["automation-calendar", "--days", "32", "--start-date", "2026-08-29"],
    )
    malformed = runner.invoke(
        app,
        ["automation-calendar", "--days", "1", "--start-date", "20260829"],
    )

    assert too_long.exit_code == 1
    assert "days debe estar entre 1 y 31" in too_long.output
    assert malformed.exit_code == 1
    assert "YYYY-MM-DD" in malformed.output


def test_automation_status_returns_settings_and_recent_runs(tmp_path: Path) -> None:
    url = platform_database_url(tmp_path)
    with PlatformStore(url) as store:
        owner, _membership = store.bootstrap_owner(
            email="owner@example.org",
            display_name="Owner",
            username="owner_colmat",
            user_id="owner-1",
        )
        settings = store.update_automation_settings(
            actor_id=owner.id,
            expected_version=1,
            enabled=True,
            slots=[persisted_test_slot()],
        )
        assert settings.version == 2
        run = store.claim_automation_run(
            actor_id=owner.id,
            idempotency_key="colmat:auto:v1:2026-08-29:dato-manana",
            slot_id="dato-manana",
            scheduled_for="2026-08-29T08:30:00-05:00",
            slot_snapshot=persisted_test_slot(),
            mode="human_review",
        )

    result = runner.invoke(
        app,
        [
            "automation-status",
            "--actor-id",
            "owner-1",
            "--database-url",
            url,
            "--limit",
            "10",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["settings"]["enabled"] is True
    assert payload["settings"]["mode"] == "human_review"
    assert payload["settings"]["version"] == 2
    assert payload["settings"]["direct_authorized"] is False
    assert payload["runs"] == [
        {
            "claimed_at": run.claimed_at.isoformat(),
            "draft_id": None,
            "error": None,
            "finished_at": None,
            "id": run.id,
            "idempotency_key": "colmat:auto:v1:2026-08-29:dato-manana",
            "mode": "human_review",
            "scheduled_for": run.scheduled_for.isoformat(),
            "settings_version": run.settings_version,
            "slot_hash": run.slot_hash,
            "slot_id": "dato-manana",
            "status": "claimed",
        }
    ]


def test_automation_mode_and_enable_use_cas_and_existing_direct_kill_switch(
    tmp_path: Path, monkeypatch
) -> None:
    url = platform_database_url(tmp_path)
    with PlatformStore(url) as store:
        owner, _membership = store.bootstrap_owner(
            email="owner@example.org",
            display_name="Owner",
            username="owner_colmat",
            user_id="owner-1",
        )
        store.update_automation_settings(
            actor_id=owner.id,
            expected_version=1,
            slots=[persisted_test_slot()],
        )
    monkeypatch.delenv("COLMAT_DIRECT_PUBLISH_ENABLED", raising=False)

    blocked_mode = runner.invoke(
        app,
        [
            "automation-mode",
            "direct",
            "--actor-id",
            "owner-1",
            "--expected-version",
            "2",
            "--database-url",
            url,
        ],
    )
    assert blocked_mode.exit_code == 1
    assert "COLMAT_DIRECT_PUBLISH_ENABLED" in blocked_mode.output

    monkeypatch.setenv("COLMAT_DIRECT_PUBLISH_ENABLED", "true")
    changed = runner.invoke(
        app,
        [
            "automation-mode",
            "direct",
            "--actor-id",
            "owner-1",
            "--expected-version",
            "2",
            "--database-url",
            url,
        ],
    )
    stale = runner.invoke(
        app,
        [
            "automation-mode",
            "human_review",
            "--actor-id",
            "owner-1",
            "--expected-version",
            "2",
            "--database-url",
            url,
        ],
    )
    monkeypatch.delenv("COLMAT_DIRECT_PUBLISH_ENABLED")
    blocked_enable = runner.invoke(
        app,
        [
            "automation-enable",
            "--actor-id",
            "owner-1",
            "--expected-version",
            "3",
            "--database-url",
            url,
        ],
    )
    monkeypatch.setenv("COLMAT_DIRECT_PUBLISH_ENABLED", "true")
    enabled = runner.invoke(
        app,
        [
            "automation-enable",
            "--actor-id",
            "owner-1",
            "--expected-version",
            "3",
            "--database-url",
            url,
        ],
    )

    assert changed.exit_code == 0, changed.output
    assert "mode=direct" in changed.output
    assert "version=3" in changed.output
    assert "direct_authorized=sí" in changed.output
    assert stale.exit_code == 1
    assert "versión actual 3" in stale.output
    assert blocked_enable.exit_code == 1
    assert "COLMAT_DIRECT_PUBLISH_ENABLED" in blocked_enable.output
    assert enabled.exit_code == 0, enabled.output
    assert "version=4" in enabled.output
    assert "no se ejecutaron runs ni publicaciones" in enabled.output
    with PlatformStore(url) as store:
        settings = store.get_automation_settings(actor_id="owner-1")
        assert settings.mode == "direct"
        assert settings.enabled is True
        assert settings.version == 4
        assert settings.slots[0]["mode"] == "direct"
        assert store.list_automation_runs(actor_id="owner-1") == []


def test_automation_sync_then_enable_and_disable_are_explicit_and_readable(
    tmp_path: Path,
) -> None:
    url = platform_database_url(tmp_path)
    with PlatformStore(url) as store:
        store.bootstrap_owner(
            email="owner@example.org",
            display_name="Owner",
            username="owner_colmat",
            user_id="owner-1",
        )

    synced = runner.invoke(
        app,
        [
            "automation-sync",
            "--actor-id",
            "owner-1",
            "--expected-version",
            "1",
            "--automation-config",
            str(AUTOMATION_PATH),
            "--policy",
            str(POLICY_PATH),
            "--database-url",
            url,
        ],
    )
    enabled = runner.invoke(
        app,
        [
            "automation-enable",
            "--actor-id",
            "owner-1",
            "--expected-version",
            "2",
            "--database-url",
            url,
        ],
    )
    disabled = runner.invoke(
        app,
        [
            "automation-disable",
            "--actor-id",
            "owner-1",
            "--expected-version",
            "3",
            "--database-url",
            url,
        ],
    )

    assert synced.exit_code == 0, synced.output
    assert "slots=2" in synced.output
    assert "enabled=no" in synced.output
    assert enabled.exit_code == 0, enabled.output
    assert disabled.exit_code == 0, disabled.output
    assert "enabled=no" in disabled.output
    with PlatformStore(url) as store:
        settings = store.get_automation_settings(actor_id="owner-1")
        assert settings.enabled is False
        assert settings.version == 4
        assert settings.timezone == "America/Bogota"
        assert settings.max_posts_per_day == 2
        assert settings.generate_images is True
        assert [slot["id"] for slot in settings.slots] == [
            "dato-manana",
            "territorio-tarde",
        ]
        assert settings.slots[0]["evidence"] == {
            "verified": False,
            "reference": None,
            "expected_figure": None,
            "expected_source": None,
        }
        assert store.list_automation_runs(actor_id="owner-1") == []


def test_automation_run_is_a_noop_when_persisted_settings_are_paused(
    tmp_path: Path, monkeypatch
) -> None:
    url = platform_database_url(tmp_path)
    with PlatformStore(url) as store:
        store.bootstrap_owner(
            email="owner@example.org",
            display_name="Owner",
            username="owner_colmat",
            user_id="owner-1",
        )
    monkeypatch.setenv("COLMAT_AUTOMATION_SCHEDULER_ID", "owner-1")
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)

    result = runner.invoke(
        app,
        [
            "automation-run",
            "--database-url",
            url,
            "--policy",
            str(POLICY_PATH),
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "enabled": False,
        "mode": "human_review",
        "processed": 0,
        "review_notifications": [],
        "settings_version": 1,
        "status": "paused",
    }


def test_automation_run_reconciles_stale_claims_before_paused_noop(
    tmp_path: Path, monkeypatch
) -> None:
    url = platform_database_url(tmp_path)
    slot = persisted_test_slot()
    scheduled_for = datetime(2026, 8, 29, 13, 30, tzinfo=UTC)
    with PlatformStore(url) as store:
        owner, _membership = store.bootstrap_owner(
            email="owner@example.org",
            display_name="Owner",
            username="owner_colmat",
            user_id="owner-1",
        )
        settings = store.update_automation_settings(
            actor_id=owner.id,
            expected_version=1,
            enabled=True,
            slots=[slot],
            now=scheduled_for,
        )
        run = store.claim_automation_run(
            actor_id=owner.id,
            idempotency_key="colmat:auto:v1:2026-08-29:dato-manana",
            slot_id="dato-manana",
            scheduled_for=scheduled_for,
            slot_snapshot=slot,
            now=scheduled_for,
        )
        store.update_automation_settings(
            actor_id=owner.id,
            expected_version=settings.version,
            enabled=False,
            now=scheduled_for + timedelta(seconds=1),
        )
    monkeypatch.setenv("COLMAT_AUTOMATION_SCHEDULER_ID", "owner-1")
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)

    result = runner.invoke(
        app,
        [
            "automation-run",
            "--database-url",
            url,
            "--policy",
            str(POLICY_PATH),
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["status"] == "paused"
    with PlatformStore(url) as store:
        persisted = store.list_automation_runs(actor_id="owner-1")
        assert len(persisted) == 1
        assert persisted[0].id == run.id
        assert persisted[0].status_value is AutomationRunStatus.FAILED
        assert (
            store.claim_automation_run(
                actor_id="owner-1",
                idempotency_key="colmat:auto:v1:2026-08-29:dato-manana",
                slot_id="dato-manana",
                scheduled_for=scheduled_for,
                slot_snapshot=slot,
                now=scheduled_for + timedelta(days=2),
            ).status_value
            is AutomationRunStatus.FAILED
        )


def test_automation_run_rejects_direct_before_loading_network_clients(
    tmp_path: Path, monkeypatch
) -> None:
    url = platform_database_url(tmp_path)
    monkeypatch.setenv("COLMAT_DIRECT_PUBLISH_ENABLED", "true")
    monkeypatch.setenv("COLMAT_LIVE_ENABLED", "true")
    with PlatformStore(url) as store:
        owner, _membership = store.bootstrap_owner(
            email="owner@example.org",
            display_name="Owner",
            username="owner_colmat",
            user_id="owner-1",
        )
        store.update_automation_settings(
            actor_id=owner.id,
            expected_version=1,
            mode="direct",
            enabled=True,
            slots=[persisted_test_slot(mode="direct")],
            min_engagement_score=80,
        )
    monkeypatch.setenv("COLMAT_AUTOMATION_SCHEDULER_ID", "owner-1")
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    result = runner.invoke(
        app,
        [
            "automation-run",
            "--database-url",
            url,
            "--policy",
            str(POLICY_PATH),
        ],
    )

    assert result.exit_code == 1
    assert "exige además la opción --live" in result.output
    with PlatformStore(url) as store:
        assert store.list_automation_runs(actor_id="owner-1") == []


def test_automation_run_builds_runtime_only_from_persisted_settings(
    tmp_path: Path, monkeypatch
) -> None:
    url = platform_database_url(tmp_path)
    slot = persisted_test_slot()
    slot["brief"] = "Brief persistido que no proviene del archivo YAML local."
    with PlatformStore(url) as store:
        owner, _membership = store.bootstrap_owner(
            email="owner@example.org",
            display_name="Owner",
            username="owner_colmat",
            user_id="owner-1",
        )
        author = store.create_user(
            actor_id=owner.id,
            email="author@example.org",
            display_name="Automation Author",
            user_id="author-1",
        )
        store.grant_membership(author.id, Role.EDITOR, actor_id=owner.id)
        store.update_automation_settings(
            actor_id=owner.id,
            expected_version=1,
            enabled=True,
            slots=[slot],
            min_engagement_score=91,
            max_posts_per_day=1,
        )
    monkeypatch.setenv("COLMAT_AUTOMATION_SCHEDULER_ID", "owner-1")
    monkeypatch.setenv("COLMAT_AUTOMATION_AUTHOR_ID", "author-1")
    monkeypatch.setenv("COLMAT_TELEGRAM_ALERT_CHAT_ID", "778899")
    monkeypatch.setenv("COLMAT_TELEGRAM_REVIEWER_USER_ID", "778899")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test-token-for-cli")
    monkeypatch.setenv("MINIMAX_API_KEY", "test-key-no-network")
    captured: dict[str, object] = {}

    class FakeDailyAutomation:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run_due(self, *, environ, progress):
            captured["environ"] = environ
            captured["progress"] = progress
            return ()

    monkeypatch.setattr("colmat_x.cli.DailyAutomation", FakeDailyAutomation)

    result = runner.invoke(
        app,
        [
            "automation-run",
            "--database-url",
            url,
            "--policy",
            str(POLICY_PATH),
        ],
    )

    assert result.exit_code == 0, result.output
    runtime_config = captured["config"]
    assert runtime_config.daily_limit == 1
    assert runtime_config.direct_min_engagement_score == 91
    assert runtime_config.slots[0].brief == slot["brief"]
    assert json.loads(result.output)["processed"] == 0
