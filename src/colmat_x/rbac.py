from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum
from types import MappingProxyType


class AuthorizationError(PermissionError):
    """El actor no tiene autorización para ejecutar la operación."""


class SeparationOfDutiesError(AuthorizationError):
    """La operación rompería la separación entre autoría y aprobación."""


class Role(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    EDITOR = "editor"
    REVIEWER = "reviewer"
    PUBLISHER = "publisher"
    SCHEDULER = "scheduler"
    AUDITOR = "auditor"


class Permission(StrEnum):
    VIEW_WORKSPACE = "workspace:view"
    MANAGE_WORKSPACE = "workspace:manage"
    MANAGE_USERS = "users:manage"
    MANAGE_MEMBERSHIPS = "memberships:manage"
    MANAGE_INTEGRATIONS = "integrations:manage"
    VIEW_DRAFTS = "drafts:view"
    CREATE_DRAFTS = "drafts:create"
    EDIT_DRAFTS = "drafts:edit"
    SUBMIT_DRAFTS = "drafts:submit"
    REVIEW_DRAFTS = "drafts:review"
    PUBLISH_DRAFTS = "drafts:publish"
    MANAGE_MEDIA = "media:manage"
    VIEW_AUTOMATION = "automation:view"
    MANAGE_SCHEDULE = "automation:schedule:manage"
    MANAGE_AUTOMATION_MODE = "automation:mode:manage"
    VIEW_TELEGRAM = "telegram:view"
    MANAGE_TELEGRAM = "telegram:manage"
    VIEW_AUDIT = "audit:view"


_ALL_PERMISSIONS = frozenset(Permission)

# La tabla es deliberadamente explícita. No se heredan permisos por un número de
# jerarquía: editor, reviewer, publisher y auditor son funciones distintas, no
# versiones "menores" de una misma función.
ROLE_PERMISSIONS: Mapping[Role, frozenset[Permission]] = MappingProxyType(
    {
        Role.OWNER: _ALL_PERMISSIONS,
        Role.ADMIN: _ALL_PERMISSIONS,
        Role.EDITOR: frozenset(
            {
                Permission.VIEW_WORKSPACE,
                Permission.VIEW_DRAFTS,
                Permission.CREATE_DRAFTS,
                Permission.EDIT_DRAFTS,
                Permission.SUBMIT_DRAFTS,
                Permission.MANAGE_MEDIA,
                Permission.VIEW_AUTOMATION,
                Permission.VIEW_TELEGRAM,
            }
        ),
        Role.REVIEWER: frozenset(
            {
                Permission.VIEW_WORKSPACE,
                Permission.VIEW_DRAFTS,
                Permission.REVIEW_DRAFTS,
                Permission.VIEW_AUTOMATION,
                Permission.VIEW_TELEGRAM,
            }
        ),
        Role.PUBLISHER: frozenset(
            {
                Permission.VIEW_WORKSPACE,
                Permission.VIEW_DRAFTS,
                Permission.PUBLISH_DRAFTS,
                Permission.VIEW_AUTOMATION,
                Permission.VIEW_TELEGRAM,
            }
        ),
        Role.SCHEDULER: frozenset(
            {
                Permission.VIEW_WORKSPACE,
                Permission.VIEW_DRAFTS,
                Permission.CREATE_DRAFTS,
                Permission.EDIT_DRAFTS,
                Permission.SUBMIT_DRAFTS,
                Permission.MANAGE_MEDIA,
                Permission.VIEW_AUTOMATION,
                Permission.MANAGE_SCHEDULE,
                Permission.VIEW_TELEGRAM,
            }
        ),
        Role.AUDITOR: frozenset(
            {
                Permission.VIEW_WORKSPACE,
                Permission.VIEW_DRAFTS,
                Permission.VIEW_AUTOMATION,
                Permission.VIEW_TELEGRAM,
                Permission.VIEW_AUDIT,
            }
        ),
    }
)

# La jerarquía solo determina qué roles se pueden delegar. Los permisos de trabajo
# siguen saliendo exclusivamente de ROLE_PERMISSIONS.
ASSIGNABLE_ROLES: Mapping[Role, frozenset[Role]] = MappingProxyType(
    {
        Role.OWNER: frozenset(Role),
        Role.ADMIN: frozenset(
            {Role.EDITOR, Role.REVIEWER, Role.PUBLISHER, Role.SCHEDULER, Role.AUDITOR}
        ),
        Role.EDITOR: frozenset(),
        Role.REVIEWER: frozenset(),
        Role.PUBLISHER: frozenset(),
        Role.SCHEDULER: frozenset(),
        Role.AUDITOR: frozenset(),
    }
)


def permissions_for(role: Role | str) -> frozenset[Permission]:
    """Devuelve una vista inmutable de los permisos efectivos del rol."""

    return ROLE_PERMISSIONS[_coerce_role(role)]


def has_permission(role: Role | str, permission: Permission | str) -> bool:
    return _coerce_permission(permission) in permissions_for(role)


def require_permission(role: Role | str, permission: Permission | str) -> None:
    normalized_role = _coerce_role(role)
    normalized_permission = _coerce_permission(permission)
    if normalized_permission not in ROLE_PERMISSIONS[normalized_role]:
        raise AuthorizationError(
            f"El rol '{normalized_role.value}' no tiene el permiso '{normalized_permission.value}'"
        )


def can_assign_role(actor_role: Role | str, target_role: Role | str) -> bool:
    return _coerce_role(target_role) in ASSIGNABLE_ROLES[_coerce_role(actor_role)]


def require_role_assignment(actor_role: Role | str, target_role: Role | str) -> None:
    normalized_actor = _coerce_role(actor_role)
    normalized_target = _coerce_role(target_role)
    require_permission(normalized_actor, Permission.MANAGE_MEMBERSHIPS)
    if normalized_target not in ASSIGNABLE_ROLES[normalized_actor]:
        raise AuthorizationError(
            f"El rol '{normalized_actor.value}' no puede asignar '{normalized_target.value}'"
        )


def require_distinct_approver(*, author_id: str, approver_id: str) -> None:
    """Impide que una persona apruebe o rechace su propia revisión."""

    normalized_author = _normalize_actor(author_id)
    normalized_approver = _normalize_actor(approver_id)
    if normalized_author == normalized_approver:
        raise SeparationOfDutiesError("El autor no puede aprobar ni rechazar su propia revisión")


def validate_role_permissions() -> None:
    """Comprueba invariantes del mapa; útil al arrancar y en pruebas."""

    if set(ROLE_PERMISSIONS) != set(Role):
        raise RuntimeError("Cada rol debe tener una definición explícita de permisos")
    if set(ASSIGNABLE_ROLES) != set(Role):
        raise RuntimeError("Cada rol debe tener una definición explícita de delegación")
    if Permission.REVIEW_DRAFTS in ROLE_PERMISSIONS[Role.EDITOR]:
        raise RuntimeError("El editor no puede recibir permisos de aprobación")
    if any(
        permission in ROLE_PERMISSIONS[Role.REVIEWER]
        for permission in (Permission.CREATE_DRAFTS, Permission.EDIT_DRAFTS)
    ):
        raise RuntimeError("El reviewer no puede recibir permisos de edición")


def _coerce_role(role: Role | str) -> Role:
    try:
        return role if isinstance(role, Role) else Role(role)
    except (TypeError, ValueError) as exc:
        raise AuthorizationError(f"Rol desconocido: {role!r}") from exc


def _coerce_permission(permission: Permission | str) -> Permission:
    try:
        return permission if isinstance(permission, Permission) else Permission(permission)
    except (TypeError, ValueError) as exc:
        raise AuthorizationError(f"Permiso desconocido: {permission!r}") from exc


def _normalize_actor(actor_id: str) -> str:
    if not isinstance(actor_id, str) or not actor_id.strip():
        raise AuthorizationError("El actor debe estar identificado")
    return actor_id.strip()


def roles_with(permission: Permission | str) -> frozenset[Role]:
    """Enumera roles autorizados sin exponer la tabla mutable internamente."""

    normalized_permission = _coerce_permission(permission)
    return frozenset(
        role
        for role, permissions in ROLE_PERMISSIONS.items()
        if normalized_permission in permissions
    )


def require_any_permission(role: Role | str, permissions: Iterable[Permission | str]) -> None:
    normalized_role = _coerce_role(role)
    normalized_permissions = tuple(_coerce_permission(item) for item in permissions)
    if not normalized_permissions or not any(
        permission in ROLE_PERMISSIONS[normalized_role] for permission in normalized_permissions
    ):
        names = ", ".join(permission.value for permission in normalized_permissions) or "ninguno"
        raise AuthorizationError(
            f"El rol '{normalized_role.value}' requiere al menos uno de: {names}"
        )


validate_role_permissions()
