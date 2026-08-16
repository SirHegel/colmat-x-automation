from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from colmat_x.config import ConfigError, XCredentials, live_enabled, load_settings
from colmat_x.content import ContentCollectionError, load_rendered_posts
from colmat_x.domain import ContentError, PostStatus, weighted_length
from colmat_x.service import Outcome, run_due_posts
from colmat_x.state import StateError, StateStore
from colmat_x.x_api import XApiClient

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


def _abort(error: Exception | str) -> None:
    typer.echo(f"Error: {error}", err=True)
    raise typer.Exit(code=1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
