import os
import tempfile
import unittest
from pathlib import Path


class MemberWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        os.environ["DATABASE_PATH"] = str(Path(cls.temp_dir.name) / "members.sqlite3")
        os.environ["COOKIE_SECURE"] = "0"
        import server

        cls.server = server
        server.DATABASE_PATH = Path(cls.temp_dir.name) / "members.sqlite3"
        server.app.config["SESSION_COOKIE_SECURE"] = False
        with server.app.app_context():
            server.initialize_database()

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

    def test_register_login_and_personal_attempt(self):
        client = self.server.app.test_client()
        registered = client.post("/api/members/register", json={
            "displayName": "สมาชิกทดสอบ",
            "username": "member.test",
            "password": "strongpass123",
        })
        self.assertEqual(registered.status_code, 201)
        token = registered.get_json()["token"]

        saved = client.post("/api/attempts", json={
            "token": token,
            "score": 17,
            "totalQuestions": 20,
            "durationSeconds": 180,
            "selectedTopic": "ทุกหมวด",
            "topicScores": {},
        })
        self.assertEqual(saved.status_code, 201)
        profile = client.get("/api/members/me")
        self.assertEqual(profile.status_code, 200)
        self.assertEqual(profile.get_json()["summary"]["attempts"], 1)
        history = client.get("/api/members/me/attempts")
        self.assertEqual(history.status_code, 200)
        history_data = history.get_json()
        self.assertEqual(history_data["pagination"]["totalItems"], 1)
        self.assertEqual(history_data["attempts"][0]["score"], 17)
        self.assertEqual(history_data["attempts"][0]["exam_mode"], "practice")
        self.assertTrue(history_data["attempts"][0]["result"]["passed"])
        self.assertEqual(history_data["attempts"][0]["result"]["requiredPercentage"], 60)
        self.assertEqual(client.get("/api/members/me/attempts?perPage=11").status_code, 400)

        updated = client.put("/api/members/me", json={
            "displayName": "สมาชิกแก้ไขแล้ว",
            "currentPassword": "strongpass123",
            "newPassword": "newstrongpass456",
        })
        self.assertEqual(updated.status_code, 200)
        self.assertTrue(updated.get_json()["passwordChanged"])

        self.assertEqual(client.post("/api/members/logout").status_code, 200)
        self.assertEqual(client.get("/api/members/me").status_code, 401)
        self.assertEqual(client.get("/api/members/me/attempts").status_code, 401)
        login = client.post("/api/members/login", json={"username": "member.test", "password": "newstrongpass456"})
        self.assertEqual(login.status_code, 200)
        login_data = login.get_json()
        self.assertEqual(login_data["user"]["displayName"], "สมาชิกแก้ไขแล้ว")
        self.assertEqual(login_data["summary"]["attempts"], 1)
        self.assertEqual(login_data["summary"]["answered"], 20)
        self.assertEqual(login_data["summary"]["best_score"], 85)


if __name__ == "__main__":
    unittest.main()
