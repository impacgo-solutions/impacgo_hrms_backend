"""One-off pre-flight check for the Microsoft 365 SMTP integration (see
email_service.py / the task report for context). Run this BEFORE setting
EMAIL_ENABLED=true, so a broken/disabled mailbox is caught here instead of
silently swallowed by email_service's own best-effort error handling.

Reads SMTP_HOST/SMTP_PORT/SMTP_USERNAME/SMTP_PASSWORD straight from
config.settings (i.e. from backend/.env) -- the credential is never
printed, logged, or echoed back, only the pass/fail outcome and (on
failure) the server's own error text, which for Microsoft 365 already
names the exact missing admin step (e.g. "SmtpClientAuthentication is
disabled for the Mailbox").

Usage (from backend/):
    python -m app.verify_m365_smtp [--send-test-to you@example.com]
"""

import argparse
import smtplib
import ssl
import sys
from email.message import EmailMessage
from email.utils import formataddr

from .config import settings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--send-test-to",
        default=None,
        help="If given, sends one real test email to this address after a successful login.",
    )
    args = parser.parse_args()

    if not settings.smtp_host:
        print("FAIL: SMTP_HOST is not set in .env -- nothing to test.")
        return 1
    if not settings.smtp_username:
        print("FAIL: SMTP_USERNAME is not set in .env.")
        return 1
    if not settings.smtp_password:
        print(
            "FAIL: SMTP_PASSWORD is not set in .env.\n"
            "      This must be the info@impacgo.com mailbox password, or -- if that\n"
            "      account has MFA/Security Defaults enabled (the M365 default) -- an\n"
            "      App Password generated for it instead of the normal sign-in password."
        )
        return 1

    print(f"Connecting to {settings.smtp_host}:{settings.smtp_port} ...")
    try:
        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15)
        server.ehlo()
        if settings.smtp_use_tls:
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
        print("STARTTLS: OK")
    except Exception as exc:
        print(f"FAIL: could not establish a TLS connection -- {exc!r}")
        return 1

    print(f"Authenticating as {settings.smtp_username} ...")
    try:
        server.login(settings.smtp_username, settings.smtp_password)
        print("AUTH LOGIN: OK -- the mailbox accepts Authenticated SMTP with this credential.")
    except smtplib.SMTPAuthenticationError as exc:
        print("FAIL: authentication was rejected by the server.")
        print(f"      Server said: {exc.smtp_code} {exc.smtp_error!r}")
        print(
            "      Most common cause for Microsoft 365: 'SmtpClientAuthentication is\n"
            "      disabled for the Mailbox' -- an admin must enable Authenticated SMTP\n"
            "      for info@impacgo.com (Exchange admin center > Recipients > Mailboxes >\n"
            "      info@impacgo.com > Mail flow settings > 'Manage email apps settings' >\n"
            "      enable 'Authenticated SMTP'), or the tenant-wide default may be\n"
            "      blocking it (Exchange Online PowerShell:\n"
            "      Set-CasMailbox info@impacgo.com -SmtpClientAuthenticationDisabled $false).\n"
            "      If the account has MFA/Security Defaults enabled, use an App Password\n"
            "      instead of the normal sign-in password."
        )
        server.quit()
        return 1
    except Exception as exc:
        print(f"FAIL: unexpected error during authentication -- {exc!r}")
        server.quit()
        return 1

    if args.send_test_to:
        print(f"Sending a real test email to {args.send_test_to} ...")
        message = EmailMessage()
        message["Subject"] = "Impacgo HRMS -- SMTP verification test"
        message["From"] = formataddr((settings.email_from_name, settings.email_from_address))
        message["To"] = args.send_test_to
        message.set_content(
            "This is a one-off verification email confirming Microsoft 365 SMTP "
            "(smtp.office365.com) is correctly configured for Impacgo HRMS "
            "notification delivery from info@impacgo.com."
        )
        try:
            server.send_message(message)
            print("Test email: SENT -- check the inbox (and Junk folder) to confirm delivery.")
        except Exception as exc:
            print(f"FAIL: login succeeded but sending the test message failed -- {exc!r}")
            server.quit()
            return 1

    server.quit()
    print("\nAll checks passed. Safe to set EMAIL_ENABLED=true in backend/.env.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
