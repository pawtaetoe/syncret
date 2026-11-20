import hashlib
import json
import os
import secrets
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from syncret.core.llm import Llm, LlmError
from syncret.server.auth import secrets_dir, write_secret
from syncret.server.db import Db, now

DEFAULTS = {"check_interval_hours": 6, "sync_interval_minutes": 60}
LIMITS = {"check_interval_hours": (1, 168), "sync_interval_minutes": (5, 1440)}
# claude setup-token tokens last a year; warn a month before
TOKEN_DAYS, TOKEN_WARN_DAYS = 365, 335


# the server's settings: intervals, the llm config file and the claude token
class Settings:
    def __init__(self, data):
        self.data = Path(data)
        self.path = self.data / "settings.json"
        self.llm_path = self.data / "llm.toml"

    def load(self):
        stored = json.loads(self.path.read_text()) if self.path.is_file() else {}
        return {**DEFAULTS, **{k: v for k, v in stored.items() if k in DEFAULTS}}

    def save(self, **values):
        settings = self.load()
        for key, value in values.items():
            low, high = LIMITS[key]
            if not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"{key} must be a whole number from {low} to {high}")
            settings[key] = value
        _write(self.path, json.dumps(settings, indent=2) + "\n")
        return settings

    def llm_text(self):
        return self.llm_path.read_text() if self.llm_path.is_file() else ""

    # replace llm.toml only if the new text loads; returns the provider names
    def save_llm(self, text):
        with tempfile.TemporaryDirectory() as temp:
            trial = Path(temp) / "llm.toml"
            trial.write_text(text)
            try:
                loaded = Llm.load(trial)
            except LlmError as error:
                raise ValueError(str(error).replace(str(trial), "llm.toml")) from error
        _write(self.llm_path, text)
        return list(loaded.providers) if loaded else []

    @property
    def token_path(self):
        return secrets_dir(self.data) / "claude-token"

    def save_token(self, token):
        token = token.strip().removeprefix("CLAUDE_CODE_OAUTH_TOKEN=").strip()
        if len(token) < 20 or any(c.isspace() for c in token):
            raise ValueError("that does not look like a token from claude setup-token")
        write_secret(self.token_path, token + "\n")
        write_secret(secrets_dir(self.data) / "claude-token.json",
                     json.dumps({"saved": now()}) + "\n")

    # when the token was saved and how old it is; the token itself is never returned
    def token_info(self):
        meta = secrets_dir(self.data) / "claude-token.json"
        if not self.token_path.is_file() or not meta.is_file():
            return None
        saved = json.loads(meta.read_text())["saved"]
        days = (datetime.now(UTC) - datetime.fromisoformat(saved)).days
        return {"saved": saved, "days": days, "expires_in": TOKEN_DAYS - days,
                "warn": days >= TOKEN_WARN_DAYS}


# write a settings file atomically
def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".partial")
    partial.write_text(text)
    os.replace(partial, path)


MACHINES = """
create table if not exists machines (
    id integer primary key,
    name text not null,
    token_hash text not null unique,
    created text not null,
    last_sync text,
    release text,
    revoked text
)
"""


# one client machine allowed to fetch releases
@dataclass(frozen=True)
class Machine:
    id: int
    name: str
    token_hash: str
    created: str
    last_sync: str | None
    release: str | None
    revoked: str | None


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


# machine tokens: shown once at creation, stored only as a sha256 of a random 256-bit token
class Machines(Db):
    def __init__(self, path):
        super().__init__(path, MACHINES)

    def create(self, name):
        name = name.strip()
        if not name or len(name) > 64:
            raise ValueError("give the machine a name of up to 64 characters")
        token = "syncret_" + secrets.token_urlsafe(32)
        with self._db() as db:
            db.execute("insert into machines (name, token_hash, created) values (?, ?, ?)",
                       (name, _hash(token), now()))
        return token

    def list(self):
        with self._db() as db:
            return [Machine(**dict(row)) for row in
                    db.execute("select * from machines order by revoked is not null, id")]

    def revoke(self, machine_id):
        with self._db() as db:
            db.execute("update machines set revoked = ? where id = ? and revoked is null",
                       (now(), machine_id))

    # the machine a token belongs to, unless it is unknown or revoked
    def check(self, token):
        if not token:
            return None
        with self._db() as db:
            row = db.execute("select * from machines where token_hash = ? and revoked is null",
                             (_hash(token),)).fetchone()
            return Machine(**dict(row)) if row else None

    def synced(self, machine_id, release):
        with self._db() as db:
            db.execute("update machines set last_sync = ?, release = ? where id = ?",
                       (now(), release, machine_id))


ACCOUNT = """
create table if not exists account_uploads (
    key text primary key,
    tree text not null,
    uploaded text not null
)
"""


# which version of each account-targeted skill was last uploaded to the claude account
class AccountUploads(Db):
    def __init__(self, path):
        super().__init__(path, ACCOUNT)

    def all(self):
        with self._db() as db:
            return {row["key"]: dict(row) for row in db.execute("select * from account_uploads")}

    def mark(self, key, tree):
        with self._db() as db:
            db.execute("insert into account_uploads (key, tree, uploaded) values (?, ?, ?)"
                       " on conflict(key) do update set tree = excluded.tree,"
                       " uploaded = excluded.uploaded", (key, tree, now()))
