import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

from syncret.core.skill import find_skills

# clone url template; tests point it at local repos
GITHUB = "https://github.com/{owner}/{repo}.git"

# names and refs never start with "-", so git cannot read them as options
_NAME = r"[\w.][\w.-]*"
_GITHUB_URL = re.compile(
    rf"(?:https?://)?(?:www\.)?github\.com/({_NAME})/({_NAME}?)(?:\.git)?"
    rf"(?:/(?:tree|blob)/({_NAME})(?:/(.*?))?)?/?"
)
_SKILLS_SH_URL = re.compile(
    rf"(?:https?://)?(?:www\.)?skills\.sh/({_NAME})/({_NAME})(?:/({_NAME}))?/?"
)
# git settings so an untrusted repo cannot run hooks or link outside the clone
_SAFE = [
    "-c", "core.symlinks=false",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false"
]


class UpstreamError(Exception):
    pass


# a skill or repo on github, from a github or skills.sh link
@dataclass(frozen=True)
class Upstream:
    owner: str
    repo: str
    ref: str = "HEAD"
    path: str = ""
    skill: str = ""

    # the upstream a link points at, or None for an unsupported link
    @classmethod
    def parse(cls, url):
        if match := _GITHUB_URL.fullmatch(url):
            owner, repo, ref, path = match.groups()
            path = (path or "").strip("/")
            if path == "SKILL.md" or path.endswith("/SKILL.md"):
                path = path.removesuffix("SKILL.md").strip("/")
            return cls(owner, repo, ref or "HEAD", path)
        if match := _SKILLS_SH_URL.fullmatch(url):
            owner, repo, skill = match.groups()
            return cls(owner, repo, skill=skill or "")
        return None

    # (skill, pinned github link) pairs from a shallow clone that is removed before returning
    def load(self):
        git = shutil.which("git")
        if not git:
            raise UpstreamError("git is not installed")
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"}

        def run(*args, cwd):
            result = subprocess.run(  # noqa: S603 - fixed git argv, url only as an argument
                [git, *_SAFE, *args], cwd=cwd, env=env, capture_output=True, text=True,
                timeout=300, check=False
            )
            if result.returncode:
                lines = result.stderr.strip().splitlines() or ["git failed"]
                raise UpstreamError(f"{self.owner}/{self.repo}: {lines[-1]}")
            return result.stdout.strip()

        source = GITHUB.format(owner=self.owner, repo=self.repo)
        with tempfile.TemporaryDirectory(prefix="syncret-") as temp:
            run("init", "-q", temp, cwd=None)
            run("fetch", "-q", "--depth", "1", "--end-of-options", source, self.ref, cwd=temp)
            run("checkout", "-q", "--detach", "FETCH_HEAD", cwd=temp)
            commit = run("rev-parse", "HEAD", cwd=temp)
            root = os.path.realpath(temp)
            start = os.path.realpath(os.path.join(root, self.path))
            if os.path.commonpath([root, start]) != root or not os.path.isdir(start):
                raise UpstreamError(f"no folder {self.path!r} in {self.owner}/{self.repo}")
            skills = find_skills(start)
            if self.skill:
                skills = [s for s in skills if self.skill in (s.name, s.path.name)]
                if not skills:
                    raise UpstreamError(f"no skill {self.skill!r} in {self.owner}/{self.repo}")
            base = f"https://github.com/{self.owner}/{self.repo}/tree/{commit}"
            return [(skill, self._link(base, root, skill)) for skill in skills]

    # github link to a skill folder at the pinned commit
    @staticmethod
    def _link(base, root, skill):
        rel = os.path.relpath(skill.path, root)
        return base if rel == "." else f"{base}/{rel}"
