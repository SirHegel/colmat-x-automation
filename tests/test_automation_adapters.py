from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, time
from pathlib import Path
from types import SimpleNamespace

import pytest

sqlalchemy = pytest.importorskip("sqlalchemy")
from sqlalchemy import select  # noqa: E402

from colmat_x.automation import (  # noqa: E402
    AUTOMATION_TIMEZONE,
    AutomationConfig,
    AutomationError,
    AutomationEvent,
    AutomationEventKind,
    AutomationGenerationError,
    AutomationMode,
    AutomationSlot,
    ClaimDecision,
    DailyAutomation,
    DraftCandidate,
    GeneratedMedia,
    GenerationRequest,
    ImageGenerationRequest,
    PreparedAutomation,
    PublicationAmbiguousError,
    SlotClaim,
    automation_slot_mapping,
)
from colmat_x.automation_adapters import (  # noqa: E402
    LIVE_PUBLISH_ENV,
    AutomationReviewNotificationWorker,
    MiniMaxAutomationGenerator,
    PlatformAutomationRepository,
    PlatformXPublisher,
    ReviewNotificationDeliveryStatus,
    TelegramAutomationNotifier,
)
from colmat_x.editorial import (  # noqa: E402
    EditorialCategory,
    Institution,
    assess_engagement,
    load_editorial_policy,
    validate_ai_draft,
)
from colmat_x.platform_store import (  # noqa: E402
    AutomationReviewNotificationStatus,
    AutomationRunStatus,
    ConflictError,
    DraftStatus,
    MediaAsset,
    PlatformStore,
    PublishStatus,
)
from colmat_x.rbac import Role  # noqa: E402
from colmat_x.telegram_api import TelegramApiError, TelegramTransportError  # noqa: E402
from colmat_x.x_api import AmbiguousMediaError, AmbiguousPublishError, XApiError  # noqa: E402
from tests.factories import ONE_PIXEL_JPEG, ONE_PIXEL_PNG  # noqa: E402

NOW = datetime(2026, 8, 29, 15, 0, tzinfo=UTC)
SCHEDULED = datetime(2026, 8, 29, 13, 30, tzinfo=UTC)
POLICY_PATH = Path("config/editorial-policy.yaml")
REVIEW_CHAT_ID = -1001234567890
REVIEW_TELEGRAM_USER_ID = 778899


@pytest.fixture
def policy():
    return load_editorial_policy(POLICY_PATH)


@pytest.fixture
def store() -> PlatformStore:
    selected = PlatformStore("sqlite+pysqlite:///:memory:")
    try:
        yield selected
    finally:
        selected.close()


def make_slot(
    *,
    slot_id: str = "dato-manana",
    mode: AutomationMode = AutomationMode.HUMAN_REVIEW,
    verified: bool = False,
    reference: str | None = None,
    expected_figure: str | None = None,
    expected_source: str | None = None,
) -> AutomationSlot:
    if verified:
        expected_figure = expected_figure or "25,2 %"
        expected_source = expected_source or "DANE 2024"
    return AutomationSlot(
        id=slot_id,
        at=time(8, 30),
        mode=mode,
        category=EditorialCategory.DATO_SEMANA,
        institution=Institution.COLMAT,
        brief="Explica el dato territorial con una fuente externa verificable.",
        generate_image=True,
        evidence_verified=verified,
        evidence_reference=reference,
        evidence_expected_figure=expected_figure,
        evidence_expected_source=expected_source,
    )


def make_claim(slot: AutomationSlot | None = None) -> SlotClaim:
    selected = slot or make_slot()
    return SlotClaim(
        idempotency_key=f"colmat:auto:v1:2026-08-29:{selected.id}",
        local_date=date(2026, 8, 29),
        scheduled_for=SCHEDULED,
        slot=selected,
    )


def make_draft(policy):
    return validate_ai_draft(
        {
            "categoria": "dato_semana",
            "institucion": "colmat",
            "texto": "Bogotá aporta 25,2 % del PIB nacional. Fuente: DANE 2024.",
            "cifra": "25,2 %",
            "fuente": "DANE 2024",
            "visual": {
                "tipo": "tipografica",
                "descripcion": "Cifra central sobre un fondo ocre sobrio.",
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


def make_media(
    content: bytes = ONE_PIXEL_PNG,
    *,
    mime_type: str = "image/png",
) -> GeneratedMedia:
    return GeneratedMedia(
        content=content,
        filename="dato-manana.png",
        mime_type=mime_type,
        sha256=hashlib.sha256(content).hexdigest(),
        alt_text="25,2 %. Cifra territorial con fuente DANE 2024.",
    )


def make_prepared(
    policy,
    *,
    slot: AutomationSlot | None = None,
    media=True,
    editorial_line: bool = False,
):
    claim = make_claim(slot)
    draft = make_draft(policy)
    candidate = DraftCandidate(
        draft=draft,
        evidence_verified=claim.slot.evidence_verified,
        evidence_reference=claim.slot.evidence_reference,
        editorial_line_month="2026-08" if editorial_line else None,
        editorial_line_version=3 if editorial_line else None,
        editorial_line_sha256=(
            hashlib.sha256(b"linea mensual").hexdigest() if editorial_line else None
        ),
    )
    return PreparedAutomation(
        claim=claim,
        candidate=candidate,
        assessment=assess_engagement(draft),
        media=make_media() if media else None,
    )


class FakeMiniMax:
    def __init__(self, draft, *, mime_type: str = "image/png") -> None:
        self.draft = draft
        self.mime_type = mime_type
        self.draft_calls = []
        self.image_calls = []

    def generate_draft(self, *args, **kwargs):
        self.draft_calls.append((args, kwargs))
        return self.draft

    def generate_image(self, *args, **kwargs):
        self.image_calls.append((args, kwargs))
        content = ONE_PIXEL_PNG
        return SimpleNamespace(
            content=content,
            mime_type=self.mime_type,
            sha256=hashlib.sha256(content).hexdigest(),
            alt_text=kwargs.get("alt_text"),
        )


class FakeX:
    def __init__(self, *, upload_error=None, post_error=None, after_upload=None) -> None:
        self.upload_error = upload_error
        self.post_error = post_error
        self.after_upload = after_upload
        self.upload_calls = []
        self.post_calls = []

    def upload_image(self, content, **kwargs):
        self.upload_calls.append((content, kwargs))
        if self.upload_error is not None:
            raise self.upload_error
        if self.after_upload is not None:
            self.after_upload()
        return SimpleNamespace(id="123456789")

    def create_post(self, text, **kwargs):
        self.post_calls.append((text, kwargs))
        if self.post_error is not None:
            raise self.post_error
        return SimpleNamespace(id="190000000000000001", text=text)


class FakeTelegram:
    def __init__(self) -> None:
        self.messages = []
        self.photos = []
        self._next_message_id = 100

    def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text, kwargs))
        self._next_message_id += 1
        return {"message_id": self._next_message_id}

    def send_photo_bytes(self, chat_id, content, **kwargs):
        self.photos.append((chat_id, content, kwargs))
        self._next_message_id += 1
        return {"message_id": self._next_message_id}


def add_member(store: PlatformStore, owner_id: str, role: Role, ordinal: int):
    user = store.create_user(
        actor_id=owner_id,
        email=f"{role.value}-{ordinal}@adapters.test",
        display_name=f"{role.value.title()} {ordinal}",
        now=NOW,
    )
    store.grant_membership(user.id, role, actor_id=owner_id, now=NOW)
    return user


def bootstrap_services(store: PlatformStore, *, direct: bool, monkeypatch):
    owner, _ = store.bootstrap_owner(email="owner@adapters.test", display_name="Owner", now=NOW)
    scheduler = add_member(store, owner.id, Role.SCHEDULER, 1)
    author = add_member(store, owner.id, Role.EDITOR, 1)
    reviewer = add_member(store, owner.id, Role.REVIEWER, 1)
    publisher = add_member(store, owner.id, Role.PUBLISHER, 1)
    store.bind_telegram_chat(
        REVIEW_CHAT_ID,
        telegram_user_id=REVIEW_TELEGRAM_USER_ID,
        actor_id=owner.id,
        user_id=reviewer.id,
        purpose="review",
        now=NOW,
    )
    mode = "direct" if direct else "human_review"
    if direct:
        monkeypatch.setenv("COLMAT_DIRECT_PUBLISH_ENABLED", "true")
    slot_mode = AutomationMode.DIRECT if direct else AutomationMode.HUMAN_REVIEW
    persisted_slots = [
        make_slot(
            mode=slot_mode,
            verified=direct,
            reference="DANE, cuenta nacional 2024" if direct else None,
        ),
        make_slot(
            slot_id="dato-tarde",
            mode=slot_mode,
            verified=direct,
            reference="DANE, cuenta nacional 2024" if direct else None,
        ),
    ]
    store.update_automation_settings(
        actor_id=owner.id,
        expected_version=1,
        enabled=True,
        mode=mode,
        slots=[automation_slot_mapping(slot) for slot in persisted_slots],
        max_posts_per_day=1,
        now=NOW,
    )
    return owner, scheduler, author, reviewer, publisher


def make_repository(store, services, tmp_path, *, retry_run_ids=None):
    _owner, scheduler, author, _reviewer, _publisher = services
    return PlatformAutomationRepository(
        store,
        scheduler_actor_id=scheduler.id,
        author_actor_id=author.id,
        reviewer_telegram_user_id=REVIEW_TELEGRAM_USER_ID,
        review_chat_id=REVIEW_CHAT_ID,
        media_root=tmp_path / "persistent-media",
        clock=lambda: NOW,
        retry_run_ids=retry_run_ids,
    )


def test_repository_requires_distinct_scheduler_and_author(store, tmp_path, monkeypatch) -> None:
    services = bootstrap_services(store, direct=False, monkeypatch=monkeypatch)
    scheduler = services[1]

    with pytest.raises(ValueError, match="distint"):
        PlatformAutomationRepository(
            store,
            scheduler_actor_id=scheduler.id,
            author_actor_id=scheduler.id,
            media_root=tmp_path,
        )


def test_minimax_generator_uses_slot_evidence_only_and_fixed_policy(policy) -> None:
    draft = make_draft(policy)
    client = FakeMiniMax(draft)
    generator = MiniMaxAutomationGenerator(client, policy=policy)
    slot = make_slot(verified=True, reference="DANE, cuenta nacional 2024")
    claim = make_claim(slot)

    candidate = generator.generate_draft(
        GenerationRequest(
            claim=claim,
            brief=slot.brief,
            category=slot.category,
            institution=slot.institution,
        )
    )

    assert candidate.evidence_verified is True
    assert candidate.evidence_reference == "DANE, cuenta nacional 2024"
    args, kwargs = client.draft_calls[0]
    assert args[1] is policy
    assert args[0].startswith(slot.brief)
    assert "cifra='25,2 %'" in args[0]
    assert "fuente='DANE 2024'" in args[0]
    assert kwargs == {"category": slot.category, "institution": slot.institution}


def test_minimax_generator_prepends_versioned_monthly_editorial_line(policy) -> None:
    draft = make_draft(policy)
    client = FakeMiniMax(draft)
    requested_months: list[str] = []

    def resolve_line(month: str) -> tuple[str, int]:
        requested_months.append(month)
        return "Analizar conflictos históricos desde el materialismo filosófico.", 3

    generator = MiniMaxAutomationGenerator(
        client,
        policy=policy,
        editorial_line_resolver=resolve_line,
    )
    claim = make_claim()
    candidate = generator.generate_draft(
        GenerationRequest(
            claim=claim,
            brief=claim.slot.brief,
            category=claim.slot.category,
            institution=claim.slot.institution,
        )
    )

    assert requested_months == ["2026-08"]
    prompt = client.draft_calls[0][0][0]
    assert prompt.startswith("Línea editorial mensual humana obligatoria")
    assert "mes=2026-08; versión=3" in prompt
    assert prompt.index("materialismo filosófico") < prompt.index(claim.slot.brief)
    assert candidate.editorial_line_month == "2026-08"
    assert candidate.editorial_line_version == 3
    assert (
        candidate.editorial_line_sha256
        == hashlib.sha256(
            "Analizar conflictos históricos desde el materialismo filosófico.".encode()
        ).hexdigest()
    )


def test_minimax_generator_fails_before_network_when_monthly_line_is_missing(policy) -> None:
    client = FakeMiniMax(make_draft(policy))
    generator = MiniMaxAutomationGenerator(
        client,
        policy=policy,
        editorial_line_resolver=lambda _month: None,
    )
    claim = make_claim()

    with pytest.raises(AutomationGenerationError, match="No existe línea editorial"):
        generator.generate_draft(
            GenerationRequest(
                claim=claim,
                brief=claim.slot.brief,
                category=claim.slot.category,
                institution=claim.slot.institution,
            )
        )

    assert client.draft_calls == []


def test_minimax_generator_downgrades_verified_slot_when_output_changes_evidence(policy) -> None:
    draft = make_draft(policy)
    generator = MiniMaxAutomationGenerator(FakeMiniMax(draft), policy=policy)
    slot = make_slot(
        verified=True,
        reference="DANE, cuenta nacional 2024",
        expected_source="DANE 2024, tabla 99",
    )
    claim = make_claim(slot)

    candidate = generator.generate_draft(
        GenerationRequest(
            claim=claim,
            brief=slot.brief,
            category=slot.category,
            institution=slot.institution,
        )
    )

    assert candidate.evidence_verified is False
    assert candidate.evidence_reference == slot.evidence_reference


def test_minimax_generator_never_infers_evidence_and_builds_safe_image(policy) -> None:
    draft = make_draft(policy)
    client = FakeMiniMax(draft)
    generator = MiniMaxAutomationGenerator(client, policy=policy)
    claim = make_claim()
    request = GenerationRequest(
        claim=claim,
        brief=claim.slot.brief,
        category=claim.slot.category,
        institution=claim.slot.institution,
    )
    assert generator.generate_draft(request).evidence_verified is False

    media = generator.generate_image(ImageGenerationRequest(claim=claim, draft=draft))

    assert media.filename == "dato-manana-2026-08-29.png"
    assert media.sha256 == hashlib.sha256(ONE_PIXEL_PNG).hexdigest()
    prompt_args, image_kwargs = client.image_calls[0]
    assert prompt_args == (draft.visual.descripcion, policy)
    assert image_kwargs["aspect_ratio"] == "16:9"
    assert "25,2 %" in image_kwargs["alt_text"]
    assert "DANE 2024" in image_kwargs["alt_text"]


def test_minimax_generator_rejects_unsupported_image_type(policy) -> None:
    draft = make_draft(policy)
    generator = MiniMaxAutomationGenerator(FakeMiniMax(draft, mime_type="image/gif"), policy=policy)
    claim = make_claim()
    with pytest.raises(AutomationGenerationError, match="tipo de imagen"):
        generator.generate_image(ImageGenerationRequest(claim=claim, draft=draft))


def test_repository_claim_is_duplicate_safe_with_frozen_clock_and_maps_daily_limit(
    store, policy, tmp_path, monkeypatch
) -> None:
    services = bootstrap_services(store, direct=False, monkeypatch=monkeypatch)
    repository = make_repository(store, services, tmp_path)
    first = make_claim()

    assert repository.claim_slot(first, daily_limit=1) is ClaimDecision.CLAIMED
    assert repository.claim_slot(first, daily_limit=1) is ClaimDecision.DUPLICATE

    second = make_claim(make_slot(slot_id="dato-tarde"))
    assert repository.claim_slot(second, daily_limit=1) is ClaimDecision.DAILY_LIMIT
    assert repository.get_record(first.idempotency_key).claim == first
    with pytest.raises(AutomationError, match="no está reclamado"):
        repository.get_record("missing-key")


def test_repository_duplicate_reports_persisted_unhealthy_status(
    store,
    policy,
    tmp_path,
    monkeypatch,
) -> None:
    services = bootstrap_services(store, direct=False, monkeypatch=monkeypatch)
    repository = make_repository(store, services, tmp_path)
    claim = make_claim()
    assert repository.claim_slot(claim, daily_limit=1) is ClaimDecision.CLAIMED
    repository.mark_failed(claim.idempotency_key, reason="fallo controlado", ambiguous=False)

    assert repository.claim_slot(claim, daily_limit=1) is ClaimDecision.DUPLICATE_FAILED


def test_repository_claims_explicit_historical_retry_by_run_id(
    store, policy, tmp_path, monkeypatch
) -> None:
    services = bootstrap_services(store, direct=False, monkeypatch=monkeypatch)
    owner, scheduler, *_rest = services
    first_repository = make_repository(store, services, tmp_path)
    claim = make_claim()
    assert first_repository.claim_slot(claim, daily_limit=1) is ClaimDecision.CLAIMED
    first_repository.mark_failed(
        claim.idempotency_key,
        reason="Fallo confirmado antes del borrador",
        ambiguous=False,
    )
    run = store.list_automation_runs(actor_id=owner.id)[0]
    store.request_automation_run_retry(
        run.id,
        actor_id=scheduler.id,
        now=NOW,
    )

    retry_repository = make_repository(
        store,
        services,
        tmp_path,
        retry_run_ids={claim.idempotency_key: run.id},
    )

    assert retry_repository.claim_retry_slot(claim, daily_limit=1) is ClaimDecision.CLAIMED
    reclaimed = store.list_automation_runs(actor_id=owner.id)[0]
    assert reclaimed.id == run.id
    assert reclaimed.attempt_count == 2
    assert reclaimed.status_value is AutomationRunStatus.CLAIMED
    assert reclaimed.retry_requested_at is None


def test_repository_persists_content_addressed_media_and_holds_for_review(
    store, policy, tmp_path, monkeypatch
) -> None:
    services = bootstrap_services(store, direct=False, monkeypatch=monkeypatch)
    owner, _scheduler, _author, _reviewer, _publisher = services
    repository = make_repository(store, services, tmp_path)
    prepared = make_prepared(policy)
    assert repository.claim_slot(prepared.claim, daily_limit=1) is ClaimDecision.CLAIMED

    repository.save_prepared(prepared)
    record = repository.get_record(prepared.claim.idempotency_key)

    assert record.draft_id is not None
    assert record.media_path is not None
    assert record.media_path.name == f"{prepared.media.sha256}.png"
    assert record.media_path.read_bytes() == prepared.media.content
    assert record.media_path.stat().st_mode & 0o777 == 0o600
    assert record.media_path.parent.stat().st_mode & 0o777 == 0o700
    assert store.get_draft(record.draft_id, actor_id=owner.id).status_value is DraftStatus.IN_REVIEW
    revision = store.get_current_revision(record.draft_id, actor_id=owner.id)
    assert revision.evidence["verification"] == {
        "reference": None,
        "verified": False,
        "expected_figure": None,
        "expected_source": None,
    }
    with store.session() as session:
        asset = session.scalar(select(MediaAsset))
        assert asset is not None
        assert asset.sha256 == prepared.media.sha256
        assert asset.asset_metadata["source"] == "minimax"

    repository.mark_review_required(
        prepared.claim.idempotency_key, reason="Revisión humana obligatoria"
    )
    run = store.list_automation_runs(actor_id=owner.id)[0]
    assert run.status_value is AutomationRunStatus.AWAITING_REVIEW
    assert run.draft_id == record.draft_id


def test_repository_persists_editorial_line_version_in_draft_evidence(
    store, policy, tmp_path, monkeypatch
) -> None:
    services = bootstrap_services(store, direct=False, monkeypatch=monkeypatch)
    owner, *_rest = services
    repository = make_repository(store, services, tmp_path)
    prepared = make_prepared(policy, media=False, editorial_line=True)
    assert repository.claim_slot(prepared.claim, daily_limit=1) is ClaimDecision.CLAIMED

    repository.save_prepared(prepared)
    record = repository.get_record(prepared.claim.idempotency_key)
    revision = store.get_current_revision(record.draft_id, actor_id=owner.id)

    assert revision.evidence["editorial_line"] == {
        "month": "2026-08",
        "version": 3,
        "sha256": hashlib.sha256(b"linea mensual").hexdigest(),
    }


def test_daily_automation_preserves_editorial_line_in_persisted_evidence(
    store, policy, tmp_path, monkeypatch
) -> None:
    services = bootstrap_services(store, direct=False, monkeypatch=monkeypatch)
    owner, *_rest = services
    repository = make_repository(store, services, tmp_path)
    scheduled_slot = make_slot()
    line = "Analizar conflictos históricos desde el materialismo filosófico."
    generator = MiniMaxAutomationGenerator(
        FakeMiniMax(make_draft(policy)),
        policy=policy,
        editorial_line_resolver=lambda month: (line, 3) if month == "2026-08" else None,
    )
    automation = DailyAutomation(
        config=AutomationConfig(
            version=1,
            timezone=AUTOMATION_TIMEZONE,
            daily_limit=1,
            direct_enabled=False,
            direct_min_engagement_score=85,
            slots=(scheduled_slot,),
        ),
        policy=policy,
        generator=generator,
        repository=repository,
        notifier=SimpleNamespace(notify=lambda _event: None),
    )

    results = automation.run_due(now=NOW, environ={})

    assert len(results) == 1
    record = repository.get_record(results[0].idempotency_key)
    revision = store.get_current_revision(record.draft_id, actor_id=owner.id)
    assert revision.evidence["editorial_line"] == {
        "month": "2026-08",
        "version": 3,
        "sha256": hashlib.sha256(line.encode()).hexdigest(),
    }


def test_repository_maps_block_failure_and_unknown_without_leaking_secret(
    store, policy, tmp_path, monkeypatch
) -> None:
    services = bootstrap_services(store, direct=False, monkeypatch=monkeypatch)
    owner, *_ = services
    repository = make_repository(store, services, tmp_path)
    claim = make_claim()
    repository.claim_slot(claim, daily_limit=1)
    repository.mark_direct_blocked(
        claim.idempotency_key, reason="token=super-secret-value sk-supersecret"
    )
    run = store.list_automation_runs(actor_id=owner.id)[0]
    assert run.status_value is AutomationRunStatus.FAILED
    assert "super-secret-value" not in run.error
    assert "sk-supersecret" not in run.error
    assert "[REDACTED]" in run.error


def setup_direct_publisher(store, policy, tmp_path, monkeypatch, *, x_client=None, media=True):
    services = bootstrap_services(store, direct=True, monkeypatch=monkeypatch)
    _owner, _scheduler, _author, reviewer, publisher = services
    repository = make_repository(store, services, tmp_path)
    slot = make_slot(
        mode=AutomationMode.DIRECT,
        verified=True,
        reference="DANE, cuenta nacional 2024",
    )
    prepared = make_prepared(policy, slot=slot, media=media)
    assert repository.claim_slot(prepared.claim, daily_limit=1) is ClaimDecision.CLAIMED
    repository.save_prepared(prepared)
    selected_x = x_client or FakeX()
    adapter = PlatformXPublisher(
        store=store,
        repository=repository,
        x_client=selected_x,
        reviewer_actor_id=reviewer.id,
        publisher_actor_id=publisher.id,
        environ={
            LIVE_PUBLISH_ENV: "true",
            "COLMAT_DIRECT_PUBLISH_ENABLED": "true",
        },
        clock=lambda: NOW,
    )
    return services, repository, prepared, selected_x, adapter


def test_direct_publisher_approves_separately_uploads_ai_media_and_is_idempotent(
    store, policy, tmp_path, monkeypatch
) -> None:
    services, repository, prepared, x_client, publisher = setup_direct_publisher(
        store, policy, tmp_path, monkeypatch
    )
    owner, _scheduler, _author, reviewer, publishing_user = services

    receipt = publisher.publish(prepared)

    assert receipt.provider_post_id == "190000000000000001"
    assert x_client.upload_calls[0][1]["alt_text"] == prepared.media.alt_text
    assert x_client.post_calls == [
        (
            prepared.draft.text,
            {"media_ids": ["123456789"], "made_with_ai": True},
        )
    ]
    record = repository.get_record(prepared.claim.idempotency_key)
    draft = store.get_draft(record.draft_id, actor_id=owner.id)
    assert draft.status_value is DraftStatus.PUBLISHED
    assert publisher.publish(prepared) == receipt
    assert len(x_client.post_calls) == 1
    attempts = []
    with store.session() as session:
        from colmat_x.platform_store import PublishAttempt

        attempts = list(session.scalars(select(PublishAttempt)))
    assert attempts[0].status_value is PublishStatus.SUCCEEDED
    assert attempts[0].requested_by == publishing_user.id
    events = store.list_audit_events(actor_id=owner.id)
    approval_events = [event for event in events if event.action == "draft.approved"]
    assert approval_events[0].actor_id == reviewer.id

    repository.mark_published(prepared.claim.idempotency_key, receipt=receipt)
    run = store.list_automation_runs(actor_id=owner.id)[0]
    assert run.status_value is AutomationRunStatus.SUCCEEDED


def test_direct_publisher_without_media_does_not_mark_ai_flag(
    store, policy, tmp_path, monkeypatch
) -> None:
    _services, _repository, prepared, x_client, publisher = setup_direct_publisher(
        store, policy, tmp_path, monkeypatch, media=False
    )
    publisher.publish(prepared)
    assert x_client.upload_calls == []
    assert x_client.post_calls[0][1] == {"media_ids": [], "made_with_ai": False}


@pytest.mark.parametrize("missing_gate", [LIVE_PUBLISH_ENV, "COLMAT_DIRECT_PUBLISH_ENABLED"])
def test_direct_publisher_rechecks_both_kill_switches(
    store, policy, tmp_path, monkeypatch, missing_gate
) -> None:
    _services, _repository, prepared, x_client, publisher = setup_direct_publisher(
        store, policy, tmp_path, monkeypatch
    )
    publisher._environ = {
        LIVE_PUBLISH_ENV: "true",
        "COLMAT_DIRECT_PUBLISH_ENABLED": "true",
    }
    del publisher._environ[missing_gate]
    with pytest.raises(AutomationError, match=missing_gate):
        publisher.publish(prepared)
    assert x_client.post_calls == []


def test_direct_publisher_closes_attempt_when_begin_publishing_fails(
    store, policy, tmp_path, monkeypatch
) -> None:
    _services, repository, prepared, x_client, publisher = setup_direct_publisher(
        store, policy, tmp_path, monkeypatch
    )

    def fail_begin(_key):
        raise ConflictError("claim revocado token=must-not-leak")

    monkeypatch.setattr(repository, "begin_publishing", fail_begin)

    with pytest.raises(AutomationError, match="claim directo"):
        publisher.publish(prepared)

    with store.session() as session:
        from colmat_x.platform_store import PublishAttempt

        attempt = session.scalar(select(PublishAttempt))
    assert attempt.status_value is PublishStatus.FAILED
    assert "must-not-leak" not in attempt.error
    assert x_client.upload_calls == []
    assert x_client.post_calls == []


def test_direct_publisher_revalidates_claim_after_upload_before_post(
    store, policy, tmp_path, monkeypatch
) -> None:
    _services, repository, prepared, x_client, publisher = setup_direct_publisher(
        store, policy, tmp_path, monkeypatch
    )
    original_begin = repository.begin_publishing
    calls = 0

    def revoke_after_upload(key):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ConflictError("el slot autorizado cambió")
        return original_begin(key)

    monkeypatch.setattr(repository, "begin_publishing", revoke_after_upload)

    with pytest.raises(AutomationError, match="después de preparar la media"):
        publisher.publish(prepared)

    with store.session() as session:
        from colmat_x.platform_store import PublishAttempt

        attempt = session.scalar(select(PublishAttempt))
    assert calls == 2
    assert len(x_client.upload_calls) == 1
    assert x_client.post_calls == []
    assert attempt.status_value is PublishStatus.FAILED


def test_direct_publisher_revalidates_runtime_gate_after_upload(
    store, policy, tmp_path, monkeypatch
) -> None:
    _services, _repository, prepared, x_client, publisher = setup_direct_publisher(
        store, policy, tmp_path, monkeypatch
    )
    x_client.after_upload = lambda: publisher._environ.pop(LIVE_PUBLISH_ENV)

    with pytest.raises(AutomationError, match="después de preparar la media"):
        publisher.publish(prepared)

    with store.session() as session:
        from colmat_x.platform_store import PublishAttempt

        attempt = session.scalar(select(PublishAttempt))
    assert len(x_client.upload_calls) == 1
    assert x_client.post_calls == []
    assert attempt.status_value is PublishStatus.FAILED


def test_direct_publisher_marks_ambiguous_and_never_retries(
    store, policy, tmp_path, monkeypatch
) -> None:
    x_client = FakeX(post_error=AmbiguousPublishError("token=must-not-leak"))
    services, repository, prepared, _x_client, publisher = setup_direct_publisher(
        store, policy, tmp_path, monkeypatch, x_client=x_client
    )
    owner, *_ = services

    with pytest.raises(PublicationAmbiguousError, match="no confirmó"):
        publisher.publish(prepared)
    with pytest.raises(PublicationAmbiguousError, match="intento previo"):
        publisher.publish(prepared)
    assert len(x_client.post_calls) == 1
    repository.mark_failed(
        prepared.claim.idempotency_key,
        reason="publicación ambigua token=must-not-leak",
        ambiguous=True,
    )
    run = store.list_automation_runs(actor_id=owner.id)[0]
    assert run.status_value is AutomationRunStatus.UNKNOWN
    assert "must-not-leak" not in run.error
    assert repository.claim_slot(prepared.claim, daily_limit=1) is ClaimDecision.DUPLICATE_UNKNOWN


@pytest.mark.parametrize(
    ("upload_error", "post_error", "expected_status"),
    (
        (AmbiguousMediaError("media uncertain"), None, PublishStatus.FAILED),
        (None, XApiError("explicit X error"), PublishStatus.FAILED),
        (None, RuntimeError("transport state unknown"), PublishStatus.UNKNOWN),
    ),
)
def test_direct_publisher_translates_x_failures_without_external_retries(
    store,
    policy,
    tmp_path,
    monkeypatch,
    upload_error,
    post_error,
    expected_status,
) -> None:
    x_client = FakeX(upload_error=upload_error, post_error=post_error)
    _services, _repository, prepared, _x, publisher = setup_direct_publisher(
        store, policy, tmp_path, monkeypatch, x_client=x_client
    )
    expected_error = (
        PublicationAmbiguousError if expected_status is PublishStatus.UNKNOWN else AutomationError
    )
    with pytest.raises(expected_error):
        publisher.publish(prepared)
    with store.session() as session:
        from colmat_x.platform_store import PublishAttempt

        attempt = session.scalar(select(PublishAttempt))
    assert attempt.status_value is expected_status
    if upload_error is not None:
        assert x_client.post_calls == []


def test_direct_publisher_requires_distinct_reviewer_and_persisted_snapshot(
    store, policy, tmp_path, monkeypatch
) -> None:
    services, repository, prepared, x_client, _publisher = setup_direct_publisher(
        store, policy, tmp_path, monkeypatch
    )
    _owner, _scheduler, author, _reviewer, publishing_user = services
    with pytest.raises(ValueError, match="distint"):
        PlatformXPublisher(
            store=store,
            repository=repository,
            x_client=x_client,
            reviewer_actor_id=author.id,
            publisher_actor_id=publishing_user.id,
        )
    with pytest.raises(ValueError, match="distintas"):
        PlatformXPublisher(
            store=store,
            repository=repository,
            x_client=x_client,
            reviewer_actor_id=services[3].id,
            publisher_actor_id=services[1].id,
        )

    tampered = PreparedAutomation(
        claim=prepared.claim,
        candidate=prepared.candidate,
        assessment=prepared.assessment,
        media=make_media(ONE_PIXEL_JPEG, mime_type="image/jpeg"),
    )
    publisher = PlatformXPublisher(
        store=store,
        repository=repository,
        x_client=x_client,
        reviewer_actor_id=services[3].id,
        publisher_actor_id=publishing_user.id,
        environ={LIVE_PUBLISH_ENV: "true", "COLMAT_DIRECT_PUBLISH_ENABLED": "true"},
    )
    with pytest.raises(AutomationError, match="snapshot"):
        publisher.publish(tampered)


def test_telegram_notifier_sends_bounded_redacted_operational_text() -> None:
    client = FakeTelegram()
    notifier = TelegramAutomationNotifier(client, chat_id=-1001234567890)
    event = AutomationEvent(
        kind=AutomationEventKind.REVIEW_REQUIRED,
        slot_id="dato-manana",
        idempotency_key="colmat:auto:v1:2026-08-29:dato-manana",
        detail="Revisar token=super-secret-value antes de aprobar.",
        engagement_score=88,
        provider_post_id=None,
    )

    notifier.notify(event)

    chat_id, text, _kwargs = client.messages[0]
    assert chat_id == -1001234567890
    assert "REVISIÓN REQUERIDA" in text
    assert "88/100" in text
    assert "no predice alcance" in text
    assert "super-secret-value" not in text
    assert "[REDACTED]" in text
    assert len(text) <= 4096


def test_telegram_notifier_sends_snapshot_image_and_one_use_review_buttons(
    store, policy, tmp_path, monkeypatch
) -> None:
    services = bootstrap_services(store, direct=False, monkeypatch=monkeypatch)
    owner, _scheduler, _author, reviewer, _publisher = services
    chat_id = REVIEW_CHAT_ID
    telegram_user_id = REVIEW_TELEGRAM_USER_ID
    repository = make_repository(store, services, tmp_path)
    prepared = make_prepared(policy)
    assert repository.claim_slot(prepared.claim, daily_limit=1) is ClaimDecision.CLAIMED
    repository.save_prepared(prepared)
    repository.mark_review_required(prepared.claim.idempotency_key, reason="Revisión humana")
    client = FakeTelegram()
    notifier = TelegramAutomationNotifier(
        client,
        chat_id=chat_id,
        store=store,
        repository=repository,
        reviewer_telegram_user_id=telegram_user_id,
        actor_id=services[1].id,
        clock=lambda: NOW,
    )

    notifier.notify(
        AutomationEvent(
            kind=AutomationEventKind.REVIEW_REQUIRED,
            slot_id=prepared.claim.slot.id,
            idempotency_key=prepared.claim.idempotency_key,
            detail="Revisión humana",
            engagement_score=prepared.assessment.score,
        )
    )

    assert len(client.messages) == 1
    sent_chat, content, options = client.photos[0]
    assert sent_chat == str(chat_id)
    assert content == prepared.media.content
    assert options["filename"] == prepared.media.filename
    assert prepared.claim.idempotency_key not in options["caption"]
    assert "reply_markup" not in options
    controls_chat, controls_text, controls_options = client.messages[0]
    assert controls_chat == str(chat_id)
    assert prepared.draft.text in controls_text
    buttons = controls_options["reply_markup"]["inline_keyboard"][0]
    assert buttons[0]["callback_data"].startswith("approve:")
    assert buttons[1]["callback_data"].startswith("reject:")
    assert buttons[0]["callback_data"] != buttons[1]["callback_data"]
    notification = store.get_automation_review_notification_for_run(
        repository.get_record(prepared.claim.idempotency_key).run_id,
        actor_id=owner.id,
    )
    assert notification.status_value is AutomationReviewNotificationStatus.SENT
    assert notification.photo_message_id is not None
    assert notification.review_message_id is not None
    assert not hasattr(notification, "approve_nonce")
    assert not hasattr(notification, "reject_nonce")


@pytest.mark.parametrize(
    ("error", "expected_delivery", "expected_persisted"),
    (
        (
            TelegramTransportError("respuesta ambigua"),
            ReviewNotificationDeliveryStatus.UNKNOWN,
            AutomationReviewNotificationStatus.UNKNOWN,
        ),
        (
            TelegramApiError("rechazo explícito"),
            ReviewNotificationDeliveryStatus.FAILED,
            AutomationReviewNotificationStatus.FAILED,
        ),
    ),
)
def test_review_outbox_never_retries_ambiguous_telegram_delivery(
    store,
    policy,
    tmp_path,
    monkeypatch,
    error,
    expected_delivery,
    expected_persisted,
) -> None:
    services = bootstrap_services(store, direct=False, monkeypatch=monkeypatch)
    owner, scheduler, _author, _reviewer, _publisher = services
    repository = make_repository(store, services, tmp_path)
    prepared = make_prepared(policy, media=False)
    assert repository.claim_slot(prepared.claim, daily_limit=1) is ClaimDecision.CLAIMED
    repository.save_prepared(prepared)
    repository.mark_review_required(
        prepared.claim.idempotency_key,
        reason="Revisión humana",
    )
    notification = store.get_automation_review_notification_for_run(
        repository.get_record(prepared.claim.idempotency_key).run_id,
        actor_id=owner.id,
    )
    client = FakeTelegram()
    calls = 0

    def fail_send(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise error

    client.send_message = fail_send
    worker = AutomationReviewNotificationWorker(
        store=store,
        telegram_client=client,
        actor_id=scheduler.id,
        media_root=repository.media_root,
        clock=lambda: NOW,
    )

    first = worker.deliver_one(notification.id)
    second = worker.deliver_one(notification.id)

    assert first.status is expected_delivery
    assert second.status is expected_delivery
    assert calls == 1
    persisted = store.get_automation_review_notification(
        notification.id,
        actor_id=owner.id,
    )
    assert persisted.status_value is expected_persisted
