import base64
import dataclasses
import datetime
import hashlib
import hmac
import json
import threading
import time
import uuid

import bcrypt
import jwt

from .config import settings

_JWT_ALGORITHM = "HS256"


@dataclasses.dataclass
class TokenData:
    user_id: uuid.UUID
    tenant_slug: str | None = None
    company_id: uuid.UUID | None = None
    employee_id: uuid.UUID | None = None
    roles: list = dataclasses.field(default_factory=list)
    # AUTH-A2 revocation claims (all optional -- tokens issued before this
    # change carry none of them and are treated as token_version 0 / no
    # password fingerprint, see deps.get_current_user).
    public_user_id: uuid.UUID | None = None  # "pid": public.users.id
    token_version: int = 0  # "tv": public.users.token_version at issue time
    password_fingerprint: str | None = None  # "pwf": see password_fingerprint()
    jti: str | None = None
    expires_at: float | None = None  # "exp" as a unix timestamp


# bcrypt only reads the first 72 bytes of its input -- longer passwords were
# silently truncated, so any two passwords sharing those 72 bytes matched.
BCRYPT_MAX_PASSWORD_BYTES = 72


def _bcrypt_input(plain_password: str) -> bytes:
    """What bcrypt actually hashes. Up to 72 UTF-8 bytes: the password itself
    (so every existing hash keeps verifying). Longer: base64(SHA-256(password))
    -- 44 bytes covering ALL of the password, base64 so it never carries a
    NUL. Used by hash_password and verify_password alike, so creation,
    change, reset and login can never disagree."""
    raw = plain_password.encode("utf-8")
    if len(raw) <= BCRYPT_MAX_PASSWORD_BYTES:
        return raw
    return base64.b64encode(hashlib.sha256(raw).digest())


def hash_password(plain_password: str) -> str:
    return bcrypt.hashpw(_bcrypt_input(plain_password), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, password_hash: str) -> bool:
    """False (never an exception) for a stored value that isn't a bcrypt
    hash -- public.users is shared with other applications, whose rows may
    hold another format. The dummy check keeps the timing of that refusal
    the same as a wrong password's."""
    try:
        return bcrypt.checkpw(_bcrypt_input(plain_password), password_hash.encode("utf-8"))
    except ValueError:
        bcrypt.checkpw(_bcrypt_input(plain_password), _DUMMY_PASSWORD_HASH)
        return False


def matches_legacy_truncated_hash(plain_password: str, password_hash: str | None) -> bool:
    """True if a >72-byte password matches a hash made BEFORE pre-hashing,
    i.e. from only its first 72 bytes. Used solely to identify accounts that
    need an admin reset -- it never authenticates anyone. Always False for
    passwords of up to 72 bytes (those hash exactly as before)."""
    raw = plain_password.encode("utf-8")
    if len(raw) <= BCRYPT_MAX_PASSWORD_BYTES:
        return False
    try:
        return bcrypt.checkpw(raw[:BCRYPT_MAX_PASSWORD_BYTES], (password_hash or "").encode("utf-8")
                              if password_hash else _DUMMY_PASSWORD_HASH)
    except ValueError:  # not a bcrypt hash (see verify_password)
        return False


# Login timing (no email enumeration): an unknown email still pays for one
# bcrypt check, against this hash. Built with the same gensalt() as
# hash_password, so it always has the same cost as a real stored hash; the
# hashed value is random and never kept, so no password can match it.
_DUMMY_PASSWORD_HASH = bcrypt.hashpw(uuid.uuid4().bytes + uuid.uuid4().bytes, bcrypt.gensalt())


def verify_password_or_dummy(plain_password: str, password_hash: str | None) -> bool:
    """verify_password, except that with no stored hash (unknown email) the
    same bcrypt work is done against a dummy hash and False is returned --
    so "no such user" and "wrong password" take comparable time."""
    if not password_hash:
        bcrypt.checkpw(_bcrypt_input(plain_password), _DUMMY_PASSWORD_HASH)
        return False
    return verify_password(plain_password, password_hash)


def password_fingerprint(password_hash: str | None) -> str | None:
    """Short keyed digest of the stored bcrypt hash, embedded in every tenant
    JWT ("pwf"). Any password change/reset rewrites the hash, so every token
    issued before it stops matching and is rejected -- revocation on
    password change that works even where public.users.token_version hasn't
    been migrated in yet. Keyed with the JWT secret so the claim reveals
    nothing about the hash itself."""
    if not password_hash:
        return None
    return hmac.new(
        settings.jwt_secret.encode("utf-8"), password_hash.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:16]


def create_access_token(
    user_id: uuid.UUID,
    tenant_slug: str | None = None,
    company_id: uuid.UUID | None = None,
    employee_id: uuid.UUID | None = None,
    roles: list[str] | None = None,
    public_user_id: uuid.UUID | None = None,
    token_version: int | None = None,
    password_hash: str | None = None,
) -> str:
    """P12: the full permission-code list is no longer embedded -- nothing
    ever read it (authorization is always re-derived from the DB per
    request; the Flutter app never decodes the JWT) and it inflated every
    request header. The client gets permissions from the login response body
    and GET /api/auth/me instead."""
    expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        minutes=settings.jwt_expire_minutes
    )
    payload: dict = {"sub": str(user_id), "exp": expires_at, "jti": uuid.uuid4().hex}
    if tenant_slug is not None:
        payload["tenant_slug"] = tenant_slug
    if company_id is not None:
        payload["company_id"] = str(company_id)
    if employee_id is not None:
        payload["employee_id"] = str(employee_id)
    if roles:
        payload["roles"] = roles
    if public_user_id is not None:
        payload["pid"] = str(public_user_id)
    if token_version is not None:
        payload["tv"] = int(token_version)
    fingerprint = password_fingerprint(password_hash)
    if fingerprint is not None:
        payload["pwf"] = fingerprint
    return jwt.encode(payload, settings.jwt_secret, algorithm=_JWT_ALGORITHM)


def decode_access_token(token: str) -> TokenData | None:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[_JWT_ALGORITHM])
        company_id = uuid.UUID(payload["company_id"]) if payload.get("company_id") else None
        employee_id = uuid.UUID(payload["employee_id"]) if payload.get("employee_id") else None
        public_user_id = uuid.UUID(payload["pid"]) if payload.get("pid") else None
        data = TokenData(
            user_id=uuid.UUID(payload["sub"]),
            tenant_slug=payload.get("tenant_slug"),
            company_id=company_id,
            employee_id=employee_id,
            roles=payload.get("roles", []),
            public_user_id=public_user_id,
            token_version=int(payload.get("tv", 0) or 0),
            password_fingerprint=payload.get("pwf"),
            jti=payload.get("jti"),
            expires_at=float(payload["exp"]) if payload.get("exp") is not None else None,
        )
    except (jwt.PyJWTError, KeyError, ValueError, TypeError):
        return None
    if data.jti is not None and is_token_revoked(data.jti):
        return None
    return data


# ── In-process revoked-token list (logout) ────────────────────────────────
# POST /api/auth/logout records the presented token's jti here so THAT token
# is rejected immediately even where public.users.token_version doesn't
# exist yet. Entries expire with the token itself. Process-local (lost on
# restart, not shared across workers) -- token_version, once migrated, is the
# durable mechanism; this is the immediate, zero-DDL one.
_revoked_lock = threading.Lock()
_revoked_jtis: dict[str, float] = {}


def revoke_token(jti: str | None, expires_at: float | None) -> None:
    if not jti:
        return
    now = time.time()
    with _revoked_lock:
        _revoked_jtis[jti] = expires_at or (now + settings.jwt_expire_minutes * 60)
        for key in [k for k, exp in _revoked_jtis.items() if exp < now]:
            del _revoked_jtis[key]


def is_token_revoked(jti: str) -> bool:
    with _revoked_lock:
        exp = _revoked_jtis.get(jti)
    return exp is not None and exp >= time.time()


# ── Signed /media URLs (SEC-04) ──────────────────────────────────────────
# A browser can't attach an Authorization header to <img src> or a
# launched URL, so every /media/... URL the API returns carries a short-
# lived `?t=<token>` bound to (path, tenant, user, expiry). /media accepts
# either that token or a normal bearer header, and then still authorizes
# the resolved user against the owning record (see media_access.py).

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _media_sig(path: str, body: str) -> str:
    mac = hmac.new(
        settings.jwt_secret.encode("utf-8"), f"media|{path}|{body}".encode("utf-8"), hashlib.sha256
    ).digest()
    return _b64url(mac[:18])


def sign_media_path(path: str, tenant_slug: str | None, user_id: uuid.UUID) -> str:
    """`path` is the part after /media/ (e.g. 'employee_photo/<id>/<f>.png').
    Expiry is bucketed to the TTL so the same response body (and therefore
    its ETag -- see CacheHeaderMiddleware) is stable within a bucket."""
    ttl = max(60, settings.media_url_ttl_seconds)
    exp = (int(time.time()) // ttl + 2) * ttl  # valid for 1-2 TTLs
    body = _b64url(
        json.dumps({"s": tenant_slug or "", "u": str(user_id), "e": exp}, separators=(",", ":")).encode()
    )
    return f"{body}.{_media_sig(path, body)}"


def verify_media_token(path: str, token: str) -> tuple[str | None, uuid.UUID] | None:
    """Returns (tenant_slug, user_id) for a valid, unexpired token for exactly
    this path, else None."""
    try:
        body, sig = token.split(".", 1)
        if not hmac.compare_digest(sig, _media_sig(path, body)):
            return None
        claims = json.loads(_b64url_decode(body))
        if int(claims["e"]) < time.time():
            return None
        return (claims.get("s") or None), uuid.UUID(claims["u"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None
