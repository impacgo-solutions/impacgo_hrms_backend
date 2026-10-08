import asyncio
import logging
import re
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.orm import Session

from . import auth_state, database, deps, media_access, models, security, ws_manager
from .config import settings
from .database import get_db
from .middleware.cache_headers import CacheHeaderMiddleware
from .middleware.errors import UnhandledErrorMiddleware
from .middleware.media_urls import MediaUrlSigningMiddleware
from .middleware.rate_limit import RateLimitMiddleware
from .storage import winlong_path
from .routers import recruitment_workflow as recruitment_workflow_router
from .routers import candidate_portal
from .routers import email_actions as email_actions_router
from .routers import worktrack as worktrack_router
from .routers import (
    approval_inbox,
    approvals_config,
    assets,
    attendance,
    audit,
    auth,
    bands,
    benefits,
    config as config_router,
    custom_modules,
    module_access,
    designations,
    documents,
    email as email_router,
    email_settings as email_settings_router,
    email_templates,
    employees,
    employee_permissions as employee_permissions_router,
    exit_letters,
    fnf,
    overtime_settings,
    compliance as compliance_router,
    leave_withdrawals,
    tasks,
    learning_documents,
    holiday_imports,
    leave,
    learning,
    me,
    modules as modules_router,
    notifications,
    organization,
    payroll,
    performance,
    permissions,
    projects,
    recruitment,
    reports,
    roles,
    settings as settings_router,
    super_admin,
    travel,
    user_roles,
    work,
)

logger = logging.getLogger(__name__)

# C19: refuse to boot a real deployment with the insecure JWT secret / CORS *.
settings.validate_for_startup()
# Microsoft Graph email: logs missing MICROSOFT_* variable NAMES (never values).
settings.validate_email_config()


class _RedactTokensFilter(logging.Filter):
    """AUTH-A6: never write bearer tokens that travel in a URL (the legacy
    `?token=` WebSocket query param, the signed `/media?t=`) to the access
    log. uvicorn.access records carry the request line in record.args."""

    _pattern = re.compile(r"([?&](?:token|t)=)[^&\s\"]+")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                self._pattern.sub(r"\1[REDACTED]", a) if isinstance(a, str) else a
                for a in record.args
            )
        elif isinstance(record.msg, str):
            record.msg = self._pattern.sub(r"\1[REDACTED]", record.msg)
        return True


logging.getLogger("uvicorn.access").addFilter(_RedactTokensFilter())
logging.getLogger("uvicorn.error").addFilter(_RedactTokensFilter())

app = FastAPI(
    title="Impacgo HRMS API",
    description="Backs login, the Administration > Roles & Permissions screen, "
    "and the Organization > Designations tab.",
    version="1.0.0",
    # L-37: no public API explorer / schema outside dev (API_DOCS_ENABLED overrides).
    docs_url="/docs" if settings.docs_enabled else None,
    redoc_url="/redoc" if settings.docs_enabled else None,
    openapi_url="/openapi.json" if settings.docs_enabled else None,
)


@app.on_event("startup")
async def _bind_ws_loop() -> None:
    """Lets ws_manager.manager.push (called synchronously from the
    after_commit hook in ws_manager.py, itself firing inside FastAPI's
    threadpool for sync route handlers) schedule sends onto this loop."""
    ws_manager.manager.bind_loop(asyncio.get_running_loop())


@app.on_event("startup")
async def _start_reminders() -> None:
    """Daily birthday / work-anniversary / holiday reminders (app/reminders.py)
    -- a daemon thread; REMINDERS_ENABLED=false turns it off."""
    from . import reminders

    reminders.start()


@app.on_event("startup")
async def _start_email_outbox() -> None:
    """Durable email outbox (email_service.process_outbox): resends queued
    emails a restart dropped and retries transient delivery failures."""
    from . import email_service

    email_service.start_outbox()


@app.on_event("startup")
async def _check_pdf_renderer() -> None:
    """FUNC-01: verify headless Chromium can launch (payslip/offer-letter
    PDFs). Runs in a background thread and only logs -- never blocks or
    fails startup. Disable with PDF_BROWSER_STARTUP_CHECK=false."""
    if settings.pdf_browser_startup_check:
        from . import template_rendering

        template_rendering.start_pdf_browser_check_in_background()


@app.exception_handler(RequestValidationError)
async def _log_request_validation_error(request: Request, exc: RequestValidationError):
    """FastAPI's default 422 handler returns exc.errors() to the client but
    never logs it server-side -- so a 422 on e.g. POST /api/employees (a
    request body field failing Pydantic, such as a blank branch_name/
    department_name) leaves nothing to diagnose from beyond "the client got
    a 422". Logging the exact per-field errors here, before returning the
    identical response body FastAPI would have sent anyway, doesn't change
    any client-visible behavior."""
    errors = _redact_secrets(exc.errors())
    logger.warning(
        "422 validation error on %s %s: %s",
        request.method, request.url.path, errors,
    )
    return JSONResponse(
        status_code=422,
        content=jsonable_encoder({"detail": errors}),
    )


def _is_secret_key(key) -> bool:
    return isinstance(key, str) and ("password" in key.lower() or "secret" in key.lower())


def _redact_value(value):
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if _is_secret_key(k) else _redact_value(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    return value


def _redact_secrets(errors: list) -> list:
    """Pydantic echoes the rejected `input` in each error -- never send a
    password back to the client or write it to the log: a password field's
    own input is dropped, and password keys inside a whole-body input (e.g.
    a model-level check) are redacted."""
    cleaned = []
    for err in errors:
        err = dict(err)
        if any(_is_secret_key(part) for part in err.get("loc", ())):
            err.pop("input", None)
        elif "input" in err:
            err["input"] = _redact_value(err["input"])
        cleaned.append(err)
    return cleaned


@app.exception_handler(Exception)
async def _log_unhandled_exception(request: Request, exc: Exception):
    """Catch-all for anything not already handled by a more specific
    handler (HTTPException, RequestValidationError above) -- i.e. genuine
    bugs and unexpected failures (DB errors, etc.), which previously
    surfaced as a bare FastAPI 500 with nothing logged beyond whatever
    uvicorn's own console output happened to show. Logs full context
    (a request id the client can quote back for support, method/path,
    the caller's user/tenant if the request carried a valid bearer token,
    and the full stack trace) and returns a generic, safe body that never
    leaks internal details to the client. get_db's own try/finally already
    closes (and thus rolls back) the DB session regardless of how the
    request ends, so no extra cleanup is needed here."""
    # WF-01: a request that waited longer than DB_LOCK_TIMEOUT_MS for a row /
    # advisory lock (e.g. a duplicate submission racing the first) is a
    # conflict to retry, not a server error.
    pgcode = getattr(getattr(exc, "orig", None), "pgcode", None)
    if pgcode == "55P03":  # lock_not_available
        return JSONResponse(
            status_code=409,
            content={"detail": "This record is being updated by another request. Please try again."},
        )
    request_id = uuid.uuid4().hex
    user_id = tenant_slug = None
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token_data = security.decode_access_token(auth_header[7:])
        if token_data is not None:
            user_id, tenant_slug = token_data.user_id, token_data.tenant_slug
    logger.error(
        "Unhandled exception [request_id=%s] on %s %s (user=%s, tenant=%s)",
        request_id, request.method, request.url.path, user_id, tenant_slug,
        exc_info=exc,
    )
    return JSONResponse(
        status_code=500,
        content={
            "detail": "An unexpected error occurred. Please try again or contact support.",
            "request_id": request_id,
        },
    )

# Innermost: signs /media URLs in JSON responses (SEC-04) -- must see the
# uncompressed body and run before CacheHeaderMiddleware computes the ETag.
app.add_middleware(MediaUrlSigningMiddleware)
# API-02: request-scoped client IP picked up by crud.create_audit_log.
from .middleware.client_ip import ClientIPMiddleware  # noqa: E402

app.add_middleware(ClientIPMiddleware)
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(CacheHeaderMiddleware)
# Added after the others but before CORS, so it runs outermost-but-one:
# a throttled request is rejected before any caching/DB work, and the 429
# still gets CORS headers so the browser app can read it.
app.add_middleware(RateLimitMiddleware)
# TC-D24: oversized non-upload bodies are refused (413) before any work.
from .middleware.body_limit import BodySizeLimitMiddleware  # noqa: E402

app.add_middleware(BodySizeLimitMiddleware)
# ERR-02: unhandled exceptions become the JSON 500 (with request_id) here,
# INSIDE CORS, so the browser can read it.
app.add_middleware(UnhandledErrorMiddleware, build_500=_log_unhandled_exception)
# L-23: inverted from/to period on any /api/reports/* endpoint -> 422.
from .middleware.report_range import ReportRangeMiddleware  # noqa: E402

app.add_middleware(ReportRangeMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
# L-36: outermost, so every response (errors, 429s, CORS preflights, /media)
# carries the security headers.
from .middleware.security_headers import SecurityHeadersMiddleware  # noqa: E402

app.add_middleware(SecurityHeadersMiddleware)

# Backs uploaded leave-request attachments (e.g. medical certificates) --
# served back at the file_url core.attachments rows store, see storage.py.
Path(settings.uploads_dir).mkdir(parents=True, exist_ok=True)


def _media_user(request: Request, rel_path: str, t: str | None, db: Session):
    """SEC-04: resolve the caller of a /media request -- a normal bearer
    header (full get_current_user checks), or the short-lived signed `?t=`
    token that API responses append to every /media URL (see
    middleware/media_urls.py) because <img>/launched URLs can't send headers."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return deps.authenticate_token(auth[7:], db)
    if not t:
        raise HTTPException(status_code=401, detail="Not authenticated")
    claims = security.verify_media_token(rel_path, t)
    if claims is None:
        raise HTTPException(status_code=401, detail="This file link has expired. Reload the page and try again.")
    tenant_slug, user_id = claims
    if not tenant_slug:
        raise HTTPException(status_code=401, detail="Not authenticated")
    state = auth_state.load_auth_state(db, security.TokenData(user_id=user_id, tenant_slug=tenant_slug))
    if state is None or not state.is_active or state.tenant_block_reason is not None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    database.set_tenant_context(tenant_slug)
    database.set_session_tenant_slug(db, tenant_slug)
    database.ensure_tenant_search_path(db)
    user = db.get(models.User, user_id)
    if user is None or user.status != "active":
        raise HTTPException(status_code=401, detail="Not authenticated")
    database.set_session_current_user(db, user.id)
    return user


@app.api_route("/media/{file_path:path}", methods=["GET", "HEAD"])
def serve_uploaded_file(
    file_path: str,
    request: Request,
    t: str | None = None,
    db: Session = Depends(get_db),
) -> FileResponse:
    """Hand-rolled replacement for `app.mount("/media", StaticFiles(...))` --
    StaticFiles resolves/stats/opens the file using plain (non-extended)
    Windows paths internally, so it 404s on exactly the same over-260-
    character absolute paths that `storage.save_uploaded_file` had to work
    around with `winlong_path` (this project's own folder is deeply
    nested). Re-using `winlong_path` here means a file that uploads
    successfully can also always be read back, on any OS.

    SEC-04 / C10: requires authentication (bearer header or signed `?t=`)
    and authorizes the caller against the record that owns the file
    (media_access.authorize). Anonymous -> 401; a record outside the
    caller's tenant/company -> 404; visible record, not allowed -> 403."""
    root = Path(settings.uploads_dir).resolve()
    candidate = (root / file_path).resolve()
    # Reject path traversal (e.g. `../../app/main.py`) -- resolve() collapses
    # `..` segments, so a candidate that ends up outside `root` is rejected.
    if root not in candidate.parents and candidate != root:
        raise HTTPException(status_code=404, detail="Not Found")
    rel_path = candidate.relative_to(root).as_posix()
    user = _media_user(request, rel_path, t, db)
    decision = media_access.authorize(db, user, rel_path)
    if decision == media_access.FORBIDDEN:
        raise HTTPException(status_code=403, detail="You don't have permission to view this file")
    if decision is not None:
        raise HTTPException(status_code=404, detail="Not Found")
    long_candidate = winlong_path(candidate)
    if not long_candidate.is_file():
        raise HTTPException(status_code=404, detail="Not Found")
    return FileResponse(long_candidate, headers=media_access.MEDIA_RESPONSE_HEADERS)


app.include_router(auth.router)
app.include_router(roles.router)
app.include_router(permissions.router)
app.include_router(designations.router)
app.include_router(bands.router)
app.include_router(organization.router)
app.include_router(employees.router)
app.include_router(attendance.router)
app.include_router(leave.router)
app.include_router(payroll.router)
app.include_router(recruitment.router)
app.include_router(performance.router)
app.include_router(learning.router)
app.include_router(benefits.router)
app.include_router(assets.router)
app.include_router(documents.router)
app.include_router(projects.router)
app.include_router(work.router)
app.include_router(worktrack_router.router)
app.include_router(travel.router)
app.include_router(audit.router)
app.include_router(reports.router)
app.include_router(settings_router.router)
app.include_router(notifications.router)
app.include_router(config_router.router)
app.include_router(modules_router.router)
app.include_router(approvals_config.router)
app.include_router(approval_inbox.router)
app.include_router(user_roles.router)
app.include_router(me.router)
app.include_router(super_admin.router)
app.include_router(custom_modules.router)
app.include_router(module_access.router)
app.include_router(email_router.router)
app.include_router(email_settings_router.router)
app.include_router(email_templates.router)
app.include_router(exit_letters.router)
app.include_router(fnf.router)
app.include_router(overtime_settings.router)
app.include_router(compliance_router.router)
app.include_router(leave_withdrawals.router)
app.include_router(tasks.router)
app.include_router(learning_documents.router)
app.include_router(holiday_imports.router)
app.include_router(recruitment_workflow_router.router)
app.include_router(candidate_portal.router)
app.include_router(email_actions_router.router)
app.include_router(employee_permissions_router.router)


@app.get("/health")
def health():
    return {"status": "ok"}
