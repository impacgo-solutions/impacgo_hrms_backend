"""Pre-flight check for the Microsoft Graph email integration.

Reads MICROSOFT_TENANT_ID / MICROSOFT_CLIENT_ID / MICROSOFT_CLIENT_SECRET /
MICROSOFT_FROM_EMAIL from backend/.env (config.settings). Never prints any
credential or token -- only which variables are missing and, on failure,
the classified reason (invalid secret, wrong tenant, missing Mail.Send, ...).

Usage (from backend/):
    python -m app.verify_graph_email                          # config + token
    python -m app.verify_graph_email --send-test-to you@example.com
"""

import argparse
import sys

from . import graph_mail
from .config import settings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--send-test-to", metavar="EMAIL")
    args = parser.parse_args()

    missing = settings.microsoft_graph_missing
    if missing:
        print(
            "Microsoft Graph email configuration is incomplete.\n"
            "Please configure MICROSOFT_TENANT_ID, MICROSOFT_CLIENT_ID, MICROSOFT_CLIENT_SECRET "
            f"and MICROSOFT_FROM_EMAIL.\nMissing: {', '.join(missing)}"
        )
        return 2
    print(f"Configuration present. Sender mailbox: {graph_mail.sender_address()}")

    try:
        graph_mail.get_access_token(force_refresh=True)
    except graph_mail.GraphMailError as exc:
        print(f"FAILED to obtain an access token [{exc.code}]: {exc}")
        return 1
    print("OK: Microsoft Entra ID issued an app-only access token (client credentials).")

    if args.send_test_to:
        try:
            status = graph_mail.send_mail(graph_mail.MailMessage(
                to=[args.send_test_to],
                subject="HRMS Microsoft Graph Email Test",
                html_body="<p>Microsoft Graph email integration is working successfully.</p>",
            ))
        except graph_mail.GraphMailError as exc:
            print(f"FAILED to send [{exc.code}]: {exc}")
            return 1
        print(f"OK: test email accepted by Microsoft Graph (HTTP {status}) -> {args.send_test_to}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
