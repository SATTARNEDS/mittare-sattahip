"""จุดเริ่มต้น WSGI สำหรับเว็บ Mittare Sattahip และ Exam Coach บนโดเมนเดียวกัน."""

from werkzeug.middleware.dispatcher import DispatcherMiddleware

from mittare_site.app import app as site_app
from server import app as exam_app


# เว็บบริการอยู่ที่ / และระบบฝึกข้อสอบแยก namespace ไว้ที่ /exam
application = DispatcherMiddleware(site_app, {"/exam": exam_app})


if __name__ == "__main__":
    from werkzeug.serving import run_simple

    run_simple("127.0.0.1", 5000, application, use_reloader=True, use_debugger=True)
