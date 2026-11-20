import re
from collections import defaultdict
from dataclasses import asdict, dataclass
from itertools import combinations

from syncret.core.skill import Skill, find_skills
from syncret.core.vet import Report, verdict, vet

OVERLAP_THRESHOLD = 0.3

_WORD = re.compile(r"[a-z0-9]+")
# common words plus the boilerplate most skill descriptions share
_STOP_WORDS = """
a an and or the of to in on for with from by as at is are be it its this that these those any
all when whenever use used using user users skill skills want wants ask asks such like into via
also other than not only your you their they them can will should would may must do does did
if else then so more most file files task tasks
"""
_STOP = set(_STOP_WORDS.split())


# skills with the same content hash
@dataclass(frozen=True)
class Duplicate:
    hash: str
    paths: list[str]


# skills with the same name but different content
@dataclass(frozen=True)
class Collision:
    name: str
    paths: list[str]


# two skills whose descriptions look alike, for the llm check to confirm
@dataclass(frozen=True)
class Overlap:
    paths: list[str]
    score: float
    label: str = "unconfirmed"


# everything one scan found; it only reports and never edits
@dataclass(frozen=True)
class ScanReport:
    verdict: str
    skills: list[Report]
    duplicates: list[Duplicate]
    collisions: list[Collision]
    overlaps: list[Overlap]

    def to_dict(self):
        return asdict(self)


# content words of a description, crudely stemmed
def _words(text):
    words = set()
    for word in _WORD.findall(text.lower()):
        if word in _STOP or len(word) < 3:
            continue
        for suffix in ("ing", "ed", "es", "s"):
            if word.endswith(suffix) and len(word) - len(suffix) >= 3:
                word = word[:-len(suffix)]
                break
        words.add(word)
    return words


# jaccard overlap of two descriptions' content words
def similarity(a, b):
    a, b = _words(a), _words(b)
    return len(a & b) / len(a | b) if a and b else 0.0


# load, vet and compare every skill under dirs
def scan(dirs):
    # a path given twice, or inside another given dir, is one skill
    by_path: dict[str, Skill] = {}
    for directory in dirs:
        for skill in find_skills(directory):
            by_path.setdefault(str(skill.path), skill)
    skills = list(by_path.values())
    reports = [vet(skill) for skill in skills]

    by_hash, by_name = defaultdict(list), defaultdict(list)
    for skill in skills:
        by_hash[skill.hash].append(skill)
        by_name[skill.name].append(skill)
    duplicates = [Duplicate(digest, [str(s.path) for s in group])
                  for digest, group in by_hash.items() if len(group) > 1]
    collisions = [Collision(name, [str(s.path) for s in group])
                  for name, group in by_name.items() if len({s.hash for s in group}) > 1]

    # one skill per content hash, so copies listed under duplicates are not paired again
    overlaps = []
    for a, b in combinations([group[0] for group in by_hash.values()], 2):
        score = similarity(a.description, b.description)
        if score >= OVERLAP_THRESHOLD:
            overlaps.append(Overlap([str(a.path), str(b.path)], round(score, 2)))
    overlaps.sort(key=lambda overlap: -overlap.score)

    overall = verdict(report.verdict for report in reports)
    return ScanReport(overall, reports, duplicates, collisions, overlaps)
