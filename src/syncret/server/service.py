import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from syncret.core.llm import Llm
from syncret.core.merge import MergeError, Source, merge
from syncret.core.review import reviewed
from syncret.core.scan import scan
from syncret.core.skill import Skill, find_skills
from syncret.core.upstream import Upstream
from syncret.core.vet import vet
from syncret.server.db import now
from syncret.server.proposals import Proposals
from syncret.server.queue import Queue
from syncret.server.repo import Repo, Stale

# scan labels that mean a new skill and a published one share a job, so a merge is suggested
MERGE_LABELS = {"duplicate", "overlapping", "conflicting"}
TARGETS = {"local", "account"}


# a request that cannot be queued, with the reason to show
class Refused(Exception):
    pass


# one skill fetched for adding: the skill, its key in the repo, and its skills.json entry
@dataclass
class Found:
    skill: Skill
    key: str
    entry: dict


# a folder or file name safe to use in the repo
def safe(name):
    return re.sub(r"[^a-z0-9._-]+", "-", name.lower()).strip("-.") or "skill"


# repo files for a skill under key; scripts keep their executable bit
def skill_files(key, skill):
    return {f"{key}/{rel}": (data, 0o755 if data.startswith(b"#!") else 0o644)
            for rel, data in skill.files.items()}


# the server: its data folder, queue, proposals and repo, the requests it accepts and the jobs
# its worker runs; requests only read and queue, jobs are the only writers
class Service:
    def __init__(self, data):
        self.data = Path(data)
        self.queue = Queue(self.data / "syncret.db")
        self.proposals = Proposals(self.data / "syncret.db")
        self.repo = Repo(self.data)
        self.releases = self.data / "releases"

    def llm(self):
        return Llm.load(self.data / "llm.toml")

    @property
    def handlers(self):
        return {"add": self._add, "vet": self._vet, "scan": self._scan, "merge": self._merge,
                "approve": self._approve, "publish": self._publish, "reject": self._reject,
                "remove": self._remove, "edit": self._edit, "scan-all": self._scan_all}

    # requests: validate, then queue a job and return its id

    def add(self, source, skills=(), targets=("local",)):
        if not source.strip():
            raise Refused("give a GitHub or skills.sh link, or a path on the server")
        if not set(targets) <= TARGETS or not targets:
            raise Refused(f"targets must be some of {sorted(TARGETS)}")
        args = {"source": source.strip(), "skills": list(skills), "targets": sorted(targets)}
        return self.queue.enqueue("add", args)

    # approve only an open, unblocked proposal whose base is still main; a stale one is rebuilt
    def approve(self, proposal_id, base):
        proposal = self._open(proposal_id)
        if proposal.blocked:
            raise Refused(f"proposal {proposal_id} is blocked by its vetting report")
        if base != proposal.base:
            raise Refused(f"base {base[:12]} is not the base of proposal {proposal_id}")
        if self.repo.head() != base:
            self._rebuild(proposal, "main moved past its base")
            raise Refused(f"proposal {proposal_id} is stale: main moved past its base; rebuilding")
        self.proposals.update(proposal_id, state="approving")
        return self.queue.enqueue("approve", {"proposal": proposal_id, "base": base})

    def reject(self, proposal_id):
        self._open(proposal_id)
        return self.queue.enqueue("reject", {"proposal": proposal_id})

    def remove(self, key):
        if key not in self.repo.skills()["skills"]:
            raise Refused(f"no skill {key!r}")
        return self.queue.enqueue("remove", {"key": key})

    # edits are for own skills only; upstream copies stay unchanged
    def edit(self, key, content):
        entry = self.repo.skills()["skills"].get(key)
        if entry is None or entry["kind"] != "own":
            raise Refused(f"{key!r} is not an own skill; only own skills can be edited")
        return self.queue.enqueue("edit", {"key": key, "content": content})

    def propose_merge(self, keys, name):
        missing = [key for key in keys if key not in self.repo.skills()["skills"]]
        if len(keys) < 2 or missing:
            raise Refused(f"merge needs two or more published skills; unknown {missing}")
        return self.queue.enqueue("merge", {"keys": list(keys), "name": name})

    def scan_all(self):
        return self.queue.enqueue("scan-all")

    # reads

    def show(self, proposal_id):
        proposal = self.proposals.get(proposal_id)
        if proposal is None:
            raise Refused(f"no proposal {proposal_id}")
        diff = ""
        if proposal.base and proposal.state in ("checking", "open", "approving"):
            diff = self.repo.diff(proposal.base, f"proposal/{proposal_id}")
        return proposal, diff

    def latest_release(self):
        path = self.releases / "latest.json"
        return json.loads(path.read_text()) if path.is_file() else None

    def _open(self, proposal_id):
        proposal = self.proposals.get(proposal_id)
        if proposal is None:
            raise Refused(f"no proposal {proposal_id}")
        if proposal.state != "open":
            raise Refused(f"proposal {proposal_id} is {proposal.state}, not open")
        return proposal

    # mark a proposal stale and queue the request that built it again
    def _rebuild(self, proposal, reason):
        self.proposals.update(proposal.id, state="stale", error=reason)
        origin = proposal.origin
        if origin.get("job"):
            job = self.queue.enqueue(origin["job"], origin["args"])
            self.proposals.note(proposal.id, f"rebuilt by job {job}")

    # a new proposal branch from main; returns its id
    def _propose(self, kind, title, origin, files, entries, keys, ctx):
        proposal_id = self.proposals.create(kind, title, origin)
        base = ctx.repo.head()
        head = ctx.repo.propose(str(proposal_id), base, files, entries, title)
        self.proposals.update(proposal_id, state="checking", base=base, head=head, skills=keys)
        ctx.log(f"proposal {proposal_id}: {title}")
        return proposal_id

    # a proposal moves to open once vet and scan have both reported
    def _settle(self, proposal_id):
        proposal = self.proposals.get(proposal_id)
        if proposal and proposal.state == "checking" and proposal.vet is not None \
                and proposal.scan is not None:
            self.proposals.update(proposal_id, state="open")

    # jobs: run by the worker, the only writer

    # skills from a link (upstream, pinned) or a path on the server (own)
    def _fetch(self, source, targets):
        upstream = Upstream.parse(source)
        if upstream:
            found = []
            for skill, link in upstream.load():
                commit = link.split("/tree/")[1].split("/")[0]
                key = f"skills/upstream/{safe(upstream.owner)}/{safe(upstream.repo)}/" \
                      f"{safe(skill.path.name)}"
                found.append(Found(skill, key, {"name": skill.name, "kind": "upstream",
                                                "path": key, "source": link, "pin": commit,
                                                "targets": targets}))
            return found
        if Path(source).is_dir():
            return [Found(skill, f"skills/own/{safe(skill.name)}",
                          {"name": skill.name, "kind": "own", "source": "local", "pin": "",
                           "path": f"skills/own/{safe(skill.name)}", "targets": targets})
                    for skill in find_skills(source)]
        raise ValueError(f"not a supported link or a folder on the server: {source}")

    # one proposal per chosen skill; with several skills and none chosen, return the choices
    def _add(self, ctx):
        args = ctx.job.args
        found = self._fetch(args["source"], args["targets"])
        chosen = args.get("skills") or []
        if chosen:
            found = [f for f in found if f.skill.name in chosen or f.skill.path.name in chosen]
        elif len(found) > 1:
            return {"choose": sorted(f.skill.name for f in found)}
        if not found:
            raise ValueError(f"no skill to add from {args['source']}")
        current = ctx.repo.skills()["skills"]
        made = []
        for f in found:
            kind = "update" if f.key in current else "add"
            files = {**ctx.repo.removal(ctx.repo.head(), f.key), **skill_files(f.key, f.skill)}
            origin = {"job": "add", "args": {**args, "skills": [f.skill.name]}}
            proposal_id = self._propose(kind, f"{kind} {f.skill.name}", origin, files,
                                        {f.key: f.entry}, [f.key], ctx)
            self.queue.enqueue("vet", {"proposal": proposal_id})
            self.queue.enqueue("scan", {"proposal": proposal_id})
            made.append(proposal_id)
        return {"proposals": made}

    # rule vetting, plus an llm review when one is configured; own skills stay private
    def _vet(self, ctx):
        proposal_id = ctx.job.args["proposal"]
        proposal = self.proposals.get(proposal_id)
        if proposal is None:
            raise ValueError(f"no proposal {proposal_id}")
        llm, ref = self.llm(), f"proposal/{proposal_id}"
        entries = ctx.repo.skills(ref)["skills"]
        reports = []
        with tempfile.TemporaryDirectory() as temp:
            for key in proposal.skills:
                if not ctx.repo.files(ref, key):
                    continue
                skill = Skill(ctx.repo.export(ref, key, temp))
                report = replace(vet(skill), path=key)
                if llm is not None:
                    own = entries.get(key, {}).get("kind") != "upstream"
                    report, note = reviewed(skill, report, llm, own)
                    self.proposals.note(proposal_id, note)
                reports.append(report.to_dict())
        self.proposals.update(proposal_id, vet=reports)
        self._settle(proposal_id)
        return {"verdicts": [r["verdict"] for r in reports]}

    # compare the proposed skills with every published one; overlaps suggest a merge
    def _scan(self, ctx):
        proposal_id = ctx.job.args["proposal"]
        proposal = self.proposals.get(proposal_id)
        if proposal is None:
            raise ValueError(f"no proposal {proposal_id}")
        ref, llm = f"proposal/{proposal_id}", self.llm()
        with tempfile.TemporaryDirectory() as temp:
            published, proposed = Path(temp) / "published", Path(temp) / "proposed"
            proposed.mkdir()
            if ctx.repo.files("published", "skills"):
                ctx.repo.export("published", "skills", published)
            for key in proposal.skills:
                if (published / key).exists():
                    shutil.rmtree(published / key)
                if ctx.repo.files(ref, key):
                    ctx.repo.export(ref, key, proposed)
            report = scan([str(published), str(proposed)], llm, own=True)

            def key_of(path):
                for root, side in ((proposed, "proposed"), (published, "published")):
                    if Path(path).is_relative_to(root):
                        return side, str(Path(path).relative_to(root))
                return "?", path

            def mapped(paths):
                sides = [key_of(p) for p in paths]
                if not any(side == "proposed" for side, _ in sides):
                    return None
                return [f"{side}:{key}" for side, key in sides]

            result = {"duplicates": [], "collisions": [], "overlaps": [], "notes": report.notes}
            for group, name in ((report.duplicates, "duplicates"),
                                (report.collisions, "collisions")):
                for item in group:
                    paths = mapped(item.paths)
                    if paths:
                        result[name].append({**vars(item), "paths": paths})
            for overlap in report.overlaps:
                paths = mapped(overlap.paths)
                if paths:
                    result["overlaps"].append({**vars(overlap), "paths": paths})
        self.proposals.update(proposal_id, scan=result)
        self._settle(proposal_id)
        if proposal.kind in ("add", "update") and llm is not None:
            for overlap in result["overlaps"]:
                existing = [p.split(":", 1)[1] for p in overlap["paths"]
                            if p.startswith("published:")]
                if overlap["label"] in MERGE_LABELS and existing:
                    job = self.queue.enqueue("merge", {"proposal": proposal_id,
                                                       "with": existing[0]})
                    self.proposals.note(proposal_id, f"{overlap['label']} with {existing[0]}: "
                                                     f"merge suggested by job {job}")
        return {"overlaps": len(result["overlaps"]), "collisions": len(result["collisions"])}

    # merge a proposed skill with a published one, or several published skills, into a new
    # proposal; the sources stay in the repo with no targets, so agents never load both
    def _merge(self, ctx):
        args, llm = ctx.job.args, self.llm()
        if llm is None:
            raise ValueError("merge needs an LLM; none configured")
        published = ctx.repo.skills("published")["skills"]
        with tempfile.TemporaryDirectory() as temp:
            sources, entries, files = [], {}, {}
            picks = [("published", key) for key in args.get("keys", [])]
            if "proposal" in args:
                proposal = self.proposals.get(args["proposal"])
                if proposal is None:
                    raise ValueError(f"no proposal {args['proposal']}")
                ref = f"proposal/{proposal.id}"
                picks = [(ref, key) for key in proposal.skills] + [("published", args["with"])]
            for index, (ref, key) in enumerate(picks):
                skill = Skill(ctx.repo.export(ref, key, Path(temp) / str(index)))
                entry = ctx.repo.skills(ref)["skills"][key]
                sources.append(Source(skill, entry["source"], commit=entry.get("pin", "")))
                entries[key] = {**entry, "targets": []}
                if ref != "published":
                    files |= skill_files(key, skill)
            first = published.get(picks[-1][1], {}).get("name", "merged")
            name = safe(args.get("name") or first)
            try:
                merged = merge(sources, name, Path(temp) / "out", llm, own=True)
            except MergeError as error:
                detail = "; ".join([str(error), *error.unexplained])
                if "proposal" in args:
                    self.proposals.note(args["proposal"], f"merge refused: {detail}")
                raise ValueError(f"merge refused: {detail}") from error
            key = f"skills/merged/{name}"
            files |= {**ctx.repo.removal(ctx.repo.head(), key),
                      **skill_files(key, Skill(merged.out))}
            entries[key] = {"name": name, "kind": "merged", "path": key, "source": "merge",
                            "pin": "", "targets": ["local"], "sources": [k for _, k in picks]}
        proposal_id = self._propose("merge", f"merge {name}", {"job": "merge", "args": args},
                                    files, entries, [key], ctx)
        self.proposals.update(proposal_id, scan={"note": "merge sources are kept with no targets"})
        if merged.untraced:
            self.proposals.note(proposal_id, f"{len(merged.untraced)} merged sentence(s) trace "
                                             "to no source; check them in the diff")
        self.queue.enqueue("vet", {"proposal": proposal_id})
        if "proposal" in args:
            self.proposals.note(args["proposal"], f"suggested merge: proposal {proposal_id}")
        return {"proposal": proposal_id}

    # move main to the proposal with git's compare-and-swap, then publish; stale ones rebuild
    def _approve(self, ctx):
        proposal_id, base = ctx.job.args["proposal"], ctx.job.args["base"]
        proposal = self.proposals.get(proposal_id)
        if proposal is None or proposal.state != "approving":
            raise ValueError(f"proposal {proposal_id} is not waiting for approval")
        try:
            ctx.repo.approve(str(proposal_id), base)
        except Stale:
            self._rebuild(proposal, "main moved past its base before approval")
            return {"stale": True}
        ctx.repo.drop(str(proposal_id))
        self.proposals.update(proposal_id, state="approved")
        self.queue.enqueue("publish")
        return {"approved": proposal_id}

    # move published to main and pack the release; safe to run again after a crash
    def _publish(self, ctx):
        old, new = ctx.repo.head("published"), ctx.repo.head()
        if old != new:
            self.releases.mkdir(parents=True, exist_ok=True)
            release = self.releases / f"{new[:12]}.tar.gz"
            partial = release.with_suffix(".partial")
            ctx.repo.archive(new, partial)
            os.replace(partial, release)
            ctx.repo.publish(new, old)
        release = self.releases / f"{new[:12]}.tar.gz"
        if release.is_file():
            latest = {"id": new[:12], "commit": new, "created": now(),
                      "sha256": hashlib.sha256(release.read_bytes()).hexdigest()}
            partial = self.releases / "latest.partial"
            partial.write_text(json.dumps(latest, indent=2) + "\n")
            os.replace(partial, self.releases / "latest.json")
        for proposal in self.proposals.list(states=["approved"]):
            self.proposals.update(proposal.id, state="published")
        return {"release": new[:12]}

    def _reject(self, ctx):
        proposal_id = ctx.job.args["proposal"]
        ctx.repo.drop(str(proposal_id))
        self.proposals.update(proposal_id, state="rejected")
        return {"rejected": proposal_id}

    def _remove(self, ctx):
        key = ctx.job.args["key"]
        files = ctx.repo.removal(ctx.repo.head(), key)
        proposal_id = self._propose("remove", f"remove {key}", {"job": "remove",
                                                                "args": ctx.job.args},
                                    files, {key: None}, [key], ctx)
        self.proposals.update(proposal_id, vet=[], scan={})
        self._settle(proposal_id)
        return {"proposal": proposal_id}

    def _edit(self, ctx):
        key, content = ctx.job.args["key"], ctx.job.args["content"]
        files = {f"{key}/SKILL.md": content.encode()}
        proposal_id = self._propose("edit", f"edit {key}", {"job": "edit", "args": ctx.job.args},
                                    files, {}, [key], ctx)
        self.proposals.update(proposal_id, scan={})
        self.queue.enqueue("vet", {"proposal": proposal_id})
        return {"proposal": proposal_id}

    # a full scan of the published skills, reported in the job's result
    def _scan_all(self, ctx):
        with tempfile.TemporaryDirectory() as temp:
            if not ctx.repo.files("published", "skills"):
                return {"skills": 0}
            root = ctx.repo.export("published", "skills", temp)
            report = scan([str(root)], self.llm(), own=True).to_dict()
        return json.loads(json.dumps(report).replace(str(root), "skills"))
