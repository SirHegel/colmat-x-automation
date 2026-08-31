from __future__ import annotations

import tomllib
from pathlib import Path

import colmat_x.research_registry as registry_module
from colmat_x.research_registry import load_gustavo_bueno_registry


def test_registry_preserves_documented_and_disputed_counts() -> None:
    registry = load_gustavo_bueno_registry()

    assert len(registry.entries) == 45
    assert sum(not item.disputed for item in registry.entries) == 44
    assert sum(item.disputed for item in registry.entries) == 1


def test_registry_selects_at_most_three_strong_title_matches() -> None:
    registry = load_gustavo_bueno_registry()

    selected = registry.select("Ensayos materialistas y materialismo filosófico")

    assert len(selected) <= 3
    assert "ensayos-materialistas" in {item.id for item in selected}
    assert registry.select("Guerra de los Supremos") == ()


def test_disputed_1962_attribution_requires_explicit_query_and_status_source() -> None:
    registry = load_gustavo_bueno_registry()

    assert all(not item.disputed for item in registry.select("filosofía escolar"))
    selected = registry.select("Curso elemental de filosofía 1962")
    disputed = next(item for item in selected if item.disputed)

    assert disputed.id == "curso-elemental-filosofia"
    assert disputed.status_url == "https://nodulo.org/ec/2010/n099p02.htm"
    assert "no tuvo arte ni parte" in (disputed.dispute_note or "")


def test_packaged_resource_fallback_and_wheel_mapping(monkeypatch, tmp_path: Path) -> None:
    payload = Path("config/gustavo-bueno-books.yaml").read_bytes()

    class PackagedResource:
        def joinpath(self, *_parts: str) -> PackagedResource:
            return self

        def read_bytes(self) -> bytes:
            return payload

    monkeypatch.setattr(registry_module, "_checkout_registry_path", lambda: tmp_path / "missing")
    monkeypatch.setattr(registry_module.resources, "files", lambda _package: PackagedResource())
    registry_module.load_gustavo_bueno_registry.cache_clear()
    try:
        packaged = registry_module.load_gustavo_bueno_registry()
    finally:
        registry_module.load_gustavo_bueno_registry.cache_clear()

    build_config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    force_include = build_config["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert len(packaged.entries) == 45
    assert force_include == {
        "config/gustavo-bueno-books.yaml": "colmat_x/data/gustavo-bueno-books.yaml"
    }
