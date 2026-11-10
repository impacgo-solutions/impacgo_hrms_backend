import datetime
import uuid
from decimal import Decimal
from typing import Annotated, Any

from pydantic import AfterValidator, Field, ValidationError, field_validator, model_validator
from pydantic import BaseModel as _PydanticBaseModel
from sqlalchemy import String

from . import password_policy
from . import employee_validation as ev


class BaseModel(_PydanticBaseModel):
    """Every schema below derives from this. Optional fields are optional
    all the way through: a blank value ("" or only spaces) sent for a field
    that has a default -- an optional text box left empty, an unpicked date
    / dropdown / id -- is treated as not sent instead of failing that
    field's format check (min_length, date / uuid / number parsing, a
    pattern, an "is required" field validator); so is a null sent for a
    field whose default isn't None (e.g. a defaulted label). It only steps in when
    validation actually failed on such a blank, so a blank that already
    validated is kept exactly as sent (some PATCH routes read "" as
    "clear"), and a REQUIRED field (no default) still fails as before."""

    @model_validator(mode="wrap")
    @classmethod
    def _blank_means_unset(cls, data, handler):
        try:
            return handler(data)
        except ValidationError as exc:
            if not isinstance(data, dict):
                raise
            fields = cls.model_fields
            blank = set()
            for err in exc.errors():
                loc = err.get("loc") or ()
                key = loc[0] if loc else None
                if not isinstance(key, str) or key not in data:
                    continue
                value = data[key]
                if not (value is None or (isinstance(value, str) and not value.strip())):
                    continue
                field = fields.get(key) or next(
                    (f for f in fields.values() if f.alias == key), None)
                if field is not None and not field.is_required():
                    blank.add(key)
            if not blank:
                raise
            return handler({k: v for k, v in data.items() if k not in blank})

# Largest values NUMERIC(12,2) / NUMERIC(14,2) columns hold (M-18).
MAX_NUMERIC_12_2 = 9_999_999_999.99
MAX_NUMERIC_14_2 = 999_999_999_999.99


def _today_with_slack(days: int) -> datetime.date:
    """Today (server date) shifted by [days] -- date-window validators give
    one day of slack either way for users in other time zones (L-19)."""
    return datetime.date.today() + datetime.timedelta(days=days)

# Every schema that SETS a password uses one of these -- one policy for
# creation, change and reset (app/password_policy.py).
NewPassword = Annotated[str, AfterValidator(password_policy.validate_password)]
PlatformPassword = Annotated[str, AfterValidator(password_policy.validate_platform_password)]


class RbacColumnOut(BaseModel):
    key: str
    label: str
    module: str


class RbacColumnsResponse(BaseModel):
    columns: list[RbacColumnOut]
    levels: list[str]
    level_labels: dict[str, str]


class PermissionOut(BaseModel):
    id: uuid.UUID
    code: str
    module: str
    resource: str
    action: str

    model_config = {"from_attributes": True}


class RoleOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    is_system: bool
    # Tenant-configurable Band -> Role hierarchy -- which core_bands row
    # (by id) this role belongs to. None = not yet mapped by this company.
    band_id: uuid.UUID | None = None

    model_config = {"from_attributes": True}


class RoleMatrixOut(BaseModel):
    role: RoleOut
    matrix: dict[str, str]  # column key -> level char ('n' if no access)
    # Built-in self-service roles only: the highest level each column may
    # hold (the role template, SEC-01). None = no cap.
    caps: dict[str, str] | None = None


class RoleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str | None = None
    band_id: uuid.UUID | None = None

    @field_validator("name")
    @classmethod
    def _not_platform_role(cls, value: str) -> str:
        # SA-01: "super_admin" is reserved for the platform console.
        import re

        if re.sub(r"[\s_\-]+", "_", value.strip().lower()) == "super_admin":
            raise ValueError("This role name is reserved")
        return value


class RoleUpdate(BaseModel):
    """PATCH /api/roles/{id} -- supports (re)assigning or clearing the Band
    mapping, and setting the dashboard/people scope + blurb backing
    GET /api/roles/scopes (see backend/db/add_role_scope_columns.sql).
    name/description/matrix already have their own dedicated update paths
    (role rename isn't supported anywhere today, and the matrix has
    PUT /api/roles/{id}/matrix)."""

    band_id: uuid.UUID | None = None
    dashboard_scope: str | None = None
    people_scope: str | None = None
    blurb: str | None = None


class MatrixUpdateRequest(BaseModel):
    # Partial update: only the columns present are changed. Level must be
    # one of 'n', 'v', 's', 'e', 'a'.
    matrix: dict[str, str]


class GranularPermissionOut(BaseModel):
    resource: str
    action: str
    label: str
    module: str
    module_name: str


class GranularPermissionsResponse(BaseModel):
    permissions: list[GranularPermissionOut]


class RoleActionsOut(BaseModel):
    role: RoleOut
    granted: dict[str, list[str]]  # resource -> [action, ...]


class RoleActionsUpdateRequest(BaseModel):
    # Partial update: only the resources present are changed; each replaces
    # that resource's full granted-action list.
    granted: dict[str, list[str]]


class UserRoleAssignRequest(BaseModel):
    role_id: uuid.UUID
    branch_id: uuid.UUID | None = None


class PasswordResetRequest(BaseModel):
    new_password: NewPassword


class ChangeMyPasswordRequest(BaseModel):
    """Edit Profile > self-service password change (POST /api/me/password).
    confirm_password is plain str (not NewPassword) -- it only needs to
    match new_password exactly; the policy check already ran on
    new_password itself, and running it twice would reject a mismatched
    confirm_password for the wrong reason."""

    new_password: NewPassword
    confirm_password: str


class UserRoleOut(BaseModel):
    role_id: uuid.UUID
    role_name: str
    branch_id: uuid.UUID | None = None


class UserOut(BaseModel):
    """Backs Administration > User Roles' user picker -- core.users has no
    other list endpoint exposed to the frontend today."""

    id: uuid.UUID
    email: str
    employee_id: uuid.UUID | None
    employee_name: str | None
    status: str


class DesignationOut(BaseModel):
    id: uuid.UUID
    name: str
    band: str | None
    is_active: bool
    # Tenant-configurable Role -> Designation hierarchy -- which core_roles
    # row (by id) this designation belongs to. None = not yet mapped.
    role_id: uuid.UUID | None = None
    # Direct tenant-configurable Designation -> Band mapping -- which
    # core_bands row (by id) this designation belongs to, independent of
    # role_id above. None = not yet mapped.
    band_id: uuid.UUID | None = None

    model_config = {"from_attributes": True}


class DesignationDeniedActionsOut(BaseModel):
    designation: DesignationOut
    denied: dict[str, list[str]]  # resource -> [action, ...] EXPLICITLY BLOCKED


class DesignationDeniedActionsUpdateRequest(BaseModel):
    # Partial update: only the resources present are changed; each replaces
    # that resource's full denied-action list. Named `denied`, not
    # `granted` -- a Designation can only restrict, never grant.
    denied: dict[str, list[str]]


class PeopleAccessOut(BaseModel):
    # False until this company grants any Role an 'employee' action -- the
    # People module is then wide open (every action) for every user,
    # exactly as it's always been. True once configured: `actions` becomes
    # the calling user's real effective set (their Role's grant minus their
    # own Designation's denials).
    configured: bool
    actions: list[str]


class MyActionsOut(BaseModel):
    resource: str
    actions: list[str]


class DesignationBandOut(BaseModel):
    band: int
    name: str
    designations: list[DesignationOut]


class DesignationCreate(BaseModel):
    """`band` (the older free-text label) is now optional -- when `band_id`
    is given instead, the router resolves that real core_bands row and
    derives `band`'s string value from its name automatically, so a caller
    no longer needs to supply both. Both are optional: with neither, the
    designation is created under the "Unassigned" band label and can be
    mapped to a Band later."""

    name: str = Field(min_length=1, max_length=120)
    band: str | None = Field(default=None, min_length=1, max_length=40)
    band_id: uuid.UUID | None = None
    role_id: uuid.UUID | None = None


class DesignationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    is_active: bool | None = None
    role_id: uuid.UUID | None = None
    band_id: uuid.UUID | None = None


class BandHierarchyDesignationOut(BaseModel):
    id: uuid.UUID
    name: str
    is_active: bool
    employee_count: int


class BandHierarchyRoleOut(BaseModel):
    id: uuid.UUID
    name: str
    employee_count: int
    designations: list[BandHierarchyDesignationOut]


class BandHierarchyBandOut(BaseModel):
    id: uuid.UUID
    name: str
    band_number: int
    employee_count: int
    roles: list[BandHierarchyRoleOut]
    # Designations mapped straight to this Band (core_designations.band_id)
    # with no Role in between -- kept separate from `roles` above so a
    # designation mapped to a Role is never double-counted here too.
    direct_designations: list[BandHierarchyDesignationOut]


class BandHierarchyOut(BaseModel):
    """Backs GET /api/organization/band-hierarchy -- the real, tenant-
    configured Band -> Role -> Designation tree (core_bands -> core_roles.
    band_id -> core_designations.role_id), plus any Designations mapped
    straight to a Band (core_designations.band_id, no Role in between) --
    used by the Organization > Designations and Org Chart tabs so both
    render the exact same structure. `configured=False` (no role or
    designation in this company has a band_id yet) means the company hasn't
    set up the hierarchy -- callers should keep showing their existing
    legacy view (grouped by the free-text Designation.band label) instead
    of an empty tree."""

    configured: bool
    bands: list[BandHierarchyBandOut]


class BandOut(BaseModel):
    """Tenant-specific Band master row -- see backend/db/
    add_core_bands_table.sql. Independent of DesignationBandOut above
    (which groups Designations by their free-text band label)."""

    id: uuid.UUID
    name: str
    code: str
    band_number: int
    description: str | None
    is_active: bool

    model_config = {"from_attributes": True}


class BandCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    code: str = Field(min_length=1, max_length=20)
    description: str | None = Field(default=None, max_length=2000)


class BandUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    code: str | None = Field(default=None, min_length=1, max_length=20)
    description: str | None = None
    is_active: bool | None = None


class RbacScopesOut(BaseModel):
    dashboard_scope: dict[str, str]
    people_scope: dict[str, str]
    role_blurbs: dict[str, str]


class LoginRequest(BaseModel):
    email: str = Field(min_length=1)
    password: str = Field(min_length=1, max_length=1024)


class EmployeeSummaryOut(BaseModel):
    id: uuid.UUID
    employee_code: str
    first_name: str
    last_name: str | None

    model_config = {"from_attributes": True}


class EmployeeListItem(BaseModel):
    id: str
    code: str
    name: str
    designation: str
    department: str
    branch: str
    manager: str
    reportingManagerId: str | None
    dottedLineManagerId: str | None = None
    type: str
    mode: str
    status: str
    email: str
    city: str
    doj: str
    band: int
    ctc: float
    # Admin-defined custom fields (Form Configuration, form "employee").
    customFields: dict[str, str] = {}


class EmployeeListPage(BaseModel):
    items: list[EmployeeListItem]
    next_cursor: str | None
    has_more: bool


class EmployeeDirectoryEntry(BaseModel):
    """Non-sensitive fields only -- see GET /employees/directory."""

    id: uuid.UUID
    name: str
    department: str | None = None
    designation: str | None = None
    branch: str | None = None
    reporting_manager_id: uuid.UUID | None = None
    dotted_line_manager_id: uuid.UUID | None = None


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    employee: EmployeeSummaryOut | None
    tenant_slug: str | None = None
    company_id: uuid.UUID | None = None
    permissions: list[str] = []


class BranchOut(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    country: str
    state: str | None
    city: str | None
    tz: str
    tax: str
    emp: int
    address_line1: str | None = None
    pincode: str | None = None
    # Dynamic reporting-hierarchy leader (see org_hierarchy.py).
    branch_manager: str = "—"
    branch_manager_id: uuid.UUID | None = None


class BranchCreate(BaseModel):
    # Optional (F25): omitted -> the server assigns a unique code.
    code: str | None = Field(default=None, min_length=1, max_length=20)
    name: str = Field(min_length=1, max_length=120)
    country: str = "India"
    state: str | None = None
    city: str | None = None
    tz: str | None = Field(default=None, max_length=60)
    tax: ev.OptGstinIn = None  # GSTIN (L-08)
    address_line1: str | None = Field(default=None, max_length=200)
    pincode: str | None = Field(default=None, max_length=10)
    branch_manager_id: uuid.UUID | None = None


class BranchUpdate(BaseModel):
    """Partial update — only the fields present are changed. code isn't
    here: it's the lookup key elsewhere."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    state: str | None = Field(default=None, max_length=80)
    city: str | None = Field(default=None, max_length=80)
    country: str | None = Field(default=None, max_length=80)
    tz: str | None = Field(default=None, max_length=60)
    tax: ev.OptGstinIn = Field(default=None, max_length=15)  # GSTIN (L-08)
    address_line1: str | None = Field(default=None, max_length=200)
    pincode: str | None = Field(default=None, max_length=10)
    branch_manager_id: uuid.UUID | None = None


class SubDepartmentOut(BaseModel):
    id: uuid.UUID
    name: str


class SubDepartmentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class SubDepartmentUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class DepartmentOut(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    head: str
    budget: str
    parent: str
    emp: int
    subs: list[str]
    # Same data as `subs`, but with each sub-department's id — needed to
    # rename one later (add sub-department form only needs the name string).
    sub_departments: list[SubDepartmentOut] = []
    # Raw number alongside `budget` (its formatted display string) — Edit
    # Department needs the actual value to edit, not the ₹-formatted text.
    annual_budget: float | None = None
    # Display name of the branch this department belongs to (or "—").
    branch: str = "—"
    # core.departments.business_unit_id -- the column already existed and is
    # reflected in `parent` (the BU's display name), but wasn't exposed as a
    # raw id until Project's own Business Unit -> Department cascade needed
    # to filter departments by id rather than fragile name-matching.
    business_unit_id: uuid.UUID | None = None
    # Raw ids for the edit dialog to pre-select the current head/branch.
    head_employee_id: uuid.UUID | None = None
    branch_id: uuid.UUID | None = None
    # Dynamic reporting-hierarchy leader assignments (see org_hierarchy.py).
    hr_representative: str = "—"
    hr_representative_id: uuid.UUID | None = None
    # All HR Representatives (primary first); hr_representative(_id) above
    # is the primary one.
    hr_representative_ids: list[uuid.UUID] = []
    hr_representatives: list[str] = []
    senior_manager: str = "—"
    senior_manager_id: uuid.UUID | None = None
    project_manager: str = "—"
    project_manager_id: uuid.UUID | None = None


class DepartmentCreate(BaseModel):
    # Optional (F25): omitted -> the server assigns a unique code.
    code: str | None = Field(default=None, min_length=1, max_length=20)
    name: str = Field(min_length=1, max_length=120)
    business_unit_name: str | None = None
    annual_budget: float | None = Field(default=None, ge=0, le=1e13)
    branch_id: uuid.UUID | None = None
    head_employee_id: uuid.UUID | None = None
    hr_representative_id: uuid.UUID | None = None
    # Several HR Representatives; when given it replaces hr_representative_id.
    hr_representative_ids: list[uuid.UUID] | None = Field(default=None, max_length=50)
    senior_manager_id: uuid.UUID | None = None
    project_manager_id: uuid.UUID | None = None


class DepartmentUpdate(BaseModel):
    """Partial update — only the fields present are changed. code isn't
    here: it's the lookup key elsewhere."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    business_unit_name: str | None = None
    head_employee_id: uuid.UUID | None = None
    annual_budget: float | None = Field(default=None, ge=0, le=1e13)
    branch_id: uuid.UUID | None = None
    hr_representative_id: uuid.UUID | None = None
    # Several HR Representatives; when given it replaces them all (an empty
    # list clears them) and takes precedence over hr_representative_id.
    hr_representative_ids: list[uuid.UUID] | None = Field(default=None, max_length=50)
    senior_manager_id: uuid.UUID | None = None
    project_manager_id: uuid.UUID | None = None


class BusinessUnitOut(BaseModel):
    id: uuid.UUID
    name: str
    head: str
    cost_center: str
    # Display name of the branch this unit belongs to (or "—"). Requires the
    # core_business_units.branch_id column (see models.BusinessUnit).
    branch: str = "—"
    head_employee_id: uuid.UUID | None = None
    branch_id: uuid.UUID | None = None


class BusinessUnitCreate(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    cost_center: str | None = None
    head_employee_id: uuid.UUID | None = None
    branch_id: uuid.UUID | None = None


class BusinessUnitUpdate(BaseModel):
    """Partial update — only the fields present are changed."""

    name: str | None = Field(default=None, min_length=1, max_length=150)
    cost_center: str | None = None
    head_employee_id: uuid.UUID | None = None
    branch_id: uuid.UUID | None = None


class BusinessUnitDepartmentsAssign(BaseModel):
    department_ids: list[uuid.UUID]


class BranchEmployeesSet(BaseModel):
    """Edit/Create Branch > Employees multi-select: the branch ends up with
    exactly these employees (duplicates in the list are ignored)."""

    employee_ids: list[uuid.UUID] = Field(default_factory=list, max_length=5000)


class BranchEmployeesOut(BaseModel):
    employee_ids: list[uuid.UUID]
    added: int
    moved_from_other_branches: int
    removed: int


class EmployeeOut(BaseModel):
    id: uuid.UUID
    employee_code: str
    first_name: str
    last_name: str | None
    work_email: str | None
    gender: str | None
    date_of_joining: datetime.date
    employment_type: str
    status: str
    band: int | None
    annual_ctc: int | None
    work_mode: str | None
    branch_name: str | None
    department_name: str | None
    designation_name: str | None
    custom_fields: dict[str, str] = {}


def _zero_band_none(v):
    """Band 0 / "0" means "no band" (forms send it when a company has none)."""
    return None if v in (0, "0") else v


class EmployeeCreate(BaseModel):
    """Identity/professional/payroll-basic fields core.employees has columns
    for, plus Education & Experience + Emergency Contact (hcm.
    employee_education/employee_prior_experience/employee_skills/
    certifications, core.contacts). Everything else the Add Employee form
    collects (benefits/attendance/leave/projects/performance/assets/
    documents/lifecycle) is derived client-side by the same formulas that
    back the existing 14 seed employees — those sections have their own
    backend wiring in later phases (Attendance, Payroll, Performance,
    Assets, Documents).

    role_name + password also create this employee a real login account
    (core.users + core.user_roles) as part of the same request — without
    it, a newly added employee would have an HR record but no way to ever
    sign in or hold an RBAC role.

    employee_code is optional: whoever creates this employee may enter one
    manually (validated for uniqueness within the company by
    crud.create_employee, which raises ValueError on a collision -- see
    that function for the exact check/lock). Left blank, it falls back to
    this company's configured number series (see
    crud.generate_employee_code, and PATCH /api/settings/employee-id-format
    for how the Organization Owner configures that series' prefix/suffix/
    padding/starting number). Either way, whatever value actually gets
    persisted is never silently overwritten."""

    employee_code: str | None = Field(default=None, max_length=20)
    first_name: str = Field(min_length=1, max_length=80)
    last_name: str | None = Field(default=None, max_length=80)
    # M-01 / L-08: closed sets, formats and bounds -- see app/employee_validation.py.
    work_email: ev.EmailIn = Field(min_length=1, max_length=150)
    role_name: str = Field(min_length=1, max_length=80)
    password: NewPassword
    gender: str | None = Field(default=None, max_length=120)
    date_of_birth: ev.OptDateText = Field(default=None, max_length=20)
    # Optional for every employment type (see _freelancer_relaxation below):
    # date_of_joining defaults to today (a Contract's start date when given),
    # status to Active, employment_type to Full-time, and branch/department/
    # designation may be left out. Only the identity/login fields
    # (first_name, work_email, role_name, password) are required.
    date_of_joining: datetime.date | None = None
    employment_type: ev.OptEmploymentTypeIn = Field(default=None, max_length=20)
    status: ev.OptStatusIn = Field(default=None, max_length=20)
    branch_name: str | None = Field(default=None, min_length=1, max_length=120)
    department_name: str | None = Field(default=None, min_length=1, max_length=120)
    designation_name: str | None = Field(default=None, min_length=1, max_length=120)
    work_mode: ev.OptWorkModeIn = Field(default=None, max_length=120)
    band: int | None = Field(default=None, ge=1, le=ev.MAX_BAND)
    # 0 (sent when the company has no bands) = no band.
    _band_zero = field_validator("band", mode="before")(classmethod(lambda cls, v: _zero_band_none(v)))
    annual_ctc: int | None = Field(default=None, ge=0, le=ev.MAX_ANNUAL_CTC)
    # Freelancer only (rejected for any other type by POST /api/employees):
    # pay rate from the joining date -- see crud.set_employee_rate.
    rate_amount: float | None = Field(default=None, gt=0, le=10000000)
    rate_unit: str | None = Field(default=None, pattern="^(hourly|daily)$")
    # ISO 4217 code of rate_amount (currencies.CURRENCIES); blank = the
    # company's default currency.
    rate_currency: str | None = Field(default=None, max_length=3)
    # Contract only (rejected for any other type). Both are required for a
    # Contract by POST /api/employees (which also checks the end date isn't
    # already past); other creators (recruitment hire, WorkTrack, imports)
    # set the end date their own way, so they're not forced here.
    contract_start_date: datetime.date | None = None
    contract_end_date: datetime.date | None = None
    reporting_manager_id: uuid.UUID | None = None
    # Two Reporting Managers: optional second/dotted-line manager, equally
    # authorized alongside reporting_manager_id to Approve/Reject this
    # employee's requests (see core_employees.dotted_line_manager_id).
    dotted_line_manager_id: uuid.UUID | None = None
    pan: ev.OptPanIn = Field(default=None, max_length=10)
    bank_name: str | None = Field(default=None, max_length=100)
    bank_account_no: ev.OptAccountNoIn = Field(default=None, max_length=30)
    bank_ifsc: ev.OptIfscIn = Field(default=None, max_length=11)
    bank_account_holder: str | None = Field(default=None, max_length=120)
    bank_branch: str | None = Field(default=None, max_length=120)
    # Payroll Information (Add Employee > Professional) -- the same fields,
    # names and limits as EmployeePayrollUpdate; pf / esi are
    # core_employees.uan / esi_number, tax_regime the employee-level column.
    pf: str | None = Field(default=None, max_length=12)
    esi: str | None = Field(default=None, max_length=17)
    tax_regime: str | None = Field(default=None, max_length=10)

    # -- Education & Experience tab --
    qualification: str | None = Field(default=None, max_length=150)
    institute: str | None = Field(default=None, max_length=200)
    specialization: str | None = Field(default=None, max_length=150)
    year_of_passing: int | None = None
    previous_employer: str | None = Field(default=None, max_length=200)
    experience_years: str | None = Field(default=None, max_length=30)
    domain: str | None = Field(default=None, max_length=150)
    skills: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)

    # -- Emergency contact (Personal tab) --
    emergency_contact_name: str | None = Field(default=None, max_length=120)
    emergency_contact_relation: str | None = Field(default=None, max_length=80)
    emergency_contact_phone: str | None = Field(default=None, max_length=20)

    # -- Personal tab fields added via manual_schema_additions --
    blood_group: str | None = Field(default=None, max_length=5)
    nationality: str | None = Field(default=None, max_length=120)
    marital_status: str | None = Field(default=None, max_length=120)
    personal_email: ev.OptEmailIn = Field(default=None, max_length=150)
    personal_phone: str | None = Field(default=None, max_length=20)
    current_address: str | None = None
    permanent_address: str | None = None

    # -- Optional sub-department --
    sub_department_name: str | None = Field(default=None, max_length=120)

    # -- Role Hierarchy assignment at creation time --
    # Assign the employee to any number of existing projects (by name) --
    # an employee can work on multiple projects at once. One
    # project_allocations row is created per project; each project already
    # carries its PM via hcm.projects.project_manager_id. An empty list
    # means the employee starts on bench — no allocation rows are created.
    project_names: list[str] = Field(default_factory=list)
    # Pre-existing field, unchanged by the move to multiple projects above:
    # accepted here but not applied by crud.create_employee (Team Lead is
    # set per-allocation afterward, via PATCH .../project-manager or
    # .../team-lead on that allocation).
    team_lead_id: uuid.UUID | None = None
    # L-10: True only when the user deliberately adds a new designation
    # ("+ Custom Designation"); otherwise an unknown designation is a 422.
    create_designation: bool = False

    # Admin-defined custom fields (Form Configuration, form "employee"):
    # {field_key: value}, validated and saved by POST /api/employees.
    custom_fields: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _freelancer_relaxation(self):
        """branch/department/designation/date_of_joining/status/
        employment_type are optional for every employment type (the path
        Freelancers always took): a missing status becomes Active, a missing
        joining date today (a Contract's start date when given) and a
        missing employment type Full-time. Contract dates (required for a
        Contract by POST /api/employees) must be in order when both given. Must run before
        _date_rules below since it fills in date_of_joining."""
        if self.employment_type is None:
            self.employment_type = "Full-time"
        is_contract = self.employment_type == "Contract"
        if is_contract:
            if (self.contract_start_date and self.contract_end_date
                    and self.contract_end_date < self.contract_start_date):
                raise ValueError("The contract end date must be on or after the contract start date.")
        elif self.contract_start_date is not None or self.contract_end_date is not None:
            raise ValueError("Contract dates can only be set for Contract employees.")
        for name in ("branch_name", "department_name", "designation_name"):
            value = getattr(self, name)
            if isinstance(value, str) and not value.strip():
                setattr(self, name, None)
        if self.status is None:
            self.status = "Active"
        if self.date_of_joining is None:
            self.date_of_joining = (self.contract_start_date if is_contract and self.contract_start_date
                                    else datetime.date.today())
        return self

    @model_validator(mode="after")
    def _date_rules(self):
        # M-01: joining date not absurd; DOB before joining with a minimum age.
        error = ev.joining_date_error(self.date_of_joining) or ev.dob_error(
            ev.as_date(self.date_of_birth), self.date_of_joining
        )
        if error:
            raise ValueError(error)
        return self

    @model_validator(mode="after")
    def _password_not_personal(self):
        password_policy.validate_password(
            self.password, context=(self.work_email, self.first_name, self.last_name)
        )
        return self


class EmployeeRoleUpdate(BaseModel):
    role_name: str = Field(min_length=1, max_length=80)


class EmployeeReportingUpdate(BaseModel):
    """Backs Employee Profile > Edit Reporting Manager
    (PATCH /api/employees/{id}/reporting) -- updates
    core.employees.reporting_manager_id and, when supplied,
    dotted_line_manager_id (the second/Two Reporting Managers field). Null
    clears either assignment."""

    reporting_manager_id: uuid.UUID | None = None
    # Optional, defaults to "leave unchanged" via exclude_unset in the
    # handler -- unlike reporting_manager_id, which this endpoint always
    # sets outright (its own long-standing contract), a caller that never
    # sends this field must not accidentally clear an existing second
    # manager.
    dotted_line_manager_id: uuid.UUID | None = None


class EmployeeHierarchyUpdate(BaseModel):
    """Backs Reporting Hierarchy > Edit (PATCH /api/employees/{id}/hierarchy).
    Each FK is routed to the owning model at save time:
      hr_representative_id / senior_manager_id → employee's Department
      branch_manager_id → employee's Branch
      branch_head_id → Company
      team_lead_id / project_manager_id → employee's primary ProjectAllocation
    All optional/partial (exclude_unset) — only supplied fields are written."""

    hr_representative_id: uuid.UUID | None = None
    # The department's HR Representatives (replaces them; empty clears).
    hr_representative_ids: list[uuid.UUID] | None = Field(default=None, max_length=50)
    team_lead_id: uuid.UUID | None = None
    project_manager_id: uuid.UUID | None = None
    senior_manager_id: uuid.UUID | None = None
    branch_manager_id: uuid.UUID | None = None
    branch_head_id: uuid.UUID | None = None


class EmployeeOrgUpdate(BaseModel):
    """Backs Employee Profile > Edit Professional Info
    (PATCH /api/employees/{id}/org) -- updates branch, department,
    designation, band, employment type/work mode/status, and the
    joining/confirmation/probation-end dates. All optional/partial.
    branch_name/department_name/designation_name are looked up by name so
    the frontend never needs to resolve UUIDs itself; the 3 date fields
    accept the same lenient formats as EmployeeCreate.date_of_birth (see
    crud.parse_lenient_date)."""

    branch_name: str | None = Field(default=None, max_length=120)
    department_name: str | None = Field(default=None, max_length=120)
    designation_name: str | None = Field(default=None, max_length=120)
    band: int | None = Field(default=None, ge=1, le=ev.MAX_BAND)
    # 0 (sent when the company has no bands) = no band.
    _band_zero = field_validator("band", mode="before")(classmethod(lambda cls, v: _zero_band_none(v)))
    employment_type: ev.OptEmploymentTypeIn = Field(default=None, max_length=20)
    work_mode: ev.OptWorkModeIn = Field(default=None, max_length=120)
    status: ev.OptStatusIn = Field(default=None, max_length=20)
    # M-02: a non-empty value that isn't a date is a 422 (never stored as None).
    date_of_joining: ev.OptDateText = None
    confirmation_date: ev.OptDateText = None
    probation_end_date: ev.OptDateText = None
    # F20: Edit Professional Info's Role, saved in the SAME transaction as
    # the fields above (it used to be a second PATCH /role call, so a role
    # failure left the org change saved and the role selection lost).
    # Needs System Settings / RBAC edit rights + the Owner-tier rules, same
    # as PATCH /api/employees/{id}/role.
    role_name: str | None = Field(default=None, min_length=1, max_length=80)
    # Admin-defined custom fields (Form Configuration, form "employee"):
    # only the keys sent are validated and saved; a blank value clears it.
    custom_fields: dict[str, Any] | None = None
    # Edit Profile edits every field: identity, managers and shift too.
    first_name: str | None = Field(default=None, min_length=1, max_length=80)
    last_name: str | None = Field(default=None, max_length=80)
    employee_code: str | None = Field(default=None, min_length=1, max_length=40)
    work_email: ev.OptEmailIn = Field(default=None, max_length=150)
    # Sent explicitly as null to clear (the field set is checked).
    reporting_manager_id: uuid.UUID | None = None
    dotted_line_manager_id: uuid.UUID | None = None
    # The employee's shift from today (null = no shift from today).
    shift_id: uuid.UUID | None = None


class EmployeeTransferRequest(BaseModel):
    """Backs the purpose-built Employee Transfer action
    (POST /api/employees/{id}/transfer) -- every field optional/partial
    (exclude_unset); only the fields present are changed, and every change
    is recorded in hcm.employee_lifecycle_events via
    crud.record_employee_change (see backend/db/add_employee_transfer_history_fields.sql)."""

    branch_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    sub_department_id: uuid.UUID | None = None
    reporting_manager_id: uuid.UUID | None = None
    dotted_line_manager_id: uuid.UUID | None = None


class ProjectAllocationTeamLeadUpdate(BaseModel):
    """Backs Edit Project Allocation's Team Lead field
    (PATCH /api/employees/{id}/team-lead) -- updates team_lead_id on the
    employee's primary hcm.project_allocations row. Null clears it."""

    team_lead_id: uuid.UUID | None = None


class ProjectAllocationPmUpdate(BaseModel):
    """Backs Edit Project Allocation's Project Manager field
    (PATCH /api/employees/{id}/project-manager) -- updates
    hcm.projects.project_manager_id for the project behind the employee's
    primary allocation (shared by every employee on that project, same as
    the project's own Project Manager). Null clears it."""

    project_manager_id: uuid.UUID | None = None


class EmployeeProfileUpdate(BaseModel):
    """Backs Employee Profile > Edit Profile — the Personal Information
    accordion's editable fields, plus the emergency contact (a separate
    core.contacts row). All optional/partial (exclude_unset), same pattern
    as CompanyProfileUpdate. date_of_birth accepts the same lenient formats
    as EmployeeCreate.date_of_birth (see crud.parse_lenient_date) --
    previously this and gender were viewable (GET .../personal) but had no
    update path at all."""

    date_of_birth: ev.OptDateText = None
    gender: str | None = Field(default=None, max_length=120)
    personal_email: ev.OptEmailIn = Field(default=None, max_length=150)
    personal_phone: str | None = Field(default=None, max_length=20)
    current_address: str | None = None
    permanent_address: str | None = None
    blood_group: str | None = Field(default=None, max_length=5)
    nationality: str | None = Field(default=None, max_length=120)
    marital_status: str | None = Field(default=None, max_length=120)
    emergency_contact_name: str | None = Field(default=None, max_length=120)
    emergency_contact_relation: str | None = Field(default=None, max_length=80)
    emergency_contact_phone: str | None = Field(default=None, max_length=20)


class EmployeeProfileOut(BaseModel):
    date_of_birth: str | None
    gender: str | None
    personal_email: str | None
    personal_phone: str | None
    current_address: str | None
    permanent_address: str | None
    blood_group: str | None
    nationality: str | None
    marital_status: str | None
    emergency_contact_name: str | None
    emergency_contact_relation: str | None
    emergency_contact_phone: str | None


class EmployeeEducationUpdate(BaseModel):
    """Backs Employee Profile > Edit Education & Experience (PATCH
    /api/employees/{id}/education). All optional/partial (exclude_unset).
    skills/certifications arrive as a single comma-separated string (the
    dialog's plain-text fields), split server-side. Certifications are
    additive-only (matched by name, never deleted) since hcm.certifications
    also backs Learning > Certifications rows that carry issuer/issue_date/
    expiry_date this endpoint has no business discarding."""

    qualification: str | None = None
    institute: str | None = None
    specialization: str | None = None
    year_of_passing: int | None = None
    certifications: str | None = None
    previous_employer: str | None = None
    experience_years: str | None = None
    skills: str | None = None
    domain: str | None = None
    # Multi-record form (add_employee_education_experience_records.sql). When
    # present each list REPLACES that employee's rows of its kind (an edit
    # form sends the whole table); omitted = rows untouched. Independent of
    # the single-record fields above, which keep working for older clients.
    education_records: list["EducationRecordIn"] | None = Field(default=None, max_length=20)
    experience_records: list["ExperienceRecordIn"] | None = Field(default=None, max_length=30)
    total_experience_range: str | None = None

    @field_validator("total_experience_range")
    @classmethod
    def _check_range(cls, v: str | None) -> str | None:
        v = (v or "").strip() or None
        if v is not None and v not in EXPERIENCE_RANGE_VALUES:
            raise ValueError(f"Total experience must be one of: {', '.join(EXPERIENCE_RANGE_VALUES)}")
        return v


# Dropdown values for Employee Details > Education & Experience. Served by
# GET /api/employees/education-options so the app never hardcodes them.
EDUCATION_LEVELS = (
    ("10th", "10th (SSC)"),
    ("Intermediate", "Intermediate (12th)"),
    ("Diploma", "Diploma"),
    ("Bachelors", "Bachelor's"),
    ("Masters", "Master's"),
    ("PhD", "PhD"),
    ("Other", "Other"),
)
EDUCATION_LEVEL_VALUES = tuple(v for v, _ in EDUCATION_LEVELS)
EXPERIENCE_RANGES = (
    ("fresher", "Fresher (no experience)"),
    ("0-1", "Less than 1 year"),
    ("1-2", "1 – 2 years"),
    ("2-3", "2 – 3 years"),
    ("3-5", "3 – 5 years"),
    ("5-8", "5 – 8 years"),
    ("8-12", "8 – 12 years"),
    ("12-15", "12 – 15 years"),
    ("15+", "15+ years"),
)
EXPERIENCE_RANGE_VALUES = tuple(v for v, _ in EXPERIENCE_RANGES)
EXPERIENCE_DOMAINS = (
    "Information Technology", "Software / SaaS", "ERP / Consulting", "Banking & Finance",
    "Insurance", "Healthcare & Pharma", "Manufacturing", "Retail & E-commerce",
    "Telecom", "Education", "Logistics & Transportation", "Construction & Real Estate",
    "Energy & Utilities", "Agriculture", "Media & Entertainment", "Government / Public Sector",
    "Hospitality & Travel", "Other",
)
MAX_EXPERIENCE_YEARS = 60


def _blank_to_none(v: str | None) -> str | None:
    v = (v or "").strip()
    return v or None


class EducationRecordIn(BaseModel):
    """One row of Education. `level` is the dropdown; `qualification` is the
    degree / course name typed by the user (B.Tech, MBA, ...)."""

    level: str
    qualification: str | None = Field(default=None, max_length=150)
    institute: str | None = Field(default=None, max_length=200)
    specialization: str | None = Field(default=None, max_length=150)
    year_of_passing: int | None = None
    class_percentage: str | None = Field(default=None, max_length=40)

    @field_validator("level")
    @classmethod
    def _check_level(cls, v: str) -> str:
        v = (v or "").strip()
        if v not in EDUCATION_LEVEL_VALUES:
            raise ValueError(f"Education level must be one of: {', '.join(EDUCATION_LEVEL_VALUES)}")
        return v

    @field_validator("qualification", "institute", "specialization", "class_percentage")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        return _blank_to_none(v)

    @field_validator("year_of_passing")
    @classmethod
    def _check_year(cls, v: int | None) -> int | None:
        if v is not None and not (1950 <= v <= datetime.date.today().year + 6):
            raise ValueError(f"Year of passing must be between 1950 and {datetime.date.today().year + 6}")
        return v


class ExperienceRecordIn(BaseModel):
    """One previous employer. Duration is `start_date` .. `end_date` (or
    `is_current`); `years_experience` is filled from the dates when omitted."""

    company_name: str = Field(max_length=200)
    domain: str | None = Field(default=None, max_length=150)
    job_role: str | None = Field(default=None, max_length=150)
    years_experience: float | None = None
    start_date: datetime.date | None = None
    end_date: datetime.date | None = None
    is_current: bool = False

    @field_validator("company_name")
    @classmethod
    def _need_company(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("Company name is required for every previous employment")
        return v

    @field_validator("domain", "job_role")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        return _blank_to_none(v)

    @field_validator("years_experience")
    @classmethod
    def _check_years(cls, v: float | None) -> float | None:
        if v is not None and not (0 <= v <= MAX_EXPERIENCE_YEARS):
            raise ValueError(f"Years of experience must be between 0 and {MAX_EXPERIENCE_YEARS}")
        return v

    @model_validator(mode="after")
    def _check_dates(self) -> "ExperienceRecordIn":
        today = datetime.date.today()
        if self.start_date and self.start_date > today:
            raise ValueError("Employment start date can't be in the future")
        if self.is_current:
            self.end_date = None
        if self.end_date:
            if self.end_date > today:
                raise ValueError("Employment end date can't be in the future")
            if self.start_date and self.end_date < self.start_date:
                raise ValueError("Employment end date can't be before the start date")
            if not self.start_date:
                raise ValueError("Add the start date as well as the end date")
        return self


class EmployeePayrollUpdate(BaseModel):
    """Backs Employee Profile > Edit Bank Details (PATCH
    /api/employees/{id}/payroll). All optional/partial (exclude_unset), same
    pattern as EmployeeProfileUpdate. pf/esi are the dialog's field names for
    core.employees.uan/esi_number; tax_regime backs a plain employee-level
    column (not a specific hcm.tax_declarations row) since the dialog treats
    it as a standing employee preference, not a yearly filing."""

    bank_name: str | None = Field(default=None, max_length=100)
    bank_account_no: ev.OptAccountNoIn = Field(default=None, max_length=30)
    bank_ifsc: ev.OptIfscIn = Field(default=None, max_length=11)
    bank_account_holder: str | None = Field(default=None, max_length=120)
    bank_branch: str | None = Field(default=None, max_length=120)
    pan: ev.OptPanIn = Field(default=None, max_length=10)
    pf: str | None = Field(default=None, max_length=12)
    esi: str | None = Field(default=None, max_length=17)
    tax_regime: str | None = Field(default=None, max_length=10)
    # core_employees.annual_ctc directly -- NOT the same as assigning/
    # changing a Salary Structure (which drives the actual Basic/Gross/Net
    # split via hcm_salary_structure_assignments and only takes effect once
    # payroll is (re)generated). This just updates the flat reference number
    # shown as "CTC (Annual)".
    annual_ctc: float | None = Field(default=None, ge=0, le=ev.MAX_ANNUAL_CTC)


class EmployeePayrollOut(BaseModel):
    ctc: float
    basic: int
    gross: int
    net: int
    variable: int
    pf: str
    esi: str
    pan: str
    taxRegime: str
    bank: str
    account: str
    ifsc: str
    accountHolder: str = "—"
    bankBranch: str = "—"


class ShiftOut(BaseModel):
    id: uuid.UUID
    name: str
    time: str
    type: str
    assigned: int
    is_active: bool
    # Raw 24h HH:MM, alongside the display-formatted `time` above -- the Edit
    # Shift dialog needs these to prefill its inputs without re-parsing
    # "9:00 AM - 6:00 PM" back into a time.
    start_time: str
    end_time: str
    # Break Management -- the shift's own break policy (see
    # crud.get_break_policy): total minutes of break time allowed across
    # ALL breaks in one session, and how many separate breaks that may be
    # split across. Never a company-wide default; always this shift's own.
    break_minutes: int
    max_breaks: int
    # Tolerance / grace period (minutes) applied symmetrically at both
    # boundaries by crud.clock_in_out: a check-in after start_time is only
    # 'late' once past start_time + grace_minutes, and a check-out only
    # becomes 'off_shift' once past end_time + grace_minutes. Always this
    # shift's own value (models.Shift.grace_minutes); no company-wide
    # default exists.
    grace_minutes: int


def _collapse_spaces(v):
    """L-11: shift names are trimmed / inner whitespace collapsed before the
    length check, so a name of only spaces is refused."""
    return " ".join(v.split()) if isinstance(v, str) else v


class ShiftCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    start_time: str = Field(pattern=r"^\d{2}:\d{2}$")
    end_time: str = Field(pattern=r"^\d{2}:\d{2}$")
    is_night: bool = False
    break_minutes: int = Field(default=60, ge=0, le=480)
    max_breaks: int = Field(default=1, ge=0, le=10)
    grace_minutes: int = Field(default=10, ge=0, le=180)

    _name = field_validator("name", mode="before")(classmethod(lambda cls, v: _collapse_spaces(v)))


class ShiftUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    start_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    end_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    is_night: bool | None = None
    is_active: bool | None = None
    break_minutes: int | None = Field(default=None, ge=0, le=480)
    max_breaks: int | None = Field(default=None, ge=0, le=10)
    grace_minutes: int | None = Field(default=None, ge=0, le=180)

    _name = field_validator("name", mode="before")(classmethod(lambda cls, v: _collapse_spaces(v)))


class ShiftAssignmentCreate(BaseModel):
    employee_id: uuid.UUID
    # Omitted = today (company timezone). Never in the past: past shift
    # history and the attendance judged against it stay unchanged.
    from_date: datetime.date | None = None


class ShiftAssignmentBulkCreate(BaseModel):
    employee_ids: list[uuid.UUID] = Field(min_length=1)
    # Omitted = today (company timezone); never in the past (see above).
    from_date: datetime.date | None = None


class ShiftAssignmentBulkResult(BaseModel):
    assigned_employee_ids: list[uuid.UUID]
    already_assigned_employee_ids: list[uuid.UUID]


class ShiftCurrentAssignmentOut(BaseModel):
    """An employee's active shift today and their next scheduled change --
    GET /shifts/current-assignments, so the Shift Management checklists can
    show "currently on <shift>" / "moves to <shift> from <date>"."""
    employee_id: uuid.UUID
    shift_id: uuid.UUID | None = None
    shift_name: str | None = None
    from_date: datetime.date | None = None
    upcoming_shift_id: uuid.UUID | None = None
    upcoming_shift_name: str | None = None
    upcoming_from_date: datetime.date | None = None


class ShiftUpcomingOut(BaseModel):
    shift: "ShiftOut"
    effective_date: datetime.date


class MyShiftScheduleOut(BaseModel):
    """GET /shifts/mine/schedule -- the caller's shift today and the next
    scheduled change (if any), straight from hcm_shift_assignments."""
    current: "ShiftOut | None" = None
    upcoming: ShiftUpcomingOut | None = None


class ShiftAssignmentHistoryOut(BaseModel):
    """One row of the complete assignment history."""
    id: uuid.UUID
    employee_id: uuid.UUID
    employee_name: str
    shift_id: uuid.UUID
    shift_name: str
    previous_shift_name: str | None = None
    effective_from: datetime.date
    effective_to: datetime.date | None = None
    # scheduled | active | ended | cancelled (never took effect)
    status: str
    assigned_at: datetime.datetime | None = None
    assigned_by: str | None = None
    updated_at: datetime.datetime | None = None
    updated_by: str | None = None


class ShiftAssignmentOut(BaseModel):
    """One employee currently actively assigned to a shift -- backs the
    Shift Management > Manage Employees checklist's pre-checked state
    (GET /shifts/{id}/assignments), which nothing exposed before this."""
    employee_id: uuid.UUID
    employee_name: str
    from_date: datetime.date


class ShiftAssignmentBulkUnassign(BaseModel):
    employee_ids: list[uuid.UUID] = Field(min_length=1)
    # The removal's Effective Date: the assignment's last day is the day
    # before. Omitted = today (company timezone).
    effective_date: datetime.date | None = None


class ShiftAssignmentBulkUnassignResult(BaseModel):
    unassigned_employee_ids: list[uuid.UUID]
    not_assigned_employee_ids: list[uuid.UUID]


class HolidayOut(BaseModel):
    id: uuid.UUID
    date: str
    day: str
    name: str
    region: str
    is_optional: bool = False
    # Created by Leave > Holiday Calendar > Upload Document.
    imported: bool = False


class HolidayCreate(BaseModel):
    holiday_date: datetime.date
    name: str = Field(min_length=1, max_length=120)
    branch_name: str | None = None
    # Optional (restricted) holiday: a working day unless leave is taken.
    is_optional: bool = False


class HolidayUpdate(BaseModel):
    holiday_date: datetime.date | None = None
    name: str | None = Field(default=None, min_length=1, max_length=120)
    # "" / "All Branches" = every branch.
    branch_name: str | None = None
    is_optional: bool | None = None


class LeaveTypeOut(BaseModel):
    id: uuid.UUID
    name: str
    code: str
    is_paid: bool
    max_days_per_year: float | None
    carry_forward: bool
    is_encashable: bool
    applicable_employment_types: list[str] = ["full_time", "part_time", "intern"]

    model_config = {"from_attributes": True}


class LeaveTypeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    # H-09: optional -- left blank, a unique code is generated from the name.
    code: str | None = Field(default=None, max_length=10)
    is_paid: bool = True
    max_days_per_year: float | None = Field(default=None, ge=0, le=366)
    carry_forward: bool = False
    is_encashable: bool = False
    applicable_employment_types: list[str] | None = None

    @field_validator("applicable_employment_types")
    @classmethod
    def _valid_employment_types(cls, v):
        return _employment_type_list(v)


class LeaveTypeUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=80)
    code: str | None = Field(None, min_length=1, max_length=10)
    is_paid: bool | None = None
    max_days_per_year: float | None = Field(default=None, ge=0, le=366)
    carry_forward: bool | None = None
    is_encashable: bool | None = None
    applicable_employment_types: list[str] | None = None

    @field_validator("applicable_employment_types")
    @classmethod
    def _valid_employment_types(cls, v):
        return _employment_type_list(v)


def _employment_type_list(v: list[str] | None) -> list[str] | None:
    """Leave type eligibility: a non-empty subset of the employment type
    codes, de-duplicated in a stable order."""
    if v is None:
        return None
    order = ("full_time", "part_time", "intern", "contract", "freelancer")
    picked = {str(x).strip().lower().replace("-", "_").replace(" ", "_") for x in v}
    bad = sorted(picked - set(order))
    if bad:
        raise ValueError(f"Unknown employment type(s): {', '.join(bad)}")
    if not picked:
        raise ValueError("Pick at least one employment type")
    return [t for t in order if t in picked]


class LeaveBalanceOut(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    leave_type_id: uuid.UUID
    leave_type_name: str
    leave_type_code: str
    allocated: float
    used: float
    # H-07: remaining = allocated + carried_forward - used - pending; may be
    # negative (over-drawn), never clamped to 0.
    remaining: float
    carried_forward: float = 0
    pending: float = 0

    model_config = {"from_attributes": True}

class LeaveBalanceCreate(BaseModel):
    """M-07/H-09: an existing leave type (id, or exact name / code)."""
    employee_id: uuid.UUID
    leave_type_id: uuid.UUID | None = None
    leave_type_name: str | None = Field(default=None, max_length=80)
    leave_type_code: str | None = Field(default=None, max_length=10)
    allocated: float = Field(ge=0, le=366)

    @model_validator(mode="after")
    def _needs_type(self):
        if self.leave_type_id is None and not (self.leave_type_name or "").strip() \
                and not (self.leave_type_code or "").strip():
            raise ValueError("Pick a leave type")
        return self

class LeaveBalanceUpdate(BaseModel):
    allocated: float = Field(ge=0, le=366)


# The only values an employee can manually pick for a given attendance
# day's Work Mode (see AttendanceRecord.work_mode) -- never inferred from
# office timings, location, check-in method, or shift.
ATTENDANCE_WORK_MODES = ("WFO", "WFH", "Client Site")


def _validate_work_mode(value: str | None) -> str | None:
    if value is not None and value not in ATTENDANCE_WORK_MODES:
        raise ValueError(f"work_mode must be one of {', '.join(ATTENDANCE_WORK_MODES)}")
    return value


class AttendanceLeaveGateOut(BaseModel):
    """GET /api/attendance/leave-gate -- lets the Attendance screen disable
    Check-In/Check-Out (and show why) BEFORE the employee even attempts the
    action, using the exact same rule crud.clock_in_out enforces server-
    side (see crud.get_blocking_leave_window) -- this is a convenience for
    the UI, never the actual security boundary; the backend still refuses
    the action outright even if a caller skips this and hits /attendance/
    clock directly."""
    full_day: bool
    # "HH:MM" 24-hour, local wall-clock (already shift-derived) -- both
    # null when full_day is True.
    start: str | None = None
    end: str | None = None
    reason: str


class AttendanceClockRequest(BaseModel):
    employee_id: uuid.UUID
    # Which single action the caller actually intends -- 'check_in' or
    # 'check_out'. Required (not inferred from current state) so a
    # duplicate/late-arriving request can never be silently reinterpreted
    # as the OTHER action just because the first request already changed
    # the record's state -- see crud.clock_in_out's docstring for exactly
    # why that mattered (a rapid double-click on "Clock In" used to be able
    # to immediately Check the employee back Out again).
    action: str = Field(pattern="^(check_in|check_out)$")
    # Optional: lets the employee declare today's Work Mode right when they
    # clock in (the moment their attendance record is first created).
    work_mode: str | None = None

    _validate_work_mode = field_validator("work_mode")(_validate_work_mode)


class AttendanceRecordEdit(BaseModel):
    """Daily Attendance > Edit Attendance (PATCH /api/attendance/records/{id})
    by a reporting manager / attendance admin. Only the fields sent change;
    check_in / check_out sent as null clear them. status omitted or "auto" =
    worked out from the times (crud.attendance_status_for). A reason is
    required -- it is kept in the record's edit history."""
    attendance_date: datetime.date | None = None
    check_in: datetime.datetime | None = None
    check_out: datetime.datetime | None = None
    work_mode: str | None = Field(default=None, max_length=20)
    status: str | None = None
    reason: str = Field(min_length=3, max_length=500)


class AttendanceEditHistoryOut(BaseModel):
    """One Edit Attendance change (from core_audit_logs)."""
    at: datetime.datetime
    edited_by: str
    reason: str | None = None
    # field -> [before, after]
    changes: dict[str, list]


class AttendanceWorkModeUpdate(BaseModel):
    """Documents > Attendance -- lets an employee (or anyone permitted to
    edit their attendance, see crud.can_submit_self_service_request) set or
    correct the manually-selected Work Mode on an existing attendance
    record, independent of check-in/out."""
    work_mode: str = Field(min_length=1)

    _validate_work_mode = field_validator("work_mode")(_validate_work_mode)


class BreakOut(BaseModel):
    """One row of AttendanceRecord.breaks -- see crud.serialize_break."""
    id: uuid.UUID
    break_start: datetime.datetime
    break_end: datetime.datetime | None
    # Minutes, computed at read time from (break_end or now) - break_start
    # -- null only in the pathological case break_start itself is missing,
    # which never actually happens (it's required on write).
    duration_minutes: float | None = None
    in_progress: bool


class BreakStatusOut(BaseModel):
    """GET /api/attendance/breaks/mine -- the caller's own live break state
    for today's attendance session, resolved entirely from real
    BreakRecord/AttendanceRecord/Shift rows (see crud.get_break_status).
    Lets the Attendance screen's countdown timer hydrate correctly after a
    refresh, logout/login, or on another device, instead of only tracking
    state in local widget memory."""
    has_attendance_record: bool
    checked_out: bool
    in_progress: bool
    current_break_id: uuid.UUID | None = None
    break_start: datetime.datetime | None = None
    elapsed_minutes: float = 0
    breaks_taken: int
    max_breaks: int
    total_break_minutes_used: float
    allowed_break_minutes: float
    remaining_break_minutes: float
    breaks: list[BreakOut] = []


class BreakActionRequest(BaseModel):
    employee_id: uuid.UUID


class AttendanceRecordOut(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    attendance_date: datetime.date
    check_in: datetime.datetime | None
    check_out: datetime.datetime | None
    work_hours: float | None
    overtime_hours: float | None = None
    status: str
    # Raw stored status (status above is the read-time display override) --
    # what the dashboard's attendance counts tally.
    stored_status: str | None = None
    # M-13: the check-in verdict (present / early_in / late), kept even
    # after check-out changes `status`.
    arrival_status: str | None = None
    # Denormalized identity fields (see crud.serialize_attendance_record) so
    # the Daily Attendance tab can render Employee/Work Mode/Geo-IP columns
    # without a separate, RBAC-gated GET /api/employees/full roster fetch --
    # that roster is scoped by a company's own People-access policy
    # (crud.can_access_people_module) and can be empty/incomplete for a
    # caller who can still see this exact attendance row.
    employee_name: str | None = None
    employee_code: str | None = None
    # The employee's own manually-selected Work Mode for THIS attendance
    # record (AttendanceRecord.work_mode) -- null until they pick one.
    # Deliberately NOT the employee profile's static Work Mode field
    # (Office/Remote/Hybrid, set under People > Professional Info): that's
    # a separate, company-declared default, not a day-by-day selection.
    work_mode: str | None = None
    branch_name: str | None = None
    # The employee's current department / designation (Daily Attendance
    # columns, sortable server-side).
    department_name: str | None = None
    designation_name: str | None = None
    # The caller may edit this record (Edit Attendance, crud.can_edit_attendance).
    can_edit: bool = False
    # Break Management -- both derived at read time from this record's real
    # BreakRecord rows (see crud.serialize_attendance_record), never
    # stored/duplicated. total_break_minutes sums every break (including
    # one still in progress, up to now); net_work_hours is work_hours minus
    # that (only meaningful once checked out -- null while still checked
    # in, since work_hours itself isn't final yet either).
    total_break_minutes: float = 0
    net_work_hours: float | None = None
    # Every Clock In -> Clock Out of the day (crud.serialize_attendance_session).
    sessions: list["AttendanceSessionOut"] = []
    session_count: int = 0
    # The employee is clocked in right now on this day's running session.
    clocked_in: bool = False

    model_config = {"from_attributes": True}


class AttendanceSessionOut(BaseModel):
    id: uuid.UUID | None = None
    clock_in: datetime.datetime | None
    clock_out: datetime.datetime | None = None
    hours: float = 0
    open: bool = False
    # Closed by the system: never clocked out (counts 0 h).
    missing_clock_out: bool = False
    work_mode: str | None = None


AttendanceRecordOut.model_rebuild()


class AttendanceHoursSummaryOut(BaseModel):
    from_date: datetime.date
    to_date: datetime.date
    days_worked: int
    sessions: int
    total_hours: float
    break_hours: float
    net_hours: float
    avg_hours_per_day: float


class AttendanceRecordPage(BaseModel):
    items: list[AttendanceRecordOut]
    next_cursor: str | None
    has_more: bool


class RegularizationCreate(BaseModel):
    employee_id: uuid.UUID
    attendance_date: datetime.date
    # Optional: blank / omitted is stored as "" (the column is NOT NULL).
    reason: str = Field(default="", max_length=2000)
    requested_in: datetime.datetime | None = None
    requested_out: datetime.datetime | None = None

    @field_validator("reason", mode="before")
    @classmethod
    def _strip_reason(cls, v):
        if v is None:
            return ""
        return v.strip() if isinstance(v, str) else v


class CompanyProfileOut(BaseModel):
    legal_name: str
    display_name: str
    fiscal_year: str
    # Raw 1-12 start month backing the `fiscal_year` display string above --
    # the Edit Company Profile form needs this to pre-select the dropdown.
    fiscal_year_start_month: int
    registered_address: str
    cin: str
    gstin: str
    pan: str
    active_users: int
    # Raw address components, alongside the combined `registered_address`
    # display string above — the Edit Company Profile form needs these
    # separately since it edits each address line individually.
    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    state: str | None = None
    pincode: str | None = None
    country: str | None = None
    industry: str
    founded_date: str
    email_domain: str
    # The single employee who oversees every Branch Manager company-wide --
    # top rung of the dynamic reporting hierarchy (see org_hierarchy.py).
    branch_head: str = "—"
    branch_head_id: uuid.UUID | None = None
    # /media/company_logo/<company_id>/<file> (signed by the media
    # middleware); set via POST /api/company-profile/logo.
    logo_url: str | None = None


class CompanyProfileUpdate(BaseModel):
    """Partial update — only the fields present are changed. Active-user
    count isn't here: it's derived from live core.users rows, not directly
    editable. fiscal_year_start_month IS editable (1-12, the calendar month
    the company's financial year begins in) -- each tenant company can run
    its own financial year."""

    display_name: str | None = Field(default=None, max_length=150)
    fiscal_year_start_month: int | None = Field(default=None, ge=1, le=12)
    legal_name: str | None = Field(default=None, max_length=200)
    gstin: ev.OptGstinIn = Field(default=None, max_length=15)
    pan: ev.OptPanIn = Field(default=None, max_length=10)
    cin: str | None = Field(default=None, max_length=25)
    address_line1: str | None = Field(default=None, max_length=200)
    address_line2: str | None = Field(default=None, max_length=200)
    city: str | None = Field(default=None, max_length=80)
    state: str | None = Field(default=None, max_length=80)
    pincode: str | None = Field(default=None, max_length=10)
    country: str | None = Field(default=None, max_length=80)
    industry: str | None = Field(default=None, max_length=100)
    # ISO date string (e.g. "2015-04-01") — converted to a date in the router
    # since core.companies.founded_date is a real date column, not text.
    founded_date: str | None = None
    email_domain: str | None = Field(default=None, max_length=150)
    branch_head_id: uuid.UUID | None = None

    @field_validator("email_domain")
    @classmethod
    def _valid_email_domain(cls, v):
        # Drives generated work emails: blank = "use the derived default",
        # otherwise a bare lowercase domain such as company.com.
        import re as _re

        if v is None:
            return None
        v = v.strip().lower()
        if v and not _re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+", v):
            raise ValueError("email_domain must be a domain like company.com")
        return v


class OrgHierarchyTierOut(BaseModel):
    band: str
    count: int
    sample_designations: str


class PayslipOut(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    month: str
    gross: int
    deductions: int
    net: int
    status: str
    # Approved Travel/Expense reimbursements paid with this slip -- separate
    # from gross/deductions/net (never salary).
    reimbursements: int = 0


class PayrollSummaryOut(BaseModel):
    total_payroll_cost: int
    employees_processed: int
    total_employees: int
    pf_contribution: int
    tds_deducted: int
    # The run these figures come from, labelled like a payslip's "month"
    # ("September 2026") -- lets the stat cards drill into exactly that
    # run's payslips. None when no run exists yet.
    period_label: str | None = None


class DashboardSummaryOut(BaseModel):
    present_today: int
    absent_today: int
    on_leave_today: int
    attendance_pct: int
    overtime_hours_week: int
    daily_present: list[int]
    # crud.dashboard_attendance_summary has always computed these three, but
    # they were missing here -- FastAPI's response_model silently strips any
    # dict key not declared on the model, so every response ever sent to the
    # Attendance screen's "Late Arrivals (MTD)"/"Absent (MTD)"/"Overtime
    # Hours (MTD)" stat cards was missing them regardless of the real
    # underlying counts.
    late_month: int = 0
    absent_month: int = 0
    overtime_hours_month: int = 0
    # DATA-01: daily_present is Mon..Sun of the current company-timezone
    # week; these label/explain it (1:1 with daily_present).
    daily_present_dates: list[datetime.date] = []
    daily_present_counts: list[int] = []
    summary_date: datetime.date | None = None
    active_headcount: int = 0


class RegularizationOut(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    attendance_date: datetime.date
    reason: str
    status: str
    requested_in: datetime.datetime | None
    requested_out: datetime.datetime | None
    approver_id: uuid.UUID | None = None
    approver_name: str = "—"
    # Denormalized so the Regularization Requests tab can render the
    # requester's name without a separate GET /api/employees/full roster
    # fetch -- that roster is gated by a company's own People-access policy
    # (crud.can_access_people_module) and can be empty/incomplete for a
    # caller who can still see this exact request.
    employee_name: str = "—"
    decision_notes: str | None = None
    decided_at: datetime.datetime | None = None
    # Two Reporting Managers: "Reporting Manager" / "Dotted-Line Manager" /
    # "Other Approver" -- classifies approver_id against the requester's own
    # two direct managers (see crud.decided_by_manager_type) so each manager
    # can immediately see whether the OTHER one of them (rather than a
    # higher-level manager or admin override) decided this. None until
    # decided.
    decided_by_manager_type: str | None = None

    model_config = {"from_attributes": True}


class RegularizationUpdate(BaseModel):
    status: str = Field(pattern="^(approved|rejected|sent_back)$")
    decision_notes: str | None = Field(default=None, max_length=2000)


class LeaveRequestCreate(BaseModel):
    employee_id: uuid.UUID
    # H-09: an existing leave type -- by id (preferred) or exact name.
    leave_type_id: uuid.UUID | None = None
    leave_type_name: str | None = Field(default=None, min_length=1, max_length=80)
    from_date: datetime.date
    to_date: datetime.date
    # H-06: optional and never trusted -- the server computes the working
    # days from the dates (weekly offs / holidays / half day) and rejects a
    # value that disagrees (leave_policy.validate_requested_days).
    days: float | None = Field(default=None, gt=0)
    reason: str | None = Field(default=None, max_length=2000)
    is_half_day: bool = False
    # 'morning' (first half) or 'afternoon' (second half) -- required when
    # is_half_day is True (see _check_half_day below), ignored otherwise.
    half_day_period: str | None = Field(default=None, pattern="^(morning|afternoon)$")
    # When days exceeds leave_type_name's remaining balance, the employee
    # chose this other company-provided leave type (from
    # GET /leave-balances, filtered client-side to ones with balance left)
    # to cover the shortfall first, before any remainder falls through to
    # LOP -- see crud.resolve_leave_request_split. None when the employee
    # had no other type with balance to choose from (or didn't need one).
    overflow_leave_type_name: str | None = Field(default=None, max_length=80)

    @model_validator(mode="after")
    def _check_date_order(self):
        if self.to_date < self.from_date:
            raise ValueError("to_date cannot be before from_date")
        if self.leave_type_id is None and not (self.leave_type_name or "").strip():
            raise ValueError("Pick a leave type")
        return self

    @model_validator(mode="after")
    def _check_half_day(self):
        # A half-day request is, by definition, a single day split in two --
        # never a multi-day range, and never a different day count than
        # exactly 0.5 (the full-day quantity the employee typed is
        # meaningless once "half day" is checked; the client is expected to
        # send 0.5 itself, but this is re-enforced here rather than trusted,
        # same as every other server-side invariant in this app).
        if self.is_half_day:
            if self.from_date != self.to_date:
                raise ValueError("A half-day leave request must be for a single date")
            if self.days is not None and self.days != 0.5:
                raise ValueError("A half-day leave request must be exactly 0.5 days")
            if self.half_day_period is None:
                raise ValueError("Select Morning or Afternoon for a half-day leave request")
        return self


class LeaveRequestEdit(BaseModel):
    """WF-04: the requester's own edit of a pending / sent-back leave request
    (PATCH /api/leave-requests/{id}/edit). The full form is sent again --
    same validation rules as LeaveRequestCreate."""
    leave_type_id: uuid.UUID | None = None
    leave_type_name: str | None = Field(default=None, min_length=1, max_length=80)
    from_date: datetime.date
    to_date: datetime.date
    days: float | None = Field(default=None, gt=0)
    reason: str | None = Field(default=None, max_length=2000)
    is_half_day: bool = False
    half_day_period: str | None = Field(default=None, pattern="^(morning|afternoon)$")

    @model_validator(mode="after")
    def _check_dates_and_half_day(self):
        if self.to_date < self.from_date:
            raise ValueError("to_date cannot be before from_date")
        if self.leave_type_id is None and not (self.leave_type_name or "").strip():
            raise ValueError("Pick a leave type")
        if self.is_half_day:
            if self.from_date != self.to_date:
                raise ValueError("A half-day leave request must be for a single date")
            if self.days is not None and self.days != 0.5:
                raise ValueError("A half-day leave request must be exactly 0.5 days")
            if self.half_day_period is None:
                raise ValueError("Select Morning or Afternoon for a half-day leave request")
        return self


class LeaveRequestOut(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    # So the frontend can render the requester's name (Leave Approvals table,
    # notification-adjacent views) without needing the sensitive, strictly
    # People-access-gated GET /employees/full roster -- a manager deciding
    # requests from someone outside their own loaded roster (e.g. no
    # company-wide People view grant) still needs their name to show up.
    employee_name: str = "—"
    leave_type_name: str
    from_date: datetime.date
    to_date: datetime.date
    days: float
    reason: str | None = None
    is_half_day: bool = False
    half_day_period: str | None = None
    # "09:00 AM - 01:00 PM" style descriptive range, computed server-side
    # from the requester's OWN assigned shift (see routers.leave.
    # _half_day_time_range) -- never a fixed/hardcoded time, and never
    # something the client can set. None whenever is_half_day is False, or
    # the employee has no active shift assignment to derive it from (no
    # invented default time is substituted in that case).
    half_day_time_range: str | None = None
    status: str
    certificate_file_names: list[str] = []
    # The caller may approve / reject / send back this request now (same
    # rule as the decision endpoint and the Approvals inbox).
    can_decide: bool = False
    # Reporting Manager Approval Workflow -- who decided this, when, and
    # with what comments (see backend/db/add_approval_decision_fields.sql).
    approver_id: uuid.UUID | None = None
    approver_name: str = "—"
    decision_notes: str | None = None
    decided_at: datetime.datetime | None = None
    # My Leave Requests needs the applied date (when this was submitted,
    # see backend/db/add_leave_request_applied_date.sql) and the assigned
    # Reporting Manager's name (who it was routed to -- distinct from
    # approver_name above, who actually decided it; usually the same
    # person, but not always, e.g. an Owner/RBAC-admin override).
    applied_date: datetime.datetime | None = None
    reporting_manager_name: str = "—"
    # Two Reporting Managers: classifies approver_id against this employee's
    # own reporting_manager_id/dotted_line_manager_id (see
    # crud.decided_by_manager_type) -- "Reporting Manager", "Dotted-Line
    # Manager", "Other Approver" (higher-level manager or admin override), or
    # None until decided.
    decided_by_manager_type: str | None = None
    # Set when this request was auto-split for exceeding available balance
    # (see backend/db/add_leave_request_link.sql) -- points at the sibling
    # leg (e.g. the Loss of Pay half), decided together as one unit.
    linked_request_id: uuid.UUID | None = None


class LeaveRequestUpdate(BaseModel):
    # 'sent_back' = Send Back for Revision, alongside the existing values.
    status: str = Field(pattern="^(approved|rejected|sent_back|l1_approved)$")
    decision_notes: str | None = Field(default=None, max_length=2000)


class AuditLogOut(BaseModel):
    ts: str
    actor: str
    action: str
    entity: str
    module: str
    ip: str


class NotificationOut(BaseModel):
    """Reporting Manager Approval Workflow's in-app notification feed --
    a new request awaiting your review, or a decision on your own request."""

    id: uuid.UUID
    title: str
    body: str
    entity_type: str | None = None
    entity_id: uuid.UUID | None = None
    read_at: datetime.datetime | None = None
    created_at: datetime.datetime

    model_config = {"from_attributes": True}


class AttachmentOut(BaseModel):
    id: uuid.UUID
    file_name: str
    file_url: str
    mime_type: str | None = None
    size_bytes: int | None = None


class SalaryComponentOut(BaseModel):
    id: uuid.UUID
    name: str
    code: str
    component_type: str
    calc_type: str
    is_taxable: bool
    # Set when the payroll engine adds this component by itself (overtime,
    # reimbursements, LOP, ...) -- it can't be put in a salary structure.
    automatic_reason: str | None = None
    custom_fields: dict[str, str] = {}

    model_config = {"from_attributes": True}

    @model_validator(mode="after")
    def _automatic(self):
        from .payroll_rules import automatic_reason
        self.automatic_reason = automatic_reason(self.name, self.code)
        return self


class SalaryComponentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    code: str = Field(min_length=1, max_length=15)
    # 'earning'/'deduction' are the original values; 'benefit' (employer-
    # cost, never touches employee net pay) and 'tax' (deduction computed
    # via the TDS estimate, or an admin-configured fixed/percent override)
    # are additive, introduced for Two Reporting Managers-era payroll work.
    # Validated against the company's ACTIVE Form Configuration options
    # (salary_component.type: earning|deduction|benefit|tax, relabel/hide/
    # reorder only -- the engine computes from these exact keys) by
    # routers/payroll.py, not by a hard-coded pattern here.
    component_type: str = Field(min_length=1, max_length=20)
    # 'stat_pf' = statutory EPF: the engine computes MIN(Basic,
    # company's pf_wage_ceiling) * 12% itself -- a structure line using
    # this component carries no amount/percent_of/percent at all.
    # 'var_annual' = a pure annual entitlement (structure line's amount * 12)
    # that is NEVER auto-divided into a monthly payment -- it only shows a
    # non-zero amount on a payslip once HR/Owner/Payroll Admin confirms a
    # specific VariablePayPayout for that employee's specific payroll run
    # (see crud.create_variable_pay_payout / _compute_and_write_slip).
    # 'stat_esi' = statutory employee ESI (0.75% of gross while gross is
    # within the 21,000 wage ceiling) -- N-06.
    # salary_component.calculation: percent|flat|stat_pf|stat_esi|var_annual.
    calc_type: str = Field(min_length=1, max_length=20)
    is_taxable: bool = True
    # Admin-defined custom fields (Form Configuration, form "salary_component").
    custom_fields: dict[str, Any] | None = None


class SalaryComponentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    component_type: str | None = Field(default=None, min_length=1, max_length=20)
    calc_type: str | None = Field(default=None, min_length=1, max_length=20)
    is_taxable: bool | None = None
    custom_fields: dict[str, Any] | None = None


class SalaryStructureComponentOut(BaseModel):
    name: str
    type: str
    amount: float
    taxable: str


class SalaryStructureOut(BaseModel):
    structure_name: str
    components: list[SalaryStructureComponentOut]


class SalaryStructureLineIn(BaseModel):
    component_id: uuid.UUID
    amount: float | None = None
    # 'balance' = "whatever's left of monthly CTC after every other earning
    # line" -- at most one per structure (create_salary_structure/
    # update_salary_structure reject a second one).
    percent_of: str | None = Field(default=None, pattern="^(ctc|basic|balance)$")
    percent: float | None = Field(default=None, ge=0, le=100)


class SalaryStructureLineOut(BaseModel):
    component_id: uuid.UUID
    component_name: str
    component_type: str
    calc_type: str
    amount: float | None
    percent_of: str | None
    percent: float | None


class SalaryStructureCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    # Omitted = today (company timezone).
    effective_from: datetime.date | None = None
    lines: list[SalaryStructureLineIn] = Field(min_length=1)
    # Phase 4: optional eligibility tags -- null means "applies to everyone".
    department_id: uuid.UUID | None = None
    designation_id: uuid.UUID | None = None
    branch_id: uuid.UUID | None = None
    grade: str | None = Field(default=None, max_length=50)


class SalaryStructureUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    effective_from: datetime.date | None = None
    lines: list[SalaryStructureLineIn] | None = None
    department_id: uuid.UUID | None = None
    designation_id: uuid.UUID | None = None
    branch_id: uuid.UUID | None = None
    grade: str | None = Field(default=None, max_length=50)


class SalaryStructureListItemOut(BaseModel):
    id: uuid.UUID
    name: str
    effective_from: datetime.date
    is_active: bool
    lines: list[SalaryStructureLineOut]
    department_id: uuid.UUID | None = None
    department_name: str | None = None
    designation_id: uuid.UUID | None = None
    designation_name: str | None = None
    branch_id: uuid.UUID | None = None
    branch_name: str | None = None
    grade: str | None = None


class SalaryStructureAssignmentCreate(BaseModel):
    employee_id: uuid.UUID
    structure_id: uuid.UUID
    # Omitted = today (company timezone).
    from_date: datetime.date | None = None
    annual_ctc: float = Field(gt=0)
    base_amount: float | None = Field(default=None, gt=0)


class SalaryStructureAssignmentOut(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    structure_id: uuid.UUID
    from_date: datetime.date
    base_amount: float
    annual_ctc: float | None
    # Set only when another assignment on this same employee has a LATER
    # from_date -- that one, not this one, is what "My Salary Structure"
    # and payroll runs currently treat as effective. Lets the Owner catch
    # this immediately instead of being confused why nothing changed.
    superseded_by_later_from_date: datetime.date | None = None
    superseded_by_later_annual_ctc: float | None = None

    model_config = {"from_attributes": True}


class VariablePayPayoutCreate(BaseModel):
    employee_id: uuid.UUID
    component_id: uuid.UUID
    payroll_run_id: uuid.UUID
    amount: float = Field(gt=0)
    notes: str | None = Field(default=None, max_length=300)


class VariablePayPayoutOut(BaseModel):
    id: uuid.UUID
    payroll_run_id: uuid.UUID
    period_month: int
    period_year: int
    amount: float
    created_at: datetime.datetime
    notes: str | None = None


class VariablePaySummaryOut(BaseModel):
    component_id: uuid.UUID
    component_name: str
    structure_assignment_id: uuid.UUID
    fiscal_year_start: datetime.date
    fiscal_year_end: datetime.date
    annual_entitlement: float
    amount_paid: float
    amount_remaining: float
    payouts: list[VariablePayPayoutOut]


class SalaryAssignmentOverrideIn(BaseModel):
    component_id: uuid.UUID
    override_type: str = Field(pattern="^(amount|percent)$")
    # L-21: bounded -- a monthly amount up to 1 crore, a percent up to 100.
    value: float = Field(ge=0, le=10_000_000)
    reason: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _percent_bound(self):
        if self.override_type == "percent" and self.value > 100:
            raise ValueError("A percent override can't exceed 100.")
        return self


class SalaryAssignmentOverrideOut(BaseModel):
    id: uuid.UUID
    assignment_id: uuid.UUID
    component_id: uuid.UUID
    component_name: str
    override_type: str
    value: float
    reason: str | None

    model_config = {"from_attributes": True}


class PayslipTemplateOut(BaseModel):
    template: dict


class PayslipTemplateUpdate(BaseModel):
    template: dict


class PayslipHtmlTemplateOut(BaseModel):
    id: uuid.UUID
    name: str
    html_body: str
    css_styles: str
    is_active: bool
    version: int
    updated_at: datetime.datetime | None = None

    model_config = {"from_attributes": True}


class PayslipHtmlTemplateUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    html_body: str = Field(min_length=1)
    css_styles: str = ""
    is_active: bool = True


class PayslipHtmlTemplatePreviewRequest(BaseModel):
    html_body: str = Field(min_length=1)
    css_styles: str = ""
    # Real employee to substitute actual payroll data from -- Live Preview
    # never fabricates sample values (see payslip_html_renderer.py).
    employee_id: uuid.UUID


class PayslipHtmlTemplatePreviewOut(BaseModel):
    rendered_html: str


class PayrollRunCreate(BaseModel):
    period_month: int = Field(ge=1, le=12)
    period_year: int = Field(ge=2000, le=2100)


class PayrollRunOut(BaseModel):
    id: uuid.UUID
    run_no: str
    period_month: int
    period_year: int
    status: str
    # H-11 period control: draft -> processed (generated) -> approved ->
    # locked -> paid; users are public user ids.
    generated_by: uuid.UUID | None = None
    generated_at: datetime.datetime | None = None
    approved_by: uuid.UUID | None = None
    approved_at: datetime.datetime | None = None
    locked_by: uuid.UUID | None = None
    locked_at: datetime.datetime | None = None
    paid_by: uuid.UUID | None = None
    paid_at: datetime.datetime | None = None
    # H-16: slips whose deductions were capped at gross pay.
    flagged_slips: int = 0

    model_config = {"from_attributes": True}


class PayrollGenerateResultOut(BaseModel):
    run: PayrollRunOut
    slips_generated: int
    flagged_slips: int = 0


class PayrollDeductionSettingsOut(BaseModel):
    """Payroll Settings > Deductions Configuration."""

    lop_deduction_enabled: bool
    asset_deduction_enabled: bool
    # TDS % on a rate-paid Freelancer's fee; null / 0 = none.
    freelancer_tds_percent: float | None = None


class PayrollDeductionSettingsUpdate(BaseModel):
    lop_deduction_enabled: bool | None = None
    asset_deduction_enabled: bool | None = None
    freelancer_tds_percent: float | None = Field(default=None, ge=0, le=30)


class EmployeeRateHistoryOut(BaseModel):
    id: uuid.UUID
    rate_amount: float
    rate_unit: str
    # ISO 4217 (never NULL here: a NULL column is the company's default).
    currency_code: str | None = None
    effective_from: datetime.date
    notes: str | None = None
    created_by_name: str | None = None
    created_at: datetime.datetime | None = None


class EmployeeRateOut(BaseModel):
    """GET/POST /api/employees/{id}/rate -- a Freelancer's pay rate."""

    employee_id: uuid.UUID
    employment_type: str | None = None
    rate_amount: float | None = None
    rate_unit: str | None = None
    currency_code: str | None = None
    # The company's payroll currency; a rate in any other currency isn't
    # converted, so payroll leaves that freelancer out (paid outside it).
    payroll_currency: str | None = None
    effective_from: datetime.date | None = None
    can_edit: bool
    history: list[EmployeeRateHistoryOut]


class EmployeeRateUpdate(BaseModel):
    rate_amount: float = Field(gt=0, le=10000000)
    rate_unit: str = Field(default="hourly", pattern="^(hourly|daily)$")
    # ISO 4217; omitted = keep the current rate's currency (else the
    # company's default).
    currency_code: str | None = Field(default=None, max_length=3)
    effective_from: datetime.date | None = None
    notes: str | None = Field(default=None, max_length=1000)


class FreelancerRateRow(BaseModel):
    employee_id: uuid.UUID
    employee_name: str
    employee_code: str | None = None
    rate_amount: float | None = None
    rate_unit: str | None = None
    currency_code: str | None = None
    effective_from: datetime.date | None = None
    approved_hours: float
    estimated_payout: float | None = None
    estimated_tds: float | None = None
    has_rate: bool


class FreelancerRatesOut(BaseModel):
    """GET /api/payroll/freelancer-rates."""

    from_date: datetime.date
    to_date: datetime.date
    tds_percent: float | None = None
    rows: list[FreelancerRateRow]


class PayrollReimbursementInclusionCreate(BaseModel):
    """Payroll > Travel & Expense Reimbursements -- HR schedules one
    already-fully-approved Travel Requisition or Expense Report/
    Reimbursement into a specific payroll period. [source_id] must point
    at a real hcm_travel_requests or hcm_expense_claims row (matching
    [source_type]) already at status='approved'."""

    source_type: str = Field(pattern="^(travel_request|expense_claim)$")
    source_id: uuid.UUID
    target_period_month: int = Field(ge=1, le=12)
    target_period_year: int = Field(ge=2000, le=2100)


class PayrollReimbursementItemOut(BaseModel):
    """One row of the unified Travel Requisition + Expense Report list --
    every fully-approved record of either real source type, whether or
    not HR has scheduled it into payroll yet ([target_period_month]/
    [inclusion_id] are None until they do)."""

    source_type: str
    source_id: uuid.UUID
    employee_id: uuid.UUID
    employee_name: str
    request_code: str
    type_label: str
    approved_amount: float
    approval_date: datetime.datetime | None
    target_period_month: int | None
    target_period_year: int | None
    payment_status: str
    inclusion_id: uuid.UUID | None
    applied_payroll_run_id: uuid.UUID | None
    # Review state once scheduled (None while Not Scheduled): the amount
    # payroll pays, 'included'/'excluded', whether a payroll run's
    # automatic identification scheduled it, HR's note, and whether it can
    # still be changed (False once paid).
    payroll_amount: float | None = None
    inclusion_status: str | None = None
    auto_included: bool = False
    notes: str | None = None
    editable: bool = True


class PayrollReimbursementInclusionUpdate(BaseModel):
    """Review before payout: edit the amount (0 < amount <= approved),
    include/exclude, defer to another payroll period, and/or note why."""

    amount: float | None = Field(default=None, gt=0)
    status: str | None = Field(default=None, pattern="^(included|excluded)$")
    target_period_month: int | None = Field(default=None, ge=1, le=12)
    target_period_year: int | None = Field(default=None, ge=2000, le=2100)
    notes: str | None = Field(default=None, max_length=500)


class PayrollReimbursementComponentOut(BaseModel):
    """Payroll > Salary Structure > Reimbursement Components. configured
    is False while the company still uses the built-in default."""

    source_type: str
    display_name: str
    is_enabled: bool
    auto_include: bool
    max_amount_per_request: float | None
    configured: bool
    updated_at: datetime.datetime | None = None


class PayrollReimbursementComponentUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=40)
    is_enabled: bool | None = None
    auto_include: bool | None = None
    # null clears the cap.
    max_amount_per_request: float | None = Field(default=None, gt=0)


class PayrollReimbursementIdentifyRequest(BaseModel):
    period_month: int = Field(ge=1, le=12)
    period_year: int = Field(ge=2000, le=2100)


class PayrollReimbursementIdentifyOut(BaseModel):
    identified: int
    items: list[PayrollReimbursementItemOut]


class ReimbursementCreate(BaseModel):
    """Maps onto a single-line hcm.expense_claims row -- Payroll's
    Reimbursement is a simpler one-category-one-amount claim than Travel &
    Expenses' fuller multi-line Expense Report, but both share this table."""

    employee_id: uuid.UUID
    # Optional: omitted / blank = "General".
    category: str = Field(default="General", min_length=1, max_length=200)
    # M-18: NUMERIC(14,2) columns (claim total + line amount).
    amount: float = Field(gt=0, le=MAX_NUMERIC_14_2)
    # Omitted = today.
    expense_date: datetime.date = Field(default_factory=datetime.date.today)

    @model_validator(mode="after")
    def _not_future(self):
        # L-19: no future-dated reimbursements (1 day of slack for time zones).
        if self.expense_date > _today_with_slack(1):
            raise ValueError("Expense date cannot be in the future")
        return self


class ReimbursementOut(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    category: str
    amount: float
    expense_date: datetime.date
    status: str
    approver_id: uuid.UUID | None = None
    approver_name: str = "—"
    decision_notes: str | None = None
    decided_at: datetime.datetime | None = None
    decided_by_manager_type: str | None = None


class ReimbursementUpdate(BaseModel):
    status: str = Field(pattern="^(approved|rejected|sent_back)$")
    decision_notes: str | None = Field(default=None, max_length=2000)


class LoanOut(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    loan_type: str
    principal_amount: float
    emi_amount: float | None
    outstanding_balance: float
    status: str

    model_config = {"from_attributes": True}


class LoanCreate(BaseModel):
    employee_id: uuid.UUID
    # Optional: omitted / blank = "General".
    loan_type: str = Field(default="General", min_length=1, max_length=60)
    # M-31: positive, bounded amounts; EMI <= principal, outstanding <= principal.
    principal_amount: float = Field(gt=0, le=100_000_000)
    emi_amount: float | None = Field(default=None, gt=0, le=100_000_000)
    # Omitted = the full principal (nothing repaid yet).
    outstanding_balance: float | None = Field(default=None, ge=0, le=100_000_000)

    @model_validator(mode="after")
    def _consistent(self):
        if self.outstanding_balance is None:
            self.outstanding_balance = self.principal_amount
        if self.outstanding_balance > self.principal_amount:
            raise ValueError("Outstanding balance can't exceed the principal amount.")
        if self.emi_amount is not None and self.emi_amount > self.principal_amount:
            raise ValueError("EMI can't exceed the principal amount.")
        return self


class TaxDeclarationOut(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    fiscal_year: str
    tax_regime: str
    hra_claimed: float | None = None
    section_80c: float | None = None
    section_80d: float | None = None
    section_80ccd_1b: float | None = None
    home_loan_interest: float | None = None
    status: str

    model_config = {"from_attributes": True}


class TaxDeclarationCreate(BaseModel):
    """M-28: non-negative amounts with the statutory caps (see
    app/tax_engine.py) -- HRA is further limited to the HRA received and
    50% of Basic when TDS is computed."""
    employee_id: uuid.UUID
    fiscal_year: str = Field(pattern=r"^\d{4}-(\d{2}|\d{4})$")
    # Omitted = the new regime (the statutory default).
    tax_regime: str = Field(default="new", pattern="^(old|new)$")
    hra_claimed: float = Field(default=0, ge=0, le=10_000_000)
    section_80c: float = Field(default=0, ge=0, le=150_000)
    section_80d: float = Field(default=0, ge=0, le=100_000)
    section_80ccd_1b: float = Field(default=0, ge=0, le=50_000)
    home_loan_interest: float = Field(default=0, ge=0, le=200_000)


class JobOpeningOut(BaseModel):
    title: str
    dept: str
    type: str
    openings: int
    status: str
    posted: str
    description: str | None = None


class JobOpeningCreate(BaseModel):
    # Only the title is required: no department, Full-time and 1 opening
    # when left out (a given department must exist).
    title: str = Field(min_length=1, max_length=150)
    department_name: str | None = Field(default=None, max_length=120)
    employment_type: str = Field(default="Full-time", min_length=1, max_length=20)
    openings: int = Field(default=1, ge=1)
    description: str | None = None


class CandidateCardOut(BaseModel):
    id: uuid.UUID
    name: str
    role: str
    exp: str


class CandidateStageUpdate(BaseModel):
    stage: str = Field(pattern="^(applied|screened|interview|offer|hired)$")


class OfferOut(BaseModel):
    id: uuid.UUID
    candidate: str
    role: str
    ctc: float
    joining: str
    status: str
    employment_type: str = ""


class OfferStatusUpdate(BaseModel):
    status: str = Field(pattern="^(accepted|rejected|withdrawn)$")


class OfferLetterHtmlTemplateOut(BaseModel):
    id: uuid.UUID
    name: str
    html_body: str
    css_styles: str
    is_active: bool
    version: int
    updated_at: datetime.datetime | None = None

    model_config = {"from_attributes": True}


class OfferLetterHtmlTemplateUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    html_body: str = Field(min_length=1)
    css_styles: str = ""
    is_active: bool = True


class OfferLetterHtmlTemplatePreviewRequest(BaseModel):
    html_body: str = Field(min_length=1)
    css_styles: str = ""
    # Real offer to substitute actual candidate/offer data from -- Live
    # Preview never fabricates sample values.
    offer_id: uuid.UUID


class OfferLetterHtmlTemplatePreviewOut(BaseModel):
    rendered_html: str


class EmailHtmlTemplateOut(BaseModel):
    id: uuid.UUID
    email_kind: str
    name: str
    html_body: str
    css_styles: str
    is_active: bool
    version: int
    updated_at: datetime.datetime | None = None

    model_config = {"from_attributes": True}


class EmailHtmlTemplateUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    html_body: str = Field(min_length=1)
    css_styles: str = ""
    is_active: bool = True


class EmailHtmlTemplatePreviewRequest(BaseModel):
    html_body: str = Field(min_length=1)
    css_styles: str = ""


class EmailHtmlTemplatePreviewOut(BaseModel):
    rendered_html: str


class ContractRenewRequest(BaseModel):
    new_end_date: datetime.date
    new_rate_amount: float | None = Field(default=None, gt=0)
    new_rate_unit: str | None = Field(default=None, pattern="^(hourly|daily|monthly)$")
    notes: str | None = Field(default=None, max_length=1000)


class ContractConvertRequest(BaseModel):
    notes: str | None = Field(default=None, max_length=1000)


class ContractExpireRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


class ContractActionOut(BaseModel):
    employee_id: uuid.UUID
    employment_type: str
    contract_end_date: datetime.date | None
    contract_rate_amount: float | None
    contract_rate_unit: str | None
    event_id: uuid.UUID
    # Conversion only: payroll needs a salary structure for this employee.
    needs_salary_structure: bool = False
    # 'active' | 'expired' (None after conversion to permanent).
    contract_status: str | None = None


class TransferEventOut(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    type: str
    from_value: str
    to_value: str
    effective_date: datetime.date
    status: str = "Approved"
    employee_name: str | None = None
    employee_code: str | None = None


class NamedRef(BaseModel):
    id: uuid.UUID
    name: str


class LifecycleEventDetailOut(BaseModel):
    """People > Transfers & Promotions > (click a row) -- every field the
    hcm.employee_lifecycle_events row actually has, both raw ids (for the
    Edit form's dropdowns) and resolved display names (for the read view)."""

    id: uuid.UUID
    employee_id: uuid.UUID
    employee_name: str
    employee_code: str
    type: str
    event_date: datetime.date
    status: str = "Approved"

    from_designation: NamedRef | None = None
    to_designation: NamedRef | None = None
    from_department: NamedRef | None = None
    to_department: NamedRef | None = None
    from_branch: NamedRef | None = None
    to_branch: NamedRef | None = None
    from_business_unit: NamedRef | None = None
    to_business_unit: NamedRef | None = None
    from_sub_department: NamedRef | None = None
    to_sub_department: NamedRef | None = None
    from_reporting_manager: NamedRef | None = None
    to_reporting_manager: NamedRef | None = None
    from_dotted_line_manager: NamedRef | None = None
    to_dotted_line_manager: NamedRef | None = None
    from_ctc: int | None = None
    to_ctc: int | None = None


class LifecycleEventUpdate(BaseModel):
    """People > Transfers & Promotions > Edit. Every field optional/partial
    (exclude_unset) -- only fields the client actually sends are changed,
    same convention as EmployeeTransferRequest. Edits the historical row
    itself only -- never re-applies to the employee's current live record
    (that would risk clobbering a *later* transfer's effect) and never
    touches any other hcm.employee_lifecycle_events row, so existing
    history is never overwritten by an edit here."""

    event_date: datetime.date | None = None
    from_designation_id: uuid.UUID | None = None
    to_designation_id: uuid.UUID | None = None
    from_department_id: uuid.UUID | None = None
    to_department_id: uuid.UUID | None = None
    from_branch_id: uuid.UUID | None = None
    to_branch_id: uuid.UUID | None = None
    from_business_unit_id: uuid.UUID | None = None
    to_business_unit_id: uuid.UUID | None = None
    from_sub_department_id: uuid.UUID | None = None
    to_sub_department_id: uuid.UUID | None = None
    from_reporting_manager_id: uuid.UUID | None = None
    to_reporting_manager_id: uuid.UUID | None = None
    from_dotted_line_manager_id: uuid.UUID | None = None
    to_dotted_line_manager_id: uuid.UUID | None = None
    from_ctc: int | None = Field(default=None, ge=0)
    to_ctc: int | None = Field(default=None, ge=0)


class ExitRequestOut(BaseModel):
    # The exit request's own id (what its notifications / approval item
    # point at), for record-level links to People > Offboarding.
    id: uuid.UUID | None = None
    employee_id: uuid.UUID
    last_working_day: datetime.date | None
    notice_period_days: int
    final_settlement_review: str
    access_revocation: str
    knowledge_transfer: str
    asset_return: str
    fnf_status: str
    employee_name: str | None = None
    employee_code: str | None = None


class ExitRequestCreate(BaseModel):
    employee_id: uuid.UUID
    # Omitted = today.
    resignation_date: datetime.date = Field(default_factory=datetime.date.today)
    last_working_day: datetime.date | None = None
    reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _dates_in_order(self):
        # M-37: resignation <= last working day (joining <= resignation is
        # checked against the employee row in crud.create_exit_request).
        if self.last_working_day is not None and self.last_working_day < self.resignation_date:
            raise ValueError("Last working day can't be before the resignation date.")
        return self


class ExitRequestDecisionUpdate(BaseModel):
    status: str = Field(pattern="^(approved|rejected|sent_back)$")
    decision_notes: str | None = Field(default=None, max_length=2000)
    last_working_day: datetime.date | None = None


class ExitRequestRecordOut(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    employee_id: uuid.UUID
    resignation_date: datetime.date
    last_working_day: datetime.date | None
    reason: str | None
    status: str
    approver_id: uuid.UUID | None = None
    approver_name: str = "—"
    decision_notes: str | None = None
    decided_at: datetime.datetime | None = None
    decided_by_manager_type: str | None = None


class InterviewOut(BaseModel):
    id: uuid.UUID
    candidate: str
    role: str
    interviewer: str
    when: str
    round: str
    status: str
    feedback: str | None = None


class InterviewOutcomeUpdate(BaseModel):
    result: str = Field(pattern="^(pass|fail)$")
    feedback: str | None = Field(default=None, max_length=2000)


class OkrOut(BaseModel):
    # Form Configuration: id lets the UI match custom field values; level is
    # the stored option value, level_label the admin-configured display label.
    id: uuid.UUID | None = None
    level: str
    level_label: str | None = None
    custom_fields: dict[str, str] = {}
    title: str
    owner: str
    progress: int
    # FE4: match goals to people by id, not display name (None = legacy row).
    owner_employee_id: uuid.UUID | None = None


class OkrCreate(BaseModel):
    # Validated against the company's Goal Level options (Form Configuration).
    # Omitted = Individual.
    level: str = Field(default="Individual", min_length=1, max_length=40)
    custom_fields: dict[str, object] | None = None
    title: str = Field(min_length=1, max_length=255)
    # Optional: omitted = owner_employee_id's name, else the creator's own.
    owner: str | None = Field(default=None, max_length=150)
    # FE4: when given, must be an employee of the caller's company; the
    # stored owner name is then taken from that employee.
    owner_employee_id: uuid.UUID | None = None


class ReviewCycleOut(BaseModel):
    id: uuid.UUID
    name: str
    type: str
    # status is the configured display label; status_key the stored value.
    status: str
    status_key: str | None = None
    due: str
    participants: int
    custom_fields: dict[str, str] = {}


class ReviewCycleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    # Optional (the columns are NOT NULL): from_date defaults to today and
    # to_date to one year after from_date (minus a day).
    from_date: datetime.date | None = None
    to_date: datetime.date | None = None
    # Validated against the company's Review Cycle Status options.
    status: str = Field(default="scheduled", min_length=1, max_length=30)
    participant_count: int = Field(default=0, ge=0, le=1_000_000)  # L-27
    custom_fields: dict[str, object] | None = None

    @model_validator(mode="after")
    def _check_date_order(self):
        if self.from_date is None:
            self.from_date = datetime.date.today()
        if self.to_date is None:
            try:
                self.to_date = self.from_date.replace(year=self.from_date.year + 1) - datetime.timedelta(days=1)
            except ValueError:  # 29 Feb
                self.to_date = self.from_date + datetime.timedelta(days=364)
        if self.to_date < self.from_date:
            raise ValueError("to_date cannot be before from_date")
        return self


class CourseOut(BaseModel):
    id: uuid.UUID | None = None
    custom_fields: dict[str, str] = {}
    name: str
    type: str
    duration: str
    category: str
    enrolled_count: int = 0
    avg_completion_pct: int = 0
    provider: str | None = None


class CertificationOut(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    name: str
    issuer: str | None
    issue_date: datetime.date | None
    expiry_date: datetime.date | None


class CertificationCreate(BaseModel):
    employee_id: uuid.UUID
    name: str = Field(min_length=1, max_length=200)
    issuer: str | None = Field(default=None, max_length=150)
    issue_date: datetime.date | None = None
    expiry_date: datetime.date | None = None

    @model_validator(mode="after")
    def _expiry_after_issue(self):  # L-28
        if self.issue_date and self.expiry_date and self.expiry_date < self.issue_date:
            raise ValueError("Expiry date can't be before the issue date.")
        return self


class CourseCreate(BaseModel):
    # Only the course name is required; a given Type must be an active
    # option and a given Duration a positive number.
    name: str = Field(min_length=1, max_length=600)
    type: str | None = Field(default=None, max_length=60)
    custom_fields: dict[str, object] | None = None
    duration: str | None = Field(default=None, max_length=30)

    @field_validator("duration")
    @classmethod
    def _positive_duration(cls, value: str | None) -> str | None:  # L-29
        import re as _re

        if value is None or not value.strip():
            return None
        match = _re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([A-Za-z][A-Za-z .]*)?\s*", value)
        if not match or float(match.group(1)) <= 0:
            raise ValueError("Duration must be a positive number, optionally with a unit (e.g. '16 hrs', '3 days').")
        return value.strip()
    category: str | None = Field(default=None, max_length=60)
    provider: str | None = Field(default=None, max_length=150)


class BenefitCategoryItemOut(BaseModel):
    id: uuid.UUID
    item: str


class BenefitCategoryOut(BaseModel):
    id: uuid.UUID
    name: str
    is_active: bool = True
    items: list[BenefitCategoryItemOut]
    # Which specific employees this plan is assigned to/visible to -- see
    # BenefitCategoryAssignment. Empty means visible to no one yet (never
    # "everyone" by default).
    employee_ids: list[uuid.UUID] = []


class BenefitCategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    # Optional: a plan may start with no items (add them later).
    items: list[str] = Field(default_factory=list)
    # Explicit "Assign to Employees" selection -- defaults to nobody, never
    # auto-assigned to the whole company, per the admin's actual selection.
    employee_ids: list[uuid.UUID] = []


class BenefitCategoryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    is_active: bool | None = None
    # Present (even as []) replaces the plan's full assignment list; absent
    # (the default) leaves current assignments untouched -- same
    # exclude_unset convention as name/is_active above.
    employee_ids: list[uuid.UUID] | None = None


class BenefitCategoryItemCreate(BaseModel):
    item: str = Field(min_length=1, max_length=200)


class BenefitCategoryItemUpdate(BaseModel):
    item: str = Field(min_length=1, max_length=200)


class BenefitsEnrollmentOut(BaseModel):
    employee_id: uuid.UUID
    insurance_plan: str | None
    esop_units: int
    dependents_covered: int
    learning_budget_total: float
    learning_budget_used: float
    cab_facility: bool
    meal_card: bool
    internet_reimbursement: bool
    wellness_program: bool

    model_config = {"from_attributes": True}


class BenefitsEnrollmentUpdate(BaseModel):
    employee_id: uuid.UUID
    insurance_plan: str | None = Field(default=None, max_length=150)
    esop_units: int | None = None
    dependents_covered: int | None = None
    learning_budget_total: float | None = None
    learning_budget_used: float | None = None
    cab_facility: bool | None = None
    meal_card: bool | None = None
    internet_reimbursement: bool | None = None
    wellness_program: bool | None = None


class AssetInventoryItemOut(BaseModel):
    id: uuid.UUID
    tag: str
    type: str
    model: str | None = None
    # status is the configured display label; status_key the stored value.
    status: str
    status_key: str | None = None
    value: str | None = None
    purchased: str
    # Asset Category: label (Personal / Office / Client, "—" for legacy
    # assets) and the stored key.
    category: str = "—"
    category_key: str | None = None
    # Optional details (None = not entered).
    name: str | None = None
    serial_number: str | None = None
    description: str | None = None
    custom_fields: dict[str, str] = {}
    images: list["AssetImageOut"] = []


class AssetImageOut(BaseModel):
    id: uuid.UUID
    # /media/asset_image/<asset_id>/<file> -- signed (?t=) by the media-URL
    # middleware on the way out, so an <img> can load it.
    url: str
    name: str
    size_bytes: int = 0
    # "image" (PNG/JPG, shown in the gallery) or "document" (PDF, Word...).
    kind: str = "image"
    ext: str = ""


AssetInventoryItemOut.model_rebuild()


ASSET_CATEGORIES: dict[str, str] = {"personal": "Personal", "office": "Office", "client": "Client"}


def _asset_category_key(v: str | None) -> str | None:
    """Blank -> None (not set); otherwise personal / office / client (key or
    label, case-insensitive)."""
    if v is None or not v.strip():
        return None
    key = v.strip().lower()
    if key not in ASSET_CATEGORIES:
        raise ValueError("Asset Category must be one of: Personal, Office, Client.")
    return key


class AssetInventoryItemCreate(BaseModel):
    # Every field is optional. Status defaults to Available; Type / Category
    # left out are simply not set; a given value / purchase date / category
    # is still format-checked. Tag (the asset's identifier) is normally left
    # out: it's generated from Administration > General Settings > Document
    # ID Formats > Asset ID (e.g. AST-001). A given tag must be unused.
    tag: str | None = Field(default=None, max_length=40)
    type: str | None = Field(default=None, max_length=60)
    category: str | None = Field(default=None, max_length=15)
    name: str | None = Field(default=None, max_length=200)
    serial_number: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=4000)
    model: str | None = Field(default=None, max_length=120)
    status: str | None = Field(default=None, max_length=15)
    value: str | None = None
    purchased: str | None = None
    custom_fields: dict[str, object] | None = None

    _category = field_validator("category")(classmethod(lambda cls, v: _asset_category_key(v)))


class AssetInventoryItemUpdate(BaseModel):
    """Edit Asset: only the fields sent change; blank / null clears an
    optional field. Nothing is required."""
    type: str | None = Field(default=None, max_length=60)
    category: str | None = Field(default=None, max_length=15)
    name: str | None = Field(default=None, max_length=200)
    serial_number: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=4000)
    model: str | None = Field(default=None, max_length=120)
    status: str | None = Field(default=None, max_length=15)
    value: str | None = None
    purchased: str | None = None
    custom_fields: dict[str, object] | None = None

    _category = field_validator("category")(classmethod(lambda cls, v: _asset_category_key(v)))


class AssetInventoryItemStatusUpdate(BaseModel):
    status: str = Field(min_length=1, max_length=15)


class AssetAssignmentOut(BaseModel):
    id: uuid.UUID
    asset_tag: str
    asset_type: str
    employee_id: uuid.UUID
    employee_name: str
    assigned_on: datetime.date
    returned_on: datetime.date | None
    return_condition: str | None
    return_notes: str | None


class AssetAssignmentCreate(BaseModel):
    asset_tag: str = Field(min_length=1, max_length=40)
    employee_id: uuid.UUID
    # Omitted = today (company timezone).
    assigned_on: datetime.date | None = None


class AssetReturnUpdate(BaseModel):
    # Omitted = today (company timezone).
    returned_on: datetime.date | None = None
    return_condition: str | None = Field(default=None, max_length=120)
    return_notes: str | None = None


class PolicyDocOut(BaseModel):
    id: uuid.UUID
    name: str
    version: str
    effective: str
    ack: str
    file_url: str | None = None


class DocumentRecordOut(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    document_type: str
    status: str
    uploaded_on: datetime.date
    file_url: str | None = None

    model_config = {"from_attributes": True}


class ProjectOut(BaseModel):
    id: uuid.UUID
    name: str
    # Real pm_projects columns -- see routers/projects.py's _to_out.
    code: str = "—"
    description: str = "—"
    priority: str = "—"
    # client/type/budget have no backing column (see update_project's
    # "Drop fields removed from DB schema" comment) -- kept as "—"
    # placeholders so existing frontend code reading these fields is
    # unaffected, not because they're resolved from anywhere.
    client: str
    type: str
    pm: str
    status: str
    start: str
    end: str
    budget: str
    # The budget's raw amount and its ISO currency code, stored separately
    # (Create / Edit Project > Currency); `budget` is them formatted with
    # that currency's symbol. No currency is assumed or converted.
    budget_amount: float | None = None
    budget_currency: str | None = None
    progress: int
    # FK to the PM employee row. None when pm_name is a free-text legacy entry
    # or when the column hasn't been backfilled. Requires
    # backend/db/add_role_hierarchy.sql to have been applied.
    pm_employee_id: uuid.UUID | None = None
    # The project's single authoritative Team Lead (see
    # backend/db/add_project_team_lead.sql) -- distinct from
    # ProjectAllocationOut.team_lead, which is per-team-member.
    team_lead_id: uuid.UUID | None = None
    team_lead: str = "—"
    # The project's own organizational mapping (see
    # backend/db/add_project_org_mapping.sql) -- distinct from
    # ProjectAllocationOut's per-team-member department/branch.
    business_unit_id: uuid.UUID | None = None
    business_unit: str = "—"
    department_id: uuid.UUID | None = None
    department: str = "—"
    branch_id: uuid.UUID | None = None
    branch: str = "—"
    # Real pm_projects.is_active column -- lets callers (e.g. Add Work
    # Entry's Project picker) filter to active projects without a second
    # endpoint.
    is_active: bool = True


class EmployeeCandidateOut(BaseModel):
    """Lightweight row for role-scoped assignment dropdowns (Add Project's
    Project Manager / Team Lead pickers) -- see
    crud.list_employees_by_role."""

    id: uuid.UUID
    name: str
    designation: str = "—"
    department: str = "—"
    department_id: uuid.UUID | None = None
    business_unit_id: uuid.UUID | None = None
    branch: str = "—"
    branch_id: uuid.UUID | None = None
    # None for an employee without a login account (still a candidate).
    role: str | None = None


class ProjectCreate(BaseModel):
    # Only the name is required; everything else may be left out / blank.
    name: str = Field(min_length=1, max_length=200)
    client: str | None = Field(default=None, max_length=200)
    type: str | None = Field(default=None, max_length=40)
    pm: str | None = Field(default=None, max_length=150)
    # Real FK counterpart to the legacy free-text `pm` name above -- lets
    # Add Project wire the same Project Manager relationship Edit Project
    # already sets via project_manager_id, instead of only ever being set
    # on a later edit.
    project_manager_id: uuid.UUID | None = None
    # Add Project's Team Lead field, filtered client-side to employees whose
    # backend role is "Team Lead". Optional: none = no Team Lead tag yet.
    team_lead_id: uuid.UUID | None = None
    start: str | None = None
    end: str | None = None
    budget: str | None = None
    # ISO 4217 code the budget is in (INR, USD, EUR, GBP, ...) -- see
    # app/currencies.py. Stored next to the amount, never converted.
    budget_currency: str | None = Field(default=None, max_length=3)
    business_unit_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    branch_id: uuid.UUID | None = None


class ProjectUpdate(BaseModel):
    """Partial update — only the fields present are changed. Edit Project
    uses project_manager_id (the real FK, same one org_hierarchy.py reads)
    rather than the legacy free-text pm_name that ProjectCreate still
    writes -- Add Project's existing flow is unaffected."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    client: str | None = Field(default=None, min_length=1, max_length=200)
    type: str | None = Field(default=None, max_length=40)
    # Real pm_projects columns -- no existing dialog field edits these yet,
    # but the API accepts them (matching ProjectCreate) so they're not
    # silently dropped the moment a caller does send them.
    description: str | None = None
    priority: str | None = Field(default=None, max_length=20)
    project_manager_id: uuid.UUID | None = None
    team_lead_id: uuid.UUID | None = None
    start: str | None = None
    end: str | None = None
    budget: str | None = None
    budget_currency: str | None = Field(default=None, max_length=3)
    status: str | None = None
    business_unit_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    branch_id: uuid.UUID | None = None

    @field_validator("status")
    @classmethod
    def _valid_status(cls, v):
        if v is not None and v not in ("draft", "planning", "active", "on_hold", "completed", "cancelled"):
            raise ValueError("Unknown project status")
        return v


class ProjectCategoryOut(BaseModel):
    """Tenant-specific Project Type master row -- see
    crud.list_project_categories/create_project_category/
    update_project_category and the Manage Project Types dialog."""

    id: uuid.UUID
    name: str
    is_active: bool

    model_config = {"from_attributes": True}


class ProjectCategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class ProjectCategoryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    is_active: bool | None = None


class ProjectTaskOut(BaseModel):
    """Lightweight row for Add Work Entry's Task picker -- see
    crud.list_tasks_for_project."""

    id: uuid.UUID
    code: str
    title: str


class ProjectStatusHistoryOut(BaseModel):
    """Backs pm_project_status_history -- see
    crud.record_project_status_change/list_project_status_history."""

    id: uuid.UUID
    from_status: str | None
    to_status: str
    changed_by_name: str = "—"
    changed_at: datetime.datetime
    remarks: str | None = None


class ProjectAllocationOut(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    employee_name: str
    project: str
    project_code: str | None = None
    role: str | None
    allocation_pct: int
    is_billable: bool
    # Display names — "—" when not set. Requires the project-allocation
    # columns mapped on models.ProjectAllocation to exist in the tenant schema.
    team_lead: str = "—"
    department: str = "—"
    branch: str = "—"
    # GET /api/project-allocations (Work & Timesheet > My Allocation) only;
    # left unset by the older per-project endpoints.
    project_id: uuid.UUID | None = None
    project_status: str | None = None
    start_date: datetime.date | None = None
    end_date: datetime.date | None = None


class ProjectAllocationCreate(BaseModel):
    project_name: str = Field(min_length=1, max_length=200)
    employee_id: uuid.UUID
    role: str | None = Field(default=None, max_length=100)
    allocation_pct: int = Field(default=100, ge=1, le=100)
    is_billable: bool = True
    team_lead_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    branch_id: uuid.UUID | None = None


class WorkEntryCreate(BaseModel):
    employee_id: uuid.UUID
    entry_date: datetime.date
    # Preferred over project_name when present -- a real project_id avoids
    # the name-lookup's fragility (rename, casing, or a stale client-side
    # project list resolving to the wrong row or failing to resolve at
    # all). project_name is kept for backward compatibility with any
    # caller that only has a name.
    project_id: uuid.UUID | None = None
    project_name: str | None = None
    # Optional free-text task label (task_id below links a real task).
    task: str | None = Field(default=None, max_length=200)
    # Optional link to a real pm_tasks row on the same project (see
    # pm_time_entries.task_id, previously unmapped) -- the `task` free-text
    # field above has no backing column on the real table and was silently
    # discarded on every save; this is the schema-compliant way to record
    # which task the hours belong to. None keeps today's behavior (a
    # project-level time entry with no specific task) unchanged.
    task_id: uuid.UUID | None = None
    # Optional; when given it must be one of the company's active options.
    category: str | None = Field(default=None, max_length=40)
    start_time: datetime.time | None = None
    end_time: datetime.time | None = None
    # le=24 matches pm_time_entries' own CHECK (hours > 0 AND hours <= 24) --
    # without it, an hours value above 24 passed validation here and only
    # failed later as an uncaught IntegrityError in create_work_entry
    # (routers/work.py has no try/except around that call), surfacing to the
    # client as a raw 500 instead of a clean 422.
    hours: float = Field(gt=0, le=24)
    description: str | None = None
    is_billable: bool = True
    # Form Configuration: admin-defined custom fields {field_key: value}.
    custom_fields: dict[str, object] | None = None
    # Project > "Other": log time on an active company project the employee
    # isn't assigned to (the allocation check is skipped for it; the entry
    # is flagged is_other_project). False = an assigned project, as before.
    other_project: bool = False


class WorkEntryOut(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    entry_date: datetime.date
    project: str | None
    project_code: str | None = None
    task: str | None
    task_id: uuid.UUID | None = None
    category: str | None
    start_time: datetime.time | None
    end_time: datetime.time | None
    hours: float
    description: str | None
    is_billable: bool
    # Reporting Manager Approval Workflow -- see
    # backend/db/add_work_entry_and_overtime_approvals.sql.
    status: str = "pending"
    approver_id: uuid.UUID | None = None
    approver_name: str = "—"
    decision_notes: str | None = None
    decided_at: datetime.datetime | None = None
    # Form Configuration: the configured label of `category` and the
    # admin-defined custom field values {field_key: value}.
    category_label: str | None = None
    custom_fields: dict[str, str] = {}
    # True = logged via Project > "Other" (not an assigned project).
    is_other_project: bool = False


class OtherProjectOption(BaseModel):
    id: uuid.UUID
    code: str | None = None
    name: str


class WorkEntryUpdate(BaseModel):
    status: str = Field(pattern="^(approved|rejected|sent_back)$")
    decision_notes: str | None = Field(default=None, max_length=2000)


class WorkEntryFieldsUpdate(BaseModel):
    """Employee self-edit of their OWN Work Entry's actual field values --
    distinct from WorkEntryUpdate (an admin status-override). All fields
    optional/partial: only what's provided is changed. Mirrors
    WorkEntryCreate's shape minus employee_id (ownership is resolved from
    the URL's work_entry_id, never client-supplied)."""
    entry_date: datetime.date | None = None
    project_id: uuid.UUID | None = None
    project_name: str | None = Field(default=None, max_length=200)
    task: str | None = Field(default=None, max_length=200)
    task_id: uuid.UUID | None = None
    category: str | None = Field(default=None, max_length=50)
    start_time: datetime.time | None = None
    end_time: datetime.time | None = None
    hours: float | None = Field(default=None, gt=0, le=24)
    description: str | None = Field(default=None, max_length=2000)
    is_billable: bool | None = None
    custom_fields: dict[str, object] | None = None
    # With project_id: the new project was picked under "Other" (see
    # WorkEntryCreate.other_project).
    other_project: bool | None = None


class OvertimeRequestCreate(BaseModel):
    employee_id: uuid.UUID
    work_date: datetime.date
    # Approved session = start_time (company local time) + hours. H-19:
    # required -- every request is a real time window that goes through the
    # enabled / overlap / shift / past-window checks (the old start-less
    # path credited hours straight to attendance, even for future dates).
    start_time: datetime.time
    # With start_time + end_time the requested hours are calculated from them;
    # the End Time must be later than the Start Time; `hours` is then ignored.
    end_time: datetime.time | None = None
    hours: float | None = Field(default=None, gt=0, le=16)
    reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _hours_from_times(self):
        if self.start_time is not None and self.end_time is not None:
            from .overtime import hours_between
            if self.end_time <= self.start_time:
                raise ValueError("End Time must be later than the Start Time.")
            self.hours = hours_between(self.start_time, self.end_time)
            if not (0 < self.hours <= 16):
                raise ValueError("Overtime must be more than 0 and at most 16 hours.")
        if self.hours is None:
            raise ValueError("Give the overtime End Time (or hours).")
        return self


class OvertimeRequestEdit(BaseModel):
    """The employee's own edit of a pending / sent-back overtime request
    (PATCH /overtime-requests/{id}/edit) -- same rules as create."""
    work_date: datetime.date
    start_time: datetime.time
    end_time: datetime.time
    reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _check_times(self):
        if self.end_time <= self.start_time:
            raise ValueError("End Time must be later than the Start Time.")
        from .overtime import hours_between
        if hours_between(self.start_time, self.end_time) > 16:
            raise ValueError("Overtime can be at most 16 hours in one request.")
        return self


class OvertimeShiftWindowOut(BaseModel):
    date: datetime.date
    label: str
    start: datetime.datetime
    end: datetime.datetime


class OvertimeDayContextOut(BaseModel):
    """GET /overtime-requests/day-context -- what an overtime request on a
    date is checked against: working day or not (holiday / weekly off) and
    the regular shift windows it must stay outside of."""
    work_date: datetime.date
    working_day: bool
    day_type: str  # working | holiday | weekly_off
    reason: str | None = None
    windows: list[OvertimeShiftWindowOut] = []


class OvertimeCompensationOut(BaseModel):
    mode: str
    status: str  # pending_payroll | in_payroll | credited | skipped
    worked_minutes: int
    counted_minutes: int
    hourly_rate: float | None = None
    rate_multiplier: float | None = None
    amount: float | None = None
    leave_type_name: str | None = None
    leave_days: float | None = None
    payroll_period: str | None = None
    calculation: str | None = None


class OvertimeRequestOut(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    work_date: datetime.date
    hours: float
    # Derived, not a stored column: this app only ever grants an overtime
    # request in full (no partial-hours approval exists anywhere in the
    # decision flow), so "approved hours" is simply `hours` once the
    # request reaches 'approved', and blank otherwise -- see
    # routers.work._overtime_request_out. Kept as its own field (rather
    # than making the UI infer it from `status == 'approved'`) so the
    # Overtime Requests tab can show Requested Hours and Approved Hours as
    # two honestly-distinct columns per the spec, without a schema/DB
    # migration or inventing partial-approval semantics that don't exist.
    approved_hours: float | None = None
    reason: str | None
    status: str
    approver_id: uuid.UUID | None = None
    approver_name: str = "—"
    # Denormalized like RegularizationOut.employee_name -- see
    # crud.serialize_attendance_record's identical docstring: the caller's
    # own People-module roster fetch can legitimately be empty/incomplete
    # while they can still see this exact request (e.g. deciding a team
    # member's request outside their People-access scope).
    employee_name: str = "—"
    decision_notes: str | None = None
    decided_at: datetime.datetime | None = None
    decided_by_manager_type: str | None = None
    # Session (app/overtime.py): scheduled | clocked_in | missed_clock_in |
    # missed_clock_out | completed -- manual punches only.
    start_time: datetime.time | None = None
    end_time: datetime.time | None = None
    planned_start: datetime.datetime | None = None
    planned_end: datetime.datetime | None = None
    session_status: str | None = None
    actual_start: datetime.datetime | None = None
    actual_end: datetime.datetime | None = None
    actual_minutes: int | None = None
    actual_duration: str | None = None  # "2h 15m"
    compensation: OvertimeCompensationOut | None = None
    can_end_early: bool = False
    # Overtime Clock In / Clock Out available to the viewer right now.
    can_clock_in: bool = False
    can_clock_out: bool = False
    missed_clock_in_at: datetime.datetime | None = None
    missed_clock_out_at: datetime.datetime | None = None


class OvertimeRequestUpdate(BaseModel):
    status: str = Field(pattern="^(approved|rejected|sent_back)$")
    decision_notes: str | None = Field(default=None, max_length=2000)


class TimesheetCreate(BaseModel):
    employee_id: uuid.UUID
    week_start: datetime.date
    # No longer read by the backend -- Submit Timesheet always computes
    # Total/Billable Hours live from the employee's actual Work Entries
    # (crud.compute_timesheet_hours / crud.submit_timesheet). Kept optional,
    # rather than removed, so an older client that still sends them doesn't
    # fail validation; the values are ignored either way.
    total_hours: float | None = None
    billable_hours: float | None = None


class TimesheetHoursOut(BaseModel):
    """Submit Timesheet's read-only Total/Billable Hours preview -- see
    GET /api/timesheets/hours-preview."""

    total_hours: float
    billable_hours: float


class TimesheetOut(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    week_start: datetime.date
    status: str
    total_hours: float
    billable_hours: float
    approver_id: uuid.UUID | None = None
    approver_name: str = "—"
    decision_notes: str | None = None
    decided_at: datetime.datetime | None = None
    submitted_at: datetime.datetime | None = None

    model_config = {"from_attributes": True}


class TimesheetUpdate(BaseModel):
    status: str = Field(pattern="^(approved|rejected|sent_back)$")
    decision_notes: str | None = Field(default=None, max_length=2000)


class SalaryRevisionRequestCreate(BaseModel):
    employee_id: uuid.UUID
    # M-33: ignored -- the current CTC is read on the server. Kept optional
    # so older clients that still send it keep working.
    current_ctc: int | None = Field(default=None, ge=0)
    proposed_ctc: int = Field(gt=0, le=10_000_000_000)
    reason: str | None = Field(default=None, max_length=2000)


class SalaryRevisionRequestOut(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    current_ctc: int | None
    proposed_ctc: int | None
    reason: str | None
    status: str
    approver_id: uuid.UUID | None = None
    approver_name: str = "—"
    decision_notes: str | None = None
    decided_at: datetime.datetime | None = None
    decided_by_manager_type: str | None = None

    model_config = {"from_attributes": True}


class SalaryRevisionRequestUpdate(BaseModel):
    status: str = Field(pattern="^(approved|rejected|sent_back)$")
    decision_notes: str | None = Field(default=None, max_length=2000)


class AssetRequestCreate(BaseModel):
    employee_id: uuid.UUID
    # Optional (blank = not specified; stored '').
    asset_type: str | None = Field(default=None, max_length=60)
    justification: str | None = Field(default=None, max_length=2000)


class AssetRequestOut(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    asset_type: str
    justification: str | None
    status: str
    approver_id: uuid.UUID | None = None
    approver_name: str = "—"
    decision_notes: str | None = None
    decided_at: datetime.datetime | None = None
    decided_by_manager_type: str | None = None

    model_config = {"from_attributes": True}


class AssetRequestUpdate(BaseModel):
    status: str = Field(pattern="^(approved|rejected|sent_back)$")
    decision_notes: str | None = Field(default=None, max_length=2000)


class AssetRecoveryDeductionCreate(BaseModel):
    """Assets > Asset Recovery Deduction. Starts as status='pending' --
    only once approved (PATCH .../decision) and Payroll Settings >
    Deductions Configuration's asset_deduction_enabled is on does this get
    picked up as a real payslip deduction line for [target_period_month]/
    [target_period_year]."""

    employee_id: uuid.UUID
    asset_assignment_id: uuid.UUID | None = None
    amount: float = Field(gt=0)
    reason: str | None = Field(default=None, max_length=2000)
    target_period_month: int = Field(ge=1, le=12)
    target_period_year: int = Field(ge=2000, le=2100)


class AssetRecoveryDeductionOut(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    employee_name: str
    asset_assignment_id: uuid.UUID | None
    asset_label: str | None = None
    amount: float
    reason: str | None
    status: str
    target_period_month: int
    target_period_year: int
    approver_id: uuid.UUID | None
    approver_name: str = "—"
    decision_notes: str | None
    decided_at: datetime.datetime | None
    applied_payroll_run_id: uuid.UUID | None
    applied_at: datetime.datetime | None

    model_config = {"from_attributes": True}


class AssetRecoveryDeductionDecision(BaseModel):
    status: str = Field(pattern="^(approved|rejected)$")
    decision_notes: str | None = Field(default=None, max_length=2000)


class HiringRequisitionCreate(BaseModel):
    requested_by: uuid.UUID
    designation_title: str = Field(min_length=1, max_length=120)
    department_name: str | None = None
    positions_count: int = Field(default=1, gt=0)
    justification: str | None = Field(default=None, max_length=2000)


class HiringRequisitionOut(BaseModel):
    id: uuid.UUID
    requested_by: uuid.UUID
    designation_title: str
    department_name: str
    positions_count: int
    justification: str | None
    status: str
    approver_id: uuid.UUID | None = None
    approver_name: str = "—"
    decision_notes: str | None = None
    decided_at: datetime.datetime | None = None
    decided_by_manager_type: str | None = None


class HiringRequisitionUpdate(BaseModel):
    status: str = Field(pattern="^(approved|rejected|sent_back)$")
    decision_notes: str | None = Field(default=None, max_length=2000)


class TaskBoardCardCreate(BaseModel):
    project_name: str = Field(min_length=1, max_length=200)
    # Omitted / blank = Backlog (the board's first column).
    column_key: str = Field(default="Backlog", min_length=1, max_length=20)
    title: str = Field(min_length=1, max_length=200)
    tag: str | None = Field(default=None, max_length=60)
    assignee_employee_id: uuid.UUID | None = None
    # Several assignees (primary first); takes precedence over the single field.
    assignee_employee_ids: list[uuid.UUID] | None = Field(default=None, max_length=50)
    # pm_tasks.story_points (db/add_task_story_points_tag.sql): whole number 0..1000.
    story_points: int | None = Field(default=None, ge=0, le=1000)
    # Review workflow (app/tasks.py): both optional (Assigned Date defaults
    # to today once an assignee is set; no deadline = never overdue).
    assigned_date: datetime.date | None = None
    deadline_at: datetime.datetime | None = None
    description: str | None = Field(default=None, max_length=5000)
    # Form Configuration: admin-defined custom fields {field_key: value}.
    custom_fields: dict[str, object] | None = None


class TaskBoardCardOut(BaseModel):
    """The board summary every employee may see -- never the description,
    submissions or files (those are Task Details, see app/tasks.py)."""

    id: uuid.UUID
    column_key: str
    title: str
    tag: str
    assignee: str
    story_points: int | None
    code: str | None = None
    assignee_employee_id: uuid.UUID | None = None
    assignee_employee_ids: list[uuid.UUID] = []
    assignees: list[str] = []
    assigned_date: datetime.date | None = None
    deadline_at: datetime.datetime | None = None
    workflow_status: str | None = None
    status_label: str | None = None
    is_overdue: bool = False
    can_view_details: bool = True
    # Form Configuration: configured label of the column + custom field values.
    column_label: str | None = None
    custom_fields: dict[str, str] = {}


class TaskBoardCardMove(BaseModel):
    column_key: str = Field(min_length=1, max_length=20)


class TaskActivityEntryOut(BaseModel):
    """One real hcm.pm_time_entries row logged against this task -- the
    Task Details screen's Activity list. Not a formal assignment (pm_tasks
    has no assignee column), just genuine backend history of who has
    actually worked on it."""

    employee_id: uuid.UUID
    employee_name: str
    entry_date: datetime.date
    hours: float
    description: str | None
    status: str


class TaskCardDetailOut(BaseModel):
    """People > Task Management > (click a card) -- the complete Task
    Details screen. Every real pm_tasks column, plus the project's name
    and a real, backend-derived activity history (see
    crud.get_task_board_card_detail)."""

    id: uuid.UUID
    code: str
    title: str
    description: str | None
    column_key: str
    status: str
    priority: str
    task_type: str
    project_id: uuid.UUID
    project_name: str
    project_code: str | None = None
    assignee: str
    due_date: datetime.date | None
    planned_start_date: datetime.date | None
    estimated_hours: float | None
    actual_hours: float
    progress_pct: int
    is_active: bool
    created_at: datetime.datetime
    updated_at: datetime.datetime
    activity: list[TaskActivityEntryOut]
    # Review workflow (app/tasks.py) -- None / empty for an unassigned task.
    assignee_employee_id: uuid.UUID | None = None
    assignee_code: str | None = None
    assignee_employee_ids: list[uuid.UUID] = []
    assignees: list[str] = []
    reporting_manager_id: uuid.UUID | None = None
    reporting_manager: str | None = None
    reporting_managers: list[str] = []
    assigned_date: datetime.date | None = None
    deadline_at: datetime.datetime | None = None
    workflow_status: str | None = None
    status_label: str | None = None
    is_overdue: bool = False
    reviewer_employee_id: uuid.UUID | None = None
    reviewer: str | None = None
    submitted_at: datetime.datetime | None = None
    completed_at: datetime.datetime | None = None
    completed_by: str | None = None
    my_roles: list[str] = []
    permissions: "TaskPermissionsOut | None" = None
    submissions: list["TaskSubmissionOut"] = []
    history: list["TaskHistoryOut"] = []
    tag: str | None = None
    story_points: int | None = None
    column_label: str | None = None
    custom_fields: dict[str, str] = {}


class TaskPermissionsOut(BaseModel):
    can_start: bool = False
    can_submit: bool = False
    can_change_reviewer: bool = False
    can_review: bool = False
    can_edit: bool = False


class TaskSubmissionOut(BaseModel):
    id: uuid.UUID
    round: int
    description: str
    submitted_by: str
    submitted_at: datetime.datetime
    reviewer_employee_id: uuid.UUID | None
    reviewer: str | None
    decision: str | None
    review_comments: str | None
    reviewed_by: str | None
    reviewed_at: datetime.datetime | None
    files: list[AttachmentOut]


class TaskHistoryOut(BaseModel):
    id: uuid.UUID
    action: str
    from_status: str | None
    to_status: str | None
    from_label: str | None
    to_label: str | None
    actor: str
    comments: str | None
    created_at: datetime.datetime


class TaskReviewerOption(BaseModel):
    id: uuid.UUID
    name: str
    employee_code: str | None
    is_reporting_manager: bool = False


class TaskReviewerChange(BaseModel):
    reviewer_employee_id: uuid.UUID | None = None


class TaskReviewDecision(BaseModel):
    decision: str = Field(pattern="^(approve|changes_requested)$")
    comments: str | None = Field(default=None, max_length=5000)


class TaskCardUpdate(BaseModel):
    """Task Management > Task Details > Edit. Every field optional/partial
    (exclude_unset) -- only fields the client actually sends are changed,
    same convention as LifecycleEventUpdate. Never includes column_key/
    status -- that stays exclusively the existing "Move to" action's job
    (TaskBoardCardMove / move_task_board_card)."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    priority: str | None = Field(default=None, min_length=1, max_length=15)
    task_type: str | None = Field(default=None, min_length=1, max_length=30)
    due_date: datetime.date | None = None
    planned_start_date: datetime.date | None = None
    # pm_tasks.estimated_hours is NUMERIC(6,2).
    estimated_hours: float | None = Field(default=None, ge=0, le=9999.99)
    tag: str | None = Field(default=None, max_length=60)
    story_points: int | None = Field(default=None, ge=0, le=1000)
    custom_fields: dict[str, object] | None = None
    # Review workflow (app/tasks.py): assign / re-assign, reschedule.
    assignee_employee_id: uuid.UUID | None = None
    # The full assignee list (primary first); takes precedence over the single field.
    assignee_employee_ids: list[uuid.UUID] | None = Field(default=None, max_length=50)
    assigned_date: datetime.date | None = None
    deadline_at: datetime.datetime | None = None


class RecognitionCreate(BaseModel):
    employee_id: uuid.UUID
    badge: str = Field(min_length=1, max_length=80)
    reason: str | None = Field(default=None, max_length=2000)
    # Omitted = today.
    given_on: datetime.date = Field(default_factory=datetime.date.today)


class RecognitionOut(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    badge: str
    reason: str | None
    given_on: datetime.date

    model_config = {"from_attributes": True}


# ── Travel Requests and Expense Claims ─────────────────────────────────────
# Dropdown values, served by GET /api/travel-expense/options so the app never
# hardcodes them.
TRAVEL_TYPES = ("Domestic", "International")
TRAVEL_FUNDING = ("Self", "Company", "Sponsored by Third Party", "Partially Sponsored")
SPONSORED_FUNDING = ("Sponsored by Third Party", "Partially Sponsored")
_SPONSORED_FUNDING_LOWER = tuple(x.lower() for x in SPONSORED_FUNDING)
TRAVEL_MODES = ("Flight", "Train", "Bus", "Car", "Cab", "Other")
ID_DOCUMENT_TYPES = ("Passport", "Aadhaar Card", "PAN Card", "Driving License", "Voter ID")


def _clean(v: str | None) -> str | None:
    v = " ".join((v or "").split())
    return v or None


class TravelItineraryIn(BaseModel):
    """One leg of the Travel Itinerary."""

    travel_from: str = Field(min_length=1, max_length=150)
    travel_to: str = Field(min_length=1, max_length=150)
    mode_of_travel: str = Field(min_length=1, max_length=40)
    departure_at: datetime.datetime
    arrival_at: datetime.datetime | None = None

    @field_validator("travel_from", "travel_to", "mode_of_travel")
    @classmethod
    def _need_text(cls, v: str) -> str:
        v = _clean(v)
        if not v:
            raise ValueError("This itinerary field is required")
        return v

    @model_validator(mode="after")
    def _check(self):
        # Kept: the database refuses an arrival before the departure. (Same
        # From / To is allowed -- local travel, round trips.)
        if self.arrival_at is not None and self.arrival_at < self.departure_at:
            raise ValueError("Arrival cannot be before departure")
        return self


class TravelCostingIn(BaseModel):
    """One Costing Details row; the total is sponsored + funded, worked out by the server."""

    expense_type: str = Field(min_length=1, max_length=80)
    comments: str | None = Field(default=None, max_length=255)
    sponsored_amount: float = Field(default=0, ge=0, le=MAX_NUMERIC_14_2)
    funded_amount: float = Field(default=0, ge=0, le=MAX_NUMERIC_14_2)

    @field_validator("expense_type")
    @classmethod
    def _need_type(cls, v: str) -> str:
        v = _clean(v)
        if not v:
            raise ValueError("Expense type is required")
        return v

    @field_validator("comments")
    @classmethod
    def _strip_comments(cls, v: str | None) -> str | None:
        return _clean(v)


class _TravelFields(BaseModel):
    """Everything on the Travel Request form except who it is for."""

    # Optional (blank = none); the itinerary / travel dates stay required.
    purpose: str | None = Field(default=None, max_length=200)
    travel_type: str | None = None
    travel_funding: str | None = None
    sponsor_details: str | None = Field(default=None, max_length=200)
    contact_number: str | None = Field(default=None, max_length=30)
    contact_email: str | None = Field(default=None, max_length=150)
    id_document_type: str | None = Field(default=None, max_length=40)
    id_document_number: str | None = Field(default=None, max_length=60)
    passport_number: str | None = Field(default=None, max_length=30)
    description: str | None = Field(default=None, max_length=5000)
    cost_center: str | None = Field(default=None, max_length=100)
    project_id: uuid.UUID | None = None
    organizer_name: str | None = Field(default=None, max_length=150)
    organizer_address: str | None = Field(default=None, max_length=255)
    event_details: str | None = Field(default=None, max_length=5000)
    itinerary: list[TravelItineraryIn] = Field(default_factory=list, max_length=50)
    costing: list[TravelCostingIn] = Field(default_factory=list, max_length=50)
    # Older single-row clients send these instead of an itinerary.
    destination: str | None = Field(default=None, max_length=150)
    from_date: datetime.date | None = None
    to_date: datetime.date | None = None
    travel_mode: str | None = Field(default=None, max_length=40)
    # M-18 / M-19: non-negative and within hcm_travel_requests.estimated_cost NUMERIC(12,2).
    estimated_cost: float | None = Field(default=None, ge=0, le=MAX_NUMERIC_12_2)
    # Admin-defined custom fields (Form Configuration, form 'travel_request'):
    # {field_key: value}. None = not sent (stored values are left alone).
    custom_fields: dict | None = None
    save_as_draft: bool = False

    @field_validator(
        "sponsor_details", "contact_number", "id_document_number", "passport_number",
        "cost_center", "organizer_name", "organizer_address", "description", "event_details",
        "travel_type", "travel_funding", "id_document_type", "contact_email",
    )
    @classmethod
    def _blank_none(cls, v: str | None) -> str | None:
        return _clean(v)

    @field_validator("purpose")
    @classmethod
    def _purpose(cls, v: str | None) -> str | None:
        return _clean(v)

    @model_validator(mode="after")
    def _check_form(self):
        # Travel Type / Funding / Identification Document Type / Mode of Travel
        # are validated against the company's Form Configuration options in
        # travel_expense.apply_travel_form (they need the database).
        if self.contact_email and ("@" not in self.contact_email or " " in self.contact_email):
            raise ValueError("Contact Email is not a valid email address")
        if self.contact_number and sum(ch.isdigit() for ch in self.contact_number) < 7:
            raise ValueError("Contact Number is not valid")
        # Only what a request needs to exist is enforced: past trips, long
        # trips, costing without an itinerary and repeated costing types are
        # all allowed, and a draft may be empty. A SUBMITTED request needs
        # an itinerary or at least a destination (where the employee goes).
        if not self.itinerary:
            if not self.save_as_draft and not (self.destination or "").strip():
                raise ValueError("Add a travel itinerary row (or a destination) before submitting")
            if self.from_date and self.to_date and self.to_date < self.from_date:
                raise ValueError("To Date cannot be before From Date")
        if sum(c.sponsored_amount + c.funded_amount for c in self.costing) > MAX_NUMERIC_12_2:
            raise ValueError("The travel costing total is too large")
        return self


class TravelRequestApiCreate(_TravelFields):
    employee_id: uuid.UUID


class TravelRequestEdit(_TravelFields):
    """Full replace of a draft / sent-back request (the form is sent whole)."""


class TravelItineraryOut(BaseModel):
    id: uuid.UUID
    travel_from: str
    travel_to: str
    mode_of_travel: str
    departure_at: datetime.datetime
    arrival_at: datetime.datetime | None = None

    model_config = {"from_attributes": True}


class TravelCostingOut(BaseModel):
    id: uuid.UUID
    expense_type: str
    comments: str | None = None
    sponsored_amount: float
    funded_amount: float
    total_amount: float

    model_config = {"from_attributes": True}


class TravelRequestOut(BaseModel):
    id: uuid.UUID
    # The request's unique ID, e.g. HR-TR-2026-00001.
    request_no: str | None = None
    employee_id: uuid.UUID
    purpose: str | None = None
    destination: str | None = None
    from_date: datetime.date
    to_date: datetime.date
    travel_mode: str | None = None
    estimated_cost: float | None = None
    status: str
    # Reporting Manager Approval Workflow -- see
    # backend/db/add_approval_decision_fields.sql (hcm.travel_requests
    # previously had no approver_id column at all).
    approver_id: uuid.UUID | None = None
    approver_name: str = "—"
    decision_notes: str | None = None
    decided_at: datetime.datetime | None = None
    decided_by_manager_type: str | None = None
    employee_name: str | None = None
    employee_code: str | None = None
    # Payroll reimbursement state once approved (see
    # crud.reimbursement_payroll_statuses), e.g. "Paid · September 2026".
    payroll_status: str | None = None
    # Full form (add_travel_expense_details.sql). All additive.
    travel_type: str | None = None
    travel_funding: str | None = None
    sponsor_details: str | None = None
    contact_number: str | None = None
    contact_email: str | None = None
    id_document_type: str | None = None
    id_document_number: str | None = None
    passport_number: str | None = None
    description: str | None = None
    cost_center: str | None = None
    project_id: uuid.UUID | None = None
    project_name: str | None = None
    organizer_name: str | None = None
    organizer_address: str | None = None
    event_details: str | None = None
    custom_fields: dict[str, str] = {}
    itinerary: list[TravelItineraryOut] = []
    costing: list[TravelCostingOut] = []
    total_sponsored: float = 0
    total_funded: float = 0
    total_cost: float = 0
    submitted_at: datetime.datetime | None = None
    created_at: datetime.datetime | None = None
    department_name: str | None = None
    can_edit: bool = False

    model_config = {"from_attributes": True}


class TravelRequestUpdate(BaseModel):
    status: str = Field(pattern="^(approved|rejected|sent_back)$")
    decision_notes: str | None = Field(default=None, max_length=2000)


class ExpenseLineItemCreate(BaseModel):
    date: datetime.date
    # The Expense Claim Type's name (validated against the configured types
    # when given; optional).
    category: str | None = Field(default=None, max_length=40)
    description: str | None = Field(default=None, max_length=255)
    # M-18: hcm_expense_claim_lines.amount is NUMERIC(14,2).
    amount: float = Field(gt=0, le=MAX_NUMERIC_14_2)
    # This expense's receipt / invoice: an attachment already uploaded to
    # this same expense report (kept when the report is edited). A new
    # receipt is uploaded after saving, with the row's line_index.
    receipt_attachment_id: uuid.UUID | None = None
    # Any expense date is accepted (e.g. a ticket booked ahead of the trip).


class ExpenseTaxIn(BaseModel):
    """A Taxes & Charges row: give a rate (% of the expenses) or a fixed amount."""

    account_head: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=255)
    rate: float | None = Field(default=None, ge=0, le=100)
    tax_amount: float | None = Field(default=None, ge=0, le=MAX_NUMERIC_14_2)

    @field_validator("account_head")
    @classmethod
    def _head(cls, v: str) -> str:
        v = _clean(v)
        if not v:
            raise ValueError("Account Head is required for a tax or charge")
        return v

    @field_validator("description")
    @classmethod
    def _desc(cls, v: str | None) -> str | None:
        return _clean(v)

    @model_validator(mode="after")
    def _rate_or_amount(self):
        if self.rate is None and self.tax_amount is None:
            raise ValueError("Enter a rate or an amount for every tax and charge")
        return self


class ExpenseAdvanceIn(BaseModel):
    advance_id: uuid.UUID
    allocated_amount: float = Field(gt=0, le=MAX_NUMERIC_14_2)


class ExpenseReportApiCreate(BaseModel):
    employee_id: uuid.UUID
    # Optional now (the claim form has no title); defaults to the posting date.
    reference: str | None = Field(default=None, max_length=200)
    # Omitted = today.
    submitted_date: datetime.date = Field(default_factory=datetime.date.today)
    # At least one expense is needed to SUBMIT; a draft may have none.
    items: list[ExpenseLineItemCreate] = Field(default_factory=list, max_length=200)
    # Expenses & Advances / Accounting / More Info tabs.
    expense_approver_id: uuid.UUID | None = None
    posting_date: datetime.date | None = None
    clearance_date: datetime.date | None = None
    is_paid: bool = False
    payable_account: str | None = Field(default=None, max_length=120)
    remark: str | None = Field(default=None, max_length=5000)
    project_id: uuid.UUID | None = None
    cost_center: str | None = Field(default=None, max_length=100)
    task_id: uuid.UUID | None = None
    travel_request_id: uuid.UUID | None = None
    taxes: list[ExpenseTaxIn] = Field(default_factory=list, max_length=20)
    advances: list[ExpenseAdvanceIn] = Field(default_factory=list, max_length=20)
    # Admin-defined custom fields (form 'expense_claim'); None = not sent.
    custom_fields: dict | None = None
    save_as_draft: bool = False

    @field_validator("payable_account", "remark", "cost_center", "reference")
    @classmethod
    def _blank_none(cls, v: str | None) -> str | None:
        return _clean(v) if v is not None and len(v) < 300 else ((v or "").strip() or None)

    @model_validator(mode="after")
    def _check_total_and_date(self):
        # M-18: the report total lands in hcm_expense_claims.total_amount NUMERIC(14,2).
        if sum(i.amount for i in self.items) > MAX_NUMERIC_14_2:
            raise ValueError("The expense report total is too large")
        if not self.items and not self.save_as_draft:
            raise ValueError("Add at least one expense before submitting")
        if self.clearance_date and self.clearance_date < (self.posting_date or self.submitted_date):
            raise ValueError("Clearance date cannot be before the posting date")
        ids = [a.advance_id for a in self.advances]
        if len(set(ids)) != len(ids):
            raise ValueError("The same advance can only be applied once")
        return self


class ExpenseReportEdit(ExpenseReportApiCreate):
    """Full replace of a draft / sent-back claim (the employee cannot change)."""

    employee_id: uuid.UUID | None = None


class ExpenseLineItemOut(BaseModel):
    expense_date: datetime.date
    category: str | None
    description: str | None
    amount: float
    claim_type_id: uuid.UUID | None = None
    # The receipt / invoice uploaded for this one expense (None = none).
    receipt: AttachmentOut | None = None

    model_config = {"from_attributes": True}


class ExpenseTaxOut(BaseModel):
    account_head: str
    description: str | None = None
    rate: float | None = None
    tax_amount: float

    model_config = {"from_attributes": True}


class ExpenseAdvanceOut(BaseModel):
    advance_id: uuid.UUID
    allocated_amount: float
    purpose: str | None = None
    posting_date: datetime.date | None = None
    advance_amount: float | None = None


class ExpenseReportOut(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    reference: str | None
    claim_date: datetime.date
    status: str
    items: list[ExpenseLineItemOut]
    approver_id: uuid.UUID | None = None
    approver_name: str = "—"
    decision_notes: str | None = None
    decided_at: datetime.datetime | None = None
    decided_by_manager_type: str | None = None
    employee_name: str | None = None
    employee_code: str | None = None
    # Payroll reimbursement state once approved (see
    # crud.reimbursement_payroll_statuses), e.g. "Paid · September 2026".
    payroll_status: str | None = None
    # Full form (add_travel_expense_details.sql). All additive.
    series: str | None = None
    expense_approver_id: uuid.UUID | None = None
    expense_approver_name: str | None = None
    department_name: str | None = None
    company_name: str | None = None
    posting_date: datetime.date | None = None
    clearance_date: datetime.date | None = None
    is_paid: bool = False
    payable_account: str | None = None
    remark: str | None = None
    project_id: uuid.UUID | None = None
    project_name: str | None = None
    cost_center: str | None = None
    task_id: uuid.UUID | None = None
    task_title: str | None = None
    travel_request_id: uuid.UUID | None = None
    taxes: list[ExpenseTaxOut] = []
    advances: list[ExpenseAdvanceOut] = []
    total_claimed: float = 0
    total_taxes: float = 0
    advance_amount: float = 0
    custom_fields: dict[str, str] = {}
    # total_amount = what payroll reimburses = claimed + taxes - advances.
    net_payable: float = 0
    submitted_at: datetime.datetime | None = None
    can_edit: bool = False
    # The viewer may record this approved claim's payment (Finance /
    # administrator, never on their own claim).
    can_record_payment: bool = False


class ExpenseReportUpdate(BaseModel):
    status: str = Field(pattern="^(approved|rejected|sent_back)$")
    decision_notes: str | None = Field(default=None, max_length=2000)


class ExpensePaymentUpdate(BaseModel):
    """Finance marks an APPROVED claim paid / sets its clearance date."""

    is_paid: bool
    clearance_date: datetime.date | None = None

    @model_validator(mode="after")
    def _check(self):
        if self.clearance_date and self.clearance_date > _today_with_slack(1):
            raise ValueError("Clearance date cannot be in the future")
        if self.clearance_date and not self.is_paid:
            raise ValueError("A clearance date only applies to a claim that is marked paid")
        return self


class ExpenseClaimTypeIn(BaseModel):
    # 40: hcm_expense_claim_lines.category is VARCHAR(40) and stores this name.
    name: str = Field(min_length=1, max_length=40)
    description: str | None = Field(default=None, max_length=255)
    is_active: bool = True

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = _clean(v)
        if not v:
            raise ValueError("Expense Category name is required")
        return v

    @field_validator("description")
    @classmethod
    def _desc(cls, v: str | None) -> str | None:
        return _clean(v)


class ExpenseClaimTypeUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=40)
    description: str | None = Field(default=None, max_length=255)
    is_active: bool | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = _clean(v)
        if not v:
            raise ValueError("Expense Category name is required")
        return v

    @field_validator("description")
    @classmethod
    def _desc(cls, v: str | None) -> str | None:
        # A cleared description is stored as no description (shown as "—").
        return _clean(v)


class ExpenseClaimTypeOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None = None
    is_active: bool
    usage_count: int = 0
    created_at: datetime.datetime | None = None


class EmployeeAdvanceIn(BaseModel):
    employee_id: uuid.UUID
    # Omitted = today.
    posting_date: datetime.date = Field(default_factory=datetime.date.today)
    advance_amount: float = Field(gt=0, le=MAX_NUMERIC_14_2)
    purpose: str | None = Field(default=None, max_length=255)
    travel_request_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _date(self):
        if self.posting_date > _today_with_slack(1):
            raise ValueError("Posting date cannot be in the future")
        return self


class EmployeeAdvanceOut(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    employee_name: str | None = None
    posting_date: datetime.date
    purpose: str | None = None
    advance_amount: float
    claimed_amount: float
    returned_amount: float
    outstanding: float
    # outstanding minus what other open claims have already set aside.
    available: float = 0
    status: str
    travel_request_id: uuid.UUID | None = None


class NotificationPreferenceOut(BaseModel):
    event_key: str
    channel: str
    enabled: bool


class NotificationPreferenceUpdate(BaseModel):
    event_key: str = Field(min_length=1, max_length=80)
    channel: str = Field(pattern="^(email|push|sms)$")
    enabled: bool


class NotificationChannelState(BaseModel):
    channel: str
    available: bool
    enabled: bool


class NotificationSettingItem(BaseModel):
    event_key: str
    category: str
    label: str
    channels: list[NotificationChannelState]


class NotificationSettingUpdate(BaseModel):
    event_key: str = Field(min_length=1, max_length=60)
    channel: str = Field(pattern="^(email|inapp)$")
    enabled: bool


class CandidateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    job_opening_title: str = Field(min_length=1, max_length=200)
    years_experience: float | None = Field(default=None, ge=0, le=80)
    email: ev.OptEmailIn = None
    phone: str | None = None


class InterviewCreate(BaseModel):
    candidate_name: str = Field(min_length=1, max_length=150)
    job_opening_title: str = Field(min_length=1, max_length=200)
    round_no: int = Field(default=1, ge=1, le=20)
    scheduled_at: datetime.datetime
    interviewer_employee_id: uuid.UUID | None = None


class OfferCreate(BaseModel):
    candidate_name: str = Field(min_length=1, max_length=150)
    job_opening_title: str = Field(min_length=1, max_length=200)
    offered_ctc: float = Field(ge=0, le=ev.MAX_ANNUAL_CTC)
    # Omitted = today.
    offer_date: datetime.date = Field(default_factory=datetime.date.today)
    proposed_joining_date: datetime.date | None = None
    status: str = Field(default="Sent", pattern="^(Sent|Accepted|Rejected|Withdrawn)$")


class SprintCreate(BaseModel):
    project_name: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=80)
    # Optional (planned dates are nullable); checked for order when both given.
    start_date: datetime.date | None = None
    end_date: datetime.date | None = None
    velocity_points: int | None = None

    _name = field_validator("name", mode="before")(classmethod(lambda cls, v: _collapse_spaces(v)))

    @model_validator(mode="after")
    def _dates_in_order(self):
        # L-15: a sprint can't end before it starts.
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("The sprint end date must be on or after its start date.")
        return self


class SprintOut(BaseModel):
    id: uuid.UUID
    name: str
    project: str
    project_code: str | None = None
    start_date: datetime.date | None = None
    end_date: datetime.date | None = None
    status: str
    velocity_points: int | None


class ReleaseCreate(BaseModel):
    project_name: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=120)
    # Omitted = today (pm_milestones.due_date is NOT NULL).
    target_date: datetime.date = Field(default_factory=datetime.date.today)
    notes: str | None = None


class ReleaseOut(BaseModel):
    id: uuid.UUID
    name: str
    project: str
    project_code: str | None = None
    target_date: datetime.date
    status: str


class TrainingSessionCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    session_date: datetime.date
    trainer_name: str | None = None
    is_mandatory: bool = False
    attendee_count: int = Field(default=0, ge=0, le=1_000_000)  # L-27


class TrainingSessionOut(BaseModel):
    id: uuid.UUID
    title: str
    session_date: datetime.date
    trainer_name: str | None
    is_mandatory: bool
    attendee_count: int

    model_config = {"from_attributes": True}


class SkillRatingOut(BaseModel):
    employee_id: uuid.UUID
    skill: str
    level: str

    model_config = {"from_attributes": True}


class SkillRatingUpsert(BaseModel):
    employee_id: uuid.UUID
    skill: str = Field(min_length=1, max_length=100)
    level: str = Field(pattern="^(Beginner|Intermediate|Advanced|Expert)$")


class IntegrationOut(BaseModel):
    name: str
    description: str | None
    is_connected: bool


class IntegrationUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=200)
    is_connected: bool


class CompanySettingsOut(BaseModel):
    default_currency: str
    default_timezone: str
    working_days_per_week: int
    # General Settings > Weekly Off Days: the effective days (weekday names,
    # Monday..Sunday order) -- derived from working_days_per_week until the
    # company saves a choice (see app/weekly_off.py).
    weekly_off_days: list[str] = Field(default_factory=lambda: ["saturday", "sunday"])
    enable_branch_level: bool = True
    enable_business_unit_level: bool = True
    enable_department_level: bool = True
    enable_sub_department_level: bool = True
    working_hours_start: datetime.time | None = None
    working_hours_end: datetime.time | None = None
    probation_period_days: int = 90
    notice_period_days: int = 30
    pf_wage_ceiling: float = 15000
    # None = no limit configured -- Work Entry/Timesheet backdating is then
    # unrestricted, matching every tenant that hasn't set this.
    work_entry_backdate_days: int | None = None
    # M-12: how many days back an attendance regularization may be raised
    # (None in the row = the default, 30).
    regularization_backdate_days: int | None = 30
    # Off by default -- see CompanySettings.auto_tds_estimate_enabled.
    auto_tds_estimate_enabled: bool = False
    # Payroll Settings > Deductions Configuration -- read-only here (edited
    # via the dedicated PATCH /api/payroll/deduction-settings, gated by the
    # Payroll module's own edit permission rather than System Settings/RBAC).
    lop_deduction_enabled: bool = False
    asset_deduction_enabled: bool = False
    # Administration > Notifications > Contract expiry alerts: how many days
    # before a contract's end date the alert goes out (stored NULL = 3).
    contract_expiry_notice_days: int | None = 3

    model_config = {"from_attributes": True}

    @field_validator("contract_expiry_notice_days", mode="before")
    @classmethod
    def _notice_days_default(cls, v):
        return 3 if v is None else v

    @field_validator("weekly_off_days", mode="before")
    @classmethod
    def _weekly_off_days(cls, v, info):
        from . import weekly_off

        if isinstance(v, list):
            return v
        parsed = weekly_off.parse(v)
        return parsed if parsed is not None else weekly_off.legacy_off_days(info.data.get("working_days_per_week"))


class CompanySettingsUpdate(BaseModel):
    default_currency: str | None = Field(default=None, max_length=10)
    default_timezone: str | None = Field(default=None, max_length=60)
    working_days_per_week: int | None = Field(default=None, ge=1, le=7)
    # Weekly Off Days (weekday names, e.g. ["saturday", "sunday"]); [] = none.
    # Saving it also sets working_days_per_week to 7 - len(days).
    weekly_off_days: list[str] | None = Field(default=None, max_length=7)
    enable_branch_level: bool | None = None
    enable_business_unit_level: bool | None = None
    enable_department_level: bool | None = None
    enable_sub_department_level: bool | None = None
    working_hours_start: datetime.time | None = None
    working_hours_end: datetime.time | None = None
    auto_tds_estimate_enabled: bool | None = None
    probation_period_days: int | None = Field(default=None, ge=0, le=365)
    notice_period_days: int | None = Field(default=None, ge=0, le=365)
    pf_wage_ceiling: float | None = Field(default=None, gt=0)
    work_entry_backdate_days: int | None = Field(default=None, ge=0, le=365)
    regularization_backdate_days: int | None = Field(default=None, ge=0, le=365)
    contract_expiry_notice_days: int | None = Field(default=None, ge=1, le=90)


class EmployeeIdFormatOut(BaseModel):
    """Administration > General Settings' Employee ID Format card, and the
    Add Employee form's suggested-next-ID field. Backed by this company's
    core.number_series row (doctype='employee') -- see
    crud.get_or_create_employee_number_series. next_code is a preview only
    (current_no + 1, formatted); it does NOT reserve or increment the
    sequence, since the employee that number would go to might never
    actually get created, or might get a different manually-entered code
    entirely."""

    prefix: str
    suffix: str
    padding: int
    next_code: str
    generation_mode: str = "auto"


class EmployeeIdFormatUpdate(BaseModel):
    """PATCH body for the Organization Owner (or anyone with System
    Settings/RBAC admin access) to reconfigure the Employee ID format.
    next_number, if given, resets where the sequence continues from (the
    next auto-suggested/generated code will be next_number + 1) -- setting
    it lower than employees that already exist is allowed (generate_employee_code
    already skips forward past any collision), so this can't create a
    duplicate, just a temporary run of skipped numbers."""

    prefix: str | None = Field(default=None, max_length=20)
    suffix: str | None = Field(default=None, max_length=20)
    padding: int | None = Field(default=None, ge=1, le=10)
    next_number: int | None = Field(default=None, ge=0)
    # 'auto' = generated from this format on Add Employee; 'manual' = typed
    # in by hand there (the format is then unused for that type).
    generation_mode: str | None = Field(default=None, pattern="^(auto|manual)$")


class IdFormatOut(BaseModel):
    """Administration > General Settings > Document ID Formats, one per
    document type (project, travel_request, expense_report).
    next_number is the lowest number the next record can get (the
    "Starting Number"); next_code is the ID the next record would get now
    (a preview -- nothing is reserved)."""

    doctype: str
    label: str
    prefix: str
    suffix: str
    padding: int
    include_year: bool
    next_number: int
    next_code: str


class IdFormatUpdate(BaseModel):
    prefix: str | None = Field(default=None, max_length=20)
    suffix: str | None = Field(default=None, max_length=20)
    padding: int | None = Field(default=None, ge=1, le=10)
    next_number: int | None = Field(default=None, ge=1, le=999_999_999)
    include_year: bool | None = None


class IdPreviewOut(BaseModel):
    doctype: str
    next_code: str


class ProjectIdFormatOut(BaseModel):
    """Administration > General Settings > Project ID Format (core.
    number_series, doctype 'project'). next_number is the number the next
    project gets; next_code is that ID, formatted (a preview -- nothing is
    reserved)."""

    prefix: str
    suffix: str
    padding: int
    next_number: int
    next_code: str


class ProjectIdFormatUpdate(BaseModel):
    """next_number may be set lower than IDs already in use: those are
    skipped, so it can never create a duplicate."""

    prefix: str | None = Field(default=None, max_length=20)
    suffix: str | None = Field(default=None, max_length=20)
    padding: int | None = Field(default=None, ge=1, le=10)
    next_number: int | None = Field(default=None, ge=1, le=999_999_999)


class EmployeeIdFormatItem(BaseModel):
    """One row of Administration > General Settings' per-Employment-Type
    Employee ID Format list (GET /api/settings/employee-id-formats). Backed
    by core.number_series, doctype='employee' for the default/fallback
    series (used by every type with no override, Full-time included) or
    'employee:<type-slug>' for a type that has its own configured series --
    see crud._employee_doctype. is_custom is False when this item is
    actually reading the shared default series (no type-specific row exists
    yet), so the UI can show e.g. "Using default format"."""

    employment_type: str
    prefix: str
    suffix: str
    padding: int
    next_code: str
    is_custom: bool
    generation_mode: str = "auto"


class EmployeeIdAvailabilityOut(BaseModel):
    """GET /api/settings/employee-id-availability -- whether a manually typed
    Employee ID is free in this company (trimmed, case-insensitive; see
    crud.employee_code_taken). A UI hint only: the create endpoint re-checks
    under a lock regardless."""

    code: str
    available: bool


class FieldRuleOut(BaseModel):
    entity_key: str
    field_key: str
    state: str
    role_id: uuid.UUID | None = None

    model_config = {"from_attributes": True}


class FieldRuleUpsert(BaseModel):
    field_key: str = Field(min_length=1, max_length=60)
    state: str = Field(pattern="^(mandatory|optional|hidden|readonly)$")
    role_id: uuid.UUID | None = None


class FieldRulesUpdateRequest(BaseModel):
    entity_key: str = Field(min_length=1, max_length=40)
    rules: list[FieldRuleUpsert]


class FieldRulesResponse(BaseModel):
    entity_key: str
    rules: dict[str, str]


class ModuleOut(BaseModel):
    key: str
    name: str
    icon: str | None
    is_core: bool
    is_enabled: bool
    nav_group: str | None = None
    permission_columns: list[str] | None = None


class ModulesConfigResponse(BaseModel):
    modules: list[ModuleOut]


class ModuleToggle(BaseModel):
    key: str
    is_enabled: bool


class ModulesUpdateRequest(BaseModel):
    modules: list[ModuleToggle]


class ApprovalWorkflowStepIn(BaseModel):
    step_order: int
    approver_type: str = Field(pattern="^(role|user|reporting_manager|dotted_line_manager|department_head|business_unit_head|branch_manager)$")
    role_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    # 'user' steps: several people, any ONE of them decides the step (stored
    # as one row per person sharing step_order). A lone user_id is accepted
    # for older clients and normalised into user_ids; user_id stays = first.
    user_ids: list[uuid.UUID] | None = None
    min_amount: float | None = None
    max_amount: float | None = None

    @model_validator(mode="after")
    def _normalise_user_ids(self):
        ids = list(dict.fromkeys(self.user_ids or ([self.user_id] if self.user_id else [])))
        self.user_ids = ids or None
        self.user_id = ids[0] if ids else None
        return self


class ApprovalWorkflowStepOut(ApprovalWorkflowStepIn):
    id: uuid.UUID

    model_config = {"from_attributes": True}


class ApprovalWorkflowOut(BaseModel):
    id: uuid.UUID
    doctype: str
    name: str
    is_active: bool
    steps: list[ApprovalWorkflowStepOut]

    model_config = {"from_attributes": True}


class ApprovalWorkflowUpsertRequest(BaseModel):
    doctype: str = Field(min_length=1, max_length=60)
    name: str = Field(min_length=1, max_length=120)
    steps: list[ApprovalWorkflowStepIn]


class HierarchyRoleBindingOut(BaseModel):
    rung_key: str
    role_id: uuid.UUID
    role_name: str


class HierarchyRoleBindingsResponse(BaseModel):
    bindings: list[HierarchyRoleBindingOut]


class HierarchyRoleBindingsUpdateRequest(BaseModel):
    # rung_key -> role_id. A rung omitted from the map is left untouched;
    # a rung mapped to null clears its existing binding (falls back to the
    # default role-name match in org_hierarchy.py).
    bindings: dict[str, uuid.UUID | None]


class OrganizationDashboardSummaryOut(BaseModel):
    employees: int
    branches: int
    business_units: int
    departments: int
    sub_departments: int
    designations: int
    roles: int
    pending_approvals: int
    employee_transfers: int


class HierarchyNodeOut(BaseModel):
    id: uuid.UUID | None   # None when PM is stored as free text (pm_name), not an employee FK
    name: str
    role: str | None


class EmployeeHierarchyOut(BaseModel):
    """Dynamic reporting hierarchy for one employee -- see
    org_hierarchy.resolve_employee_hierarchy. Every field is null when that
    rung doesn't apply to the employee's role or nothing has been assigned
    yet, never an error."""

    employee_id: uuid.UUID
    hr_representative: HierarchyNodeOut | None = None
    # Every HR Representative of the employee's department (primary first).
    hr_representatives: list[HierarchyNodeOut] = []
    team_lead: HierarchyNodeOut | None = None
    project_manager: HierarchyNodeOut | None = None
    senior_manager: HierarchyNodeOut | None = None
    branch_manager: HierarchyNodeOut | None = None
    branch_head: HierarchyNodeOut | None = None


class ReportChartPoint(BaseModel):
    label: str
    value: float


class ReportChartSeries(BaseModel):
    name: str
    points: list[ReportChartPoint]


class ReportChart(BaseModel):
    # 'bar' | 'line' | 'donut' | 'stacked_bar' -- see report_helpers.py's
    # builder functions, one per chart_type.
    chart_type: str
    title: str
    series: list[ReportChartSeries]


class ReportAccessOut(BaseModel):
    """GET /api/reports/access -- crud.report_access for the caller."""

    scope: str
    report_types: list[str]
    # How many employees' data the caller sees; None = the whole company.
    employee_count: int | None = None


class ReportOut(BaseModel):
    report_type: str
    from_date: str
    to_date: str
    columns: list[str]
    rows: list[list[str]]
    summary: dict[str, str]
    # Additive -- existing report types that haven't been enriched yet
    # simply leave these at their defaults, so no existing caller/response
    # shape changes. See report_helpers.py for how these are built.
    charts: list[ReportChart] = []
    # '<metric label>' -> '<previous-period value> (<+/-% change>)', only
    # populated when the caller passed compare_previous=true.
    comparison: dict[str, str] | None = None


class ReportPdfLabelValue(BaseModel):
    label: str = Field(max_length=120)
    value: str = Field(max_length=300)


class ReportPdfExportRequest(BaseModel):
    """POST /api/reports/export/pdf -- the exact report the viewer is
    looking at (after filters / drill-down), rendered server-side into a
    branded PDF. See report_pdf_renderer.py."""
    title: str = Field(min_length=1, max_length=150)
    subtitle: str | None = Field(default=None, max_length=300)
    period_label: str | None = Field(default=None, max_length=120)
    generated_at: str | None = Field(default=None, max_length=60)
    filters: list[ReportPdfLabelValue] = Field(default_factory=list, max_length=20)
    # L-24: which report + the query parameters the screen used; the server
    # re-runs that report for the caller (report_export.py) and prints only
    # its own data. summary/columns/rows/charts sent by older clients are
    # accepted for compatibility but ignored.
    report_type: str = Field(min_length=1, max_length=40)
    params: dict[str, str | None] = Field(default_factory=dict, max_length=30)
    drill_down_column: str | None = Field(default=None, max_length=120)
    drill_down_value: str | None = Field(default=None, max_length=300)
    summary: list[ReportPdfLabelValue] = Field(default_factory=list, max_length=24)
    columns: list[str] = Field(default_factory=list, max_length=40)
    rows: list[list[str]] = Field(default_factory=list, max_length=10000)
    charts: list[ReportChart] = Field(default_factory=list, max_length=12)


# ── HRMS Super Admin (platform-level, public-schema) ───────────────────────
# Distinct from every schema above: these back routers/super_admin.py, which
# operates on public.tenants/admin_users/platform_settings/
# platform_notifications only -- never a tenant schema's own tables (except
# read-only, cross-tenant audit-log aggregation). Mirrors the reference
# Calviq Super Admin app's own data shapes 1:1.

class SuperAdminLoginRequest(BaseModel):
    email: str
    password: str = Field(max_length=1024)


class SuperAdminLoginResponse(BaseModel):
    access_token: str
    full_name: str
    email: str
    role: str


class HrmsAdminOut(BaseModel):
    """One row per HRMS tenant company -- the Super Admin app's "Admin"
    record (public.tenants, extended)."""
    id: uuid.UUID
    slug: str
    company_name: str
    contact_name: str | None
    contact_email: str | None
    contact_phone: str | None
    plan_type: str
    status: str
    trial_ends_at: datetime.date | None
    is_active: bool
    created_at: datetime.datetime
    # SA-13: what the HRMS enforces right now (active/trial/expired/blocked).
    effective_status: str | None = None

    model_config = {"from_attributes": True}


class HrmsAdminCreate(BaseModel):
    # C17: normalized (trim + lowercase) then checked against
    # provision_tenant.SLUG_PATTERN -- the slug becomes a schema name.
    tenant_slug: str = Field(min_length=2, max_length=60)

    @field_validator("tenant_slug")
    @classmethod
    def _validate_tenant_slug(cls, value: str) -> str:
        import re as _re

        slug = value.strip().lower()
        if not _re.fullmatch(r"[a-z][a-z0-9_-]{1,59}", slug):
            raise ValueError(
                "Tenant slug must be 2-60 characters: a lowercase letter first, then "
                "lowercase letters, digits, '_' or '-'."
            )
        return slug

    company_name: str = Field(min_length=1, max_length=150)
    contact_name: str = Field(min_length=1, max_length=150)
    contact_email: str = Field(min_length=3, max_length=150)
    contact_phone: str | None = Field(default=None, max_length=20)
    owner_password: NewPassword
    plan_type: str = Field(default="trial", pattern="^(trial|monthly|yearly)$")
    trial_days: int | None = Field(default=None, ge=1, le=365)

    @model_validator(mode="after")
    def _password_not_personal(self):
        password_policy.validate_password(
            self.owner_password, context=(self.contact_email, self.contact_name)
        )
        return self


class HrmsAdminUpdate(BaseModel):
    contact_name: str | None = Field(default=None, max_length=150)
    contact_email: str | None = Field(default=None, max_length=150)
    contact_phone: str | None = Field(default=None, max_length=20)
    company_name: str | None = Field(default=None, max_length=150)


class HrmsAdminExtendTrialRequest(BaseModel):
    days: int = Field(gt=0, le=365)


class HrmsAdminPlanUpdate(BaseModel):
    plan_type: str = Field(pattern="^(trial|monthly|yearly)$")


class HrmsAdminPasswordResetRequest(BaseModel):
    new_password: NewPassword


class SuperAdminProfileOut(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str | None
    role: str
    is_active: bool
    last_login_at: datetime.datetime | None
    created_at: datetime.datetime

    model_config = {"from_attributes": True}


class SuperAdminChangePasswordRequest(BaseModel):
    current_password: str = Field(max_length=1024)
    new_password: PlatformPassword


class SuperAdminUserCreate(BaseModel):
    full_name: str = Field(min_length=1, max_length=150)
    email: str = Field(min_length=3, max_length=150, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: PlatformPassword


class SuperAdminAnalyticsOut(BaseModel):
    total_admins: int
    active_count: int
    trial_count: int
    expired_count: int
    blocked_count: int
    monthly_count: int
    yearly_count: int
    mrr: float
    signup_by_month: list[dict]


class PlatformSettingsOut(BaseModel):
    trial_days: int
    currency: str
    timezone: str
    monthly_price: float
    yearly_price: float
    yearly_discount_pct: float
    stripe_public_key: str | None
    # SA-05: the secret itself is never returned, only whether one is set.
    has_stripe_secret: bool = False
    auto_renew: bool
    alert_enabled: bool

    model_config = {"from_attributes": True}


class PlatformSettingsUpdate(BaseModel):
    trial_days: int | None = Field(default=None, ge=1, le=365)
    currency: str | None = Field(default=None, max_length=10)
    timezone: str | None = Field(default=None, max_length=60)
    monthly_price: float | None = Field(default=None, ge=0)
    yearly_price: float | None = Field(default=None, ge=0)
    yearly_discount_pct: float | None = Field(default=None, ge=0, le=100)
    stripe_public_key: str | None = None
    stripe_secret_key: str | None = None
    auto_renew: bool | None = None
    alert_enabled: bool | None = None


class PlatformNotificationOut(BaseModel):
    id: uuid.UUID
    title: str
    body: str
    target_segment: str
    created_at: datetime.datetime
    is_read: bool
    # SA-07: set on the broadcast response -- organizations it reached.
    delivered_count: int | None = None

    model_config = {"from_attributes": True}


class BroadcastRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1)
    target_segment: str = Field(default="all", pattern="^(all|trial|expiring|active)$")


class PlatformAuditLogEntryOut(BaseModel):
    tenant_slug: str
    company_name: str
    action: str
    doctype: str
    document_id: str | None
    changes: dict | None
    created_at: datetime.datetime


# ── Experience & Relieving Letters (routers/exit_letters.py) ─────────────────

class ExitLetterTemplateOut(BaseModel):
    id: uuid.UUID
    letter_type: str
    name: str
    html_body: str
    css_styles: str
    is_active: bool
    version: int
    signatory_name: str | None = None
    signatory_designation: str | None = None
    updated_at: datetime.datetime | None = None
    # Inline previews (data URLs) of the uploaded company seal / authorized
    # signature -- null when not set.
    seal_image_data_url: str | None = None
    signature_image_data_url: str | None = None

    model_config = {"from_attributes": True}


class ExitLetterTemplateUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    html_body: str = Field(min_length=1)
    css_styles: str = ""
    is_active: bool = True
    signatory_name: str | None = Field(default=None, max_length=120)
    signatory_designation: str | None = Field(default=None, max_length=120)


class ExitLetterTemplatePreviewRequest(BaseModel):
    html_body: str = Field(min_length=1)
    css_styles: str = ""
    # Real exit request whose employee data is substituted -- never sample data.
    exit_request_id: uuid.UUID
    signatory_name: str | None = Field(default=None, max_length=120)
    signatory_designation: str | None = Field(default=None, max_length=120)
    # Unsaved seal / signature picked in the editor: null = use the saved
    # image, "" = none, else a PNG/JPG data URL (checked server-side).
    seal_image_data_url: str | None = Field(default=None, max_length=1_500_000)
    signature_image_data_url: str | None = Field(default=None, max_length=1_500_000)


class ExitLetterGenerateRequest(BaseModel):
    notes: str | None = Field(default=None, max_length=500)
    # Also email the new PDF to the employee (personal email first -- leavers
    # lose their work mailbox), queued after the letter is saved.
    email_employee: bool = False


class ExitLetterOut(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    exit_request_id: uuid.UUID
    letter_type: str
    letter_title: str
    version: int
    letter_number: str
    template_name: str
    template_version: int
    is_current: bool
    notes: str | None = None
    file_size: int
    generated_by_name: str | None = None
    generated_at: datetime.datetime | None = None
    # Set on generation when the PDF was also emailed (queued) to this address.
    emailed_to: str | None = None


class ExitLetterRequestOut(BaseModel):
    exit_request_id: uuid.UUID
    employee_id: uuid.UUID
    employee_name: str
    employee_code: str | None = None
    employee_email: str | None = None
    employee_personal_email: str | None = None
    designation: str | None = None
    department: str | None = None
    status: str
    resignation_date: datetime.date
    last_working_day: datetime.date | None = None
    # Experience & Relieving Letter eligibility (kept for older clients).
    eligible: bool
    eligibility_reason: str
    # Per document type: {"experience_relieving": {"eligible": ..., "reason": ...}, "fnf_statement": ...}
    eligibility: dict[str, dict] = {}
    fnf_status: str = "none"
    letters: list[ExitLetterOut]


# ── Full & Final Settlement (routers/fnf.py) ────────────────────────────────

class FnfLineIn(BaseModel):
    line_type: str = Field(pattern="^(earning|deduction)$")
    component: str = Field(min_length=1, max_length=60)
    description: str | None = Field(default=None, max_length=200)
    # Positive rupees; the line's type says whether it is paid or recovered.
    amount: Decimal = Field(gt=0, le=Decimal("999999999999.99"), decimal_places=2)


class FnfLineOut(BaseModel):
    id: uuid.UUID
    line_type: str
    component: str
    description: str | None = None
    amount: float

    model_config = {"from_attributes": True}


class FnfSaveRequest(BaseModel):
    lines: list[FnfLineIn] = Field(default_factory=list, max_length=60)
    notes: str | None = Field(default=None, max_length=2000)


class FnfApproveRequest(BaseModel):
    notes: str | None = Field(default=None, max_length=1000)


class FnfReopenRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)


class FnfMarkPaidRequest(BaseModel):
    # Omitted = today (company timezone); Payment Mode is optional.
    payment_date: datetime.date | None = None
    payment_mode: str | None = Field(default=None, max_length=120)
    payment_reference: str | None = Field(default=None, max_length=80)


class FnfSettlementOut(BaseModel):
    id: uuid.UUID
    status: str
    payable_amount: float
    recovery_amount: float
    net_amount: float
    notes: str | None = None
    prepared_by_name: str | None = None
    prepared_at: datetime.datetime | None = None
    approved_by_name: str | None = None
    approved_at: datetime.datetime | None = None
    approval_notes: str | None = None
    payment_date: datetime.date | None = None
    payment_mode: str | None = None
    payment_reference: str | None = None
    paid_at: datetime.datetime | None = None
    lines: list[FnfLineOut] = []


class FnfReferenceItemOut(BaseModel):
    category: str
    title: str
    detail: str
    severity: str
    suggested_line_type: str | None = None
    suggested_component: str | None = None
    suggested_description: str | None = None
    suggested_amount: float | None = None
    basis: str | None = None


class FnfSummaryOut(BaseModel):
    exit_request_id: uuid.UUID
    employee_id: uuid.UUID
    employee_name: str
    employee_code: str | None = None
    designation: str | None = None
    department: str | None = None
    exit_status: str
    resignation_date: datetime.date
    last_working_day: datetime.date | None = None
    fnf_status: str
    net_amount: float | None = None
    can_prepare: bool
    prepare_blocked_reason: str | None = None


class FnfDetailOut(FnfSummaryOut):
    date_of_joining: datetime.date | None = None
    service_period: str
    settlement: FnfSettlementOut | None = None
    reference_items: list[FnfReferenceItemOut] = []
    earning_components: list[str]
    deduction_components: list[str]
    # What the CURRENT user may do right now.
    can_edit: bool
    can_approve: bool
    approve_blocked_reason: str | None = None
    can_reopen: bool
    can_mark_paid: bool


# ── Overtime Settings / Report (routers/overtime_settings.py) ───────────────

class OvertimeSettingsOut(BaseModel):
    enabled: bool
    compensation_mode: str
    rate_basis: str
    rate_multiplier: float
    monthly_days_divisor: int
    hours_per_day: float
    fixed_hourly_rate: float | None = None
    comp_off_leave_type_id: uuid.UUID | None = None
    comp_off_leave_type_name: str | None = None
    comp_off_full_day_hours: float
    comp_off_half_day_hours: float
    min_minutes: int
    rounding: str
    updated_at: datetime.datetime | None = None
    saved: bool
    can_edit: bool
    leave_types: list[dict] = []
    # Payable overtime waiting for the next payroll run (editors only).
    pending_payroll_count: int = 0
    pending_payroll_amount: float = 0


class OvertimeSettingsUpdate(BaseModel):
    enabled: bool
    compensation_mode: str = Field(pattern="^(payable|comp_off)$")
    rate_basis: str = Field(pattern="^(basic|gross|fixed)$")
    rate_multiplier: float = Field(gt=0, le=5)
    monthly_days_divisor: int = Field(ge=1, le=31)
    hours_per_day: float = Field(gt=0, le=24)
    fixed_hourly_rate: float | None = Field(default=None, gt=0, le=100000)
    comp_off_leave_type_id: uuid.UUID | None = None
    comp_off_full_day_hours: float = Field(gt=0, le=24)
    comp_off_half_day_hours: float = Field(gt=0, le=24)
    min_minutes: int = Field(ge=0, le=600)
    rounding: str = Field(pattern="^(exact|nearest_15|nearest_30|floor_30)$")

    @model_validator(mode="after")
    def _consistent(self):
        if self.rate_basis == "fixed" and not self.fixed_hourly_rate:
            raise ValueError("Enter the fixed hourly overtime rate.")
        if self.comp_off_half_day_hours > self.comp_off_full_day_hours:
            raise ValueError("Half-day comp-off hours cannot exceed full-day hours.")
        return self


class OvertimeReportRowOut(BaseModel):
    request_id: uuid.UUID
    employee_id: uuid.UUID
    employee_name: str
    employee_code: str | None = None
    department: str | None = None
    work_date: datetime.date
    requested_hours: float
    approved_hours: float | None = None
    status: str
    approver_name: str
    reporting_manager_name: str | None = None
    day_type: str | None = None  # working | holiday | weekly_off
    start_time: datetime.time | None = None
    end_time: datetime.time | None = None
    decided_at: datetime.datetime | None = None
    decision_notes: str | None = None
    reason: str | None = None
    planned_start: datetime.datetime | None = None
    planned_end: datetime.datetime | None = None
    session_status: str | None = None
    actual_start: datetime.datetime | None = None
    actual_end: datetime.datetime | None = None
    actual_minutes: int | None = None
    actual_duration: str | None = None
    # "Missed Clock In (reminded 18:10)" / "Missed Clock Out (...)" / None
    missed_clock_in_at: datetime.datetime | None = None
    missed_clock_out_at: datetime.datetime | None = None
    missed_punches: str | None = None
    compensation_mode: str | None = None
    compensation_status: str | None = None
    amount: float | None = None
    leave_days: float | None = None
    payroll_period: str | None = None


class OvertimeReportOut(BaseModel):
    rows: list[OvertimeReportRowOut]
    total_requests: int
    approved_requests: int
    total_requested_hours: float = 0
    total_approved_hours: float = 0
    missed_clock_ins: int = 0
    missed_clock_outs: int = 0
    total_worked_minutes: int
    total_worked: str
    total_amount: float
    total_leave_days: float


# ── Holiday Calendar > Upload Document (routers/holiday_imports.py) ─────────

class HolidayImportRowOut(BaseModel):
    date: datetime.date
    day: str
    name: str
    branch_id: uuid.UUID | None = None
    branch_name: str
    is_optional: bool = False
    source_text: str | None = None
    warnings: list[str] = []
    # Already on the calendar (same date + name + applicability): skipped on import.
    duplicate: bool = False


class HolidayImportOut(BaseModel):
    id: uuid.UUID
    original_filename: str
    file_size: int | None = None
    status: str
    extracted_count: int
    imported_count: int
    skipped_count: int
    error: str | None = None
    uploaded_by_name: str | None = None
    uploaded_at: datetime.datetime
    imported_by_name: str | None = None
    imported_at: datetime.datetime | None = None
    rows: list[HolidayImportRowOut] = []
    result: dict | None = None
    branches: list[dict] = []


class HolidayImportConfirmRow(BaseModel):
    date: datetime.date
    name: str = Field(min_length=1, max_length=120)
    branch_id: uuid.UUID | None = None
    is_optional: bool = False


class HolidayImportConfirmIn(BaseModel):
    rows: list[HolidayImportConfirmRow] = Field(min_length=1, max_length=500)
