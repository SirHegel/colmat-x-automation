from __future__ import annotations

import os
from pathlib import Path

from colmat_x.config import ConfigError, _trusted_user_home

PROJECT_MEDIA_PROFILE = "project"
USER_MEDIA_PROFILE = "user"
SYSTEM_MEDIA_PROFILE = "system"
PROJECT_MEDIA_RELATIVE_PATH = Path(".state/media")
LEGACY_PROJECT_MEDIA_PATHS = (
    Path(".state/media/automation"),
    Path(".state/media/generation"),
)
USER_MEDIA_RELATIVE_PATH = Path(".local/share/colmat/media")
SYSTEM_MEDIA_ROOT = Path("/var/lib/colmat-x/media")


def configured_worker_media_root(
    raw: str | None,
    *,
    project_root: Path | None = None,
) -> Path:
    """Select a fixed media root without interpreting environment data as a path."""

    roots = _worker_media_roots(project_root=project_root)
    if raw is None:
        return roots[PROJECT_MEDIA_PROFILE]
    if not isinstance(raw, str):
        raise ConfigError("La raíz de media del worker no es válida")
    selected = raw.strip()
    if not selected or len(selected) > 512 or any(ord(char) < 32 for char in selected):
        raise ConfigError("La raíz de media del worker no es válida")

    # Values from the environment are lookup keys only. Returning the selected
    # trusted object prevents traversal, sibling-prefix and symlink-alias input.
    aliases = {
        PROJECT_MEDIA_PROFILE: roots[PROJECT_MEDIA_PROFILE],
        USER_MEDIA_PROFILE: roots[USER_MEDIA_PROFILE],
        SYSTEM_MEDIA_PROFILE: roots[SYSTEM_MEDIA_PROFILE],
        os.fspath(PROJECT_MEDIA_RELATIVE_PATH): roots[PROJECT_MEDIA_PROFILE],
        os.fspath(USER_MEDIA_RELATIVE_PATH): roots[USER_MEDIA_PROFILE],
        f"~/{USER_MEDIA_RELATIVE_PATH.as_posix()}": roots[USER_MEDIA_PROFILE],
    }
    canonical_project_root = roots[PROJECT_MEDIA_PROFILE].parents[1]
    for legacy in LEGACY_PROJECT_MEDIA_PATHS:
        aliases[os.fspath(legacy)] = roots[PROJECT_MEDIA_PROFILE]
        aliases[os.fspath(_canonical_trusted_path(canonical_project_root / legacy))] = roots[
            PROJECT_MEDIA_PROFILE
        ]
    for root in roots.values():
        aliases[os.fspath(root)] = root
    result = aliases.get(selected)
    if result is None:
        raise ConfigError(
            "La raíz de media debe usar el perfil project, user o system, o su ruta canónica exacta"
        )
    return result


def require_trusted_media_root(value: Path) -> Path:
    """Require an absolute in-process path capability, never a raw string."""

    if not isinstance(value, Path):
        raise TypeError("media_root debe ser una Path autorizada")
    if not value.is_absolute() or ".." in value.parts:
        raise ValueError("media_root debe ser una ruta absoluta autorizada")
    return value


def _worker_media_roots(*, project_root: Path | None) -> dict[str, Path]:
    selected_project_root = Path.cwd() if project_root is None else project_root
    if not isinstance(selected_project_root, Path) or not selected_project_root.is_absolute():
        raise ValueError("project_root debe ser una Path absoluta")
    return {
        PROJECT_MEDIA_PROFILE: _canonical_trusted_path(
            selected_project_root / PROJECT_MEDIA_RELATIVE_PATH
        ),
        USER_MEDIA_PROFILE: _canonical_trusted_path(
            _trusted_user_home() / USER_MEDIA_RELATIVE_PATH
        ),
        SYSTEM_MEDIA_PROFILE: _canonical_trusted_path(SYSTEM_MEDIA_ROOT),
    }


def _canonical_trusted_path(value: Path) -> Path:
    # ``value`` is composed exclusively from fixed roots above.
    return Path(os.path.normcase(os.path.realpath(os.fspath(value))))
