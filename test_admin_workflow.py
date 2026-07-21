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
        detail = self.client.get(f"/api/admin/questions/{question_id}").get_json()
        self.assertEqual(detail["audience"], "agent")
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

    def test_broker_questions_stay_out_of_public_bank(self) -> None:
        original_total = self.client.get("/api/meta").get_json()["totalQuestions"]
        payload = {
            "audience": "broker",
            "topic": "นายหน้าประกันวินาศภัย",
            "q": "คำถามทดสอบสำหรับคลังนายหน้าที่ต้องไม่แสดงในคลังสาธารณะคือข้อใด?",
            "o": ["คำตอบที่ถูก", "ตัวเลือกสอง", "ตัวเลือกสาม", "ตัวเลือกสี่"],
            "a": "คำตอบที่ถูก",
            "e": "ใช้ตรวจสอบว่าข้อสอบประเภทนายหน้าถูกแยกออกจากหน้าฝึกของตัวแทนเสมอ",
            "difficulty": "medium",
            "examFrequency": "low",
            "sourceTitle": "เอกสารอ้างอิงทดสอบ",
            "sourceUrl": "https://example.com/broker-reference",
            "verifiedAt": "2026-07-21",
        }
        created = self.client.post("/api/admin/questions", json=payload, headers=self.headers)
        self.assertEqual(created.status_code, 201)
        question_id = created.get_json()["id"]
        self.client.post(f"/api/admin/questions/{question_id}/submit", headers=self.headers)
        published = self.client.post(f"/api/admin/questions/{question_id}/publish", headers=self.headers)
        self.assertEqual(published.status_code, 200)
        self.assertEqual(self.client.get("/api/meta").get_json()["totalQuestions"], original_total)
        broker_rows = self.client.get("/api/admin/questions?audience=broker").get_json()["items"]
        self.assertTrue(any(row["id"] == question_id for row in broker_rows))
        public_rows = self.client.get("/api/questions").get_json()
        self.assertFalse(any(row["id"] == question_id for row in public_rows))

        broker_topics = self.client.get("/api/admin/topics?audience=broker&status=published").get_json()
        self.assertTrue(any(item["topic"] == payload["topic"] and item["questionCount"] >= 1 for item in broker_topics))

    def test_admin_question_pagination(self) -> None:
        first_page = self.client.get("/api/admin/questions?page=1&perPage=10")
        self.assertEqual(first_page.status_code, 200)
        data = first_page.get_json()
        self.assertLessEqual(len(data["items"]), 10)
        self.assertEqual(data["pagination"]["page"], 1)
        self.assertEqual(data["pagination"]["perPage"], 10)
        self.assertGreater(data["pagination"]["totalItems"], 0)

        last_page = self.client.get("/api/admin/questions?page=999999&perPage=10").get_json()
        self.assertEqual(last_page["pagination"]["page"], last_page["pagination"]["totalPages"])
        self.assertFalse(last_page["pagination"]["hasNext"])
        self.assertEqual(self.client.get("/api/admin/questions?perPage=11").status_code, 400)

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
