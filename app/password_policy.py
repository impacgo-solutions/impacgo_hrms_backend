"""One password policy for every flow that sets a password -- employee /
owner account creation, admin reset, owner reset, super-admin create and
change. Applied through the `NewPassword` / `PlatformPassword` schema types
(app/schemas.py), so a request that breaks it gets a 422 whose message says
which rule failed (shown as-is by both apps) and never anything about other
accounts.

Rules (validate_password):
  * 8-100 characters (platform accounts: 12+), not blank;
  * at least 3 of: lower-case, upper-case, digit, symbol (any other
    character, including non-ASCII letters, counts as a symbol);
  * not a common / breached password: the bundled list below, checked
    case-insensitively and also with trailing digits/symbols removed
    ("Welcome@123", "Password1!" -> "welcome", "password"), and not one
    character / an obvious sequence repeated;
  * not containing the account's own email name or person's name, where the
    flow knows them;
  * not in the Have I Been Pwned corpus (PASSWORD_BREACH_CHECK_ENABLED, on
    by default), via its k-anonymity range API -- only the first 5 hex
    characters of the password's SHA-1 leave the server, with response
    padding on. A lookup failure never blocks the user (the local checks
    still apply).
"""

from __future__ import annotations

import hashlib
import logging
import re
import urllib.request

from pydantic_core import PydanticCustomError

from .config import settings

logger = logging.getLogger(__name__)

MIN_LENGTH = 8
PLATFORM_MIN_LENGTH = 12
MAX_LENGTH = 100

POLICY_SUMMARY = (
    "Passwords need at least 8 characters and 3 of: upper-case letter, lower-case letter, "
    "digit, symbol. Common or previously breached passwords are not allowed."
)

# Most common passwords / base words from public breach-frequency lists, plus
# defaults typical for HR systems. Compared lower-cased, with and without
# trailing digits/symbols.
_COMMON = frozenset("""
123456 1234567 12345678 123456789 1234567890 12345 1234 111111 000000 123123 654321 666666 121212
112233 123321 987654321 147258369 159753 789456 password passw0rd p@ssw0rd p@ssword pass passwd
password1 qwerty qwertyuiop qwerty123 asdfgh asdfghjkl zxcvbnm 1q2w3e4r 1q2w3e4r5t 1qaz2wsx
qazwsx abc123 abcd1234 abcdef abcdefgh iloveyou letmein welcome welcome1 admin administrator
root toor login master monkey dragon football baseball soccer sunshine princess shadow superman
batman trustno1 starwars whatever freedom secret secret123 changeme default guest test
tester testing test123 demo user user123 hello hello123 charlie michael jordan jennifer hunter
ranger buster thomas robert daniel killer pepper ginger summer winter spring autumn flower
cheese computer internet google samsung apple india bharat mumbai delhi bangalore hyderabad
chennai pune kolkata krishna ganesh shiva sairam jaishriram omnamah hanuman lakshmi
company office manager employee staff payroll hrms people impacgo impacgoinfo temp temp123
newpassword new password123 mypassword yourpassword nopassword access access123 hr hradmin
""".split())

_TRAILING = re.compile(r"[\d\W_]+$")
_LEADING = re.compile(r"^[\d\W_]+")
_SEQUENCES = ("abcdefghijklmnopqrstuvwxyz", "0123456789", "qwertyuiopasdfghjklzxcvbnm")


def _fail(message: str) -> PydanticCustomError:
    return PydanticCustomError("password_policy", message)


def _is_common(value: str) -> bool:
    lowered = value.lower()
    candidates = {lowered, _TRAILING.sub("", lowered), _LEADING.sub("", _TRAILING.sub("", lowered))}
    # Simple leetspeak undo: "P@ssw0rd" -> "password".
    for c in list(candidates):
        candidates.add(c.translate(str.maketrans("@4310$5!7", "aaeiossit")))
    return any(c in _COMMON for c in candidates if c)


def _is_trivial(value: str) -> bool:
    lowered = value.lower()
    if len(set(lowered)) <= 2:
        return True
    return any(lowered in seq or lowered in seq[::-1] for seq in _SEQUENCES)


def _classes(value: str) -> int:
    return sum((
        any(c.islower() for c in value),
        any(c.isupper() for c in value),
        any(c.isdigit() for c in value),
        any(not c.isalnum() or not c.isascii() for c in value),
    ))


def _personal_parts(context: tuple[str | None, ...]) -> set[str]:
    parts: set[str] = set()
    for item in context:
        if not item:
            continue
        item = item.split("@", 1)[0]
        for piece in re.split(r"[^a-z0-9]+", item.lower()):
            if len(piece) >= 4:
                parts.add(piece)
    return parts


def _breached_online(value: str) -> bool:
    sha1 = hashlib.sha1(value.encode("utf-8")).hexdigest().upper()
    prefix, suffix = sha1[:5], sha1[5:]
    try:
        req = urllib.request.Request(
            f"https://api.pwnedpasswords.com/range/{prefix}",
            headers={"Add-Padding": "true", "User-Agent": "impacgo-hrms-password-policy"},
        )
        with urllib.request.urlopen(req, timeout=settings.password_breach_check_timeout_seconds) as resp:
            body = resp.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 -- fail open; the local list still applied
        logger.warning("Breached-password lookup unavailable; local checks only")
        return False
    for line in body.splitlines():
        found, _, count = line.partition(":")
        if found.strip() == suffix and count.strip() not in ("", "0"):
            return True
    return False


def validate_password(value: str, *, min_length: int = MIN_LENGTH, context: tuple[str | None, ...] = ()) -> str:
    """Returns `value` unchanged, or raises PydanticCustomError (a 422 inside
    a schema) naming the first rule it breaks."""
    if not isinstance(value, str) or not value.strip():
        raise _fail("Password is required.")
    if len(value) < min_length:
        raise _fail(f"Password must be at least {min_length} characters.")
    if len(value) > MAX_LENGTH:
        raise _fail(f"Password must be at most {MAX_LENGTH} characters.")
    if _classes(value) < 3:
        raise _fail(
            "Password must include at least 3 of: upper-case letter, lower-case letter, digit, symbol."
        )
    if _is_trivial(value) or _is_common(value):
        raise _fail("This password is too common or has appeared in a data breach. Choose a different one.")
    lowered = value.lower()
    if any(part in lowered for part in _personal_parts(context)):
        raise _fail("Password must not contain your name or email address.")
    if settings.password_breach_check_enabled and _breached_online(value):
        raise _fail("This password is too common or has appeared in a data breach. Choose a different one.")
    return value


def validate_platform_password(value: str) -> str:
    return validate_password(value, min_length=PLATFORM_MIN_LENGTH)
