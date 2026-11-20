import fcntl
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path

# git settings so the server never runs hooks or waits on a prompt
_SAFE = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
         "-c", "user.name=syncret", "-c", "user.email=syncret@localhost"]
EMPTY = {"skills": {}}


# the repo is locked by another writer, or a write was tried without the lock
class RepoLocked(Exception):
    pass


# main moved since the proposal was built, so approving it would publish unreviewed changes
class Stale(Exception):
    pass


# the skills repo: a bare git repo that only the lock holder writes to
class Repo:
    def __init__(self, data):
        self.data = Path(data)
        self.path = self.data / "repo.git"
        self._lock = None

    def git(self, *args, stdin=None, env=None):
        result = subprocess.run(  # noqa: S603 - fixed git argv
            [shutil.which("git") or "git", *_SAFE, *args], cwd=self.path,
            input=stdin, capture_output=True, check=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})}
        )
        if result.returncode:
            raise RuntimeError(result.stderr.decode(errors="replace").strip() or "git failed")
        return result.stdout

    def text(self, *args, env=None):
        return self.git(*args, env=env).decode().strip()

    # hold the writer lock until released; a second writer fails at once instead of waiting
    @contextmanager
    def locked(self):
        self.data.mkdir(parents=True, exist_ok=True)
        handle = (self.data / "repo.lock").open("w")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            handle.close()
            raise RepoLocked("another syncret worker holds the repo lock") from error
        self._lock = handle
        try:
            yield self
        finally:
            self._lock = None
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()

    def _writer(self):
        if self._lock is None:
            raise RepoLocked("repo writes need the lock")

    # create the repo with an empty skills.json on main and published, once
    def init(self):
        self._writer()
        if self.path.exists():
            return
        self.path.mkdir(parents=True)
        self.git("init", "--bare", "--quiet", "--initial-branch=main", ".")
        commit = self.commit_tree(None, {"skills.json": _json(EMPTY)}, "init")
        self.git("update-ref", "refs/heads/main", commit)
        self.git("update-ref", "refs/heads/published", commit)

    def head(self, branch="main"):
        return self.text("rev-parse", "--verify", f"refs/heads/{branch}")

    def skills(self, ref="main"):
        return json.loads(self.git("show", f"{ref}:skills.json"))

    # every file path under a folder at a ref
    def files(self, ref, folder):
        out = self.text("ls-tree", "-r", "--name-only", ref, "--", folder)
        return out.splitlines() if out else []

    # a commit on top of parent with files added, replaced or (value None) removed
    def commit_tree(self, parent, changes, message):
        self._writer()
        with tempfile.TemporaryDirectory() as temp:
            env = {"GIT_INDEX_FILE": str(Path(temp) / "index")}
            if parent:
                self.git("read-tree", parent, env=env)
            for path, value in sorted(changes.items()):
                if value is None:
                    # a zero mode and hash removes the entry; works without a work tree
                    line = f"0 {'0' * 40}\t{path}\n".encode()
                    self.git("update-index", "--index-info", stdin=line, env=env)
                    continue
                data, mode = value if isinstance(value, tuple) else (value, 0o644)
                blob = self.git("hash-object", "-w", "--stdin", stdin=data).decode().strip()
                kind = "100755" if mode & 0o111 else "100644"
                self.git("update-index", "--add", "--cacheinfo", f"{kind},{blob},{path}", env=env)
            tree = self.text("write-tree", env=env)
        args = ["commit-tree", tree, "-m", message] + (["-p", parent] if parent else [])
        return self.text(*args)

    # a proposal branch from base: files changed, skills.json entries set or (None) removed
    def propose(self, proposal, base, files, entries, message):
        skills = self.skills(base)
        for key, entry in entries.items():
            if entry is None:
                skills["skills"].pop(key, None)
            else:
                skills["skills"][key] = entry
        commit = self.commit_tree(base, {**files, "skills.json": _json(skills)}, message)
        self.git("update-ref", f"refs/heads/proposal/{proposal}", commit)
        return commit

    # removals for every file under a folder, for propose()
    def removal(self, ref, folder):
        return dict.fromkeys(self.files(ref, folder))

    def diff(self, base, ref):
        return self.git("diff", "--no-color", "--no-ext-diff", "--stat", "--patch",
                        base, ref).decode(errors="replace")

    # move main to the proposal only if main still points at base; atomic in git
    def approve(self, proposal, base):
        self._writer()
        commit = self.head(f"proposal/{proposal}")
        try:
            self.git("update-ref", "refs/heads/main", commit, base)
        except RuntimeError as error:
            raise Stale(f"main moved past {base[:12]}; rebuild the proposal") from error
        return commit

    def drop(self, proposal):
        self._writer()
        self.git("update-ref", "-d", f"refs/heads/proposal/{proposal}")

    # move published to commit only if it still points at old
    def publish(self, commit, old):
        self._writer()
        try:
            self.git("update-ref", "refs/heads/published", commit, old)
        except RuntimeError as error:
            raise Stale("published moved during publish") from error

    # the tree at ref as a gzipped tarball, written to path
    def archive(self, ref, path):
        self._writer()
        self.git("archive", "--format=tar.gz", f"--output={path}", ref)

    # the files under folder at ref, unpacked into a new temp folder for reading
    def export(self, ref, folder, into):
        tar = self.git("archive", "--format=tar", ref, "--", folder)
        with tempfile.TemporaryFile() as handle:
            handle.write(tar)
            handle.seek(0)
            with tarfile.open(fileobj=handle) as archive:
                archive.extractall(into, filter="data")
        return Path(into) / folder


def _json(value):
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
