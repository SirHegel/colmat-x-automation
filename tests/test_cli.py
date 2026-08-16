from __future__ import annotations

from datetime import UTC, datetime, timedelta

from typer.testing import CliRunner

from colmat_x.cli import app
from colmat_x.domain import PostStatus
from colmat_x.state import StateStore
from tests.factories import make_post

runner = CliRunner()


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
