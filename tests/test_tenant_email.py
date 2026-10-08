"""Per-tenant email (public.tenant_email_settings): encryption, validation,
tenant isolation, providers, no secret leakage. No database or network needed
(everything external is mocked).

    cd backend && venv\\Scripts\\python -m unittest tests.test_tenant_email -v
"""

from __future__ import annotations

import datetime
import json
import smtplib
import types
import unittest
import uuid
from unittest import mock

from cryptography.fernet import Fernet
from fastapi import HTTPException
from pydantic import ValidationError

from app import database, email_service, graph_mail, tenant_email
from app.config import settings
from app.routers import email_settings as router
from app.tenant_email import admin, encryption, store
from app.tenant_email.providers import EmailSendError, ProviderConfig, get_provider
from app.tenant_email.providers import sendgrid as sendgrid_provider
from app.tenant_email.providers import ses as ses_provider

KEY = Fernet.generate_key().decode()
SECRET_A, SECRET_B = "smtp-pass-of-tenant-A!", "graph-secret-of-tenant-B!"
GUID = "11111111-2222-3333-4444-555555555555"


def _row(**kw):
    base = dict(
        id=uuid.uuid4(), tenant_id=uuid.uuid4(), provider="smtp", smtp_host="smtp.office365.com", smtp_port=587,
        smtp_username="hr@a.com", smtp_password_encrypted=None, smtp_use_tls=True, smtp_use_ssl=False,
        microsoft_tenant_id=None, microsoft_client_id=None, microsoft_client_secret_encrypted=None,
        sendgrid_api_key_encrypted=None, ses_region=None, ses_access_key_id=None, ses_secret_access_key_encrypted=None,
        from_email="hr@a.com", from_name="A HR", reply_to=None, is_enabled=True,
        created_at=datetime.datetime.now(datetime.timezone.utc), updated_at=datetime.datetime.now(datetime.timezone.utc),
        updated_by=None,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


class _Keyed(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(settings, "tenant_email_encryption_key", KEY)
        p.start()
        self.addCleanup(p.stop)
        store.invalidate()
        self.addCleanup(store.invalidate)


class EncryptionTests(_Keyed):
    def test_roundtrip_and_not_plaintext(self):
        token = encryption.encrypt_secret(SECRET_A)
        self.assertNotIn(SECRET_A, token)
        self.assertTrue(token.startswith("enc:v1:"))
        self.assertEqual(encryption.decrypt_secret(token), SECRET_A)

    def test_each_encryption_differs(self):
        self.assertNotEqual(encryption.encrypt_secret("x1"), encryption.encrypt_secret("x1"))

    def test_missing_key_fails_closed(self):
        with mock.patch.object(settings, "tenant_email_encryption_key", ""):
            with self.assertRaises(encryption.EncryptionConfigError):
                encryption.encrypt_secret("secret")
            self.assertFalse(encryption.is_configured())

    def test_invalid_key_fails_closed(self):
        with mock.patch.object(settings, "tenant_email_encryption_key", "not-a-fernet-key"):
            with self.assertRaises(encryption.EncryptionConfigError):
                encryption.encrypt_secret("secret")

    def test_wrong_key_or_tampering_cannot_decrypt_and_error_hides_value(self):
        token = encryption.encrypt_secret(SECRET_A)
        with mock.patch.object(settings, "tenant_email_encryption_key", Fernet.generate_key().decode()):
            with self.assertRaises(encryption.EncryptionConfigError) as cm:
                encryption.decrypt_secret(token)
        self.assertNotIn(SECRET_A, str(cm.exception))
        with self.assertRaises(encryption.EncryptionConfigError):
            encryption.decrypt_secret(token[:-4] + "AAAA")
        with self.assertRaises(encryption.EncryptionConfigError):
            encryption.decrypt_secret("plaintext-password")  # never accepts unencrypted values

    def test_key_rotation(self):
        old = encryption.encrypt_secret("rotating")
        new_key = Fernet.generate_key().decode()
        with mock.patch.object(settings, "tenant_email_encryption_key", f"{new_key},{KEY}"):
            self.assertEqual(encryption.decrypt_secret(old), "rotating")
            self.assertNotEqual(encryption.encrypt_secret("rotating"), old)


class ValidationTests(_Keyed):
    def _smtp(self, **kw):
        base = dict(provider="smtp", from_email="hr@a.com", smtp_host="smtp.office365.com", smtp_port=587,
                    smtp_username="hr@a.com", smtp_password=SECRET_A)
        base.update(kw)
        return admin.EmailSettingsIn(**base)

    def test_valid_smtp(self):
        admin.validate(self._smtp(), None)

    def test_rejects_bad_values(self):
        for kw in ({"from_email": "nope"}, {"reply_to": "bad"}, {"smtp_port": 0}, {"smtp_port": 70000},
                   {"smtp_host": "bad host!"}, {"smtp_use_tls": True, "smtp_use_ssl": True},
                   {"smtp_use_tls": False, "smtp_use_ssl": False}, {"smtp_password": None}):
            with self.subTest(kw=kw), self.assertRaises(admin.EmailSettingsError):
                admin.validate(self._smtp(**kw), None)

    def test_blank_secret_allowed_only_when_one_is_stored_for_same_provider(self):
        stored = _row(smtp_password_encrypted="enc:v1:x")
        admin.validate(self._smtp(smtp_password=None), stored)
        with self.assertRaises(admin.EmailSettingsError):
            admin.validate(self._smtp(smtp_password=None), _row(provider="sendgrid", smtp_password_encrypted="enc:v1:x"))

    def test_graph_requires_guids_and_secret(self):
        ok = dict(provider="microsoft_graph", from_email="hr@b.com", microsoft_tenant_id=GUID,
                  microsoft_client_id=GUID, microsoft_client_secret=SECRET_B)
        admin.validate(admin.EmailSettingsIn(**ok), None)
        for bad in ({"microsoft_tenant_id": "contoso"}, {"microsoft_client_id": None}, {"microsoft_client_secret": None}):
            with self.subTest(bad=bad), self.assertRaises(admin.EmailSettingsError):
                admin.validate(admin.EmailSettingsIn(**{**ok, **bad}), None)

    def test_sendgrid_and_ses_required_fields(self):
        with self.assertRaises(admin.EmailSettingsError):
            admin.validate(admin.EmailSettingsIn(provider="sendgrid", from_email="a@b.com"), None)
        ses = dict(provider="ses", from_email="a@b.com", ses_region="us-east-1", ses_access_key_id="AKIA",
                   ses_secret_access_key="s")
        admin.validate(admin.EmailSettingsIn(**ses), None)
        with self.assertRaises(admin.EmailSettingsError):
            admin.validate(admin.EmailSettingsIn(**{**ses, "ses_region": "mars"}), None)

    def test_tenant_id_in_body_is_rejected(self):
        with self.assertRaises(ValidationError):
            self._smtp(tenant_id=str(uuid.uuid4()))
        with self.assertRaises(ValidationError):
            admin.EmailSettingsIn(provider="carrier_pigeon", from_email="a@b.com")


class SaveAndOutputTests(_Keyed):
    def _db(self, existing=None):
        db = mock.MagicMock()
        return db, mock.patch.object(store, "get_row", return_value=existing)

    def test_save_encrypts_and_output_has_no_secret(self):
        db, p = self._db()
        payload = admin.EmailSettingsIn(provider="smtp", from_email="hr@a.com", smtp_host="smtp.office365.com",
                                        smtp_port=587, smtp_username="hr@a.com", smtp_password=SECRET_A)
        with p:
            row = admin.save(db, uuid.uuid4(), payload, updated_by=None)
        self.assertNotIn(SECRET_A, row.smtp_password_encrypted)
        self.assertEqual(encryption.decrypt_secret(row.smtp_password_encrypted), SECRET_A)
        out = admin.to_out(row)
        dumped = json.dumps(out.model_dump(mode="json"))
        self.assertNotIn(SECRET_A, dumped)
        self.assertNotIn(row.smtp_password_encrypted, dumped)
        self.assertTrue(out.has_password)
        self.assertNotIn("smtp_password", out.model_dump())
        self.assertNotIn(SECRET_A, repr(payload))

    def test_blank_secret_keeps_stored_ciphertext(self):
        existing = _row(smtp_password_encrypted=encryption.encrypt_secret(SECRET_A))
        before = existing.smtp_password_encrypted
        db, p = self._db(existing)
        payload = admin.EmailSettingsIn(provider="smtp", from_email="new@a.com", smtp_host="smtp.office365.com",
                                        smtp_port=587, smtp_username="hr@a.com", smtp_password="  ")
        with p:
            row = admin.save(db, existing.tenant_id, payload, updated_by=None)
        self.assertEqual(row.smtp_password_encrypted, before)
        self.assertEqual(row.from_email, "new@a.com")

    def test_switching_provider_clears_old_secret(self):
        existing = _row(smtp_password_encrypted=encryption.encrypt_secret(SECRET_A))
        db, p = self._db(existing)
        payload = admin.EmailSettingsIn(provider="sendgrid", from_email="hr@a.com", sendgrid_api_key="SG.key")
        with p:
            row = admin.save(db, existing.tenant_id, payload, updated_by=None)
        self.assertIsNone(row.smtp_password_encrypted)
        self.assertIsNone(row.smtp_host)
        self.assertEqual(encryption.decrypt_secret(row.sendgrid_api_key_encrypted), "SG.key")

    def test_save_without_key_refuses_and_stores_nothing(self):
        db, p = self._db()
        payload = admin.EmailSettingsIn(provider="smtp", from_email="hr@a.com", smtp_host="smtp.office365.com",
                                        smtp_port=587, smtp_username="hr@a.com", smtp_password=SECRET_A)
        with p, mock.patch.object(settings, "tenant_email_encryption_key", ""):
            with self.assertRaises(encryption.EncryptionConfigError):
                admin.save(db, uuid.uuid4(), payload, updated_by=None)
        db.add.assert_not_called()
        db.commit.assert_not_called()


class TenantIsolationTests(_Keyed):
    """Tenant A's mail is only ever sent with tenant A's configuration."""

    def setUp(self):
        super().setUp()
        self.id_a, self.id_b = uuid.uuid4(), uuid.uuid4()
        self.rows = {
            self.id_a: _row(tenant_id=self.id_a, from_email="hr@companya.com", smtp_username="hr@companya.com",
                            smtp_password_encrypted=encryption.encrypt_secret(SECRET_A)),
            self.id_b: _row(tenant_id=self.id_b, provider="microsoft_graph", from_email="hr@companyb.com",
                            microsoft_tenant_id=GUID, microsoft_client_id=GUID,
                            microsoft_client_secret_encrypted=encryption.encrypt_secret(SECRET_B)),
        }
        ids = {"tenant_a": self.id_a, "tenant_b": self.id_b}
        for patcher in (
            mock.patch.object(store, "_public_session", return_value=mock.MagicMock()),
            mock.patch.object(store, "tenant_id_for_slug", side_effect=lambda db, slug: ids.get(slug)),
            mock.patch.object(store, "get_row", side_effect=lambda db, tid: self.rows.get(tid)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_each_tenant_gets_its_own_config(self):
        a, b = store.load_config("tenant_a"), store.load_config("tenant_b")
        self.assertEqual((a.provider, a.from_email, a.smtp_password), ("smtp", "hr@companya.com", SECRET_A))
        self.assertEqual((b.provider, b.from_email, b.microsoft_client_secret), ("microsoft_graph", "hr@companyb.com", SECRET_B))
        self.assertNotIn(SECRET_B, repr(a) + repr(b))  # secrets never reach repr/log formatting
        self.assertNotIn(SECRET_A, repr(a) + repr(b))

    def test_unknown_or_missing_tenant_gets_nothing_not_someone_elses(self):
        self.assertIsNone(store.load_config("tenant_c"))
        self.assertIsNone(store.load_config(None))

    def test_disabled_tenant_does_not_send_and_does_not_fall_back(self):
        self.rows[self.id_a].is_enabled = False
        with mock.patch.object(settings, "email_global_fallback_tenants", "*"):
            self.assertIsNone(store.load_config("tenant_a"))

    def test_no_silent_global_fallback(self):
        with mock.patch.object(settings, "email_global_fallback_tenants", ""), \
             mock.patch.object(settings, "smtp_host", "smtp.impacgo.example"), \
             mock.patch.object(settings, "email_from_address", "info@impacgo.com"):
            self.assertIsNone(store.load_config("tenant_c"))

    def test_fallback_only_for_listed_tenants(self):
        with mock.patch.object(settings, "email_global_fallback_tenants", "tenant_c"), \
             mock.patch.object(settings, "smtp_host", "smtp.impacgo.example"), \
             mock.patch.object(settings, "smtp_password", "global-pass"), \
             mock.patch.object(settings, "email_from_address", "info@impacgo.com"):
            c = store.load_config("tenant_c")
            self.assertEqual((c.source, c.from_email), ("global_fallback", "info@impacgo.com"))
            store.invalidate()
            self.assertIsNone(store.load_config("tenant_d"))
            # a tenant WITH its own row never uses the global one
            self.assertEqual(store.load_config("tenant_a").source, "tenant")

    def test_undecryptable_row_is_unusable_not_plaintext_or_fallback(self):
        self.rows[self.id_a].smtp_password_encrypted = "enc:v1:garbage"
        with mock.patch.object(settings, "email_global_fallback_tenants", "*"):
            self.assertIsNone(store.load_config("tenant_a"))

    def test_deliver_uses_the_jobs_tenant_credentials_only(self):
        seen = []

        class Spy:
            def __init__(self, config):
                seen.append(config)

            def send(self, message):
                return 250

        with mock.patch.dict("app.tenant_email.providers.REGISTRY", {"smtp": Spy, "microsoft_graph": Spy}):
            for slug in ("tenant_a", "tenant_b", "tenant_a"):
                job = email_service.EmailJob(email_type="TEST", to=["x@example.com"], subject="s", html_body="b",
                                             tenant_slug=slug)
                self.assertEqual(email_service._deliver(job)[0], "SENT")
        self.assertEqual([c.from_email for c in seen], ["hr@companya.com", "hr@companyb.com", "hr@companya.com"])
        self.assertEqual(seen[0].smtp_password, SECRET_A)
        self.assertEqual(seen[1].microsoft_client_secret, SECRET_B)
        self.assertEqual(seen[1].smtp_password, "")  # B never sees A's SMTP password

    def test_unconfigured_tenant_is_skipped_not_sent(self):
        job = email_service.EmailJob(email_type="TEST", to=["x@example.com"], subject="s", tenant_slug="tenant_c")
        status, _ps, code, _msg, via = email_service._deliver(job)
        self.assertEqual((status, code, via), ("SKIPPED", "not_configured", None))

    def test_transport_and_sender_follow_the_tenant(self):
        self.assertEqual(email_service.transport("tenant_a"), "smtp")
        self.assertEqual(email_service.transport("tenant_b"), "graph")
        self.assertEqual(email_service.sender_address("tenant_b"), "hr@companyb.com")
        self.assertIsNone(email_service.transport("tenant_c"))

    def test_global_kill_switch(self):
        with mock.patch.object(settings, "email_enabled", False):
            self.assertIsNone(store.load_config("tenant_a"))

    def test_provider_failure_message_has_no_credentials(self):
        class Boom:
            def __init__(self, config):
                self.c = config

            def send(self, message):
                raise RuntimeError(f"login failed for {self.c.smtp_password}")

        with mock.patch.dict("app.tenant_email.providers.REGISTRY", {"smtp": Boom}):
            job = email_service.EmailJob(email_type="TEST", to=["x@example.com"], subject="s", tenant_slug="tenant_a")
            status, _p, _c, message, _v = email_service._deliver(job)
        self.assertEqual(status, "FAILED")
        self.assertNotIn(SECRET_A, str(message))

    def test_dev_allowed_domain_default_is_the_tenants_own_domain(self):
        with mock.patch.object(settings, "app_env", "dev"), mock.patch.object(settings, "email_allowed_domains", ""):
            self.assertEqual(email_service.allowed_domains("tenant_a"), {"companya.com"})
            self.assertEqual(email_service.allowed_domains("tenant_b"), {"companyb.com"})
            self.assertEqual(email_service.allowed_domains("tenant_c"), {"invalid.invalid"})


class RouterTenantResolutionTests(_Keyed):
    def test_tenant_comes_from_the_session_never_the_request(self):
        db, tid = mock.MagicMock(), uuid.uuid4()
        with mock.patch.object(database, "get_session_tenant_slug", return_value="tenant_a"), \
             mock.patch.object(store, "tenant_id_for_slug", return_value=tid) as lookup:
            self.assertEqual(router._own_tenant_id(db), tid)
        lookup.assert_called_once_with(db, "tenant_a")

    def test_no_session_tenant_is_404(self):
        with mock.patch.object(database, "get_session_tenant_slug", return_value=None):
            with self.assertRaises(HTTPException) as cm:
                router._own_tenant_id(mock.MagicMock())
        self.assertEqual(cm.exception.status_code, 404)

    def test_get_returns_only_the_callers_row_without_secrets(self):
        tid = uuid.uuid4()
        row = _row(tenant_id=tid, smtp_password_encrypted=encryption.encrypt_secret(SECRET_A))
        with mock.patch.object(database, "get_session_tenant_slug", return_value="tenant_a"), \
             mock.patch.object(store, "tenant_id_for_slug", return_value=tid), \
             mock.patch.object(store, "get_row", return_value=row) as get_row:
            out = router.get_email_settings(db=mock.MagicMock(), current_user=object())
        get_row.assert_called_once()
        self.assertEqual(get_row.call_args.args[1], tid)
        self.assertTrue(out.has_password)
        self.assertNotIn(SECRET_A, json.dumps(out.model_dump(mode="json")))

    def test_post_conflicts_when_settings_exist(self):
        tid = uuid.uuid4()
        payload = admin.EmailSettingsIn(provider="sendgrid", from_email="a@b.com", sendgrid_api_key="k")
        with mock.patch.object(database, "get_session_tenant_slug", return_value="tenant_a"), \
             mock.patch.object(store, "tenant_id_for_slug", return_value=tid), \
             mock.patch.object(store, "get_row", return_value=_row()):
            with self.assertRaises(HTTPException) as cm:
                router.create_email_settings(payload, db=mock.MagicMock(), current_user=object())
        self.assertEqual(cm.exception.status_code, 409)

    def test_save_without_key_is_503_and_rolls_back(self):
        payload = admin.EmailSettingsIn(provider="sendgrid", from_email="a@b.com", sendgrid_api_key="k")
        db = mock.MagicMock()
        with mock.patch.object(store, "get_row", return_value=None), mock.patch.object(settings, "tenant_email_encryption_key", ""):
            with self.assertRaises(HTTPException) as cm:
                router._save(db, uuid.uuid4(), payload, None)
        self.assertEqual(cm.exception.status_code, 503)
        db.rollback.assert_called()

    def test_routes_are_registered_with_expected_guards(self):
        from app.main import app
        paths = {(m, r.path) for r in app.routes if hasattr(r, "methods") for m in r.methods}
        for m, p in (("GET", "/api/admin/email-settings"), ("POST", "/api/admin/email-settings"),
                     ("PUT", "/api/admin/email-settings"), ("DELETE", "/api/admin/email-settings"),
                     ("POST", "/api/admin/email-settings/test"),
                     ("PUT", "/api/super-admin/tenants/{tenant_id}/email-settings")):
            self.assertIn((m, p), paths)
        # unauthenticated access is refused on every one of them
        from fastapi.testclient import TestClient
        client = TestClient(app)
        for method, path in (("get", "/api/admin/email-settings"), ("put", "/api/admin/email-settings"),
                             ("post", "/api/admin/email-settings/test"),
                             ("get", f"/api/super-admin/tenants/{uuid.uuid4()}/email-settings")):
            self.assertIn(getattr(client, method)(path).status_code, (401, 403, 422), path)


class ProviderTests(_Keyed):
    def _msg(self):
        return graph_mail.MailMessage(to=["emp@example.com"], subject="Hello", html_body="<b>hi</b>", text_body="hi")

    def test_smtp_logs_in_with_the_tenants_password_over_starttls(self):
        cfg = ProviderConfig(provider="smtp", from_email="hr@a.com", from_name="A HR", smtp_host="smtp.example.com",
                             smtp_port=587, smtp_username="hr@a.com", smtp_password=SECRET_A, reply_to="r@a.com")
        server = mock.MagicMock()
        server.__enter__.return_value = server
        with mock.patch("smtplib.SMTP", return_value=server) as smtp:
            self.assertEqual(get_provider(cfg).send(self._msg()), 250)
        smtp.assert_called_once()
        server.starttls.assert_called_once()
        server.login.assert_called_once_with("hr@a.com", SECRET_A)
        sent = server.send_message.call_args.args[0]
        self.assertIn("hr@a.com", sent["From"])
        self.assertEqual(sent["Reply-To"], "r@a.com")

    def test_smtp_refuses_credentials_over_plaintext(self):
        cfg = ProviderConfig(provider="smtp", from_email="a@b.com", smtp_host="h.example.com", smtp_username="u",
                             smtp_password="p", smtp_use_tls=False, smtp_use_ssl=False)
        with self.assertRaises(EmailSendError) as cm:
            get_provider(cfg).send(self._msg())
        self.assertEqual(cm.exception.code, "insecure_smtp")

    def test_smtp_auth_error_is_classified_without_the_server_reply(self):
        cfg = ProviderConfig(provider="smtp", from_email="a@b.com", smtp_host="h.example.com", smtp_username="u",
                             smtp_password=SECRET_A)
        server = mock.MagicMock()
        server.__enter__.return_value = server
        server.login.side_effect = smtplib.SMTPAuthenticationError(535, f"bad credentials {SECRET_A}".encode())
        with mock.patch("smtplib.SMTP", return_value=server):
            with self.assertRaises(EmailSendError) as cm:
                get_provider(cfg).send(self._msg())
        self.assertEqual(cm.exception.code, "auth_failed")
        self.assertNotIn(SECRET_A, str(cm.exception))

    def test_graph_provider_uses_the_tenants_own_app_and_mailbox(self):
        cfg = ProviderConfig(provider="microsoft_graph", from_email="hr@companyb.com", from_name="B HR",
                             microsoft_tenant_id=GUID, microsoft_client_id="cid", microsoft_client_secret=SECRET_B)
        with mock.patch.object(graph_mail, "send_mail", return_value=202) as send:
            self.assertEqual(get_provider(cfg).send(self._msg()), 202)
        passed = send.call_args.args[1]
        self.assertEqual((passed.tenant_id, passed.client_id, passed.client_secret, passed.from_email),
                         (GUID, "cid", SECRET_B, "hr@companyb.com"))
        self.assertEqual(send.call_args.args[0].from_name, "B HR")
        self.assertNotIn(SECRET_B, repr(passed))

    def test_graph_tokens_are_cached_per_tenant_config(self):
        a = graph_mail.GraphConfig("t1", "c1", "s1", "a@a.com")
        b = graph_mail.GraphConfig("t2", "c2", "s2", "b@b.com")
        tokens = iter([("tok-a", 9e12), ("tok-b", 9e12)])
        with mock.patch.object(graph_mail, "_fetch_token", side_effect=lambda cfg: next(tokens)):
            self.assertEqual(graph_mail.get_access_token(cfg=a), "tok-a")
            self.assertEqual(graph_mail.get_access_token(cfg=b), "tok-b")
            self.assertEqual(graph_mail.get_access_token(cfg=a), "tok-a")  # cached, A's token not B's
        graph_mail.invalidate_token(a)
        graph_mail.invalidate_token(b)

    def test_graph_payload_uses_config_sender(self):
        cfg = graph_mail.GraphConfig("t", "c", "s", "hr@companyb.com", on_behalf="boss@companyb.com", on_behalf_name="Boss")
        payload = graph_mail.build_payload(self._msg(), cfg=cfg)
        self.assertEqual(payload["message"]["from"]["emailAddress"]["address"], "boss@companyb.com")
        self.assertEqual(payload["message"]["sender"]["emailAddress"]["address"], "hr@companyb.com")

    def test_sendgrid_payload(self):
        cfg = ProviderConfig(provider="sendgrid", from_email="hr@a.com", from_name="A HR", sendgrid_api_key="SG.k",
                             reply_to="r@a.com")
        p = sendgrid_provider.build_payload(cfg, self._msg())
        self.assertEqual(p["from"], {"email": "hr@a.com", "name": "A HR"})
        self.assertEqual(p["reply_to"], {"email": "r@a.com"})
        self.assertNotIn("SG.k", json.dumps(p))

    def test_sendgrid_http_errors_are_classified(self):
        import urllib.error
        cfg = ProviderConfig(provider="sendgrid", from_email="hr@a.com", sendgrid_api_key="SG.secret")
        err = urllib.error.HTTPError("u", 401, "no", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(EmailSendError) as cm:
                get_provider(cfg).send(self._msg())
        self.assertEqual(cm.exception.code, "auth_failed")
        self.assertNotIn("SG.secret", str(cm.exception))

    def test_ses_sigv4_is_deterministic_and_hides_secret(self):
        now = datetime.datetime(2026, 10, 8, 12, 0, 0, tzinfo=datetime.timezone.utc)
        kw = dict(access_key="AKIAEXAMPLE", secret_key="topsecret", region="us-east-1",
                  host="email.us-east-1.amazonaws.com", path="/v2/email/outbound-emails", body=b"{}", now=now)
        h1, h2 = ses_provider.sigv4_headers(**kw), ses_provider.sigv4_headers(**kw)
        self.assertEqual(h1, h2)
        self.assertIn("Credential=AKIAEXAMPLE/20261008/us-east-1/ses/aws4_request", h1["Authorization"])
        self.assertNotIn("topsecret", json.dumps(h1))
        self.assertNotEqual(h1, ses_provider.sigv4_headers(**{**kw, "body": b"{\"x\":1}"}))

    def test_unsupported_provider(self):
        with self.assertRaises(EmailSendError):
            get_provider(ProviderConfig(provider="carrier_pigeon"))


class AsyncServiceTests(_Keyed):
    def test_async_send_email_resolves_tenant_and_sends_once(self):
        import asyncio
        tid = uuid.uuid4()
        sent = []
        with mock.patch.object(database, "SessionLocal", return_value=mock.MagicMock()), \
             mock.patch.object(store, "slug_for_tenant_id", return_value="tenant_a"), \
             mock.patch("app.tenant_email.service.send", side_effect=lambda slug, msg, **k: sent.append((slug, msg)) or (250, None)):
            status = asyncio.run(tenant_email.email_service.send_email(tid, "emp@example.com", "S", "<p>b</p>"))
        self.assertEqual(status, 250)
        self.assertEqual(sent[0][0], "tenant_a")
        self.assertEqual(sent[0][1].to, ["emp@example.com"])


if __name__ == "__main__":
    unittest.main()
