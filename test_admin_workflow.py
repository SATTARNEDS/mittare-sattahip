"""Regression test สำหรับ workflow หลังบ้านด้วยฐานข้อมูลชั่วคราว"""

from __future__ import annotations

import os
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_admin_can_review_member_attempts_by_mode(self) -> None:
        member_client = server.app.test_client()
        username = f"learner{int(__import__('time').time_ns())}"
        registered = member_client.post("/api/members/register", json={
            "displayName": f"ผู้เรียน {username[-6:]}",
            "username": username,
            "password": "Strong-Member-123",
        })
        self.assertEqual(registered.status_code, 201)
        saved = member_client.post("/api/attempts", json={
            "token": registered.get_json()["token"],
            "score": 78,
            "totalQuestions": 100,
            "durationSeconds": 2400,
            "selectedTopic": "จำลองสอบจริง",
            "examMode": "simulation",
            "topicScores": {server.ETHICS_TOPIC: {"correct": 16, "total": 20}},
        })
        self.assertEqual(saved.status_code, 201)

        members = self.client.get(f"/api/admin/members?search={username}")
        self.assertEqual(members.status_code, 200)
        member = members.get_json()[0]
        self.assertEqual(member["attempts"], 1)
        self.assertEqual(member["simulation_attempts"], 1)

        history = self.client.get(f"/api/admin/members/{member['id']}/attempts")
        self.assertEqual(history.status_code, 200)
        history_data = history.get_json()
        self.assertEqual(history_data["pagination"]["totalItems"], 1)
        self.assertFalse(history_data["pagination"]["hasNext"])
        attempt = history_data["attempts"][0]
        self.assertEqual(attempt["exam_mode"], "simulation")
        self.assertEqual(attempt["topicScores"][server.ETHICS_TOPIC]["correct"], 16)
        self.assertTrue(attempt["result"]["passed"])
        self.assertEqual(attempt["result"]["ethicsScore"], 16)
        self.assertEqual(attempt["result"]["otherScore"], 62)
        self.assertEqual(self.client.get(f"/api/admin/members/{member['id']}/attempts?perPage=11").status_code, 400)

    def test_pdf_preview_and_import_as_drafts(self) -> None:
        self.assertEqual(server.PDF_IMPORT_MAX_BYTES, 25 * 1024 * 1024)
        self.assertEqual(server.PDF_IMPORT_MAX_PAGES, 300)
        self.assertEqual(server.PDF_IMPORT_MAX_QUESTIONS, 500)
        extracted_text = """ข้อ 1. การประกันภัยมีประโยชน์สำคัญอย่างไร
ก. ช่วยกระจายความเสี่ยง
ข. ทำให้ไม่เกิดอุบัติเหตุ
ค. ทำให้ทรัพย์สินมีราคาเพิ่ม
ง. ยกเลิกกฎหมายทั้งหมด
เฉลย: ก
คำอธิบาย: การประกันภัยช่วยเฉลี่ยและกระจายความเสียหายระหว่างสมาชิก

ข้อ 2. ผู้เอาประกันภัยควรปฏิบัติอย่างไร
ก. ปกปิดข้อเท็จจริง
ข. เปิดเผยข้อความจริงที่สำคัญ
ค. ไม่อ่านกรมธรรม์
ง. แจ้งข้อมูลเท็จ
เฉลย: ข
คำอธิบาย: ผู้เอาประกันภัยต้องเปิดเผยข้อความจริงซึ่งเป็นสาระสำคัญ
"""

        class FakePage:
            def extract_text(self):
                return extracted_text

        class FakeReader:
            is_encrypted = False
            pages = [FakePage()]

        with patch.object(server, "PdfReader", return_value=FakeReader()):
            preview = self.client.post(
                "/api/admin/pdf-import/preview",
                data={"pdf": (io.BytesIO(b"%PDF-1.7 test"), "test-questions.pdf")},
                headers=self.headers,
                content_type="multipart/form-data",
            )
        self.assertEqual(preview.status_code, 200)
        preview_data = preview.get_json()
        self.assertEqual(len(preview_data["questions"]), 2)
        self.assertTrue(all(question["ready"] for question in preview_data["questions"]))

        questions = []
        for question in preview_data["questions"]:
            questions.append({**question, "topic": "หมวดนำเข้า PDF", "audience": "agent",
                              "difficulty": "medium", "examFrequency": "medium"})
        imported = self.client.post(
            "/api/admin/pdf-import/commit",
            json={"sourceTitle": preview_data["filename"], "questions": questions},
            headers=self.headers,
        )
        self.assertEqual(imported.status_code, 201)
        result = imported.get_json()
        self.assertEqual(len(result["imported"]), 2)
        for item in result["imported"]:
            detail = self.client.get(f"/api/admin/questions/{item['id']}").get_json()
            self.assertEqual(detail["status"], "draft")
            self.assertFalse(detail["isActive"])

    def test_first_200_explanations_use_official_sources(self) -> None:
        with server.app.app_context():
            rows = server.get_database().execute(
                """SELECT id, explanation, explanation_source_url, explanation_review_status
                FROM questions WHERE topic IN (?, ?) ORDER BY id""",
                ("พระราชบัญญัติประกันวินาศภัย", "จรรยาบรรณนายหน้าประกันวินาศภัย"),
            ).fetchall()
        self.assertEqual(len(rows), 200)
        self.assertTrue(all(not row["explanation"].startswith("ตามเฉลย") for row in rows))
        self.assertTrue(all(row["explanation_source_url"].startswith("https://") for row in rows))
        self.assertTrue(all(row["explanation_review_status"] != "unreviewed" for row in rows))

    def test_environment_can_reset_existing_admin_password_and_unlock_login(self) -> None:
        new_password = "Reset-Test-Password-456"
        database = server.sqlite3.connect(server.DATABASE_PATH)
        database.execute(
            "INSERT OR REPLACE INTO login_throttle(throttle_key, failure_count, blocked_until, updated_at) VALUES (?, ?, ?, ?)",
            ("127.0.0.1|testadmin", 5, 9999999999, server.utc_now()),
        )
        database.commit()
        database.close()
        os.environ["RESET_ADMIN_PASSWORD"] = "1"
        os.environ["ADMIN_PASSWORD"] = new_password
        try:
            with server.app.app_context():
                server.initialize_database()
            reset_client = server.app.test_client()
            login = reset_client.post(
                "/api/admin/login", json={"username": "testadmin", "password": new_password}
            )
            self.assertEqual(login.status_code, 200)
            os.environ["RESET_ADMIN_PASSWORD"] = "true"
            os.environ["ADMIN_USERNAME"] = "recoveryadmin"
            with server.app.app_context():
                server.initialize_database()
            recovery_login = server.app.test_client().post(
                "/api/admin/login", json={"username": "recoveryadmin", "password": new_password}
            )
            self.assertEqual(recovery_login.status_code, 200)
        finally:
            os.environ["RESET_ADMIN_PASSWORD"] = "0"
            os.environ["ADMIN_USERNAME"] = "testadmin"
            os.environ["ADMIN_PASSWORD"] = "Strong-Test-Password-123"
            with server.app.app_context():
                os.environ["RESET_ADMIN_PASSWORD"] = "1"
                server.initialize_database()
                server.get_database().execute("UPDATE admin_users SET is_active=0 WHERE username='recoveryadmin'")
                server.get_database().commit()
                os.environ["RESET_ADMIN_PASSWORD"] = "0"

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
