"""Durable email outbox (app/email_service.py): a queued email travels with
its core_email_logs row until delivered, so a restart / crash / lost DB
connection never silently drops it; transient failures are retried; an
email is never sent twice.

    cd backend && venv/Scripts/python -m unittest tests.test_email_outbox -v

Nothing reaches Microsoft (_deliver is faked) and every database change runs
inside a transaction that is ALWAYS rolled back (tenant Infyq by default,
EMAIL_TEST_TENANT to override).
"""

from __future__ import annotations

import datetime
import os
import sys
import unittest
import uuid
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import Session, undefer  # noqa: E402

from app import database, models  # noqa: E402
from app import email_service as es  # noqa: E402
from app.config import settings  # noqa: E402

TENANT = os.environ.get("EMAIL_TEST_TENANT", "Infyq")
SENT = ("SENT", 202, None, None, "graph")
THROTTLED = ("FAILED", 429, "throttled", "Microsoft Graph is throttling requests; will retry.", "graph")
BAD_RECIPIENT = ("FAILED", 400, "invalid_recipient", "Microsoft Graph rejected a recipient email address.", "graph")


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.engine.connect()
        self.outer = self.conn.begin()
        self.conn.execute(text(f'SET LOCAL search_path TO "{TENANT}", public'))
        self.db = Session(bind=self.conn, join_transaction_mode="create_savepoint")
        database.set_session_tenant_slug(self.db, TENANT)
        self.company = self.db.scalars(select(models.Company)).first()
        # Workers use the test session; nothing leaves this transaction.
        mock.patch.object(es, "_open_session", lambda slug: self.db).start()
        mock.patch.object(self.db, "close", lambda: None).start()
        mock.patch.object(settings, "email_allowed_domains", "*").start()  # any domain (L-38: blank = dev default)
        mock.patch.object(es, "transport", lambda slug=None: "graph").start()
        self.submitted = []
        mock.patch.object(es._executor, "submit",
                          side_effect=lambda fn, *a: self.submitted.append((fn, a))).start()
        self.delivered = []
        self.outcome = SENT
        mock.patch.object(es, "_deliver", side_effect=self._fake_deliver).start()

    def tearDown(self):
        mock.patch.stopall()
        self.db.close()
        self.outer.rollback()
        self.conn.close()

    def _fake_deliver(self, job):
        self.delivered.append(job)
        return self.outcome

    def _queue(self, subject="Outbox test", **job_kwargs) -> models.EmailLog:
        log = es.queue_email(self.db, es.EmailJob(
            email_type="OUTBOX_TEST", to=["someone@impacgo.com"], subject=subject,
            html_body="<p>Hello</p>", company_id=self.company.id, **job_kwargs,
        ))
        self.db.commit()
        self.assertIsNotNone(log)
        return log

    def _run_submitted(self):
        while self.submitted:
            fn, args = self.submitted.pop(0)
            fn(*args)

    def _row(self, log_id) -> models.EmailLog:
        self.db.expire_all()
        return self.db.scalars(select(models.EmailLog).options(undefer(models.EmailLog.payload))
                               .where(models.EmailLog.id == log_id)).one()

    def test_queued_email_carries_its_content(self):
        log = self._queue()
        row = self._row(log.id)
        self.assertEqual(row.status, "QUEUED")
        self.assertEqual(row.payload["to"], ["someone@impacgo.com"])
        self.assertEqual(row.payload["html_body"], "<p>Hello</p>")
        self.assertEqual(len(self.submitted), 1, "still dispatched immediately after commit")

    def test_delivery_claims_once_and_clears_the_content(self):
        log = self._queue()
        self._run_submitted()
        row = self._row(log.id)
        self.assertEqual((row.status, row.attempts), ("SENT", 1))
        self.assertIsNone(row.payload, "no message body is kept once delivered")
        self.assertIsNone(row.claimed_at)
        self.assertIsNotNone(row.sent_at)
        # A second worker for the same row (duplicate dispatch, another
        # process) can't claim it -- nothing is sent twice.
        es._worker(es._job_from_log(row), row.id, TENANT)
        self.assertEqual(len(self.delivered), 1)

    def test_transient_failure_is_retried_then_delivered(self):
        self.outcome = THROTTLED
        log = self._queue()
        self._run_submitted()
        row = self._row(log.id)
        self.assertEqual((row.status, row.attempts, row.error_code), ("QUEUED", 1, "throttled"))
        self.assertIsNotNone(row.payload, "kept for the retry")
        self.assertGreater(row.next_attempt_at, datetime.datetime.now(datetime.timezone.utc))
        self.assertIn("Retrying automatically", row.error_message)
        # Not due yet: the outbox leaves it alone.
        es.process_outbox(TENANT)
        self.assertEqual(self.submitted, [])
        # Due now: the outbox picks it up and it is delivered.
        row.next_attempt_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)
        self.db.commit()
        self.outcome = SENT
        self.assertEqual(es.process_outbox(TENANT)["dispatched"], 1)
        self._run_submitted()
        row = self._row(log.id)
        self.assertEqual((row.status, row.attempts), ("SENT", 2))
        self.assertIsNone(row.payload)

    def test_permanent_failure_is_final(self):
        self.outcome = BAD_RECIPIENT
        log = self._queue()
        self._run_submitted()
        row = self._row(log.id)
        self.assertEqual((row.status, row.error_code), ("FAILED", "invalid_recipient"))
        self.assertIsNone(row.payload)
        self.assertIsNone(row.next_attempt_at)

    def test_retries_stop_after_max_attempts(self):
        self.outcome = THROTTLED
        log = self._queue()
        row = self._row(log.id)
        row.attempts = es._OUTBOX_MAX_ATTEMPTS - 1
        self.db.commit()
        self._run_submitted()
        row = self._row(log.id)
        self.assertEqual((row.status, row.attempts), ("FAILED", es._OUTBOX_MAX_ATTEMPTS))
        self.assertIsNone(row.payload)

    def test_email_dropped_by_a_restart_is_recovered(self):
        log = self._queue()
        self.submitted.clear()  # the in-memory worker queue died with the process
        row = self._row(log.id)
        row.created_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5)
        self.db.commit()
        self.assertEqual(es.process_outbox(TENANT)["dispatched"], 1)
        self._run_submitted()
        self.assertEqual(self._row(log.id).status, "SENT")
        self.assertEqual(self.delivered[0].subject, "Outbox test")
        self.assertEqual(self.delivered[0].html_body, "<p>Hello</p>")

    def test_fresh_queued_email_is_left_to_its_worker(self):
        self._queue()
        self.submitted.clear()
        self.assertEqual(es.process_outbox(TENANT)["dispatched"], 0)

    def test_stale_claim_is_reported_not_resent(self):
        log = self._queue()
        self.submitted.clear()
        row = self._row(log.id)
        row.status = "SENDING"
        row.claimed_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=20)
        self.db.commit()
        self.assertGreaterEqual(es.process_outbox(TENANT)["unconfirmed"], 1)
        row = self._row(log.id)
        self.assertEqual((row.status, row.error_code), ("FAILED", "delivery_unconfirmed"))
        self.assertIsNone(row.payload)
        self.assertEqual(self.delivered, [], "never resent: it may already have been delivered")

    def test_legacy_queued_row_without_content_is_closed_out(self):
        legacy = models.EmailLog(
            id=uuid.uuid4(), company_id=self.company.id, email_type="OUTBOX_TEST", recipient="x@impacgo.com",
            subject="Old", status="QUEUED", attempts=0,
            created_at=datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2),
        )
        self.db.add(legacy)
        self.db.commit()
        self.assertGreaterEqual(es.process_outbox(TENANT)["not_dispatched"], 1)
        row = self._row(legacy.id)
        self.assertEqual((row.status, row.error_code), ("FAILED", "not_dispatched"))

    def test_deferred_attachment_is_rebuilt_from_its_recipe(self):
        built = []

        def builder(tenant_slug, recipe):
            built.append((tenant_slug, recipe["n"]))
            return [es.Attachment("Rebuilt.pdf", b"%PDF-1.4 test")]

        es.register_attachment_recipe("outbox_test", builder)
        try:
            log = self._queue(attachment_recipe={"kind": "outbox_test", "n": 7}, attachment_label="Rebuilt.pdf")
            self.submitted.clear()  # lost: the in-memory factory is gone too
            row = self._row(log.id)
            row.created_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5)
            self.db.commit()
            es.process_outbox(TENANT)
            self._run_submitted()
        finally:
            es._ATTACHMENT_RECIPES.pop("outbox_test", None)
        self.assertEqual(built, [(TENANT, 7)])
        self.assertEqual([a.name for a in self.delivered[0].attachments], ["Rebuilt.pdf"])
        self.assertEqual(self._row(log.id).status, "SENT")


if __name__ == "__main__":
    unittest.main()
