from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import replace
from datetime import UTC, date, datetime, time
from pathlib import Path

import pytest
import yaml

from colmat_x.automation import (
    ALL_AUTOMATION_WEEKDAYS,
    AUTOMATION_TIMEZONE,
    DIRECT_PUBLISH_ENV,
    MAX_GENERATED_IMAGE_BYTES,
    AutomationConfig,
    AutomationConfigurationError,
    AutomationEvent,
    AutomationEventKind,
    AutomationGenerationError,
    AutomationMode,
    AutomationSlot,
    AutomationStatus,
    AutomationWeekday,
    ClaimDecision,
    DailyAutomation,
    DraftCandidate,
    GeneratedMedia,
    PublicationAmbiguousError,
    PublicationReceipt,
    automation_slot_mapping,
    draft_matches_expected_evidence,
    load_automation_config,
    slot_idempotency_key,
)
from colmat_x.editorial import (
    EditorialCategory,
    Institution,
    load_editorial_policy,
    validate_ai_draft,
)
from tests.factories import ONE_PIXEL_JPEG, ONE_PIXEL_PNG, ONE_PIXEL_WEBP

POLICY_PATH = Path("config/editorial-policy.yaml")
AUTOMATION_PATH = Path("config/automation.yaml")
NOW = datetime(2026, 8, 29, 14, 0, tzinfo=UTC)  # 09:00 en Bogotá


@pytest.fixture
def policy():
    return load_editorial_policy(POLICY_PATH)


def editorial_draft(
    policy,
    *,
    institution: Institution = Institution.ESCUELA,
    category: EditorialCategory = EditorialCategory.DATO_SEMANA,
):
    return validate_ai_draft(
        {
            "categoria": category.value,
            "institucion": institution.value,
            "texto": "Bogotá aporta 25,2 % del PIB nacional. Fuente: DANE 2024.",
            "cifra": "25,2 %",
            "fuente": "DANE 2024",
            "visual": {
                "tipo": (
                    "ficha_territorio"
                    if category is EditorialCategory.FICHA_TERRITORIO
                    else "tipografica"
                ),
                "descripcion": "La cifra ocupa el centro sobre fondo ocre.",
                "colores": ["ocre_basal", "tinta"],
                "tipografia": "Arial",
                "incluye_retrato_persona_viva": False,
                "usa_simbolos": False,
                "serie_completa": False,
                "eje_truncado": False,
            },
        },
        policy,
    )


def slot(
    *,
    slot_id: str = "dato-manana",
    at: time = time(8, 30),
    mode: AutomationMode = AutomationMode.HUMAN_REVIEW,
    category: EditorialCategory = EditorialCategory.DATO_SEMANA,
    institution: Institution = Institution.ESCUELA,
    generate_image: bool = False,
    evidence_verified: bool = False,
    evidence_reference: str | None = None,
    evidence_expected_figure: str | None = None,
    evidence_expected_source: str | None = None,
    weekdays: tuple[AutomationWeekday, ...] = ALL_AUTOMATION_WEEKDAYS,
) -> AutomationSlot:
    return AutomationSlot(
        id=slot_id,
        at=at,
        mode=mode,
        category=category,
        institution=institution,
        brief="Explica una cifra territorial reciente con su fuente primaria verificable.",
        weekdays=weekdays,
        generate_image=generate_image,
        evidence_verified=evidence_verified,
        evidence_reference=evidence_reference,
        evidence_expected_figure=evidence_expected_figure,
        evidence_expected_source=evidence_expected_source,
    )


def verified_direct_slot(*, generate_image: bool = False) -> AutomationSlot:
    return slot(
        mode=AutomationMode.DIRECT,
        generate_image=generate_image,
        evidence_verified=True,
        evidence_reference="DANE, Cuentas nacionales 2024, tabla 1.",
        evidence_expected_figure="25,2 %",
        evidence_expected_source="DANE 2024",
    )


def automation_config(
    *slots: AutomationSlot,
    direct_enabled: bool = False,
    minimum_score: int = 85,
) -> AutomationConfig:
    selected = slots or (slot(),)
    return AutomationConfig(
        version=1,
        timezone=AUTOMATION_TIMEZONE,
        daily_limit=len(selected),
        direct_enabled=direct_enabled,
        direct_min_engagement_score=minimum_score,
        slots=tuple(selected),
    )


def generated_media() -> GeneratedMedia:
    content = ONE_PIXEL_PNG
    return GeneratedMedia(
        content=content,
        filename="dato.png",
        mime_type="image/png",
        sha256=hashlib.sha256(content).hexdigest(),
        alt_text="Cifra territorial con fuente visible.",
    )


class FakeGenerator:
    def __init__(self, candidate: DraftCandidate, *, media: GeneratedMedia | None = None) -> None:
        self.candidate = candidate
        self.media = media or generated_media()
        self.draft_requests = []
        self.image_requests = []
        self.error: Exception | None = None

    def generate_draft(self, request):
        self.draft_requests.append(request)
        if self.error is not None:
            raise self.error
        return self.candidate

    def generate_image(self, request):
        self.image_requests.append(request)
        return self.media


class FakeRepository:
    def __init__(self) -> None:
        self.claims = []
        self.claimed_keys: set[str] = set()
        self.claims_by_date: defaultdict[date, int] = defaultdict(int)
        self.forced_decision: ClaimDecision | None = None
        self.prepared = []
        self.review_required = []
        self.direct_blocked = []
        self.published = []
        self.failed = []
        self.fail_save = False
        self.fail_review = False
        self.fail_blocked = False
        self.fail_published = False

    def claim_slot(self, claim, *, daily_limit):
        self.claims.append((claim, daily_limit))
        if self.forced_decision is not None:
            return self.forced_decision
        if claim.idempotency_key in self.claimed_keys:
            return ClaimDecision.DUPLICATE
        if self.claims_by_date[claim.local_date] >= daily_limit:
            return ClaimDecision.DAILY_LIMIT
        self.claimed_keys.add(claim.idempotency_key)
        self.claims_by_date[claim.local_date] += 1
        return ClaimDecision.CLAIMED

    def save_prepared(self, prepared):
        if self.fail_save:
            raise RuntimeError("database-password-must-not-leak")
        self.prepared.append(prepared)

    def mark_review_required(self, idempotency_key, *, reason):
        if self.fail_review:
            raise RuntimeError("review persistence unavailable")
        self.review_required.append((idempotency_key, reason))

    def mark_direct_blocked(self, idempotency_key, *, reason):
        if self.fail_blocked:
            raise RuntimeError("blocked persistence unavailable")
        self.direct_blocked.append((idempotency_key, reason))

    def mark_published(self, idempotency_key, *, receipt):
        if self.fail_published:
            raise RuntimeError("confirmation persistence unavailable")
        self.published.append((idempotency_key, receipt))

    def mark_failed(self, idempotency_key, *, reason, ambiguous):
        self.failed.append((idempotency_key, reason, ambiguous))


class FakeNotifier:
    def __init__(self, *, fail: bool = False) -> None:
        self.events: list[AutomationEvent] = []
        self.fail = fail

    def notify(self, event):
        self.events.append(event)
        if self.fail:
            raise RuntimeError("telegram unavailable")


class FakePublisher:
    def __init__(self) -> None:
        self.calls = []
        self.error: Exception | None = None
        self.receipt: object = PublicationReceipt(provider_post_id="123456789")

    def publish(self, prepared):
        self.calls.append(prepared)
        if self.error is not None:
            raise self.error
        return self.receipt


def engine(
    *,
    policy,
    config: AutomationConfig,
    generator: FakeGenerator,
    repository: FakeRepository | None = None,
    notifier: FakeNotifier | None = None,
    publisher: FakePublisher | None = None,
):
    repository = repository or FakeRepository()
    notifier = notifier or FakeNotifier()
    return (
        DailyAutomation(
            config=config,
            policy=policy,
            generator=generator,
            repository=repository,
            notifier=notifier,
            publisher=publisher,
        ),
        repository,
        notifier,
    )


def write_document(tmp_path: Path, document: dict) -> Path:
    path = tmp_path / "automation.yaml"
    path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def sample_document() -> dict:
    return yaml.safe_load(AUTOMATION_PATH.read_text(encoding="utf-8"))


def test_canonical_config_uses_bogota_closed_categories_and_safe_defaults(policy) -> None:
    config = load_automation_config(AUTOMATION_PATH, policy=policy)

    assert config.timezone == "America/Bogota"
    assert config.daily_limit == 2
    assert config.direct_enabled is False
    assert config.direct_min_engagement_score == 85
    assert [item.id for item in config.slots] == ["dato-manana", "territorio-tarde"]
    assert config.slots[0].weekdays == (AutomationWeekday.MONDAY,)
    assert config.slots[1].weekdays == tuple(AutomationWeekday)
    assert all(item.mode is AutomationMode.HUMAN_REVIEW for item in config.slots)
    assert config.slots[0].generate_image is True
    assert config.slots[1].generate_image is False
    assert all(item.evidence_verified is False for item in config.slots)
    assert all(item.evidence_reference is None for item in config.slots)
    assert all(item.evidence_expected_figure is None for item in config.slots)
    assert all(item.evidence_expected_source is None for item in config.slots)
    assert {item.category for item in config.slots} <= set(policy.taxonomy)


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (lambda value: value.update(timezone="UTC"), "exclusivamente America/Bogota"),
        (lambda value: value.update(daily_limit=True), "daily_limit debe ser un entero"),
        (
            lambda value: value["direct"].update(minimum_engagement_score=79),
            "minimum_engagement_score debe estar entre 80 y 100",
        ),
        (lambda value: value["slots"][0].update(mode="automatic"), "human_review o direct"),
        (
            lambda value: value["slots"][0].update(category="tendencia"),
            "taxonomía canónica",
        ),
        (lambda value: value["slots"][0].update(at="8:30"), "formato 24 horas"),
        (
            lambda value: value["slots"][0].update(generate_image="false"),
            "generate_image debe ser booleano",
        ),
        (
            lambda value: value["slots"][0]["evidence"].update(verified=True),
            "referencia externa auditable",
        ),
        (
            lambda value: value["slots"][0]["evidence"].update(
                verified=True,
                reference="DANE, Cuentas nacionales 2024",
            ),
            "cifra y fuente esperadas concretas",
        ),
        (
            lambda value: value["slots"][0]["evidence"].update(expected_figure="25,2 %"),
            "expected_figure y expected_source juntos",
        ),
        (lambda value: value.update(daily_limit=1), "slots supera daily_limit"),
    ],
)
def test_config_rejects_unsafe_or_open_values(tmp_path: Path, policy, mutation, match) -> None:
    document = sample_document()
    mutation(document)
    path = write_document(tmp_path, document)

    with pytest.raises(AutomationConfigurationError, match=match):
        load_automation_config(path, policy=policy)


def test_config_rejects_unknown_fields_duplicate_ids_and_duplicate_yaml_keys(
    tmp_path: Path, policy
) -> None:
    unknown = sample_document()
    unknown["unreviewed_action"] = True
    with pytest.raises(AutomationConfigurationError, match="Campos desconocidos"):
        load_automation_config(write_document(tmp_path, unknown), policy=policy)

    duplicated = sample_document()
    duplicated["slots"][1]["id"] = duplicated["slots"][0]["id"]
    with pytest.raises(AutomationConfigurationError, match="no admite duplicados"):
        load_automation_config(write_document(tmp_path, duplicated), policy=policy)

    duplicate_key = tmp_path / "duplicate-key.yaml"
    duplicate_key.write_text(
        AUTOMATION_PATH.read_text(encoding="utf-8") + "\ndaily_limit: 3\n",
        encoding="utf-8",
    )
    with pytest.raises(AutomationConfigurationError, match="YAML.*inválido"):
        load_automation_config(duplicate_key, policy=policy)


@pytest.mark.parametrize(
    "weekdays",
    [[], ["Lunes"], ["monday"], ["lunes", "lunes"], ["miercoles"]],
)
def test_config_rejects_noncanonical_weekdays(tmp_path: Path, policy, weekdays) -> None:
    document = sample_document()
    document["slots"][0]["weekdays"] = weekdays

    with pytest.raises(AutomationConfigurationError, match="weekdays"):
        load_automation_config(write_document(tmp_path, document), policy=policy)


def test_weekdays_are_canonical_in_snapshot_and_default_to_every_day(policy) -> None:
    monday_only = slot(weekdays=(AutomationWeekday.MONDAY,))
    every_day = slot(slot_id="dato-diario")

    assert automation_slot_mapping(monday_only)["weekdays"] == ["lunes"]
    assert "weekdays" not in automation_slot_mapping(every_day)
    assert monday_only.runs_on(date(2026, 8, 31)) is True
    assert monday_only.runs_on(date(2026, 8, 30)) is False


def test_weekday_restriction_controls_due_claim_and_idempotency(policy) -> None:
    generator = FakeGenerator(DraftCandidate(draft=editorial_draft(policy)))
    monday_only = slot(weekdays=(AutomationWeekday.MONDAY,))
    scheduler, repository, _ = engine(
        policy=policy,
        config=automation_config(monday_only),
        generator=generator,
    )

    assert scheduler.run_due(now=NOW, environ={}) == ()  # sábado local
    assert repository.claims == []

    monday = datetime(2026, 8, 31, 14, 0, tzinfo=UTC)
    result = scheduler.run_due(now=monday, environ={})[0]

    assert result.idempotency_key == "colmat:auto:v1:2026-08-31:dato-manana"
    assert repository.claims[0][0].local_date == date(2026, 8, 31)


def test_idempotency_key_is_deterministic_per_local_date_and_slot() -> None:
    first = slot_idempotency_key(date(2026, 8, 29), "dato-manana")
    repeated = slot_idempotency_key(date(2026, 8, 29), "dato-manana")
    next_day = slot_idempotency_key(date(2026, 8, 30), "dato-manana")

    assert first == repeated == "colmat:auto:v1:2026-08-29:dato-manana"
    assert next_day != first
    with pytest.raises(ValueError, match="date"):
        slot_idempotency_key(datetime(2026, 8, 29, tzinfo=UTC), "dato-manana")


def test_human_review_generates_optional_media_and_never_calls_publisher(policy) -> None:
    candidate = DraftCandidate(draft=editorial_draft(policy))
    generator = FakeGenerator(candidate)
    publisher = FakePublisher()
    scheduler, repository, notifier = engine(
        policy=policy,
        config=automation_config(slot(generate_image=True)),
        generator=generator,
        publisher=publisher,
    )

    result = scheduler.run_due(now=NOW, environ={})[0]

    assert result.status is AutomationStatus.REVIEW_REQUIRED
    assert result.media_generated is True
    assert result.assessment is not None
    assert result.assessment.publication_authorized is False
    assert "no predice ni garantiza alcance" in result.assessment.disclaimer
    assert len(generator.draft_requests) == len(generator.image_requests) == 1
    assert len(repository.prepared) == len(repository.review_required) == 1
    assert publisher.calls == []
    assert notifier.events[0].kind is AutomationEventKind.REVIEW_REQUIRED


def test_future_slots_are_not_claimed_and_naive_now_is_rejected(policy) -> None:
    generator = FakeGenerator(DraftCandidate(draft=editorial_draft(policy)))
    scheduler, repository, _ = engine(
        policy=policy,
        config=automation_config(slot(at=time(10, 0))),
        generator=generator,
    )

    assert scheduler.run_due(now=NOW, environ={}) == ()
    assert repository.claims == []
    with pytest.raises(ValueError, match="zona horaria"):
        scheduler.run_due(now=datetime(2026, 8, 29, 9, 0), environ={})


def test_due_slots_are_ordered_by_bogota_time_and_use_the_local_date(policy) -> None:
    generator = FakeGenerator(DraftCandidate(draft=editorial_draft(policy)))
    late = slot(slot_id="dato-tarde", at=time(9, 30))
    early = slot(slot_id="dato-temprano", at=time(8, 15))
    scheduler, repository, _ = engine(
        policy=policy,
        config=automation_config(late, early),
        generator=generator,
    )

    results = scheduler.run_due(now=datetime(2026, 8, 29, 15, 0, tzinfo=UTC), environ={})

    assert [result.slot_id for result in results] == ["dato-temprano", "dato-tarde"]
    assert all(result.scheduled_for.utcoffset().total_seconds() == -5 * 3600 for result in results)
    assert [claim.local_date for claim, _limit in repository.claims] == [
        date(2026, 8, 29),
        date(2026, 8, 29),
    ]
    assert generator.image_requests == []


def test_duplicate_and_daily_limit_stop_before_generation(policy) -> None:
    generator = FakeGenerator(DraftCandidate(draft=editorial_draft(policy)))
    scheduler, repository, _ = engine(
        policy=policy,
        config=automation_config(slot()),
        generator=generator,
    )

    first = scheduler.run_due(now=NOW, environ={})[0]
    repeated = scheduler.run_due(now=NOW, environ={})[0]
    repository.forced_decision = ClaimDecision.DAILY_LIMIT
    limited = scheduler.run_due(now=datetime(2026, 8, 30, 14, tzinfo=UTC), environ={})[0]

    assert first.status is AutomationStatus.REVIEW_REQUIRED
    assert repeated.status is AutomationStatus.SKIPPED_DUPLICATE
    assert limited.status is AutomationStatus.SKIPPED_DAILY_LIMIT
    assert len(generator.draft_requests) == 1


@pytest.mark.parametrize(
    ("config_enabled", "environment", "inject_publisher", "expected"),
    [
        (False, {DIRECT_PUBLISH_ENV: "true"}, True, "direct.enabled"),
        (True, {}, True, DIRECT_PUBLISH_ENV),
        (True, {DIRECT_PUBLISH_ENV: "1"}, True, "exactamente true"),
        (True, {DIRECT_PUBLISH_ENV: "true"}, False, "publisher"),
    ],
)
def test_direct_gate_is_claimed_then_terminal_and_notified_once(
    policy,
    config_enabled,
    environment,
    inject_publisher,
    expected,
) -> None:
    generator = FakeGenerator(
        DraftCandidate(
            draft=editorial_draft(policy),
            evidence_verified=True,
            evidence_reference="DANE, Cuentas nacionales 2024, tabla 1.",
        )
    )
    publisher = FakePublisher() if inject_publisher else None
    scheduler, repository, notifier = engine(
        policy=policy,
        config=automation_config(slot(mode=AutomationMode.DIRECT), direct_enabled=config_enabled),
        generator=generator,
        publisher=publisher,
    )

    blocked = scheduler.run_due(now=NOW, environ=environment)[0]
    repeated = scheduler.run_due(now=NOW, environ=environment)[0]

    assert blocked.status is AutomationStatus.DIRECT_BLOCKED
    assert expected in blocked.detail
    assert repeated.status is AutomationStatus.SKIPPED_DUPLICATE
    assert len(repository.claims) == 2
    assert len(repository.direct_blocked) == 1
    assert generator.draft_requests == []
    assert len(notifier.events) == 1
    assert notifier.events[0].kind is AutomationEventKind.DIRECT_BLOCKED
    if publisher is not None:
        assert publisher.calls == []


def test_direct_without_verified_evidence_falls_back_to_human_review(policy) -> None:
    generator = FakeGenerator(DraftCandidate(draft=editorial_draft(policy)))
    publisher = FakePublisher()
    scheduler, repository, _ = engine(
        policy=policy,
        config=automation_config(slot(mode=AutomationMode.DIRECT), direct_enabled=True),
        generator=generator,
        publisher=publisher,
    )

    result = scheduler.run_due(now=NOW, environ={DIRECT_PUBLISH_ENV: "true"})[0]

    assert result.status is AutomationStatus.REVIEW_REQUIRED
    assert "evidencia" in result.detail
    assert len(repository.review_required) == 1
    assert publisher.calls == []


def test_direct_evidence_must_match_expected_figure_and_source_in_generated_text(
    policy,
) -> None:
    draft = editorial_draft(policy)
    mismatched_slot = slot(
        mode=AutomationMode.DIRECT,
        evidence_verified=True,
        evidence_reference="DANE, Cuentas nacionales 2024, tabla 1.",
        evidence_expected_figure="25,2 %",
        evidence_expected_source="DANE 2024, tabla 99",
    )
    assert draft_matches_expected_evidence(draft, mismatched_slot) is False
    generator = FakeGenerator(
        DraftCandidate(
            draft=draft,
            evidence_verified=True,
            evidence_reference=mismatched_slot.evidence_reference,
        )
    )
    publisher = FakePublisher()
    scheduler, repository, _ = engine(
        policy=policy,
        config=automation_config(mismatched_slot, direct_enabled=True),
        generator=generator,
        publisher=publisher,
    )

    result = scheduler.run_due(now=NOW, environ={DIRECT_PUBLISH_ENV: "true"})[0]

    assert result.status is AutomationStatus.REVIEW_REQUIRED
    assert "cifra y la fuente" in result.detail
    assert repository.prepared[0].candidate.evidence_verified is False
    assert len(repository.review_required) == 1
    assert publisher.calls == []


def test_direct_evidence_rejects_figure_embedded_in_a_larger_number(policy) -> None:
    draft = editorial_draft(policy)
    adversarial = replace(
        draft,
        texto=draft.text.replace("25,2 %", "125,2 %"),
    )

    assert draft_matches_expected_evidence(adversarial, verified_direct_slot()) is False


def test_direct_below_engagement_threshold_falls_back_without_reclassifying(policy) -> None:
    generator = FakeGenerator(
        DraftCandidate(
            draft=editorial_draft(policy),
            evidence_verified=True,
            evidence_reference="DANE, Cuentas nacionales 2024, tabla 1.",
        )
    )
    publisher = FakePublisher()
    scheduler, repository, _ = engine(
        policy=policy,
        config=automation_config(
            verified_direct_slot(),
            direct_enabled=True,
            minimum_score=100,
        ),
        generator=generator,
        publisher=publisher,
    )

    result = scheduler.run_due(now=NOW, environ={DIRECT_PUBLISH_ENV: "true"})[0]

    assert result.assessment is not None and result.assessment.score < 100
    assert result.status is AutomationStatus.REVIEW_REQUIRED
    assert "no alcanza el umbral" in result.detail
    assert "no predice alcance" in result.detail
    assert len(repository.review_required) == 1
    assert publisher.calls == []


def test_direct_publishes_only_after_every_gate_and_persists_receipt(policy) -> None:
    generator = FakeGenerator(
        DraftCandidate(
            draft=editorial_draft(policy),
            evidence_verified=True,
            evidence_reference="DANE, Cuentas nacionales 2024, tabla 1.",
        )
    )
    publisher = FakePublisher()
    scheduler, repository, notifier = engine(
        policy=policy,
        config=automation_config(
            verified_direct_slot(),
            direct_enabled=True,
            minimum_score=80,
        ),
        generator=generator,
        publisher=publisher,
    )

    result = scheduler.run_due(now=NOW, environ={DIRECT_PUBLISH_ENV: "true"})[0]

    assert result.status is AutomationStatus.PUBLISHED
    assert result.receipt == PublicationReceipt(provider_post_id="123456789")
    assert len(publisher.calls) == 1
    assert publisher.calls[0].claim.idempotency_key == result.idempotency_key
    assert len(repository.prepared) == len(repository.published) == 1
    assert repository.failed == []
    assert notifier.events[0].kind is AutomationEventKind.PUBLISHED


def test_direct_generated_media_always_falls_back_to_visual_human_review(policy) -> None:
    generator = FakeGenerator(
        DraftCandidate(
            draft=editorial_draft(policy),
            evidence_verified=True,
            evidence_reference="DANE, Cuentas nacionales 2024, tabla 1.",
        )
    )
    publisher = FakePublisher()
    scheduler, repository, notifier = engine(
        policy=policy,
        config=automation_config(
            verified_direct_slot(generate_image=True),
            direct_enabled=True,
            minimum_score=80,
        ),
        generator=generator,
        publisher=publisher,
    )

    result = scheduler.run_due(now=NOW, environ={DIRECT_PUBLISH_ENV: "true"})[0]

    assert result.status is AutomationStatus.REVIEW_REQUIRED
    assert result.media_generated is True
    assert "revisión visual humana" in result.detail
    assert len(repository.review_required) == 1
    assert publisher.calls == []
    assert notifier.events[0].kind is AutomationEventKind.REVIEW_REQUIRED


def test_direct_territory_sheet_requires_local_authorship_review(policy) -> None:
    reference = "DANE, Cuentas nacionales 2024, tabla 1."
    territory_slot = slot(
        mode=AutomationMode.DIRECT,
        category=EditorialCategory.FICHA_TERRITORIO,
        evidence_verified=True,
        evidence_reference=reference,
        evidence_expected_figure="25,2 %",
        evidence_expected_source="DANE 2024",
    )
    generator = FakeGenerator(
        DraftCandidate(
            draft=editorial_draft(
                policy,
                category=EditorialCategory.FICHA_TERRITORIO,
            ),
            evidence_verified=True,
            evidence_reference=reference,
        )
    )
    publisher = FakePublisher()
    scheduler, repository, _notifier = engine(
        policy=policy,
        config=automation_config(
            territory_slot,
            direct_enabled=True,
            minimum_score=80,
        ),
        generator=generator,
        publisher=publisher,
    )

    result = scheduler.run_due(now=NOW, environ={DIRECT_PUBLISH_ENV: "true"})[0]

    assert result.status is AutomationStatus.REVIEW_REQUIRED
    assert "autoría local" in result.detail
    assert len(repository.review_required) == 1
    assert publisher.calls == []


def test_generation_mismatch_is_failed_before_media_or_publisher(policy) -> None:
    generator = FakeGenerator(DraftCandidate(draft=editorial_draft(policy)))
    publisher = FakePublisher()
    scheduler, repository, notifier = engine(
        policy=policy,
        config=automation_config(
            slot(
                mode=AutomationMode.DIRECT,
                institution=Institution.COLMAT,
                generate_image=True,
            ),
            direct_enabled=True,
        ),
        generator=generator,
        publisher=publisher,
    )

    result = scheduler.run_due(now=NOW, environ={DIRECT_PUBLISH_ENV: "true"})[0]

    assert result.status is AutomationStatus.FAILED
    assert "institución solicitada" in result.detail
    assert generator.image_requests == []
    assert publisher.calls == []
    assert repository.failed[0][2] is False
    assert notifier.events[0].kind is AutomationEventKind.FAILED


def test_external_failures_are_sanitized_and_claim_stays_terminal(policy) -> None:
    generator = FakeGenerator(DraftCandidate(draft=editorial_draft(policy)))
    repository = FakeRepository()
    repository.fail_save = True
    scheduler, _, _ = engine(
        policy=policy,
        config=automation_config(slot()),
        generator=generator,
        repository=repository,
    )

    result = scheduler.run_due(now=NOW, environ={})[0]

    assert result.status is AutomationStatus.FAILED
    assert "database-password-must-not-leak" not in result.detail
    assert repository.failed[0][2] is False
    assert scheduler.run_due(now=NOW, environ={})[0].status is AutomationStatus.SKIPPED_DUPLICATE


def test_ambiguous_publish_is_unknown_and_never_auto_retried(policy) -> None:
    generator = FakeGenerator(
        DraftCandidate(
            draft=editorial_draft(policy),
            evidence_verified=True,
            evidence_reference="DANE, Cuentas nacionales 2024, tabla 1.",
        )
    )
    publisher = FakePublisher()
    publisher.error = PublicationAmbiguousError("X no confirmó la operación")
    scheduler, repository, notifier = engine(
        policy=policy,
        config=automation_config(verified_direct_slot(), direct_enabled=True),
        generator=generator,
        publisher=publisher,
    )

    unknown = scheduler.run_due(now=NOW, environ={DIRECT_PUBLISH_ENV: "true"})[0]
    repeated = scheduler.run_due(now=NOW, environ={DIRECT_PUBLISH_ENV: "true"})[0]

    assert unknown.status is AutomationStatus.UNKNOWN
    assert repeated.status is AutomationStatus.SKIPPED_DUPLICATE
    assert len(publisher.calls) == 1
    assert repository.failed[0][2] is True
    assert notifier.events[0].kind is AutomationEventKind.UNKNOWN


def test_lost_persistence_after_provider_success_is_unknown(policy) -> None:
    generator = FakeGenerator(
        DraftCandidate(
            draft=editorial_draft(policy),
            evidence_verified=True,
            evidence_reference="DANE, Cuentas nacionales 2024, tabla 1.",
        )
    )
    publisher = FakePublisher()
    repository = FakeRepository()
    repository.fail_published = True
    scheduler, _, notifier = engine(
        policy=policy,
        config=automation_config(verified_direct_slot(), direct_enabled=True),
        generator=generator,
        repository=repository,
        publisher=publisher,
    )

    result = scheduler.run_due(now=NOW, environ={DIRECT_PUBLISH_ENV: "true"})[0]

    assert result.status is AutomationStatus.UNKNOWN
    assert result.receipt is not None
    assert repository.failed[0][2] is True
    assert notifier.events[0].kind is AutomationEventKind.UNKNOWN


def test_notification_failure_cannot_turn_confirmed_publish_into_retry(policy) -> None:
    generator = FakeGenerator(
        DraftCandidate(
            draft=editorial_draft(policy),
            evidence_verified=True,
            evidence_reference="DANE, Cuentas nacionales 2024, tabla 1.",
        )
    )
    publisher = FakePublisher()
    notifier = FakeNotifier(fail=True)
    scheduler, repository, _ = engine(
        policy=policy,
        config=automation_config(verified_direct_slot(), direct_enabled=True),
        generator=generator,
        notifier=notifier,
        publisher=publisher,
    )

    result = scheduler.run_due(now=NOW, environ={DIRECT_PUBLISH_ENV: "true"})[0]

    assert result.status is AutomationStatus.PUBLISHED
    assert result.notification_delivered is False
    assert len(repository.published) == 1
    assert repository.failed == []


def test_media_and_verified_evidence_validate_material_fields(policy) -> None:
    content = ONE_PIXEL_PNG
    with pytest.raises(AutomationGenerationError, match="SHA-256 no coincide"):
        GeneratedMedia(
            content=content,
            filename="image.png",
            mime_type="image/png",
            sha256="0" * 64,
            alt_text="Descripción",
        )
    with pytest.raises(AutomationGenerationError, match="nombre de imagen"):
        GeneratedMedia(
            content=content,
            filename="../image.png",
            mime_type="image/png",
            sha256=hashlib.sha256(content).hexdigest(),
            alt_text="Descripción",
        )
    oversized = b"x" * (MAX_GENERATED_IMAGE_BYTES + 1)
    with pytest.raises(AutomationGenerationError, match="máximo seguro de 5 MB"):
        GeneratedMedia(
            content=oversized,
            filename="image.png",
            mime_type="image/png",
            sha256=hashlib.sha256(oversized).hexdigest(),
            alt_text="Descripción",
        )
    with pytest.raises(AutomationGenerationError, match="referencia auditable"):
        DraftCandidate(draft=editorial_draft(policy), evidence_verified=True)


@pytest.mark.parametrize(
    ("content", "mime_type"),
    [
        (ONE_PIXEL_JPEG, "image/jpeg"),
        (ONE_PIXEL_PNG, "image/png"),
        (ONE_PIXEL_WEBP, "image/webp"),
    ],
)
def test_generated_media_accepts_matching_supported_signatures(
    content: bytes, mime_type: str
) -> None:
    media = GeneratedMedia(
        content=content,
        filename="imagen.bin",
        mime_type=mime_type,
        sha256=hashlib.sha256(content).hexdigest(),
        alt_text="Descripción accesible",
    )

    assert media.mime_type == mime_type


@pytest.mark.parametrize(
    ("content", "mime_type", "message"),
    [
        (b"contenido que no es una imagen", "image/png", "firma"),
        (ONE_PIXEL_PNG, "image/jpeg", "no coincide"),
        (ONE_PIXEL_WEBP, "image/png", "no coincide"),
    ],
)
def test_generated_media_rejects_false_or_mismatched_bytes(
    content: bytes, mime_type: str, message: str
) -> None:
    with pytest.raises(AutomationGenerationError, match=message):
        GeneratedMedia(
            content=content,
            filename="imagen.bin",
            mime_type=mime_type,
            sha256=hashlib.sha256(content).hexdigest(),
            alt_text="Descripción accesible",
        )
