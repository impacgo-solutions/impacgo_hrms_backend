"""Operator commands for passwords (bcrypt 72-byte fix and manual setup).

    cd backend
    venv/Scripts/python -m app.password_admin list-flagged
    venv/Scripts/python -m app.password_admin reset-platform-admin --email ops@example.com
    venv/Scripts/python -m app.password_admin hash

  * list-flagged          every HRMS tenant login flagged as needing an admin
                          reset (its hash came from a >72-byte password; see
                          db/add_password_reset_required.sql). Tenant admins
                          reset these in the app (User Roles > Reset Password
                          or GET /api/users/password-reset-required); the
                          Super Admin console resets tenant owners.
  * reset-platform-admin  new password for a Super Admin console account
                          (there is no API to reset another platform admin).
  * hash                  prints a password hash for manual SQL such as
                          db/provision_new_tenant_and_owner.sql.

Passwords are always read with a no-echo prompt (never a command-line
argument, so they stay out of shell history), checked against the password
policy, and hashed by security.hash_password -- the same handling as every
API flow.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from pydantic_core import PydanticCustomError
from sqlalchemy import text

from . import auth_state, password_policy, security


def _prompt(min_length: int) -> str:
    while True:
        first = getpass.getpass("New password: ")
        if first != getpass.getpass("Repeat password: "):
            print("Passwords don't match.", file=sys.stderr)
            continue
        try:
            return password_policy.validate_password(first, min_length=min_length)
        except PydanticCustomError as exc:
            print(exc.message(), file=sys.stderr)


def list_flagged() -> int:
    from .database import SessionLocal

    db = SessionLocal()
    try:
        slugs = [r for r in db.execute(text(
            "SELECT DISTINCT tenant_slug FROM public.tenant_modules WHERE module_code = 'hcm' AND is_enabled "
            "ORDER BY 1")).scalars()]
        found = 0
        for slug in slugs:
            if not auth_state.has_reset_required_column(db, slug):
                print(f"{slug}\t(migration db/add_password_reset_required.sql not run for this tenant)")
                continue
            for r in db.execute(text(
                f"SELECT email, password_reset_required_at FROM {auth_state._quoted(slug)}.core_users "
                "WHERE password_reset_required ORDER BY password_reset_required_at"
            )).all():
                found += 1
                print(f"{slug}\t{r.email}\tflagged {r.password_reset_required_at:%Y-%m-%d %H:%M}")
        if not found:
            print("No HRMS accounts are waiting for a password reset.")
        return 0
    finally:
        db.close()


def reset_platform_admin(email: str) -> int:
    from . import crud
    from .database import SessionLocal

    db = SessionLocal()
    try:
        admin = crud.find_admin_user_by_email(db, email)
        if admin is None or not crud.is_platform_super_admin(db, admin):
            print("No platform admin with that email.", file=sys.stderr)
            return 1
        admin.password_hash = security.hash_password(_prompt(password_policy.PLATFORM_MIN_LENGTH))
        crud.create_platform_audit_log(db, admin, "password_reset_cli")
        db.commit()
        print("Password reset. Existing sessions of that account are signed out.")
        return 0
    finally:
        db.close()


def print_hash() -> int:
    print(security.hash_password(_prompt(password_policy.MIN_LENGTH)))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.password_admin", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list-flagged")
    reset = sub.add_parser("reset-platform-admin")
    reset.add_argument("--email", required=True)
    sub.add_parser("hash")
    args = parser.parse_args(argv)
    if args.command == "list-flagged":
        return list_flagged()
    if args.command == "reset-platform-admin":
        return reset_platform_admin(args.email)
    return print_hash()


if __name__ == "__main__":
    raise SystemExit(main())
