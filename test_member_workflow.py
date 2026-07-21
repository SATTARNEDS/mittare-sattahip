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

        self.assertEqual(client.post("/api/members/logout").status_code, 200)
        self.assertEqual(client.get("/api/members/me").status_code, 401)
        login = client.post("/api/members/login", json={"username": "member.test", "password": "strongpass123"})
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.get_json()["user"]["displayName"], "สมาชิกทดสอบ")


if __name__ == "__main__":
    unittest.main()
