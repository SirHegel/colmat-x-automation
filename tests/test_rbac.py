from __future__ import annotations

import pytest

from colmat_x.rbac import (
    ASSIGNABLE_ROLES,
    ROLE_PERMISSIONS,
    AuthorizationError,
    Permission,
    Role,
    SeparationOfDutiesError,
    can_assign_role,
    has_permission,
    permissions_for,
    require_distinct_approver,
    require_permission,
    require_role_assignment,
    roles_with,
)


def test_every_role_has_an_explicit_immutable_permission_set() -> None:
    assert set(ROLE_PERMISSIONS) == set(Role)
    assert set(ASSIGNABLE_ROLES) == set(Role)
    assert all(isinstance(permissions_for(role), frozenset) for role in Role)
    assert permissions_for(Role.OWNER) == frozenset(Permission)


def test_editor_and_reviewer_permissions_are_separated() -> None:
    assert has_permission(Role.EDITOR, Permission.EDIT_DRAFTS)
    assert has_permission(Role.EDITOR, Permission.SUBMIT_DRAFTS)
    assert not has_permission(Role.EDITOR, Permission.REVIEW_DRAFTS)

    assert has_permission(Role.REVIEWER, Permission.REVIEW_DRAFTS)
    assert not has_permission(Role.REVIEWER, Permission.CREATE_DRAFTS)
    assert not has_permission(Role.REVIEWER, Permission.EDIT_DRAFTS)

    with pytest.raises(AuthorizationError, match="no tiene el permiso"):
        require_permission(Role.EDITOR, Permission.REVIEW_DRAFTS)


def test_publisher_and_auditor_have_narrow_responsibilities() -> None:
    assert roles_with(Permission.PUBLISH_DRAFTS) == {
        Role.OWNER,
        Role.ADMIN,
        Role.PUBLISHER,
    }
    assert has_permission(Role.AUDITOR, Permission.VIEW_AUDIT)
    assert not has_permission(Role.AUDITOR, Permission.PUBLISH_DRAFTS)
    assert not has_permission(Role.PUBLISHER, Permission.REVIEW_DRAFTS)


def test_scheduler_can_manage_calendar_but_not_mode_or_publication() -> None:
    assert has_permission(Role.SCHEDULER, Permission.VIEW_AUTOMATION)
    assert has_permission(Role.SCHEDULER, Permission.MANAGE_SCHEDULE)
    assert has_permission(Role.SCHEDULER, Permission.CREATE_DRAFTS)
    assert has_permission(Role.SCHEDULER, Permission.SUBMIT_DRAFTS)
    assert not has_permission(Role.SCHEDULER, Permission.MANAGE_AUTOMATION_MODE)
    assert not has_permission(Role.SCHEDULER, Permission.PUBLISH_DRAFTS)
    assert roles_with(Permission.MANAGE_AUTOMATION_MODE) == {Role.OWNER, Role.ADMIN}


def test_role_delegation_hierarchy_protects_privileged_roles() -> None:
    assert can_assign_role(Role.OWNER, Role.OWNER)
    assert can_assign_role(Role.OWNER, Role.ADMIN)
    assert can_assign_role(Role.ADMIN, Role.EDITOR)
    assert can_assign_role(Role.ADMIN, Role.REVIEWER)
    assert can_assign_role(Role.ADMIN, Role.SCHEDULER)
    assert not can_assign_role(Role.ADMIN, Role.ADMIN)
    assert not can_assign_role(Role.ADMIN, Role.OWNER)
    assert not can_assign_role(Role.EDITOR, Role.AUDITOR)

    with pytest.raises(AuthorizationError, match="no puede asignar"):
        require_role_assignment(Role.ADMIN, Role.ADMIN)


@pytest.mark.parametrize("role", [Role.OWNER, Role.ADMIN, Role.REVIEWER])
def test_no_role_can_approve_its_own_revision(role: Role) -> None:
    assert has_permission(role, Permission.REVIEW_DRAFTS)
    with pytest.raises(SeparationOfDutiesError, match="propia revisión"):
        require_distinct_approver(author_id="user-1", approver_id="user-1")


def test_unknown_roles_and_permissions_fail_closed() -> None:
    with pytest.raises(AuthorizationError, match="Rol desconocido"):
        permissions_for("superadmin")
    with pytest.raises(AuthorizationError, match="Permiso desconocido"):
        has_permission(Role.OWNER, "everything")
