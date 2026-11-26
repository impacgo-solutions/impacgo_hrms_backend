"""Uploaded files are served at /api/media/<path> as well as /media/<path>:
production's reverse proxy forwards only /api to the backend (/media came
back as the web app's index.html, so profile photos and the company logo
never loaded). Same auth, signed-token, access and header rules on both.

    cd backend && venv/Scripts/python -m unittest tests.test_media_api_path -v
"""

from __future__ import annotations

import os
import shutil
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import security  # noqa: E402
from app.config import settings  # noqa: E402
from tests.test_people_visibility import PeopleTestBase  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class MediaApiPathTests(PeopleTestBase):
    def setUp(self):
        super().setUp()
        self.owner = self.users["owner"]
        self.rel = f"employee_photo/{self.owner.employee_id}/qa-{uuid.uuid4().hex[:8]}.png"
        path = Path(settings.uploads_dir).resolve() / self.rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(PNG)
        self.addCleanup(lambda: path.unlink(missing_ok=True))

    def signed(self, prefix: str) -> str:
        t = security.sign_media_path(self.rel, self.tenant, self.owner.id)
        return f"{prefix}{self.rel}?t={t}"

    def test_both_paths_serve_the_same_file(self):
        for prefix in ("/media/", "/api/media/"):
            with self.subTest(prefix=prefix):
                r = self.client.get(self.signed(prefix))
                self.assertEqual(r.status_code, 200, r.text[:200])
                self.assertEqual(r.content, PNG)
                self.assertTrue(r.headers["content-type"].startswith("image/png"))
                # The uploaded-file security headers, not the API's.
                self.assertIn("img-src 'self'", r.headers.get("content-security-policy", ""))
                self.assertEqual(r.headers.get("x-content-type-options"), "nosniff")

    def test_api_path_keeps_auth_and_traversal_checks(self):
        self.assertEqual(self.client.get(f"/api/media/{self.rel}").status_code, 401)
        bad = self.client.get(f"/api/media/{self.rel}?t=not-a-token")
        self.assertEqual(bad.status_code, 401)
        # A token is bound to its own path.
        other = f"employee_photo/{self.owner.employee_id}/other.png"
        t = security.sign_media_path(self.rel, self.tenant, self.owner.id)
        self.assertEqual(self.client.get(f"/api/media/{other}?t={t}").status_code, 401)
        for path in ("/api/media/../app/main.py", "/api/media/..%2F..%2Fapp%2Fmain.py"):
            self.assertIn(self.client.get(path).status_code, (401, 404), path)


if __name__ == "__main__":
    unittest.main()
