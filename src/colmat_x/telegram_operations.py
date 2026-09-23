"""Adaptador entre los comandos del bot de Telegram y la plataforma.

Este módulo implementa :class:`colmat_x.telegram_bot.TelegramBotOperations` sobre
:class:`colmat_x.platform_store.PlatformStore`. Todas sus operaciones consultan o
mutan el estado transaccional; ninguna llama a X ni a un proveedor de IA. Las
órdenes con efecto externo o irreversible devuelven un teclado de confirmación
cuyo nonce vive en ``control_confirmations`` y solo vale una vez.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from colmat_x.automation import (
    DEFAULT_AUTOMATION_CONFIG_PATH,
    AutomationConfigurationError,
    automation_slot_mapping,
    load_automation_config,
)
from colmat_x.editorial import DEFAULT_POLICY_PATH, EditorialPolicyError, load_editorial_policy
from colmat_x.platform_store import (
    CANONICAL_AUTOMATION_WEEKDAYS,
    AutomationMode,
    AutomationRunStatus,
    ConflictError,
    DraftStatus,
    EditorialLine,
    GenerationRequestStatus,
    NotFoundError,
    PlatformStore,
    PlatformStoreError,
    PublicationRequestStatus,
    StaleSnapshotError,
)
from colmat_x.rbac import AuthorizationError, Role
from colmat_x.research_registry import (
    RESEARCH_ONLY_BRIEF_PREFIX,
    ResearchRegistry,
    load_gustavo_bueno_registry,
)
from colmat_x.telegram_bot import (
    RESEARCH_PATTERN_STEPS,
    TELEGRAM_DELEGATABLE_ROLES,
    BotAutomationMode,
    CommandResult,
    ControlAction,
    DecisionResult,
    InlineButton,
    SafeguardGate,
    approval_callback_data,
    confirmation_keyboard,
    rejection_callback_data,
)

CONFIRMATION_TTL = timedelta(minutes=10)
REVIEW_BUTTONS_TTL = timedelta(hours=6)
STALE_RUN_GRACE = timedelta(minutes=30)
_LOCAL_ZONE = ZoneInfo("America/Bogota")
_SAFE_ERRORS = (AuthorizationError, PlatformStoreError, ValueError)
_ACTION_LABELS: Mapping[ControlAction, str] = {
    ControlAction.SAFEGUARD_LIVE_ON: "abrir el seguro live",
    ControlAction.SAFEGUARD_DIRECT_ON: "abrir el seguro direct",
    ControlAction.MODE_DIRECT: "cambiar la agenda a direct",
    ControlAction.AGENDA_ENABLE: "activar la agenda en direct",
    ControlAction.USER_DEACTIVATE: "dar de baja a un integrante",
    ControlAction.RECONCILE_PUBLISHED: "conciliar como publicado",
    ControlAction.RECONCILE_NOT_PUBLISHED: "conciliar como no publicado",
}
_SAFEGUARD_FIELDS: Mapping[SafeguardGate, str] = {
    SafeguardGate.LIVE: "live_enabled",
    SafeguardGate.DIRECT: "direct_publish_enabled",
}
_SAFEGUARD_OPEN_ACTIONS: Mapping[SafeguardGate, ControlAction] = {
    SafeguardGate.LIVE: ControlAction.SAFEGUARD_LIVE_ON,
    SafeguardGate.DIRECT: ControlAction.SAFEGUARD_DIRECT_ON,
}


class PlatformTelegramOperations:
    """Operaciones editoriales y operativas permitidas desde Telegram; nunca publica."""

    def __init__(
        self,
        store: PlatformStore,
        *,
        generate_images: bool = True,
        research_registry: ResearchRegistry | None = None,
        now: Callable[[], datetime] | None = None,
        automation_config_path: Path = DEFAULT_AUTOMATION_CONFIG_PATH,
        policy_path: Path = DEFAULT_POLICY_PATH,
    ) -> None:
        self.store = store
        if not isinstance(generate_images, bool):
            raise TypeError("generate_images debe ser booleano")
        self._generate_images = generate_images
        if research_registry is not None and not isinstance(research_registry, ResearchRegistry):
            raise TypeError("research_registry debe ser ResearchRegistry")
        self._research_registry = research_registry
        self._now = now or (lambda: datetime.now(UTC))
        self._automation_config_path = Path(automation_config_path)
        self._policy_path = Path(policy_path)

    # ------------------------------------------------------------------
    # Consultas editoriales y de equipo
    # ------------------------------------------------------------------

    def get_status(self, *, telegram_user_id: int, chat_id: int) -> str:
        actor = self._actor(telegram_user_id, chat_id)
        drafts = self.store.list_drafts(actor_id=actor.id)
        counts = Counter(draft.status for draft in drafts)
        labels = (
            (DraftStatus.DRAFT.value, "borradores"),
            (DraftStatus.IN_REVIEW.value, "en revisión"),
            (DraftStatus.APPROVED.value, "aprobados"),
            (DraftStatus.REJECTED.value, "rechazados"),
            (DraftStatus.PUBLISHED.value, "publicados"),
        )
        summary = "; ".join(f"{counts[key]} {label}" for key, label in labels)
        return f"Estado editorial: {summary}. Total: {len(drafts)}."

    def get_team(self, *, telegram_user_id: int, chat_id: int) -> str:
        actor = self._actor(telegram_user_id, chat_id)
        memberships = self.store.list_memberships(actor_id=actor.id)
        lines = ["Equipo Colmat:"]
        for membership in memberships:
            user = self.store.get_user(membership.user_id)
            state = "activo" if user.is_active else "inactivo"
            lines.append(f"• {user.display_name} — {membership.role} ({state})")
        return "\n".join(lines)

    def list_telegram_users(self, *, telegram_user_id: int, chat_id: int) -> str:
        actor = self._actor(telegram_user_id, chat_id)
        try:
            memberships = self.store.list_memberships(actor_id=actor.id)
            bindings = self.store.list_telegram_bindings(actor_id=actor.id)
        except (AuthorizationError, NotFoundError, ValueError):
            return "No se pudieron consultar los vínculos; la operación requiere al owner."
        bindings_by_user: dict[str, list[str]] = {}
        for binding in bindings:
            if binding.purpose != "control":
                continue
            state = "activo" if binding.is_active else "inactivo"
            private = "privado" if binding.chat_id == binding.telegram_user_id else "otro chat"
            bindings_by_user.setdefault(binding.user_id, []).append(
                f"{binding.telegram_user_id} ({state}, {private})"
            )
        lines = ["Usuarios y acceso Telegram (IDs numéricos; no @username):"]
        for index, membership in enumerate(memberships):
            user = self.store.get_user(membership.user_id)
            telegram_ids = ", ".join(bindings_by_user.get(user.id, ())) or "sin vínculo"
            state = "activo" if user.is_active else "inactivo"
            entry = (
                f"• {user.display_name} — {membership.role}, {state}\n"
                f"  user_id={user.id}; Telegram={telegram_ids}"
            )
            if len("\n".join((*lines, entry))) > 3_800:
                lines.append(
                    f"… {len(memberships) - index} usuario(s) omitidos; consulta la auditoría "
                    "o el panel para el listado completo."
                )
                break
            lines.append(entry)
        return "\n".join(lines)

    def invite_telegram_user(
        self,
        target_telegram_user_id: int,
        role: str,
        email: str,
        display_name: str,
        *,
        request_id: str,
        telegram_user_id: int,
        chat_id: int,
    ) -> CommandResult:
        del request_id  # El ID privado + los datos exactos hacen el alta idempotente.
        actor = self._actor(telegram_user_id, chat_id)
        try:
            user, membership, _binding = self.store.provision_telegram_team_member(
                actor_id=actor.id,
                telegram_user_id=target_telegram_user_id,
                email=email,
                display_name=display_name,
                role=Role(role),
            )
        except (AuthorizationError, ConflictError, NotFoundError, ValueError):
            return CommandResult(
                "No se completó el alta: debe ejecutarla el owner con un ID numérico no "
                "vinculado, un correo nuevo y un rol delegable.",
                accepted=False,
            )
        return CommandResult(
            f"Alta lista: {user.display_name} ({membership.role}), user_id={user.id}, "
            f"Telegram={target_telegram_user_id}. Debe abrir el bot en privado y usar /start."
        )

    def bind_telegram_user(
        self,
        target_telegram_user_id: int,
        user_id: str,
        *,
        request_id: str,
        telegram_user_id: int,
        chat_id: int,
    ) -> CommandResult:
        del request_id  # bind_telegram_chat es un upsert auditado por identidad exacta.
        actor = self._actor(telegram_user_id, chat_id)
        try:
            actor_membership = next(
                membership
                for membership in self.store.list_memberships(actor_id=actor.id)
                if membership.user_id == actor.id
            )
            if Role(actor_membership.role) is not Role.OWNER:
                raise AuthorizationError("Solo el owner puede vincular otras identidades")
            self.store.bind_telegram_chat(
                target_telegram_user_id,
                telegram_user_id=target_telegram_user_id,
                actor_id=actor.id,
                user_id=user_id,
                purpose="control",
            )
        except (AuthorizationError, ConflictError, NotFoundError, StopIteration, ValueError):
            return CommandResult(
                "No se creó el vínculo: comprueba el user_id, el ID numérico y que la "
                "identidad no pertenezca a otra cuenta.",
                accepted=False,
            )
        return CommandResult(
            f"Telegram {target_telegram_user_id} quedó vinculado a user_id={user_id} en chat "
            "privado. El rol de la membresía limita sus comandos."
        )

    def get_editorial_line(
        self,
        month: str | None,
        *,
        telegram_user_id: int,
        chat_id: int,
    ) -> str:
        actor = self._actor(telegram_user_id, chat_id)
        selected_month = month or self._editorial_month()
        line = self.store.get_editorial_line(selected_month, actor_id=actor.id)
        if line is None:
            return f"No hay línea editorial fijada para {selected_month}."
        return f"Línea editorial {line.month} (v{line.version}): {line.line_text}"

    def set_editorial_line(
        self,
        month: str,
        text: str,
        *,
        request_id: str,
        telegram_user_id: int,
        chat_id: int,
    ) -> CommandResult:
        del request_id  # El upsert idéntico no crea una versión adicional.
        actor = self._actor(telegram_user_id, chat_id)
        try:
            line = self.store.set_editorial_line(month, text, actor_id=actor.id)
        except (AuthorizationError, ConflictError, NotFoundError, ValueError):
            return CommandResult(
                "No se fijó la línea: solo el owner puede modificar un mes AAAA-MM válido.",
                accepted=False,
            )
        return CommandResult(
            f"Línea editorial {line.month} fijada en versión {line.version}. "
            "No se generó ni publicó contenido."
        )

    def research_topic(
        self,
        topic: str,
        *,
        request_id: str,
        telegram_user_id: int,
        chat_id: int,
    ) -> CommandResult:
        actor = self._actor(telegram_user_id, chat_id)
        month = self._editorial_month()
        try:
            line = self.store.get_editorial_line(month, actor_id=actor.id)
            if line is None:
                raise ConflictError("No hay línea editorial mensual")
            registry = self._research_registry or load_gustavo_bueno_registry()
            # La directriz mensual contextualiza la síntesis, pero no debe contaminar
            # la selección bibliográfica por título del tema solicitado.
            selected_references = registry.select(topic)
            pattern = " ".join(
                f"{index}) {step}" for index, step in enumerate(RESEARCH_PATTERN_STEPS, start=1)
            )
            brief_prefix = (
                f"{RESEARCH_ONLY_BRIEF_PREFIX} Tema: {topic}. Línea {line.month} "
                f"v{line.version}: {line.line_text} Patrón obligatorio: {pattern}"
            )
            if selected_references:
                suffix = (
                    " Catálogo por título, no cita: "
                    + " | ".join(
                        _research_reference_text(reference) for reference in selected_references
                    )
                    + ". URLs acotadas; verifica pertinencia y hechos."
                )
            else:
                suffix = (
                    " Sin obra pertinente: no inventes. URLs maestras autorizadas: "
                    + " ".join(registry.canonical_urls[:2])
                    + ". El worker recuperará solo esas fuentes; inferencias POR VERIFICAR."
                )
            brief = brief_prefix + suffix
            while len(brief) > 1_000 and len(selected_references) > 1:
                selected_references = selected_references[:-1]
                suffix = (
                    " Catálogo por título, no cita: "
                    + " | ".join(
                        _research_reference_text(reference) for reference in selected_references
                    )
                    + ". URLs acotadas; verifica pertinencia y hechos."
                )
                brief = brief_prefix + suffix
            if len(brief) > 1_000:
                raise ValueError("Tema, línea y referencias superan el brief permitido")
            generation_request = self.store.enqueue_generation_request(
                brief,
                actor_id=actor.id,
                telegram_user_id=telegram_user_id,
                chat_id=chat_id,
                idempotency_key=request_id,
                generate_image=False,
                category="dato_semana",
                institution="colmat",
            )
        except (AuthorizationError, ConflictError, NotFoundError, ValueError):
            return CommandResult(
                "No se encoló la investigación: se requiere owner/editor, línea del mes y "
                "capacidad disponible.",
                accepted=False,
            )
        return CommandResult(
            f"Síntesis exploratoria encolada como {generation_request.id} con la línea "
            f"{line.month} v{line.version} y {len(selected_references)} coincidencia(s) de "
            "catálogo por título. OpenClaw recuperará solo las URLs oficiales autorizadas y "
            "asignará la síntesis a MiniMax; no es navegación web abierta ni es publicable. "
            "Tras verificarla, copia sus hallazgos manualmente a /generar para crear una pieza "
            "nueva sujeta a revisión humana."
        )

    def get_research_patterns(self, *, telegram_user_id: int, chat_id: int) -> str:
        actor = self._actor(telegram_user_id, chat_id)
        # Revalida owner/editor en el store; no depende solo del permiso del procesador.
        self.store.get_editorial_line(self._editorial_month(), actor_id=actor.id)
        registry = self._research_registry or load_gustavo_bueno_registry()
        documented_count = sum(not reference.disputed for reference in registry.entries)
        disputed_count = sum(reference.disputed for reference in registry.entries)
        lines = [
            "Patrón de investigación Colmat:",
            f"Registro canónico cargado: {documented_count} títulos o series documentados + "
            f"{disputed_count} atribución "
            "disputada; se envían como máximo 3 coincidencias por título, nunca el catálogo "
            "completo, y siempre se verifica su pertinencia.",
        ]
        lines.extend(
            f"{index}. {step}" for index, step in enumerate(RESEARCH_PATTERN_STEPS, start=1)
        )
        lines.append(
            "MiniMax solo sintetiza las fuentes del encargo o del registro canónico. La "
            "verificación externa sigue siendo humana; sin fuente identificable, todo queda "
            "POR VERIFICAR y nunca pasa a publicación automática."
        )
        return "\n".join(lines)

    def get_calendar(
        self,
        *,
        days: int,
        telegram_user_id: int,
        chat_id: int,
    ) -> str:
        actor = self._actor(telegram_user_id, chat_id)
        settings = self.store.get_automation_settings(actor_id=actor.id)
        zone = ZoneInfo(settings.timezone)
        first_day = self._now().astimezone(zone).date()
        lines = [
            "Agenda Colmat "
            f"({settings.timezone}; {'activa' if settings.enabled else 'pausada'}; "
            f"modo {settings.mode}):"
        ]
        slots = tuple(settings.slots or ())
        if not slots:
            lines.append("• No hay slots diarios configurados.")
            return "\n".join(lines)
        for offset in range(days):
            local_day = first_day + timedelta(days=offset)
            for raw_slot in slots:
                if not isinstance(raw_slot, Mapping):
                    continue
                weekdays = raw_slot.get("weekdays")
                if weekdays is not None and (
                    not isinstance(weekdays, list)
                    or CANONICAL_AUTOMATION_WEEKDAYS[local_day.weekday()] not in weekdays
                ):
                    continue
                slot_id = str(raw_slot.get("id") or "slot")
                at = str(raw_slot.get("at") or "--:--")
                mode = str(raw_slot.get("mode") or settings.mode)
                lines.append(f"• {local_day.isoformat()} {at} — {slot_id} [{mode}]")
        return "\n".join(lines)

    def get_mode(self, *, telegram_user_id: int, chat_id: int) -> str:
        actor = self._actor(telegram_user_id, chat_id)
        settings = self.store.get_automation_settings(actor_id=actor.id)
        state = "activa" if settings.enabled else "pausada"
        return f"Modo actual: {settings.mode}; automatización {state}; versión {settings.version}."

    def set_mode(
        self,
        mode: BotAutomationMode,
        *,
        request_id: str,
        telegram_user_id: int,
        chat_id: int,
    ) -> CommandResult:
        """Solo aplica ``human_review`` en un paso; ``direct`` pasa por ``request_direct_mode``."""

        del request_id  # La CAS persistida y el update_id deduplicado protegen este cambio.
        if mode is BotAutomationMode.DIRECT:
            return CommandResult(
                "El modo direct exige confirmación de dos pasos; usa /modo direct en tu chat "
                "privado y confirma con el botón.",
                accepted=False,
            )
        actor = self._actor(telegram_user_id, chat_id)
        try:
            current = self.store.get_automation_settings(actor_id=actor.id)
            updated = self._apply_mode(actor, current.version, AutomationMode(mode.value))
        except _SAFE_ERRORS:
            return CommandResult(
                "No se cambió el modo: faltan permisos, la versión cambió o el kill switch "
                "direct sigue cerrado.",
                accepted=False,
            )
        return CommandResult(
            f"Modo configurado: {updated.mode}; versión {updated.version}. "
            "Este cambio no publica contenido por sí solo."
        )

    def request_direct_mode(
        self,
        *,
        request_id: str,
        telegram_user_id: int,
        chat_id: int,
    ) -> CommandResult:
        del request_id
        actor = self._actor(telegram_user_id, chat_id)
        try:
            current = self.store.get_automation_settings(actor_id=actor.id)
            safeguards = self.store.get_publication_safeguards(actor_id=actor.id)
        except _SAFE_ERRORS:
            return CommandResult("No se pudo consultar la configuración vigente.", accepted=False)
        if current.mode == AutomationMode.DIRECT.value:
            return CommandResult(f"La agenda ya está en direct (versión {current.version}).")
        if not safeguards.direct_publish_enabled:
            return CommandResult(
                "direct está bloqueado: abre primero el seguro persistido con "
                "/seguro direct on (owner/admin, chat privado, doble paso). El host del worker "
                "además debe tener COLMAT_DIRECT_PUBLISH_ENABLED=true y --live.",
                accepted=False,
            )
        slots = tuple(current.slots or ())
        text = (
            f"Vas a cambiar la agenda a modo direct (versión {current.version}, "
            f"{'activa' if current.enabled else 'pausada'}, {len(slots)} slot(s)). En direct el "
            "worker publica sin revisión humana las piezas de texto con evidencia verificada, "
            "siempre que el host conserve ambas variables de entorno, --live y los seguros "
            "persistidos abiertos."
        )
        return self._confirm(
            actor,
            ControlAction.MODE_DIRECT,
            {"expected_version": current.version},
            text,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
        )

    def generate_draft(
        self,
        brief: str,
        *,
        request_id: str,
        telegram_user_id: int,
        chat_id: int,
    ) -> CommandResult:
        actor = self._actor(telegram_user_id, chat_id)
        try:
            month = self._editorial_month()
            line = self.store.get_editorial_line(month, actor_id=actor.id)
            if line is None:
                raise ConflictError("No hay línea editorial mensual")
            guided_brief = (
                f"Línea editorial {line.month} v{line.version}: {line.line_text} Encargo: {brief}"
            )
            if len(guided_brief) > 1_000:
                raise ValueError("La línea y el encargo superan el brief permitido")
            generation_request = self.store.enqueue_generation_request(
                guided_brief,
                actor_id=actor.id,
                telegram_user_id=telegram_user_id,
                chat_id=chat_id,
                idempotency_key=request_id,
                generate_image=self._generate_images,
            )
        except (
            AuthorizationError,
            ConflictError,
            NotFoundError,
            ValueError,
        ):
            return CommandResult(
                "No se encoló la generación. Se requiere owner/editor, una línea editorial "
                "del mes y un encargo que junto con ella no supere 1000 caracteres; no se "
                "llamó a MiniMax ni se realizó ninguna publicación.",
                accepted=False,
            )
        return CommandResult(
            f"Generación encolada como {generation_request.id}. OpenClaw la asignará a "
            f"MiniMax con la línea {line.month} v{line.version} fuera del webhook; el resultado "
            "siempre exigirá revisión humana"
            f"{' e incluirá imagen' if generation_request.generate_image else ''}."
        )

    def request_publication(
        self,
        post_id: str,
        *,
        request_id: str,
        telegram_user_id: int,
        chat_id: int,
    ) -> CommandResult:
        actor = self._actor(telegram_user_id, chat_id)
        try:
            revision = self.store.get_current_revision(post_id, actor_id=actor.id)
            publication_request = self.store.enqueue_publication_request(
                post_id,
                actor_id=actor.id,
                expected_snapshot_hash=revision.snapshot_hash,
                idempotency_key=request_id,
            )
        except (
            AuthorizationError,
            ConflictError,
            NotFoundError,
            StaleSnapshotError,
            ValueError,
        ):
            return CommandResult(
                "No se encoló: el borrador debe estar aprobado, conservar el snapshot y ser "
                "solicitado por un publisher autorizado.",
                accepted=False,
            )
        return CommandResult(
            f"Solicitud encolada como {publication_request.id}; no se publicó durante el webhook."
        )

    def approve_post(
        self,
        *,
        post_id: str,
        snapshot_hash: str,
        telegram_user_id: int,
        chat_id: int,
    ) -> DecisionResult:
        actor = self._actor(telegram_user_id, chat_id)
        try:
            self.store.approve_draft(
                post_id,
                actor_id=actor.id,
                expected_snapshot_hash=snapshot_hash,
            )
        except (AuthorizationError, ConflictError, NotFoundError, StaleSnapshotError):
            return DecisionResult(
                "No se registró la aprobación: el contenido o sus permisos cambiaron.",
                accepted=False,
            )
        return DecisionResult(
            f"El borrador {post_id} fue aprobado; todavía no se ha publicado.",
        )

    def reject_post(
        self,
        *,
        post_id: str,
        snapshot_hash: str,
        telegram_user_id: int,
        chat_id: int,
    ) -> DecisionResult:
        actor = self._actor(telegram_user_id, chat_id)
        try:
            self.store.reject_draft(
                post_id,
                actor_id=actor.id,
                expected_snapshot_hash=snapshot_hash,
                reason="Rechazado mediante el control editorial de Telegram.",
            )
        except (AuthorizationError, ConflictError, NotFoundError, StaleSnapshotError):
            return DecisionResult(
                "No se registró el rechazo: el contenido o sus permisos cambiaron.",
                accepted=False,
            )
        return DecisionResult(f"El borrador {post_id} fue rechazado.")

    # ------------------------------------------------------------------
    # Agenda persistida
    # ------------------------------------------------------------------

    def get_automation_status(self, *, telegram_user_id: int, chat_id: int) -> str:
        actor = self._actor(telegram_user_id, chat_id)
        settings = self.store.get_automation_settings(actor_id=actor.id)
        safeguards = self.store.get_publication_safeguards(actor_id=actor.id)
        runs = self.store.list_automation_runs(actor_id=actor.id, limit=100)
        counts = Counter(run.status for run in runs)
        retries = sum(1 for run in runs if run.retry_requested_at is not None)
        slots = tuple(settings.slots or ())
        lines = [
            "Agenda persistida:",
            f"• Estado: {'activa' if settings.enabled else 'pausada'}; modo {settings.mode}; "
            f"versión {settings.version}; zona {settings.timezone}.",
            f"• Slots: {len(slots)}; imágenes {'sí' if settings.generate_images else 'no'}; "
            f"tope diario {settings.max_posts_per_day}; "
            f"umbral direct {settings.min_engagement_score}.",
            "• Autorización direct: "
            + (
                f"{settings.direct_authorized_by} el {_local(settings.direct_authorized_at)}"
                if settings.direct_authorized_by and settings.direct_authorized_at
                else "ninguna"
            )
            + ".",
            f"• Seguros persistidos: live {_state(safeguards.live_enabled)}; "
            f"direct {_state(safeguards.direct_publish_enabled)} (versión {safeguards.version}).",
            "• Runs recientes (máx. 100): "
            + (
                ", ".join(f"{status} {count}" for status, count in sorted(counts.items()))
                or "ninguno"
            )
            + f"; reintentos pendientes {retries}.",
            "Usa /agenda runs para el detalle y /seguros para los seguros.",
        ]
        return "\n".join(lines)

    def list_automation_runs(self, *, limit: int, telegram_user_id: int, chat_id: int) -> str:
        actor = self._actor(telegram_user_id, chat_id)
        runs = self.store.list_automation_runs(actor_id=actor.id, limit=limit)
        if not runs:
            return "No hay runs de automatización registrados."
        lines = [f"Últimos {len(runs)} run(s) de automatización:"]
        for run in runs:
            entry = (
                f"• {_local(run.scheduled_for)} {run.slot_id} [{run.mode}] {run.status} "
                f"intento {run.attempt_count}; id={run.id}"
            )
            if run.retry_requested_at is not None:
                entry += "; reintento solicitado"
            if run.error:
                entry += f"; error: {_bounded(run.error, 160)}"
            lines.append(entry)
        return "\n".join(lines)

    def validate_automation_config(self, *, telegram_user_id: int, chat_id: int) -> str:
        self._actor(telegram_user_id, chat_id)
        try:
            schedule = self._load_schedule()
        except (AutomationConfigurationError, EditorialPolicyError, ValueError) as exc:
            return f"Agenda YAML inválida ({type(exc).__name__}): {_bounded(str(exc), 400)}"
        except OSError:
            return "Agenda YAML inválida: el archivo de agenda o la política no están disponibles."
        modes = ", ".join(sorted({slot.mode.value for slot in schedule.slots}))
        return (
            f"Agenda YAML válida: timezone={schedule.timezone}; slots={len(schedule.slots)}; "
            f"daily_limit={schedule.daily_limit}; modos={modes}. No se sincronizó ni ejecutó "
            "nada; usa /agenda sincronizar para copiarla a la base."
        )

    def sync_automation_config(
        self,
        *,
        request_id: str,
        telegram_user_id: int,
        chat_id: int,
    ) -> CommandResult:
        del request_id  # La CAS de settings hace idempotente el reintento del update.
        actor = self._actor(telegram_user_id, chat_id)
        try:
            schedule = self._load_schedule()
        except (AutomationConfigurationError, EditorialPolicyError, ValueError, OSError):
            return CommandResult(
                "No se sincronizó: la agenda YAML desplegada no es válida. Revisa /agenda validar.",
                accepted=False,
            )
        try:
            current = self.store.get_automation_settings(actor_id=actor.id)
            mode = AutomationMode(current.mode)
            slots: list[dict[str, object]] = []
            for slot in schedule.slots:
                mapped = automation_slot_mapping(slot)
                mapped["mode"] = mode.value
                slots.append(mapped)
            updated = self.store.update_automation_settings(
                actor_id=actor.id,
                expected_version=current.version,
                timezone=schedule.timezone,
                slots=slots,
                generate_images=any(slot.generate_image for slot in schedule.slots),
                min_engagement_score=schedule.direct_min_engagement_score,
                max_posts_per_day=schedule.daily_limit,
            )
        except _SAFE_ERRORS:
            return CommandResult(
                "No se sincronizó: faltan permisos de agenda, la versión cambió o la agenda "
                "contradice el modo vigente.",
                accepted=False,
            )
        return CommandResult(
            f"Agenda sincronizada: slots={len(updated.slots)}; timezone={updated.timezone}; "
            f"versión {updated.version}; {'activa' if updated.enabled else 'pausada'}. "
            "No se ejecutaron runs ni publicaciones."
        )

    def enable_automation(
        self,
        *,
        request_id: str,
        telegram_user_id: int,
        chat_id: int,
    ) -> CommandResult:
        del request_id
        actor = self._actor(telegram_user_id, chat_id)
        try:
            current = self.store.get_automation_settings(actor_id=actor.id)
        except _SAFE_ERRORS:
            return CommandResult("No se pudo consultar la agenda.", accepted=False)
        if current.enabled:
            return CommandResult(f"La agenda ya está activa (versión {current.version}).")
        if current.mode == AutomationMode.DIRECT.value:
            text = (
                f"Vas a activar la agenda en modo direct (versión {current.version}, "
                f"{len(tuple(current.slots or ()))} slot(s)). El worker podrá publicar sin "
                "revisión humana en cuanto el host tenga sus variables de entorno, --live y "
                "los seguros persistidos abiertos."
            )
            return self._confirm(
                actor,
                ControlAction.AGENDA_ENABLE,
                {"expected_version": current.version},
                text,
                telegram_user_id=telegram_user_id,
                chat_id=chat_id,
            )
        try:
            updated = self.store.update_automation_settings(
                actor_id=actor.id,
                expected_version=current.version,
                enabled=True,
            )
        except _SAFE_ERRORS:
            return CommandResult(
                "No se activó la agenda: faltan permisos, la versión cambió o no hay slots.",
                accepted=False,
            )
        return CommandResult(
            f"Agenda activada en modo {updated.mode}; versión {updated.version}. "
            "No se ejecutaron runs ni publicaciones."
        )

    def disable_automation(
        self,
        *,
        request_id: str,
        telegram_user_id: int,
        chat_id: int,
    ) -> CommandResult:
        del request_id
        actor = self._actor(telegram_user_id, chat_id)
        try:
            current = self.store.get_automation_settings(actor_id=actor.id)
            if not current.enabled:
                return CommandResult(f"La agenda ya está pausada (versión {current.version}).")
            updated = self.store.update_automation_settings(
                actor_id=actor.id,
                expected_version=current.version,
                enabled=False,
            )
        except _SAFE_ERRORS:
            return CommandResult(
                "No se pausó la agenda: faltan permisos o la versión cambió.",
                accepted=False,
            )
        return CommandResult(
            f"Agenda pausada; versión {updated.version}. El worker dejará de reclamar slots; "
            "los runs ya terminales no cambian."
        )

    def retry_automation_run(
        self,
        run_id: str,
        *,
        request_id: str,
        telegram_user_id: int,
        chat_id: int,
    ) -> CommandResult:
        del request_id  # La solicitud persistida de retry ya es idempotente por run_id.
        actor = self._actor(telegram_user_id, chat_id)
        try:
            run = self.store.request_automation_run_retry(run_id, actor_id=actor.id)
        except _SAFE_ERRORS:
            return CommandResult(
                "No se solicitó el reintento: solo admite runs failed sin borrador, con la "
                "agenda y el modo vigentes, y requiere owner, admin o scheduler.",
                accepted=False,
            )
        return CommandResult(
            f"Reintento solicitado para el run {run.id} ({run.slot_id}, intento "
            f"{run.attempt_count}). El siguiente automation-run lo reclamará con la misma "
            "clave de idempotencia; no se ejecutó nada ahora."
        )

    # ------------------------------------------------------------------
    # Seguros persistidos de publicación real
    # ------------------------------------------------------------------

    def get_safeguards(self, *, telegram_user_id: int, chat_id: int) -> str:
        actor = self._actor(telegram_user_id, chat_id)
        safeguards = self.store.get_publication_safeguards(actor_id=actor.id)
        lines = [
            f"Seguros persistidos de publicación real (versión {safeguards.version}):",
            "• live: "
            + _state(safeguards.live_enabled)
            + _signed(safeguards.live_enabled_by, safeguards.live_enabled_at),
            "• direct: "
            + _state(safeguards.direct_publish_enabled)
            + _signed(safeguards.direct_enabled_by, safeguards.direct_enabled_at),
            "El worker solo contacta a X cuando estos seguros están abiertos y, además, el host "
            "tiene COLMAT_LIVE_ENABLED=true (y COLMAT_DIRECT_PUBLISH_ENABLED=true más --live "
            "para direct). Las variables del host no son visibles ni operables desde el bot.",
        ]
        return "\n".join(lines)

    def request_safeguard_change(
        self,
        gate: SafeguardGate,
        enabled: bool,
        *,
        request_id: str,
        telegram_user_id: int,
        chat_id: int,
    ) -> CommandResult:
        del request_id
        actor = self._actor(telegram_user_id, chat_id)
        field_name = _SAFEGUARD_FIELDS[gate]
        try:
            safeguards = self.store.get_publication_safeguards(actor_id=actor.id)
        except _SAFE_ERRORS:
            return CommandResult("No se pudieron consultar los seguros.", accepted=False)
        if bool(getattr(safeguards, field_name)) == enabled:
            return CommandResult(
                f"El seguro {gate.value} ya está {_state(enabled)} (versión {safeguards.version})."
            )
        if not enabled:
            try:
                updated = self.store.update_publication_safeguards(
                    actor_id=actor.id,
                    expected_version=safeguards.version,
                    **{field_name: False},
                )
            except _SAFE_ERRORS:
                return CommandResult(
                    "No se cerró el seguro: se requiere owner o admin y la versión vigente.",
                    accepted=False,
                )
            closing_note = (
                "El worker dejará de contactar a X desde su siguiente comprobación."
                if gate is SafeguardGate.LIVE
                else "Si la agenda sigue en direct, el worker se detendrá antes de reclamar "
                "slots; considera /modo human_review."
            )
            return CommandResult(
                f"Seguro {gate.value} cerrado; versión {updated.version}. {closing_note}"
            )
        if gate is SafeguardGate.LIVE:
            text = (
                "Vas a abrir el seguro persistido live. Con él abierto, publication-run y "
                "automation-run pueden enviar a X snapshots aprobados, siempre que el host "
                "tenga COLMAT_LIVE_ENABLED=true y --live."
            )
        else:
            text = (
                "Vas a abrir el seguro persistido direct. Habilita registrar el modo direct y, "
                "junto con live y las variables del host, permite publicar sin revisión humana "
                "las piezas de texto con evidencia verificada."
            )
        return self._confirm(
            actor,
            _SAFEGUARD_OPEN_ACTIONS[gate],
            {"expected_version": safeguards.version},
            text,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
        )

    # ------------------------------------------------------------------
    # Cola, revisión y conciliación
    # ------------------------------------------------------------------

    def list_queue(self, *, limit: int, telegram_user_id: int, chat_id: int) -> str:
        actor = self._actor(telegram_user_id, chat_id)
        requests = self.store.list_publication_requests(actor_id=actor.id, limit=limit)
        generation = self.store.list_generation_requests(actor_id=actor.id, limit=200)
        generation_counts = Counter(item.status for item in generation)
        lines = [
            "Cola de generación: "
            + ", ".join(
                f"{status.value} {generation_counts[status.value]}"
                for status in (
                    GenerationRequestStatus.QUEUED,
                    GenerationRequestStatus.CLAIMED,
                )
            )
            + ".",
        ]
        if not requests:
            lines.append("Cola de publicación: sin solicitudes.")
            return "\n".join(lines)
        lines.append(f"Cola de publicación (últimas {len(requests)}):")
        for request in requests:
            entry = (
                f"• {request.status} — draft {request.draft_id}; id={request.id}; "
                f"creada {_local(request.created_at)}"
            )
            if request.error:
                entry += f"; {_bounded(request.error, 120)}"
            lines.append(entry)
        if any(request.status == PublicationRequestStatus.UNKNOWN.value for request in requests):
            lines.append(
                "Hay resultados unknown: verifica la cuenta de X y usa /cola conciliar en "
                "tu chat privado."
            )
        return "\n".join(lines)

    def request_reconciliation(
        self,
        publication_request_id: str,
        *,
        published: bool,
        provider_post_id: str | None,
        request_id: str,
        telegram_user_id: int,
        chat_id: int,
    ) -> CommandResult:
        del request_id
        actor = self._actor(telegram_user_id, chat_id)
        try:
            request = self.store.get_publication_request(publication_request_id, actor_id=actor.id)
        except _SAFE_ERRORS:
            return CommandResult("No existe esa solicitud de publicación.", accepted=False)
        if request.status != PublicationRequestStatus.UNKNOWN.value:
            return CommandResult(
                f"La solicitud está en {request.status}; solo se concilian resultados unknown.",
                accepted=False,
            )
        if published:
            action = ControlAction.RECONCILE_PUBLISHED
            text = (
                f"Vas a conciliar la solicitud {request.id} (draft {request.draft_id}) como "
                f"PUBLICADA en X con id {provider_post_id}. El draft quedará publicado y no "
                "se volverá a enviar."
            )
        else:
            action = ControlAction.RECONCILE_NOT_PUBLISHED
            text = (
                f"Vas a conciliar la solicitud {request.id} (draft {request.draft_id}) como NO "
                "publicada tras comprobar la cuenta de X. El mismo snapshot no se reencola: "
                "para publicar de nuevo hay que revisar y aprobar una revisión nueva."
            )
        parameters: dict[str, Any] = {"request_id": request.id}
        if provider_post_id is not None:
            parameters["provider_post_id"] = provider_post_id
        return self._confirm(
            actor,
            action,
            parameters,
            text,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
        )

    def withdraw_approval(
        self,
        post_id: str,
        *,
        request_id: str,
        telegram_user_id: int,
        chat_id: int,
    ) -> CommandResult:
        del request_id
        actor = self._actor(telegram_user_id, chat_id)
        try:
            revision = self.store.get_current_revision(post_id, actor_id=actor.id)
            draft = self.store.withdraw_approval(
                post_id,
                actor_id=actor.id,
                expected_snapshot_hash=revision.snapshot_hash,
                reason="Aprobación retirada mediante el control editorial de Telegram.",
            )
        except _SAFE_ERRORS:
            return CommandResult(
                "No se retiró la aprobación: el draft debe estar aprobado, sin solicitud de "
                "publicación activa, y la orden debe darla un revisor distinto del autor. Si "
                "ya está en cola, cierra el seguro live.",
                accepted=False,
            )
        return CommandResult(
            f"Aprobación retirada: el borrador {draft.id} volvió a {draft.status}; requiere "
            "una nueva revisión y aprobación del snapshot exacto."
        )

    def request_review(
        self,
        post_id: str,
        *,
        request_id: str,
        telegram_user_id: int,
        chat_id: int,
    ) -> CommandResult:
        del request_id
        actor = self._actor(telegram_user_id, chat_id)
        try:
            draft = self.store.get_draft(post_id, actor_id=actor.id)
            revision = self.store.get_current_revision(post_id, actor_id=actor.id)
            if draft.status != DraftStatus.IN_REVIEW.value:
                raise ConflictError("El draft no está en revisión")
            now = self._now()
            approve, reject = self.store.issue_review_callback_intents(
                post_id,
                expected_snapshot_hash=revision.snapshot_hash,
                telegram_user_id=telegram_user_id,
                chat_id=chat_id,
                expires_at=now + REVIEW_BUTTONS_TTL,
                now=now,
            )
        except _SAFE_ERRORS:
            return CommandResult(
                "No se emitieron botones: el borrador debe estar en revisión y tú debes ser un "
                "revisor distinto del autor.",
                accepted=False,
            )
        text = (
            f"Revisión del borrador {draft.id} (revisión {revision.revision_number}, snapshot "
            f"{revision.snapshot_hash[:12]}…, publicar {_local(revision.publish_at)}):\n\n"
            f"{_bounded(revision.text, 1500)}\n\n"
            "Los botones valen 6 horas, una sola vez y solo para ti en este chat. Aprobar no "
            "publica: la publicación sigue exigiendo /publicar y los seguros."
        )
        keyboard = (
            (
                InlineButton("Aprobar", approval_callback_data(approve.nonce)),
                InlineButton("Rechazar", rejection_callback_data(reject.nonce)),
            ),
        )
        return CommandResult(text, keyboard=keyboard)

    # ------------------------------------------------------------------
    # Equipo
    # ------------------------------------------------------------------

    def request_user_deactivation(
        self,
        user_id: str,
        *,
        request_id: str,
        telegram_user_id: int,
        chat_id: int,
    ) -> CommandResult:
        del request_id
        actor = self._actor(telegram_user_id, chat_id)
        try:
            membership, user = self._delegable_member(actor, user_id)
        except _SAFE_ERRORS:
            return CommandResult(
                "No se puede dar de baja: el user_id debe pertenecer a un integrante con rol "
                "editor, reviewer, publisher, scheduler o auditor. Owners y admins se "
                "administran únicamente desde el host.",
                accepted=False,
            )
        if user.id == actor.id:
            return CommandResult("No puedes darte de baja a ti mismo.", accepted=False)
        if not user.is_active:
            return CommandResult(f"{user.display_name} ya está inactivo.")
        text = (
            f"Vas a dar de baja a {user.display_name} ({membership.role}, user_id={user.id}). "
            "Perderá el acceso al bot y al panel de inmediato; puede reactivarse con /alta."
        )
        return self._confirm(
            actor,
            ControlAction.USER_DEACTIVATE,
            {"user_id": user.id},
            text,
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
        )

    def reactivate_user(
        self,
        user_id: str,
        *,
        request_id: str,
        telegram_user_id: int,
        chat_id: int,
    ) -> CommandResult:
        del request_id  # set_user_active es idempotente para el mismo valor.
        actor = self._actor(telegram_user_id, chat_id)
        try:
            _membership, user = self._delegable_member(actor, user_id)
            if user.is_active:
                return CommandResult(f"{user.display_name} ya está activo.")
            updated = self.store.set_user_active(user.id, active=True, actor_id=actor.id)
        except _SAFE_ERRORS:
            return CommandResult(
                "No se reactivó: solo el owner puede reactivar integrantes con rol delegable.",
                accepted=False,
            )
        return CommandResult(
            f"{updated.display_name} vuelve a estar activo (user_id={updated.id})."
        )

    def change_user_role(
        self,
        user_id: str,
        role: str,
        *,
        request_id: str,
        telegram_user_id: int,
        chat_id: int,
    ) -> CommandResult:
        del request_id  # change_membership_role con el mismo rol es idempotente.
        actor = self._actor(telegram_user_id, chat_id)
        if role not in TELEGRAM_DELEGATABLE_ROLES:
            return CommandResult("Ese rol no se puede asignar desde Telegram.", accepted=False)
        try:
            membership, user = self._delegable_member(actor, user_id)
            if membership.role == role:
                return CommandResult(f"{user.display_name} ya tiene el rol {role}.")
            updated = self.store.change_membership_role(user.id, Role(role), actor_id=actor.id)
        except _SAFE_ERRORS:
            return CommandResult(
                "No se cambió el rol: solo el owner puede hacerlo y únicamente entre roles "
                "delegables (editor, reviewer, publisher, scheduler, auditor).",
                accepted=False,
            )
        return CommandResult(
            f"{user.display_name} pasa de {membership.role} a {updated.role}. Sus permisos "
            "siguen siendo los del rol, sin herencia."
        )

    # ------------------------------------------------------------------
    # Diagnóstico y auditoría
    # ------------------------------------------------------------------

    def run_diagnostics(self, *, telegram_user_id: int, chat_id: int) -> str:
        actor = self._actor(telegram_user_id, chat_id)
        lines = ["Diagnóstico del plano de control:"]
        try:
            settings = self.store.get_automation_settings(actor_id=actor.id)
            safeguards = self.store.get_publication_safeguards(actor_id=actor.id)
        except _SAFE_ERRORS:
            lines.append("• Base de datos: sin acceso a la configuración persistida.")
            return "\n".join(lines)
        lines.append(
            f"• Base de datos: ok; agenda {'activa' if settings.enabled else 'pausada'}, modo "
            f"{settings.mode}, versión {settings.version}, {len(tuple(settings.slots or ()))} "
            "slot(s)."
        )
        lines.append(
            f"• Seguros persistidos: live {_state(safeguards.live_enabled)}, direct "
            f"{_state(safeguards.direct_publish_enabled)}."
        )
        month = self._editorial_month()
        try:
            line = self.store.get_editorial_line(month, actor_id=actor.id)
            lines.append(
                f"• Línea editorial {month}: "
                + (f"fijada (v{line.version})." if line is not None else "AUSENTE.")
            )
        except _SAFE_ERRORS:
            lines.append(f"• Línea editorial {month}: sin permiso para consultarla.")
        try:
            schedule = self._load_schedule()
            lines.append(f"• Agenda YAML desplegada: válida ({len(schedule.slots)} slot(s)).")
        except (AutomationConfigurationError, EditorialPolicyError, ValueError, OSError):
            lines.append("• Agenda YAML desplegada: inválida o ausente.")
        try:
            requests = self.store.list_publication_requests(actor_id=actor.id, limit=200)
            request_counts = Counter(item.status for item in requests)
            generation = self.store.list_generation_requests(actor_id=actor.id, limit=200)
            generation_counts = Counter(item.status for item in generation)
            lines.append(
                "• Cola de publicación: "
                + ", ".join(
                    f"{status.value} {request_counts[status.value]}"
                    for status in PublicationRequestStatus
                )
                + "."
            )
            lines.append(
                "• Cola de generación: "
                + ", ".join(
                    f"{status.value} {generation_counts[status.value]}"
                    for status in GenerationRequestStatus
                )
                + "."
            )
            lines.append(self._last_publication_line(actor, requests))
        except _SAFE_ERRORS:
            lines.append("• Colas: sin permiso para consultarlas.")
        try:
            runs = self.store.list_automation_runs(actor_id=actor.id, limit=100)
            run_counts = Counter(run.status for run in runs)
            now = self._now()
            stale = sum(
                1
                for run in runs
                if run.status
                in {AutomationRunStatus.CLAIMED.value, AutomationRunStatus.PUBLISHING.value}
                and run.claimed_at < now - STALE_RUN_GRACE
            )
            retries = sum(1 for run in runs if run.retry_requested_at is not None)
            lines.append(
                "• Runs recientes: "
                + (
                    ", ".join(f"{status} {count}" for status, count in sorted(run_counts.items()))
                    or "ninguno"
                )
                + f"; estancados {stale}; reintentos pendientes {retries}."
            )
        except _SAFE_ERRORS:
            lines.append("• Runs: sin permiso para consultarlos.")
        lines.append(
            "• Host: la identidad de X (x-whoami), las credenciales de MiniMax y las variables "
            "COLMAT_LIVE_ENABLED/COLMAT_DIRECT_PUBLISH_ENABLED viven solo en el worker; el "
            "webhook no las tiene ni las verifica."
        )
        return "\n".join(lines)

    def list_audit(self, *, limit: int, telegram_user_id: int, chat_id: int) -> str:
        actor = self._actor(telegram_user_id, chat_id)
        try:
            events = self.store.list_recent_audit_events(actor_id=actor.id, limit=limit)
        except _SAFE_ERRORS:
            return "No tienes permiso de auditoría en la plataforma."
        if not events:
            return "No hay eventos de auditoría."
        lines = [f"Últimos {len(events)} evento(s) de auditoría:"]
        for event in events:
            lines.append(
                f"• #{event.sequence} {_local(event.occurred_at)} {event.action} "
                f"{event.entity_type}={event.entity_id} por {event.actor_id}"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Confirmaciones de dos pasos
    # ------------------------------------------------------------------

    def resolve_confirmation(
        self,
        nonce: str,
        *,
        action: ControlAction,
        confirm: bool,
        request_id: str,
        telegram_user_id: int,
        chat_id: int,
    ) -> CommandResult:
        del request_id  # El nonce es de un solo uso; el update_id ya está deduplicado.
        actor = self._actor(telegram_user_id, chat_id)
        label = _ACTION_LABELS[action]
        try:
            confirmation = self.store.consume_control_confirmation(
                nonce,
                action=action.value,
                telegram_user_id=telegram_user_id,
                chat_id=chat_id,
                confirm=confirm,
                now=self._now(),
            )
        except _SAFE_ERRORS:
            return CommandResult(
                "La confirmación no es válida, venció o ya se usó. Vuelve a emitir la orden.",
                accepted=False,
            )
        if not confirm:
            return CommandResult(f"Orden cancelada ({label}); no se aplicó ningún cambio.")
        parameters = confirmation.parameters if isinstance(confirmation.parameters, Mapping) else {}
        try:
            return self._apply_confirmed_action(actor, action, parameters)
        except _SAFE_ERRORS:
            return CommandResult(
                f"La orden confirmada ({label}) no se aplicó: la configuración cambió, faltan "
                "permisos o el objetivo ya no existe. Consulta el estado y repite la orden.",
                accepted=False,
            )

    def _apply_confirmed_action(
        self,
        actor: Any,
        action: ControlAction,
        parameters: Mapping[str, Any],
    ) -> CommandResult:
        if action in {ControlAction.SAFEGUARD_LIVE_ON, ControlAction.SAFEGUARD_DIRECT_ON}:
            gate = (
                SafeguardGate.LIVE
                if action is ControlAction.SAFEGUARD_LIVE_ON
                else SafeguardGate.DIRECT
            )
            updated = self.store.update_publication_safeguards(
                actor_id=actor.id,
                expected_version=_int_parameter(parameters, "expected_version"),
                **{_SAFEGUARD_FIELDS[gate]: True},
            )
            return CommandResult(
                f"Seguro {gate.value} abierto por {actor.display_name}; versión "
                f"{updated.version}. Sigue vigente el seguro del host: sin sus variables de "
                "entorno y --live el worker no contacta a X."
            )
        if action is ControlAction.MODE_DIRECT:
            updated = self._apply_mode(
                actor,
                _int_parameter(parameters, "expected_version"),
                AutomationMode.DIRECT,
            )
            return CommandResult(
                f"Modo configurado: {updated.mode}; versión {updated.version}; autorizado por "
                f"{actor.display_name}. Este cambio no publica contenido por sí solo."
            )
        if action is ControlAction.AGENDA_ENABLE:
            updated = self.store.update_automation_settings(
                actor_id=actor.id,
                expected_version=_int_parameter(parameters, "expected_version"),
                enabled=True,
            )
            return CommandResult(
                f"Agenda activada en modo {updated.mode}; versión {updated.version}. "
                "No se ejecutaron runs ni publicaciones."
            )
        if action is ControlAction.USER_DEACTIVATE:
            user_id = _str_parameter(parameters, "user_id")
            _membership, user = self._delegable_member(actor, user_id)
            updated_user = self.store.set_user_active(user.id, active=False, actor_id=actor.id)
            return CommandResult(
                f"{updated_user.display_name} quedó inactivo (user_id={updated_user.id}); sus "
                "vínculos de Telegram y sesiones web dejan de valer."
            )
        if action in {ControlAction.RECONCILE_PUBLISHED, ControlAction.RECONCILE_NOT_PUBLISHED}:
            published = action is ControlAction.RECONCILE_PUBLISHED
            request = self.store.reconcile_publication_request(
                _str_parameter(parameters, "request_id"),
                actor_id=actor.id,
                published=published,
                provider_post_id=(
                    _str_parameter(parameters, "provider_post_id") if published else None
                ),
                note=(f"Verificado manualmente en X desde Telegram por {actor.display_name}."),
            )
            outcome = "publicada" if published else "no publicada"
            return CommandResult(
                f"Solicitud {request.id} conciliada como {outcome}; estado final {request.status}."
            )
        raise ValueError(f"Acción de control desconocida: {action!r}")

    # ------------------------------------------------------------------
    # Auxiliares
    # ------------------------------------------------------------------

    def _actor(self, telegram_user_id: int, chat_id: int) -> Any:
        return self.store.resolve_telegram_actor(
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
        )

    def _confirm(
        self,
        actor: Any,
        action: ControlAction,
        parameters: Mapping[str, Any],
        text: str,
        *,
        telegram_user_id: int,
        chat_id: int,
    ) -> CommandResult:
        now = self._now()
        try:
            issued = self.store.issue_control_confirmation(
                actor_id=actor.id,
                action=action.value,
                parameters=parameters,
                telegram_user_id=telegram_user_id,
                chat_id=chat_id,
                expires_at=now + CONFIRMATION_TTL,
                now=now,
            )
        except _SAFE_ERRORS:
            return CommandResult(
                "No se pudo preparar la confirmación: usa tu chat privado y resuelve las "
                "confirmaciones pendientes antes de emitir otra.",
                accepted=False,
            )
        return CommandResult(
            f"{text}\n\nConfirma con el botón en los próximos 10 minutos. El botón es de un "
            "solo uso y solo vale para ti en este chat.",
            keyboard=confirmation_keyboard(action, issued.nonce),
        )

    def _apply_mode(self, actor: Any, expected_version: int, mode: AutomationMode) -> Any:
        current = self.store.get_automation_settings(actor_id=actor.id)
        if current.version != expected_version:
            raise ConflictError("La configuración cambió")
        slots = _automation_slots_for_mode(current.slots, mode)
        return self.store.update_automation_settings(
            actor_id=actor.id,
            expected_version=expected_version,
            mode=mode,
            slots=slots,
        )

    def _delegable_member(self, actor: Any, user_id: str) -> tuple[Any, Any]:
        membership = next(
            (
                item
                for item in self.store.list_memberships(actor_id=actor.id)
                if item.user_id == user_id
            ),
            None,
        )
        if membership is None:
            raise NotFoundError("El usuario no pertenece al equipo")
        if membership.role not in TELEGRAM_DELEGATABLE_ROLES:
            raise AuthorizationError("Owners y admins solo se administran desde el host")
        return membership, self.store.get_user(user_id)

    def _last_publication_line(self, actor: Any, requests: list[Any]) -> str:
        succeeded = next(
            (
                item
                for item in requests
                if item.status == PublicationRequestStatus.SUCCEEDED.value
                and item.publish_attempt_id
            ),
            None,
        )
        if succeeded is None:
            return "• Última publicación confirmada: ninguna registrada."
        try:
            attempt = self.store.get_publish_attempt(
                succeeded.publish_attempt_id, actor_id=actor.id
            )
        except _SAFE_ERRORS:
            return "• Última publicación confirmada: intento no consultable."
        return (
            f"• Última publicación confirmada: {_local(attempt.finished_at)}; post "
            f"{attempt.provider_post_id}; draft {attempt.draft_id}."
        )

    def _load_schedule(self) -> Any:
        policy = load_editorial_policy(self._policy_path)
        return load_automation_config(self._automation_config_path, policy=policy)

    def _editorial_month(self) -> str:
        return editorial_month(self._now())


EDITORIAL_BRIEF_MAX_CHARACTERS = 1_000


def editorial_month(now: datetime) -> str:
    """Mes editorial en la fecha local de Bogotá, no en UTC."""

    return now.astimezone(_LOCAL_ZONE).strftime("%Y-%m")


def guided_brief(line: EditorialLine | None, brief: str) -> str:
    """Antepone la línea editorial vigente con su versión, como exige el manual.

    La comparte toda puerta de entrada a la cola de generación —Telegram y panel
    web—, para que ninguna encole un encargo sin la directriz del mes.
    """

    if line is None:
        raise ConflictError("No hay línea editorial mensual")
    guided = f"Línea editorial {line.month} v{line.version}: {line.line_text} Encargo: {brief}"
    if len(guided) > EDITORIAL_BRIEF_MAX_CHARACTERS:
        raise ValueError("La línea y el encargo superan el brief permitido")
    return guided


def _automation_slots_for_mode(
    slots: object,
    mode: AutomationMode,
) -> list[dict[str, object]]:
    if not isinstance(slots, list):
        raise ValueError("Los slots persistidos no tienen formato de lista")
    aligned: list[dict[str, object]] = []
    for index, raw_slot in enumerate(slots):
        if not isinstance(raw_slot, Mapping):
            raise ValueError(f"El slot persistido {index} no tiene formato de objeto")
        aligned.append({**raw_slot, "mode": mode.value})
    return aligned


def _research_reference_text(reference: Any) -> str:
    if reference.disputed:
        return (
            f"{reference.title} ({reference.year}) {reference.url}; atribución editorial "
            "disputada; Bueno negó participación: "
            f"{reference.status_url}"
        )
    return f"{reference.title} ({reference.year}) {reference.url}"


def _int_parameter(parameters: Mapping[str, Any], name: str) -> int:
    value = parameters.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"El parámetro {name} de la confirmación no es válido")
    return value


def _str_parameter(parameters: Mapping[str, Any], name: str) -> str:
    value = parameters.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > 120:
        raise ValueError(f"El parámetro {name} de la confirmación no es válido")
    return value


def _state(enabled: object) -> str:
    return "ABIERTO" if enabled else "cerrado"


def _signed(actor_id: object, moment: datetime | None) -> str:
    if not actor_id or moment is None:
        return ""
    return f" (por {actor_id} el {_local(moment)})"


def _local(value: datetime | None) -> str:
    if value is None:
        return "-"
    return value.astimezone(_LOCAL_ZONE).strftime("%Y-%m-%d %H:%M")


def _bounded(value: object, limit: int) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
