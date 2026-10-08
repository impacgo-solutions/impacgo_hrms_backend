import datetime
import uuid

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Integer, Numeric, SmallInteger, String, Text, Time, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base

# These models map onto tables in the tenant schema, resolved via PostgreSQL search_path
# (created from the project's real database dump). They intentionally map
# only the columns this backend needs — the tables carry additional
# columns (audit fields, other FKs) that aren't touched here.


class Currency(Base):
    __tablename__ = "currencies"
    __table_args__ = {"schema": "public"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    code: Mapped[str] = mapped_column(String(3))
    name: Mapped[str] = mapped_column(String(50))
    symbol: Mapped[str] = mapped_column(String(5))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Company(Base):
    __tablename__ = "core_companies"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(150))
    legal_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    gstin: Mapped[str | None] = mapped_column(String(15), nullable=True)
    pan: Mapped[str | None] = mapped_column(String(10), nullable=True)
    cin: Mapped[str | None] = mapped_column(String(25), nullable=True)
    default_currency_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("public.currencies.id")
    )
    fiscal_year_start_month: Mapped[int] = mapped_column(SmallInteger, default=4)
    address_line1: Mapped[str | None] = mapped_column(String(200), nullable=True)
    address_line2: Mapped[str | None] = mapped_column(String(200), nullable=True)
    city: Mapped[str | None] = mapped_column(String(80), nullable=True)
    state: Mapped[str | None] = mapped_column(String(80), nullable=True)
    pincode: Mapped[str | None] = mapped_column(String(10), nullable=True)
    country: Mapped[str] = mapped_column(String(80), default="India")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # -- Manual addition, see backend/db/add_employee_company_fields.sql --
    industry: Mapped[str | None] = mapped_column(String(100), nullable=True)
    founded_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    logo_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # -- Manual addition, see backend/db/add_company_email_domain.sql --
    # Drives the domain half of a new hire's auto-generated work email
    # (see crud.derive_company_email_domain) -- e.g. "brightscale.com" for
    # BrightScale, "saitechnologies.com" for Sai Technologies. Falls back to
    # a name-derived domain when unset so older/seeded companies keep working.
    email_domain: Mapped[str | None] = mapped_column(String(150), nullable=True)

    # -- Manual addition, see backend/db/add_org_hierarchy_fields.sql --
    # The single employee who oversees every Branch Manager company-wide --
    # the top rung of the dynamic reporting chain (see org_hierarchy.py).
    branch_head_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id"), nullable=True
    )

    # Audit columns -- created_by/created_at were columns on the real table
    # all along but never mapped here; auto-populated on every insert/update
    # by the session-wide audit-stamp hook (see database.py) rather than any
    # per-router code, same as every other model's audit columns below.
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Branch(Base):
    __tablename__ = "core_branches"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id")
    )
    code: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(120))
    gstin: Mapped[str | None] = mapped_column(String(15), nullable=True)
    is_head_office: Mapped[bool] = mapped_column(Boolean, default=False)
    city: Mapped[str | None] = mapped_column(String(80), nullable=True)
    state: Mapped[str | None] = mapped_column(String(80), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    address_line1: Mapped[str | None] = mapped_column(String(200), nullable=True)
    pincode: Mapped[str | None] = mapped_column(String(10), nullable=True)
    country: Mapped[str | None] = mapped_column(String(80), nullable=True)
    tz: Mapped[str | None] = mapped_column(String(60), nullable=True)

    # -- Manual addition, see backend/db/add_org_hierarchy_fields.sql --
    # The Branch Manager every Senior Manager/GM in this branch reports to
    # (see org_hierarchy.py).
    branch_manager_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id"), nullable=True
    )

    # Audit columns -- real table columns, never mapped before; auto-
    # populated by the session-wide audit-stamp hook (see database.py).
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BusinessUnit(Base):
    """Maps to core.business_units — a manual addition, see
    backend/db/manual_schema_additions.sql. Queries against this table 404
    with a clear Postgres error until that table is created."""

    __tablename__ = "core_business_units"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id")
    )
    name: Mapped[str] = mapped_column(String(150))
    head_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id"), nullable=True
    )
    cost_center: Mapped[str | None] = mapped_column(String(20), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # -- Manual addition: core_business_units.branch_id (nullable FK to
    # core_branches). Its original migration script was removed from
    # backend/db/; the column already exists on every live tenant schema.
    # A database rebuilt from the repo's DDL needs it added, or every
    # business-unit query 500s (SQLAlchemy selects every mapped column).
    branch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_branches.id"), nullable=True
    )


class Department(Base):
    __tablename__ = "core_departments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id")
    )
    branch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_branches.id"), nullable=True
    )
    code: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(120))
    head_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id"), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # -- Manual additions (backend/db/manual_schema_additions.sql, section 1) --
    business_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_business_units.id"), nullable=True
    )
    annual_budget: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)

    # -- Manual addition, see backend/db/add_org_hierarchy_fields.sql --
    # Dynamic reporting-hierarchy leader assignments for this department
    # (see org_hierarchy.py). hr_representative_id applies to every employee
    # in the department unconditionally; senior_manager_id/project_manager_id
    # feed the Team Lead/Project Manager/Senior Manager rungs of the chain.
    hr_representative_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id"), nullable=True
    )
    senior_manager_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id"), nullable=True
    )
    project_manager_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id"), nullable=True
    )

    # Audit columns -- real table columns, never mapped before; auto-
    # populated by the session-wide audit-stamp hook (see database.py).
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SubDepartment(Base):
    """Maps to core.sub_departments — a manual addition, see
    backend/db/manual_schema_additions.sql section 1."""

    __tablename__ = "core_sub_departments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    department_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_departments.id")
    )
    name: Mapped[str] = mapped_column(String(120))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class NumberSeries(Base):
    """core.number_series -- configurable, per-company/per-doctype document
    numbering. Backs auto-generated Employee Codes (doctype='employee')
    today: current_no is the last sequence number issued, formatted as
    f"{prefix}{str(current_no).zfill(padding)}{suffix}" (see
    crud.generate_employee_code). doctype is generic so other documents
    (invoices, POs, etc.) can reuse this same table later without a schema
    change -- fiscal_infix exists for that future use and isn't read by the
    employee-code generator.

    -- Manual addition, see backend/db/add_employee_number_series_suffix.sql --
    suffix didn't exist on the original table; that migration adds it.
    """

    __tablename__ = "core_number_series"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id")
    )
    doctype: Mapped[str] = mapped_column(String(60))
    prefix: Mapped[str] = mapped_column(String(20), default="")
    suffix: Mapped[str] = mapped_column(String(20), default="")
    fiscal_infix: Mapped[bool] = mapped_column(Boolean, default=True)
    padding: Mapped[int] = mapped_column(SmallInteger, default=5)
    current_no: Mapped[int] = mapped_column(BigInteger, default=0)


class Employee(Base):
    __tablename__ = "core_employees"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id")
    )
    branch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_branches.id"), nullable=True
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_departments.id"), nullable=True
    )
    designation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_designations.id"), nullable=True
    )
    employee_code: Mapped[str] = mapped_column(String(20))
    first_name: Mapped[str] = mapped_column(String(80))
    last_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    work_email: Mapped[str | None] = mapped_column(String(150), nullable=True)
    # core_employees.phone was dropped from the DB (see db/full_db.sql) --
    # do not re-add it here. Work phone lives nowhere on this table anymore;
    # personal_phone below is the only phone column left on core_employees.
    date_of_birth: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    gender: Mapped[str | None] = mapped_column(String(15), nullable=True)
    date_of_joining: Mapped[datetime.date] = mapped_column(Date)
    employment_type: Mapped[str] = mapped_column(String(20), default="full_time")
    # Contract employees only (backend/db/add_employee_contract_end_date.sql);
    # NULL for everyone else.
    contract_end_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    # Contract employees only (backend/db/add_employee_contract_rate.sql).
    contract_rate_amount: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    contract_rate_unit: Mapped[str | None] = mapped_column(String(10), nullable=True)
    reporting_manager_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), default="active")
    pan: Mapped[str | None] = mapped_column(String(10), nullable=True)
    uan: Mapped[str | None] = mapped_column(String(12), nullable=True)
    esi_number: Mapped[str | None] = mapped_column(String(17), nullable=True)
    bank_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    bank_account_no: Mapped[str | None] = mapped_column(String(30), nullable=True)
    bank_ifsc: Mapped[str | None] = mapped_column(String(11), nullable=True)
    tax_regime: Mapped[str | None] = mapped_column(String(10), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    confirmation_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    probation_end_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    dotted_line_manager_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id"), nullable=True
    )

    # -- Manual additions (backend/db/manual_schema_additions.sql, section 2) --
    sub_department_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_sub_departments.id"), nullable=True
    )
    work_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    band: Mapped[int | None] = mapped_column(nullable=True)
    annual_ctc: Mapped[int | None] = mapped_column(nullable=True)

    # -- Manual addition, see backend/db/add_employee_company_fields.sql --
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # created_at/updated_at are real table columns too, just never mapped
    # before now -- auto-populated by the session-wide audit-stamp hook (see
    # database.py) alongside created_by/updated_by above.
    created_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    blood_group: Mapped[str | None] = mapped_column(String(5), nullable=True)
    nationality: Mapped[str | None] = mapped_column(String(50), nullable=True)
    marital_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    personal_email: Mapped[str | None] = mapped_column(String(150), nullable=True)
    personal_phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    current_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    permanent_address: Mapped[str | None] = mapped_column(Text, nullable=True)

    # -- Manual addition, see backend/db/add_employee_photo_url.sql --
    # The stored file_url of this employee's self-uploaded profile picture
    # (storage.save_uploaded_file, entity_type="employee_photo"). NULL means
    # no photo set yet -- every avatar display falls back to initials.
    photo_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    branch: Mapped["Branch | None"] = relationship(foreign_keys=[branch_id])
    department: Mapped["Department | None"] = relationship(foreign_keys=[department_id])
    designation: Mapped["Designation | None"] = relationship(foreign_keys=[designation_id])
    sub_department: Mapped["SubDepartment | None"] = relationship(foreign_keys=[sub_department_id])
    reporting_manager: Mapped["Employee | None"] = relationship(
        foreign_keys=[reporting_manager_id], remote_side=[id]
    )
    dotted_line_manager: Mapped["Employee | None"] = relationship(
        foreign_keys=[dotted_line_manager_id], remote_side=[id]
    )


class User(Base):
    __tablename__ = "core_users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id")
    )
    email: Mapped[str] = mapped_column(String(150))
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    password_hash: Mapped[str] = mapped_column(Text)
    employee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), default="active")
    last_login_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    full_name: Mapped[str | None] = mapped_column(String(150), nullable=True)

    # Audit columns -- real table columns, never mapped before; auto-
    # populated by the session-wide audit-stamp hook (see database.py).
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    employee: Mapped["Employee | None"] = relationship()


class AdminUser(Base):
    """public.admin_users — global login store for all tenant users.
    Same UUID as the corresponding core.users row for FK compatibility."""
    __tablename__ = "admin_users"
    __table_args__ = {"schema": "public"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(150), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    role: Mapped[str] = mapped_column(String(30), nullable=False, default="user")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.datetime.utcnow
    )


class PublicUser(Base):
    """public.users — auth + tenant routing only.
    employee_id links to the tenant schema's core_employees.id."""
    __tablename__ = "users"
    __table_args__ = {"schema": "public"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(150), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    tenant_slug: Mapped[str] = mapped_column(String(60), nullable=False)
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(String(60), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.datetime.utcnow
    )
    # Separate employee identity from auth account identity.
    # Run: ALTER TABLE public.users ADD COLUMN IF NOT EXISTS employee_id uuid;
    #      UPDATE public.users SET employee_id = id WHERE employee_id IS NULL;
    employee_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class Tenant(Base):
    """public.tenants — registry mapping tenant slugs to companies. Also
    doubles as the HRMS Super Admin's "Admin" record (one row per HRMS
    tenant company): is_active stays the hard on/off switch it always
    was; plan_type/trial_ends_at/status/contact_* are additive columns
    added for the HRMS Super Admin app's trial/plan lifecycle (mirrors
    the reference Calviq Super Admin's own Admin shape) -- untouched by
    and invisible to every existing tenant-scoped endpoint."""
    __tablename__ = "tenants"
    __table_args__ = {"schema": "public"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(60), nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.datetime.utcnow
    )
    plan_type: Mapped[str] = mapped_column(String(10), nullable=False, default="trial")
    trial_ends_at: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="trial")
    contact_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String(150), nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(20), nullable=True)


class PlatformSettings(Base):
    """public.platform_settings — one singleton row of HRMS Super Admin
    platform-wide config (trial length, pricing, Stripe keys). Stored
    config only, same as the reference Calviq Super Admin app -- no real
    payment gateway is called anywhere by this app either."""
    __tablename__ = "platform_settings"
    __table_args__ = {"schema": "public"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trial_days: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=14)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="USD")
    timezone: Mapped[str] = mapped_column(String(60), nullable=False, default="IST (UTC+5:30)")
    monthly_price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=99)
    yearly_price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=999)
    yearly_discount_pct: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False, default=16)
    stripe_public_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    stripe_secret_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    auto_renew: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    alert_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.datetime.utcnow
    )


class PlatformAuditLog(Base):
    """public.platform_audit_logs -- SA-06: who did what on the HRMS Super
    Admin console (backend/db/add_platform_audit_logs.sql)."""
    __tablename__ = "platform_audit_logs"
    __table_args__ = {"schema": "public"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_admin_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    actor_email: Mapped[str | None] = mapped_column(String(150), nullable=True)
    action: Mapped[str] = mapped_column(String(60), nullable=False)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    tenant_slug: Mapped[str | None] = mapped_column(String(60), nullable=True)
    changes: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )


class PlatformNotification(Base):
    """public.platform_notifications — HRMS Super Admin broadcast/
    notification feed (its own Notifications page + Broadcast page)."""
    __tablename__ = "platform_notifications"
    __table_args__ = {"schema": "public"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    target_segment: Mapped[str] = mapped_column(String(20), nullable=False, default="all")
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.datetime.utcnow
    )
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class UserRole(Base):
    __tablename__ = "core_user_roles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_users.id"), primary_key=True
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_roles.id"), primary_key=True
    )
    branch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_branches.id"), nullable=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_users.id"), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.datetime.utcnow
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_users.id"), nullable=True
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.datetime.utcnow
    )

    role: Mapped["Role"] = relationship()


class Role(Base):
    __tablename__ = "core_roles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id")
    )
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    # Tenant-configurable Band -> Role hierarchy: which core_bands row this
    # Role belongs to. Plain nullable uuid, no DB-level FK -- same
    # app-enforced-only convention as Employee.band/core_bands itself (see
    # backend/db/add_band_role_designation_hierarchy.sql). Null means "not
    # yet mapped" -- crud.list_roles falls back to returning every role for
    # the company until at least one role in it gets a band_id.
    band_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_users.id"), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.datetime.utcnow
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_users.id"), nullable=True
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.datetime.utcnow
    )

    # -- Manual additions, see backend/db/add_role_scope_columns.sql --
    # Null means "not backfilled for this role name" -- routers/roles.py's
    # GET /api/roles/scopes falls back to its hardcoded dicts in that case,
    # matching this app's behavior before these columns existed.
    dashboard_scope: Mapped[str | None] = mapped_column(String(20), nullable=True)
    people_scope: Mapped[str | None] = mapped_column(String(20), nullable=True)
    blurb: Mapped[str | None] = mapped_column(Text, nullable=True)

    role_permissions: Mapped[list["RolePermission"]] = relationship(
        back_populates="role", cascade="all, delete-orphan"
    )


class Permission(Base):
    __tablename__ = "core_permissions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    code: Mapped[str] = mapped_column(String(120), unique=True)
    module: Mapped[str] = mapped_column(String(20))
    resource: Mapped[str] = mapped_column(String(60))
    action: Mapped[str] = mapped_column(String(30))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_users.id"), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.datetime.utcnow
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_users.id"), nullable=True
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.datetime.utcnow
    )


class RolePermission(Base):
    __tablename__ = "core_role_permissions"

    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_roles.id"), primary_key=True
    )
    permission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_permissions.id"), primary_key=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_users.id"), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.datetime.utcnow
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_users.id"), nullable=True
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.datetime.utcnow
    )

    role: Mapped["Role"] = relationship(back_populates="role_permissions")
    permission: Mapped["Permission"] = relationship()


class Designation(Base):
    __tablename__ = "core_designations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id")
    )
    name: Mapped[str] = mapped_column(String(120))
    band: Mapped[str | None] = mapped_column(String(40), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Tenant-configurable Role -> Designation hierarchy: which core_roles row
    # this Designation belongs to. Independent of `band` above (the older
    # free-text label) -- same nullable-uuid-no-FK convention as Role.band_id.
    # Null means "not yet mapped" -- crud.list_designations_for_role falls
    # back to every designation for the company until at least one gets a
    # role_id.
    role_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Direct tenant-configurable Designation -> Band mapping -- independent
    # of role_id above: a company can map a Designation straight to a Band
    # without going through a Role. Same nullable-uuid-no-FK convention.
    # Null means "not yet mapped" -- crud.list_designations_for_role's
    # band_id filter (and the hierarchy tree) simply has nothing to match
    # here until an admin sets it, or Employee Creation auto-creates a new
    # designation under a selected Band (see crud.create_employee).
    band_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_users.id"), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.datetime.utcnow
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_users.id"), nullable=True
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.datetime.utcnow
    )


class DesignationPermission(Base):
    """core.designation_permissions -- added via add_designation_permissions.sql.
    A DENY-LIST, not a grant list: presence of a (designation_id,
    permission_id) row means that Designation is EXPLICITLY RESTRICTED from
    that action, overriding whatever the user's Role(s) grant. Absence of a
    row means "not restricted by Designation -- deferred entirely to Role".
    Designation can only narrow access a Role already granted, never widen
    it on its own -- see crud.can_access_people_module.

    Schema matches that migration exactly: plain `id` PK (not a composite
    key like RolePermission), no DB-level FK on designation_id/permission_id
    (same app-enforced-only convention as Designation.role_id/band_id), no
    updated_by/updated_at -- a denial is either present or absent, never
    "edited" in place."""

    __tablename__ = "core_designation_permissions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    # Plain nullable-free uuid columns, no DB-level FK -- same convention as
    # Designation.role_id/band_id (the migration's own tables have none
    # either). No ORM relationship() for the same reason those two fields
    # don't have one -- crud.py looks up the related row by id directly.
    designation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    permission_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_users.id"), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.datetime.utcnow
    )


class Band(Base):
    """core.bands -- tenant-specific Band master (see backend/db/
    add_core_bands_table.sql). Deliberately independent of Designation.band
    (the free-text label above, grouping designation titles) and of
    Employee.band (a plain int with no FK) -- this table is the new,
    admin-manageable source of truth for the Add Employee "Grade / Band"
    dropdown's options, while everything that already read/wrote those two
    older columns keeps doing so unchanged."""

    __tablename__ = "core_bands"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id")
    )
    name: Mapped[str] = mapped_column(String(80))
    code: Mapped[str] = mapped_column(String(20))
    band_number: Mapped[int] = mapped_column(Integer)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=datetime.datetime.utcnow)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), default=datetime.datetime.utcnow)


class Shift(Base):
    __tablename__ = "hcm_shifts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id")
    )
    name: Mapped[str] = mapped_column(String(80))
    start_time: Mapped[datetime.time] = mapped_column(Time)
    end_time: Mapped[datetime.time] = mapped_column(Time)
    break_minutes: Mapped[int] = mapped_column(SmallInteger, default=60)
    grace_minutes: Mapped[int] = mapped_column(SmallInteger, default=10)
    is_night: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # -- Manual addition, see Break Management migration -- number of
    # breaks an employee assigned to this shift may take per attendance
    # session; break_minutes above is the TOTAL allowed break time across
    # all of them, not per-break.
    max_breaks: Mapped[int] = mapped_column(SmallInteger, default=1)


class Holiday(Base):
    __tablename__ = "hcm_holidays"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id")
    )
    branch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_branches.id"), nullable=True
    )
    holiday_date: Mapped[datetime.date] = mapped_column(Date)
    name: Mapped[str] = mapped_column(String(120))
    is_optional: Mapped[bool] = mapped_column(Boolean, default=False)
    # The Upload Document import that created it (add_holiday_imports.sql);
    # None = added by hand.
    source_import_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    branch: Mapped["Branch | None"] = relationship()


class HolidayImport(Base):
    """Leave > Holiday Calendar > Upload Document: the stored document, the
    holidays extracted from it, and the import result
    (add_holiday_imports.sql). status: extracted -> imported | cancelled;
    failed = nothing readable in the document."""

    __tablename__ = "hcm_holiday_imports"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_companies.id"))
    original_filename: Mapped[str] = mapped_column(String(255))
    file_url: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(12))
    extracted_rows: Mapped[list] = mapped_column(JSONB, default=list)
    extracted_count: Mapped[int] = mapped_column(Integer, default=0)
    imported_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    uploaded_by_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    uploaded_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    imported_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    imported_by_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    imported_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


LEAVE_TYPE_DEFAULT_EMPLOYMENT_TYPES = ("full_time", "part_time", "intern")


class LeaveType(Base):
    __tablename__ = "hcm_leave_types"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id")
    )
    name: Mapped[str] = mapped_column(String(80))
    code: Mapped[str] = mapped_column(String(10))
    is_paid: Mapped[bool] = mapped_column(Boolean, default=True)
    max_days_per_year: Mapped[float | None] = mapped_column(Numeric(5, 1), nullable=True)
    carry_forward: Mapped[bool] = mapped_column(Boolean, default=False)
    is_encashable: Mapped[bool] = mapped_column(Boolean, default=False)
    # backend/db/add_leave_type_employment_types.sql -- contract employees
    # are excluded unless HR adds 'contract' for this type.
    applicable_employment_types: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=lambda: list(LEAVE_TYPE_DEFAULT_EMPLOYMENT_TYPES)
    )


class AttendanceRecord(Base):
    __tablename__ = "hcm_attendance_records"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id")
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id")
    )
    attendance_date: Mapped[datetime.date] = mapped_column(Date)
    check_in: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    check_out: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    work_hours: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    # Real column on hcm_attendance_records (DEFAULT 0) that was never
    # mapped here -- every crud.py aggregate reading it via
    # getattr(r, "overtime_hours", None) silently fell through to None/0
    # regardless of approved Overtime Requests. See
    # crud.apply_overtime_to_attendance_record, the only writer.
    overtime_hours: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True, default=0)
    status: Mapped[str] = mapped_column(String(12), default="present")
    # M-13 (2026_10_06_attendance_work_rules.sql): the check-in verdict
    # (present / early_in / late), never overwritten by check-out.
    arrival_status: Mapped[str | None] = mapped_column(String(12), nullable=True)
    source: Mapped[str] = mapped_column(String(12), default="web")
    # Manually selected by the employee for this specific attendance day --
    # 'WFO' / 'WFH' / 'Client Site' (see crud.ATTENDANCE_WORK_MODES). Never
    # inferred from office timings, check-in method, or shift; null until
    # the employee actually picks one (see backend/db/manual_schema_
    # additions.sql, work-mode section).
    work_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)

    employee: Mapped["Employee"] = relationship(foreign_keys=[employee_id])
    breaks: Mapped[list["BreakRecord"]] = relationship(
        order_by="BreakRecord.break_start", viewonly=True,
        primaryjoin="AttendanceRecord.id == BreakRecord.attendance_record_id",
    )


class BreakRecord(Base):
    """Employee Break Management -- one row per break taken during an
    attendance session (see AttendanceRecord.breaks). break_end is null
    while the break is in progress; duration is always computed at read
    time from (break_end - break_start) rather than stored redundantly
    (see crud.serialize_break / crud.total_break_minutes), the same
    "derive, don't duplicate" convention _effective_attendance_status
    already uses for attendance status."""
    __tablename__ = "hcm_break_records"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id")
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id")
    )
    attendance_record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hcm_attendance_records.id")
    )
    break_start: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    break_end: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AttendanceRegularization(Base):
    __tablename__ = "hcm_attendance_regularizations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id")
    )
    attendance_date: Mapped[datetime.date] = mapped_column(Date)
    requested_in: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    requested_out: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(12), default="pending")
    approver_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id"), nullable=True
    )
    # -- Manual addition, see backend/db/add_approval_decision_fields.sql --
    decision_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    employee: Mapped["Employee"] = relationship(foreign_keys=[employee_id])


class LeaveRequest(Base):
    __tablename__ = "hcm_leave_requests"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id")
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id")
    )
    leave_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hcm_leave_types.id")
    )
    from_date: Mapped[datetime.date] = mapped_column(Date)
    to_date: Mapped[datetime.date] = mapped_column(Date)
    days: Mapped[float] = mapped_column(Numeric(4, 1))
    is_half_day: Mapped[bool] = mapped_column(Boolean, default=False)
    # 'morning' or 'afternoon' -- which half of the day, only meaningful
    # when is_half_day is True. Nullable/unused for a normal full-day
    # request. See crud.create_leave_request and schemas.
    # LeaveRequestCreate's half-day validator for how this is populated
    # and constrained.
    half_day_period: Mapped[str | None] = mapped_column(String(10), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="pending")
    approver_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id"), nullable=True
    )
    approved_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # -- Manual addition, see backend/db/add_approval_decision_fields.sql --
    # `approved_at` above is reused as "decided at" in application code (set
    # on any decision, not only approval) rather than adding a duplicate
    # column.
    decision_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # -- Manual addition, see backend/db/add_leave_request_link.sql --
    # Points at the sibling half of an auto-split request (e.g. requested
    # days exceeded balance -> primary leg + Loss-of-Pay leg). No FK
    # enforced at the DB level, matching leave_type_id's existing
    # unenforced-FK convention on this same table -- application code
    # (routers/leave.py) keeps both sides consistent.
    linked_leave_request_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    # -- Manual addition, see backend/db/add_leave_request_applied_date.sql --
    created_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # created_by/updated_by/updated_at are real table columns too, just
    # never mapped before now -- auto-populated by the session-wide
    # audit-stamp hook (see database.py).
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    employee: Mapped["Employee"] = relationship(foreign_keys=[employee_id])
    leave_type: Mapped["LeaveType"] = relationship()


class SalaryComponent(Base):
    __tablename__ = "hcm_salary_components"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id")
    )
    name: Mapped[str] = mapped_column(String(80))
    code: Mapped[str] = mapped_column(String(15))
    component_type: Mapped[str] = mapped_column(String(22))
    calc_type: Mapped[str] = mapped_column(String(10), default="fixed")
    is_taxable: Mapped[bool] = mapped_column(Boolean, default=True)


class SalaryStructure(Base):
    """Backs Payroll > Salary Structure / My Salary Structure -- previously
    those tabs showed a fixed, hardcoded formula table with no backend at
    all (see payroll_seed_data.py's own docstring on why nothing was
    fabricated here). No structures are seeded either, for the same
    reason -- this becomes real the moment rows exist in hcm.
    salary_structures/salary_structure_lines/salary_structure_assignments."""

    __tablename__ = "hcm_salary_structures"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id")
    )
    name: Mapped[str] = mapped_column(String(120))
    effective_from: Mapped[datetime.date] = mapped_column(Date)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Phase 4 of the redesign: optional eligibility/classification tags --
    # all nullable, null meaning "applies to everyone" (this structure's
    # original, still-supported behavior). Lets the structure list group/
    # filter instead of being one flat alphabetical pile of similarly-named
    # entries, and lets the Assign dialog suggest the structure(s) that
    # already match a given employee's own department/designation/branch.
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_departments.id"), nullable=True
    )
    designation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_designations.id"), nullable=True
    )
    branch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_branches.id"), nullable=True
    )
    grade: Mapped[str | None] = mapped_column(String(50), nullable=True)

    lines: Mapped[list["SalaryStructureLine"]] = relationship(cascade="all, delete-orphan")


class SalaryStructureLine(Base):
    __tablename__ = "hcm_salary_structure_lines"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    structure_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hcm_salary_structures.id")
    )
    component_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hcm_salary_components.id")
    )
    amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    percent_of: Mapped[str | None] = mapped_column(String(15), nullable=True)
    percent: Mapped[float | None] = mapped_column(Numeric(7, 3), nullable=True)

    component: Mapped["SalaryComponent"] = relationship()


class SalaryStructureAssignment(Base):
    __tablename__ = "hcm_salary_structure_assignments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id")
    )
    structure_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hcm_salary_structures.id")
    )
    from_date: Mapped[datetime.date] = mapped_column(Date)
    base_amount: Mapped[float] = mapped_column(Numeric(14, 2))
    annual_ctc: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    # Tiebreaker for "current assignment" lookups (order by from_date DESC,
    # created_at DESC) -- two assignments dated the same day (e.g. an admin
    # corrects a same-day mistake) previously had no reliable way to say
    # which one is actually current; from_date alone isn't unique enough.
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))

    structure: Mapped["SalaryStructure"] = relationship()


class SalaryStructureAssignmentOverride(Base):
    """Phase 1 of the salary-structure redesign: lets one specific
    employee's assignment deviate from their structure's own formula on a
    single component (e.g. "Transport = ₹2,000 flat for this person"),
    without editing the shared template -- which is what kept happening
    before this existed (admins creating near-duplicate templates just to
    express one person's exception). _resolve_structure_lines_monthly
    checks this table before falling back to the template's own line."""

    __tablename__ = "hcm_salary_structure_assignment_overrides"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    assignment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hcm_salary_structure_assignments.id")
    )
    component_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hcm_salary_components.id")
    )
    # 'amount' (a literal monthly ₹) or 'percent' (interpreted the same way
    # the overridden line itself would be -- % of CTC unless the line was
    # a % of Basic line, in which case % of Basic).
    override_type: Mapped[str] = mapped_column(String(10))
    value: Mapped[float] = mapped_column(Numeric(14, 2))
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id"), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))

    component: Mapped["SalaryComponent"] = relationship()


class VariablePayPayout(Base):
    """One confirmed, HR/Owner/Payroll-Admin-triggered Variable Pay payment
    for one employee in one specific payroll run -- the only way a
    component whose calc_type is 'var_annual' (see SalaryComponent) ever
    gets a non-zero amount on a payslip. Variable Pay is otherwise a pure
    annual entitlement (structure_assignment's line.amount * 12) that is
    never auto-divided across months.

    fiscal_year_start anchors "how much of the annual entitlement has been
    paid so far this fiscal year" (crud._fiscal_year_window's own window),
    computed fresh from these rows every time -- never cached/summed onto
    the assignment or component. The (employee_id, component_id,
    payroll_run_id) unique constraint stops the same month from
    accidentally recording two payouts of the same component."""

    __tablename__ = "hcm_variable_pay_payouts"
    __table_args__ = (
        UniqueConstraint(
            "employee_id", "component_id", "payroll_run_id",
            name="uq_variable_pay_payout_employee_component_run",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id")
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id")
    )
    structure_assignment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hcm_salary_structure_assignments.id")
    )
    component_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hcm_salary_components.id")
    )
    payroll_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hcm_payroll_runs.id")
    )
    fiscal_year_start: Mapped[datetime.date] = mapped_column(Date)
    amount: Mapped[float] = mapped_column(Numeric(14, 2))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_users.id"), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(String(300), nullable=True)

    employee: Mapped["Employee"] = relationship()
    component: Mapped["SalaryComponent"] = relationship()
    payroll_run: Mapped["PayrollRun"] = relationship()


class PayrollRun(Base):
    __tablename__ = "hcm_payroll_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id")
    )
    run_no: Mapped[str] = mapped_column(String(30))
    period_month: Mapped[int] = mapped_column(SmallInteger)
    period_year: Mapped[int] = mapped_column(SmallInteger)
    from_date: Mapped[datetime.date] = mapped_column(Date)
    to_date: Mapped[datetime.date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(12), default="draft")
    # H-11 period control (db/2026_10_06_payroll_period_control.sql):
    # draft -> processed (generated) -> approved -> locked -> paid. Users
    # are public user ids; see app/payroll_period.py.
    generated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    generated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    approved_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    locked_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    paid_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    flagged_slips: Mapped[int] = mapped_column(default=0)

    # Audit columns -- real table columns, never mapped before; auto-
    # populated by the session-wide audit-stamp hook (see database.py).
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PayrollArrear(Base):
    """H-12: a back-dated difference for an already-locked run
    (source_run_id), paid / recovered in a later run (target_run_id)."""

    __tablename__ = "hcm_payroll_arrears"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    employee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_employees.id"))
    source_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("hcm_payroll_runs.id"))
    target_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("hcm_payroll_runs.id"))
    amount: Mapped[float] = mapped_column(Numeric(14, 2))
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))


class LoanRecovery(Base):
    """M-31: one loan's EMI recovered on one slip; applied to the loan's
    outstanding balance when the run is locked (applied_at)."""

    __tablename__ = "hcm_loan_recoveries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    loan_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("hcm_loans.id"))
    employee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_employees.id"))
    payroll_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("hcm_payroll_runs.id"))
    slip_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("hcm_salary_slips.id"))
    amount: Mapped[float] = mapped_column(Numeric(14, 2))
    applied_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))


class SalarySlip(Base):
    __tablename__ = "hcm_salary_slips"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    payroll_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hcm_payroll_runs.id")
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id")
    )
    working_days: Mapped[float] = mapped_column(Numeric(4, 1))
    lop_days: Mapped[float] = mapped_column(Numeric(4, 1), default=0)
    gross_pay: Mapped[float] = mapped_column(Numeric(14, 2))
    total_deductions: Mapped[float] = mapped_column(Numeric(14, 2))
    net_pay: Mapped[float] = mapped_column(Numeric(14, 2))
    status: Mapped[str] = mapped_column(String(12), default="draft")
    # Payroll > Travel & Expense Reimbursements -- the sum of this slip's
    # hcm_salary_slip_reimbursement_lines. Deliberately NEVER added into
    # gross_pay/total_deductions/net_pay above (those three keep their
    # exact pre-existing meaning/calculation) -- a reimbursement is neither
    # salary nor a deduction, just a separate real amount paid out
    # alongside this payslip. See crud._compute_and_write_slip.
    reimbursements_total: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    # H-14 / H-16 (db/2026_10_06_payroll_period_control.sql): payable
    # (prorated) days, comma-separated flags (e.g. deductions_capped), and
    # the deduction shortfall recovered in the next run.
    payable_days: Mapped[float | None] = mapped_column(Numeric(5, 1), nullable=True)
    flags: Mapped[str | None] = mapped_column(String(200), nullable=True)
    deduction_carry_forward: Mapped[float] = mapped_column(Numeric(14, 2), default=0)


class ExpenseClaim(Base):
    """Backs both Payroll > Reimbursements (a single-line claim) and, in a
    later phase, Travel & Expenses' fuller multi-line Expense Reports —
    same underlying entity, core.expense_claim_lines carries the detail."""

    __tablename__ = "hcm_expense_claims"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id")
    )
    claim_no: Mapped[str] = mapped_column(String(30))
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id")
    )
    claim_date: Mapped[datetime.date] = mapped_column(Date)
    purpose: Mapped[str | None] = mapped_column(String(200), nullable=True)
    total_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    status: Mapped[str] = mapped_column(String(12), default="draft")
    approver_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id"), nullable=True
    )

    # -- Manual addition (backend/db/manual_schema_additions.sql section 9) --
    current_step: Mapped[int] = mapped_column(SmallInteger, default=1)

    # -- Manual addition, see backend/db/add_approval_decision_fields.sql --
    decision_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Audit columns -- real table columns, never mapped before; auto-
    # populated by the session-wide audit-stamp hook (see database.py).
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    employee: Mapped["Employee"] = relationship(foreign_keys=[employee_id])
    lines: Mapped[list["ExpenseClaimLine"]] = relationship(cascade="all, delete-orphan")


class ExpenseClaimLine(Base):
    __tablename__ = "hcm_expense_claim_lines"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    claim_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hcm_expense_claims.id")
    )
    expense_date: Mapped[datetime.date] = mapped_column(Date)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    amount: Mapped[float] = mapped_column(Numeric(14, 2))

    # -- Manual addition (backend/db/manual_schema_additions.sql section 9) --
    category: Mapped[str | None] = mapped_column(String(40), nullable=True)


class Loan(Base):
    """Maps to hcm.loans — a manual addition, see
    backend/db/manual_schema_additions.sql section 12."""

    __tablename__ = "hcm_loans"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id")
    )
    loan_type: Mapped[str] = mapped_column(String(60))
    principal_amount: Mapped[float] = mapped_column(Numeric(14, 2))
    emi_amount: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    outstanding_balance: Mapped[float] = mapped_column(Numeric(14, 2))
    status: Mapped[str] = mapped_column(String(12), default="active")


class TaxDeclaration(Base):
    """Maps to hcm.tax_declarations — a manual addition, see
    backend/db/manual_schema_additions.sql section 12."""

    __tablename__ = "hcm_tax_declarations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id")
    )
    fiscal_year: Mapped[str] = mapped_column(String(9))
    tax_regime: Mapped[str] = mapped_column(String(10))
    hra_claimed: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    section_80c: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    # M-28/M-30 (db/2026_10_06_payroll_period_control.sql).
    section_80d: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    section_80ccd_1b: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    home_loan_interest: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    status: Mapped[str] = mapped_column(String(12), default="draft")


class JobOpening(Base):
    __tablename__ = "hcm_job_openings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id")
    )
    title: Mapped[str] = mapped_column(String(150))
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_departments.id"), nullable=True
    )
    vacancies: Mapped[int] = mapped_column(SmallInteger, default=1)
    status: Mapped[str] = mapped_column(String(12), default="open")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    designation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_designations.id"), nullable=True
    )
    branch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_branches.id"), nullable=True
    )

    # -- Manual addition (backend/db/manual_schema_additions.sql section 11) --
    employment_type: Mapped[str] = mapped_column(String(20), default="full_time")
    posted_date: Mapped[datetime.date] = mapped_column(Date)

    # Audit columns -- real table columns, never mapped before; auto-
    # populated by the session-wide audit-stamp hook (see database.py).
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # -- Recruitment workflow (backend/db/add_recruitment_workflow.sql) --
    # status: draft | open | paused | closed (legacy 'on_hold' reads as paused)
    requisition_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    work_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    reporting_manager_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    experience_min: Mapped[float | None] = mapped_column(Numeric(4, 1), nullable=True)
    experience_max: Mapped[float | None] = mapped_column(Numeric(4, 1), nullable=True)
    salary_min: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    salary_max: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    required_skills: Mapped[str | None] = mapped_column(Text, nullable=True)
    qualifications: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_joining_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    hiring_team: Mapped[list] = mapped_column(JSONB, default=list)
    interview_stages: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    publish_scope: Mapped[str | None] = mapped_column(String(10), nullable=True)
    published_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    department: Mapped["Department | None"] = relationship()


class Candidate(Base):
    __tablename__ = "hcm_candidates"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id")
    )
    name: Mapped[str] = mapped_column(String(150))
    email: Mapped[str | None] = mapped_column(String(150), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    source: Mapped[str | None] = mapped_column(String(40), nullable=True)

    # -- Manual addition (backend/db/manual_schema_additions.sql section 11) --
    years_experience: Mapped[float | None] = mapped_column(Numeric(4, 1), nullable=True)

    # -- Recruitment workflow (backend/db/add_recruitment_workflow.sql) --
    current_company: Mapped[str | None] = mapped_column(String(150), nullable=True)
    current_designation: Mapped[str | None] = mapped_column(String(150), nullable=True)
    current_ctc: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    expected_ctc: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    notice_period_days: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    current_location: Mapped[str | None] = mapped_column(String(150), nullable=True)
    skills: Mapped[str | None] = mapped_column(Text, nullable=True)
    qualification: Mapped[str | None] = mapped_column(String(200), nullable=True)
    work_authorization: Mapped[str | None] = mapped_column(String(80), nullable=True)
    linkedin_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    resume_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    resume_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    resume_uploaded_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class JobApplication(Base):
    __tablename__ = "hcm_job_applications"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    opening_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hcm_job_openings.id")
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hcm_candidates.id")
    )
    stage: Mapped[str] = mapped_column(String(20), default="applied")
    rating: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)

    # -- Recruitment workflow (backend/db/add_recruitment_workflow.sql) --
    # status: see recruitment_workflow.APPLICATION_STATUSES; `stage` keeps
    # the legacy 5-column board value in step for the old endpoints.
    status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    stage_index: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    stage_results: Mapped[dict] = mapped_column(JSONB, default=dict)
    previous_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    hold_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    hold_review_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    rejection_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    withdrawal_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    withdrawal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    closed_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    applied_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    current_stage_entered_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    employee_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    opening: Mapped["JobOpening"] = relationship()
    candidate: Mapped["Candidate"] = relationship()


class Interview(Base):
    """Interviewer is a free-text name in the frontend's Interview model
    (not an Employee cross-reference), so hydrating just needs
    interviewer_id -> employee.name -- unblocked now that real employees
    exist (see crud.list_interviews)."""

    __tablename__ = "hcm_interviews"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hcm_job_applications.id")
    )
    round_no: Mapped[int] = mapped_column(SmallInteger, default=1)
    # Empty while the interview is at the invitation stage (interviewers
    # still confirming availability -- backend/db/add_interview_invitations.sql).
    scheduled_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    interviewer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id"), nullable=True
    )
    result: Mapped[str | None] = mapped_column(String(12), nullable=True)
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)

    # -- Recruitment workflow (backend/db/add_recruitment_workflow.sql) --
    stage_key: Mapped[str | None] = mapped_column(String(40), nullable=True)
    stage_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    interview_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    scheduled_end: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    mode: Mapped[str | None] = mapped_column(String(12), nullable=True)
    location: Mapped[str | None] = mapped_column(Text, nullable=True)
    meeting_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    candidate_attendance: Mapped[str | None] = mapped_column(String(12), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    reschedule_count: Mapped[int] = mapped_column(SmallInteger, default=0)
    feedback_required: Mapped[bool] = mapped_column(Boolean, default=True)
    completed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # -- Round-based interviews (backend/db/add_interview_rounds.sql) --
    # round_id set = this row is one candidate's instance of a shared round
    # session; NULL = an existing/legacy invite-flow interview, untouched.
    round_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hcm_interview_rounds.id"), nullable=True
    )
    started_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    ended_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    hr_decision: Mapped[str | None] = mapped_column(String(12), nullable=True)
    hr_decision_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    hr_decision_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    hr_decision_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    application: Mapped["JobApplication"] = relationship()
    interviewer: Mapped["Employee | None"] = relationship()
    round: Mapped["InterviewRound | None"] = relationship()
    participants: Mapped[list["InterviewParticipant"]] = relationship(
        back_populates="interview", cascade="all, delete-orphan"
    )
    feedback_entries: Mapped[list["InterviewFeedback"]] = relationship(
        back_populates="interview", cascade="all, delete-orphan"
    )


class InterviewRound(Base):
    """A shared interview-round session on a job opening (Recruitment >
    Job Opening > Interview Rounds, backend/db/add_interview_rounds.sql):
    one date/time + interviewer pool per round, applying to every candidate
    who reaches that stage. `round_key` is the matching interview_stages
    stage key, so a round is 1:1 with a pipeline stage by construction."""

    __tablename__ = "hcm_interview_rounds"
    __table_args__ = (UniqueConstraint("opening_id", "round_key"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_companies.id"))
    opening_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("hcm_job_openings.id"))
    round_key: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(120))
    sort_order: Mapped[int] = mapped_column(SmallInteger, default=1)
    interview_type: Mapped[str] = mapped_column(String(20), default="interview")
    scheduled_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scheduled_end: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    mode: Mapped[str | None] = mapped_column(String(12), nullable=True)
    location: Mapped[str | None] = mapped_column(Text, nullable=True)
    meeting_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="scheduled")
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    reschedule_count: Mapped[int] = mapped_column(SmallInteger, default=0)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    opening: Mapped["JobOpening"] = relationship()
    interviewers: Mapped[list["InterviewRoundInterviewer"]] = relationship(
        back_populates="round", cascade="all, delete-orphan"
    )


class InterviewRoundInterviewer(Base):
    """The checkbox-picked eligible-interviewer pool for a round. Soft
    -removable (removed_at) so reassigning an interviewer keeps history
    instead of deleting the row."""

    __tablename__ = "hcm_interview_round_interviewers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    round_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("hcm_interview_rounds.id"))
    employee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_employees.id"))
    assigned_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    assigned_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    removed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    removed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    round: Mapped["InterviewRound"] = relationship(back_populates="interviewers")
    employee: Mapped["Employee"] = relationship()


class InterviewParticipant(Base):
    __tablename__ = "hcm_interview_participants"
    __table_args__ = (UniqueConstraint("interview_id", "employee_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    interview_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("hcm_interviews.id"))
    employee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_employees.id"))
    role: Mapped[str] = mapped_column(String(20), default="interviewer")
    # The interviewer's answer to the invitation: None/pending, accepted,
    # declined, reschedule (backend/db/add_interview_invitations.sql).
    attendance: Mapped[str | None] = mapped_column(String(12), nullable=True)
    response_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposed_availability: Mapped[str | None] = mapped_column(Text, nullable=True)
    responded_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invited_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invited_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    replaced_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    interview: Mapped["Interview"] = relationship(back_populates="participants")
    employee: Mapped["Employee"] = relationship()


class InterviewFeedback(Base):
    __tablename__ = "hcm_interview_feedback"
    __table_args__ = (UniqueConstraint("interview_id", "interviewer_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    interview_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("hcm_interviews.id"))
    interviewer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_employees.id"))
    recommendation: Mapped[str] = mapped_column(String(12))
    rating: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    scores: Mapped[dict] = mapped_column(JSONB, default=dict)
    strengths: Mapped[str | None] = mapped_column(Text, nullable=True)
    concerns: Mapped[str | None] = mapped_column(Text, nullable=True)
    comments: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    submitted_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    interview: Mapped["Interview"] = relationship(back_populates="feedback_entries")
    interviewer: Mapped["Employee"] = relationship()


class Offer(Base):
    __tablename__ = "hcm_offers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hcm_job_applications.id")
    )
    offered_ctc: Mapped[float] = mapped_column(Numeric(14, 2))
    offer_date: Mapped[datetime.date] = mapped_column(Date)
    proposed_joining_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="sent")

    # -- Recruitment workflow (backend/db/add_recruitment_workflow.sql) --
    version: Mapped[int] = mapped_column(Integer, default=1)
    designation: Mapped[str | None] = mapped_column(String(150), nullable=True)
    department_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    branch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    work_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    employment_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    reporting_manager_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    salary_breakup: Mapped[str | None] = mapped_column(Text, nullable=True)
    benefits: Mapped[str | None] = mapped_column(Text, nullable=True)
    probation_months: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    notice_period_days: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    working_hours: Mapped[str | None] = mapped_column(String(80), nullable=True)
    expiry_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    terms: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    submitted_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    approved_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    decision_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    last_sent_to: Mapped[str | None] = mapped_column(String(150), nullable=True)
    viewed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    responded_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    response_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    accepted_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decline_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    withdraw_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # -- Contract offers (backend/db/add_offer_contract_terms.sql) --
    # 'rate' only for employment_type == 'contract'; every other offer keeps
    # the column default 'ctc' and leaves the rest NULL.
    compensation_type: Mapped[str] = mapped_column(String(10), default="ctc")
    rate_amount: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    rate_unit: Mapped[str | None] = mapped_column(String(10), nullable=True)
    contract_duration_months: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    contract_end_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    application: Mapped["JobApplication"] = relationship()


class RecruitmentSettings(Base):
    """Per-company recruitment configuration; no row = the defaults in
    recruitment_workflow.DEFAULTS."""

    __tablename__ = "hcm_recruitment_settings"

    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_companies.id"), primary_key=True)
    offer_approval_required: Mapped[bool] = mapped_column(Boolean, default=True)
    offer_expiry_days: Mapped[int] = mapped_column(SmallInteger, default=7)
    default_interview_stages: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    sources: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    rejection_reasons: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    preboarding_checklist: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    verification_failure_policy: Mapped[str] = mapped_column(String(10), default="hold")
    allow_verification_override: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RecruitmentHistory(Base):
    """Append-only stage history / audit trail of every recruitment record
    (never deleted, also for rejected / withdrawn applications)."""

    __tablename__ = "hcm_recruitment_history"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    entity_type: Mapped[str] = mapped_column(String(30))
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    application_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    action: Mapped[str] = mapped_column(String(60))
    old_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    new_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    comments: Mapped[str | None] = mapped_column(Text, nullable=True)
    # `metadata` is reserved on declarative classes.
    meta: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    actor_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))


class Preboarding(Base):
    """New joiner: between offer acceptance and employee creation."""

    __tablename__ = "hcm_preboardings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_companies.id"))
    application_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("hcm_job_applications.id"), unique=True)
    candidate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("hcm_candidates.id"))
    offer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("hcm_offers.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(24))
    joining_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    designation: Mapped[str | None] = mapped_column(String(150), nullable=True)
    department_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    branch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    reporting_manager_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    employment_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    work_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    joiner_details: Mapped[dict] = mapped_column(JSONB, default=dict)
    verification_status: Mapped[str] = mapped_column(String(24), default="pending")
    verification_override: Mapped[bool] = mapped_column(Boolean, default=False)
    override_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    joining_status: Mapped[str | None] = mapped_column(String(12), nullable=True)
    joining_confirmed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    joining_confirmed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    joining_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    no_show_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    employee_status: Mapped[str] = mapped_column(String(12), default="not_created")
    employee_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    employee_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    employee_created_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    application: Mapped["JobApplication"] = relationship()
    candidate: Mapped["Candidate"] = relationship()
    offer: Mapped["Offer | None"] = relationship()
    tasks: Mapped[list["PreboardingTask"]] = relationship(
        back_populates="preboarding", order_by="PreboardingTask.sort_order", cascade="all, delete-orphan"
    )


class PreboardingTask(Base):
    __tablename__ = "hcm_preboarding_tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    preboarding_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("hcm_preboardings.id"))
    category: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(150))
    task_type: Mapped[str] = mapped_column(String(16))
    field_group: Mapped[str | None] = mapped_column(String(20), nullable=True)
    required: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(24))
    sort_order: Mapped[int] = mapped_column(SmallInteger, default=0)
    due_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    preboarding: Mapped["Preboarding"] = relationship(back_populates="tasks")
    documents: Mapped[list["PreboardingDocument"]] = relationship(
        back_populates="task", order_by="PreboardingDocument.version", cascade="all, delete-orphan"
    )


class PreboardingDocument(Base):
    """One uploaded version of a preboarding document (all versions kept)."""

    __tablename__ = "hcm_preboarding_documents"
    __table_args__ = (UniqueConstraint("task_id", "version"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("hcm_preboarding_tasks.id"))
    preboarding_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("hcm_preboardings.id"))
    version: Mapped[int] = mapped_column(Integer)
    file_url: Mapped[str] = mapped_column(Text)
    original_filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    uploaded_by_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    uploaded_via: Mapped[str] = mapped_column(String(12), default="hr")
    uploaded_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    reviewed_by_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    reviewed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    task: Mapped["PreboardingTask"] = relationship(back_populates="documents")


class CandidatePortalLink(Base):
    """A candidate self-service link (offer response / preboarding
    uploads); only the SHA-256 of the token is stored."""

    __tablename__ = "hcm_candidate_portal_links"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    purpose: Mapped[str] = mapped_column(String(12))
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))


class CompanyOkr(Base):
    """Maps to hcm.company_okrs — a manual addition, see
    backend/db/manual_schema_additions.sql section 7."""

    __tablename__ = "hcm_company_okrs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id")
    )
    level: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(255))
    owner_name: Mapped[str] = mapped_column(String(150))
    # FE4: the owning employee (db/add_okr_owner_employee_id.sql). NULL for
    # legacy rows whose owner_name matched no single employee.
    owner_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id"), nullable=True
    )
    progress_pct: Mapped[int] = mapped_column(SmallInteger, default=0)


class AppraisalCycle(Base):
    __tablename__ = "hcm_appraisal_cycles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id")
    )
    name: Mapped[str] = mapped_column(String(80))
    from_date: Mapped[datetime.date] = mapped_column(Date)
    to_date: Mapped[datetime.date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(12), default="active")

    # -- Manual addition (backend/db/manual_schema_additions.sql section 7) --
    participant_count: Mapped[int] = mapped_column(default=0)


class Recognition(Base):
    __tablename__ = "hcm_recognitions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id")
    )
    badge: Mapped[str] = mapped_column(String(80))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    given_on: Mapped[datetime.date] = mapped_column(Date)

    employee: Mapped["Employee"] = relationship()


class TrainingCourse(Base):
    __tablename__ = "hcm_training_courses"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id")
    )
    title: Mapped[str] = mapped_column(String(200))
    course_type: Mapped[str] = mapped_column(String(20), default="internal")
    duration_hours: Mapped[float | None] = mapped_column(Numeric(6, 1), nullable=True)

    # -- Manual addition (backend/db/manual_schema_additions.sql section 7) --
    category: Mapped[str | None] = mapped_column(String(60), nullable=True)
    duration_label: Mapped[str | None] = mapped_column(String(30), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(150), nullable=True)


class TrainingEnrollment(Base):
    """Backs the Learning > Course Catalog cards' real enrolled-count/
    completion stats (see crud.list_training_courses_with_stats)."""

    __tablename__ = "hcm_training_enrollments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    course_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("hcm_training_courses.id"))
    employee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_employees.id"))
    status: Mapped[str] = mapped_column(String(15), default="enrolled")
    completion_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    score: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)


class Certification(Base):
    """Not hydrated as its own employee-linked list until now -- see
    crud.list_certifications, unblocked now that real employees exist."""

    __tablename__ = "hcm_certifications"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id")
    )
    name: Mapped[str] = mapped_column(String(200))
    issuer: Mapped[str | None] = mapped_column(String(150), nullable=True)
    issue_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    expiry_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)


class BenefitCategory(Base):
    """Maps to hcm.benefit_categories — a manual addition, see
    backend/db/manual_schema_additions.sql section 3. Backs only the
    *custom* categories added via New Benefit Plan — the 10 built-in cards
    are static UI content (icons/colors hardcoded in benefits_screen.dart),
    not seed data, so they stay local."""

    __tablename__ = "hcm_benefit_categories"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id")
    )
    name: Mapped[str] = mapped_column(String(120))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    items: Mapped[list["BenefitCategoryItem"]] = relationship(
        back_populates="category", cascade="all, delete-orphan"
    )
    assignments: Mapped[list["BenefitCategoryAssignment"]] = relationship(
        back_populates="category", cascade="all, delete-orphan"
    )


class BenefitCategoryItem(Base):
    __tablename__ = "hcm_benefit_category_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    category_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hcm_benefit_categories.id")
    )
    item: Mapped[str] = mapped_column(String(200))

    category: Mapped["BenefitCategory"] = relationship(back_populates="items")


class BenefitCategoryAssignment(Base):
    """Maps to hcm.benefit_category_assignments — which specific employees a
    Benefit Plan is visible to. A plan with zero assignment rows is visible
    to no one (never "everyone" by default): the admin must explicitly pick
    who it applies to via New/Edit Benefit Plan's Assign to Employees
    control."""

    __tablename__ = "hcm_benefit_category_assignments"
    __table_args__ = (
        UniqueConstraint("category_id", "employee_id", name="uq_benefit_category_assignment"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    category_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hcm_benefit_categories.id")
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id")
    )

    category: Mapped["BenefitCategory"] = relationship(back_populates="assignments")


class EmployeeBenefits(Base):
    """Model exists for completeness, not hydrated: per-employee, and the
    demo employees whose benefitsInfo the Enrollment tab shows don't exist
    as backend rows -- same reasoning as Recognition/Certification."""

    __tablename__ = "hcm_employee_benefits"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id")
    )
    insurance_plan: Mapped[str | None] = mapped_column(String(150), nullable=True)
    esop_units: Mapped[int] = mapped_column(default=0)
    dependents_covered: Mapped[int] = mapped_column(SmallInteger, default=0)
    learning_budget_total: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    learning_budget_used: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    cab_facility: Mapped[bool] = mapped_column(Boolean, default=False)
    meal_card: Mapped[bool] = mapped_column(Boolean, default=False)
    internet_reimbursement: Mapped[bool] = mapped_column(Boolean, default=False)
    wellness_program: Mapped[bool] = mapped_column(Boolean, default=False)


class AssetInventoryItem(Base):
    """Maps to hcm.asset_inventory — a manual addition, see
    backend/db/manual_schema_additions.sql section 4. Not employee-linked,
    so unlike assignments/returns below, this is safe to hydrate + create."""

    __tablename__ = "hcm_asset_inventory"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id")
    )
    asset_tag: Mapped[str] = mapped_column(String(40))
    asset_type: Mapped[str] = mapped_column(String(60))
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(String(15), default="available")
    purchase_value: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    purchased_on: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)


class AssetAssignment(Base):
    __tablename__ = "hcm_asset_assignments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hcm_asset_inventory.id")
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id")
    )
    assigned_on: Mapped[datetime.date] = mapped_column(Date)
    returned_on: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    return_condition: Mapped[str | None] = mapped_column(String(60), nullable=True)
    return_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    asset: Mapped["AssetInventoryItem"] = relationship()
    employee: Mapped["Employee"] = relationship()


class AssetRecoveryDeduction(Base):
    """Assets > Asset Recovery Deduction -- an approved rupee amount to
    recover from one employee (e.g. an unreturned/damaged asset), targeted
    at a specific payroll period. Picked up automatically by
    crud._compute_and_write_slip as a real payslip deduction line when
    Payroll Settings > Deductions Configuration has asset_deduction_enabled
    turned on (see crud.get_approved_asset_recovery_amount). Maps to
    hcm.asset_recovery_deductions, added via
    backend/db/add_asset_recovery_deductions.sql.

    applied_payroll_run_id is the duplicate-prevention guard: once a
    payroll run has consumed this row, it's excluded from every other
    run's eligibility query, so the same approved amount is never deducted
    twice across two different months' payroll runs -- it stays eligible
    for a still-draft run being regenerated in place, since that query
    also matches the run that already consumed it."""

    __tablename__ = "hcm_asset_recovery_deductions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_companies.id"))
    employee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_employees.id"))
    asset_assignment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hcm_asset_assignments.id"), nullable=True
    )
    amount: Mapped[float] = mapped_column(Numeric(12, 2))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="pending")
    target_period_month: Mapped[int] = mapped_column(SmallInteger)
    target_period_year: Mapped[int] = mapped_column(SmallInteger)
    approver_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id"), nullable=True
    )
    decision_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    applied_payroll_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hcm_payroll_runs.id"), nullable=True
    )
    applied_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    employee: Mapped["Employee"] = relationship(foreign_keys=[employee_id])
    asset_assignment: Mapped["AssetAssignment | None"] = relationship()


class DocumentRecord(Base):
    """Backs Upload Document (People > Employee Documents). Picking a mock
    (not-yet-hydrated) employee still shows the upload locally immediately
    either way (best-effort, same fallback pattern as every other write in
    this app) — it just won't find a matching employee_id server-side."""

    __tablename__ = "hcm_document_records"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id")
    )
    document_type: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(15), default="pending")
    uploaded_on: Mapped[datetime.date] = mapped_column(Date)
    file_url: Mapped[str | None] = mapped_column(Text, nullable=True)


class PolicyDocument(Base):
    """Maps to hcm.policy_documents — a manual addition, see
    backend/db/manual_schema_additions.sql section 5. Backs both Company
    Policies (doc_kind='policy') and Templates (doc_kind='template') —
    neither is employee-linked, so both are safe to hydrate."""

    __tablename__ = "hcm_policy_documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id")
    )
    doc_kind: Mapped[str] = mapped_column(String(15), default="policy")
    name: Mapped[str] = mapped_column(String(200))
    version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    effective_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)

    # -- Manual addition (backend/db/manual_schema_additions.sql section 5) --
    acknowledgement_pct: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    file_url: Mapped[str | None] = mapped_column(Text, nullable=True)


class PayslipHtmlTemplate(Base):
    """Maps to hcm.payslip_html_templates -- the company's own HTML+CSS
    payslip design (Documents > Templates > Payslip Template), distinct
    from CompanySettings.payslip_template's older JSON "layout toggles"
    blob (which reportlab's render_payslip_pdf still uses as the fallback
    for any company that hasn't authored an HTML template). Several named
    templates may be saved per company (no longer one-row-per-company);
    exactly one may be ACTIVE at a time (see ux_payslip_tpl_active, a
    partial unique index on company_id WHERE is_active).

    is_active toggles whether payslip generation actually uses this
    template without discarding a work-in-progress draft -- when no
    template is active, generation falls back to the legacy reportlab
    renderer exactly as if no HTML template existed."""

    __tablename__ = "hcm_payslip_html_templates"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id")
    )
    name: Mapped[str] = mapped_column(String(120), default="Standard Payslip")
    html_body: Mapped[str] = mapped_column(Text)
    css_styles: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OfferLetterHtmlTemplate(Base):
    """Maps to hcm.offer_letter_html_templates -- the company's own HTML+CSS
    offer letter design (Documents > Templates > Offer Letter Template).
    Identical shape/semantics to PayslipHtmlTemplate above -- several named
    templates may be saved per company, exactly one ACTIVE at a time (see
    ux_offer_tpl_active) -- there is no legacy fallback renderer here since
    no offer-letter generation existed before this feature, so a company
    with no active template simply can't generate one yet, same as a
    brand-new Payslip Template company before its first save."""

    __tablename__ = "hcm_offer_letter_html_templates"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id")
    )
    name: Mapped[str] = mapped_column(String(120), default="Standard Offer Letter")
    html_body: Mapped[str] = mapped_column(Text)
    css_styles: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Project(Base):
    """Maps to acme.pm_projects (actual DB schema)."""

    __tablename__ = "pm_projects"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_companies.id"))
    code: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="planning")
    priority: Mapped[str] = mapped_column(String(15), default="medium")
    is_billable: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    planned_start_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    planned_end_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    actual_start_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    actual_end_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    project_manager_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("core_employees.id"), nullable=True)
    branch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("core_branches.id"), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # created_by/updated_by/updated_at are real table columns too, just
    # never mapped before now -- auto-populated by the session-wide
    # audit-stamp hook (see database.py).
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Real pm_projects columns, previously unmapped -- see crud.
    # get_or_create_customer/get_or_create_project_category. Add/Edit
    # Project's Client (free text) and Type (fixed dropdown) fields resolve
    # to these instead of being silently discarded on save.
    customer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    category_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    # Manually added by the user to pm_projects in _template/acme/Infyq (no
    # DB-level FK, matching branch_id/customer_id/category_id's own
    # app-enforced-only convention on this same table) -- see
    # routers/projects.py's _require_business_unit_in_company/
    # _require_department_in_company.
    business_unit_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    department_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    project_manager: Mapped["Employee | None"] = relationship(foreign_keys=[project_manager_id])
    branch: Mapped["Branch | None"] = relationship()
    customer: Mapped["Customer | None"] = relationship(
        foreign_keys=[customer_id], primaryjoin="Project.customer_id == Customer.id"
    )
    category: Mapped["ProjectCategory | None"] = relationship(
        foreign_keys=[category_id], primaryjoin="Project.category_id == ProjectCategory.id"
    )
    business_unit: Mapped["BusinessUnit | None"] = relationship(
        foreign_keys=[business_unit_id], primaryjoin="Project.business_unit_id == BusinessUnit.id"
    )
    department: Mapped["Department | None"] = relationship(
        foreign_keys=[department_id], primaryjoin="Project.department_id == Department.id"
    )


class Customer(Base):
    """Maps to acme.core_customers -- the real Client entity backing
    Project's Client field, which previously had no backing column at all
    (ProjectOut.client was hardcoded to "—", see routers/projects.py). Only
    the columns Projects needs are mapped; this table also carries GSTIN/
    credit-limit/tax fields used by other, unrelated modules."""

    __tablename__ = "core_customers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_companies.id"))
    code: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class ProjectCategory(Base):
    """Maps to acme.pm_project_categories -- the real Type entity backing
    Project's Type field. Originally just an internal get-or-create target
    for Add/Edit Project's Type dropdown (see
    crud.get_or_create_project_category); now also the tenant-configurable
    master itself -- see crud.list_project_categories/create_project_category/
    update_project_category and the Manage Project Types dialog.
    UNIQUE(company_id, name) on the real table (verified against
    db/full_db.sql) backs both the get-or-create path and the strict-create
    path's duplicate rejection."""

    __tablename__ = "pm_project_categories"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_companies.id"))
    name: Mapped[str] = mapped_column(String(120))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Real table columns, previously unmapped -- auto-populated by the
    # session-wide audit-stamp hook (see database.py), same as every other
    # model's audit columns.
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProjectRole(Base):
    """Maps to acme.pm_project_roles -- the lookup table
    ProjectAllocation.project_role_id was already a column for but nothing
    ever set (Team Members' typed Role was discarded; ProjectAllocationOut.
    role was hardcoded to "—"). UNIQUE(company_id, name) backs the
    get-or-create pattern in crud.get_or_create_project_role."""

    __tablename__ = "pm_project_roles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_companies.id"))
    name: Mapped[str] = mapped_column(String(80))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class ProjectBudget(Base):
    """Maps to acme.pm_project_budgets -- see
    crud.get_project_budget/set_project_budget_amount. The real table
    supports a fuller multi-line ledger (one row per fiscal year/GL
    account/capex-opex split), but Edit Project's Budget field is a single
    plain-amount input, so this app only ever keeps one active row per
    project (current fiscal year, budget_type='opex', no GL account) rather
    than building fiscal-year/account pickers into that field."""

    __tablename__ = "pm_project_budgets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("pm_projects.id"))
    fiscal_year_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    budget_type: Mapped[str] = mapped_column(String(10), default="opex")
    currency_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    planned_amount: Mapped[float] = mapped_column(Numeric(18, 2))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class ProjectStatusHistory(Base):
    """Maps to acme.pm_project_status_history -- a status-change audit
    trail table that already existed but had no model/CRUD wired to it.
    One row is inserted whenever update_project changes Project.status."""

    __tablename__ = "pm_project_status_history"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("pm_projects.id"))
    from_status: Mapped[str | None] = mapped_column(String(15), nullable=True)
    to_status: Mapped[str] = mapped_column(String(15))
    changed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    changed_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)


class WorkEntry(Base):
    """Maps to acme.pm_time_entries (actual DB schema)."""

    __tablename__ = "pm_time_entries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    timesheet_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("pm_timesheets.id"))
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("pm_projects.id"))
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Free-text task title as typed by the user -- distinct from task_id
    # (an optional link to a real pm_tasks board card). Added because
    # WorkEntryCreate.task was previously accepted and validated but had
    # no backing column, so it was silently discarded on every save.
    task_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    entry_date: Mapped[datetime.date] = mapped_column(Date)
    category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    start_time: Mapped[datetime.time | None] = mapped_column(Time, nullable=True)
    end_time: Mapped[datetime.time | None] = mapped_column(Time, nullable=True)
    hours: Mapped[float] = mapped_column(Numeric(4, 2))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_billable: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(12), default="pending")
    approver_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("core_employees.id"), nullable=True)
    decision_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # created_by is a real pm_time_entries column too, just never mapped
    # before now -- auto-populated by the session-wide audit-stamp hook
    # (see database.py), same as every other model's created_by. Unlike
    # most other tables in this app, pm_time_entries genuinely has no
    # updated_by/updated_at columns at all (verified against
    # db/full_db.sql) -- there is nothing to map for those two.
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    project: Mapped["Project | None"] = relationship()
    timesheet: Mapped["Timesheet"] = relationship()
    task: Mapped["TaskBoardCard | None"] = relationship(
        foreign_keys=[task_id], primaryjoin="WorkEntry.task_id == TaskBoardCard.id"
    )


class OvertimeRequest(Base):
    """Maps to hcm.overtime_requests -- a brand-new table (see
    backend/db/add_work_entry_and_overtime_approvals.sql), built from
    scratch for the Reporting Manager Approval Workflow. No prior
    overtime-request concept existed anywhere in this app."""

    __tablename__ = "hcm_overtime_requests"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id")
    )
    work_date: Mapped[datetime.date] = mapped_column(Date)
    hours: Mapped[float] = mapped_column(Numeric(4, 2))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="pending")
    approver_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id"), nullable=True
    )
    decision_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))

    # -- Manual additions, see backend/db/add_overtime_compensation.sql --
    # Requested start (company local time on work_date); planned_start /
    # planned_end are the approved session window (start + approved hours).
    start_time: Mapped[datetime.time | None] = mapped_column(Time, nullable=True)
    planned_start: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    planned_end: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Manual punches (app/overtime.py), never automatic:
    #   scheduled -> clocked_in -> completed (Overtime Clock In / Clock Out)
    #   scheduled -> missed_clock_in   (no clock-in by start + grace; reminder)
    #   clocked_in -> missed_clock_out (no clock-out by end + grace; reminder)
    # A missed punch can still be made late. None = a request approved
    # before sessions existed; legacy "in_progress" reads as clocked_in.
    session_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # add_overtime_clock_punches.sql
    end_time: Mapped[datetime.time | None] = mapped_column(Time, nullable=True)
    missed_in_notified_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    missed_out_notified_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    actual_start: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    actual_end: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    actual_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ended_early_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # None until the session completes; then processed | skipped.
    compensation_status: Mapped[str | None] = mapped_column(String(12), nullable=True)


class OvertimeSettings(Base):
    """Administration > Attendance & Payroll > Overtime Settings -- how a
    company compensates approved overtime (add_overtime_compensation.sql)."""

    __tablename__ = "hcm_overtime_settings"

    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id"), primary_key=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    compensation_mode: Mapped[str] = mapped_column(String(10), default="payable")  # payable | comp_off
    rate_basis: Mapped[str] = mapped_column(String(10), default="basic")  # basic | gross | fixed
    rate_multiplier: Mapped[float] = mapped_column(Numeric(5, 2), default=2)
    monthly_days_divisor: Mapped[int] = mapped_column(SmallInteger, default=26)
    hours_per_day: Mapped[float] = mapped_column(Numeric(4, 2), default=8)
    fixed_hourly_rate: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    comp_off_leave_type_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    comp_off_full_day_hours: Mapped[float] = mapped_column(Numeric(4, 2), default=8)
    comp_off_half_day_hours: Mapped[float] = mapped_column(Numeric(4, 2), default=4)
    min_minutes: Mapped[int] = mapped_column(SmallInteger, default=30)
    rounding: Mapped[str] = mapped_column(String(12), default="exact")
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OvertimeCompensation(Base):
    """How one completed overtime session was compensated -- exactly once
    per request (UNIQUE overtime_request_id). status: pending_payroll ->
    in_payroll (Overtime Pay line on a run), credited (comp-off), skipped."""

    __tablename__ = "hcm_overtime_compensations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_companies.id"))
    overtime_request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hcm_overtime_requests.id"), unique=True
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_employees.id"))
    mode: Mapped[str] = mapped_column(String(10))
    worked_minutes: Mapped[int] = mapped_column(Integer)
    counted_minutes: Mapped[int] = mapped_column(Integer)
    hourly_rate: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    rate_multiplier: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    amount: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    leave_type_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    leave_days: Mapped[float | None] = mapped_column(Numeric(4, 1), nullable=True)
    leave_allocation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    target_period_month: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    target_period_year: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    applied_payroll_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    applied_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    calculation: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))


class Timesheet(Base):
    """Maps to acme.pm_timesheets (actual DB schema)."""

    __tablename__ = "pm_timesheets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_companies.id"))
    employee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_employees.id"))
    week_start: Mapped[datetime.date] = mapped_column("week_start_date", Date)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    total_hours: Mapped[float] = mapped_column(Numeric(5, 2), default=0)
    billable_hours: Mapped[float] = mapped_column(Numeric(5, 2), default=0)
    # Real pm_timesheets column, previously unmapped -- set when the
    # employee actually submits (crud.submit_timesheet), distinct from
    # decided_at below (which is when the Reporting Manager acts).
    submitted_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approver_id: Mapped[uuid.UUID | None] = mapped_column("approved_by", UUID(as_uuid=True), ForeignKey("core_employees.id"), nullable=True)
    decision_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class TaskBoardCard(Base):
    """Maps to acme.pm_tasks (actual DB schema)."""

    __tablename__ = "pm_tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_companies.id"))
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("pm_projects.id"))
    code: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(200))
    task_type: Mapped[str] = mapped_column(String(30), default="task")
    priority: Mapped[str] = mapped_column(String(15), default="medium")
    status: Mapped[str] = mapped_column(String(20), default="todo")
    actual_hours: Mapped[float] = mapped_column(Numeric(6, 2), default=0)
    progress_pct: Mapped[int] = mapped_column(SmallInteger, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # -- Real pm_tasks columns, previously unmapped (see Task Details screen,
    # crud.get_task_board_card_detail) -- confirmed live via information_schema
    # that the table already has these; only the ORM mapping was missing.
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    due_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    planned_start_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    estimated_hours: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    # created_by/updated_by are real table columns too, just never mapped
    # before now -- auto-populated by the session-wide audit-stamp hook
    # (see database.py), same as created_at/updated_at above.
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    actual_start_date: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    actual_end_date: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # -- Review workflow (db/add_task_review_workflow.sql, see app/tasks.py).
    # workflow_status NULL = an unassigned task (free "Move to", as before).
    assignee_employee_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    assigned_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    deadline_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    workflow_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    reviewer_employee_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    submitted_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    project: Mapped["Project | None"] = relationship()


class TaskSubmission(Base):
    """One submission round of an assigned task (db/add_task_review_workflow.sql):
    the assignee's completion description, the reviewer they chose, and the
    review outcome. Files are core_attachments (entity_type 'task_submission')."""

    __tablename__ = "pm_task_submissions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    round: Mapped[int] = mapped_column(SmallInteger)
    description: Mapped[str] = mapped_column(Text)
    submitted_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    submitted_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    reviewer_employee_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    decision: Mapped[str | None] = mapped_column(String(20), nullable=True)
    review_comments: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    reviewed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class TaskHistory(Base):
    """Every review-workflow transition of a task -- the audit trail shown on
    Task Details (db/add_task_review_workflow.sql)."""

    __tablename__ = "pm_task_history"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String(40))
    from_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    actor_employee_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    comments: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))


class TravelRequestModel(Base):
    """Maps to hcm.travel_requests — a manual addition, see
    backend/db/manual_schema_additions.sql section 9. Same self-service
    create-only wiring as WorkEntry: New Travel Request always targets the
    logged-in user."""

    __tablename__ = "hcm_travel_requests"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id")
    )
    purpose: Mapped[str | None] = mapped_column(String(200), nullable=True)
    destination: Mapped[str | None] = mapped_column(String(150), nullable=True)
    from_date: Mapped[datetime.date] = mapped_column(Date)
    to_date: Mapped[datetime.date] = mapped_column(Date)
    travel_mode: Mapped[str | None] = mapped_column(String(40), nullable=True)
    estimated_cost: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="pending")
    current_step: Mapped[int] = mapped_column(SmallInteger, default=1)
    # -- Manual addition, see backend/db/add_approval_decision_fields.sql --
    approver_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id"), nullable=True
    )
    decision_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class SalaryRevisionRequest(Base):
    __tablename__ = "hcm_salary_revision_requests"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id")
    )
    current_ctc: Mapped[int | None] = mapped_column(nullable=True)
    proposed_ctc: Mapped[int | None] = mapped_column(nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="pending")
    # -- Manual addition, see backend/db/add_approval_decision_fields.sql --
    approver_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id"), nullable=True
    )
    decision_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AssetRequestModel(Base):
    __tablename__ = "hcm_asset_requests"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id")
    )
    asset_type: Mapped[str] = mapped_column(String(60))
    justification: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="pending")
    # -- Manual addition, see backend/db/add_approval_decision_fields.sql --
    approver_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id"), nullable=True
    )
    decision_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class HiringRequisition(Base):
    """Maps to hcm.hiring_requisitions — a manual addition, see
    backend/db/add_hiring_requisitions_table.sql. The one Standalone Approval
    type with no pre-existing backend table (Asset Request/Salary Revision
    both already had one)."""

    __tablename__ = "hcm_hiring_requisitions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    requested_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id")
    )
    designation_title: Mapped[str] = mapped_column(String(120))
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_departments.id"), nullable=True
    )
    positions_count: Mapped[int] = mapped_column(Integer, default=1)
    justification: Mapped[str | None] = mapped_column(Text, nullable=True)
    # draft | pending (= pending approval) | changes_requested | approved |
    # rejected | cancelled | closed -- "pending" stays the stored value for
    # pending approval so the Approvals inbox keeps working unchanged.
    status: Mapped[str] = mapped_column(String(20), default="pending")
    # Reporting Manager Approval Workflow / Two Reporting Managers
    # cross-visibility -- previously had no decision-tracking columns at all
    # (see crud.decided_by_manager_type).
    approver_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id"), nullable=True
    )
    decision_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # -- Recruitment workflow (backend/db/add_recruitment_workflow.sql) --
    # status: draft | pending | sent_back | approved | rejected | cancelled | closed
    company_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    employment_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    work_mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    branch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    sub_department_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    reporting_manager_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    target_joining_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    experience_min: Mapped[float | None] = mapped_column(Numeric(4, 1), nullable=True)
    experience_max: Mapped[float | None] = mapped_column(Numeric(4, 1), nullable=True)
    salary_min: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    salary_max: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    required_skills: Mapped[str | None] = mapped_column(Text, nullable=True)
    qualifications: Mapped[str | None] = mapped_column(Text, nullable=True)
    job_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    budget_cost_center: Mapped[str | None] = mapped_column(String(120), nullable=True)
    priority: Mapped[str | None] = mapped_column(String(10), nullable=True)
    hiring_team: Mapped[list] = mapped_column(JSONB, default=list)
    interview_stages: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    submitted_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    opening_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    requested_by_employee: Mapped["Employee"] = relationship(foreign_keys=[requested_by])
    department: Mapped["Department | None"] = relationship()


class EmployeeEducation(Base):
    """Backs Add Employee's Education & Experience tab (qualification/
    institute/specialization/year of passing)."""

    __tablename__ = "hcm_employee_education"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id")
    )
    qualification: Mapped[str | None] = mapped_column(String(150), nullable=True)
    institute: Mapped[str | None] = mapped_column(String(200), nullable=True)
    specialization: Mapped[str | None] = mapped_column(String(150), nullable=True)
    year_of_passing: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)


class EmployeePriorExperience(Base):
    """Backs Add Employee's Previous Employer / Total Experience / Domain
    Expertise fields."""

    __tablename__ = "hcm_employee_prior_experience"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id")
    )
    employer_name: Mapped[str] = mapped_column(String(200))
    years_experience: Mapped[float | None] = mapped_column(Numeric(4, 1), nullable=True)
    domain: Mapped[str | None] = mapped_column(String(150), nullable=True)


class EmployeeSkill(Base):
    """Backs Add Employee's Relevant Skills field -- one row per skill,
    split from that field's comma-separated free text."""

    __tablename__ = "hcm_employee_skills"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id")
    )
    skill: Mapped[str] = mapped_column(String(100))


class Contact(Base):
    """Maps to core.addresses/core.contacts' sibling table core.contacts --
    used here only for Add Employee's Emergency Contact fields
    (entity_type='employee', is_emergency=true). Polymorphic like
    core.addresses/attachments/comments; only the columns this backend
    needs are mapped."""

    __tablename__ = "core_contacts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id")
    )
    entity_type: Mapped[str] = mapped_column(String(30))
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    name: Mapped[str] = mapped_column(String(120))
    designation: Mapped[str | None] = mapped_column(String(80), nullable=True)
    email: Mapped[str | None] = mapped_column(String(150), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    is_emergency: Mapped[bool] = mapped_column(Boolean, default=False)


class AuditLog(Base):
    """Maps to core.audit_logs — one row per admin-level action this backend
    explicitly logs (see crud.create_audit_log and its call sites). Not a
    full request-audit trail across every endpoint; scoped to the
    Administration > Audit Logs screen's most meaningful actions."""

    __tablename__ = "core_audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id")
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_users.id"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(20))
    doctype: Mapped[str] = mapped_column(String(60))
    document_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    changes: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))

    user: Mapped["User | None"] = relationship()


class Attachment(Base):
    """Maps to core.attachments -- generic polymorphic file attachment keyed
    by (entity_type, entity_id). First (and so far only) user: supporting
    documents on a leave request (e.g. a medical certificate) uploaded via
    the Apply Leave form."""

    __tablename__ = "core_attachments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id")
    )
    entity_type: Mapped[str] = mapped_column(String(60))
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    file_name: Mapped[str] = mapped_column(String(255))
    file_url: Mapped[str] = mapped_column(Text)
    mime_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_users.id"), nullable=True
    )


class SalarySlipLine(Base):
    """Backs the Employee Profile > Payroll tab's component breakdown
    (Basic/HRA/Conveyance/etc.) for a given salary slip."""

    __tablename__ = "hcm_salary_slip_lines"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    slip_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("hcm_salary_slips.id"))
    component_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("hcm_salary_components.id"))
    amount: Mapped[float] = mapped_column(Numeric(14, 2))

    component: Mapped["SalaryComponent"] = relationship()


class PayrollReimbursementInclusion(Base):
    """Payroll > Travel & Expense Reimbursements -- once a Travel
    Requisition (TravelRequestModel) or Expense Report/Reimbursement
    (ExpenseClaim -- both flows share hcm.expense_claims, see
    crud.list_reimbursements' docstring) is fully approved
    (status == 'approved'), HR schedules its real approved amount into a
    specific payroll period here. Picked up automatically by
    crud._compute_and_write_slip as a real SalarySlipReimbursementLine --
    never folded into gross_pay/total_deductions, so it's never mistaken
    for salary or a deduction. Maps to
    hcm.payroll_reimbursement_inclusions, added via
    backend/db/add_payroll_reimbursement_inclusions.sql.

    source_id is polymorphic (points at either hcm_travel_requests.id or
    hcm_expense_claims.id depending on source_type) -- no single FK target
    is possible, same convention already used by this app's other
    polymorphic lookups (e.g. comments/attachments keyed by
    entity_type+entity_id).

    The UNIQUE (source_type, source_id) constraint is the duplicate-
    INCLUSION guard: one approved travel/expense record can only ever be
    scheduled into payroll once. applied_payroll_run_id is the separate
    duplicate-PAYOUT guard (same pattern as AssetRecoveryDeduction): once a
    payroll run has consumed this row, it's excluded from every other
    run's eligibility query, but stays eligible for a still-draft run
    being regenerated in place."""

    __tablename__ = "hcm_payroll_reimbursement_inclusions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_companies.id"))
    employee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_employees.id"))
    source_type: Mapped[str] = mapped_column(String(20))
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    amount: Mapped[float] = mapped_column(Numeric(14, 2))
    target_period_month: Mapped[int] = mapped_column(SmallInteger)
    target_period_year: Mapped[int] = mapped_column(SmallInteger)
    applied_payroll_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hcm_payroll_runs.id"), nullable=True
    )
    applied_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Review state (backend/db/add_payroll_reimbursement_components.sql):
    # 'included' | 'excluded' -- an excluded row is never paid but keeps
    # the UNIQUE (source_type, source_id) guard in force. amount is what
    # payroll pays; approved_amount the source's approved figure at
    # scheduling time (amount may be lowered, never raised above it).
    status: Mapped[str] = mapped_column(String(12), default="included")
    approved_amount: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    auto_included: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    employee: Mapped["Employee"] = relationship(foreign_keys=[employee_id])


class PayrollReimbursementComponent(Base):
    """Payroll > Salary Structure > Reimbursement Components -- how one
    company handles Travel Reimbursement ('travel_request') or Expense
    Reimbursement ('expense_claim') in payroll. Optional: with no row the
    built-in default applies (enabled, not auto-included -- see
    crud.get_reimbursement_components). Maps to
    hcm_payroll_reimbursement_components (backend/db/
    add_payroll_reimbursement_components.sql).

    is_enabled=False -> that type is never paid through payroll (scheduled
    rows stay, unpaid, until it is re-enabled). auto_include=True -> every
    approved record of that type not yet scheduled is identified
    automatically for the payroll run of its approval period.
    max_amount_per_request caps what auto-identification schedules per
    record (HR can still review the amount)."""

    __tablename__ = "hcm_payroll_reimbursement_components"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_companies.id"))
    source_type: Mapped[str] = mapped_column(String(20))
    display_name: Mapped[str] = mapped_column(String(40))
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_include: Mapped[bool] = mapped_column(Boolean, default=False)
    max_amount_per_request: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SalarySlipReimbursementLine(Base):
    """One Travel/Expense reimbursement line on a payslip -- the audit link
    back to the PayrollReimbursementInclusion (and, through it, the real
    Travel Requisition/Expense Report record) that produced it. Kept
    entirely separate from SalarySlipLine/SalaryComponent (which only
    model earning/deduction/tax/benefit) so a reimbursement is never
    miscategorized as any of those."""

    __tablename__ = "hcm_salary_slip_reimbursement_lines"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    slip_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("hcm_salary_slips.id"))
    inclusion_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hcm_payroll_reimbursement_inclusions.id")
    )
    label: Mapped[str] = mapped_column(String(40))
    amount: Mapped[float] = mapped_column(Numeric(14, 2))


class ShiftAssignment(Base):
    """Backs the Employee Profile > Professional tab's assigned Shift."""

    __tablename__ = "hcm_shift_assignments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_employees.id"))
    shift_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("hcm_shifts.id"))
    # from_date is the Effective Date; to_date the last day it applies
    # (NULL = open-ended). to_date < from_date = superseded / cancelled
    # before it ever took effect (kept as history).
    from_date: Mapped[datetime.date] = mapped_column(Date)
    to_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    # History (add_shift_assignment_history.sql; NULL on rows made before it).
    previous_shift_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    shift: Mapped["Shift"] = relationship()


class FiscalYear(Base):
    """Maps to the pre-existing (previously unmapped) fin.fiscal_years
    table -- LeaveAllocation.fiscal_year_id references a row here. See
    crud.get_or_create_fiscal_year, which looks up/creates the row for a
    company's current fiscal year rather than assuming one already exists."""

    __tablename__ = "fin_fiscal_years"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id")
    )
    name: Mapped[str] = mapped_column(String(20))
    start_date: Mapped[datetime.date] = mapped_column(Date)
    end_date: Mapped[datetime.date] = mapped_column(Date)
    is_closed: Mapped[bool] = mapped_column(Boolean, default=False)


class LeaveAllocation(Base):
    """Backs the Employee Profile > Leave tab's CL/SL/EL/Comp-Off/LOP
    balances (allocated vs used days per leave type per fiscal year)."""

    __tablename__ = "hcm_leave_allocations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_employees.id"))
    leave_type_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("hcm_leave_types.id"))
    fiscal_year_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("fin_fiscal_years.id"))
    allocated_days: Mapped[float] = mapped_column(Numeric(5, 1))
    carried_forward_days: Mapped[float] = mapped_column(Numeric(5, 1), default=0)
    used_days: Mapped[float] = mapped_column(Numeric(5, 1), default=0)

    leave_type: Mapped["LeaveType"] = relationship()


class ProjectAllocation(Base):
    """Maps to acme.pm_resource_allocations (actual DB schema)."""

    __tablename__ = "pm_resource_allocations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("pm_projects.id"))
    employee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_employees.id"))
    project_role_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    allocation_pct: Mapped[float] = mapped_column(Numeric(5, 2), default=100)
    start_date: Mapped[datetime.date] = mapped_column(Date)
    end_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    project: Mapped["Project"] = relationship()
    employee: Mapped["Employee"] = relationship(foreign_keys=[employee_id])
    project_role: Mapped["ProjectRole | None"] = relationship(
        foreign_keys=[project_role_id], primaryjoin="ProjectAllocation.project_role_id == ProjectRole.id"
    )


class Goal(Base):
    """Backs the Performance screen's "Overall Goal Completion" KPI --
    Employee.performanceInfo.goalsCompleted is the average of an
    employee's own goals' progress_pct (see crud.list_employees_full)."""

    __tablename__ = "hcm_goals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    cycle_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("hcm_appraisal_cycles.id"))
    employee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_employees.id"))
    title: Mapped[str] = mapped_column(String(200))
    weight_pct: Mapped[float] = mapped_column(Numeric(5, 2), default=100)
    progress_pct: Mapped[float] = mapped_column(Numeric(5, 2), default=0)
    status: Mapped[str] = mapped_column(String(12), default="active")


class Appraisal(Base):
    """Backs the Employee Profile > Performance tab's rating/last review."""

    __tablename__ = "hcm_appraisals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    cycle_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("hcm_appraisal_cycles.id"))
    employee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_employees.id"))
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id"), nullable=True
    )
    self_score: Mapped[float | None] = mapped_column(Numeric(4, 2), nullable=True)
    manager_score: Mapped[float | None] = mapped_column(Numeric(4, 2), nullable=True)
    final_rating: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(String(15), default="pending")

    cycle: Mapped["AppraisalCycle"] = relationship()


class ExitRequestModel(Base):
    """Backs People > Offboarding -- maps to hcm.exit_requests."""

    __tablename__ = "hcm_exit_requests"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_employees.id"))
    resignation_date: Mapped[datetime.date] = mapped_column(Date)
    last_working_day: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(15), default="submitted")
    exit_interview_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Reporting Manager Approval Workflow / Two Reporting Managers
    # cross-visibility -- previously had no decision-tracking columns at all
    # (see crud.decided_by_manager_type).
    approver_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id"), nullable=True
    )
    decision_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Audit columns -- real table columns, never mapped before; auto-
    # populated by the session-wide audit-stamp hook (see database.py).
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CelebrationLog(Base):
    """Backs the Birthday / Work Anniversary celebration reminders (see
    crud.check_daily_celebrations) -- one row per (employee,
    celebration_type, year) already broadcast. The UNIQUE constraint is the
    actual duplicate-prevention mechanism: `INSERT ... ON CONFLICT DO
    NOTHING RETURNING id` is atomic, so repeated/concurrent triggers the
    same day never re-fire the notification/email. See
    backend/db/add_celebration_log.sql."""

    __tablename__ = "hcm_celebration_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_employees.id"))
    celebration_type: Mapped[str] = mapped_column(String(20))
    year: Mapped[int] = mapped_column()
    notified_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )


class ExitChecklistItem(Base):
    __tablename__ = "hcm_exit_checklist_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    exit_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("hcm_exit_requests.id"))
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id"), nullable=True
    )
    task: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(12), default="pending")


class FinalSettlement(Base):
    __tablename__ = "hcm_final_settlements"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    exit_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("hcm_exit_requests.id"))
    payable_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    recovery_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    net_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    # draft -> approved -> paid ('posted', from older data, reads as approved).
    status: Mapped[str] = mapped_column(String(12), default="draft")

    # -- Manual additions, see backend/db/add_fnf_settlements.sql --
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    prepared_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    prepared_by_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    prepared_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    approved_by_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    approved_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approval_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    payment_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    payment_mode: Mapped[str | None] = mapped_column(String(30), nullable=True)
    payment_reference: Mapped[str | None] = mapped_column(String(80), nullable=True)
    paid_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    paid_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    lines: Mapped[list["FinalSettlementLine"]] = relationship(
        order_by="FinalSettlementLine.sort_order", cascade="all, delete-orphan"
    )


class FinalSettlementLine(Base):
    """One HR-entered earning or deduction of a Full & Final Settlement
    (backend/db/add_fnf_settlements.sql)."""

    __tablename__ = "hcm_final_settlement_lines"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    settlement_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hcm_final_settlements.id", ondelete="CASCADE")
    )
    line_type: Mapped[str] = mapped_column(String(10))  # 'earning' | 'deduction'
    component: Mapped[str] = mapped_column(String(60))
    description: Mapped[str | None] = mapped_column(String(200), nullable=True)
    amount: Mapped[float] = mapped_column(Numeric(14, 2))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class EmployeeLifecycleEvent(Base):
    """Backs the Employee Profile > Lifecycle tab and People > Transfers &
    Promotions -- zero rows for any company today, so both stay on their
    empty state until this table is populated (see
    crud.list_lifecycle_events). crud.record_employee_change is the first
    thing that actually writes rows here, hooked into the existing
    hierarchy-update endpoints and the new POST /transfer endpoint (see
    add_employee_transfer_history_fields.sql for the from/to columns that
    change added)."""

    __tablename__ = "hcm_employee_lifecycle_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_employees.id"))
    event_type: Mapped[str] = mapped_column(String(20))
    event_date: Mapped[datetime.date] = mapped_column(Date)
    from_designation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_designations.id"), nullable=True
    )
    to_designation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_designations.id"), nullable=True
    )
    from_department_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_departments.id"), nullable=True
    )
    to_department_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_departments.id"), nullable=True
    )
    from_ctc: Mapped[int | None] = mapped_column(nullable=True)
    to_ctc: Mapped[int | None] = mapped_column(nullable=True)

    # -- Manual additions, see add_employee_transfer_history_fields.sql --
    from_branch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_branches.id"), nullable=True
    )
    to_branch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_branches.id"), nullable=True
    )
    from_business_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_business_units.id"), nullable=True
    )
    to_business_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_business_units.id"), nullable=True
    )
    from_sub_department_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_sub_departments.id"), nullable=True
    )
    to_sub_department_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_sub_departments.id"), nullable=True
    )
    from_reporting_manager_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id"), nullable=True
    )
    to_reporting_manager_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id"), nullable=True
    )
    from_dotted_line_manager_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id"), nullable=True
    )
    to_dotted_line_manager_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id"), nullable=True
    )
    # -- Contract renewals / conversions (add_contract_lifecycle_fields.sql) --
    from_contract_end_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    to_contract_end_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    from_contract_rate: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    to_contract_rate: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    contract_rate_unit: Mapped[str | None] = mapped_column(String(10), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    from_designation: Mapped["Designation | None"] = relationship(foreign_keys=[from_designation_id])
    to_designation: Mapped["Designation | None"] = relationship(foreign_keys=[to_designation_id])
    from_department: Mapped["Department | None"] = relationship(foreign_keys=[from_department_id])
    to_department: Mapped["Department | None"] = relationship(foreign_keys=[to_department_id])
    from_branch: Mapped["Branch | None"] = relationship(foreign_keys=[from_branch_id])
    to_branch: Mapped["Branch | None"] = relationship(foreign_keys=[to_branch_id])
    from_business_unit: Mapped["BusinessUnit | None"] = relationship(foreign_keys=[from_business_unit_id])
    to_business_unit: Mapped["BusinessUnit | None"] = relationship(foreign_keys=[to_business_unit_id])
    from_sub_department: Mapped["SubDepartment | None"] = relationship(foreign_keys=[from_sub_department_id])
    to_sub_department: Mapped["SubDepartment | None"] = relationship(foreign_keys=[to_sub_department_id])


class Sprint(Base):
    """Maps to acme.pm_project_phases (actual DB schema)."""

    __tablename__ = "pm_project_phases"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("pm_projects.id"))
    name: Mapped[str] = mapped_column(String(80))
    sequence: Mapped[int] = mapped_column(SmallInteger, default=1)
    planned_start_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    planned_end_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="not_started")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    project: Mapped["Project"] = relationship()


class Release(Base):
    """Maps to acme.pm_milestones (actual DB schema)."""

    __tablename__ = "pm_milestones"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("pm_projects.id"))
    phase_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    name: Mapped[str] = mapped_column(String(120))
    due_date: Mapped[datetime.date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    project: Mapped["Project"] = relationship()


class TrainingSession(Base):
    """Backs Learning > Training Calendar. Maps to hcm.training_sessions,
    added via backend/db/add_kanban_training_skills_integrations_settings.sql."""

    __tablename__ = "hcm_training_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id")
    )
    title: Mapped[str] = mapped_column(String(200))
    session_date: Mapped[datetime.date] = mapped_column(Date)
    trainer_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    is_mandatory: Mapped[bool] = mapped_column(Boolean, default=False)
    attendee_count: Mapped[int] = mapped_column(Integer, default=0)


class EmployeeSkillRating(Base):
    """Backs Performance > KPIs & Skill Matrix's "Level" column. Maps to
    hcm.employee_skill_ratings, added via
    backend/db/add_kanban_training_skills_integrations_settings.sql --
    distinct from hcm.employee_skills, which is a flat skill-name list with
    no level/rating concept."""

    __tablename__ = "hcm_employee_skill_ratings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_employees.id")
    )
    skill: Mapped[str] = mapped_column(String(100))
    level: Mapped[str] = mapped_column(String(20))


class Integration(Base):
    """Backs Administration > Integrations' Connect/Disconnect toggle. Maps
    to core.integrations, added via
    backend/db/add_kanban_training_skills_integrations_settings.sql."""

    __tablename__ = "core_integrations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id")
    )
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str | None] = mapped_column(String(200), nullable=True)
    is_connected: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))


class CompanySettings(Base):
    """Backs Administration > General Settings' Organization Defaults card
    (Default Currency / Default Time Zone / Working Days per Week). Maps to
    core.company_settings, added via
    backend/db/add_kanban_training_skills_integrations_settings.sql.
    Fiscal Year Start is NOT here -- it already lives on
    Company.fiscal_year_start_month, editable via PATCH /company-profile
    (see CompanyProfileUpdate)."""

    __tablename__ = "core_company_settings"

    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id"), primary_key=True
    )
    default_currency: Mapped[str] = mapped_column(String(10), default="INR")
    default_timezone: Mapped[str] = mapped_column(String(60), default="IST (UTC+5:30)")
    working_days_per_week: Mapped[int] = mapped_column(SmallInteger, default=5)

    # Organization-hierarchy level toggles -- added via
    # add_org_hierarchy_toggles.sql. Default true so an unconfigured company
    # behaves exactly as before (every level enabled). Consulted by
    # crud.get_field_rules to decide whether branch_id/business_unit_id/
    # sub_department_id are mandatory on an employee record.
    enable_branch_level: Mapped[bool] = mapped_column(Boolean, default=True)
    enable_business_unit_level: Mapped[bool] = mapped_column(Boolean, default=True)
    enable_department_level: Mapped[bool] = mapped_column(Boolean, default=True)
    enable_sub_department_level: Mapped[bool] = mapped_column(Boolean, default=True)

    # Company policy defaults -- added via add_org_hierarchy_toggles.sql.
    working_hours_start: Mapped[datetime.time | None] = mapped_column(Time, nullable=True)
    working_hours_end: Mapped[datetime.time | None] = mapped_column(Time, nullable=True)
    probation_period_days: Mapped[int] = mapped_column(SmallInteger, default=90)
    notice_period_days: Mapped[int] = mapped_column(SmallInteger, default=30)

    # One common company-wide payslip layout/format, JSON-encoded -- added
    # this session (nullable TEXT column, already applied live). None means
    # "not configured yet"; crud.get_payslip_template falls back to a
    # sensible default shape in that case, same hydrate-with-fallback
    # convention as get_employee_salary_structure.
    payslip_template: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Gates crud._compute_and_write_slip's auto-TDS-estimate fallback
    # (which otherwise silently injects a guessed tax deduction whenever a
    # salary structure has no explicit tax-type component at all). Default
    # False for every company: no automatic tax guess unless the
    # Organization Owner/CEO explicitly turns this on in Administration >
    # General Settings -- a structure with its own real tax component
    # (e.g. a flat "Income Tax" line) is unaffected either way, since that
    # was never this fallback in the first place.
    auto_tds_estimate_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    # Work Entry / Timesheet approval-removal feature: how many days back an
    # employee may submit/edit a missed Work Entry or Timesheet. None means
    # "no limit configured" -- crud.get_work_entry_backdate_days's caller
    # treats that as unrestricted, matching every other tenant/company that
    # never sets this until an Organization Owner explicitly configures it.
    work_entry_backdate_days: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    # M-12 (2026_10_06_attendance_work_rules.sql): how many days back an
    # attendance regularization may be raised (default 30).
    regularization_backdate_days: Mapped[int | None] = mapped_column(SmallInteger, nullable=True, default=30)

    # Phase 2 of the salary structure redesign: the statutory EPF wage
    # ceiling (₹15,000 is India's current statutory default) -- a
    # 'stat_pf'-calc_type SalaryComponent resolves to
    # MIN(Basic, pf_wage_ceiling) * 12%, computed by the engine rather than
    # an admin typing a rupee figure that silently goes stale for anyone
    # whose Basic sits below (or the ceiling itself changes).
    pf_wage_ceiling: Mapped[float] = mapped_column(Numeric(10, 2), default=15000)

    # Payroll Settings > Deductions Configuration -- gates
    # crud._compute_and_write_slip's LOP / Asset-recovery deduction lines.
    # Both default False so no existing tenant's payslip figures change
    # until an HR/Organization Owner explicitly turns either on. See
    # backend/db/add_payroll_deduction_settings.sql.
    lop_deduction_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    asset_deduction_enabled: Mapped[bool] = mapped_column(Boolean, default=False)


class FieldRule(Base):
    """Per-company, per-entity, optionally per-role field state config
    (mandatory/optional/hidden/readonly). Maps to core.field_rules, added
    via add_field_rules.sql. A missing row for a given (company, entity,
    field) is NOT an error -- crud.get_field_rules falls back to
    crud.DEFAULT_FIELD_RULES, so an empty table changes no existing
    company's behavior. role_id NULL = company-wide default; a non-null
    role_id overrides the company-wide default for that role only."""

    __tablename__ = "core_field_rules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id")
    )
    entity_key: Mapped[str] = mapped_column(String(40))
    field_key: Mapped[str] = mapped_column(String(60))
    state: Mapped[str] = mapped_column(String(10))
    role_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_roles.id"), nullable=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_users.id"), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.datetime.utcnow
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_users.id"), nullable=True
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))


class Module(Base):
    """Global catalog of toggleable app areas -- keys match the Flutter
    sidebar's existing kNav ids 1:1 (lib/data/seed/nav_seed.dart). Maps to
    core.modules, added via add_modules_and_menus.sql. Not company-scoped --
    this only changes when a new module is added to the product; per-company
    enable/disable lives on CompanyModule."""

    __tablename__ = "modules"
    __table_args__ = {"schema": "public"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    key: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    icon: Mapped[str | None] = mapped_column(String(40), nullable=True)
    sort_order: Mapped[int] = mapped_column(SmallInteger, default=0)
    is_core: Mapped[bool] = mapped_column(Boolean, default=False)

    # -- Manual additions, see backend/db/add_module_nav_metadata.sql --
    # nav_group: which sidebar section this renders under (NULL = ungrouped,
    # e.g. Dashboard). permission_columns: which RBAC column(s) gate this
    # module's nav visibility (NULL = not yet migrated on this row, treated
    # by the frontend as "use the legacy hardcoded gate"; empty array =
    # migrated and genuinely always-visible).
    nav_group: Mapped[str | None] = mapped_column(String(40), nullable=True)
    permission_columns: Mapped[list[str] | None] = mapped_column(
        ARRAY(String), nullable=True
    )


class CompanyModule(Base):
    """Per-company module enable/disable. Maps to core.company_modules,
    added via add_modules_and_menus.sql. A missing row for a given
    (company, module) means "enabled" -- see crud.get_enabled_module_keys --
    so an unconfigured company sees every module exactly as before this
    table existed."""

    __tablename__ = "core_company_modules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id")
    )
    module_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("public.modules.id")
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_users.id"), nullable=True
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))

    module: Mapped["Module"] = relationship()


class MenuItem(Base):
    """Finer-than-module nav entries -- maps to core.menu_items, added via
    add_modules_and_menus.sql. Not required for a company that only ever
    toggles at the module level; exists so a future finer-grained toggle
    doesn't need another migration."""

    __tablename__ = "menu_items"
    __table_args__ = {"schema": "public"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    module_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("public.modules.id")
    )
    key: Mapped[str] = mapped_column(String(40), unique=True)
    label: Mapped[str] = mapped_column(String(80))
    sort_order: Mapped[int] = mapped_column(SmallInteger, default=0)


class CompanyMenuItem(Base):
    """Per-company menu-item enable/disable. Maps to core.company_menu_items,
    added via add_modules_and_menus.sql. Same "missing row = enabled" rule
    as CompanyModule."""

    __tablename__ = "core_company_menu_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id")
    )
    menu_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("public.menu_items.id")
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class PermissionAction(Base):
    """Metadata-driven catalog of granular (resource, action) permissions,
    grouped by real module -- maps to public.permission_actions, added via
    add_permission_actions_catalog.sql. Global, not per-tenant (same
    placement as Module/MenuItem): every tenant shares one catalog of
    *available* actions; which of a tenant's own roles are actually
    GRANTED which action is still stored per-tenant in that tenant
    schema's own core.role_permissions (see crud.apply_actions_update).

    This is the single source of truth crud.list_permission_actions reads
    -- adding a row here (a plain INSERT, no code change) makes a new
    module's actions available in GET /api/permission-actions and
    Administration > Add Custom Role immediately."""

    __tablename__ = "permission_actions"
    __table_args__ = (UniqueConstraint("resource", "action"), {"schema": "public"})

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    module_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("public.modules.id")
    )
    resource: Mapped[str] = mapped_column(String(60))
    action: Mapped[str] = mapped_column(String(30))
    label: Mapped[str] = mapped_column(String(120))
    sort_order: Mapped[int] = mapped_column(SmallInteger, default=0)

    module: Mapped["Module"] = relationship()


class HierarchyRoleBinding(Base):
    """Per-company binding of a reporting-hierarchy rung (Team Lead/Project
    Manager/Senior Manager/Branch Manager/Branch Head) to one of that
    company's own roles. Maps to core.hierarchy_role_bindings, added via
    add_hierarchy_role_bindings.sql. A missing row for a given (company,
    rung_key) means org_hierarchy.py falls back to its hardcoded built-in
    role name for that rung -- see org_hierarchy.resolve_rung_role_name."""

    __tablename__ = "core_hierarchy_role_bindings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id")
    )
    rung_key: Mapped[str] = mapped_column(String(30))
    role_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_roles.id"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_users.id"), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.datetime.utcnow
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_users.id"), nullable=True
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.datetime.utcnow
    )

    role: Mapped["Role"] = relationship()


class ApprovalWorkflow(Base):
    """One named, ordered approval chain for one document type, per
    company. Maps to core.approval_workflows -- exists in the reference
    schema but was never ORM-mapped/used until approval_engine.py. See
    wire_approval_workflow_engine.sql."""

    __tablename__ = "core_approval_workflows"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id")
    )
    doctype: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(120))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_users.id"), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.datetime.utcnow
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_users.id"), nullable=True
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.datetime.utcnow
    )

    steps: Mapped[list["ApprovalWorkflowStep"]] = relationship(
        back_populates="workflow", cascade="all, delete-orphan",
        order_by="ApprovalWorkflowStep.step_order",
    )


class ApprovalWorkflowStep(Base):
    """One ordered step of an ApprovalWorkflow. approver_type is one of
    'role' | 'user' | 'reporting_manager' | 'dotted_line_manager' |
    'department_head' | 'business_unit_head' | 'branch_manager' --
    dynamic types resolve through org_hierarchy.py at decision time; 'role'
    and 'user' point at a specific role_id/user_id. min_amount/max_amount
    optionally band a step to only apply within an amount range (e.g. an
    expense claim step that only kicks in above a threshold)."""

    __tablename__ = "core_approval_workflow_steps"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_approval_workflows.id")
    )
    step_order: Mapped[int] = mapped_column(SmallInteger)
    approver_type: Mapped[str] = mapped_column(String(20))
    role_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_roles.id"), nullable=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_users.id"), nullable=True
    )
    min_amount: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    max_amount: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_users.id"), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.datetime.utcnow
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_users.id"), nullable=True
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.datetime.utcnow
    )

    workflow: Mapped["ApprovalWorkflow"] = relationship(back_populates="steps")


class ApprovalRequest(Base):
    """One in-flight or decided approval instance for one document (e.g.
    one leave request). document_id is a generic polymorphic reference (no
    DB-level FK, since it can point at any of the ~8 request tables this
    replaces per-table single-approver logic for) -- same pattern
    core.attachments/core.comments already use for polymorphic references."""

    __tablename__ = "core_approval_requests"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id")
    )
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_approval_workflows.id")
    )
    doctype: Mapped[str] = mapped_column(String(60))
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String(15), default="pending")
    current_step: Mapped[int] = mapped_column(SmallInteger, default=1)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_users.id"), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))

    workflow: Mapped["ApprovalWorkflow"] = relationship()


class ApprovalAction(Base):
    """One decision (approve/reject) recorded against one step of one
    ApprovalRequest -- the audit trail of who acted on which step, when."""

    __tablename__ = "core_approval_actions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_approval_requests.id")
    )
    step_order: Mapped[int] = mapped_column(SmallInteger)
    actor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_users.id"))
    action: Mapped[str] = mapped_column(String(15))
    comments: Mapped[str | None] = mapped_column(Text, nullable=True)
    acted_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))


class NotificationPreference(Base):
    """Backs Administration > Notifications -- a per-user, per-event-type,
    per-channel toggle matrix ("Leave request submitted" x Email/Push/SMS,
    etc). Maps to core.notification_preferences, added via
    backend/db/add_kanban_training_skills_integrations_settings.sql (not the
    same table as core.notifications, which stores delivered notification
    instances, not preferences)."""

    __tablename__ = "core_notification_preferences"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_users.id"))
    event_key: Mapped[str] = mapped_column(String(80))
    channel: Mapped[str] = mapped_column(String(10))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Notification(Base):
    """Delivered in-app notification instances -- the table
    NotificationPreference's own docstring already anticipated but that had
    no model/CRUD/router wired to it until the Reporting Manager Approval
    Workflow needed somewhere to record "your request was approved" /
    "you have a new request to review". Maps to core.notifications, which
    already exists live (it is part of the base tenant DDL, see
    backend/db/full_db.sql) -- no migration needed for this table itself."""

    __tablename__ = "core_notifications"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_companies.id")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_users.id"))
    title: Mapped[str] = mapped_column(String(150))
    body: Mapped[str] = mapped_column(Text)
    entity_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    read_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))


# ── Custom modules (Administration > Roles & Permissions > Custom Modules) ──
# backend/db/add_custom_modules.sql -- tenant tables. A custom module is an
# admin-defined records workspace; access is granted per EMPLOYEE (not per
# role) through CustomModuleAssignment.actions.

class CustomModule(Base):
    __tablename__ = "core_custom_modules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_companies.id"))
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    icon: Mapped[str | None] = mapped_column(String(40), nullable=True)
    actions: Mapped[list[str]] = mapped_column(ARRAY(Text), default=lambda: ["view"])
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CustomModuleAssignment(Base):
    __tablename__ = "core_custom_module_assignments"
    __table_args__ = (UniqueConstraint("module_id", "employee_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    module_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_custom_modules.id", ondelete="CASCADE")
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_employees.id"))
    actions: Mapped[list[str]] = mapped_column(ARRAY(Text))
    granted_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    granted_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
    updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CustomModuleRecord(Base):
    __tablename__ = "core_custom_module_records"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    module_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("core_custom_modules.id", ondelete="CASCADE")
    )
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_companies.id"))
    title: Mapped[str] = mapped_column(String(200))
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_by_employee: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_by_employee: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    decided_by_employee: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    decided_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
    updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EmployeePermission(Base):
    """core_employee_permissions -- an admin-set access level for one
    employee on one module that replaces the role's (see
    backend/db/add_employee_permissions.sql and app/employee_permissions.py)."""
    __tablename__ = "core_employee_permissions"
    __table_args__ = (UniqueConstraint("employee_id", "module"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    employee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_employees.id"))
    module: Mapped[str] = mapped_column(String(40))
    level: Mapped[str] = mapped_column(String(10))
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EmployeeAccessGrant(Base):
    """core_employee_access_grants -- extra access to a built-in module
    granted to one employee on top of their role (additive). See
    backend/db/add_employee_access_grants.sql and crud.employee_access_grants."""
    __tablename__ = "core_employee_access_grants"
    __table_args__ = (UniqueConstraint("employee_id", "resource"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    employee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_employees.id"))
    resource: Mapped[str] = mapped_column(String(40))
    actions: Mapped[list[str]] = mapped_column(ARRAY(Text))
    granted_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    granted_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
    updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EmailLog(Base):
    """Delivery record for every HRMS email (app/email_service.py) -- what was
    sent, to whom, about which HRMS record, and whether Microsoft Graph (or
    the SMTP fallback) accepted it. Written in the SAME transaction as the
    business change that triggers it (status QUEUED) and only handed to the
    sender after that transaction commits; the sender then marks it SENT /
    FAILED. SKIPPED = email is not configured. Body and attachment contents
    are never stored -- only their names. idempotency_key suppresses the
    same event being emailed twice (see email_service.queue_email). Maps to
    core_email_logs (backend/db/add_email_logs.sql)."""

    __tablename__ = "core_email_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    email_type: Mapped[str] = mapped_column(String(40))
    sender: Mapped[str | None] = mapped_column(String(150), nullable=True)
    recipient: Mapped[str] = mapped_column(Text)
    cc: Mapped[str | None] = mapped_column(Text, nullable=True)
    bcc: Mapped[str | None] = mapped_column(Text, nullable=True)
    subject: Mapped[str] = mapped_column(String(300))
    attachment_names: Mapped[str | None] = mapped_column(Text, nullable=True)
    related_entity_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    related_entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="QUEUED")
    transport: Mapped[str | None] = mapped_column(String(10), nullable=True)
    provider_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Durable outbox (backend/db/add_email_outbox.sql): the queued email
    # itself while it is undelivered (cleared on a final status), the retry
    # time after a transient failure, and when a worker claimed it.
    payload: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True, deferred=True)
    next_attempt_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)



class ExitLetterHtmlTemplate(Base):
    """Documents > Templates > Experience & Relieving Letter Template -- the
    company's own HTML+CSS design for the combined exit letter. Same shape
    and semantics as OfferLetterHtmlTemplate -- several named templates may
    be saved per company AND letter_type, exactly one ACTIVE at a time (see
    ux_exit_letter_tpl_active), plus the tenant-configured authorized
    signatory shown on the letter. Maps to hcm_exit_letter_html_templates
    (backend/db/add_exit_letters.sql)."""

    __tablename__ = "hcm_exit_letter_html_templates"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_companies.id"))
    letter_type: Mapped[str] = mapped_column(String(20))  # 'experience_relieving'
    name: Mapped[str] = mapped_column(String(120))
    html_body: Mapped[str] = mapped_column(Text)
    css_styles: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    signatory_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    signatory_designation: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # /media/exit_letter_asset/<company_id>/<file> -- never served by /media
    # (media_access denies the type); embedded only into the letter
    # (backend/db/add_exit_letter_seal_signature.sql).
    seal_image_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    signature_image_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EmailHtmlTemplate(Base):
    """Documents > Templates > Email Templates -- a company's own HTML+CSS
    design for a transactional email content template (approvals, leave
    decisions, interview/candidate messages, reward & promotion
    announcements, etc). Same multi-template/one-active shape as the
    document templates above, keyed by email_kind instead of letter_type
    (see email_service.EMAIL_KINDS for the closed set of values). Maps to
    hcm_email_templates (backend/db/add_multi_templates.sql)."""

    __tablename__ = "hcm_email_templates"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_companies.id"))
    email_kind: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(120))
    html_body: Mapped[str] = mapped_column(Text)
    css_styles: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ExitLetter(Base):
    """One generated Experience & Relieving Letter for an employee's exit --
    an immutable snapshot: the fully rendered HTML, the exact placeholder
    values used, the template name/version it came from and the stored PDF
    (uploads/exit_letter/<employee_id>/..., served only through the
    authenticated /api/exit-letters/{id}/pdf endpoint). Regenerating adds a
    new row with version + 1 and flips is_current; earlier rows and files
    are never modified, so editing the template later never changes a
    letter already issued. Maps to hcm_exit_letters."""

    __tablename__ = "hcm_exit_letters"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_companies.id"))
    employee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("core_employees.id"))
    exit_request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("hcm_exit_requests.id"))
    letter_type: Mapped[str] = mapped_column(String(20))
    version: Mapped[int] = mapped_column(Integer, default=1)
    letter_number: Mapped[str] = mapped_column(String(60))
    template_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    template_name: Mapped[str] = mapped_column(String(120))
    template_version: Mapped[int] = mapped_column(Integer)
    rendered_html: Mapped[str] = mapped_column(Text)
    placeholders: Mapped[dict] = mapped_column(JSONB, default=dict)
    file_url: Mapped[str] = mapped_column(Text)
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    generated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    generated_by_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    generated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ── Missing Attendance & Work Compliance (db/add_attendance_compliance.sql) ──

class ComplianceSettings(Base):
    """Tenant configuration for the EOD compliance check (app/compliance.py):
    the cutoff time (company local) and which checks run."""

    __tablename__ = "hcm_compliance_settings"

    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    eod_cutoff: Mapped[datetime.time] = mapped_column(Time, default=datetime.time(23, 0))
    check_attendance: Mapped[bool] = mapped_column(Boolean, default=True)
    check_overtime: Mapped[bool] = mapped_column(Boolean, default=True)
    check_work_entry: Mapped[bool] = mapped_column(Boolean, default=True)
    check_timesheet: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_employee: Mapped[bool] = mapped_column(Boolean, default=True)
    # Shift-based reminders (db/add_shift_reminders.sql; app/shift_reminders.py)
    reminders_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    clock_in_reminder_minutes: Mapped[int] = mapped_column(SmallInteger, default=15)
    end_reminder_minutes: Mapped[int] = mapped_column(SmallInteger, default=15)
    updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class ComplianceRun(Base):
    """One EOD pass per company per date -- the guard against duplicate alerts."""

    __tablename__ = "hcm_compliance_runs"

    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    run_date: Mapped[datetime.date] = mapped_column(Date, primary_key=True)
    processed_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    exceptions: Mapped[int] = mapped_column(Integer, default=0)


class ComplianceException(Base):
    """A missing check-in / check-out, missed overtime punch, missing work
    entry or timesheet found at the EOD cutoff. Unique per employee / date /
    type / reference (an overtime request or a timesheet week)."""

    __tablename__ = "hcm_compliance_exceptions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    employee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    exception_date: Mapped[datetime.date] = mapped_column(Date)
    exception_type: Mapped[str] = mapped_column(String(30))
    reference_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    reference_key: Mapped[str] = mapped_column(String(40), default="")
    expected_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    actual_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    regularization_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    regularization_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    manager_notified_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    employee_notified_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notification_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="open")
    resolved_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LeaveWithdrawal(Base):
    """An employee's request to revoke a pending / approved leave
    (db/add_leave_withdrawals.sql). Decided through the same Reporting
    Manager approval workflow as Leave Apply (doctype "leave_withdrawal");
    the leave stays active until this is approved."""

    __tablename__ = "hcm_leave_withdrawals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    employee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    leave_request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    leave_status_before: Mapped[str] = mapped_column(String(12))
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending | approved | rejected
    requested_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    approver_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    decision_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    restored_days: Mapped[float | None] = mapped_column(Numeric(5, 1), nullable=True)
    restored_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class ShiftReminder(Base):
    """A shift-based reminder (Clock In / prepare for Clock Out / Work Entry
    & Timesheet) -- one per employee / date / kind, so never shown twice;
    acknowledged_at = its popup was dismissed (on any device)."""

    __tablename__ = "hcm_shift_reminders"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    employee_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    reminder_date: Mapped[datetime.date] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(String(20))  # clock_in | clock_out | work_entry
    title: Mapped[str] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text)
    due_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    shift_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    notification_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LearningDocument(Base):
    """Learning & Development > Learning Documents (db/add_learning_documents.sql).
    Metadata only -- the file is in upload storage (uploads/learning_document/
    <id>/), served through /media and authorized by media_access."""

    __tablename__ = "hcm_learning_documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str] = mapped_column(String(40), default="Learning")
    file_name: Mapped[str] = mapped_column(String(255))
    file_url: Mapped[str] = mapped_column(Text)
    file_ext: Mapped[str] = mapped_column(String(10))
    mime_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    content_sha256: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(SmallInteger, default=1)
    uploaded_by_employee_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    uploaded_by_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    uploaded_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    deleted_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class LearningDocumentHistory(Base):
    """Upload history of Learning Documents: uploaded / updated /
    file_replaced / deleted (db/add_learning_documents.sql)."""

    __tablename__ = "hcm_learning_document_history"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(200))
    file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    actor_employee_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))


class TenantEmailSettings(Base):
    """public.tenant_email_settings -- one row per tenant (UNIQUE tenant_id):
    that tenant's outgoing-email provider and credentials. Every secret column
    (*_encrypted) holds app/tenant_email/encryption.py ciphertext, never
    plaintext. Global (public), deliberately NOT duplicated per tenant schema."""
    __tablename__ = "tenant_email_settings"
    __table_args__ = {"schema": "public"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("public.tenants.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False, default="smtp")
    # smtp
    smtp_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    smtp_port: Mapped[int | None] = mapped_column(Integer, nullable=True, default=587)
    smtp_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    smtp_password_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    smtp_use_tls: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    smtp_use_ssl: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # microsoft_graph
    microsoft_tenant_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    microsoft_client_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    microsoft_client_secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    # sendgrid
    sendgrid_api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ses (SESv2 API)
    ses_region: Mapped[str | None] = mapped_column(String(30), nullable=True)
    ses_access_key_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ses_secret_access_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    # common
    from_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    from_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reply_to: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=func.now())
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=func.now())
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
