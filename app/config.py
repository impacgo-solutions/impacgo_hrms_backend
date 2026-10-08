import logging
import re

from pydantic_settings import BaseSettings, SettingsConfigDict

_logger = logging.getLogger(__name__)

# The fallback JWT secret below is only acceptable for local development --
# see Settings.validate_for_startup, which refuses to boot outside dev with it.
INSECURE_DEFAULT_JWT_SECRET = "dev-only-insecure-secret-change-me"
_DEV_ENVS = {"dev", "development", "local", "test"}
_ORIGIN_RE = re.compile(r"^https?://[A-Za-z0-9.\-]+(:\d{1,5})?$")


class Settings(BaseSettings):
    database_url: str
    cors_origins: str = "*"
    default_company_name: str = "my company"
    default_tenant_slug: str = "default"
    jwt_secret: str = INSECURE_DEFAULT_JWT_SECRET
    jwt_expire_minutes: int = 480
    uploads_dir: str = "uploads"

    # Deployment environment (APP_ENV). Anything other than dev/development/
    # local/test is treated as a real deployment: the API refuses to start
    # with the insecure default JWT secret, and refuses CORS_ORIGINS=* unless
    # CORS_ALLOW_WILDCARD=true is set explicitly (see validate_for_startup).
    app_env: str = "dev"
    cors_allow_wildcard: bool = False
    # L-37: interactive API docs (/docs, /redoc, /openapi.json). Unset =
    # on only in dev (APP_ENV dev/local/test); API_DOCS_ENABLED=true/false
    # overrides either way.
    api_docs_enabled: bool | None = None

    # Account lockout (AUTH-A3): after `login_max_failed_attempts` wrong
    # passwords in a row, login for that account is refused (429 +
    # Retry-After) for `login_lockout_minutes`, even with the right password.
    login_max_failed_attempts: int = 5
    login_lockout_minutes: int = 15

    # Password policy (app/password_policy.py): Have I Been Pwned range
    # lookup for every new password (create / change / reset; never login).
    # Only the first 5 hex characters of the password's SHA-1 are sent to
    # api.pwnedpasswords.com (k-anonymity) -- never the password or the full
    # hash. Lookup failures fall back to the bundled common-password list.
    # PASSWORD_BREACH_CHECK_ENABLED=false turns it off (e.g. no outbound
    # internet).
    password_breach_check_enabled: bool = True
    password_breach_check_timeout_seconds: float = 2.0

    # Tenant lifecycle enforcement (AUTH-A4): public.tenants.status =
    # 'blocked' / is_active = false, and an ended trial (status 'trial' with
    # trial_ends_at in the past) are refused at login and on every request.
    enforce_tenant_status: bool = True
    enforce_trial_expiry: bool = True
    # How long (seconds) per-request auth state (public.users is_active /
    # token_version / password fingerprint + tenant status) is cached in
    # process memory. Logout invalidates its own entry immediately.
    auth_state_cache_seconds: int = 15

    # Lifetime of the signed `?t=` token appended to /media/... URLs in API
    # responses (SEC-04). URLs are re-signed on every response.
    media_url_ttl_seconds: int = 3600

    # Real email delivery for notifications (see email_service.py). Off by
    # default -- unset/blank smtp_host disables sending entirely so a dev
    # environment with no mail account configured behaves exactly as before
    # (in-app notifications only). Never put real credentials here or in
    # any committed file -- these are read from the environment / .env
    # (gitignored) only.
    email_enabled: bool = True
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False
    email_from_address: str = ""
    email_from_name: str = "Impacgo HRMS"

    # Microsoft Graph email (app/graph_mail.py) -- Microsoft Entra ID app
    # registration using the OAuth 2.0 client-credentials flow and the
    # Mail.Send APPLICATION permission. When all four are set, Graph is the
    # transport for every HRMS email (SMTP above is used only while they are
    # blank). MICROSOFT_CLIENT_SECRET is the secret's VALUE, not its Secret
    # ID. Backend environment / .env only -- never in the frontend, never in
    # git, never logged. See docs/MICROSOFT_GRAPH_EMAIL.md.
    microsoft_tenant_id: str = ""
    microsoft_client_id: str = ""
    microsoft_client_secret: str = ""
    microsoft_from_email: str = ""
    # Graph request timeout (seconds) and retries for transient failures
    # (429 throttling, 5xx, network errors).
    microsoft_graph_timeout_seconds: int = 20
    microsoft_graph_max_retries: int = 3
    # Optional "on behalf of" sender: the email is sent through the
    # MICROSOFT_FROM_EMAIL mailbox with From = this address (Outlook shows
    # "info@... on behalf of <name>"). Exchange requires the FROM mailbox to
    # hold Send on Behalf on this mailbox (Set-Mailbox <this> -GrantSendOnBehalfTo
    # <MICROSOFT_FROM_EMAIL>); until then emails go from MICROSOFT_FROM_EMAIL
    # with Reply-To this address (graph_mail.send_mail). Blank = off.
    microsoft_on_behalf_of: str = ""
    microsoft_on_behalf_of_name: str = ""
    # Daily reminders (app/reminders.py): birthdays / work anniversaries on
    # the day, holidays REMINDER_HOLIDAY_DAYS_BEFORE days ahead, sent from
    # REMINDER_SEND_HOUR (company local time) onwards. Background loop in
    # the API process; REMINDERS_ENABLED=false turns it off.
    reminders_enabled: bool = True
    reminder_send_hour: int = 9
    reminder_holiday_days_before: int = 1
    reminder_poll_minutes: int = 15
    # Per-tenant email (public.tenant_email_settings, app/tenant_email/).
    # Master key encrypting every tenant's SMTP password / Graph client secret /
    # API key at rest: one or more Fernet keys, comma-separated (first
    # encrypts, all decrypt -- rotation). Generate:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # Without it secrets can neither be stored nor read (never plaintext).
    tenant_email_encryption_key: str = ""
    # Tenants (slugs, comma-separated, or "*") that may fall back to the global
    # SMTP_* / MICROSOFT_* settings above when they have no row in
    # tenant_email_settings. Empty (default) = NO fallback: an unconfigured
    # tenant sends no email rather than borrowing another company's mailbox.
    email_global_fallback_tenants: str = ""
    # Optional Reply-To for HRMS emails (e.g. hr@impacgo.com); blank = none.
    email_reply_to: str = ""
    # Extra recipients (comma-separated) for new leave applications, in
    # addition to the employee's reporting manager(s), e.g. the HR inbox.
    email_hr_recipients: str = ""
    # Safety net for test / staging databases full of demo employees:
    # comma-separated recipient domains (e.g. "impacgo.com"). When set, mail
    # to any other domain is never sent (automatic: logged SKIPPED with
    # error_code domain_not_allowed; manual sends: refused). Blank = any.
    email_allowed_domains: str = ""
    # Graph's sendMail request is limited to ~4 MB; base64 adds a third, so
    # attachments are capped at 3 MB in total per email.
    email_max_attachment_bytes: int = 3 * 1024 * 1024
    # Durable email outbox (email_service.process_outbox): how often queued
    # emails a restart dropped, and transient-failure retries, are picked up.
    email_outbox_poll_seconds: int = 60
    # Manual HR-document / payslip / offer-letter / test emails per user
    # per minute (middleware/rate_limit.py).
    rate_limit_email_per_minute: int = 10
    # Base URL of the deployed Flutter web app, used to build the "direct
    # link to the relevant HRMS screen" inside notification emails.
    frontend_base_url: str = "http://localhost:8090"
    # Public base URL of THIS API, used for candidate self-service links
    # (offer response / preboarding document upload) emailed to candidates,
    # who have no HRMS login. Must be reachable by candidates in production.
    # Blank = FRONTEND_BASE_URL (deploy/nginx serves /api on the app's domain).
    public_api_base_url: str = ""

    # Approve / Reject buttons inside approval emails (app/email_actions.py).
    # Off by default; links expire after email_action_ttl_hours and open a
    # confirmation page served from PUBLIC_API_BASE_URL (or FRONTEND_BASE_URL).
    email_actions_enabled: bool = False
    email_action_ttl_hours: int = 72

    # API rate limiting (middleware/rate_limit.py). Requests per minute per
    # logged-in user (or per client IP when there's no valid token); login is
    # always per IP. Bursts up to the full per-minute amount are allowed.
    # Only set rate_limit_trust_proxy when running behind a reverse proxy
    # that sets X-Forwarded-For -- otherwise clients can spoof their IP.
    rate_limit_enabled: bool = True
    rate_limit_per_minute: int = 300
    rate_limit_anon_per_minute: int = 60
    rate_limit_login_per_minute: int = 10
    rate_limit_sensitive_per_minute: int = 5
    rate_limit_upload_per_minute: int = 30
    rate_limit_trust_proxy: bool = False

    # DB connection pool / per-connection safety timeouts (database.py).
    # pool_size + max_overflow must stay <= Postgres max_connections (100,
    # shared) divided by the number of app worker processes. Timeouts are
    # in milliseconds; 0 disables that timeout.
    db_pool_size: int = 20
    db_max_overflow: int = 10
    db_pool_timeout: int = 10
    db_pool_recycle: int = 1800
    db_statement_timeout_ms: int = 30000
    db_lock_timeout_ms: int = 5000
    db_idle_in_transaction_timeout_ms: int = 60000
    # Launch headless Chromium once at startup to verify PDF rendering works
    # (logs a loud error if not; never blocks startup). See template_rendering.py.
    pdf_browser_startup_check: bool = True
    # Extra hosts (comma-separated) whose images/fonts/stylesheets PDF
    # rendering may fetch; everything else except the company logo is blocked.
    pdf_allowed_asset_hosts: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    @property
    def is_dev(self) -> bool:
        return self.app_env.strip().lower() in _DEV_ENVS

    @property
    def docs_enabled(self) -> bool:
        return self.is_dev if self.api_docs_enabled is None else bool(self.api_docs_enabled)

    def invalid_cors_origins(self) -> list[str]:
        """M-41: entries of CORS_ORIGINS that aren't a bare http(s) origin
        (scheme://host[:port], no path, no wildcard)."""
        bad = []
        for origin in self.cors_origin_list:
            if origin == "*":
                continue
            if not _ORIGIN_RE.match(origin):
                bad.append(origin)
        return bad

    def validate_for_startup(self) -> None:
        """C19: refuse to start a real deployment with insecure defaults.
        Local dev (APP_ENV unset / 'dev') only gets warnings, so an existing
        .env with CORS_ORIGINS=* keeps working."""
        insecure_secret = (
            self.jwt_secret == INSECURE_DEFAULT_JWT_SECRET or len(self.jwt_secret) < 32
        )
        wildcard_cors = self.cors_origins.strip() == "*"
        invalid_origins = self.invalid_cors_origins()
        if self.is_dev:
            _logger.warning(
                "APP_ENV=%s (development mode): CORS / JWT checks only warn, /docs is %s, and email "
                "goes only to EMAIL_ALLOWED_DOMAINS (default: the sender's domain). Set "
                "APP_ENV=production in every real deployment.",
                self.app_env, "on" if self.docs_enabled else "off",
            )
            if invalid_origins:
                _logger.warning("CORS_ORIGINS has invalid origin entries: %s", ", ".join(invalid_origins))
            if insecure_secret:
                _logger.warning(
                    "JWT_SECRET is the insecure default (or shorter than 32 chars) -- "
                    "acceptable only for local development (APP_ENV=%s).", self.app_env,
                )
            if wildcard_cors:
                _logger.warning(
                    "CORS_ORIGINS=* -- any website can call this API from a browser. "
                    "Acceptable only for local development (APP_ENV=%s).", self.app_env,
                )
            return
        if insecure_secret:
            raise RuntimeError(
                f"Refusing to start: APP_ENV={self.app_env!r} but JWT_SECRET is the insecure "
                "default or shorter than 32 characters. Set a long random JWT_SECRET."
            )
        if wildcard_cors and not self.cors_allow_wildcard:
            raise RuntimeError(
                f"Refusing to start: APP_ENV={self.app_env!r} with CORS_ORIGINS=*. List the "
                "real frontend origins, or set CORS_ALLOW_WILDCARD=true to accept this explicitly."
            )
        # M-41: every listed origin must be an exact scheme://host[:port].
        if invalid_origins:
            raise RuntimeError(
                f"Refusing to start: APP_ENV={self.app_env!r} and CORS_ORIGINS contains invalid "
                f"origins {invalid_origins}. Use exact origins such as https://hrms.example.com "
                "(no path, no trailing slash, no wildcard), comma-separated."
            )
        insecure_http = [
            o for o in self.cors_origin_list
            if o.startswith("http://") and not re.match(r"^http://(localhost|127\.0\.0\.1)(:\d+)?$", o)
        ]
        if insecure_http:
            _logger.warning("CORS_ORIGINS lists plain-http origins in APP_ENV=%s: %s",
                            self.app_env, ", ".join(insecure_http))
        if wildcard_cors:
            _logger.warning("CORS_ORIGINS=* accepted because CORS_ALLOW_WILDCARD=true.")

    _MICROSOFT_KEYS = (
        "microsoft_tenant_id", "microsoft_client_id",
        "microsoft_client_secret", "microsoft_from_email",
    )

    @property
    def microsoft_graph_missing(self) -> list[str]:
        """Names (never values) of the MICROSOFT_* variables still blank."""
        return [k.upper() for k in self._MICROSOFT_KEYS if not (getattr(self, k) or "").strip()]

    @property
    def microsoft_graph_configured(self) -> bool:
        return not self.microsoft_graph_missing

    def validate_email_config(self) -> None:
        """Startup check for Microsoft Graph email: logs which variables are
        missing (names only). Never raises -- email is best-effort and the
        rest of the HRMS must keep working without it."""
        if not (self.tenant_email_encryption_key or "").strip():
            _logger.error(
                "TENANT_EMAIL_ENCRYPTION_KEY is not set: per-tenant email settings cannot be saved or "
                "decrypted (secrets are never stored in plaintext). Generate a key: python -c \"from "
                "cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )
        if not (self.email_global_fallback_tenants or "").strip():
            # Tenants send only with their own public.tenant_email_settings row; the
            # global SMTP_* / MICROSOFT_* variables below are not used at all.
            _logger.info("Email: per-tenant settings only (EMAIL_GLOBAL_FALLBACK_TENANTS is empty).")
            return
        missing = self.microsoft_graph_missing
        if not missing:
            _logger.info(
                "Microsoft Graph email configured (sender %s).", self.microsoft_from_email.strip()
            )
            return
        if len(missing) < len(self._MICROSOFT_KEYS):
            _logger.error(
                "Microsoft Graph email configuration is incomplete. Please configure "
                "MICROSOFT_TENANT_ID, MICROSOFT_CLIENT_ID, MICROSOFT_CLIENT_SECRET and "
                "MICROSOFT_FROM_EMAIL (missing: %s). HRMS emails are not sent via Graph.",
                ", ".join(missing),
            )
        elif self.smtp_host:
            _logger.warning(
                "Microsoft Graph email is not configured -- using the legacy SMTP transport. "
                "Configure MICROSOFT_TENANT_ID, MICROSOFT_CLIENT_ID, MICROSOFT_CLIENT_SECRET and "
                "MICROSOFT_FROM_EMAIL to send via Microsoft Graph."
            )
        else:
            _logger.warning(
                "Microsoft Graph email configuration is incomplete. Please configure "
                "MICROSOFT_TENANT_ID, MICROSOFT_CLIENT_ID, MICROSOFT_CLIENT_SECRET and "
                "MICROSOFT_FROM_EMAIL. HRMS emails are logged but not sent."
            )

    @property
    def cors_origin_list(self) -> list[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
