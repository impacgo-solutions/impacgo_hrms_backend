"""Dynamic reporting-hierarchy resolver.

Nothing here is stored per-employee -- every call walks whichever
department/branch/company row the employee currently belongs to (plus their
active project allocation, for the Team Lead/Project Manager rungs), so
moving an employee's department/branch/project instantly changes what they
resolve to with zero extra writes. See backend/db/add_org_hierarchy_fields.sql
for the assigned-leader FK fields this reads, and rbac_columns.py for the
RBAC role strings the chain is keyed on.

Chain: Associate/Intern & Professional/IC Employee -> Team Lead ->
Project Manager -> Senior Manager/GM -> Branch Manager -> Branch Head.
"""

from dataclasses import dataclass
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import crud, models

TL_ROLE = "Team Lead"
PM_ROLE = "Project Manager"
SR_MGR_ROLE = "General Manager / Sr. Manager"
BR_MGR_ROLE = "Branch Manager"
BR_HEAD_ROLE = "Branch Head"
IC_ROLES = {"Professional / IC Employee", "Associate / Intern"}

# rung_key -> hardcoded built-in role name, used only as the fallback when a
# company has no core.hierarchy_role_bindings row for that rung (see
# resolve_rung_role_name). A company that never touches this feature keeps
# resolving on these exact names, unchanged from before this table existed.
_RUNG_DEFAULTS = {
    "team_lead": TL_ROLE,
    "project_manager": PM_ROLE,
    "senior_manager": SR_MGR_ROLE,
    "branch_manager": BR_MGR_ROLE,
    "branch_head": BR_HEAD_ROLE,
}


def resolve_rung_role_name(db: Session, company_id: uuid.UUID, rung_key: str) -> str:
    """The role name this company currently treats as the given hierarchy
    rung -- a company-configured core.hierarchy_role_bindings row if one
    exists, else the hardcoded built-in name. Never raises; an unknown
    rung_key falls back to itself (defensive, shouldn't happen since
    _RUNG_DEFAULTS' keys are the only ones ever passed in)."""
    binding = db.scalar(
        select(models.HierarchyRoleBinding)
        .where(
            models.HierarchyRoleBinding.company_id == company_id,
            models.HierarchyRoleBinding.rung_key == rung_key,
        )
    )
    if binding is not None:
        return binding.role.name
    return _RUNG_DEFAULTS.get(rung_key, rung_key)


@dataclass
class HierarchyNode:
    id: uuid.UUID | None   # None for display-only nodes resolved from pm_name
    name: str
    role: str | None


@dataclass
class EmployeeHierarchy:
    hr_representative: HierarchyNode | None
    team_lead: HierarchyNode | None
    project_manager: HierarchyNode | None
    senior_manager: HierarchyNode | None
    branch_manager: HierarchyNode | None
    branch_head: HierarchyNode | None


def _node(db: Session, employee_id: uuid.UUID | None) -> HierarchyNode | None:
    """Resolves an employee FK to a display node. Never raises -- an unset
    FK or an orphaned reference (deleted employee row) both degrade to None."""
    if employee_id is None:
        return None
    employee = db.get(models.Employee, employee_id)
    if employee is None:
        return None
    return HierarchyNode(
        id=employee.id,
        name=crud._full_name(employee),
        role=crud.get_employee_role_name(db, employee.id),
    )


def _name_node(name: str, role: str = PM_ROLE) -> HierarchyNode:
    """Display-only node for a PM stored as free text (pm_name) rather than
    as an employee FK — shown in the hierarchy card but not selectable in the
    edit-form dropdown (id=None keeps the dropdown at '— Unassigned —')."""
    return HierarchyNode(id=None, name=name, role=role)


def resolve_employee_hierarchy(db: Session, employee: models.Employee) -> EmployeeHierarchy:
    role = crud.get_employee_role_name(db, employee.id)
    department = employee.department
    branch = employee.branch
    company = db.get(models.Company, employee.company_id)

    # Per-company rung -> role-name bindings (core.hierarchy_role_bindings),
    # falling back to the hardcoded built-in names when unconfigured -- see
    # resolve_rung_role_name.
    tl_role = resolve_rung_role_name(db, employee.company_id, "team_lead")
    pm_role = resolve_rung_role_name(db, employee.company_id, "project_manager")
    sr_mgr_role = resolve_rung_role_name(db, employee.company_id, "senior_manager")

    hr_representative = _node(
        db, department.hr_representative_id if department else None
    )

    team_lead: HierarchyNode | None = None
    project_manager: HierarchyNode | None = None
    if role in IC_ROLES:
        # Team Lead/Project Manager only apply once the employee is
        # actually assigned to a project -- unassigned ICs/Associates get
        # null for both rungs, per the chain's own "if assigned to a
        # project" condition.
        allocation = crud.get_primary_project_allocation(db, employee.id)
        if allocation is not None:
            # pm_resource_allocations has no team_lead_id column: a
            # project's Team Lead is the allocation carrying the "Team Lead"
            # project-role (crud.resolve_project_team_lead_id). pm_projects
            # has no free-text pm_name either, so the old _name_node branch
            # is gone.
            team_lead = _node(
                db, crud.resolve_project_team_lead_id(db, allocation.project_id)
            )
            project = allocation.project
            pm_id = project.project_manager_id if project else None
            project_manager = (
                _node(db, pm_id)
                or _node(db, department.project_manager_id if department else None)
            )
    elif role == tl_role:
        # A Team Lead's own Project Manager link is unconditional (not tied
        # to a specific project), resolved from the department fallback.
        project_manager = _node(
            db, department.project_manager_id if department else None
        )

    senior_manager: HierarchyNode | None = None
    if role in IC_ROLES or role in (tl_role, pm_role):
        senior_manager = _node(
            db, department.senior_manager_id if department else None
        )

    branch_manager: HierarchyNode | None = None
    if role in IC_ROLES or role in (tl_role, pm_role, sr_mgr_role):
        branch_manager = _node(db, branch.branch_manager_id if branch else None)

    br_head_role = resolve_rung_role_name(db, employee.company_id, "branch_head")
    branch_head: HierarchyNode | None = None
    if role != br_head_role:
        branch_head = _node(db, company.branch_head_id if company else None)

    return EmployeeHierarchy(
        hr_representative=hr_representative,
        team_lead=team_lead,
        project_manager=project_manager,
        senior_manager=senior_manager,
        branch_manager=branch_manager,
        branch_head=branch_head,
    )
