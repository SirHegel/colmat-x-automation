from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest

from colmat_x.platform_store import ConflictError, PlatformStore


def _restricted_slot() -> dict[str, object]:
    return {
        "id": "dato-manana",
        "at": "10:00",
        "weekdays": ["lunes"],
        "mode": "human_review",
        "category": "dato_semana",
        "institution": "colmat",
        "brief": "Explica una cifra territorial con su fuente primaria verificable.",
        "generate_image": True,
        "evidence": {
            "verified": False,
            "reference": None,
            "expected_figure": None,
            "expected_source": None,
        },
    }


def _slot_hash(slot: dict[str, object]) -> str:
    encoded = json.dumps(
        slot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def test_store_rejects_off_weekday_and_hashes_restricted_schedule() -> None:
    saturday = datetime(2026, 8, 29, 15, 0, tzinfo=UTC)  # 10:00 en Bogotá
    monday = datetime(2026, 8, 31, 15, 0, tzinfo=UTC)
    slot = _restricted_slot()
    with PlatformStore("sqlite+pysqlite:///:memory:") as store:
        owner, _ = store.bootstrap_owner(
            email="owner@weekday.test",
            display_name="Owner",
            now=saturday,
        )
        store.update_automation_settings(
            actor_id=owner.id,
            expected_version=1,
            enabled=True,
            slots=[slot],
            now=saturday,
        )

        with pytest.raises(ConflictError, match="día local"):
            store.claim_automation_run(
                actor_id=owner.id,
                idempotency_key="colmat:auto:v1:2026-08-29:dato-manana",
                slot_id="dato-manana",
                scheduled_for=saturday,
                slot_snapshot=slot,
                now=saturday,
            )

        run = store.claim_automation_run(
            actor_id=owner.id,
            idempotency_key="colmat:auto:v1:2026-08-31:dato-manana",
            slot_id="dato-manana",
            scheduled_for=monday,
            slot_snapshot=slot,
            now=monday,
        )

    unrestricted = dict(slot)
    unrestricted.pop("weekdays")
    assert run.slot_hash == _slot_hash(slot)
    assert run.slot_hash != _slot_hash(unrestricted)
