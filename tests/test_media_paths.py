from pathlib import Path

import pytest

import colmat_x.media_paths as media_paths
from colmat_x.config import ConfigError


def test_configured_media_root_maps_only_exact_static_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    home.mkdir()
    monkeypatch.setattr(media_paths, "_trusted_user_home", lambda: home)
    project_media = (project / ".state/media").resolve()
    user_media = (home / ".local/share/colmat/media").resolve()

    assert media_paths.configured_worker_media_root(None, project_root=project) == project_media
    assert (
        media_paths.configured_worker_media_root("project", project_root=project) == project_media
    )
    assert (
        media_paths.configured_worker_media_root(".state/media", project_root=project)
        == project_media
    )
    assert media_paths.configured_worker_media_root("user", project_root=project) == user_media
    assert (
        media_paths.configured_worker_media_root(str(user_media), project_root=project)
        == user_media
    )
    assert media_paths.configured_worker_media_root(
        "/var/lib/colmat-x/media", project_root=project
    ) == Path("/var/lib/colmat-x/media")


@pytest.mark.parametrize(
    "raw",
    (
        "../media",
        ".state/media/child",
        "/var/lib/colmat-x/media-evil",
        "/tmp/arbitrary-media",
        "project\x00escape",
    ),
)
def test_configured_media_root_rejects_path_injection(
    raw: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(media_paths, "_trusted_user_home", lambda: tmp_path / "home")

    with pytest.raises(ConfigError, match="raíz de media"):
        media_paths.configured_worker_media_root(raw, project_root=tmp_path)


@pytest.mark.parametrize(
    "legacy",
    (".state/media/automation", ".state/media/generation"),
)
def test_legacy_local_media_aliases_converge_on_shared_root(
    legacy: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(media_paths, "_trusted_user_home", lambda: tmp_path / "home")

    assert (
        media_paths.configured_worker_media_root(legacy, project_root=tmp_path)
        == (tmp_path / ".state/media").resolve()
    )


def test_worker_constructor_boundary_requires_absolute_path_capability() -> None:
    with pytest.raises(TypeError, match="Path autorizada"):
        media_paths.require_trusted_media_root("/var/lib/colmat-x/media")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="absoluta autorizada"):
        media_paths.require_trusted_media_root(Path(".state/media"))

    trusted = Path("/var/lib/colmat-x/media")
    assert media_paths.require_trusted_media_root(trusted) is trusted
