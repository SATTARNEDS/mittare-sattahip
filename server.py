from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from functools import wraps
from io import BytesIO
from pathlib import Path

from flask import Flask, g, jsonify, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash
from pypdf import PdfReader

from extra_questions import EXTRA_QUESTIONS
from pdf_questions_2567 import PDF_QUESTIONS_2567

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATABASE_PATH = Path(os.environ.get("DATABASE_PATH", DATA_DIR / "exam_coach.sqlite3"))

app = Flask(__name__, static_folder=None)
app.config["JSON_AS_ASCII"] = False
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY", secrets.token_hex(32)),
    SESSION_COOKIE_NAME="mittare_exam_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "1") == "1",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 8,
)

QUESTION_STATUSES = {"draft", "pending", "published", "paused"}
QUESTION_AUDIENCES = {"agent", "broker", "general"}
PDF_SOURCE_TITLE = "แนวข้อสอบนายหน้าประกันภัย 2567 ได้จากแหววมา.pdf (หัวเอกสารระบุอัปเดต 2565)"
AGENT_PDF_SOURCE_TITLE = "รวมสอบตัวแทน.pdf หน้า 5-18"
ETHICS_TOPIC = "จรรยาบรรณและศีลธรรมของตัวแทนประกันวินาศภัย"
SIMULATION_SECTIONS = (
    ("จรรยาบรรณตัวแทนประกันวินาศภัย", ETHICS_TOPIC, 20, None, None),
    ("ความรู้ทั่วไปเกี่ยวกับการประกันวินาศภัย", "หลักการประกันภัยและกฎหมายแพ่งพาณิชย์", 4, 1094, 1117),
    ("วิชาประกันอัคคีภัย", "ประกันอัคคีภัยและความเสี่ยงภัยทรัพย์สิน", 4, None, None),
    ("วิชาการประกันภัยรถยนต์", "ประกันภัยรถยนต์", 4, None, None),
    ("วิชาการประกันภัยทางทะเลและขนส่ง", "ประกันภัยทางทะเลและขนส่ง", 4, None, None),
    ("วิชาการประกันภัยเบ็ดเตล็ด", "ประกันภัยเบ็ดเตล็ด", 4, None, None),
    ("ประมวลกฎหมายแพ่งและพาณิชย์ว่าด้วยการประกันภัย", "หลักการประกันภัยและกฎหมายแพ่งพาณิชย์", 10, 1118, 1237),
    ("พ.ร.บ.ประกันวินาศภัย", "พระราชบัญญัติประกันวินาศภัย", 10, None, None),
)
ADMIN_ROLES = {"head", "admin"}
PDF_IMPORT_MAX_BYTES = 25 * 1024 * 1024
PDF_IMPORT_MAX_PAGES = 300
PDF_IMPORT_MAX_QUESTIONS = 500


def get_database() -> sqlite3.Connection:
    if "database" not in g:
        DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
        g.database = sqlite3.connect(DATABASE_PATH, timeout=10)
        g.database.row_factory = sqlite3.Row
        g.database.execute("PRAGMA foreign_keys=ON")
        g.database.execute("PRAGMA journal_mode=WAL")
        g.database.execute("PRAGMA busy_timeout=10000")
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


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_columns(database: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row["name"] for row in database.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, definition in columns.items():
        if name not in existing:
            database.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


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
        CREATE TABLE IF NOT EXISTS admin_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL COLLATE NOCASE UNIQUE,
            password_hash TEXT NOT NULL,
            display_name TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('head', 'admin')),
            is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_login_at TEXT
        );
        CREATE TABLE IF NOT EXISTS question_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
            version_number INTEGER NOT NULL,
            snapshot_json TEXT NOT NULL,
            action TEXT NOT NULL,
            changed_by INTEGER REFERENCES admin_users(id),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(question_id, version_number)
        );
        CREATE INDEX IF NOT EXISTS idx_question_versions_question ON question_versions(question_id, version_number DESC);
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_user_id INTEGER REFERENCES admin_users(id),
            action TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT,
            details_json TEXT NOT NULL DEFAULT '{}',
            ip_address TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_audit_logs_date ON audit_logs(created_at DESC);
        CREATE TABLE IF NOT EXISTS login_throttle (
            throttle_key TEXT PRIMARY KEY,
            failure_count INTEGER NOT NULL DEFAULT 0,
            blocked_until INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    ensure_columns(database, "questions", {
        "status": "TEXT NOT NULL DEFAULT 'published'",
        "difficulty": "TEXT NOT NULL DEFAULT 'medium'",
        "exam_frequency": "TEXT NOT NULL DEFAULT 'medium'",
        "source_title": "TEXT NOT NULL DEFAULT ''",
        "source_url": "TEXT NOT NULL DEFAULT ''",
        "verified_at": "TEXT",
        "created_by": "INTEGER REFERENCES admin_users(id)",
        "approved_by": "INTEGER REFERENCES admin_users(id)",
        "published_at": "TEXT",
        "audience": "TEXT NOT NULL DEFAULT 'general'",
    })
    ensure_columns(database, "users", {
        "username": "TEXT COLLATE NOCASE",
        "password_hash": "TEXT",
        "last_login_at": "TEXT",
    })
    ensure_columns(database, "attempts", {
        "exam_mode": "TEXT NOT NULL DEFAULT 'practice'",
    })
    database.execute("UPDATE attempts SET exam_mode='simulation' WHERE selected_topic='จำลองสอบจริง'")
    database.execute(
        "UPDATE attempts SET exam_mode='topic' WHERE exam_mode='practice' "
        "AND selected_topic NOT IN ('ทุกหมวด', '', 'จำลองสอบจริง')"
    )
    database.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(username) WHERE username IS NOT NULL")
    database.execute(
        "UPDATE questions SET status='published', published_at=COALESCE(published_at, created_at) "
        "WHERE status IS NULL OR status=''"
    )
    seed_rows = read_legacy_questions() + EXTRA_QUESTIONS
    database.executemany(
        """
        INSERT INTO questions(id, topic, question_text, options_json, correct_answer, explanation)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO NOTHING
        """,
        [(row[0], row[1], row[2], json.dumps(row[3], ensure_ascii=False), row[4], row[5]) for row in seed_rows],
    )
    database.executemany(
        """
        INSERT INTO questions(id, topic, question_text, options_json, correct_answer, explanation,
                              source_title, status, is_active, published_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'published', 1, CURRENT_TIMESTAMP)
        ON CONFLICT(id) DO NOTHING
        """,
        [
            (
                row[0], row[1], row[2], json.dumps(row[3], ensure_ascii=False), row[4], row[5],
                PDF_SOURCE_TITLE,
            )
            for row in PDF_QUESTIONS_2567
        ],
    )
    agent_ethics_rows = []
    for filename in ("agent_ethics_questions_1.json", "agent_ethics_questions_1b.json",
                     "agent_ethics_questions_2.json", "agent_ethics_questions_3.json"):
        with (BASE_DIR / filename).open("r", encoding="utf-8") as source_file:
            agent_ethics_rows.extend(json.load(source_file))
    database.executemany(
        """
        INSERT INTO questions(id, topic, question_text, options_json, correct_answer, explanation,
                              source_title, status, is_active, published_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'published', 1, CURRENT_TIMESTAMP)
        ON CONFLICT(id) DO UPDATE SET topic=excluded.topic, question_text=excluded.question_text,
            options_json=excluded.options_json, correct_answer=excluded.correct_answer,
            explanation=excluded.explanation, source_title=excluded.source_title,
            status='published', is_active=1
        """,
        [(row["id"], row["topic"], row["q"], json.dumps(row["o"], ensure_ascii=False), row["a"], row["e"],
          AGENT_PDF_SOURCE_TITLE) for row in agent_ethics_rows],
    )
    # พักคลังนายหน้าไว้ก่อนตามขอบเขตระบบตัวแทน โดยไม่ลบข้อมูลหรือประวัติการแก้ไข
    database.execute(
        "UPDATE questions SET is_active=0, status='paused' WHERE topic LIKE '%นายหน้า%'"
    )
    # เอกสารต้นฉบับมีข้อซ้ำ 4 คู่ จึงพักสำเนาซ้ำเพื่อไม่ให้สุ่มเจอคำถามเดียวกันในชุดเดียว
    database.execute(
        "UPDATE questions SET is_active=0, status='paused' WHERE id IN (3025, 3034, 3037, 3040)"
    )
    database.execute("UPDATE questions SET audience='broker' WHERE topic LIKE '%นายหน้า%'")
    database.execute(
        "UPDATE questions SET audience='agent' WHERE topic LIKE '%ตัวแทน%' OR source_title=?",
        (AGENT_PDF_SOURCE_TITLE,),
    )
    database.execute("CREATE INDEX IF NOT EXISTS idx_questions_audience_status ON questions(audience, status, is_active)")
    bootstrap_username = os.environ.get("ADMIN_USERNAME", "").strip()
    bootstrap_password = os.environ.get("ADMIN_PASSWORD", "")
    if bootstrap_username and len(bootstrap_password) >= 12:
        existing_admin = database.execute("SELECT id FROM admin_users LIMIT 1").fetchone()
        reset_requested = os.environ.get("RESET_ADMIN_PASSWORD", "0").strip().lower() in {"1", "true", "yes"}
        reset_target = database.execute(
            "SELECT id FROM admin_users WHERE username=?", (bootstrap_username,)
        ).fetchone()
        if reset_requested:
            password_hash = generate_password_hash(bootstrap_password)
            if reset_target is not None:
                database.execute(
                    "UPDATE admin_users SET password_hash=?, is_active=1, updated_at=? WHERE id=?",
                    (password_hash, utc_now(), reset_target["id"]),
                )
            else:
                database.execute(
                    "INSERT INTO admin_users(username, password_hash, display_name, role) VALUES (?, ?, ?, 'admin')",
                    (bootstrap_username, password_hash, "ผู้ดูแลระบบ MT4"),
                )
            # Member throttles start with "member|"; every other key belongs to the admin login.
            database.execute(
                "DELETE FROM login_throttle WHERE throttle_key NOT LIKE 'member|%'"
            )
            print(f"[admin-reset] password reset and login unlocked for {bootstrap_username}", flush=True)
        elif existing_admin is None:
            database.execute(
                "INSERT INTO admin_users(username, password_hash, display_name, role) VALUES (?, ?, ?, 'admin')",
                (bootstrap_username, generate_password_hash(bootstrap_password), "ผู้ดูแลระบบ MT4"),
            )
    database.commit()


def serialize_question(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "topic": row["topic"], "q": row["question_text"],
        "o": json.loads(row["options_json"]), "a": row["correct_answer"], "e": row["explanation"],
    }


def serialize_admin_question(row: sqlite3.Row) -> dict:
    question = serialize_question(row)
    question.update({
        "status": row["status"], "isActive": bool(row["is_active"]),
        "audience": row["audience"],
        "difficulty": row["difficulty"], "examFrequency": row["exam_frequency"],
        "sourceTitle": row["source_title"], "sourceUrl": row["source_url"],
        "verifiedAt": row["verified_at"], "createdAt": row["created_at"],
        "updatedAt": row["updated_at"], "publishedAt": row["published_at"],
        "creatorName": row["creator_name"] if "creator_name" in row.keys() else None,
        "approverName": row["approver_name"] if "approver_name" in row.keys() else None,
    })
    return question


def current_admin() -> sqlite3.Row | None:
    admin_id = session.get("admin_user_id")
    if not admin_id:
        return None
    return get_database().execute(
        "SELECT id, username, display_name, role FROM admin_users WHERE id=? AND is_active=1", (admin_id,)
    ).fetchone()


def require_admin(*roles: str):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            admin = current_admin()
            if admin is None:
                return jsonify({"error": "กรุณาเข้าสู่ระบบผู้ดูแล"}), 401
            if roles and admin["role"] not in roles:
                return jsonify({"error": "บัญชีนี้ไม่มีสิทธิ์ดำเนินการ"}), 403
            g.admin_user = admin
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                csrf_token = request.headers.get("X-CSRF-Token", "")
                if not csrf_token or not secrets.compare_digest(csrf_token, session.get("csrf_token", "")):
                    return jsonify({"error": "คำขอหมดอายุ กรุณาเข้าสู่ระบบใหม่"}), 403
            return view(*args, **kwargs)
        return wrapped
    return decorator


def record_audit(action: str, entity_type: str, entity_id: object = None, details: dict | None = None) -> None:
    admin = getattr(g, "admin_user", None)
    forwarded = request.headers.get("X-Forwarded-For", "")
    ip_address = (forwarded.split(",")[0].strip() or request.remote_addr or "")[:64]
    get_database().execute(
        """INSERT INTO audit_logs(admin_user_id, action, entity_type, entity_id, details_json, ip_address)
        VALUES (?, ?, ?, ?, ?, ?)""",
        (admin["id"] if admin else None, action, entity_type, str(entity_id) if entity_id is not None else None,
         json.dumps(details or {}, ensure_ascii=False), ip_address),
    )


def save_question_version(question_id: int, action: str, changed_by: int) -> None:
    database = get_database()
    row = database.execute("SELECT * FROM questions WHERE id=?", (question_id,)).fetchone()
    if row is None:
        return
    last_version = database.execute(
        "SELECT COALESCE(MAX(version_number), 0) AS number FROM question_versions WHERE question_id=?",
        (question_id,),
    ).fetchone()["number"]
    database.execute(
        """INSERT INTO question_versions(question_id, version_number, snapshot_json, action, changed_by)
        VALUES (?, ?, ?, ?, ?)""",
        (question_id, last_version + 1, json.dumps(dict(row), ensure_ascii=False), action, changed_by),
    )


def validate_question_payload(payload: dict) -> tuple[dict | None, str | None]:
    topic = " ".join(str(payload.get("topic", "")).split())[:100]
    question = " ".join(str(payload.get("q", "")).split())[:1000]
    explanation = " ".join(str(payload.get("e", "")).split())[:2000]
    options = [" ".join(str(value).split())[:500] for value in payload.get("o", [])]
    answer = " ".join(str(payload.get("a", "")).split())[:500]
    audience = str(payload.get("audience", "agent")).strip()
    if not topic or len(question) < 10 or len(explanation) < 10:
        return None, "กรุณากรอกหมวด คำถาม และคำอธิบายให้ครบถ้วน"
    if len(options) != 4 or any(not option for option in options) or len(set(options)) != 4:
        return None, "ต้องมีตัวเลือกที่ไม่ซ้ำกันครบ 4 ตัวเลือก"
    if options.count(answer) != 1:
        return None, "กรุณาเลือกคำตอบที่ถูกต้องจากตัวเลือกเพียงหนึ่งข้อ"
    if audience not in QUESTION_AUDIENCES:
        return None, "ประเภทคลังข้อสอบไม่ถูกต้อง"
    source_url = str(payload.get("sourceUrl", "")).strip()[:1000]
    if source_url and not re.match(r"^https://", source_url, re.IGNORECASE):
        return None, "ลิงก์อ้างอิงต้องขึ้นต้นด้วย https://"
    return {
        "topic": topic, "q": question, "e": explanation, "o": options, "a": answer, "audience": audience,
        "difficulty": str(payload.get("difficulty", "medium")) if payload.get("difficulty") in {"easy", "medium", "hard"} else "medium",
        "examFrequency": str(payload.get("examFrequency", "medium")) if payload.get("examFrequency") in {"high", "medium", "low"} else "medium",
        "sourceTitle": " ".join(str(payload.get("sourceTitle", "")).split())[:300],
        "sourceUrl": source_url,
        "verifiedAt": str(payload.get("verifiedAt", "")).strip()[:10] or None,
    }, None


def parse_pdf_question_text(text: str, source_title: str) -> list[dict]:
    """แยกข้อสอบรูปแบบ ข้อ 1 / ก. ข. ค. ง. / เฉลย จากข้อความใน PDF"""
    normalized = str(text or "").replace("\r", "\n")
    normalized = re.sub(r"[ \t]+", " ", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    question_pattern = re.compile(r"(?m)^\s*(?:ข้อ\s*)?(\d{1,5})\s*[.)]\s*(.+?)\s*$")
    matches = list(question_pattern.finditer(normalized))
    results = []
    option_pattern = re.compile(r"(?m)^\s*([กขคง]|[A-Da-d]|[1-4])\s*[.)]\s*(.+?)\s*$")
    answer_pattern = re.compile(r"(?im)^\s*(?:เฉลย|คำตอบ)\s*[:：]?\s*([กขคง]|[A-Da-d]|[1-4]|.+?)\s*$")
    explanation_pattern = re.compile(r"(?im)^\s*(?:คำอธิบาย|อธิบาย|เหตุผล)\s*[:：]\s*(.+?)\s*$")
    key_order = {"ก": 0, "ข": 1, "ค": 2, "ง": 3, "A": 0, "B": 1, "C": 2, "D": 3,
                 "1": 0, "2": 1, "3": 2, "4": 3}
    for index, match in enumerate(matches[:PDF_IMPORT_MAX_QUESTIONS]):
        block_end = matches[index + 1].start() if index + 1 < len(matches) else len(normalized)
        block = normalized[match.end():block_end]
        option_matches = list(option_pattern.finditer(block))
        options = []
        for option_index, option_match in enumerate(option_matches[:4]):
            end_candidates = [len(block)]
            if option_index + 1 < len(option_matches):
                end_candidates.append(option_matches[option_index + 1].start())
            answer_match_after = answer_pattern.search(block, option_match.end())
            explanation_match_after = explanation_pattern.search(block, option_match.end())
            if answer_match_after:
                end_candidates.append(answer_match_after.start())
            if explanation_match_after:
                end_candidates.append(explanation_match_after.start())
            continuation = block[option_match.end():min(end_candidates)]
            options.append(" ".join(f"{option_match.group(2)} {continuation}".split())[:500])
        question_continuation_end = option_matches[0].start() if option_matches else len(block)
        question = " ".join(f"{match.group(2)} {block[:question_continuation_end]}".split())[:1000]
        answer_match = answer_pattern.search(block)
        answer_value = answer_match.group(1).strip() if answer_match else ""
        answer_index = key_order.get(answer_value.upper() if answer_value.isascii() else answer_value)
        answer = options[answer_index] if answer_index is not None and answer_index < len(options) else ""
        if not answer and answer_value:
            answer_normalized = " ".join(answer_value.split())
            answer = next((option for option in options if option == answer_normalized or option.startswith(answer_normalized)), "")
        explanation_match = explanation_pattern.search(block)
        explanation = " ".join(explanation_match.group(1).split())[:2000] if explanation_match else ""
        warnings = []
        if len(question) < 10:
            warnings.append("อ่านข้อความคำถามไม่ครบ")
        if len(options) != 4 or any(not option for option in options):
            warnings.append("ต้องมีตัวเลือกครบ 4 ตัว")
        if not answer:
            warnings.append("ไม่พบเฉลยที่ตรงกับตัวเลือก")
        if len(explanation) < 10:
            warnings.append("ไม่พบคำอธิบายเฉลยอย่างน้อย 10 ตัวอักษร")
        results.append({
            "sourceNumber": int(match.group(1)), "q": question, "o": options, "a": answer,
            "e": explanation, "sourceTitle": source_title, "warnings": warnings,
        })
    return results


@app.get("/api/meta")
def api_meta():
    row = get_database().execute(
        "SELECT COUNT(*) AS total, COUNT(DISTINCT topic) AS topics FROM questions "
        "WHERE is_active=1 AND status='published' AND audience<>'broker'"
    ).fetchone()
    return jsonify({
        "totalQuestions": row["total"], "totalTopics": row["topics"], "examSize": 20,
        "simulation": {"totalQuestions": 60, "totalPoints": 100,
                       "ethicsQuestions": 20, "ethicsPassCorrect": 14, "otherPassPoints": 48},
    })


@app.get("/api/topics")
def api_topics():
    rows = get_database().execute(
        "SELECT topic, COUNT(*) AS question_count FROM questions WHERE is_active=1 AND status='published' AND audience<>'broker' "
        "GROUP BY topic ORDER BY CASE WHEN topic=? THEN 0 ELSE 1 END, topic",
        (ETHICS_TOPIC,),
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
    filters = ["is_active=1", "status='published'", "audience<>'broker'"]
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


@app.get("/api/exam-simulation")
def api_exam_simulation():
    """สร้างชุดจำลองตามสัดส่วนในหลักเกณฑ์สอบ โดยเรียงจรรยาบรรณไว้ก่อน."""
    database = get_database()
    sections: list[dict] = []
    section_counts: list[dict] = []
    for label, topic, required, first_id, last_id in SIMULATION_SECTIONS:
        filters = ["is_active=1", "status='published'", "topic=?"]
        parameters: list[object] = [topic]
        if first_id is not None and last_id is not None:
            filters.append("id BETWEEN ? AND ?")
            parameters.extend((first_id, last_id))
        rows = database.execute(
            f"SELECT * FROM questions WHERE {' AND '.join(filters)} ORDER BY RANDOM() LIMIT ?",
            (*parameters, required),
        ).fetchall()
        if len(rows) < required:
            return jsonify({"error": f"คลังหมวด {label} มีไม่ครบ {required} ข้อ"}), 409
        sections.extend(serialize_question(row) for row in rows)
        section_counts.append({"label": label, "questionCount": required})
    return jsonify({"questions": sections, "sections": section_counts, "durationSeconds": 0,
                    "totalPoints": 100, "ethicsTopic": ETHICS_TOPIC,
                    "ethicsQuestions": 20, "ethicsPassCorrect": 14,
                    "ethicsPointPerQuestion": 1, "otherPointPerQuestion": 2, "otherPassPoints": 48})


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


def serialize_member(user: sqlite3.Row) -> dict:
    return {"id": user["id"], "displayName": user["display_name"], "username": user["username"], "teamName": user["team_name"]}


@app.post("/api/members/register")
def register_member():
    payload = request.get_json(silent=True) or {}
    display_name = " ".join(str(payload.get("displayName", "")).split())
    username = str(payload.get("username", "")).strip().lower()
    password = str(payload.get("password", ""))
    if not 2 <= len(display_name) <= 40:
        return jsonify({"error": "กรุณากรอกชื่อที่ใช้แสดง 2-40 ตัวอักษร"}), 400
    if not re.fullmatch(r"[a-z0-9._-]{3,30}", username):
        return jsonify({"error": "ชื่อผู้ใช้ใช้ a-z, 0-9, จุด ขีดกลาง หรือขีดล่าง 3-30 ตัว"}), 400
    if len(password) < 8:
        return jsonify({"error": "รหัสผ่านต้องมีอย่างน้อย 8 ตัวอักษร"}), 400
    database = get_database()
    token = secrets.token_urlsafe(24)
    try:
        cursor = database.execute(
            "INSERT INTO users(display_name, team_name, access_token, username, password_hash) VALUES (?, 'MT4', ?, ?, ?)",
            (display_name, token, username, generate_password_hash(password)),
        )
        database.commit()
    except sqlite3.IntegrityError:
        return jsonify({"error": "ชื่อผู้ใช้หรือชื่อที่ใช้แสดงนี้มีอยู่แล้ว"}), 409
    session["member_user_id"] = cursor.lastrowid
    user = database.execute("SELECT * FROM users WHERE id=?", (cursor.lastrowid,)).fetchone()
    return jsonify({"user": serialize_member(user), "token": token}), 201


@app.post("/api/members/login")
def login_member():
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username", "")).strip().lower()
    password = str(payload.get("password", ""))
    database = get_database()
    throttle_key = f"member|{request.remote_addr or 'unknown'}|{username}"[:180]
    throttle = database.execute("SELECT * FROM login_throttle WHERE throttle_key=?", (throttle_key,)).fetchone()
    now_epoch = int(time.time())
    if throttle and throttle["blocked_until"] > now_epoch:
        return jsonify({"error": "ลองเข้าสู่ระบบหลายครั้งเกินไป กรุณารอ 5 นาที"}), 429
    user = database.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if user is None or not user["password_hash"] or not check_password_hash(user["password_hash"], password):
        failures = (throttle["failure_count"] if throttle else 0) + 1
        blocked_until = now_epoch + 300 if failures >= 5 else 0
        database.execute(
            """INSERT INTO login_throttle(throttle_key, failure_count, blocked_until, updated_at)
            VALUES (?, ?, ?, ?) ON CONFLICT(throttle_key) DO UPDATE SET
            failure_count=excluded.failure_count, blocked_until=excluded.blocked_until, updated_at=excluded.updated_at""",
            (throttle_key, failures, blocked_until, utc_now()),
        )
        database.commit()
        return jsonify({"error": "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง"}), 401
    database.execute("DELETE FROM login_throttle WHERE throttle_key=?", (throttle_key,))
    database.execute("UPDATE users SET last_login_at=? WHERE id=?", (utc_now(), user["id"]))
    database.commit()
    session["member_user_id"] = user["id"]
    return jsonify({"user": serialize_member(user), "token": user["access_token"]})


@app.get("/api/members/me")
def current_member():
    member_id = session.get("member_user_id")
    if not member_id:
        return jsonify({"error": "ยังไม่ได้เข้าสู่ระบบ"}), 401
    database = get_database()
    user = database.execute("SELECT * FROM users WHERE id=?", (member_id,)).fetchone()
    if user is None:
        session.pop("member_user_id", None)
        return jsonify({"error": "ไม่พบบัญชีสมาชิก"}), 401
    summary = database.execute(
        """SELECT COUNT(*) AS attempts, COALESCE(MAX(ROUND(score * 100.0 / total_questions)), 0) AS best_score,
        COALESCE(ROUND(AVG(score * 100.0 / total_questions)), 0) AS average_score
        FROM attempts WHERE user_id=?""",
        (member_id,),
    ).fetchone()
    return jsonify({"user": serialize_member(user), "token": user["access_token"], "summary": dict(summary)})


@app.get("/api/members/me/attempts")
def current_member_attempts():
    member_id = session.get("member_user_id")
    if not member_id:
        return jsonify({"error": "กรุณาเข้าสู่ระบบเพื่อดูประวัติ"}), 401
    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = int(request.args.get("perPage", 10))
    except (TypeError, ValueError):
        return jsonify({"error": "ข้อมูลหน้าไม่ถูกต้อง"}), 400
    if per_page not in {10, 20, 50}:
        return jsonify({"error": "จำนวนรายการต่อหน้าไม่ถูกต้อง"}), 400
    database = get_database()
    user = database.execute("SELECT id, display_name, username FROM users WHERE id=?", (member_id,)).fetchone()
    if user is None:
        session.pop("member_user_id", None)
        return jsonify({"error": "ไม่พบบัญชีสมาชิก"}), 401
    total_items = database.execute("SELECT COUNT(*) AS count FROM attempts WHERE user_id=?", (member_id,)).fetchone()["count"]
    total_pages = max(1, (total_items + per_page - 1) // per_page)
    page = min(page, total_pages)
    rows = database.execute(
        """SELECT id, score, total_questions, duration_seconds, selected_topic, topic_scores_json,
        exam_mode, completed_at FROM attempts WHERE user_id=? ORDER BY completed_at DESC, id DESC LIMIT ? OFFSET ?""",
        (member_id, per_page, (page - 1) * per_page),
    ).fetchall()
    attempts = []
    for row in rows:
        item = dict(row)
        try:
            item["topicScores"] = json.loads(item.pop("topic_scores_json"))
        except (TypeError, json.JSONDecodeError):
            item["topicScores"] = {}
            item.pop("topic_scores_json", None)
        attempts.append(item)
    return jsonify({"user": dict(user), "attempts": attempts, "pagination": {
        "page": page, "perPage": per_page, "totalItems": total_items, "totalPages": total_pages,
        "hasNext": page < total_pages,
    }})


@app.put("/api/members/me")
def update_member():
    member_id = session.get("member_user_id")
    if not member_id:
        return jsonify({"error": "กรุณาเข้าสู่ระบบอีกครั้ง"}), 401
    payload = request.get_json(silent=True) or {}
    display_name = " ".join(str(payload.get("displayName", "")).split())
    current_password = str(payload.get("currentPassword", ""))
    new_password = str(payload.get("newPassword", ""))
    if not 2 <= len(display_name) <= 40:
        return jsonify({"error": "กรุณากรอกชื่อที่ใช้แสดง 2-40 ตัวอักษร"}), 400
    if new_password and len(new_password) < 8:
        return jsonify({"error": "รหัสผ่านใหม่ต้องมีอย่างน้อย 8 ตัวอักษร"}), 400
    database = get_database()
    user = database.execute("SELECT * FROM users WHERE id=?", (member_id,)).fetchone()
    if user is None or not user["password_hash"] or not check_password_hash(user["password_hash"], current_password):
        return jsonify({"error": "รหัสผ่านปัจจุบันไม่ถูกต้อง"}), 403
    try:
        if new_password:
            database.execute(
                "UPDATE users SET display_name=?, password_hash=? WHERE id=?",
                (display_name, generate_password_hash(new_password), member_id),
            )
        else:
            database.execute("UPDATE users SET display_name=? WHERE id=?", (display_name, member_id))
        database.commit()
    except sqlite3.IntegrityError:
        return jsonify({"error": "ชื่อที่ใช้แสดงนี้มีสมาชิกคนอื่นใช้แล้ว"}), 409
    updated = database.execute("SELECT * FROM users WHERE id=?", (member_id,)).fetchone()
    return jsonify({"user": serialize_member(updated), "passwordChanged": bool(new_password)})


@app.post("/api/members/logout")
def logout_member():
    session.pop("member_user_id", None)
    return jsonify({"loggedOut": True})


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
    exam_mode = str(payload.get("examMode", "practice")).strip().lower()
    if exam_mode not in {"practice", "topic", "simulation"}:
        return jsonify({"error": "รูปแบบการสอบไม่ถูกต้อง"}), 400
    database.execute(
        """INSERT INTO attempts(user_id, score, total_questions, duration_seconds, selected_topic,
        topic_scores_json, exam_mode) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (user["id"], score, total, duration, str(payload.get("selectedTopic", "ทุกหมวด"))[:80],
         json.dumps(topic_scores, ensure_ascii=False), exam_mode),
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


@app.post("/api/admin/login")
def admin_login():
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username", "")).strip()[:80]
    password = str(payload.get("password", ""))
    database = get_database()
    forwarded = request.headers.get("X-Forwarded-For", "")
    client_ip = (forwarded.split(",")[0].strip() or request.remote_addr or "unknown")[:64]
    throttle_key = f"{client_ip}|{username.casefold()}"[:180]
    throttle = database.execute("SELECT * FROM login_throttle WHERE throttle_key=?", (throttle_key,)).fetchone()
    now_epoch = int(time.time())
    if throttle and throttle["blocked_until"] > now_epoch:
        return jsonify({"error": "เข้าสู่ระบบผิดหลายครั้ง กรุณารอ 15 นาทีแล้วลองใหม่"}), 429
    admin = database.execute("SELECT * FROM admin_users WHERE username=? AND is_active=1", (username,)).fetchone()
    if admin is None or not check_password_hash(admin["password_hash"], password):
        failure_count = (throttle["failure_count"] if throttle else 0) + 1
        blocked_until = now_epoch + 900 if failure_count >= 5 else 0
        database.execute(
            """INSERT INTO login_throttle(throttle_key, failure_count, blocked_until, updated_at)
            VALUES (?, ?, ?, ?) ON CONFLICT(throttle_key) DO UPDATE SET
            failure_count=excluded.failure_count, blocked_until=excluded.blocked_until, updated_at=excluded.updated_at""",
            (throttle_key, failure_count, blocked_until, utc_now()),
        )
        database.commit()
        return jsonify({"error": "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง"}), 401
    database.execute("DELETE FROM login_throttle WHERE throttle_key=?", (throttle_key,))
    session.clear()
    session.permanent = True
    session["admin_user_id"] = admin["id"]
    session["csrf_token"] = secrets.token_urlsafe(24)
    database.execute("UPDATE admin_users SET last_login_at=?, updated_at=? WHERE id=?", (utc_now(), utc_now(), admin["id"]))
    g.admin_user = admin
    record_audit("login", "admin_user", admin["id"])
    database.commit()
    return jsonify({
        "user": {"id": admin["id"], "username": admin["username"], "displayName": admin["display_name"], "role": admin["role"]},
        "csrfToken": session["csrf_token"],
    })


@app.get("/api/admin/setup-status")
def admin_setup_status():
    configured = get_database().execute("SELECT 1 FROM admin_users WHERE is_active=1 LIMIT 1").fetchone() is not None
    return jsonify({"configured": configured, "persistentStorage": not str(DATABASE_PATH).startswith("/tmp/")})


@app.post("/api/admin/logout")
@require_admin()
def admin_logout():
    record_audit("logout", "admin_user", g.admin_user["id"])
    get_database().commit()
    session.clear()
    return jsonify({"loggedOut": True})


@app.get("/api/admin/me")
@require_admin()
def admin_me():
    admin = g.admin_user
    return jsonify({
        "user": {"id": admin["id"], "username": admin["username"], "displayName": admin["display_name"], "role": admin["role"]},
        "csrfToken": session["csrf_token"],
    })


@app.get("/api/admin/dashboard")
@require_admin()
def admin_dashboard():
    database = get_database()
    counts = {row["status"]: row["count"] for row in database.execute(
        "SELECT status, COUNT(*) AS count FROM questions GROUP BY status"
    ).fetchall()}
    counts["total"] = sum(counts.values())
    for audience in QUESTION_AUDIENCES:
        counts[audience] = database.execute(
            "SELECT COUNT(*) AS count FROM questions WHERE audience=?", (audience,)
        ).fetchone()["count"]
    counts["admins"] = database.execute("SELECT COUNT(*) AS count FROM admin_users WHERE is_active=1").fetchone()["count"]
    return jsonify(counts)


ADMIN_QUESTION_SELECT = """SELECT q.*, creator.display_name AS creator_name, approver.display_name AS approver_name
FROM questions q
LEFT JOIN admin_users creator ON creator.id=q.created_by
LEFT JOIN admin_users approver ON approver.id=q.approved_by"""


@app.get("/api/admin/questions")
@require_admin()
def admin_questions():
    status = request.args.get("status", "").strip()
    audience = request.args.get("audience", "").strip()
    topic = request.args.get("topic", "").strip()
    search = request.args.get("search", "").strip()[:200]
    try:
        page = max(1, int(request.args.get("page", "1")))
        per_page = int(request.args.get("perPage", "25"))
    except ValueError:
        return jsonify({"error": "page and perPage must be numbers"}), 400
    if per_page not in {10, 25, 50, 100}:
        return jsonify({"error": "perPage must be 10, 25, 50 or 100"}), 400
    filters = ["1=1"]
    parameters: list[object] = []
    if status in QUESTION_STATUSES:
        filters.append("q.status=?")
        parameters.append(status)
    if audience in QUESTION_AUDIENCES:
        filters.append("q.audience=?")
        parameters.append(audience)
    if topic:
        filters.append("q.topic=?")
        parameters.append(topic)
    if search:
        filters.append("(q.question_text LIKE ? OR q.explanation LIKE ?)")
        parameters.extend([f"%{search}%", f"%{search}%"])
    database = get_database()
    where_clause = " AND ".join(filters)
    total_items = database.execute(
        f"SELECT COUNT(*) AS count FROM questions q WHERE {where_clause}", parameters
    ).fetchone()["count"]
    total_pages = max(1, (total_items + per_page - 1) // per_page)
    page = min(page, total_pages)
    rows = database.execute(
        f"{ADMIN_QUESTION_SELECT} WHERE {where_clause} "
        "ORDER BY q.updated_at DESC, q.id DESC LIMIT ? OFFSET ?",
        (*parameters, per_page, (page - 1) * per_page),
    ).fetchall()
    return jsonify({
        "items": [serialize_admin_question(row) for row in rows],
        "pagination": {
            "page": page, "perPage": per_page, "totalItems": total_items,
            "totalPages": total_pages, "hasPrevious": page > 1, "hasNext": page < total_pages,
        },
    })


@app.get("/api/admin/topics")
@require_admin()
def admin_topics():
    audience = request.args.get("audience", "").strip()
    status = request.args.get("status", "").strip()
    filters = ["1=1"]
    parameters: list[object] = []
    if audience in QUESTION_AUDIENCES:
        filters.append("audience=?")
        parameters.append(audience)
    if status in QUESTION_STATUSES:
        filters.append("status=?")
        parameters.append(status)
    rows = get_database().execute(
        f"SELECT topic, COUNT(*) AS question_count FROM questions WHERE {' AND '.join(filters)} "
        "GROUP BY topic ORDER BY CASE WHEN topic=? THEN 0 ELSE 1 END, topic",
        (*parameters, ETHICS_TOPIC),
    ).fetchall()
    return jsonify([{"topic": row["topic"], "questionCount": row["question_count"]} for row in rows])


@app.get("/api/admin/questions/<int:question_id>")
@require_admin()
def admin_question_detail(question_id: int):
    row = get_database().execute(f"{ADMIN_QUESTION_SELECT} WHERE q.id=?", (question_id,)).fetchone()
    if row is None:
        return jsonify({"error": "ไม่พบข้อสอบ"}), 404
    return jsonify(serialize_admin_question(row))


@app.post("/api/admin/pdf-import/preview")
@require_admin()
def admin_pdf_import_preview():
    uploaded = request.files.get("pdf")
    if uploaded is None or not uploaded.filename:
        return jsonify({"error": "กรุณาเลือกไฟล์ PDF"}), 400
    filename = re.sub(r"[^0-9A-Za-zก-๙._ -]", "_", Path(uploaded.filename).name).strip()[:200]
    if not filename.lower().endswith(".pdf"):
        return jsonify({"error": "รองรับเฉพาะไฟล์นามสกุล .pdf"}), 400
    content = uploaded.stream.read(PDF_IMPORT_MAX_BYTES + 1)
    if len(content) > PDF_IMPORT_MAX_BYTES:
        return jsonify({"error": "ไฟล์ PDF ต้องมีขนาดไม่เกิน 25 MB"}), 413
    if not content.startswith(b"%PDF-"):
        return jsonify({"error": "ไฟล์นี้ไม่ใช่ PDF ที่ถูกต้อง"}), 400
    try:
        reader = PdfReader(BytesIO(content), strict=False)
        if reader.is_encrypted:
            return jsonify({"error": "ไม่รองรับ PDF ที่มีรหัสผ่าน"}), 400
        if len(reader.pages) > PDF_IMPORT_MAX_PAGES:
            return jsonify({"error": f"PDF ต้องมีไม่เกิน {PDF_IMPORT_MAX_PAGES} หน้า"}), 413
        page_texts = []
        for page in reader.pages:
            page_texts.append(page.extract_text() or "")
    except Exception:
        return jsonify({"error": "ไม่สามารถอ่านข้อความจาก PDF นี้ได้ กรุณาตรวจว่าไฟล์ไม่เสียหาย"}), 400
    extracted_text = "\n\n".join(page_texts).strip()
    if len(extracted_text) < 40:
        return jsonify({"error": "PDF นี้เป็นไฟล์สแกนหรือไม่มีข้อความ กรุณา OCR ภาษาไทยก่อนนำเข้า"}), 422
    questions = parse_pdf_question_text(extracted_text, filename)
    if not questions:
        return jsonify({"error": "ไม่พบรูปแบบข้อสอบ กรุณาใช้เลขข้อและตัวเลือก ก. ข. ค. ง. พร้อมบรรทัด เฉลย:"}), 422
    database = get_database()
    for question in questions:
        duplicate = database.execute("SELECT id FROM questions WHERE question_text=?", (question["q"],)).fetchone()
        question["duplicateId"] = duplicate["id"] if duplicate else None
        if duplicate:
            question["warnings"].append(f"ซ้ำกับ ID {duplicate['id']} ในคลัง")
        question["ready"] = not question["warnings"]
    record_audit("preview", "pdf_import", filename, {"pages": len(reader.pages), "questions": len(questions)})
    database.commit()
    return jsonify({"filename": filename, "pageCount": len(reader.pages), "questions": questions,
                    "truncated": len(questions) >= PDF_IMPORT_MAX_QUESTIONS})


@app.post("/api/admin/pdf-import/commit")
@require_admin()
def admin_pdf_import_commit():
    payload = request.get_json(silent=True) or {}
    questions = payload.get("questions")
    if not isinstance(questions, list) or not questions:
        return jsonify({"error": "กรุณาเลือกข้อสอบอย่างน้อย 1 ข้อ"}), 400
    if len(questions) > 200:
        return jsonify({"error": "นำเข้าได้สูงสุดครั้งละ 200 ข้อ"}), 400
    database = get_database()
    next_id = database.execute("SELECT COALESCE(MAX(id), 0) + 1 AS id FROM questions").fetchone()["id"]
    imported = []
    errors = []
    seen_questions = set()
    now = utc_now()
    for position, payload_question in enumerate(questions, start=1):
        cleaned, error = validate_question_payload(payload_question if isinstance(payload_question, dict) else {})
        source_number = payload_question.get("sourceNumber", position) if isinstance(payload_question, dict) else position
        if error:
            errors.append({"sourceNumber": source_number, "error": error})
            continue
        normalized_question = cleaned["q"].casefold()
        if normalized_question in seen_questions:
            errors.append({"sourceNumber": source_number, "error": "คำถามซ้ำภายในชุดนำเข้า"})
            continue
        duplicate = database.execute("SELECT id FROM questions WHERE question_text=?", (cleaned["q"],)).fetchone()
        if duplicate:
            errors.append({"sourceNumber": source_number, "error": f"ซ้ำกับ ID {duplicate['id']} ในคลัง"})
            continue
        database.execute(
            """INSERT INTO questions(id, topic, question_text, options_json, correct_answer, explanation,
            status, is_active, difficulty, exam_frequency, source_title, source_url, verified_at, audience,
            created_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'draft', 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (next_id, cleaned["topic"], cleaned["q"], json.dumps(cleaned["o"], ensure_ascii=False), cleaned["a"],
             cleaned["e"], cleaned["difficulty"], cleaned["examFrequency"], cleaned["sourceTitle"],
             cleaned["sourceUrl"], cleaned["verifiedAt"], cleaned["audience"], g.admin_user["id"], now, now),
        )
        save_question_version(next_id, "created", g.admin_user["id"])
        imported.append({"id": next_id, "sourceNumber": source_number})
        seen_questions.add(normalized_question)
        next_id += 1
    record_audit("import", "pdf_import", payload.get("sourceTitle", "PDF"),
                 {"imported": len(imported), "rejected": len(errors)})
    database.commit()
    status_code = 201 if imported else 400
    return jsonify({"imported": imported, "errors": errors, "status": "draft"}), status_code


@app.post("/api/admin/questions")
@require_admin()
def admin_create_question():
    cleaned, error = validate_question_payload(request.get_json(silent=True) or {})
    if error:
        return jsonify({"error": error}), 400
    database = get_database()
    duplicate = database.execute("SELECT id FROM questions WHERE question_text=?", (cleaned["q"],)).fetchone()
    if duplicate:
        return jsonify({"error": f"พบคำถามซ้ำกับข้อ {duplicate['id']}"}), 409
    next_id = database.execute("SELECT COALESCE(MAX(id), 0) + 1 AS id FROM questions").fetchone()["id"]
    now = utc_now()
    database.execute(
        """INSERT INTO questions(id, topic, question_text, options_json, correct_answer, explanation,
        status, is_active, difficulty, exam_frequency, source_title, source_url, verified_at, audience,
        created_by, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 'draft', 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (next_id, cleaned["topic"], cleaned["q"], json.dumps(cleaned["o"], ensure_ascii=False), cleaned["a"],
         cleaned["e"], cleaned["difficulty"], cleaned["examFrequency"], cleaned["sourceTitle"], cleaned["sourceUrl"],
         cleaned["verifiedAt"], cleaned["audience"], g.admin_user["id"], now, now),
    )
    save_question_version(next_id, "created", g.admin_user["id"])
    record_audit("create", "question", next_id, {"status": "draft"})
    database.commit()
    return jsonify({"id": next_id, "status": "draft"}), 201


@app.put("/api/admin/questions/<int:question_id>")
@require_admin()
def admin_update_question(question_id: int):
    cleaned, error = validate_question_payload(request.get_json(silent=True) or {})
    if error:
        return jsonify({"error": error}), 400
    database = get_database()
    existing = database.execute("SELECT * FROM questions WHERE id=?", (question_id,)).fetchone()
    if existing is None:
        return jsonify({"error": "ไม่พบข้อสอบ"}), 404
    if existing["status"] == "pending" and g.admin_user["role"] != "admin":
        return jsonify({"error": "ข้อสอบอยู่ระหว่างตรวจ ผู้ดูแลระบบเท่านั้นที่แก้ไขได้"}), 409
    duplicate = database.execute(
        "SELECT id FROM questions WHERE question_text=? AND id<>?", (cleaned["q"], question_id)
    ).fetchone()
    if duplicate:
        return jsonify({"error": f"พบคำถามซ้ำกับข้อ {duplicate['id']}"}), 409
    # การแก้เนื้อหาใด ๆ ต้องกลับไปผ่านการตรวจใหม่เสมอ
    new_status = "draft"
    database.execute(
        """UPDATE questions SET topic=?, question_text=?, options_json=?, correct_answer=?, explanation=?,
        difficulty=?, exam_frequency=?, source_title=?, source_url=?, verified_at=?, audience=?, status=?, is_active=0,
        approved_by=NULL, published_at=NULL, updated_at=? WHERE id=?""",
        (cleaned["topic"], cleaned["q"], json.dumps(cleaned["o"], ensure_ascii=False), cleaned["a"], cleaned["e"],
         cleaned["difficulty"], cleaned["examFrequency"], cleaned["sourceTitle"], cleaned["sourceUrl"],
         cleaned["verifiedAt"], cleaned["audience"], new_status, utc_now(), question_id),
    )
    save_question_version(question_id, "updated", g.admin_user["id"])
    record_audit("update", "question", question_id, {"status": new_status})
    database.commit()
    return jsonify({"updated": True, "status": new_status})


@app.post("/api/admin/questions/<int:question_id>/submit")
@require_admin()
def admin_submit_question(question_id: int):
    database = get_database()
    row = database.execute("SELECT status, source_title, source_url FROM questions WHERE id=?", (question_id,)).fetchone()
    if row is None:
        return jsonify({"error": "ไม่พบข้อสอบ"}), 404
    if row["status"] != "draft":
        return jsonify({"error": "ส่งตรวจได้เฉพาะข้อสอบฉบับร่าง"}), 409
    if not row["source_title"]:
        return jsonify({"error": "กรุณาระบุแหล่งอ้างอิงก่อนส่งตรวจ"}), 400
    database.execute("UPDATE questions SET status='pending', is_active=0, updated_at=? WHERE id=?", (utc_now(), question_id))
    save_question_version(question_id, "submitted", g.admin_user["id"])
    record_audit("submit", "question", question_id)
    database.commit()
    return jsonify({"status": "pending"})


@app.post("/api/admin/questions/<int:question_id>/publish")
@require_admin("admin")
def admin_publish_question(question_id: int):
    database = get_database()
    row = database.execute("SELECT status, source_title FROM questions WHERE id=?", (question_id,)).fetchone()
    if row is None:
        return jsonify({"error": "ไม่พบข้อสอบ"}), 404
    if row["status"] != "pending":
        return jsonify({"error": "เผยแพร่ได้เฉพาะข้อสอบที่รอตรวจ"}), 409
    if not row["source_title"]:
        return jsonify({"error": "ข้อสอบยังไม่มีแหล่งอ้างอิง"}), 400
    now = utc_now()
    database.execute(
        "UPDATE questions SET status='published', is_active=1, approved_by=?, published_at=?, updated_at=? WHERE id=?",
        (g.admin_user["id"], now, now, question_id),
    )
    save_question_version(question_id, "published", g.admin_user["id"])
    record_audit("publish", "question", question_id)
    database.commit()
    return jsonify({"status": "published"})


@app.post("/api/admin/questions/<int:question_id>/reject")
@require_admin("admin")
def admin_reject_question(question_id: int):
    payload = request.get_json(silent=True) or {}
    reason = " ".join(str(payload.get("reason", "")).split())[:500]
    if len(reason) < 5:
        return jsonify({"error": "กรุณาระบุเหตุผลที่ส่งกลับให้แก้ไข"}), 400
    database = get_database()
    row = database.execute("SELECT status FROM questions WHERE id=?", (question_id,)).fetchone()
    if row is None:
        return jsonify({"error": "ไม่พบข้อสอบ"}), 404
    if row["status"] != "pending":
        return jsonify({"error": "ส่งกลับได้เฉพาะข้อสอบที่รอตรวจ"}), 409
    database.execute("UPDATE questions SET status='draft', is_active=0, updated_at=? WHERE id=?", (utc_now(), question_id))
    save_question_version(question_id, "rejected", g.admin_user["id"])
    record_audit("reject", "question", question_id, {"reason": reason})
    database.commit()
    return jsonify({"status": "draft"})


@app.post("/api/admin/questions/<int:question_id>/pause")
@require_admin("admin")
def admin_pause_question(question_id: int):
    database = get_database()
    row = database.execute("SELECT status FROM questions WHERE id=?", (question_id,)).fetchone()
    if row is None:
        return jsonify({"error": "ไม่พบข้อสอบ"}), 404
    if row["status"] != "published":
        return jsonify({"error": "ระงับได้เฉพาะข้อสอบที่เผยแพร่แล้ว"}), 409
    database.execute("UPDATE questions SET status='paused', is_active=0, updated_at=? WHERE id=?", (utc_now(), question_id))
    save_question_version(question_id, "paused", g.admin_user["id"])
    record_audit("pause", "question", question_id)
    database.commit()
    return jsonify({"status": "paused"})


@app.post("/api/admin/questions/<int:question_id>/restore")
@require_admin("admin")
def admin_restore_question(question_id: int):
    database = get_database()
    row = database.execute("SELECT status FROM questions WHERE id=?", (question_id,)).fetchone()
    if row is None:
        return jsonify({"error": "ไม่พบข้อสอบ"}), 404
    if row["status"] != "paused":
        return jsonify({"error": "กู้คืนได้เฉพาะข้อสอบที่ถูกระงับ"}), 409
    now = utc_now()
    database.execute(
        "UPDATE questions SET status='published', is_active=1, approved_by=?, published_at=COALESCE(published_at, ?), updated_at=? WHERE id=?",
        (g.admin_user["id"], now, now, question_id),
    )
    save_question_version(question_id, "restored", g.admin_user["id"])
    record_audit("restore", "question", question_id)
    database.commit()
    return jsonify({"status": "published"})


@app.get("/api/admin/questions/<int:question_id>/versions")
@require_admin()
def admin_question_versions(question_id: int):
    rows = get_database().execute(
        """SELECT v.id, v.version_number, v.action, v.snapshot_json, v.created_at, a.display_name AS changed_by
        FROM question_versions v LEFT JOIN admin_users a ON a.id=v.changed_by
        WHERE v.question_id=? ORDER BY v.version_number DESC""", (question_id,)
    ).fetchall()
    return jsonify([{
        "id": row["id"], "version": row["version_number"], "action": row["action"],
        "snapshot": json.loads(row["snapshot_json"]), "createdAt": row["created_at"], "changedBy": row["changed_by"],
    } for row in rows])


@app.get("/api/admin/audit-logs")
@require_admin("admin")
def admin_audit_logs():
    rows = get_database().execute(
        """SELECT l.*, a.display_name FROM audit_logs l LEFT JOIN admin_users a ON a.id=l.admin_user_id
        ORDER BY l.id DESC LIMIT 200"""
    ).fetchall()
    return jsonify([{
        "id": row["id"], "action": row["action"], "entityType": row["entity_type"],
        "entityId": row["entity_id"], "details": json.loads(row["details_json"]),
        "adminName": row["display_name"] or "ระบบ", "createdAt": row["created_at"],
    } for row in rows])


@app.get("/api/admin/users")
@require_admin("admin")
def admin_users():
    rows = get_database().execute(
        "SELECT id, username, display_name, role, is_active, created_at, last_login_at FROM admin_users ORDER BY id"
    ).fetchall()
    return jsonify([dict(row) for row in rows])


@app.get("/api/admin/members")
@require_admin("admin")
def admin_members():
    database = get_database()
    search = request.args.get("search", "").strip()[:80]
    parameters: list[object] = []
    where = ""
    if search:
        where = "WHERE u.display_name LIKE ? OR COALESCE(u.username, '') LIKE ?"
        parameters.extend((f"%{search}%", f"%{search}%"))
    rows = database.execute(
        f"""SELECT u.id, u.display_name, u.username, u.team_name, u.created_at, u.last_login_at,
        COUNT(a.id) AS attempts,
        COALESCE(ROUND(AVG(a.score * 100.0 / a.total_questions)), 0) AS average_score,
        COALESCE(MAX(ROUND(a.score * 100.0 / a.total_questions)), 0) AS best_score,
        MAX(a.completed_at) AS last_attempt_at,
        SUM(CASE WHEN a.exam_mode='simulation' THEN 1 ELSE 0 END) AS simulation_attempts,
        SUM(CASE WHEN a.exam_mode='topic' THEN 1 ELSE 0 END) AS topic_attempts
        FROM users u LEFT JOIN attempts a ON a.user_id=u.id {where}
        GROUP BY u.id ORDER BY COALESCE(MAX(a.completed_at), u.created_at) DESC, u.id DESC LIMIT 500""",
        parameters,
    ).fetchall()
    return jsonify([dict(row) for row in rows])


@app.get("/api/admin/members/<int:member_id>/attempts")
@require_admin("admin")
def admin_member_attempts(member_id: int):
    database = get_database()
    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = int(request.args.get("perPage", 10))
    except (TypeError, ValueError):
        return jsonify({"error": "ข้อมูลหน้าไม่ถูกต้อง"}), 400
    if per_page not in {10, 20, 50}:
        return jsonify({"error": "จำนวนรายการต่อหน้าไม่ถูกต้อง"}), 400
    member = database.execute(
        "SELECT id, display_name, username, team_name, created_at, last_login_at FROM users WHERE id=?",
        (member_id,),
    ).fetchone()
    if member is None:
        return jsonify({"error": "ไม่พบสมาชิก"}), 404
    total_items = database.execute("SELECT COUNT(*) AS count FROM attempts WHERE user_id=?", (member_id,)).fetchone()["count"]
    total_pages = max(1, (total_items + per_page - 1) // per_page)
    page = min(page, total_pages)
    attempts = database.execute(
        """SELECT id, score, total_questions, duration_seconds, selected_topic, topic_scores_json,
        exam_mode, completed_at FROM attempts WHERE user_id=? ORDER BY completed_at DESC, id DESC LIMIT ? OFFSET ?""",
        (member_id, per_page, (page - 1) * per_page),
    ).fetchall()
    items = []
    for row in attempts:
        item = dict(row)
        try:
            item["topicScores"] = json.loads(item.pop("topic_scores_json"))
        except (TypeError, json.JSONDecodeError):
            item["topicScores"] = {}
            item.pop("topic_scores_json", None)
        items.append(item)
    return jsonify({"member": dict(member), "attempts": items, "pagination": {
        "page": page, "perPage": per_page, "totalItems": total_items, "totalPages": total_pages,
        "hasNext": page < total_pages,
    }})


@app.post("/api/admin/users")
@require_admin("admin")
def admin_create_user():
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username", "")).strip()[:80]
    display_name = " ".join(str(payload.get("displayName", "")).split())[:120]
    password = str(payload.get("password", ""))
    role = str(payload.get("role", "head"))
    if not re.fullmatch(r"[A-Za-z0-9._-]{4,80}", username):
        return jsonify({"error": "ชื่อผู้ใช้ต้องมี 4 ตัวขึ้นไป และใช้ตัวอักษรอังกฤษ ตัวเลข จุด ขีดกลาง หรือขีดล่าง"}), 400
    if len(display_name) < 2 or len(password) < 12 or role not in ADMIN_ROLES:
        return jsonify({"error": "กรุณากรอกชื่อ รหัสผ่านอย่างน้อย 12 ตัว และบทบาทให้ถูกต้อง"}), 400
    database = get_database()
    try:
        cursor = database.execute(
            "INSERT INTO admin_users(username, password_hash, display_name, role) VALUES (?, ?, ?, ?)",
            (username, generate_password_hash(password), display_name, role),
        )
    except sqlite3.IntegrityError:
        return jsonify({"error": "ชื่อผู้ใช้นี้มีอยู่แล้ว"}), 409
    record_audit("create", "admin_user", cursor.lastrowid, {"role": role, "displayName": display_name})
    database.commit()
    return jsonify({"id": cursor.lastrowid}), 201


@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/admin")
def admin_index():
    return send_from_directory(BASE_DIR, "admin.html")


@app.get("/<path:filename>")
def static_files(filename: str):
    if filename not in {"app.js", "styles.css", "admin.js", "admin.css", "favicon.ico", "assets/mt4-heart-logo.png", "assets/team-community-logo.png", "assets/one-team-logo.png"}:
        return jsonify({"error": "not found"}), 404
    return send_from_directory(BASE_DIR, filename)


with app.app_context():
    initialize_database()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
