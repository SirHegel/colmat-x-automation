from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from colmat_x.cli import _load_trusted_worker_environment, app
from colmat_x.config import ConfigError
from colmat_x.credential_cipher import PRIVATE_KEY_ENV, PUBLIC_KEY_ENV, CredentialCipher
from colmat_x.platform_store import PlatformStore

runner = CliRunner()
SECRET = "sk-cli-secret-value-000123"


def database_url(tmp_path: Path) -> str:
    return f"sqlite+pysqlite:///{tmp_path / 'platform.db'}"


def bootstrap(url: str) -> None:
    with PlatformStore(url) as store:
        store.bootstrap_owner(
            email="owner@example.org",
            display_name="Owner",
            username="owner_colmat",
            user_id="owner-1",
        )


def test_worker_env_loader_loads_each_provider_and_the_private_key_from_own_files(
    tmp_path: Path, monkeypatch
) -> None:
    anthropic_file = tmp_path / "anthropic.env"
    anthropic_file.write_text("ANTHROPIC_API_KEY=anthropic-secret\n", encoding="utf-8")
    anthropic_file.chmod(0o600)
    private_key, _public = CredentialCipher.generate_keypair()
    key_file = tmp_path / "sobres.env"
    key_file.write_text(f"{PRIVATE_KEY_ENV}={private_key}\n", encoding="utf-8")
    key_file.chmod(0o600)
    primary = tmp_path / "worker.env"
    primary.write_text(
        f"COLMAT_ANTHROPIC_ENV_FILE='{anthropic_file}'\n"
        f"COLMAT_CREDENTIAL_PRIVATE_KEY_ENV_FILE='{key_file}'\n",
        encoding="utf-8",
    )
    primary.chmod(0o600)
    # setenv registra la restauración: el cargador escribe en os.environ y nada
    # debe quedar en el entorno de los tests siguientes.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "inherited-anthropic")
    monkeypatch.setenv(PRIVATE_KEY_ENV, "inherited-private")

    _load_trusted_worker_environment(primary)

    assert os.environ["ANTHROPIC_API_KEY"] == "anthropic-secret"
    assert os.environ[PRIVATE_KEY_ENV] == private_key

    wrong = tmp_path / "wrong.env"
    wrong.write_text("OPENAI_API_KEY=misplaced-secret\n", encoding="utf-8")
    wrong.chmod(0o600)
    mixed = tmp_path / "mixed.env"
    mixed.write_text(f"COLMAT_GEMINI_ENV_FILE='{wrong}'\n", encoding="utf-8")
    mixed.chmod(0o600)
    with pytest.raises(ConfigError, match="debe contener solo GEMINI_API_KEY") as error:
        _load_trusted_worker_environment(mixed)
    assert "misplaced-secret" not in str(error.value)


@pytest.mark.parametrize(
    ("variable", "expected"),
    [
        ("OPENAI_API_KEY", "rechaza credenciales de OpenAI"),
        ("GEMINI_API_KEY", "rechaza credenciales de Google Gemini"),
        (PRIVATE_KEY_ENV, "clave privada"),
    ],
)
def test_publication_worker_rejects_every_ai_secret(monkeypatch, variable, expected) -> None:
    for name in ("MINIMAX_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(variable, "must-not-enter-publication-worker")

    result = runner.invoke(app, ["publication-run", "--live"])

    assert result.exit_code == 1
    assert expected in result.output
    assert "must-not-enter-publication-worker" not in result.output


def test_ai_engine_and_credential_commands_use_cas_and_never_print_secrets(
    tmp_path: Path, monkeypatch
) -> None:
    url = database_url(tmp_path)
    bootstrap(url)
    _private, public = CredentialCipher.generate_keypair()
    monkeypatch.setenv(PUBLIC_KEY_ENV, public)
    monkeypatch.delenv(PRIVATE_KEY_ENV, raising=False)

    stored = runner.invoke(
        app,
        [
            "ai-credential-set",
            "openai",
            "--actor-id",
            "owner-1",
            "--no-verify",
            "--database-url",
            url,
        ],
        input=f"{SECRET}\n",
    )
    assert stored.exit_code == 0, stored.output
    assert "proveedor=openai" in stored.output
    assert f"final=…{SECRET[-4:]}" in stored.output
    assert "comprobación=omitida" in stored.output
    assert SECRET not in stored.output

    status = runner.invoke(
        app, ["ai-engine-status", "--actor-id", "owner-1", "--database-url", url]
    )
    assert status.exit_code == 0, status.output
    payload = json.loads(status.output)
    assert payload["selection"]["text_provider"] == "minimax"
    assert payload["encryption"] == {
        "can_decrypt": False,
        "can_encrypt": True,
        "key_id": payload["encryption"]["key_id"],
        "private_key_variable": PRIVATE_KEY_ENV,
        "public_key_variable": PUBLIC_KEY_ENV,
    }
    openai_entry = next(item for item in payload["providers"] if item["name"] == "openai")
    assert openai_entry["stored"]["last_four"] == SECRET[-4:]
    assert SECRET not in status.output
    assert "envelope" not in status.output

    changed = runner.invoke(
        app,
        [
            "ai-engine",
            "--actor-id",
            "owner-1",
            "--expected-version",
            "1",
            "--text-provider",
            "anthropic",
            "--text-model",
            "claude-sonnet-5",
            "--image-provider",
            "openai",
            "--database-url",
            url,
        ],
    )
    assert changed.exit_code == 0, changed.output
    assert "texto=anthropic/claude-sonnet-5" in changed.output
    assert "imagen=openai/gpt-image-2.5-sunburst" in changed.output
    assert "version=2" in changed.output

    stale = runner.invoke(
        app,
        [
            "ai-engine",
            "--actor-id",
            "owner-1",
            "--expected-version",
            "1",
            "--text-provider",
            "gemini",
            "--database-url",
            url,
        ],
    )
    assert stale.exit_code == 1
    assert "versión actual 2" in stale.output

    invalid = runner.invoke(
        app,
        [
            "ai-engine",
            "--actor-id",
            "owner-1",
            "--expected-version",
            "2",
            "--image-provider",
            "anthropic",
            "--database-url",
            url,
        ],
    )
    assert invalid.exit_code == 1
    assert "uno de" in invalid.output

    requested = runner.invoke(
        app, ["ai-credential-verify", "openai", "--actor-id", "owner-1", "--database-url", url]
    )
    assert requested.exit_code == 0, requested.output
    assert "Verificación solicitada" in requested.output

    deleted = runner.invoke(
        app, ["ai-credential-delete", "openai", "--actor-id", "owner-1", "--database-url", url]
    )
    assert deleted.exit_code == 0
    assert "Clave eliminada" in deleted.output
    missing = runner.invoke(
        app, ["ai-credential-delete", "openai", "--actor-id", "owner-1", "--database-url", url]
    )
    assert "No había clave" in missing.output

    with PlatformStore(url) as store:
        actions = [event.action for event in store.list_audit_events(actor_id="owner-1", limit=200)]
    for action in (
        "ai_credential.stored",
        "ai_engine.settings_updated",
        "ai_credential.verification_requested",
        "ai_credential.deleted",
    ):
        assert action in actions


def test_credential_set_without_public_key_fails_closed(tmp_path: Path, monkeypatch) -> None:
    url = database_url(tmp_path)
    bootstrap(url)
    monkeypatch.delenv(PUBLIC_KEY_ENV, raising=False)
    monkeypatch.delenv(PRIVATE_KEY_ENV, raising=False)

    result = runner.invoke(
        app,
        [
            "ai-credential-set",
            "gemini",
            "--actor-id",
            "owner-1",
            "--no-verify",
            "--database-url",
            url,
        ],
        input=f"{SECRET}\n",
    )

    assert result.exit_code == 1
    assert PUBLIC_KEY_ENV in result.output
    assert SECRET not in result.output


def test_keypair_command_prints_two_urlsafe_keys() -> None:
    result = runner.invoke(app, ["ai-credential-keypair"])

    assert result.exit_code == 0
    lines = dict(line.split("=", 1) for line in result.output.splitlines() if "=" in line)
    assert set(lines) >= {PRIVATE_KEY_ENV, PUBLIC_KEY_ENV}
    for value in (lines[PRIVATE_KEY_ENV], lines[PUBLIC_KEY_ENV]):
        assert len(base64.urlsafe_b64decode(value)) == 32
