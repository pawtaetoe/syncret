import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
create table if not exists jobs (
    id integer primary key,
    kind text not null,
    args text not null,
    state text not null default 'queued',
    attempts integer not null default 0,
    max_attempts integer not null default 3,
    log text not null default '',
    result text,
    error text,
    created text not null,
    started text,
    finished text
)
"""


# one job as stored
@dataclass(frozen=True)
class Job:
    id: int
    kind: str
    args: dict
    state: str
    attempts: int
    max_attempts: int
    log: str
    result: dict | None
    error: str | None
    created: str
    started: str | None
    finished: str | None


def _now():
    return datetime.now(UTC).isoformat(timespec="seconds")


def _job(row):
    values = dict(row)
    values["args"] = json.loads(values["args"])
    values["result"] = json.loads(values["result"]) if values["result"] else None
    return Job(**values)


# a job queue in sqlite that survives restarts; any process may read and enqueue
class Queue:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("pragma journal_mode=wal")
            db.execute(SCHEMA)

    def _db(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("pragma busy_timeout=30000")
        return closing(db)

    def enqueue(self, kind, args=None, max_attempts=3):
        with self._db() as db:
            cursor = db.execute(
                "insert into jobs (kind, args, max_attempts, created) values (?, ?, ?, ?)",
                (kind, json.dumps(args or {}), max_attempts, _now())
            )
            return cursor.lastrowid

    def get(self, job_id):
        with self._db() as db:
            row = db.execute("select * from jobs where id = ?", (job_id,)).fetchone()
            return _job(row) if row else None

    def jobs(self, limit=100):
        with self._db() as db:
            rows = db.execute("select * from jobs order by id desc limit ?", (limit,))
            return [_job(row) for row in rows]

    # the oldest queued job, now running; one statement, so two claimers never get the same job
    def claim(self):
        with self._db() as db:
            row = db.execute(
                "update jobs set state = 'running', attempts = attempts + 1, started = ?"
                " where id = (select id from jobs where state = 'queued' order by id limit 1)"
                " returning *",
                (_now(),)
            ).fetchone()
            return _job(row) if row else None

    def log(self, job_id, line):
        with self._db() as db:
            db.execute("update jobs set log = log || ? where id = ?",
                       (line.rstrip() + "\n", job_id))

    def finish(self, job_id, result=None):
        with self._db() as db:
            db.execute(
                "update jobs set state = 'done', result = ?, finished = ? where id = ?",
                (json.dumps(result or {}), _now(), job_id)
            )

    # a failed job goes back in the queue until it runs out of attempts
    def fail(self, job_id, error):
        with self._db() as db:
            db.execute(
                "update jobs set error = ?, finished = ?, state = case"
                " when attempts < max_attempts then 'queued' else 'failed' end where id = ?",
                (str(error), _now(), job_id)
            )

    # jobs left running by a crash or restart go back in the queue, or fail when out of attempts
    def recover(self):
        with self._db() as db:
            db.execute(
                "update jobs set error = 'interrupted by a restart', state = case"
                " when attempts < max_attempts then 'queued' else 'failed' end"
                " where state = 'running'"
            )
