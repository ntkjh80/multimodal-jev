"""SQLite 저장소. 모델 설정·실험 스냅샷과 개별 관측치를 영속화합니다.

연결을 작업마다 열어 Windows/macOS의 스레드 제한을 피합니다. WAL과
busy_timeout은 짧은 쓰기 작업이 겹칠 때 대기하도록 하며 GPU 추론 중에는
DB 트랜잭션을 열어 두지 않습니다.
"""
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import default_models


def now():
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix):
    return prefix + "_" + uuid.uuid4().hex[:16]


def dumps(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


class Store:
    """테이블 이름은 코드의 고정 목록만 허용하고 값은 SQL 매개변수로 전달합니다."""
    TABLES = {"models", "datasets", "assets", "runs"}

    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / "assets").mkdir(exist_ok=True)
        self.path = self.directory / "bench.sqlite3"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            for table in self.TABLES:
                db.execute("CREATE TABLE IF NOT EXISTS " + table + " (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS results (run_id TEXT, model_id TEXT, sample_id TEXT, repetition INTEGER, payload TEXT NOT NULL, PRIMARY KEY (run_id, model_id, sample_id, repetition))")
        for model in default_models():
            if not self.get("models", model["id"]):
                self.put("models", model)
        # 프로세스가 종료되었다면 실행 중이었다고 계속 표시하지 않습니다.
        for run in self.list("runs"):
            if run["status"] in ("running", "queued", "stopping"):
                run.update(status="interrupted", updated_at=now())
                self.put("runs", run)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=20)
        db.execute("PRAGMA busy_timeout=20000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def put(self, table, payload):
        assert table in self.TABLES
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO " + table + " VALUES (?, ?)", (payload["id"], dumps(payload)))
        return payload

    def get(self, table, item_id):
        assert table in self.TABLES
        with self.connect() as db:
            row = db.execute("SELECT payload FROM " + table + " WHERE id=?", (item_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, table):
        assert table in self.TABLES
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM " + table + " ORDER BY rowid DESC").fetchall()
        return [json.loads(row[0]) for row in rows]

    def result(self, payload):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO results VALUES (?,?,?,?,?)", (
                payload["run_id"], payload["model_id"], payload["sample_id"], payload["repetition"], dumps(payload)))

    def results(self, run_id):
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM results WHERE run_id=? ORDER BY rowid", (run_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]
