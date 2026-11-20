import re
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from itertools import product
from pathlib import Path

from syncret.core.skill import FRONTMATTER

# similarity at or above which an old rule counts as changed rather than dropped
CHANGED = 0.6

_RULE = re.compile(
    r"\b(?:always|never|must|only|cannot)\b|\b(?:don|can|shouldn|mustn)['\u2019]t\b"
    r"|\bdo\s+not\b|\bshould\s+not\b",
    re.I
)
_NEGATIVE = re.compile(
    r"\b(?:never|cannot)\b|\b(?:don|can|shouldn|mustn)['\u2019]t\b|\b(?:do|should|must)\s+not\b",
    re.I
)
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_MARKUP = re.compile(r"^\s*(?:[-*+]|\d+[.)]|#+|>)\s*|[*_]+")


# lowercase words of a rule without markdown, list markers or end punctuation
def words(text):
    text = _MARKUP.sub("", text).replace("\u2019", "'").lower()
    return re.findall(r"[\w'`./~-]+", text)


# how alike two rules read, from 0 to 1
def similarity(a, b):
    return SequenceMatcher(None, words(a), words(b)).ratio()


# one rule sentence and where it is
@dataclass(frozen=True)
class Rule:
    line: int
    text: str

    @property
    def words(self):
        return words(self.text)

    @property
    def negative(self):
        return bool(_NEGATIVE.search(self.text))


# an old rule and the new rule it became
@dataclass(frozen=True)
class Pair:
    old: Rule
    new: Rule
    score: float
    flipped: bool


# every rule kept, changed, dropped or added between two versions
@dataclass
class RuleDiff:
    kept: list[Pair] = field(default_factory=list)
    changed: list[Pair] = field(default_factory=list)
    dropped: list[Rule] = field(default_factory=list)
    added: list[Rule] = field(default_factory=list)

    # a dropped rule or one whose meaning flipped needs a look before shipping
    @property
    def ok(self):
        return not self.dropped and not any(p.flipped for p in self.changed)

    def to_dict(self):
        return {**asdict(self), "ok": self.ok}


# SKILL.md of a skill folder, or the file itself
def skill_file(path):
    path = Path(path)
    return path / "SKILL.md" if path.is_dir() else path


# rule sentences in a skill's body, skipping frontmatter and code blocks
def extract_rules(text):
    match = FRONTMATTER.match(text)
    start = text[:match.end()].count("\n") if match else 0
    rules, fenced = [], False
    for number, line in enumerate(text.split("\n")[start:], start + 1):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced:
            continue
        for sentence in _SENTENCE.split(line.strip()):
            if _RULE.search(sentence):
                rules.append(Rule(number, sentence))
    return rules


# match old rules to new ones, best pairs first, each rule used once
def diff_rules(old_text, new_text):
    old, new = extract_rules(old_text), extract_rules(new_text)
    scores = sorted(
        ((SequenceMatcher(None, a.words, b.words).ratio(), i, j)
         for (i, a), (j, b) in product(enumerate(old), enumerate(new))),
        reverse=True
    )
    result, used_old, used_new = RuleDiff(), set(), set()
    for score, i, j in scores:
        if score < CHANGED or i in used_old or j in used_new:
            continue
        used_old.add(i)
        used_new.add(j)
        a, b = old[i], new[j]
        pair = Pair(a, b, round(score, 2), a.negative != b.negative)
        (result.kept if a.words == b.words else result.changed).append(pair)
    result.dropped = [r for i, r in enumerate(old) if i not in used_old]
    result.added = [r for j, r in enumerate(new) if j not in used_new]
    for rules in (result.kept, result.changed):
        rules.sort(key=lambda p: p.old.line)
    return result
