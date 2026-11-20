import hashlib
import os
import re
from pathlib import Path

import yaml

_FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)^---[ \t]*\r?$\n?", re.S | re.M)


# one skill: a directory holding SKILL.md, read once and never executed
class Skill:
    def __init__(self, path):
        self.path = Path(path)
        self.files = {}
        self.links = {}
        self.frontmatter = {}
        self.text = ""
        self.body = ""
        self.error = None
        self._read_files()
        self._parse()
        self.hash = self._hash()

    @property
    def name(self):
        name = self.frontmatter.get("name")
        return name if isinstance(name, str) and name else self.path.name

    @property
    def description(self):
        description = self.frontmatter.get("description")
        return description if isinstance(description, str) else ""

    # every file under the skill keyed by relative path, symlinks resolved, loops skipped;
    # a symlink that leaves the skill is recorded in links and never read
    def _read_files(self):
        errors = []
        root = self.path.resolve()

        def visit(directory, prefix, ancestors):
            try:
                children = sorted(directory.iterdir())
            except OSError as error:
                errors.append(f"{prefix or '.'}: {error.strerror}")
                return
            for child in children:
                rel = prefix + child.name
                try:
                    real = child.resolve()
                    if child.is_symlink() and real != root and root not in real.parents:
                        self.links[rel] = os.readlink(child)
                    elif child.is_dir():
                        if child.name != ".git" and real not in ancestors:
                            visit(child, rel + "/", ancestors | {real})
                    elif child.is_file():
                        self.files[rel] = child.read_bytes()
                except OSError as error:
                    errors.append(f"{rel}: {error.strerror}")

        visit(self.path, "", {root})
        if errors:
            self.error = "unreadable: " + ", ".join(errors)

    # split SKILL.md into yaml frontmatter and body
    def _parse(self):
        self.text = self.files.get("SKILL.md", b"").decode("utf-8-sig", errors="replace")
        match = _FRONTMATTER.match(self.text)
        if not match:
            self.body = self.text
            self.error = self.error or "missing frontmatter"
            return
        self.body = self.text[match.end():]
        try:
            frontmatter = yaml.safe_load(match.group(1))
        except yaml.YAMLError as error:
            self.error = self.error or "broken frontmatter: " + " ".join(str(error).split())
            return
        if not isinstance(frontmatter, dict):
            self.error = self.error or "frontmatter is not a mapping"
            return
        self.frontmatter = frontmatter
        missing = [key for key in ("name", "description") if not frontmatter.get(key)]
        if missing:
            self.error = self.error or f"frontmatter has no {' or '.join(missing)}"

    # sha256 over relative paths, contents and outside link targets, so copies match
    def _hash(self):
        digest = hashlib.sha256()
        for rel in sorted(self.files):
            data = self.files[rel]
            digest.update(f"{rel}\0{len(data)}\0".encode())
            digest.update(data)
        for rel in sorted(self.links):
            digest.update(f"{rel}\0link\0{self.links[rel]}\0".encode())
        return digest.hexdigest()


# every skill at or under path; a dir with SKILL.md is not searched further
def find_skills(path):
    path = Path(path)
    if (path / "SKILL.md").is_file():
        return [Skill(path)]
    skills = []

    def visit(directory, ancestors):
        try:
            children = sorted(directory.iterdir())
        except OSError:
            return
        for child in children:
            try:
                if child.name.startswith(".") or not child.is_dir():
                    continue
                real = child.resolve()
                if real in ancestors:
                    continue
                if (child / "SKILL.md").is_file():
                    skills.append(Skill(child))
                else:
                    visit(child, ancestors | {real})
            except OSError:
                continue

    visit(path, {path.resolve()})
    return skills
