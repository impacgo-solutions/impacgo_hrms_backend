import datetime
import uuid
from contextvars import ContextVar

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

def _pg_connect_options() -> str:
    """Per-connection server-side safety timeouts (DB-03), sent as libpq
    startup options so they cost no extra round trip. Each is
    env-configurable via config.py (DB_STATEMENT_TIMEOUT_MS etc.); 0 turns
    that one off."""
    opts = []
    if settings.db_statement_timeout_ms > 0:
        opts.append(f"-c statement_timeout={int(settings.db_statement_timeout_ms)}")
    if settings.db_lock_timeout_ms > 0:
        opts.append(f"-c lock_timeout={int(settings.db_lock_timeout_ms)}")
    if settings.db_idle_in_transaction_timeout_ms > 0:
        opts.append(
            f"-c idle_in_transaction_session_timeout={int(settings.db_idle_in_transaction_timeout_ms)}"
        )
    return " ".join(opts)


_connect_options = _pg_connect_options()

# PERF-04: the default QueuePool (5 + 10 overflow, 30 s timeout, no
# recycle) was smaller than uvicorn's 40-thread pool, so moderate load
# queued for up to the full 30 s pool timeout. Sized/tuned via env
# (DB_POOL_SIZE, DB_MAX_OVERFLOW, DB_POOL_TIMEOUT, DB_POOL_RECYCLE).
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_timeout=settings.db_pool_timeout,
    pool_recycle=settings.db_pool_recycle,
    connect_args={"options": _connect_options} if _connect_options else {},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# SEC-08 / C15: tenant routing uses SET LOCAL (transaction-scoped, see
# _apply_tenant_search_path), so a pooled connection never carries one
# tenant's search_path into the next checkout. A few legacy paths (seed/
# migration scripts, anything issuing a plain session-level
# `SET search_path`) still can -- this marks such a connection "dirty" so
# the checkin hook below resets it before anyone else can check it out.
# Zero extra round trips on the normal request path.
@event.listens_for(engine, "before_cursor_execute")
def _track_session_level_search_path(conn, cursor, statement, parameters, context, executemany):
    head = statement.lstrip()[:40].lower()
    if head.startswith("set ") and "search_path" in head and not head.startswith("set local"):
        conn.info["search_path_dirty"] = True


@event.listens_for(engine, "checkin")
def _reset_dirty_search_path(dbapi_connection, connection_record):
    if dbapi_connection is None or not connection_record.info.pop("search_path_dirty", False):
        return
    try:
        cur = dbapi_connection.cursor()
        cur.execute("RESET search_path")
        cur.close()
        dbapi_connection.commit()
    except Exception:  # pragma: no cover - never hand a tenant-scoped connection back out
        connection_record.invalidate()

# Set by deps.set_tenant_context() at the start of each request.
# The after_begin listener picks it up and applies SET LOCAL search_path
# for every new transaction (including ones that start after a mid-request commit).
_tenant_slug_ctx: ContextVar[str | None] = ContextVar("_tenant_slug", default=None)


def set_tenant_context(slug: str) -> None:
    _tenant_slug_ctx.set(slug)


def get_tenant_slug() -> str | None:
    return _tenant_slug_ctx.get()


def set_session_tenant_slug(db: Session, slug: str) -> None:
    """Stamps the tenant slug onto this Session instance's own `.info` dict
    (plain per-Session storage, not a ContextVar) so code further down the
    same request can read it back reliably. get_tenant_slug()'s ContextVar
    does NOT survive this: FastAPI runs sync dependencies/route handlers
    each in their own threadpool-copied context (starlette's
    run_in_threadpool / anyio.to_thread.run_sync), so a ContextVar.set() in
    one dependency (deps.get_current_user) is invisible to another
    (the endpoint body, or crud helpers it calls) even within the same
    request -- only values threaded through as real arguments (like this
    `db` Session object) are guaranteed to be shared."""
    db.info["tenant_slug"] = slug


def get_session_tenant_slug(db: Session) -> str | None:
    return db.info.get("tenant_slug")


def set_session_current_user(db: Session, user_id: uuid.UUID) -> None:
    """Stamps the authenticated user's id onto this Session's `.info` dict --
    same pattern as set_session_tenant_slug above, for the identical reason
    (a ContextVar set in deps.get_current_user's threadpool-copied context
    wouldn't be visible anywhere else). Read by _stamp_audit_fields below so
    created_by/updated_by get populated automatically instead of every
    crud.py create_*/update_* function setting them by hand."""
    db.info["current_user_id"] = user_id


def get_session_current_user(db: Session) -> uuid.UUID | None:
    return db.info.get("current_user_id")


class Base(DeclarativeBase):
    pass


@event.listens_for(Session, "before_flush")
def _stamp_audit_fields(session, flush_context, instances):
    """Common audit-field mechanism: any mapped model carrying
    created_at/created_by/updated_at/updated_by (checked per-instance via
    its own mapper, so a model missing one or more of these columns just
    silently skips those) gets them populated automatically on every INSERT
    and UPDATE -- no crud.py create_*/update_* function needs to set them
    itself. Registered on the base Session class (not just database.
    SessionLocal) so it fires for every session anywhere in the app,
    including the seed scripts' own separate sessionmakers (e.g.
    seed_new_company.py's --tenant path binds a fresh engine/sessionmaker
    per invocation).

    Runs inside before_flush, so the stamps are part of the exact same
    flush/transaction as the rows they describe -- if that transaction
    rolls back, the stamps roll back with it; there's no separate write to
    fail out of sync.

    On create: created_at/created_by are only filled if still unset (so
    code that already explicitly assigns them, e.g. routers/employees.py's
    `employee.created_by = current_user.id`, is left alone rather than
    overwritten with -- in that specific case -- the identical value
    anyway); updated_at/updated_by are always set fresh, matching "a new
    record's last-updated moment is its creation moment".
    On update: only updated_at/updated_by are touched, exactly per spec --
    created_at/created_by are never revisited after insert.

    No user context (e.g. a seed script run with no authenticated caller)
    just leaves created_by/updated_by NULL, same as if this mechanism
    didn't exist -- both columns are nullable everywhere they're mapped, so
    this never blocks a write.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    user_id = session.info.get("current_user_id")

    for obj in session.new:
        mapped = {c.key for c in obj.__mapper__.column_attrs}
        if "created_at" in mapped and getattr(obj, "created_at", None) is None:
            obj.created_at = now
        if (
            "created_by" in mapped
            and getattr(obj, "created_by", None) is None
            and user_id is not None
        ):
            obj.created_by = user_id
        if "updated_at" in mapped:
            obj.updated_at = now
        if "updated_by" in mapped and user_id is not None:
            obj.updated_by = user_id

    for obj in session.dirty:
        if obj in session.new or obj in session.deleted:
            continue
        if not session.is_modified(obj, include_collections=False):
            continue
        mapped = {c.key for c in obj.__mapper__.column_attrs}
        if "updated_at" in mapped:
            obj.updated_at = now
        if "updated_by" in mapped and user_id is not None:
            obj.updated_by = user_id


def ensure_tenant_search_path(db: Session) -> None:
    """Makes sure the current transaction of `db` is routed to the tenant
    stamped by set_session_tenant_slug -- exactly once per transaction.
    Opening the transaction fires _apply_tenant_search_path (which issues
    the SET LOCAL and records it); only if the transaction was already
    open before the slug was stamped does this issue the SET itself.
    Replaces the unconditional explicit `SET search_path` callers used to
    run on top of after_begin's own SET (PERF-05: 2-3 SETs per request)."""
    slug = get_session_tenant_slug(db)
    if not slug:
        return
    db.connection()  # autobegin -> after_begin applies SET LOCAL
    txn = db.get_transaction()
    marker = db.info.get("_search_path_txn")
    if marker is None or marker[0] != slug or marker[1] is not txn:
        db.execute(text(f'SET LOCAL search_path TO "{slug}", public'))
        db.info["_search_path_txn"] = (slug, txn)


@event.listens_for(SessionLocal, "after_begin")
def _apply_tenant_search_path(session, transaction, connection):
    # session.info first: it's the same Session instance threaded through
    # every dependency in a request (see set_session_tenant_slug), so it
    # survives fine even when this listener fires on a transaction that
    # begins well after deps.get_current_user ran -- e.g. autobegin opening
    # a fresh transaction (possibly on a different pooled connection) right
    # after a mid-request rollback/commit. The ContextVar is a best-effort
    # fallback for callers with no Session handy to stamp (the seed scripts).
    #
    # Savepoints (begin_nested) run inside the outer transaction, which
    # already carries the SET LOCAL -- re-issuing it per savepoint was pure
    # overhead (PERF-05).
    if transaction.nested:
        return
    slug = session.info.get("tenant_slug") or _tenant_slug_ctx.get()
    if slug:
        # SET LOCAL lasts until this transaction ends. Every later
        # transaction in the same request (after a mid-request commit/
        # rollback) fires this listener again, so routing still holds
        # across commits -- but the setting can never outlive the
        # transaction and leak to the next user of this pooled connection
        # (SEC-08 / C15).
        connection.execute(text(f'SET LOCAL search_path TO "{slug}", public'))
        session.info["_search_path_txn"] = (slug, transaction)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
