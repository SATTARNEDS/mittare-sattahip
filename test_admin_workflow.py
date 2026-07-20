"""Regression test สำหรับ workflow หลังบ้านด้วยฐานข้อมูลชั่วคราว"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

TEMP_DIRECTORY = tempfile.TemporaryDirectory()
os.environ["DATABASE_PATH"] = str(Path(TEMP_DIRECTORY.name) / "test.sqlite3")
os.environ["ADMIN_USERNAME"] = "testadmin"
os.environ["ADMIN_PASSWORD"] = "Strong-Test-Password-123"
os.environ["COOKIE_SECURE"] = "0"

import server  # noqa: E402


class AdminWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = server.app.test_client()
        login = self.client.post(
            "/api/admin/login", json={"username": "testadmin", "password": "Strong-Test-Password-123"}
        )
        self.assertEqual(login.status_code, 200)
        self.csrf = login.get_json()["csrfToken"]
        self.headers = {"X-CSRF-Token": self.csrf}

    def test_complete_question_workflow(self) -> None:
        original_total = self.client.get("/api/meta").get_json()["totalQuestions"]
        payload = {
            "topic": "หมวดทดสอบ",
            "q": "คำถามสำหรับทดสอบขั้นตอนการอนุมัติข้อสอบคือข้อใด?",
            "o": ["ตัวเลือกที่ถูก", "ตัวเลือกสอง", "ตัวเลือกสาม", "ตัวเลือกสี่"],
            "a": "ตัวเลือกที่ถูก",
            "e": "คำอธิบายนี้ใช้ตรวจว่าข้อสอบผ่าน workflow ได้ครบถ้วน",
            "difficulty": "medium",
            "examFrequency": "high",
            "sourceTitle": "เอกสารอ้างอิงสำหรับการทดสอบ",
            "sourceUrl": "https://example.com/reference",
            "verifiedAt": "2026-07-20",
        }
        created = self.client.post("/api/admin/questions", json=payload, headers=self.headers)
        self.assertEqual(created.status_code, 201)
        question_id = created.get_json()["id"]
        self.assertEqual(self.client.get("/api/meta").get_json()["totalQuestions"], original_total)

        submitted = self.client.post(f"/api/admin/questions/{question_id}/submit", headers=self.headers)
        self.assertEqual(submitted.get_json()["status"], "pending")
        published = self.client.post(f"/api/admin/questions/{question_id}/publish", headers=self.headers)
        self.assertEqual(published.get_json()["status"], "published")
        self.assertEqual(self.client.get("/api/meta").get_json()["totalQuestions"], original_total + 1)

        paused = self.client.post(f"/api/admin/questions/{question_id}/pause", headers=self.headers)
        self.assertEqual(paused.get_json()["status"], "paused")
        self.assertEqual(self.client.get("/api/meta").get_json()["totalQuestions"], original_total)
        restored = self.client.post(f"/api/admin/questions/{question_id}/restore", headers=self.headers)
        self.assertEqual(restored.get_json()["status"], "published")

        versions = self.client.get(f"/api/admin/questions/{question_id}/versions").get_json()
        self.assertEqual([version["action"] for version in versions], ["restored", "paused", "published", "submitted", "created"])

    def test_csrf_is_required(self) -> None:
        response = self.client.post("/api/admin/questions/1/pause")
        self.assertEqual(response.status_code, 403)

    def test_head_cannot_publish(self) -> None:
        created = self.client.post(
            "/api/admin/users",
            headers=self.headers,
            json={"username": "teamhead", "displayName": "หัวหน้าทีมทดสอบ", "password": "Head-Password-12345", "role": "head"},
        )
        self.assertIn(created.status_code, {201, 409})
        head_client = server.app.test_client()
        login = head_client.post("/api/admin/login", json={"username": "teamhead", "password": "Head-Password-12345"})
        self.assertEqual(login.status_code, 200)
        response = head_client.post("/api/admin/questions/1/publish", headers={"X-CSRF-Token": login.get_json()["csrfToken"]})
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
