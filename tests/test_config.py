from __future__ import annotations

import os
from pathlib import Path

import pytest

import colmat_x.config as config_module
from colmat_x.config import ConfigError, XCredentials, live_enabled, load_settings


def test_loads_paths_relative_to_project_root(configured_project) -> None:
    project, config_path = configured_project

    loaded = load_settings(config_path)

    assert loaded.root == project.root
    assert loaded.paths.content_dir == project.paths.content_dir
    assert loaded.brand.timezone == "America/Bogota"


def test_selected_project_never_inherits_dotenv_secrets_from_cwd(
    configured_project, monkeypatch, tmp_path: Path
) -> None:
    project, config_path = configured_project
    foreign_cwd = tmp_path / "foreign"
    foreign_cwd.mkdir()
    (foreign_cwd / ".env").write_text(
        f"COLMAT_CONFIG={config_path}\n"
        "COLMAT_STATE_DB=.state/foreign.db\n"
        "COLMAT_LIVE_ENABLED=true\n"
        "X_ACCESS_TOKEN=foreign-token\n",
        encoding="utf-8",
    )
    (project.root / ".env").write_text(
        "COLMAT_STATE_DB=.state/selected.db\nCOLMAT_LIVE_ENABLED=false\n",
        encoding="utf-8",
    )
    for name in (
        "COLMAT_CONFIG",
        "COLMAT_STATE_DB",
        "COLMAT_LIVE_ENABLED",
        "X_ACCESS_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(foreign_cwd)

    loaded = load_settings()

    assert loaded.paths.state_db == project.root / ".state" / "selected.db"
    assert live_enabled() is False
    assert "X_ACCESS_TOKEN" not in os.environ


def test_live_mode_defaults_to_disabled(monkeypatch) -> None:
    monkeypatch.delenv("COLMAT_LIVE_ENABLED", raising=False)
    assert live_enabled() is False
    monkeypatch.setenv("COLMAT_LIVE_ENABLED", "sí")
    assert live_enabled() is True
    monkeypatch.setenv("COLMAT_LIVE_ENABLED", "quizás")
    with pytest.raises(ConfigError, match="true o false"):
        live_enabled()


def test_credentials_never_accept_partial_configuration(monkeypatch) -> None:
    for name in (
        "X_CONSUMER_KEY",
        "X_CONSUMER_SECRET",
        "X_ACCESS_TOKEN",
        "X_ACCESS_TOKEN_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("X_CONSUMER_KEY", "only-one")

    with pytest.raises(ConfigError, match="Faltan credenciales"):
        XCredentials.from_environment()


def test_credentials_repr_never_exposes_secrets(monkeypatch) -> None:
    values = {
        "X_CONSUMER_KEY": "consumer-visible-secret",
        "X_CONSUMER_SECRET": "consumer-super-secret",
        "X_ACCESS_TOKEN": "access-sensitive-token",
        "X_ACCESS_TOKEN_SECRET": "access-super-secret",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    representation = repr(XCredentials.from_environment())

    assert all(value not in representation for value in values.values())


def test_rejects_unknown_timezone(configured_project) -> None:
    _, config_path = configured_project
    document = config_path.read_text(encoding="utf-8").replace("America/Bogota", "Planeta/Colmat")
    config_path.write_text(document, encoding="utf-8")

    with pytest.raises(ConfigError, match="Zona horaria desconocida"):
        load_settings(config_path)


def test_rejects_malformed_timezone_without_traceback(configured_project) -> None:
    _, config_path = configured_project
    document = config_path.read_text(encoding="utf-8").replace("America/Bogota", "../UTC")
    config_path.write_text(document, encoding="utf-8")

    with pytest.raises(ConfigError, match="Zona horaria desconocida"):
        load_settings(config_path)


def test_rejects_blank_state_override(configured_project, monkeypatch) -> None:
    _, config_path = configured_project
    monkeypatch.setenv("COLMAT_STATE_DB", "   ")

    with pytest.raises(ConfigError, match="COLMAT_STATE_DB no puede estar vacío"):
        load_settings(config_path)


def test_rejects_config_outside_working_directory_and_home(
    configured_project, monkeypatch, tmp_path: Path
) -> None:
    _, config_path = configured_project
    trusted_cwd = tmp_path / "trusted-cwd"
    trusted_home = trusted_cwd / "home"
    trusted_cwd.mkdir()
    trusted_home.mkdir()
    monkeypatch.chdir(trusted_cwd)
    monkeypatch.setenv("HOME", str(trusted_home))
    monkeypatch.setattr(config_module, "_trusted_user_home", lambda: trusted_home)

    with pytest.raises(ConfigError, match="directorio de trabajo.*perfil"):
        load_settings(config_path)


def test_rejects_config_symlink_escape(configured_project, monkeypatch, tmp_path: Path) -> None:
    project, _ = configured_project
    trusted_cwd = tmp_path / "trusted-cwd"
    trusted_home = trusted_cwd / "home"
    trusted_cwd.mkdir()
    trusted_home.mkdir()
    (trusted_cwd / "selected").symlink_to(project.root, target_is_directory=True)
    monkeypatch.chdir(trusted_cwd)
    monkeypatch.setenv("HOME", str(trusted_home))
    monkeypatch.setattr(config_module, "_trusted_user_home", lambda: trusted_home)

    with pytest.raises(ConfigError, match="directorio de trabajo.*perfil"):
        load_settings("selected/config/colmat.yaml")


def test_home_environment_cannot_move_trusted_boundary(configured_project, monkeypatch) -> None:
    monkeypatch.setenv("HOME", "/etc")

    with pytest.raises(ConfigError, match="directorio de trabajo.*perfil"):
        load_settings("/etc/passwd")


def test_filesystem_root_is_never_a_trusted_working_boundary(
    configured_project, monkeypatch
) -> None:
    monkeypatch.chdir("/")

    with pytest.raises(ConfigError, match="directorio de trabajo.*perfil"):
        load_settings("/etc/passwd")


def test_rejects_project_path_traversal(configured_project, tmp_path: Path) -> None:
    _, config_path = configured_project
    outside = tmp_path.parent / f"{tmp_path.name}-outside-content"
    outside.mkdir()
    document = config_path.read_text(encoding="utf-8").replace(
        "content_dir: content/posts", f"content_dir: ../{outside.name}"
    )
    config_path.write_text(document, encoding="utf-8")

    with pytest.raises(ConfigError, match="paths.content_dir.*dentro del proyecto"):
        load_settings(config_path)


def test_rejects_project_path_symlink_escape(configured_project, tmp_path: Path) -> None:
    project, config_path = configured_project
    outside = tmp_path.parent / f"{tmp_path.name}-outside-templates"
    outside.mkdir()
    (project.root / "escaped-templates").symlink_to(outside, target_is_directory=True)
    document = config_path.read_text(encoding="utf-8").replace(
        "templates_dir: content/templates", "templates_dir: escaped-templates"
    )
    config_path.write_text(document, encoding="utf-8")

    with pytest.raises(ConfigError, match="paths.templates_dir.*dentro del proyecto"):
        load_settings(config_path)


def test_rejects_arbitrary_absolute_state_path(configured_project, monkeypatch) -> None:
    _, config_path = configured_project
    monkeypatch.setenv("COLMAT_STATE_DB", "/etc/passwd")

    with pytest.raises(ConfigError, match="paths.state_db.*dentro del proyecto"):
        load_settings(config_path)


def test_accepts_dedicated_system_service_state_path(configured_project, monkeypatch) -> None:
    _, config_path = configured_project
    monkeypatch.setenv("COLMAT_STATE_DB", "/var/lib/colmat-x/production.db")

    loaded = load_settings(config_path)

    assert loaded.paths.state_db == Path("/var/lib/colmat-x/production.db")


def test_rejects_duplicate_safety_keys(configured_project) -> None:
    _, config_path = configured_project
    document = config_path.read_text(encoding="utf-8").replace(
        "  max_posts_per_day: 2",
        "  max_posts_per_day: 2\n  max_posts_per_day: 3",
    )
    config_path.write_text(document, encoding="utf-8")

    with pytest.raises(ConfigError, match="clave duplicada 'max_posts_per_day'"):
        load_settings(config_path)


def test_rejects_unsafe_length(configured_project) -> None:
    _, config_path = configured_project
    original = config_path.read_text(encoding="utf-8")
    config_path.write_text(original.replace("max_weighted_length: 280", "max_weighted_length: 281"))
    with pytest.raises(ConfigError, match="no puede superar"):
        load_settings(config_path)
