from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from jinja2 import FileSystemLoader, StrictUndefined, TemplateError
from jinja2.sandbox import SandboxedEnvironment

from colmat_x.config import ProjectSettings
from colmat_x.domain import (
    ContentError,
    RenderedPost,
    normalized_content_hash,
    parse_publish_at,
    validate_post_id,
    validate_rendered_text,
)
from colmat_x.yaml_utils import load_yaml_unique

ALLOWED_FIELDS = {"id", "template", "publish_at", "data"}


class ContentCollectionError(ContentError):
    """Uno o más archivos de contenido no son válidos."""


def load_rendered_posts(settings: ProjectSettings) -> list[RenderedPost]:
    environment = SandboxedEnvironment(
        loader=FileSystemLoader(settings.paths.templates_dir),
        undefined=StrictUndefined,
        autoescape=False,
        trim_blocks=False,
        lstrip_blocks=False,
        keep_trailing_newline=False,
    )
    files = sorted(
        path
        for pattern in ("*.yaml", "*.yml")
        for path in settings.paths.content_dir.glob(pattern)
        if path.is_file()
    )
    posts: list[RenderedPost] = []
    errors: list[str] = []
    for path in files:
        try:
            posts.append(_load_one(path, settings, environment))
        except (ContentError, TemplateError, yaml.YAMLError) as exc:
            errors.append(f"{path.name}: {exc}")

    ids: dict[str, Path] = {}
    hashes: dict[str, Path] = {}
    for post in posts:
        if post.id in ids:
            errors.append(
                f"{post.source_path.name}: id duplicado '{post.id}' "
                f"(también en {ids[post.id].name})"
            )
        else:
            ids[post.id] = post.source_path
        if post.content_hash in hashes:
            errors.append(
                f"{post.source_path.name}: texto duplicado "
                f"(también en {hashes[post.content_hash].name})"
            )
        else:
            hashes[post.content_hash] = post.source_path

    if errors:
        raise ContentCollectionError("\n".join(errors))
    return sorted(posts, key=lambda post: (post.publish_at_utc, post.id))


def _load_one(
    path: Path,
    settings: ProjectSettings,
    environment: SandboxedEnvironment,
) -> RenderedPost:
    try:
        raw_document = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ContentError(f"no se pudo leer como UTF-8: {exc}") from exc
    document = load_yaml_unique(raw_document)
    if not isinstance(document, dict):
        raise ContentError("la raíz debe ser un objeto YAML")

    invalid_keys = [repr(key) for key in document if not isinstance(key, str)]
    if invalid_keys:
        raise ContentError(
            "todas las claves principales deben ser texto: " + ", ".join(invalid_keys)
        )
    unknown = sorted(set(document) - ALLOWED_FIELDS)
    if unknown:
        raise ContentError("campos desconocidos: " + ", ".join(unknown))

    post_id = validate_post_id(document.get("id"))
    template_name = _template_name(document.get("template"))
    publish_at = parse_publish_at(document.get("publish_at"))

    data = document.get("data")
    if not isinstance(data, dict):
        raise ContentError("'data' debe ser un objeto")
    _validate_template_data(data)

    try:
        template = environment.get_template(f"{template_name}.j2")
        rendered = template.render(**data)
    except TemplateError as exc:
        raise ContentError(f"no se pudo renderizar '{template_name}': {exc}") from exc
    text = _normalize_rendered_text(rendered)
    validate_rendered_text(
        text,
        max_weighted_length=settings.safety.max_weighted_length,
        allow_urls=settings.safety.allow_urls,
    )

    return RenderedPost(
        id=post_id,
        template=template_name,
        publish_at=publish_at,
        text=text,
        content_hash=normalized_content_hash(text),
        source_path=path,
    )


def _template_name(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ContentError("'template' debe ser texto no vacío")
    if not value.replace("-", "").replace("_", "").isalnum():
        raise ContentError("'template' solo admite letras, números, guion y guion bajo")
    return value


def _validate_template_data(data: dict[Any, Any]) -> None:
    for key, value in data.items():
        if not isinstance(key, str) or not key:
            raise ContentError("todas las claves de 'data' deben ser texto no vacío")
        if isinstance(value, (dict, list, tuple, set)):
            raise ContentError(f"data.{key} debe ser un valor simple, no una colección")


def _normalize_rendered_text(rendered: str) -> str:
    lines = [line.rstrip() for line in rendered.replace("\r\n", "\n").split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)
