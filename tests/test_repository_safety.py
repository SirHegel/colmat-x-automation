from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SECRET_NAMES = {
    "X_CONSUMER_KEY",
    "X_CONSUMER_SECRET",
    "X_ACCESS_TOKEN",
    "X_ACCESS_TOKEN_SECRET",
    "DATABASE_URL",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_WEBHOOK_SECRET",
    "MINIMAX_API_KEY",
}


def test_env_example_contains_names_but_no_credentials() -> None:
    assignments: dict[str, str] = {}
    for raw_line in (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        assignments[name] = value

    assert assignments.keys() >= SECRET_NAMES
    assert all(assignments[name] == "" for name in SECRET_NAMES)


def test_private_runtime_paths_are_ignored() -> None:
    ignore_rules = {
        line.strip()
        for line in (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert {
        ".env",
        ".env.*",
        ".state/",
        ".venv/",
        ".vercel/",
        "logs/",
        "*.db",
        "*.db-*",
        "*.sqlite",
        "*.sqlite3",
        "*.log",
    } <= ignore_rules


def test_vercel_bundle_explicitly_excludes_private_runtime_files() -> None:
    ignore_rules = {
        line.strip()
        for line in (PROJECT_ROOT / ".vercelignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert {".env*", ".state/", ".venv/", ".vercel/", ".git/"} <= ignore_rules
