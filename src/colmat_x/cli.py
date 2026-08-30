from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

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
from colmat_x.minimax import MiniMaxClient, MiniMaxError
from colmat_x.platform_store import (
    Membership,
    PlatformStore,
    PlatformStoreError,
    User,
)
from colmat_x.rbac import AuthorizationError, Role, require_role_assignment
from colmat_x.service import Outcome, run_due_posts
from colmat_x.state import StateError, StateStore
from colmat_x.x_api import XApiClient, XApiError

app = typer.Typer(
    name="colmat-x",
    help="Planifica y publica contenido de Colmat en X con aprobación humana.",
    no_args_is_help=True,
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
                user_id=user_id,
            )
    except (PlatformStoreError, AuthorizationError, ValueError) as exc:
        _abort(exc)
    except (SQLAlchemyError, ImportError, OSError):
        _abort_database()
    typer.echo(
        f"Owner creado: id={user.id} | display={user.display_name} | "
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
            help="Rol: owner, admin, editor, reviewer, publisher o auditor.",
        ),
    ],
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
        f"Miembro creado: id={user.id} | display={user.display_name} | "
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
    typer.echo(f"{'ID':<36}  {'DISPLAY':<24}  {'EMAIL':<32}  {'ROLE':<10}  ACTIVO")
    for membership, user in rows:
        active = "sí" if user.is_active else "no"
        typer.echo(
            f"{user.id:<36}  {user.display_name:<24}  {user.email:<32}  "
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
