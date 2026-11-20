import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path


def now():
    return datetime.now(UTC).isoformat(timespec="seconds")


# one sqlite file shared by the queue and proposals; a connection per call, wal mode
class Db:
    def __init__(self, path, schema):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("pragma journal_mode=wal")
            db.execute(schema)

    def _db(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("pragma busy_timeout=30000")
        return closing(db)
