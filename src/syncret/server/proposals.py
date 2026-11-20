import json
from dataclasses import dataclass

from syncret.server.db import Db, now

SCHEMA = """
create table if not exists proposals (
    id integer primary key,
    kind text not null,
    title text not null,
    state text not null default 'building',
    base text,
    head text,
    skills text not null default '[]',
    origin text not null default '{}',
    vet text,
    scan text,
    notes text not null default '[]',
    error text,
    created text not null,
    updated text not null
)
"""
JSON_FIELDS = ("skills", "origin", "vet", "scan", "notes")
COLUMNS = {"kind", "title", "state", "base", "head", "error", *JSON_FIELDS}
# building: being written; checking: vet and scan pending; open: ready for review
STATES = ("building", "checking", "open", "approving", "approved", "published", "rejected",
          "stale", "failed")


# one proposal: what it changes, the commit it was built on, and its reports
@dataclass(frozen=True)
class Proposal:
    id: int
    kind: str
    title: str
    state: str
    base: str | None
    head: str | None
    skills: list
    origin: dict
    vet: list | None
    scan: dict | None
    notes: list
    error: str | None
    created: str
    updated: str

    # a proposal with a blocked vet report can never be approved
    @property
    def blocked(self):
        return any(report["verdict"] == "block" for report in self.vet or [])


def _proposal(row):
    values = dict(row)
    for key in JSON_FIELDS:
        if values[key] is not None:
            values[key] = json.loads(values[key])
    return Proposal(**values)


# proposals in the shared sqlite file
class Proposals(Db):
    def __init__(self, path):
        super().__init__(path, SCHEMA)

    def create(self, kind, title, origin):
        with self._db() as db:
            stamp = now()
            cursor = db.execute(
                "insert into proposals (kind, title, origin, created, updated)"
                " values (?, ?, ?, ?, ?)",
                (kind, title, json.dumps(origin), stamp, stamp)
            )
            return cursor.lastrowid

    def get(self, proposal_id):
        with self._db() as db:
            row = db.execute("select * from proposals where id = ?", (proposal_id,)).fetchone()
            return _proposal(row) if row else None

    def list(self, states=None, limit=200):
        with self._db() as db:
            if states:
                marks = ",".join("?" * len(states))
                rows = db.execute(f"select * from proposals where state in ({marks})"  # noqa: S608
                                  " order by id desc limit ?", (*states, limit))
            else:
                rows = db.execute("select * from proposals order by id desc limit ?", (limit,))
            return [_proposal(row) for row in rows]

    # set fields; json fields are encoded here
    def update(self, proposal_id, **fields):
        unknown = set(fields) - COLUMNS
        if unknown:
            raise ValueError(f"unknown proposal fields {sorted(unknown)}")
        if "state" in fields and fields["state"] not in STATES:
            raise ValueError(f"unknown state {fields['state']!r}")
        values = {k: json.dumps(v) if k in JSON_FIELDS else v for k, v in fields.items()}
        values["updated"] = now()
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self._db() as db:
            # column names come only from COLUMNS, values are bound
            db.execute(f"update proposals set {assignments} where id = ?",  # noqa: S608
                       (*values.values(), proposal_id))

    def note(self, proposal_id, line):
        proposal = self.get(proposal_id)
        if proposal is not None:
            self.update(proposal_id, notes=[*proposal.notes, line])
