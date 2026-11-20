import json
from dataclasses import dataclass

from syncret.server.db import Db, now

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


def _job(row):
    values = dict(row)
    values["args"] = json.loads(values["args"])
    values["result"] = json.loads(values["result"]) if values["result"] else None
    return Job(**values)


# a job queue in sqlite that survives restarts; any process may read and enqueue
class Queue(Db):
    def __init__(self, path):
        super().__init__(path, SCHEMA)

    def enqueue(self, kind, args=None, max_attempts=3):
        with self._db() as db:
            cursor = db.execute(
                "insert into jobs (kind, args, max_attempts, created) values (?, ?, ?, ?)",
                (kind, json.dumps(args or {}), max_attempts, now())
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
                (now(),)
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
                (json.dumps(result or {}), now(), job_id)
            )

    # a failed job goes back in the queue until it runs out of attempts
    def fail(self, job_id, error):
        with self._db() as db:
            db.execute(
                "update jobs set error = ?, finished = ?, state = case"
                " when attempts < max_attempts then 'queued' else 'failed' end where id = ?",
                (str(error), now(), job_id)
            )

    # jobs left running by a crash or restart go back in the queue, or fail when out of attempts
    def recover(self):
        with self._db() as db:
            db.execute(
                "update jobs set error = 'interrupted by a restart', state = case"
                " when attempts < max_attempts then 'queued' else 'failed' end"
                " where state = 'running'"
            )
