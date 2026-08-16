from __future__ import annotations

from dataclasses import replace

import pytest

from colmat_x.content import ContentCollectionError, load_rendered_posts
from colmat_x.domain import ContentError, validate_rendered_text, weighted_length


def test_loads_and_renders_post(project_settings) -> None:
    (project_settings.paths.content_dir / "post.yaml").write_text(
        """
id: colmat-idea-001
template: idea
publish_at: "2026-08-16T09:00:00-05:00"
data:
  titulo: Una pregunta abre caminos.
  desarrollo: Pensar en colectivo transforma la mirada.
  cierre: Sigamos conversando.
""".strip(),
        encoding="utf-8",
    )

    posts = load_rendered_posts(project_settings)

    assert len(posts) == 1
    assert posts[0].text.startswith("Una pregunta")
    assert posts[0].publish_at_utc.isoformat() == "2026-08-16T14:00:00+00:00"


def test_approval_fields_are_not_accepted_in_source_yaml(project_settings) -> None:
    (project_settings.paths.content_dir / "post.yaml").write_text(
        """
id: colmat-idea-002
template: idea
publish_at: "2026-08-16T09:00:00-05:00"
approved: true
data:
  titulo: Título
  desarrollo: Desarrollo
  cierre: Cierre
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ContentCollectionError, match="campos desconocidos: approved"):
        load_rendered_posts(project_settings)


def test_rejects_naive_schedule(project_settings) -> None:
    (project_settings.paths.content_dir / "post.yaml").write_text(
        """
id: colmat-idea-003
template: idea
publish_at: "2026-08-16T09:00:00"
data:
  titulo: Título
  desarrollo: Desarrollo
  cierre: Cierre
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ContentCollectionError, match="zona horaria"):
        load_rendered_posts(project_settings)


@pytest.mark.parametrize(
    "publish_at",
    ["9999-12-31T23:59:59-05:00", "0001-01-01T05:00:00+05:00"],
)
def test_rejects_schedule_outside_operational_range(project_settings, publish_at: str) -> None:
    (project_settings.paths.content_dir / "post.yaml").write_text(
        f"""
id: colmat-idea-future
template: idea
publish_at: "{publish_at}"
data:
  titulo: Fuera de rango
  desarrollo: Fecha no representable en UTC.
  cierre: ""
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ContentCollectionError, match="rango|años 2000"):
        load_rendered_posts(project_settings)


def test_rejects_unicode_surrogates_without_a_traceback(project_settings) -> None:
    (project_settings.paths.content_dir / "post.yaml").write_text(
        """
id: colmat-idea-unicode
template: idea
publish_at: "2026-08-16T09:00:00-05:00"
data:
  titulo: Texto inválido
  desarrollo: "\\uD800"
  cierre: ""
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ContentCollectionError, match="Unicode no válido"):
        load_rendered_posts(project_settings)


def test_rejects_urls_when_cost_guard_is_active(project_settings) -> None:
    (project_settings.paths.content_dir / "post.yaml").write_text(
        """
id: colmat-idea-004
template: idea
publish_at: "2026-08-16T09:00:00-05:00"
data:
  titulo: Consulta
  desarrollo: https://example.com/una-ruta-muy-larga
  cierre: ""
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ContentCollectionError, match="contiene una URL"):
        load_rendered_posts(project_settings)

    enabled = replace(
        project_settings,
        safety=replace(project_settings.safety, allow_urls=True),
    )
    assert len(load_rendered_posts(enabled)) == 1


def test_weighted_length_counts_url_and_emoji() -> None:
    assert weighted_length("abc") == 3
    assert weighted_length("🧠") == 2
    assert weighted_length("Ve https://example.com/una/ruta") == 35
    assert weighted_length("Ve https://example.com.") == 27
    assert weighted_length("https://example.com🧠") == 25
    assert weighted_length("https://example.com#tag") == 27
    assert weighted_length("x" * 255 + " <https://e.co>") == 281


def test_rejects_duplicate_rendered_text(project_settings) -> None:
    body = """
template: idea
publish_at: "2026-08-16T09:00:00-05:00"
data:
  titulo: Igual
  desarrollo: El mismo contenido
  cierre: Fin
""".strip()
    (project_settings.paths.content_dir / "one.yaml").write_text(
        f"id: colmat-igual-001\n{body}", encoding="utf-8"
    )
    (project_settings.paths.content_dir / "two.yaml").write_text(
        f"id: colmat-igual-002\n{body}", encoding="utf-8"
    )

    with pytest.raises(ContentCollectionError, match="texto duplicado"):
        load_rendered_posts(project_settings)


def test_rejects_duplicate_yaml_keys(project_settings) -> None:
    (project_settings.paths.content_dir / "post.yaml").write_text(
        """
id: colmat-idea-005
template: idea
publish_at: "2026-08-16T09:00:00-05:00"
publish_at: "2026-08-17T09:00:00-05:00"
data:
  titulo: Título
  desarrollo: Desarrollo
  cierre: Cierre
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ContentCollectionError, match="clave duplicada 'publish_at'"):
        load_rendered_posts(project_settings)


def test_empty_content_collection_is_valid(project_settings) -> None:
    assert load_rendered_posts(project_settings) == []


def test_weighted_length_normalizes_nfc_and_detects_bare_domains(project_settings) -> None:
    assert weighted_length("cafe\u0301") == 4
    assert weighted_length("Ve example.com") == 26

    (project_settings.paths.content_dir / "post.yaml").write_text(
        """
id: colmat-idea-006
template: idea
publish_at: "2026-08-16T09:00:00-05:00"
data:
  titulo: Consulta
  desarrollo: Visita www.example.com, example.org o пример.рф
  cierre: ""
""".strip(),
        encoding="utf-8",
    )
    with pytest.raises(ContentCollectionError, match="contiene una URL"):
        load_rendered_posts(project_settings)


def test_url_cost_guard_normalizes_decomposed_unicode() -> None:
    with pytest.raises(ContentError, match="contiene una URL"):
        validate_rendered_text(
            "Visita cafe\u0301.com",
            max_weighted_length=280,
            allow_urls=False,
        )
