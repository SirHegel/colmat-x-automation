from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from dotenv import dotenv_values, load_dotenv

from colmat_x.yaml_utils import load_yaml_unique


class ConfigError(ValueError):
    """La configuración del proyecto no es válida."""


def _trusted_user_home() -> Path:
    """Return the OS account profile without trusting environment overrides."""

    if os.name == "nt":
        import ctypes

        buffer = ctypes.create_unicode_buffer(32_768)
        result = ctypes.windll.shell32.SHGetFolderPathW(None, 40, None, 0, buffer)
        if result != 0 or not buffer.value:
            raise ConfigError("No se pudo determinar el perfil del usuario actual")
        return Path(buffer.value)

    import pwd

    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError) as exc:
        raise ConfigError("No se pudo determinar el perfil del usuario actual") from exc


@dataclass(frozen=True)
class BrandSettings:
    name: str
    timezone: str
    language: str

    @property
    def zoneinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass(frozen=True)
class PathSettings:
    content_dir: Path
    templates_dir: Path
    state_db: Path


@dataclass(frozen=True)
class SafetySettings:
    max_weighted_length: int
    max_posts_per_run: int
    max_posts_per_day: int
    allow_urls: bool
    lease_minutes: int


@dataclass(frozen=True)
class ProjectSettings:
    root: Path
    brand: BrandSettings
    paths: PathSettings
    safety: SafetySettings


@dataclass(frozen=True)
class XCredentials:
    consumer_key: str = field(repr=False)
    consumer_secret: str = field(repr=False)
    access_token: str = field(repr=False)
    access_token_secret: str = field(repr=False)

    @classmethod
    def from_environment(cls) -> XCredentials:
        names = (
            "X_CONSUMER_KEY",
            "X_CONSUMER_SECRET",
            "X_ACCESS_TOKEN",
            "X_ACCESS_TOKEN_SECRET",
        )
        values = {name: os.getenv(name, "").strip() for name in names}
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise ConfigError("Faltan credenciales para publicar: " + ", ".join(sorted(missing)))

        return cls(
            consumer_key=values["X_CONSUMER_KEY"],
            consumer_secret=values["X_CONSUMER_SECRET"],
            access_token=values["X_ACCESS_TOKEN"],
            access_token_secret=values["X_ACCESS_TOKEN_SECRET"],
        )


def live_enabled() -> bool:
    raw = os.getenv("COLMAT_LIVE_ENABLED", "false").strip().casefold()
    if raw in {"1", "true", "yes", "on", "si", "sí"}:
        return True
    if raw in {"0", "false", "no", "off", ""}:
        return False
    raise ConfigError("COLMAT_LIVE_ENABLED debe ser true o false")


def load_settings(config_path: str | Path | None = None) -> ProjectSettings:
    requested = config_path
    if requested is None:
        requested = os.getenv("COLMAT_CONFIG")
    if requested is None:
        # El .env del directorio de trabajo solo puede indicar dónde está el
        # proyecto. Sus credenciales y banderas nunca deben contaminar otro
        # proyecto seleccionado mediante COLMAT_CONFIG.
        requested = dotenv_values(Path.cwd() / ".env").get("COLMAT_CONFIG", "config/colmat.yaml")
    if requested is None:
        raise ConfigError("COLMAT_CONFIG no puede estar vacío")
    if isinstance(requested, str):
        requested = requested.strip()
        if not requested:
            raise ConfigError("COLMAT_CONFIG no puede estar vacío")
    path = _resolve_config_path(requested)
    if not path.is_file():
        raise ConfigError(f"No existe el archivo de configuración: {path}")

    root = path.parent.parent if path.parent.name == "config" else path.parent
    load_dotenv(root / ".env", override=False)

    try:
        raw_document = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"No se pudo leer {path} como UTF-8: {exc}") from exc
    try:
        document = load_yaml_unique(raw_document)
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML inválido en {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ConfigError(f"La raíz de {path} debe ser un objeto YAML")

    _validate_keys(document, {"brand", "paths", "safety"}, "raíz")

    brand_data = _section(document, "brand")
    paths_data = _section(document, "paths")
    safety_data = _section(document, "safety")
    _validate_keys(brand_data, {"name", "timezone", "language"}, "brand")
    _validate_keys(paths_data, {"content_dir", "templates_dir", "state_db"}, "paths")
    _validate_keys(
        safety_data,
        {
            "max_weighted_length",
            "max_posts_per_run",
            "max_posts_per_day",
            "allow_urls",
            "lease_minutes",
        },
        "safety",
    )

    brand = BrandSettings(
        name=_nonempty_string(brand_data, "name"),
        timezone=_nonempty_string(brand_data, "timezone"),
        language=_nonempty_string(brand_data, "language"),
    )
    try:
        ZoneInfo(brand.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"Zona horaria desconocida: {brand.timezone}") from exc

    paths = PathSettings(
        content_dir=_project_path(root, paths_data, "content_dir"),
        templates_dir=_project_path(root, paths_data, "templates_dir"),
        state_db=_state_path(
            root, _optional_env("COLMAT_STATE_DB") or _nonempty_string(paths_data, "state_db")
        ),
    )
    safety = SafetySettings(
        max_weighted_length=_positive_int(safety_data, "max_weighted_length"),
        max_posts_per_run=_positive_int(safety_data, "max_posts_per_run"),
        max_posts_per_day=_positive_int(safety_data, "max_posts_per_day"),
        allow_urls=_boolean(safety_data, "allow_urls"),
        lease_minutes=_positive_int(safety_data, "lease_minutes"),
    )
    if safety.max_weighted_length > 280:
        raise ConfigError("'max_weighted_length' no puede superar el límite estándar de X: 280")

    if not paths.content_dir.is_dir():
        raise ConfigError(f"No existe el directorio de contenido: {paths.content_dir}")
    if not paths.templates_dir.is_dir():
        raise ConfigError(f"No existe el directorio de plantillas: {paths.templates_dir}")

    return ProjectSettings(root=root, brand=brand, paths=paths, safety=safety)


def _section(document: dict[str, Any], name: str) -> dict[str, Any]:
    value = document.get(name)
    if not isinstance(value, dict):
        raise ConfigError(f"La sección '{name}' es obligatoria y debe ser un objeto")
    return value


def _validate_keys(section: dict[Any, Any], allowed: set[str], location: str) -> None:
    invalid_types = [repr(key) for key in section if not isinstance(key, str)]
    if invalid_types:
        raise ConfigError(
            f"Todas las claves de '{location}' deben ser texto: " + ", ".join(invalid_types)
        )
    unknown = sorted(set(section) - allowed)
    if unknown:
        raise ConfigError(f"Campos desconocidos en '{location}': " + ", ".join(unknown))


def _nonempty_string(section: dict[str, Any], name: str) -> str:
    value = section.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"'{name}' debe ser texto no vacío")
    return value.strip()


def _positive_int(section: dict[str, Any], name: str) -> int:
    value = section.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"'{name}' debe ser un entero mayor que cero")
    return value


def _boolean(section: dict[str, Any], name: str) -> bool:
    value = section.get(name)
    if not isinstance(value, bool):
        raise ConfigError(f"'{name}' debe ser true o false")
    return value


def _project_path(root: Path, section: dict[str, Any], name: str) -> Path:
    return _resolve_under_root(root, _nonempty_string(section, name), label=f"paths.{name}")


def _resolve_config_path(requested: str | Path) -> Path:
    """Resolve a config file only below the working directory or the user's home.

    ``COLMAT_CONFIG`` and ``--config`` are operator-controlled inputs.  Normalizing
    before checking a directory boundary prevents ``..`` and symlink escapes while
    still allowing a project below the current user's workspace to be selected.
    """

    raw = os.fspath(requested)
    try:
        expanded = os.path.expanduser(raw)
        if not os.path.isabs(expanded):
            expanded = os.path.join(os.fspath(Path.cwd()), expanded)
        candidate = os.path.normcase(os.path.realpath(expanded))
        cwd = os.path.normcase(os.path.realpath(os.fspath(Path.cwd())))
        home = os.path.normcase(os.path.realpath(os.fspath(_trusted_user_home())))
    except (OSError, TypeError, ValueError) as exc:
        raise ConfigError("La ruta de configuración no es válida") from exc

    if cwd != os.path.dirname(cwd) and candidate.startswith(cwd.rstrip(os.sep) + os.sep):
        return Path(candidate)
    if home != os.path.dirname(home) and candidate.startswith(home.rstrip(os.sep) + os.sep):
        return Path(candidate)
    raise ConfigError(
        "El archivo de configuración debe estar dentro del directorio de trabajo "
        "o del perfil del usuario"
    )


def _resolve_under_root(root: Path, raw: str, *, label: str) -> Path:
    """Return a normalized descendant of ``root`` or fail closed."""

    try:
        trusted_root = os.path.normcase(os.path.realpath(os.fspath(root)))
        expanded = os.path.expanduser(raw)
        unresolved = expanded if os.path.isabs(expanded) else os.path.join(trusted_root, expanded)
        candidate = os.path.normcase(os.path.realpath(unresolved))
    except (OSError, TypeError, ValueError) as exc:
        raise ConfigError(f"'{label}' no contiene una ruta válida") from exc

    if trusted_root != os.path.dirname(trusted_root) and candidate.startswith(
        trusted_root.rstrip(os.sep) + os.sep
    ):
        return Path(candidate)
    raise ConfigError(f"'{label}' debe permanecer dentro del proyecto")


def _state_path(root: Path, raw: str) -> Path:
    """Resolve state storage under the project or the fixed system service root."""

    if not os.path.isabs(os.path.expanduser(raw)):
        return _resolve_under_root(root, raw, label="paths.state_db")

    # The packaged systemd deployment uses this dedicated, administrator-owned
    # directory.  No other absolute destination is accepted from configuration.
    return _resolve_under_root(Path("/var/lib/colmat-x"), raw, label="paths.state_db")


def _optional_env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        raise ConfigError(f"{name} no puede estar vacío")
    return stripped
