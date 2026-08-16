from __future__ import annotations

from pathlib import Path

import pytest

from colmat_x.config import BrandSettings, PathSettings, ProjectSettings, SafetySettings


@pytest.fixture
def project_settings(tmp_path: Path) -> ProjectSettings:
    content_dir = tmp_path / "content" / "posts"
    templates_dir = tmp_path / "content" / "templates"
    content_dir.mkdir(parents=True)
    templates_dir.mkdir(parents=True)
    (templates_dir / "idea.j2").write_text(
        "{{ titulo }}\n\n{{ desarrollo }}{% if cierre %}\n\n{{ cierre }}{% endif %}",
        encoding="utf-8",
    )
    return ProjectSettings(
        root=tmp_path,
        brand=BrandSettings(name="Colmat", timezone="America/Bogota", language="es"),
        paths=PathSettings(
            content_dir=content_dir,
            templates_dir=templates_dir,
            state_db=tmp_path / ".state" / "test.db",
        ),
        safety=SafetySettings(
            max_weighted_length=280,
            max_posts_per_run=2,
            max_posts_per_day=2,
            allow_urls=False,
            lease_minutes=10,
        ),
    )


@pytest.fixture
def configured_project(project_settings: ProjectSettings) -> tuple[ProjectSettings, Path]:
    config_dir = project_settings.root / "config"
    config_dir.mkdir()
    config_path = config_dir / "colmat.yaml"
    config_path.write_text(
        """
brand:
  name: Colmat
  timezone: America/Bogota
  language: es
paths:
  content_dir: content/posts
  templates_dir: content/templates
  state_db: .state/test.db
safety:
  max_weighted_length: 280
  max_posts_per_run: 2
  max_posts_per_day: 2
  allow_urls: false
  lease_minutes: 10
""".strip(),
        encoding="utf-8",
    )
    return project_settings, config_path
