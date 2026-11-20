import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from itertools import combinations

from syncret.core.review import Skipped, label_pair
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


# two skills that may do the same job; unconfirmed until an llm labels the pair
@dataclass(frozen=True)
class Overlap:
    paths: list[str]
    score: float
    label: str = "unconfirmed"
    summary: str = ""
    disagreements: list[dict] = field(default_factory=list)
    provider: str = ""
    note: str = ""


# everything one scan found; it only reports and never edits
@dataclass(frozen=True)
class ScanReport:
    verdict: str
    skills: list[Report]
    duplicates: list[Duplicate]
    collisions: list[Collision]
    overlaps: list[Overlap]
    notes: list[str] = field(default_factory=list)

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


# an overlap for a pair, labelled by the llm when one is given and allowed to see the pair
def _overlap(a, b, score, llm, own, notes):
    paths = [str(a.path), str(b.path)]
    if llm is None:
        return Overlap(paths, score)
    try:
        label = label_pair(a, b, llm, own)
    except Skipped as error:
        return Overlap(paths, score, note=str(error))
    if label.dropped_quotes:
        notes.append(f"{a.name} / {b.name}: {label.dropped_quotes} disagreement(s) dropped, "
                     "quotes not found in the skills")
    return Overlap(paths, score, label.label, label.summary, label.disagreements, label.provider)


# load, vet and compare every skill under dirs; with an llm, label overlaps and collisions
def scan(dirs, llm=None, own=True):
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

    # one skill per content hash, so copies listed under duplicates are not paired again;
    # with an llm, same-name pairs are labelled too, however different their descriptions
    pairs = []
    for a, b in combinations([group[0] for group in by_hash.values()], 2):
        score = round(similarity(a.description, b.description), 2)
        if score >= OVERLAP_THRESHOLD or (llm is not None and a.name == b.name):
            pairs.append((a, b, score))
    notes = []
    overlaps = [_overlap(a, b, score, llm, own, notes) for a, b, score in pairs]
    overlaps.sort(key=lambda overlap: -overlap.score)

    overall = verdict(report.verdict for report in reports)
    return ScanReport(overall, reports, duplicates, collisions, overlaps, notes)
