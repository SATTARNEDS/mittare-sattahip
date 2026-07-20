from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
from pathlib import Path

from flask import Flask, g, jsonify, request, send_from_directory

from extra_questions import EXTRA_QUESTIONS

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATABASE_PATH = Path(os.environ.get("DATABASE_PATH", DATA_DIR / "exam_coach.sqlite3"))

app = Flask(__name__, static_folder=None)
app.config["JSON_AS_ASCII"] = False


def get_database() -> sqlite3.Connection:
    if "database" not in g:
        DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
        g.database = sqlite3.connect(DATABASE_PATH, timeout=10)
        g.database.row_factory = sqlite3.Row
        g.database.execute("PRAGMA foreign_keys=ON")
        g.database.execute("PRAGMA journal_mode=WAL")
    return g.database


@app.teardown_appcontext
def close_database(_error: BaseException | None) -> None:
    database = g.pop("database", None)
    if database is not None:
        database.close()


@app.after_request
def add_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
        "frame-ancestors 'self'; base-uri 'self'; form-action 'self'"
    )
    return response


def read_legacy_questions() -> list[tuple]:
    """อ่านคลังเดิม 50 ข้อจาก app.js เพื่อนำเข้า DB โดยไม่เก็บข้อมูลซ้ำสองชุด"""
    source = (BASE_DIR / "app.js").read_text(encoding="utf-8")
    pattern = re.compile(
        r'\{id:(\d+),topic:"([^"]+)",q:"([^"]+)",o:\[(.*?)\],a:"([^"]+)",e:"([^"]+)"\}'
    )
    questions = []
    for match in pattern.finditer(source):
        options = re.findall(r'"([^"]*)"', match.group(4))
        if len(options) == 4:
            questions.append((int(match.group(1)), match.group(2), match.group(3), options, match.group(5), match.group(6)))
    return questions


def initialize_database() -> None:
    database = get_database()
    database.executescript(
        """
        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY,
            topic TEXT NOT NULL,
            question_text TEXT NOT NULL,
            options_json TEXT NOT NULL,
            correct_answer TEXT NOT NULL,
            explanation TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_questions_active_topic ON questions(is_active, topic);
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            display_name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            team_name TEXT NOT NULL DEFAULT 'MT4',
            access_token TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            score INTEGER NOT NULL CHECK(score >= 0),
            total_questions INTEGER NOT NULL CHECK(total_questions > 0),
            duration_seconds INTEGER NOT NULL DEFAULT 0 CHECK(duration_seconds >= 0),
            selected_topic TEXT NOT NULL DEFAULT 'ทุกหมวด',
            topic_scores_json TEXT NOT NULL DEFAULT '{}',
            completed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_attempts_user_date ON attempts(user_id, completed_at DESC);
        """
    )
    seed_rows = read_legacy_questions() + EXTRA_QUESTIONS
    database.executemany(
        """
        INSERT INTO questions(id, topic, question_text, options_json, correct_answer, explanation)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            topic=excluded.topic,
            question_text=excluded.question_text,
            options_json=excluded.options_json,
            correct_answer=excluded.correct_answer,
            explanation=excluded.explanation,
            updated_at=CURRENT_TIMESTAMP
        """,
        [(row[0], row[1], row[2], json.dumps(row[3], ensure_ascii=False), row[4], row[5]) for row in seed_rows],
    )
    database.commit()


def serialize_question(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "topic": row["topic"], "q": row["question_text"],
        "o": json.loads(row["options_json"]), "a": row["correct_answer"], "e": row["explanation"],
    }


@app.get("/api/meta")
def api_meta():
    row = get_database().execute(
        "SELECT COUNT(*) AS total, COUNT(DISTINCT topic) AS topics FROM questions WHERE is_active=1"
    ).fetchone()
    return jsonify({"totalQuestions": row["total"], "totalTopics": row["topics"], "examSize": 20})


@app.get("/api/topics")
def api_topics():
    rows = get_database().execute(
        "SELECT topic, COUNT(*) AS question_count FROM questions WHERE is_active=1 GROUP BY topic ORDER BY topic"
    ).fetchall()
    return jsonify([{"topic": row["topic"], "questionCount": row["question_count"]} for row in rows])


@app.get("/healthz")
def health_check():
    get_database().execute("SELECT 1").fetchone()
    return jsonify({"status": "ok"})


@app.get("/api/questions")
def api_questions():
    limit_value = request.args.get("limit", "20")
    try:
        limit = 500 if limit_value == "all" else max(1, min(int(limit_value), 100))
    except ValueError:
        return jsonify({"error": "limit must be a number or 'all'"}), 400
    excluded = [int(value) for value in request.args.get("exclude", "").split(",") if value.isdigit()][:100]
    selected_topic = request.args.get("topic", "").strip()
    database = get_database()
    filters = ["is_active=1"]
    parameters: list[object] = []
    if selected_topic:
        filters.append("topic=?")
        parameters.append(selected_topic)
    where_clause = " AND ".join(filters)
    if excluded:
        placeholders = ",".join("?" for _ in excluded)
        rows = database.execute(
            f"""SELECT * FROM questions WHERE {where_clause}
            ORDER BY CASE WHEN id IN ({placeholders}) THEN 1 ELSE 0 END, RANDOM() LIMIT ?""",
            (*parameters, *excluded, limit),
        ).fetchall()
    else:
        rows = database.execute(
            f"SELECT * FROM questions WHERE {where_clause} ORDER BY RANDOM() LIMIT ?", (*parameters, limit)
        ).fetchall()
    return jsonify([serialize_question(row) for row in rows])


@app.post("/api/users")
def create_user():
    payload = request.get_json(silent=True) or {}
    display_name = " ".join(str(payload.get("displayName", "")).split())
    if not 2 <= len(display_name) <= 40:
        return jsonify({"error": "กรุณาใช้ชื่อ 2-40 ตัวอักษร"}), 400
    token = secrets.token_urlsafe(24)
    database = get_database()
    try:
        cursor = database.execute(
            "INSERT INTO users(display_name, team_name, access_token) VALUES (?, 'MT4', ?)",
            (display_name, token),
        )
        database.commit()
    except sqlite3.IntegrityError:
        return jsonify({"error": "ชื่อนี้มีผู้ใช้งานแล้ว กรุณาเพิ่มชื่อเล่นหรือเลขท้าย"}), 409
    return jsonify({"id": cursor.lastrowid, "displayName": display_name, "teamName": "MT4", "token": token}), 201


@app.post("/api/attempts")
def save_attempt():
    payload = request.get_json(silent=True) or {}
    token = str(payload.get("token", ""))
    database = get_database()
    user = database.execute("SELECT id FROM users WHERE access_token=?", (token,)).fetchone()
    if user is None:
        return jsonify({"error": "ไม่พบผู้ใช้งาน กรุณาลงชื่อใหม่"}), 401
    try:
        score = int(payload.get("score", 0))
        total = int(payload.get("totalQuestions", 0))
        duration = max(0, int(payload.get("durationSeconds", 0)))
    except (TypeError, ValueError):
        return jsonify({"error": "ข้อมูลผลสอบไม่ถูกต้อง"}), 400
    if total < 1 or score < 0 or score > total:
        return jsonify({"error": "คะแนนไม่ถูกต้อง"}), 400
    topic_scores = payload.get("topicScores", {})
    database.execute(
        """INSERT INTO attempts(user_id, score, total_questions, duration_seconds, selected_topic, topic_scores_json)
        VALUES (?, ?, ?, ?, ?, ?)""",
        (user["id"], score, total, duration, str(payload.get("selectedTopic", "ทุกหมวด"))[:80], json.dumps(topic_scores, ensure_ascii=False)),
    )
    database.commit()
    return jsonify({"saved": True}), 201


@app.get("/api/leaderboard")
def leaderboard():
    rows = get_database().execute(
        """SELECT u.display_name, u.team_name, COUNT(a.id) AS attempts,
        MAX(ROUND(a.score * 100.0 / a.total_questions)) AS best_score,
        ROUND(AVG(a.score * 100.0 / a.total_questions)) AS average_score
        FROM users u JOIN attempts a ON a.user_id=u.id
        GROUP BY u.id ORDER BY best_score DESC, average_score DESC, attempts DESC, u.display_name LIMIT 10"""
    ).fetchall()
    return jsonify([dict(row) for row in rows])


@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/<path:filename>")
def static_files(filename: str):
    if filename not in {"app.js", "styles.css", "favicon.ico", "assets/mt4-heart-logo.png", "assets/team-community-logo.png", "assets/one-team-logo.png"}:
        return jsonify({"error": "not found"}), 404
    return send_from_directory(BASE_DIR, filename)


with app.app_context():
    initialize_database()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
