"""Per-request account/tenant state checks shared by login and
deps.get_current_user (AUTH-A2 token revocation, AUTH-A3 lockout, AUTH-A4
tenant status).

Column tolerance
────────────────
public.users.token_version / failed_login_attempts / locked_until come from
backend/db/add_public_user_security_columns.sql, which has NOT been run on
every database (it wasn't on impacgo_test_dev as of 2026-09-24). Which of
them exist is detected once per process from information_schema, and the
code degrades instead of failing:
  * token_version missing  -> no durable "log out everywhere"; logout still
    revokes the presented token via security.revoke_token (in-process), and
    password change/reset still revokes every older token through the "pwf"
    password-fingerprint claim.
  * lockout columns missing -> failed-attempt counters are kept in process
    memory (per email), which still stops a single-process brute force.
Restart the API after running the migration so the new columns are picked up.
"""

import dataclasses
import datetime
import threading
import time
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from . import security
from .config import settings

_OPTIONAL_COLUMNS = ("token_version", "failed_login_attempts", "locked_until")
_columns_lock = threading.Lock()
_present_columns: set[str] | None = None


def _public_user_columns(db: Session) -> set[str]:
    global _present_columns
    if _present_columns is None:
        rows = db.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'users' "
                "AND column_name = ANY(:cols)"
            ),
            {"cols": list(_OPTIONAL_COLUMNS)},
        ).scalars().all()
        with _columns_lock:
            _present_columns = set(rows)
    return _present_columns


def has_token_version(db: Session) -> bool:
    return "token_version" in _public_user_columns(db)


# ── Accounts needing an admin password reset (bcrypt 72-byte fix) ────────
# Stored on each HRMS tenant's own core_users (db/add_password_reset_required.sql
# adds it only to tenants with the 'hcm' module) -- never on the shared
# public.users, which other applications' tenants use too. A tenant without
# the column (not HRMS, or not migrated yet) simply never flags anyone.

_reset_column_lock = threading.Lock()
_reset_column_by_tenant: dict[str, bool] = {}


def _quoted(slug: str) -> str:
    return '"' + slug.replace('"', '""') + '"'


def has_reset_required_column(db: Session, tenant_slug: str | None) -> bool:
    if not tenant_slug:
        return False
    if tenant_slug not in _reset_column_by_tenant:
        present = db.execute(
            text(
                "SELECT 1 FROM information_schema.columns WHERE table_schema = :s "
                "AND table_name = 'core_users' AND column_name = 'password_reset_required'"
            ),
            {"s": tenant_slug},
        ).first() is not None
        with _reset_column_lock:
            _reset_column_by_tenant[tenant_slug] = present
    return _reset_column_by_tenant[tenant_slug]


def flag_password_reset_required(db: Session, public_user) -> bool:
    """Marks the tenant login behind `public_user` as needing an admin
    password reset. Caller commits. False (no-op) outside migrated HRMS
    tenants."""
    slug = public_user.tenant_slug
    if not has_reset_required_column(db, slug):
        return False
    db.execute(
        text(
            f"UPDATE {_quoted(slug)}.core_users SET password_reset_required = true, "
            "password_reset_required_at = COALESCE(password_reset_required_at, now()) "
            "WHERE employee_id = :eid"
        ),
        {"eid": public_user.employee_id or public_user.id},
    )
    return True


def is_password_reset_required(db: Session, public_user) -> bool:
    slug = public_user.tenant_slug
    if not has_reset_required_column(db, slug):
        return False
    return bool(db.execute(
        text(f"SELECT bool_or(password_reset_required) FROM {_quoted(slug)}.core_users WHERE employee_id = :eid"),
        {"eid": public_user.employee_id or public_user.id},
    ).scalar())


def clear_password_reset_required(db: Session, tenant_slug: str | None, core_user_id: uuid.UUID) -> None:
    """Called wherever a new password hash is written. Caller commits."""
    if not has_reset_required_column(db, tenant_slug):
        return
    db.execute(
        text(
            f"UPDATE {_quoted(tenant_slug)}.core_users SET password_reset_required = false, "
            "password_reset_required_at = NULL WHERE id = :id AND password_reset_required"
        ),
        {"id": core_user_id},
    )


def is_hrms_tenant(db: Session, tenant_slug: str) -> bool:
    """Tenant has the HRMS ('hcm') module enabled -- HRMS-only jobs skip the
    rest (e.g. a retail-only tenant sharing this database)."""
    return db.execute(
        text("SELECT 1 FROM public.tenant_modules WHERE tenant_slug = :s AND module_code = 'hcm' AND is_enabled"),
        {"s": tenant_slug},
    ).first() is not None


# public.users is shared with other applications (retail, fin/scm). Their
# logins must not open an HRMS session: login refuses them and a token for
# such a tenant is refused per request (load_auth_state).
NOT_HRMS_TENANT_DETAIL = "This account does not have access to Impacgo People."
_HRMS_EXISTS_SQL = (
    "EXISTS (SELECT 1 FROM public.tenant_modules tm WHERE tm.tenant_slug = u.tenant_slug "
    "AND tm.module_code = 'hcm' AND tm.is_enabled)"
)


def has_lockout_columns(db: Session) -> bool:
    cols = _public_user_columns(db)
    return "failed_login_attempts" in cols and "locked_until" in cols


# ── Tenant lifecycle ──────────────────────────────────────────────────────

def tenant_block_reason(
    status: str | None, is_active: bool | None, trial_ends_at: datetime.date | None
) -> str | None:
    """Human-readable reason this tenant may not be used right now, or None.
    A tenant row that doesn't exist at all (status None) is not blocked here
    -- older tenants pre-date public.tenants rows being mandatory."""
    if status is None and is_active is None:
        return None
    if settings.enforce_tenant_status and (status == "blocked" or is_active is False):
        return "Your organization's account has been suspended. Please contact support."
    if settings.enforce_tenant_status and status == "expired":
        return "Your organization's subscription has expired. Please contact support to renew."
    if (
        settings.enforce_trial_expiry
        and status == "trial"
        and trial_ends_at is not None
        and trial_ends_at < datetime.date.today()
    ):
        return "Your organization's free trial has ended. Please contact support to upgrade."
    return None


def tenant_block_reason_for_slug(db: Session, tenant_slug: str) -> str | None:
    row = db.execute(
        text("SELECT status, is_active, trial_ends_at FROM public.tenants WHERE slug = :s"),
        {"s": tenant_slug},
    ).first()
    if row is None:
        return None
    return tenant_block_reason(row.status, row.is_active, row.trial_ends_at)


# ── Per-request account state (cached briefly) ────────────────────────────

@dataclasses.dataclass(frozen=True)
class AuthState:
    public_user_id: uuid.UUID
    is_active: bool
    password_fingerprint: str | None
    token_version: int
    tenant_block_reason: str | None


_cache_lock = threading.Lock()
_cache: dict[tuple, tuple[float, AuthState | None]] = {}


def invalidate(public_user_id: uuid.UUID | None = None) -> None:
    with _cache_lock:
        if public_user_id is None:
            _cache.clear()
            return
        for key in [k for k, (_exp, st) in _cache.items() if st and st.public_user_id == public_user_id]:
            del _cache[key]


def load_auth_state(db: Session, token: "security.TokenData") -> AuthState | None:
    """Current public.users + public.tenants state behind a tenant token, or
    None if no login row backs it any more. Looked up by the token's "pid"
    claim; tokens issued before that claim existed fall back to (tenant,
    core user id / employee id) -- public.users.id usually, but not always,
    equals core_users.id."""
    key = (token.public_user_id, token.tenant_slug, token.user_id, token.employee_id)
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
    if hit is not None and hit[0] > now:
        return hit[1]

    tv_col = "u.token_version" if has_token_version(db) else "0"
    base = (
        f"SELECT u.id, u.is_active, u.password_hash, {tv_col} AS token_version, "
        "t.status AS t_status, t.is_active AS t_active, t.trial_ends_at, "
        f"{_HRMS_EXISTS_SQL} AS is_hrms "
        "FROM public.users u LEFT JOIN public.tenants t ON t.slug = u.tenant_slug "
    )
    if token.public_user_id is not None:
        row = db.execute(text(base + "WHERE u.id = :pid"), {"pid": token.public_user_id}).first()
    else:
        row = db.execute(
            text(
                base + "WHERE u.tenant_slug = :slug AND (u.id = :uid OR u.employee_id = :eid) "
                "ORDER BY (u.id = :uid) DESC LIMIT 1"
            ),
            {"slug": token.tenant_slug, "uid": token.user_id, "eid": token.employee_id},
        ).first()
    state = None
    if row is not None:
        state = AuthState(
            public_user_id=row.id,
            is_active=bool(row.is_active),
            password_fingerprint=security.password_fingerprint(row.password_hash),
            token_version=int(row.token_version or 0),
            tenant_block_reason=(
                tenant_block_reason(row.t_status, row.t_active, row.trial_ends_at)
                if row.is_hrms else NOT_HRMS_TENANT_DETAIL
            ),
        )
    with _cache_lock:
        _cache[key] = (now + max(0, settings.auth_state_cache_seconds), state)
        if len(_cache) > 5000:
            for k in [k for k, (exp, _s) in _cache.items() if exp <= now]:
                del _cache[k]
    return state


def bump_token_version(db: Session, public_user_id: uuid.UUID | None) -> bool:
    """Invalidates every token issued to this login so far ("log out
    everywhere"). Caller commits. Returns False (no-op) when the
    token_version column hasn't been migrated in yet."""
    if public_user_id is None:
        return False
    invalidate(public_user_id)
    if not has_token_version(db):
        return False
    db.execute(
        text("UPDATE public.users SET token_version = token_version + 1 WHERE id = :id"),
        {"id": public_user_id},
    )
    return True


def public_user_id_for_core_user(db: Session, core_user) -> uuid.UUID | None:
    """public.users row behind a tenant core_users row (same id in most, but
    not all, tenants -- otherwise matched by employee_id)."""
    row = db.execute(
        text(
            "SELECT id FROM public.users WHERE id = :uid OR (employee_id = :eid AND :eid IS NOT NULL) "
            "ORDER BY (id = :uid) DESC LIMIT 1"
        ),
        {"uid": core_user.id, "eid": core_user.employee_id},
    ).first()
    return row.id if row is not None else None


# ── Account lockout (AUTH-A3) ─────────────────────────────────────────────

_lockout_lock = threading.Lock()
# email -> (consecutive failures, locked_until unix ts)
_memory_failures: dict[str, tuple[int, float]] = {}


def _lock_seconds() -> int:
    return max(1, settings.login_lockout_minutes) * 60


# Stand-in id for an unknown email: the lockout statements below still run
# against it (matching no row), so an unknown email makes the same database
# round trips as a real account and can't be told apart by response time.
_NO_USER_ID = uuid.UUID(int=0)


def lockout_remaining_seconds(db: Session, email: str, public_user) -> int:
    """Seconds until this account may try again (0 = not locked)."""
    now = time.time()
    if has_lockout_columns(db):
        locked_until = db.execute(
            text("SELECT locked_until FROM public.users WHERE id = :id"),
            {"id": public_user.id if public_user is not None else _NO_USER_ID},
        ).scalar()
    if public_user is not None and has_lockout_columns(db):
        if locked_until is not None:
            remaining = locked_until.timestamp() - now
            if remaining > 0:
                return int(remaining) + 1
        return 0
    with _lockout_lock:
        _count, until = _memory_failures.get(email, (0, 0.0))
    return int(until - now) + 1 if until > now else 0


def record_failed_login(db: Session, email: str, public_user) -> int:
    """Counts one failed attempt; returns the lock duration in seconds if this
    attempt triggered a lock, else 0. Caller commits."""
    max_attempts = max(1, settings.login_max_failed_attempts)
    if public_user is not None and has_lockout_columns(db):
        row = db.execute(
            text(
                "UPDATE public.users SET failed_login_attempts = failed_login_attempts + 1, "
                "locked_until = CASE WHEN failed_login_attempts + 1 >= :max "
                "THEN now() + make_interval(secs => :secs) ELSE locked_until END "
                "WHERE id = :id RETURNING failed_login_attempts"
            ),
            {"id": public_user.id, "max": max_attempts, "secs": _lock_seconds()},
        ).first()
        if row is not None and row.failed_login_attempts >= max_attempts:
            db.execute(
                text("UPDATE public.users SET failed_login_attempts = 0 WHERE id = :id"),
                {"id": public_user.id},
            )
            return _lock_seconds()
        return 0
    if has_lockout_columns(db):
        # Unknown email: same UPDATE, matching no row (timing parity); the
        # attempt itself is counted in memory below.
        db.execute(
            text(
                "UPDATE public.users SET failed_login_attempts = failed_login_attempts + 1, "
                "locked_until = CASE WHEN failed_login_attempts + 1 >= :max "
                "THEN now() + make_interval(secs => :secs) ELSE locked_until END "
                "WHERE id = :id RETURNING failed_login_attempts"
            ),
            {"id": _NO_USER_ID, "max": max_attempts, "secs": _lock_seconds()},
        ).first()
    now = time.time()
    with _lockout_lock:
        count, until = _memory_failures.get(email, (0, 0.0))
        count += 1
        if count >= max_attempts:
            _memory_failures[email] = (0, now + _lock_seconds())
            return _lock_seconds()
        _memory_failures[email] = (count, until)
        if len(_memory_failures) > 10000:
            for k in [k for k, (c, u) in _memory_failures.items() if u < now and c == 0]:
                del _memory_failures[k]
    return 0


def reset_failed_logins(db: Session, email: str, public_user) -> None:
    if public_user is not None and has_lockout_columns(db):
        db.execute(
            text(
                "UPDATE public.users SET failed_login_attempts = 0, locked_until = NULL "
                "WHERE id = :id AND (failed_login_attempts <> 0 OR locked_until IS NOT NULL)"
            ),
            {"id": public_user.id},
        )
    with _lockout_lock:
        _memory_failures.pop(email, None)
