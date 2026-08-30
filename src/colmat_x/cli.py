from __future__ import annotations

import json
import os
import stat
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from colmat_x.automation import (
    DEFAULT_AUTOMATION_CONFIG_PATH,
    AutomationConfigurationError,
    AutomationError,
    AutomationSlot,
    AutomationStatus,
    DailyAutomation,
    automation_slot_mapping,
    load_automation_config,
    parse_automation_config,
    slot_idempotency_key,
)
from colmat_x.automation_adapters import (
    AutomationReviewNotificationWorker,
    MiniMaxAutomationGenerator,
    PlatformAutomationRepository,
    PlatformXPublisher,
    ReviewNotificationDeliveryResult,
    ReviewNotificationDeliveryStatus,
    TelegramAutomationNotifier,
)
from colmat_x.config import ConfigError, XCredentials, live_enabled, load_settings
from colmat_x.content import ContentCollectionError, load_rendered_posts
from colmat_x.domain import ContentError, PostStatus, weighted_length
from colmat_x.editorial import (
    DEFAULT_POLICY_PATH,
    EditorialCategory,
    EditorialPolicyError,
    EditorialValidationError,
    Institution,
    assess_engagement,
    load_editorial_policy,
    validate_ai_draft,
)
from colmat_x.generation_worker import (
    GENERATION_ENABLED_ENV,
    QueuedGenerationWorker,
    QueueGenerationStatus,
)
from colmat_x.media_paths import configured_worker_media_root
from colmat_x.minimax import MiniMaxClient, MiniMaxError
from colmat_x.platform_store import AutomationMode as StoredAutomationMode
from colmat_x.platform_store import (
    ConflictError,
    Membership,
    PlatformStore,
    PlatformStoreError,
    User,
)
from colmat_x.publication_worker import QueuedPublicationWorker, QueuePublicationStatus
from colmat_x.rbac import AuthorizationError, Role, require_role_assignment
from colmat_x.service import Outcome, run_due_posts
from colmat_x.state import StateError, StateStore
from colmat_x.telegram_api import (
    BotCommand,
    TelegramApiClient,
    TelegramApiError,
    TelegramConfigurationError,
    TelegramCredentials,
    TelegramProtocolError,
)
from colmat_x.x_api import XApiClient, XApiError

app = typer.Typer(
    name="colmat-x",
    help=("Planifica contenido de Colmat en X y opera su scheduler seguro con aprobación humana."),
    no_args_is_help=True,
)

AUTOMATION_RUN_RECONCILIATION_GRACE = timedelta(minutes=30)
TELEGRAM_CONTROL_COMMANDS = (
    BotCommand("estado", "Consultar estado editorial y automatización"),
    BotCommand("equipo", "Ver usuarios y roles del equipo"),
    BotCommand("calendario", "Consultar la agenda de publicaciones"),
    BotCommand("modo", "Ver o cambiar revisión humana/directo"),
    BotCommand("generar", "Crear un borrador con MiniMax"),
    BotCommand("publicar", "Encolar un borrador aprobado"),
    BotCommand("ayuda", "Mostrar comandos y controles"),
)

ConfigOption = Annotated[
    Path | None,
    typer.Option(
        "--config",
        "-c",
        help="Archivo de configuración (por defecto config/colmat.yaml).",
    ),
]

DatabaseUrlOption = Annotated[
    str | None,
    typer.Option(
        "--database-url",
        envvar="DATABASE_URL",
        metavar="URL",
        help="Base de plataforma; también puede definirse con DATABASE_URL.",
    ),
]

PolicyOption = Annotated[
    Path,
    typer.Option(
        "--policy",
        help="Política editorial que debe validar el borrador.",
    ),
]

AutomationConfigOption = Annotated[
    Path,
    typer.Option(
        "--automation-config",
        help="Programación semanal (por defecto config/automation.yaml).",
    ),
]


@app.command()
def validate(config: ConfigOption = None) -> None:
    """Valida todos los contenidos y plantillas, sin crear estado."""
    try:
        settings = load_settings(config)
        posts = load_rendered_posts(settings)
    except (ConfigError, ContentError) as exc:
        _abort(exc)
    typer.echo(f"OK: {len(posts)} contenido(s) fuente válidos.")


@app.command()
def preview(
    config: ConfigOption = None,
    post_id: Annotated[str | None, typer.Option("--id", help="Muestra únicamente este ID.")] = None,
) -> None:
    """Muestra el texto final y la hora local sin tocar la cola."""
    try:
        settings = load_settings(config)
        posts = load_rendered_posts(settings)
    except (ConfigError, ContentError) as exc:
        _abort(exc)
    selected = [post for post in posts if post_id is None or post.id == post_id]
    if not selected:
        _abort(f"No se encontró la publicación '{post_id}'")
    for index, post in enumerate(selected):
        if index:
            typer.echo()
        local_time = post.publish_at.astimezone(settings.brand.zoneinfo)
        typer.echo(f"[{post.id}] {local_time.isoformat()}")
        typer.echo(
            f"plantilla: {post.template} | peso: {weighted_length(post.text)}/"
            f"{settings.safety.max_weighted_length}"
        )
        typer.echo(f"snapshot de aprobación: {post.approval_snapshot_hash}")
        typer.echo("─" * 60)
        typer.echo(post.text)
        typer.echo("─" * 60)


@app.command()
def sync(config: ConfigOption = None) -> None:
    """Sincroniza los YAML válidos con la cola local SQLite."""
    try:
        settings = load_settings(config)
        posts = load_rendered_posts(settings)
        store = StateStore(settings.paths.state_db)
        summary = store.sync_posts(posts)
    except (ConfigError, ContentError, StateError) as exc:
        _abort(exc)
    typer.echo(
        "Cola sincronizada: "
        f"{summary.inserted} nuevas, {summary.updated} actualizadas, "
        f"{summary.unchanged} sin cambios, {summary.protected} protegidas, "
        f"{summary.cancelled} canceladas por archivo ausente."
    )


@app.command()
def approve(
    post_id: Annotated[str, typer.Argument(help="ID local que se va a aprobar.")],
    reviewer: Annotated[
        str, typer.Option("--by", help="Persona o equipo responsable de la aprobación.")
    ],
    snapshot: Annotated[
        str,
        typer.Option(
            "--snapshot",
            help="Hash completo mostrado por preview para confirmar el texto revisado.",
        ),
    ],
    config: ConfigOption = None,
) -> None:
    """Aprueba el snapshot actual; cualquier cambio posterior invalida la aprobación."""
    try:
        settings = load_settings(config)
        posts = load_rendered_posts(settings)
        selected = next((post for post in posts if post.id == post_id), None)
        if selected is None:
            raise StateError(f"No existe un YAML vigente para '{post_id}'")
        snapshot = snapshot.strip().casefold()
        if snapshot != selected.approval_snapshot_hash:
            raise StateError("El hash no coincide; vuelve a ejecutar preview y revisa el texto")
        store = StateStore(settings.paths.state_db)
        store.sync_posts(posts)
        approval_hash = store.approve(post_id, reviewer, snapshot)
        queued = next(post for post in store.list_posts() if post.id == post_id)
    except (ConfigError, ContentError, StateError) as exc:
        _abort(exc)
    typer.echo(f"'{post_id}' aprobado por {queued.approved_by}; snapshot={approval_hash[:12]}…")
    if queued.scheduled_at_utc <= datetime.now(UTC):
        typer.echo("Aviso: la hora ya venció; quedará listo para el siguiente run-due.")


@app.command()
def withdraw(
    post_id: Annotated[str, typer.Argument(help="ID cuya aprobación se retirará.")],
    config: ConfigOption = None,
) -> None:
    """Retira una aprobación antes de que se publique."""
    try:
        settings = load_settings(config)
        StateStore(settings.paths.state_db).withdraw_approval(post_id)
    except (ConfigError, StateError) as exc:
        _abort(exc)
    typer.echo(f"Aprobación retirada; '{post_id}' volvió a draft.")


@app.command()
def restore(
    post_id: Annotated[str, typer.Argument(help="ID cancelado que volverá a draft.")],
    config: ConfigOption = None,
) -> None:
    """Restaura como borrador un YAML que se eliminó y luego se repuso."""
    try:
        settings = load_settings(config)
        posts = load_rendered_posts(settings)
        if post_id not in {post.id for post in posts}:
            raise StateError(f"No existe un YAML vigente para '{post_id}'")
        selected = next(post for post in posts if post.id == post_id)
        store = StateStore(settings.paths.state_db)
        store.restore_cancelled(selected)
    except (ConfigError, ContentError, StateError) as exc:
        _abort(exc)
    typer.echo(f"'{post_id}' volvió a draft; revísalo y apruébalo de nuevo.")


@app.command("run-due")
def run_due(
    config: ConfigOption = None,
    live: Annotated[
        bool,
        typer.Option(
            "--live",
            help="Publica realmente. Sin esta opción siempre es una simulación.",
        ),
    ] = False,
    limit: Annotated[
        int | None,
        typer.Option(help="Tope para esta ejecución; nunca supera el tope configurado."),
    ] = None,
) -> None:
    """Procesa publicaciones vencidas; el modo predeterminado solo simula."""
    try:
        settings = load_settings(config)
        store = StateStore(settings.paths.state_db)
        recovered = store.recover_expired_leases()
        if recovered:
            typer.echo(
                f"Error: {recovered} publicación(es) pasaron a unknown; "
                "concílialas manualmente antes de continuar.",
                err=True,
            )
            raise typer.Exit(code=2)
        posts = load_rendered_posts(settings)
        store.sync_posts(posts)

        publisher = None
        if live:
            if not live_enabled():
                raise ConfigError(
                    "Publicación bloqueada: define COLMAT_LIVE_ENABLED=true y conserva --live"
                )
            publisher = XApiClient(XCredentials.from_environment())

        results = run_due_posts(
            store,
            settings,
            live=live,
            publisher=publisher,
            limit=limit,
        )
    except (ConfigError, ContentCollectionError, StateError, ValueError) as exc:
        _abort(exc)

    if not results:
        typer.echo("No hay publicaciones aprobadas y vencidas.")
        return

    failed = False
    for result in results:
        label = result.post_id or "cola"
        if result.outcome == Outcome.DRY_RUN:
            typer.echo(f"SIMULACIÓN [{label}] — no se llamó a X")
            typer.echo(result.detail)
        elif result.outcome == Outcome.PUBLISHED:
            typer.echo(f"PUBLICADA [{label}] x_post_id={result.x_post_id}")
        elif result.outcome in {Outcome.BUSY, Outcome.DAILY_LIMIT}:
            typer.echo(f"AVISO [{label}] {result.detail}")
        else:
            failed = True
            typer.echo(f"{result.outcome.value.upper()} [{label}] {result.detail}", err=True)
    if failed:
        raise typer.Exit(code=2)


@app.command()
def status(
    config: ConfigOption = None,
    state: Annotated[
        str | None,
        typer.Option("--state", help="Filtra: draft, scheduled, published, etc."),
    ] = None,
) -> None:
    """Lista el estado local de la cola."""
    try:
        settings = load_settings(config)
        wanted = PostStatus(state) if state else None
        store = StateStore(settings.paths.state_db)
        posts = store.list_posts(wanted)
    except (ConfigError, StateError, ValueError) as exc:
        _abort(exc)
    if not posts:
        typer.echo("La cola no contiene publicaciones para ese filtro.")
        return
    typer.echo(f"{'ID':<30} {'ESTADO':<11} {'HORA LOCAL':<25} {'APROBÓ':<18} X ID")
    for post in posts:
        local_time = post.scheduled_at_utc.astimezone(settings.brand.zoneinfo).isoformat()
        typer.echo(
            f"{post.id:<30} {post.status.value:<11} {local_time:<25} "
            f"{(post.approved_by or '-'):<18} {post.x_post_id or '-'}"
        )
        if post.last_error:
            typer.echo(f"  error: {post.last_error}")


@app.command()
def retry(
    post_id: Annotated[str, typer.Argument(help="ID local que se va a reencolar.")],
    config: ConfigOption = None,
    confirm_not_published: Annotated[
        bool,
        typer.Option(
            "--confirm-not-published",
            help="Permite reintentar un estado unknown tras comprobar manualmente X.",
        ),
    ] = False,
) -> None:
    """Reencola un fallo confirmado; unknown exige confirmación adicional."""
    try:
        settings = load_settings(config)
        target = StateStore(settings.paths.state_db).retry(
            post_id, confirm_not_published=confirm_not_published
        )
    except (ConfigError, StateError) as exc:
        _abort(exc)
    typer.echo(f"'{post_id}' quedó en {target.value}.")


@app.command("reconcile-published")
def reconcile_published(
    post_id: Annotated[str, typer.Argument(help="ID local en estado unknown.")],
    x_post_id: Annotated[str, typer.Argument(help="ID comprobado en X.")],
    config: ConfigOption = None,
) -> None:
    """Marca un resultado unknown como publicado después de verificarlo en X."""
    try:
        settings = load_settings(config)
        StateStore(settings.paths.state_db).reconcile_as_published(post_id, x_post_id)
    except (ConfigError, StateError) as exc:
        _abort(exc)
    typer.echo(f"'{post_id}' quedó conciliado con x_post_id={x_post_id}.")


@app.command()
def doctor(
    config: ConfigOption = None,
    credentials: Annotated[
        bool,
        typer.Option(
            "--credentials",
            help="Exige los cuatro secretos aunque la publicación real siga desactivada.",
        ),
    ] = False,
) -> None:
    """Comprueba archivos, cola y opcionalmente presencia de credenciales."""
    try:
        settings = load_settings(config)
        posts = load_rendered_posts(settings)
        StateStore(settings.paths.state_db)
        mode = "simulación"
        enabled = live_enabled()
        if credentials or enabled:
            XCredentials.from_environment()
            mode = (
                "publicación real habilitada"
                if enabled
                else "credenciales presentes; publicación real deshabilitada"
            )
    except (ConfigError, ContentError, StateError) as exc:
        _abort(exc)
    typer.echo(f"OK: configuración, {len(posts)} contenido(s) y base local; modo: {mode}.")


@app.command("team-bootstrap")
def team_bootstrap(
    email: Annotated[str, typer.Option("--email", help="Email de la cuenta owner.")],
    display_name: Annotated[
        str,
        typer.Option("--display", help="Nombre visible de la cuenta owner."),
    ],
    username: Annotated[
        str | None,
        typer.Option("--username", help="Username único opcional, sin @."),
    ] = None,
    user_id: Annotated[
        str | None,
        typer.Option("--user-id", help="ID estable opcional para la cuenta owner."),
    ] = None,
    database_url: DatabaseUrlOption = None,
) -> None:
    """Crea el primer owner; solo funciona en una base de plataforma vacía."""

    try:
        with PlatformStore(database_url) as store:
            _require_empty_platform(store)
            user, membership = store.bootstrap_owner(
                email=email,
                display_name=display_name,
                username=username,
                user_id=user_id,
            )
    except (PlatformStoreError, AuthorizationError, ValueError) as exc:
        _abort(exc)
    except (SQLAlchemyError, ImportError, OSError):
        _abort_database()
    typer.echo(
        f"Owner creado: id={user.id} | username={_display_username(user.username)} | "
        f"display={user.display_name} | "
        f"email={user.email} | role={membership.role} | activo=sí"
    )


@app.command("team-add")
def team_add(
    actor_id: Annotated[
        str,
        typer.Option("--actor-id", help="ID del owner o admin que realiza el alta."),
    ],
    email: Annotated[str, typer.Option("--email", help="Email de la nueva cuenta.")],
    display_name: Annotated[
        str,
        typer.Option("--display", help="Nombre visible de la nueva cuenta."),
    ],
    role: Annotated[
        str,
        typer.Option(
            "--role",
            help="Rol: owner, admin, editor, reviewer, publisher, scheduler o auditor.",
        ),
    ],
    username: Annotated[
        str | None,
        typer.Option("--username", help="Username único opcional, sin @."),
    ] = None,
    user_id: Annotated[
        str | None,
        typer.Option("--user-id", help="ID estable opcional para la cuenta."),
    ] = None,
    database_url: DatabaseUrlOption = None,
) -> None:
    """Crea una cuenta y le concede un rol con compensación si el alta queda incompleta."""

    try:
        target_role = Role(role.strip().casefold())
        with PlatformStore(database_url) as store:
            actor_membership = next(
                (
                    item
                    for item in store.list_memberships(actor_id=actor_id)
                    if item.user_id == actor_id
                ),
                None,
            )
            if actor_membership is None:
                raise AuthorizationError("El actor no pertenece al equipo activo")
            require_role_assignment(Role(actor_membership.role), target_role)

            user = store.create_user(
                actor_id=actor_id,
                email=email,
                display_name=display_name,
                username=username,
                user_id=user_id,
            )
            try:
                membership = store.grant_membership(
                    user.id,
                    target_role,
                    actor_id=actor_id,
                )
            except (PlatformStoreError, AuthorizationError, ValueError, SQLAlchemyError) as exc:
                if not _cleanup_unassigned_user(store, user.id):
                    raise PlatformStoreError(
                        "No se concedió el rol y la cuenta incompleta requiere revisión manual"
                    ) from exc
                raise
    except (PlatformStoreError, AuthorizationError, ValueError) as exc:
        _abort(exc)
    except (SQLAlchemyError, ImportError, OSError):
        _abort_database()
    typer.echo(
        f"Miembro creado: id={user.id} | username={_display_username(user.username)} | "
        f"display={user.display_name} | "
        f"email={user.email} | role={membership.role} | activo=sí"
    )


@app.command("team-list")
def team_list(
    actor_id: Annotated[
        str,
        typer.Option("--actor-id", help="ID de un miembro autorizado del equipo."),
    ],
    database_url: DatabaseUrlOption = None,
) -> None:
    """Lista cuentas, roles y estado activo del equipo."""

    try:
        with PlatformStore(database_url) as store:
            memberships = store.list_memberships(actor_id=actor_id)
            rows = [(membership, store.get_user(membership.user_id)) for membership in memberships]
    except (PlatformStoreError, AuthorizationError, ValueError) as exc:
        _abort(exc)
    except (SQLAlchemyError, ImportError, OSError):
        _abort_database()
    if not rows:
        typer.echo("El equipo no tiene miembros.")
        return
    typer.echo(
        f"{'ID':<36}  {'USERNAME':<20}  {'DISPLAY':<24}  {'EMAIL':<32}  {'ROLE':<10}  ACTIVO"
    )
    for membership, user in rows:
        active = "sí" if user.is_active else "no"
        typer.echo(
            f"{user.id:<36}  {_display_username(user.username):<20}  "
            f"{user.display_name:<24}  {user.email:<32}  "
            f"{membership.role:<10}  {active}"
        )


@app.command("telegram-bind")
def telegram_bind(
    actor_id: Annotated[
        str,
        typer.Option("--actor-id", help="ID del owner o admin que autoriza el vínculo."),
    ],
    user_id: Annotated[
        str,
        typer.Option("--user-id", help="ID de la cuenta de plataforma que se vincula."),
    ],
    telegram_user_id: Annotated[
        int,
        typer.Option("--telegram-user-id", help="Valor numérico from.id de Telegram."),
    ],
    chat_id: Annotated[
        int,
        typer.Option("--chat-id", help="ID numérico del chat autorizado."),
    ],
    purpose: Annotated[
        str,
        typer.Option("--purpose", help="Uso del chat: control, review o alerts."),
    ] = "control",
    database_url: DatabaseUrlOption = None,
) -> None:
    """Vincula una identidad de Telegram a una cuenta y un chat concretos."""

    try:
        with PlatformStore(database_url) as store:
            binding = store.bind_telegram_chat(
                chat_id,
                telegram_user_id=telegram_user_id,
                actor_id=actor_id,
                user_id=user_id,
                purpose=purpose.strip().casefold(),
            )
    except (PlatformStoreError, AuthorizationError, ValueError) as exc:
        _abort(exc)
    except (SQLAlchemyError, ImportError, OSError):
        _abort_database()
    typer.echo(
        f"Telegram vinculado: user_id={binding.user_id} | "
        f"telegram_user_id={binding.telegram_user_id} | chat_id={binding.chat_id} | "
        f"purpose={binding.purpose} | activo={'sí' if binding.is_active else 'no'}"
    )


@app.command("x-whoami")
def x_whoami(
    config: ConfigOption = None,
    expected_username: Annotated[
        str | None,
        typer.Option(
            "--expected-username",
            help="Usuario institucional esperado, con o sin @.",
        ),
    ] = None,
    expected_user_id: Annotated[
        str | None,
        typer.Option("--expected-user-id", help="ID numérico institucional esperado."),
    ] = None,
) -> None:
    """Verifica la cuenta autenticada en X sin revelar credenciales."""

    try:
        load_settings(config)
        credentials = XCredentials.from_environment()
        expected_username = _first_environment_value(
            expected_username,
            "EXPECTED_X_USERNAME",
            "X_EXPECTED_USERNAME",
        )
        expected_user_id = _first_environment_value(
            expected_user_id,
            "EXPECTED_X_USER_ID",
            "X_EXPECTED_USER_ID",
        )
        identity = XApiClient(credentials).verify_identity(
            expected_username=expected_username,
            expected_user_id=expected_user_id,
        )
    except (ConfigError, XApiError, ValueError) as exc:
        _abort(exc)
    typer.echo(
        json.dumps(
            {"id": identity.id, "username": identity.username, "name": identity.name},
            ensure_ascii=False,
            sort_keys=True,
        )
    )


@app.command("ai-draft")
def ai_draft(
    brief: Annotated[str, typer.Argument(help="Brief factual para el borrador editorial.")],
    policy_path: PolicyOption = DEFAULT_POLICY_PATH,
    category: Annotated[
        str | None,
        typer.Option(
            "--category",
            help="Categoría editorial canónica opcional.",
        ),
    ] = None,
    institution: Annotated[
        str | None,
        typer.Option(
            "--institution",
            help="Institución canónica opcional.",
        ),
    ] = None,
    config: ConfigOption = None,
) -> None:
    """Genera un borrador validado; nunca lo aprueba, programa ni publica."""

    try:
        settings = load_settings(config)
        resolved_policy = policy_path if policy_path.is_absolute() else settings.root / policy_path
        policy = load_editorial_policy(resolved_policy)
        requested_category = EditorialCategory(category) if category is not None else None
        requested_institution = Institution(institution) if institution is not None else None
        generated = MiniMaxClient().generate_draft(
            brief,
            policy,
            category=requested_category,
            institution=requested_institution,
        )
        validated = validate_ai_draft(generated.to_mapping(), policy)
        assessment = assess_engagement(validated)
    except (
        ConfigError,
        EditorialPolicyError,
        EditorialValidationError,
        MiniMaxError,
        ValueError,
    ) as exc:
        _abort(exc)

    payload = validated.to_mapping()
    payload.update(
        {
            "assessment": assessment.to_mapping(),
            "status": "draft",
            "publication_authorized": False,
            "requires_human_approval": True,
            "notice": "Borrador generado por IA; no se publicó ni se programó.",
        }
    )
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


@app.command("automation-validate")
def automation_validate(
    automation_config: AutomationConfigOption = DEFAULT_AUTOMATION_CONFIG_PATH,
    policy_path: PolicyOption = DEFAULT_POLICY_PATH,
) -> None:
    """Valida política y scheduler semanal sin crear runs ni publicar."""

    try:
        policy = load_editorial_policy(policy_path)
        schedule = load_automation_config(automation_config, policy=policy)
    except (AutomationConfigurationError, EditorialPolicyError, ValueError) as exc:
        _abort(exc)
    modes = ", ".join(sorted({slot.mode.value for slot in schedule.slots}))
    typer.echo(
        "OK: scheduler válido; "
        f"timezone={schedule.timezone} | slots={len(schedule.slots)} | "
        f"daily_limit={schedule.daily_limit} | modes={modes} | no se publicó nada"
    )


@app.command("automation-calendar")
def automation_calendar(
    automation_config: AutomationConfigOption = DEFAULT_AUTOMATION_CONFIG_PATH,
    policy_path: PolicyOption = DEFAULT_POLICY_PATH,
    days: Annotated[
        int,
        typer.Option("--days", help="Horizonte del calendario, entre 1 y 31 días."),
    ] = 7,
    start_date: Annotated[
        str | None,
        typer.Option(
            "--start-date",
            help="Fecha local inicial YYYY-MM-DD; por defecto hoy en Bogotá.",
        ),
    ] = None,
) -> None:
    """Proyecta próximos slots locales; es una vista y no ejecuta el scheduler."""

    try:
        if not 1 <= days <= 31:
            raise ValueError("days debe estar entre 1 y 31")
        policy = load_editorial_policy(policy_path)
        schedule = load_automation_config(automation_config, policy=policy)
        first_day = (
            _parse_calendar_date(start_date)
            if start_date is not None
            else datetime.now(schedule.zoneinfo).date()
        )
    except (AutomationConfigurationError, EditorialPolicyError, ValueError) as exc:
        _abort(exc)

    typer.echo(
        f"{'FECHA':<10}  {'HORA':<5}  {'SLOT':<20}  {'MODO':<12}  "
        f"{'CATEGORÍA':<20}  {'IMAGEN':<6}  IDEMPOTENCY KEY"
    )
    ordered_slots = sorted(schedule.slots, key=lambda item: (item.at, item.id))
    for offset in range(days):
        local_day = first_day + timedelta(days=offset)
        for item in ordered_slots:
            if not item.runs_on(local_day):
                continue
            key = slot_idempotency_key(local_day, item.id)
            typer.echo(
                f"{local_day.isoformat():<10}  {item.at.strftime('%H:%M'):<5}  "
                f"{item.id:<20}  {item.mode.value:<12}  {item.category.value:<20}  "
                f"{('sí' if item.generate_image else 'no'):<6}  {key}"
            )


@app.command("automation-status")
def automation_status(
    actor_id: Annotated[
        str,
        typer.Option("--actor-id", help="Miembro autorizado que consulta el scheduler."),
    ],
    database_url: DatabaseUrlOption = None,
    limit: Annotated[
        int,
        typer.Option("--limit", help="Cantidad máxima de runs recientes, entre 1 y 1000."),
    ] = 100,
) -> None:
    """Muestra settings persistidos y runs recientes del scheduler."""

    try:
        with PlatformStore(database_url) as store:
            settings = store.get_automation_settings(actor_id=actor_id)
            runs = store.list_automation_runs(actor_id=actor_id, limit=limit)
    except (PlatformStoreError, AuthorizationError, ValueError) as exc:
        _abort(exc)
    except (SQLAlchemyError, ImportError, OSError):
        _abort_database()

    payload = {
        "settings": {
            "enabled": settings.enabled,
            "mode": settings.mode,
            "timezone": settings.timezone,
            "slots": settings.slots,
            "generate_images": settings.generate_images,
            "min_engagement_score": settings.min_engagement_score,
            "max_posts_per_day": settings.max_posts_per_day,
            "version": settings.version,
            "direct_authorized": settings.direct_authorized_by is not None,
            "direct_authorized_at": _iso_or_none(settings.direct_authorized_at),
            "updated_at": settings.updated_at.isoformat(),
        },
        "runs": [
            {
                "id": run.id,
                "idempotency_key": run.idempotency_key,
                "slot_id": run.slot_id,
                "scheduled_for": run.scheduled_for.isoformat(),
                "mode": run.mode,
                "settings_version": run.settings_version,
                "slot_hash": run.slot_hash,
                "status": run.status,
                "draft_id": run.draft_id,
                "error": run.error,
                "claimed_at": run.claimed_at.isoformat(),
                "finished_at": _iso_or_none(run.finished_at),
            }
            for run in runs
        ],
    }
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


@app.command("automation-sync")
def automation_sync(
    actor_id: Annotated[
        str,
        typer.Option("--actor-id", help="Owner, admin o scheduler que sincroniza la agenda."),
    ],
    expected_version: Annotated[
        int,
        typer.Option("--expected-version", help="Versión CAS leída previamente."),
    ],
    automation_config: AutomationConfigOption = DEFAULT_AUTOMATION_CONFIG_PATH,
    policy_path: PolicyOption = DEFAULT_POLICY_PATH,
    database_url: DatabaseUrlOption = None,
) -> None:
    """Copia la agenda YAML validada a la plataforma; no la activa ni ejecuta."""

    try:
        policy = load_editorial_policy(policy_path)
        schedule = load_automation_config(automation_config, policy=policy)
        with PlatformStore(database_url) as store:
            current = store.get_automation_settings(actor_id=actor_id)
            _require_settings_version(current.version, expected_version)
            slots = _persisted_slots(schedule.slots, StoredAutomationMode(current.mode))
            updated = store.update_automation_settings(
                actor_id=actor_id,
                expected_version=expected_version,
                timezone=schedule.timezone,
                slots=slots,
                generate_images=any(slot.generate_image for slot in schedule.slots),
                min_engagement_score=schedule.direct_min_engagement_score,
                max_posts_per_day=schedule.daily_limit,
            )
    except (
        AutomationConfigurationError,
        EditorialPolicyError,
        PlatformStoreError,
        AuthorizationError,
        ValueError,
    ) as exc:
        _abort(exc)
    except (SQLAlchemyError, ImportError, OSError):
        _abort_database()
    typer.echo(
        f"Agenda sincronizada: slots={len(updated.slots)} | timezone={updated.timezone} | "
        f"version={updated.version} | enabled={'sí' if updated.enabled else 'no'}; "
        "no se ejecutaron runs ni publicaciones"
    )


@app.command("automation-mode")
def automation_mode(
    mode: Annotated[
        str,
        typer.Argument(help="Modo cerrado: human_review o direct."),
    ],
    actor_id: Annotated[
        str,
        typer.Option("--actor-id", help="Owner o admin que autoriza el cambio."),
    ],
    expected_version: Annotated[
        int,
        typer.Option("--expected-version", help="Versión CAS leída previamente."),
    ],
    database_url: DatabaseUrlOption = None,
) -> None:
    """Cambia el modo con CAS; direct conserva el kill switch del servidor."""

    try:
        requested_mode = StoredAutomationMode(mode.strip().casefold())
        with PlatformStore(database_url) as store:
            current = store.get_automation_settings(actor_id=actor_id)
            _require_settings_version(current.version, expected_version)
            changes: dict[str, object] = {"mode": requested_mode}
            aligned_slots = _slots_for_mode(current.slots, requested_mode)
            if aligned_slots != current.slots:
                changes["slots"] = aligned_slots
            updated = store.update_automation_settings(
                actor_id=actor_id,
                expected_version=expected_version,
                **changes,
            )
    except (PlatformStoreError, AuthorizationError, ValueError) as exc:
        _abort(exc)
    except (SQLAlchemyError, ImportError, OSError):
        _abort_database()
    typer.echo(
        f"Modo actualizado: mode={updated.mode} | version={updated.version} | "
        f"enabled={'sí' if updated.enabled else 'no'} | "
        f"direct_authorized={'sí' if updated.direct_authorized_by else 'no'}"
    )


@app.command("automation-enable")
def automation_enable(
    actor_id: Annotated[
        str,
        typer.Option("--actor-id", help="Owner, admin o scheduler que activa la agenda."),
    ],
    expected_version: Annotated[
        int,
        typer.Option("--expected-version", help="Versión CAS leída previamente."),
    ],
    database_url: DatabaseUrlOption = None,
) -> None:
    """Activa explícitamente la agenda con CAS; no ejecuta ni publica ningún slot."""

    try:
        with PlatformStore(database_url) as store:
            current = store.get_automation_settings(actor_id=actor_id)
            _require_settings_version(current.version, expected_version)
            updated = store.update_automation_settings(
                actor_id=actor_id,
                expected_version=expected_version,
                enabled=True,
            )
    except (PlatformStoreError, AuthorizationError, ValueError) as exc:
        _abort(exc)
    except (SQLAlchemyError, ImportError, OSError):
        _abort_database()
    typer.echo(
        f"Scheduler activado: enabled=sí | mode={updated.mode} | version={updated.version}; "
        "no se ejecutaron runs ni publicaciones"
    )


@app.command("automation-disable")
def automation_disable(
    actor_id: Annotated[
        str,
        typer.Option("--actor-id", help="Owner, admin o scheduler que pausa la agenda."),
    ],
    expected_version: Annotated[
        int,
        typer.Option("--expected-version", help="Versión CAS leída previamente."),
    ],
    database_url: DatabaseUrlOption = None,
) -> None:
    """Pausa nuevos claims con CAS; no altera runs ya terminales ni publica."""

    try:
        with PlatformStore(database_url) as store:
            current = store.get_automation_settings(actor_id=actor_id)
            _require_settings_version(current.version, expected_version)
            updated = store.update_automation_settings(
                actor_id=actor_id,
                expected_version=expected_version,
                enabled=False,
            )
    except (PlatformStoreError, AuthorizationError, ValueError) as exc:
        _abort(exc)
    except (SQLAlchemyError, ImportError, OSError):
        _abort_database()
    typer.echo(
        f"Scheduler pausado: enabled=no | mode={updated.mode} | version={updated.version}; "
        "no se ejecutaron runs ni publicaciones"
    )


@app.command("automation-run")
def automation_run(
    database_url: DatabaseUrlOption = None,
    policy_path: PolicyOption = DEFAULT_POLICY_PATH,
    env_file: Annotated[
        Path | None,
        typer.Option(
            "--env-file",
            help="Archivo local 0600, regular y propiedad del usuario del worker.",
        ),
    ] = None,
    live: Annotated[
        bool,
        typer.Option(
            "--live",
            help="Tercera confirmación requerida cuando el modo persistido es direct.",
        ),
    ] = False,
) -> None:
    """Ejecuta slots vencidos usando únicamente settings persistidos y actores de servicio."""

    try:
        _load_trusted_worker_environment(env_file)
        scheduler_actor_id = _required_worker_environment("COLMAT_AUTOMATION_SCHEDULER_ID")
        media_root = configured_worker_media_root(os.getenv("COLMAT_AUTOMATION_MEDIA_ROOT"))
        telegram: TelegramApiClient | None = None
        with PlatformStore(database_url) as store:
            reconciliation_now = datetime.now(UTC)
            store.reconcile_stale_automation_runs(
                actor_id=scheduler_actor_id,
                stale_before=reconciliation_now - AUTOMATION_RUN_RECONCILIATION_GRACE,
                now=reconciliation_now,
            )
            review_notifications = [
                ReviewNotificationDeliveryResult(
                    notification_id=item.id,
                    automation_run_id=item.automation_run_id,
                    status=ReviewNotificationDeliveryStatus.UNKNOWN,
                    detail=("La lease venció; la entrega requiere conciliación y no se reenviará."),
                )
                for item in store.expire_automation_review_notification_claims(
                    actor_id=scheduler_actor_id,
                    now=reconciliation_now,
                )
            ]
            if store.has_queued_automation_review_notifications(actor_id=scheduler_actor_id):
                telegram = TelegramApiClient(TelegramCredentials.from_environment())
                review_notifications.extend(
                    AutomationReviewNotificationWorker(
                        store=store,
                        telegram_client=telegram,
                        actor_id=scheduler_actor_id,
                        media_root=media_root,
                    ).drain()
                )
            settings = store.get_automation_settings(actor_id=scheduler_actor_id)
            if not settings.enabled:
                typer.echo(
                    json.dumps(
                        {
                            "enabled": False,
                            "mode": settings.mode,
                            "processed": 0,
                            "review_notifications": [
                                {
                                    "automation_run_id": item.automation_run_id,
                                    "detail": item.detail,
                                    "notification_id": item.notification_id,
                                    "status": item.status.value,
                                }
                                for item in review_notifications
                            ],
                            "settings_version": settings.version,
                            "status": "paused",
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
                if any(
                    item.status is not ReviewNotificationDeliveryStatus.SENT
                    for item in review_notifications
                ):
                    raise typer.Exit(code=1)
                return

            policy = load_editorial_policy(policy_path)
            schedule = parse_automation_config(
                {
                    "version": 1,
                    "timezone": settings.timezone,
                    "daily_limit": settings.max_posts_per_day,
                    "direct": {
                        "enabled": settings.mode == StoredAutomationMode.DIRECT.value,
                        "minimum_engagement_score": settings.min_engagement_score,
                    },
                    "slots": list(settings.slots or ()),
                },
                policy=policy,
            )
            if not schedule.slots:
                raise AutomationConfigurationError(
                    "La automatización activa requiere al menos un slot persistido"
                )
            direct_mode = settings.mode == StoredAutomationMode.DIRECT.value
            if direct_mode:
                _require_direct_worker_preflight(live=live)

            author_actor_id = _required_worker_environment("COLMAT_AUTOMATION_AUTHOR_ID")
            telegram_chat_id = _required_worker_environment("COLMAT_TELEGRAM_ALERT_CHAT_ID")
            reviewer_telegram_user_id = _required_worker_environment(
                "COLMAT_TELEGRAM_REVIEWER_USER_ID"
            )
            if telegram is None:
                telegram = TelegramApiClient(TelegramCredentials.from_environment())
            repository = PlatformAutomationRepository(
                store,
                scheduler_actor_id=scheduler_actor_id,
                author_actor_id=author_actor_id,
                media_root=media_root,
            )
            notifier = TelegramAutomationNotifier(
                telegram,
                chat_id=telegram_chat_id,
                store=store,
                repository=repository,
                reviewer_telegram_user_id=reviewer_telegram_user_id,
                actor_id=scheduler_actor_id,
            )
            generator = MiniMaxAutomationGenerator(MiniMaxClient(), policy=policy)

            publisher = None
            if direct_mode:
                reviewer_actor_id = _required_worker_environment("COLMAT_AUTOMATION_REVIEWER_ID")
                publisher_actor_id = _required_worker_environment("COLMAT_AUTOMATION_PUBLISHER_ID")
                x_client = XApiClient(XCredentials.from_environment())
                x_client.verify_identity(
                    expected_user_id=_required_worker_environment("EXPECTED_X_USER_ID"),
                    expected_username=_required_worker_environment("EXPECTED_X_USERNAME"),
                )
                publisher = PlatformXPublisher(
                    store=store,
                    repository=repository,
                    x_client=x_client,
                    reviewer_actor_id=reviewer_actor_id,
                    publisher_actor_id=publisher_actor_id,
                    environ=os.environ,
                )

            results = DailyAutomation(
                config=schedule,
                policy=policy,
                generator=generator,
                repository=repository,
                notifier=notifier,
                publisher=publisher,
            ).run_due(environ=os.environ, progress=_emit_automation_progress)
    except (
        AutomationConfigurationError,
        AuthorizationError,
        ConfigError,
        EditorialPolicyError,
        MiniMaxError,
        PlatformStoreError,
        SQLAlchemyError,
        TelegramApiError,
        TelegramConfigurationError,
        XApiError,
        OSError,
        ValueError,
    ) as exc:
        _abort(exc)

    payload = {
        "enabled": True,
        "mode": settings.mode,
        "processed": len(results),
        "review_notifications": [
            {
                "automation_run_id": item.automation_run_id,
                "detail": item.detail,
                "notification_id": item.notification_id,
                "status": item.status.value,
            }
            for item in review_notifications
        ],
        "settings_version": settings.version,
        "results": [
            {
                "detail": result.detail,
                "idempotency_key": result.idempotency_key,
                "media_generated": result.media_generated,
                "notification_delivered": result.notification_delivered,
                "scheduled_for": result.scheduled_for.isoformat(),
                "slot_id": result.slot_id,
                "status": result.status.value,
            }
            for result in results
        ],
    }
    typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    failed = {AutomationStatus.FAILED, AutomationStatus.UNKNOWN, AutomationStatus.DIRECT_BLOCKED}
    notification_required = {AutomationStatus.REVIEW_REQUIRED, AutomationStatus.PUBLISHED}
    if (
        any(
            item.status is not ReviewNotificationDeliveryStatus.SENT
            for item in review_notifications
        )
        or any(result.status in failed for result in results)
        or any(
            result.status in notification_required and not result.notification_delivered
            for result in results
        )
    ):
        raise typer.Exit(code=1)


@app.command("telegram-commands")
def telegram_commands(
    env_file: Annotated[
        Path | None,
        typer.Option(
            "--env-file",
            help="Archivo local 0600 que contiene TELEGRAM_BOT_TOKEN.",
        ),
    ] = None,
) -> None:
    """Registra y verifica el menú privado del bot sin mostrar su token."""

    scope = {"type": "all_private_chats"}
    desired = [command.as_payload() for command in TELEGRAM_CONTROL_COMMANDS]
    try:
        _load_trusted_worker_environment(env_file)
        client = TelegramApiClient(TelegramCredentials.from_environment())
        client.set_my_commands(TELEGRAM_CONTROL_COMMANDS, scope=scope)
        persisted = client.get_my_commands(scope=scope)
        if persisted != desired:
            raise TelegramProtocolError("Telegram no confirmó el menú esperado")
    except (
        ConfigError,
        OSError,
        TelegramApiError,
        TelegramConfigurationError,
        ValueError,
    ) as exc:
        _abort(exc)
    typer.echo(
        json.dumps(
            {"commands": [item["command"] for item in desired], "registered": True},
            ensure_ascii=False,
            sort_keys=True,
        )
    )


@app.command("publication-run")
def publication_run(
    database_url: DatabaseUrlOption = None,
    env_file: Annotated[
        Path | None,
        typer.Option(
            "--env-file",
            help="Archivo local 0600, regular y propiedad del usuario del worker.",
        ),
    ] = None,
    live: Annotated[
        bool,
        typer.Option("--live", help="Confirmación explícita exigida para contactar a X."),
    ] = False,
    limit: Annotated[
        int,
        typer.Option("--limit", help="Máximo de solicitudes por ejecución, entre 1 y 20."),
    ] = 5,
) -> None:
    """Consume la cola aprobada de Telegram con lease, fencing e idempotencia."""

    try:
        _load_trusted_worker_environment(env_file)
        _reject_publication_minimax_credentials()
        if not live:
            raise ConfigError("publication-run exige la opción --live")
        if os.getenv("COLMAT_LIVE_ENABLED", "").strip().casefold() != "true":
            raise ConfigError("publication-run exige COLMAT_LIVE_ENABLED=true exactamente")
        scheduler_actor_id = _required_worker_environment("COLMAT_AUTOMATION_SCHEDULER_ID")
        publisher_actor_id = _required_worker_environment("COLMAT_AUTOMATION_PUBLISHER_ID")
        x_client = XApiClient(XCredentials.from_environment())
        with PlatformStore(database_url) as store:
            results = QueuedPublicationWorker(
                store=store,
                x_client=x_client,
                publisher_actor_id=publisher_actor_id,
                scheduler_actor_id=scheduler_actor_id,
                media_root=configured_worker_media_root(os.getenv("COLMAT_AUTOMATION_MEDIA_ROOT")),
                environ=os.environ,
            ).run(limit=limit)
    except (
        AutomationError,
        AuthorizationError,
        ConfigError,
        PlatformStoreError,
        SQLAlchemyError,
        XApiError,
        OSError,
        ValueError,
    ) as exc:
        _abort(exc)

    typer.echo(
        json.dumps(
            {
                "processed": len(results),
                "results": [
                    {
                        "detail": result.detail,
                        "draft_id": result.draft_id,
                        "provider_post_id": result.provider_post_id,
                        "request_id": result.request_id,
                        "status": result.status.value,
                    }
                    for result in results
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    if any(result.status is not QueuePublicationStatus.SUCCEEDED for result in results):
        raise typer.Exit(code=1)


@app.command("generation-run")
def generation_run(
    database_url: DatabaseUrlOption = None,
    policy_path: PolicyOption = DEFAULT_POLICY_PATH,
    env_file: Annotated[
        Path | None,
        typer.Option(
            "--env-file",
            help="Archivo local 0600 del worker, deliberadamente sin credenciales de X.",
        ),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", help="Máximo de generaciones y entregas, entre 1 y 20."),
    ] = 5,
) -> None:
    """Asigna a MiniMax la cola de OpenClaw y entrega revisiones por Telegram."""

    try:
        _load_trusted_worker_environment(env_file)
        if os.getenv(GENERATION_ENABLED_ENV, "").strip().casefold() != "true":
            raise ConfigError(f"generation-run exige {GENERATION_ENABLED_ENV}=true exactamente")
        _reject_generation_x_credentials()
        worker_actor_id = _required_worker_environment("COLMAT_AUTOMATION_SCHEDULER_ID")
        author_actor_id = _required_worker_environment("COLMAT_AUTOMATION_AUTHOR_ID")
        policy = load_editorial_policy(policy_path)
        with PlatformStore(database_url) as store:
            results = QueuedGenerationWorker(
                store=store,
                minimax_client=MiniMaxClient(),
                telegram_client=TelegramApiClient(TelegramCredentials.from_environment()),
                policy=policy,
                worker_actor_id=worker_actor_id,
                author_actor_id=author_actor_id,
                media_root=configured_worker_media_root(os.getenv("COLMAT_GENERATION_MEDIA_ROOT")),
                environ=os.environ,
            ).run(limit=limit)
    except (
        AutomationError,
        AuthorizationError,
        ConfigError,
        EditorialPolicyError,
        MiniMaxError,
        PlatformStoreError,
        SQLAlchemyError,
        TelegramApiError,
        TelegramConfigurationError,
        OSError,
        ValueError,
    ) as exc:
        _abort(exc)

    typer.echo(
        json.dumps(
            {
                "processed": len(results),
                "results": [
                    {
                        "detail": result.detail,
                        "draft_id": result.draft_id,
                        "entity_id": result.entity_id,
                        "request_id": result.request_id,
                        "status": result.status.value,
                    }
                    for result in results
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    successful = {QueueGenerationStatus.GENERATED, QueueGenerationStatus.NOTIFIED}
    if any(result.status not in successful for result in results):
        raise typer.Exit(code=1)


def _display_username(username: str | None) -> str:
    return f"@{username}" if username else "-"


def _parse_calendar_date(value: str) -> date:
    normalized = value.strip()
    try:
        parsed = date.fromisoformat(normalized)
    except (TypeError, ValueError) as exc:
        raise ValueError("start-date debe usar YYYY-MM-DD") from exc
    if normalized != parsed.isoformat():
        raise ValueError("start-date debe usar YYYY-MM-DD")
    return parsed


def _iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _emit_automation_progress(event: str, slot_id: str, status: str | None) -> None:
    payload: dict[str, object] = {
        "event": f"automation.slot_{event}",
        "slot_id": slot_id,
    }
    if status is not None:
        payload["status"] = status
    typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _load_trusted_worker_environment(path: Path | None) -> None:
    if path is None:
        return
    candidate = path.expanduser()
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise ConfigError("No se pudo abrir el archivo de entorno del worker") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ConfigError("El archivo de entorno del worker debe ser regular, no un enlace")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise ConfigError("El archivo de entorno del worker pertenece a otro usuario")
    if metadata.st_mode & 0o077:
        raise ConfigError("El archivo de entorno del worker debe tener permisos 0600")
    if not load_dotenv(
        candidate.resolve(strict=True),
        override=True,
        interpolate=False,
    ):
        raise ConfigError("El archivo de entorno del worker está vacío o no pudo cargarse")


def _required_worker_environment(name: str) -> str:
    value = os.getenv(name, "")
    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized:
        raise ConfigError(f"Falta la configuración de servicio requerida: {name}")
    if len(normalized) > 256 or any(ord(character) < 32 for character in normalized):
        raise ConfigError(f"La configuración de servicio {name} no es válida")
    return normalized


def _reject_generation_x_credentials() -> None:
    forbidden = (
        "X_CONSUMER_KEY",
        "X_CONSUMER_SECRET",
        "X_ACCESS_TOKEN",
        "X_ACCESS_TOKEN_SECRET",
    )
    if any(os.getenv(name, "").strip() for name in forbidden):
        raise ConfigError(
            "generation-run rechaza credenciales de X; usa un entorno OpenClaw separado"
        )


def _reject_publication_minimax_credentials() -> None:
    if os.getenv("MINIMAX_API_KEY", "").strip():
        raise ConfigError(
            "publication-run rechaza credenciales de MiniMax; usa un entorno OpenClaw separado"
        )


def _require_direct_worker_preflight(*, live: bool) -> None:
    if not live:
        raise ConfigError("El modo direct exige además la opción --live del worker fijo")
    for name in ("COLMAT_LIVE_ENABLED", "COLMAT_DIRECT_PUBLISH_ENABLED"):
        if os.getenv(name, "").strip().casefold() != "true":
            raise ConfigError(f"El modo direct exige {name}=true exactamente")


def _require_settings_version(current: int, expected: int) -> None:
    if isinstance(expected, bool) or expected < 1:
        raise ValueError("expected-version debe ser un entero positivo")
    if current != expected:
        raise ConflictError(f"La configuración cambió (versión actual {current})")


def _slots_for_mode(slots: object, mode: StoredAutomationMode) -> list[dict[str, object]]:
    if not isinstance(slots, list):
        raise ValueError("Los slots persistidos no tienen formato de lista")
    aligned: list[dict[str, object]] = []
    for index, item in enumerate(slots):
        if not isinstance(item, dict):
            raise ValueError(f"El slot persistido {index} no tiene formato de objeto")
        copied = dict(item)
        if "mode" in copied:
            copied["mode"] = mode.value
        aligned.append(copied)
    return aligned


def _persisted_slots(
    slots: Sequence[AutomationSlot], mode: StoredAutomationMode
) -> list[dict[str, object]]:
    persisted: list[dict[str, object]] = []
    for item in slots:
        selected = automation_slot_mapping(item)
        selected["mode"] = mode.value
        persisted.append(selected)
    return persisted


def _require_empty_platform(store: PlatformStore) -> None:
    with store.session() as session:
        users = session.scalar(select(func.count(User.id))) or 0
        memberships = session.scalar(select(func.count(Membership.id))) or 0
    if users or memberships:
        raise PlatformStoreError(
            "team-bootstrap solo puede ejecutarse en una base de plataforma vacía"
        )


def _cleanup_unassigned_user(store: PlatformStore, user_id: str) -> bool:
    """Compensa el alta si la concesión de rol falla después de crear la cuenta."""

    try:
        with store.session() as session:
            has_membership = session.scalar(
                select(Membership.id).where(Membership.user_id == user_id).limit(1)
            )
            if has_membership is not None:
                return False
            user = session.get(User, user_id)
            if user is not None:
                session.delete(user)
                session.commit()
        return True
    except SQLAlchemyError:
        return False


def _first_environment_value(explicit: str | None, *names: str) -> str | None:
    if explicit is not None and explicit.strip():
        return explicit.strip()
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return None


def _abort_database() -> None:
    _abort("No se pudo abrir la base de plataforma; revisa DATABASE_URL y el controlador")


def _abort(error: Exception | str) -> None:
    typer.echo(f"Error: {error}", err=True)
    raise typer.Exit(code=1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
