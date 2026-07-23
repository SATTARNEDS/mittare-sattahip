import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from werkzeug.datastructures import FileStorage
from werkzeug.exceptions import BadRequest

import mittare_site.app as site_module


class SiteSecurityTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        instance_dir = Path(self.temp_dir.name)
        self.path_patchers = [
            patch.object(site_module, "INSTANCE_DIR", instance_dir),
            patch.object(site_module, "UPLOAD_DIR", instance_dir / "uploads"),
            patch.object(site_module, "PRODUCT_MEDIA_DIR", instance_dir / "product-media"),
            patch.object(site_module, "PRODUCT_MEDIA_CONFIG_PATH", instance_dir / "product_media.json"),
            patch.object(site_module, "DATABASE_PATH", instance_dir / "mittare.sqlite3"),
        ]
        for patcher in self.path_patchers:
            patcher.start()
        self.env = patch.dict(os.environ, {
            "SECRET_KEY": "site-security-test-secret",
            "ADMIN_USERNAME": "secureadmin",
            "ADMIN_PASSWORD": "StrongPassword-123!",
            "COOKIE_SECURE": "1",
            "LINE_CHANNEL_SECRET": "test-line-secret",
        }, clear=False)
        self.env.start()
        self.app = site_module.create_app()
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()

    def tearDown(self):
        self.env.stop()
        for patcher in reversed(self.path_patchers):
            patcher.stop()
        self.temp_dir.cleanup()

    def login(self):
        response = self.client.post("/api/session", json={
            "username": "secureadmin", "password": "StrongPassword-123!",
        })
        self.assertEqual(response.status_code, 200)
        return response.get_json()["csrfToken"]

    def test_cookie_headers_and_csrf_are_enforced(self):
        response = self.client.get("/")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("object-src 'none'", response.headers["Content-Security-Policy"])
        self.assertIn("style-src 'self' https://fonts.googleapis.com", response.headers["Content-Security-Policy"])
        self.assertIn("font-src 'self' https://fonts.gstatic.com", response.headers["Content-Security-Policy"])
        response.close()

        csrf_token = self.login()
        cookie = self.client.get_cookie("mittare_site_session")
        self.assertTrue(cookie.secure)
        self.assertTrue(cookie.http_only)
        self.assertEqual(cookie.same_site, "Lax")
        self.assertEqual(self.client.post("/api/demo/seed").status_code, 403)
        allowed = self.client.post("/api/demo/seed", headers={"X-CSRF-Token": csrf_token})
        self.assertEqual(allowed.status_code, 200)

    def test_login_rate_limit_and_line_signature(self):
        for _ in range(site_module.LOGIN_MAX_FAILURES):
            response = self.client.post("/api/session", json={"username": "secureadmin", "password": "wrong"})
        self.assertEqual(response.status_code, 401)
        blocked = self.client.post("/api/session", json={
            "username": "secureadmin", "password": "StrongPassword-123!",
        })
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(self.client.post("/api/line/webhook", json={"events": []}).status_code, 400)

    def test_upload_content_must_match_extension(self):
        fake_pdf = FileStorage(stream=io.BytesIO(b"not-a-pdf"), filename="fake.pdf")
        with self.assertRaises(BadRequest):
            site_module.validate_uploaded_file(fake_pdf, "pdf")
        real_pdf = FileStorage(stream=io.BytesIO(b"%PDF-1.7\n%%EOF"), filename="real.pdf")
        site_module.validate_uploaded_file(real_pdf, "pdf")

    def test_malformed_data_and_lookup_enumeration_are_limited(self):
        self.assertEqual(
            self.client.post("/api/customer/policies", data="[]", content_type="application/json").status_code,
            400,
        )
        short_phone = self.client.post("/api/customer/policies", json={"phone": "123456"})
        self.assertEqual(short_phone.status_code, 400)
        self.assertEqual(short_phone.headers["Cache-Control"], "no-store, max-age=0")
        for _ in range(site_module.PUBLIC_LOOKUP_LIMIT - 2):
            self.client.post("/api/customer/policies", json={"reference": "UNKNOWN-REFERENCE"})
        blocked = self.client.post("/api/customer/policies", json={"reference": "UNKNOWN-REFERENCE"})
        self.assertEqual(blocked.status_code, 429)

    def test_policy_fields_reject_invalid_dates_numbers_and_status(self):
        csrf_token = self.login()
        base_data = {
            "customerName": "ลูกค้าทดสอบ",
            "customerPhone": "0812345678",
            "insuranceCategory": "รถยนต์",
            "endDate": "2026-12-31",
            "salesStatus": "new",
        }
        invalid_date = self.client.post(
            "/api/policies", data={**base_data, "endDate": "31/12/2026"},
            headers={"X-CSRF-Token": csrf_token},
        )
        self.assertEqual(invalid_date.status_code, 400)
        invalid_number = self.client.post(
            "/api/policies", data={**base_data, "premiumAmount": "NaN"},
            headers={"X-CSRF-Token": csrf_token},
        )
        self.assertEqual(invalid_number.status_code, 400)
        invalid_status = self.client.post(
            "/api/policies", data={**base_data, "salesStatus": "<script>alert(1)</script>"},
            headers={"X-CSRF-Token": csrf_token},
        )
        self.assertEqual(invalid_status.status_code, 400)


if __name__ == "__main__":
    unittest.main()
