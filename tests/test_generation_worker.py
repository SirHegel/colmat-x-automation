from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from colmat_x.automation import AutomationError
from colmat_x.editorial import load_editorial_policy, validate_ai_draft
from colmat_x.generation_worker import (
    MAX_GENERATED_IMAGE_BYTES,
    QueuedGenerationWorker,
    QueueGenerationStatus,
)
from colmat_x.minimax import GeneratedImage
from colmat_x.platform_store import (
    Approval,
    CallbackIntent,
    ConflictError,
    DraftStatus,
    GenerationNotification,
    GenerationNotificationStatus,
    GenerationRequestStatus,
    PlatformStore,
    PublicationRequest,
)
from colmat_x.rbac import Role
from colmat_x.telegram_api import TelegramTransportError
from tests.factories import ONE_PIXEL_JPEG, ONE_PIXEL_PNG

NOW = datetime(2026, 8, 30, 16, 0, tzinfo=UTC)
POLICY = load_editorial_policy(Path("config/editorial-policy.yaml"))


class FakeMiniMax:
    def __init__(self, *, image: GeneratedImage | None = None) -> None:
        self.image = image
        self.draft_calls: list[str] = []
        self.image_calls: list[str] = []

    def generate_draft(self, brief, policy, *, category=None, institution=None):
        assert policy is POLICY
        assert category is None
        assert institution is None
        self.draft_calls.append(brief)
        return validate_ai_draft(
            {
                "categoria": "dato_semana",
                "institucion": "colmat",
                "texto": "Colombia registra 25 % del indicador. Fuente: DANE 2024.",
                "cifra": "25 %",
                "fuente": "DANE 2024",
                "visual": {
                    "tipo": "tipografica",
                    "descripcion": "La cifra ocupa el centro sobre fondo ocre y tinta.",
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

    def generate_image(self, prompt, policy, **kwargs):
        assert policy is POLICY
        assert kwargs["aspect_ratio"] == "16:9"
        self.image_calls.append(prompt)
        assert self.image is not None
        return self.image


class FakeTelegram:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.messages: list[tuple[int, str, object]] = []
        self.photos: list[tuple[int, bytes, str, str]] = []

    def send_message(self, chat_id, text, *, reply_markup=None, **_kwargs):
        if self.error is not None:
            raise self.error
        self.messages.append((chat_id, text, reply_markup))
        return {"message_id": 501}

    def send_photo_bytes(
        self,
        chat_id,
        content,
        *,
        filename,
        mime_type,
        **_kwargs,
    ):
        if self.error is not None:
            raise self.error
        self.photos.append((chat_id, content, filename, mime_type))
        return {"message_id": 500}


def team(store: PlatformStore):
    owner, _ = store.bootstrap_owner(
        email="owner@generation.test",
        display_name="Owner",
        now=NOW,
    )
    scheduler = store.create_user(
        actor_id=owner.id,
        email="scheduler@generation.test",
        display_name="OpenClaw",
        now=NOW,
    )
    store.grant_membership(scheduler.id, Role.SCHEDULER, actor_id=owner.id, now=NOW)
    author = store.create_user(
        actor_id=owner.id,
        email="author@generation.test",
        display_name="MiniMax Author",
        now=NOW,
    )
    store.grant_membership(author.id, Role.EDITOR, actor_id=owner.id, now=NOW)
    store.bind_telegram_chat(
        -202,
        telegram_user_id=101,
        actor_id=owner.id,
        user_id=owner.id,
        purpose="review",
        now=NOW,
    )
    return owner, scheduler, author


def enqueue(store: PlatformStore, owner_id: str, *, generate_image: bool):
    return store.enqueue_generation_request(
        "Explica una cifra nacional con su fuente primaria verificable.",
        actor_id=owner_id,
        telegram_user_id=101,
        chat_id=-202,
        idempotency_key="telegram:77:generar",
        generate_image=generate_image,
        now=NOW,
    )


def worker(
    store: PlatformStore,
    scheduler_id: str,
    author_id: str,
    tmp_path: Path,
    *,
    minimax: FakeMiniMax,
    telegram: FakeTelegram,
    enabled: bool = True,
) -> QueuedGenerationWorker:
    ticks = iter(NOW + timedelta(seconds=index) for index in range(100))
    return QueuedGenerationWorker(
        store=store,
        minimax_client=minimax,  # type: ignore[arg-type]
        telegram_client=telegram,  # type: ignore[arg-type]
        policy=POLICY,
        worker_actor_id=scheduler_id,
        author_actor_id=author_id,
        media_root=tmp_path / "media",
        environ={"COLMAT_GENERATION_ENABLED": "true" if enabled else "false"},
        clock=lambda: next(ticks),
    )


def test_worker_generates_review_and_delivers_hash_bound_callbacks(tmp_path: Path) -> None:
    with PlatformStore("sqlite+pysqlite:///:memory:") as store:
        owner, scheduler, author = team(store)
        request = enqueue(store, owner.id, generate_image=False)
        telegram = FakeTelegram()
        results = worker(
            store,
            scheduler.id,
            author.id,
            tmp_path,
            minimax=FakeMiniMax(),
            telegram=telegram,
        ).run()

        stored = store.get_generation_request(request.id, actor_id=owner.id)
        draft = store.get_draft(stored.draft_id or "", actor_id=owner.id)
        notification = next(
            item
            for item in store.list_generation_requests(actor_id=owner.id)
            if item.id == request.id
        )
        with store.session() as session:
            outbox = session.scalar(
                select(GenerationNotification).where(
                    GenerationNotification.generation_request_id == request.id
                )
            )
            assert outbox is not None
            callback_count = session.scalar(select(func.count(CallbackIntent.id)))
            approval_count = session.scalar(select(func.count(Approval.id)))
            publication_count = session.scalar(select(func.count(PublicationRequest.id)))

    assert [result.status for result in results] == [
        QueueGenerationStatus.GENERATED,
        QueueGenerationStatus.NOTIFIED,
    ]
    assert stored.status_value is GenerationRequestStatus.SUCCEEDED
    assert notification.draft_id == draft.id
    assert draft.status_value is DraftStatus.IN_REVIEW
    assert draft.created_by == author.id
    assert approval_count == 0
    assert publication_count == 0
    assert callback_count == 2
    assert outbox.status_value is GenerationNotificationStatus.SENT
    assert {column.name for column in GenerationNotification.__table__.columns}.isdisjoint(
        {"approve_nonce", "reject_nonce"}
    )
    assert len(telegram.messages) == 1
    keyboard = telegram.messages[0][2]
    assert isinstance(keyboard, dict)
    callbacks = keyboard["inline_keyboard"][0]
    assert callbacks[0]["callback_data"].startswith("approve:")
    assert callbacks[1]["callback_data"].startswith("reject:")


def test_editor_request_is_delivered_to_bound_reviewer_with_valid_callbacks(
    tmp_path: Path,
) -> None:
    with PlatformStore("sqlite+pysqlite:///:memory:") as store:
        owner, scheduler, author = team(store)
        editor = store.create_user(
            actor_id=owner.id,
            email="requester@generation.test",
            display_name="Editorial Requester",
            now=NOW,
        )
        store.grant_membership(editor.id, Role.EDITOR, actor_id=owner.id, now=NOW)
        store.bind_telegram_chat(
            -303,
            telegram_user_id=202,
            actor_id=owner.id,
            user_id=editor.id,
            purpose="control",
            now=NOW,
        )
        request = store.enqueue_generation_request(
            "Explica una cifra nacional con su fuente primaria verificable.",
            actor_id=editor.id,
            telegram_user_id=202,
            chat_id=-303,
            idempotency_key="telegram:88:generar",
            generate_image=False,
            now=NOW,
        )
        telegram = FakeTelegram()

        results = worker(
            store,
            scheduler.id,
            author.id,
            tmp_path,
            minimax=FakeMiniMax(),
            telegram=telegram,
        ).run()

        with store.session() as session:
            outbox = session.scalar(
                select(GenerationNotification).where(
                    GenerationNotification.generation_request_id == request.id
                )
            )
            callbacks = list(
                session.scalars(
                    select(CallbackIntent).where(CallbackIntent.draft_id == outbox.draft_id)
                )
            )

    assert [item.status for item in results] == [
        QueueGenerationStatus.GENERATED,
        QueueGenerationStatus.NOTIFIED,
    ]
    assert outbox is not None
    assert outbox.telegram_user_id == "101"
    assert outbox.chat_id == "-202"
    assert [message[0] for message in telegram.messages] == [-202]
    assert len(callbacks) == 2
    assert {intent.user_id for intent in callbacks} == {owner.id}


def test_worker_generates_and_verifies_image_before_review(tmp_path: Path) -> None:
    image_content = ONE_PIXEL_PNG
    image = GeneratedImage(
        content=image_content,
        mime_type="image/png",
        sha256=hashlib.sha256(image_content).hexdigest(),
        width=1280,
        height=720,
        model="image-01",
        request_id="image-request",
        alt_text="Descripción accesible.",
    )
    with PlatformStore("sqlite+pysqlite:///:memory:") as store:
        owner, scheduler, author = team(store)
        request = enqueue(store, owner.id, generate_image=True)
        telegram = FakeTelegram()
        results = worker(
            store,
            scheduler.id,
            author.id,
            tmp_path,
            minimax=FakeMiniMax(image=image),
            telegram=telegram,
        ).run()
        revision = store.get_current_revision(
            store.get_generation_request(request.id, actor_id=owner.id).draft_id or "",
            actor_id=owner.id,
        )

    assert revision.image_sha256 == image.sha256
    assert [result.status for result in results] == [
        QueueGenerationStatus.GENERATED,
        QueueGenerationStatus.NOTIFIED,
    ]
    assert telegram.photos[0][1] == image_content
    assert telegram.photos[0][3] == "image/png"


def test_worker_rejects_generated_image_over_five_mib_without_draft(tmp_path: Path) -> None:
    content = b"x" * (MAX_GENERATED_IMAGE_BYTES + 1)
    image = GeneratedImage(
        content=content,
        mime_type="image/png",
        sha256=hashlib.sha256(content).hexdigest(),
        width=1280,
        height=720,
        model="image-01",
        request_id=None,
        alt_text="Imagen grande.",
    )
    with PlatformStore("sqlite+pysqlite:///:memory:") as store:
        owner, scheduler, author = team(store)
        request = enqueue(store, owner.id, generate_image=True)
        selected_worker = worker(
            store,
            scheduler.id,
            author.id,
            tmp_path,
            minimax=FakeMiniMax(image=image),
            telegram=FakeTelegram(),
        )
        with pytest.raises(AutomationError, match="5 MiB"):
            selected_worker._persist_image(image)
        results = selected_worker.run()
        stored = store.get_generation_request(request.id, actor_id=owner.id)
        drafts = store.list_drafts(actor_id=owner.id)

    assert [result.status for result in results] == [QueueGenerationStatus.FAILED]
    assert stored.status_value is GenerationRequestStatus.FAILED
    assert drafts == []
    assert not (tmp_path / "media").exists() or not any((tmp_path / "media").iterdir())


@pytest.mark.parametrize(
    ("content", "mime_type", "message"),
    [
        (b"contenido que no es una imagen", "image/png", "firma"),
        (ONE_PIXEL_PNG, "image/jpeg", "no coincide"),
    ],
)
def test_worker_rejects_false_or_mismatched_generated_image_without_draft(
    tmp_path: Path,
    content: bytes,
    mime_type: str,
    message: str,
) -> None:
    image = GeneratedImage(
        content=content,
        mime_type=mime_type,
        sha256=hashlib.sha256(content).hexdigest(),
        width=1280,
        height=720,
        model="image-01",
        request_id=None,
        alt_text="Descripción accesible.",
    )
    with PlatformStore("sqlite+pysqlite:///:memory:") as store:
        owner, scheduler, author = team(store)
        request = enqueue(store, owner.id, generate_image=True)
        selected_worker = worker(
            store,
            scheduler.id,
            author.id,
            tmp_path,
            minimax=FakeMiniMax(image=image),
            telegram=FakeTelegram(),
        )
        with pytest.raises(AutomationError, match=message):
            selected_worker._persist_image(image)
        results = selected_worker.run()
        stored = store.get_generation_request(request.id, actor_id=owner.id)
        drafts = store.list_drafts(actor_id=owner.id)

    assert [result.status for result in results] == [QueueGenerationStatus.FAILED]
    assert stored.status_value is GenerationRequestStatus.FAILED
    assert drafts == []
    assert not (tmp_path / "media").exists()


@pytest.mark.parametrize(
    ("content", "mime_type", "message"),
    [
        (b"contenido durable que no es una imagen", "image/png", "firma"),
        (ONE_PIXEL_JPEG, "image/png", "no coincide"),
    ],
)
def test_notification_load_rejects_false_or_mismatched_durable_image(
    tmp_path: Path,
    content: bytes,
    mime_type: str,
    message: str,
) -> None:
    with PlatformStore("sqlite+pysqlite:///:memory:") as store:
        owner, scheduler, author = team(store)
        media_root = tmp_path / "media"
        media_root.mkdir()
        digest = hashlib.sha256(content).hexdigest()
        image_path = media_root / f"{digest}.png"
        image_path.write_bytes(content)
        store.register_media_asset(
            actor_id=author.id,
            kind="image",
            url=image_path.as_uri(),
            sha256=digest,
            mime_type=mime_type,
            byte_size=len(content),
            metadata={"filename": "revision.png", "alt_text": "Descripción accesible."},
            now=NOW,
        )
        selected_worker = worker(
            store,
            scheduler.id,
            author.id,
            tmp_path,
            minimax=FakeMiniMax(),
            telegram=FakeTelegram(),
        )

        with pytest.raises(AutomationError, match=message):
            selected_worker._load_notification_image(digest)


def test_transport_ambiguity_is_terminal_and_not_retried(tmp_path: Path) -> None:
    with PlatformStore("sqlite+pysqlite:///:memory:") as store:
        owner, scheduler, author = team(store)
        request = enqueue(store, owner.id, generate_image=False)
        first = worker(
            store,
            scheduler.id,
            author.id,
            tmp_path,
            minimax=FakeMiniMax(),
            telegram=FakeTelegram(error=TelegramTransportError("ambiguous")),
        ).run()
        second = worker(
            store,
            scheduler.id,
            author.id,
            tmp_path,
            minimax=FakeMiniMax(),
            telegram=FakeTelegram(),
        ).run()
        with store.session() as session:
            outbox = session.scalar(
                select(GenerationNotification).where(
                    GenerationNotification.generation_request_id == request.id
                )
            )

    assert [item.status for item in first] == [
        QueueGenerationStatus.GENERATED,
        QueueGenerationStatus.NOTIFICATION_UNKNOWN,
    ]
    assert second == ()
    assert outbox is not None
    assert outbox.status_value is GenerationNotificationStatus.UNKNOWN


def test_gate_and_fencing_block_generation(tmp_path: Path) -> None:
    with PlatformStore("sqlite+pysqlite:///:memory:") as store:
        owner, scheduler, author = team(store)
        request = enqueue(store, owner.id, generate_image=False)
        disabled = worker(
            store,
            scheduler.id,
            author.id,
            tmp_path,
            minimax=FakeMiniMax(),
            telegram=FakeTelegram(),
            enabled=False,
        )
        with pytest.raises(AutomationError, match="COLMAT_GENERATION_ENABLED"):
            disabled.run()
        claim = store.claim_generation_request(
            actor_id=scheduler.id,
            lease_seconds=5,
            now=NOW,
        )
        assert claim is not None
        with pytest.raises(ConflictError, match="token o fence"):
            store.validate_generation_claim(
                request.id,
                actor_id=scheduler.id,
                claim_token="x" * 43,
                claim_fence=claim.claim_fence,
                now=NOW + timedelta(seconds=1),
            )
        with pytest.raises(ConflictError, match="quedó UNKNOWN"):
            store.validate_generation_claim(
                request.id,
                actor_id=scheduler.id,
                claim_token=claim.claim_token,
                claim_fence=claim.claim_fence,
                now=NOW + timedelta(seconds=6),
            )
        stored = store.get_generation_request(request.id, actor_id=owner.id)

    assert stored.status_value is GenerationRequestStatus.UNKNOWN
