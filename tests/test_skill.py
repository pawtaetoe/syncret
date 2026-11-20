import shutil
from pathlib import Path

from syncret.core.skill import Skill, find_skills

FIXTURES = Path(__file__).parent / "fixtures" / "skills"


# a single skill dir parses frontmatter, body and files
def test_load_single_skill():
    [skill] = find_skills(FIXTURES / "good")
    assert skill.error is None
    assert skill.name == "good"
    assert skill.description.startswith("Format CSV")
    assert skill.body.lstrip().startswith("# Good")
    assert set(skill.files) == {"SKILL.md", "scripts/summarise.sh"}


# a folder of skills finds every skill, nested ones included
def test_load_folder():
    names = sorted(skill.path.name for skill in find_skills(FIXTURES))
    assert names == [
        "broken-yaml", "good", "hooks-bad", "nested", "no-frontmatter", "remote-run-bad"
    ]


# a copy and a symlinked copy hash the same as the original
def test_hash_matches_copies(tmp_path):
    shutil.copytree(FIXTURES / "good", tmp_path / "copy")
    (tmp_path / "link").symlink_to(FIXTURES / "good")
    original = Skill(FIXTURES / "good")
    assert Skill(tmp_path / "copy").hash == original.hash
    assert Skill(tmp_path / "link").hash == original.hash


# any change to a file changes the hash
def test_hash_changes_with_content(tmp_path):
    shutil.copytree(FIXTURES / "good", tmp_path / "copy")
    (tmp_path / "copy" / "scripts" / "summarise.sh").write_text("echo changed\n")
    assert Skill(tmp_path / "copy").hash != Skill(FIXTURES / "good").hash


# symlink loops inside a skill or a skills folder do not hang
def test_symlink_loops(tmp_path):
    shutil.copytree(FIXTURES / "good", tmp_path / "skills" / "good")
    (tmp_path / "skills" / "good" / "loop").symlink_to(tmp_path / "skills" / "good")
    (tmp_path / "skills" / "back").symlink_to(tmp_path / "skills")
    (tmp_path / "skills" / "self").symlink_to(tmp_path / "skills" / "self")
    [skill] = find_skills(tmp_path / "skills")
    assert set(skill.files) == {"SKILL.md", "scripts/summarise.sh"}


# the same skill reached through two paths is listed twice, for scan to group
def test_symlinked_skill_listed_per_path(tmp_path):
    (tmp_path / "a").symlink_to(FIXTURES / "good")
    (tmp_path / "b").symlink_to(FIXTURES / "good")
    assert len(find_skills(tmp_path)) == 2


# missing or broken frontmatter gives a load error, not a crash
def test_frontmatter_errors(tmp_path):
    assert Skill(FIXTURES / "no-frontmatter").error == "missing frontmatter"
    assert Skill(FIXTURES / "broken-yaml").error.startswith("broken frontmatter")
    (tmp_path / "list").mkdir()
    (tmp_path / "list" / "SKILL.md").write_text("---\n- a\n---\n")
    assert Skill(tmp_path / "list").error == "frontmatter is not a mapping"
    (tmp_path / "bare").mkdir()
    (tmp_path / "bare" / "SKILL.md").write_text("---\nname: bare\n---\n")
    assert Skill(tmp_path / "bare").error == "frontmatter has no description"


# a skill without a frontmatter name falls back to its directory name
def test_name_fallback():
    assert Skill(FIXTURES / "no-frontmatter").name == "no-frontmatter"


# an empty folder has no skills
def test_empty_folder(tmp_path):
    assert find_skills(tmp_path) == []


# a symlink leaving the skill is recorded, never read; links inside the skill are read
def test_outside_links_not_read(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET\n")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "key").write_text("TOPSECRET\n")
    shutil.copytree(FIXTURES / "good", tmp_path / "skill")
    (tmp_path / "skill" / "notes.txt").symlink_to(secret)
    (tmp_path / "skill" / "refs").symlink_to(tmp_path / "outside")
    (tmp_path / "skill" / "helper.sh").symlink_to(tmp_path / "skill" / "scripts" / "summarise.sh")
    skill = Skill(tmp_path / "skill")
    assert skill.links == {"notes.txt": str(secret), "refs": str(tmp_path / "outside")}
    assert set(skill.files) == {"SKILL.md", "scripts/summarise.sh", "helper.sh"}
    assert not any(b"TOPSECRET" in data for data in skill.files.values())


# an outside link counts in the hash, so changing its target changes the hash
def test_outside_link_in_hash(tmp_path):
    shutil.copytree(FIXTURES / "good", tmp_path / "a")
    shutil.copytree(FIXTURES / "good", tmp_path / "b")
    (tmp_path / "a" / "x").symlink_to("/etc/hostname")
    (tmp_path / "b" / "x").symlink_to("/etc/hosts")
    assert Skill(tmp_path / "a").hash != Skill(tmp_path / "b").hash
