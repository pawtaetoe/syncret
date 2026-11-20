import re
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from syncret.core.skill import Skill

SEVERITIES = ("pass", "warn", "block")

# a check gets the skill and its text files as lines, and yields (file, line, text)
type Hit = tuple[str, int, str]
type Check = Callable[[Skill, dict[str, list[str]]], Iterator[Hit]]

_NEGATION = re.compile(r"\b(?:never|not|avoid)\b|n't\b", re.I)
_WARNING = re.compile(
    r"\b(?:never|not|avoid|reject|red\s+flags?|dangerous|unsafe|malicious|suspicious"
    r"|forbidden)\b|n't\b",
    re.I
)
_LIST_ITEM = re.compile(r"(\s*)(?:[-*+]|\d+[.)])\s")


# one line that triggered a rule
@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str
    file: str
    line: int
    text: str


# one vetting rule; a block drops to warn when the line says never or not (negatable)
# or is a list item under a warning lead-in such as "red flags:" (mentionable)
@dataclass(frozen=True)
class Rule:
    id: str
    severity: str
    check: Check
    negatable: bool = False
    mentionable: bool = False

    def run(self, skill, texts):
        findings = []
        for file, line, text in self.check(skill, texts):
            negated = self.negatable and _NEGATION.search(text)
            mentioned = self.mentionable and _WARNING.search(_lead_in(texts.get(file, []), line))
            severity = "warn" if negated or mentioned else self.severity
            findings.append(Finding(self.id, severity, file, line, text))
        return findings


# the line a list item hangs from: the nearest earlier line that is not a sibling item
def _lead_in(lines, number):
    item = _LIST_ITEM.match(lines[number - 1]) if 0 < number <= len(lines) else None
    if not item:
        return ""
    indent = len(item.group(1))
    for line in reversed(lines[:number - 1]):
        sibling = _LIST_ITEM.match(line)
        if not line.strip() or (sibling and len(sibling.group(1)) >= indent):
            continue
        # a wrapped line of a sibling item is indented past the item marker
        if not sibling and len(line) - len(line.lstrip()) > indent:
            continue
        return line
    return ""


# every line in every text file that matches pattern
def _grep(pattern, texts):
    for rel, lines in texts.items():
        for number, line in enumerate(lines, 1):
            if pattern.search(line):
                yield rel, number, line


# line number of a top-level frontmatter key in SKILL.md, 1 if not found
def _key_line(texts, key):
    lines = texts.get("SKILL.md", [])
    pattern = re.compile(rf"""["']?{key}["']?\s*:""")
    number = next((i for i, line in enumerate(lines, 1) if pattern.match(line)), 1)
    return number, lines[number - 1] if lines else ""


# true when a negation comes before start in the same clause
def _negated_before(line, start):
    return bool(_NEGATION.search(re.split(r"[.!?;:]", line[:start])[-1]))


# frontmatter hooks run commands on agent events, outside the skill's own instructions
def _hooks(skill, texts):
    if "hooks" in skill.frontmatter:
        yield "SKILL.md", *_key_line(texts, "hooks")


_FETCH = r"(?:curl|wget|iwr|irm|Invoke-WebRequest|Invoke-RestMethod)\b"
_SHELL = r"(?:sudo\s+(?:-\S+\s+)*)?(?:(?:ba|z|da|k|fi)?sh|python[\d.]*|perl|ruby|node|iex)\b"
_REMOTE_RUN = re.compile(
    rf"{_FETCH}[^\n]*?\|\s*{_SHELL}"
    rf"|(?:\b{_SHELL}\s+(?:-\w+\s+)*|\beval\s+|\bsource\s+|(?:^|\s)\.\s+)[\"']?(?:\$\(|<\(|`)\s*{_FETCH}"
    rf"|\b(?:iex|Invoke-Expression)\b[^\n]*{_FETCH}",
    re.I
)


# downloaded code piped straight into a shell or interpreter
def _remote_run(skill, texts):
    yield from _grep(_REMOTE_RUN, texts)


_NPX = re.compile(r"\bnpx\s+(?:-\S+\s+)*([^\s`'\";|&)]+)")
_PIP = re.compile(r"\b(?:pip3?|uv\s+pip|python[\d.]*\s+-m\s+pip)\s+install\s+([^\n`;|&)#]*)")
_PIP_VALUED = {"-r", "--requirement", "-c", "--constraint", "-e", "--editable", "-i",
               "--index-url", "--extra-index-url", "-t", "--target", "--prefix", "-f",
               "--find-links", "--python"}


# a package spec that is a path, url or variable rather than a registry name
def _local(spec):
    return spec.startswith((".", "/", "~", "$")) or "://" in spec or spec.endswith(
        (".whl", ".tar.gz", ".zip")
    )


# npx package without an exact version
def _npx_unpinned(spec):
    version = spec[1:].partition("@")[2]
    return not _local(spec) and not version[:1].isdigit()


# pip install with any registry package lacking ==
def _pip_unpinned(args):
    tokens = iter(args.split())
    for token in tokens:
        if token.startswith("-"):
            if token in _PIP_VALUED:
                next(tokens, None)
            continue
        spec = token.strip("'\"")
        if not _local(spec) and "==" not in spec:
            return True
    return False


# npx or pip installs that fetch whatever version is current
def _unpinned_install(skill, texts):
    for rel, lines in texts.items():
        for number, line in enumerate(lines, 1):
            npx = any(_npx_unpinned(m.group(1)) for m in _NPX.finditer(line))
            pip = any(_pip_unpinned(m.group(1)) for m in _PIP.finditer(line))
            if npx or pip:
                yield rel, number, line


_BLOB = re.compile(r"[A-Za-z0-9+/_-]{200,}={0,2}")
_BLOB_KINDS = (str.isdigit, str.isupper, str.islower)


# long base64 or hex runs that hide content from a reader
def _encoded_blob(skill, texts):
    for rel, lines in texts.items():
        for number, line in enumerate(lines, 1):
            for match in _BLOB.finditer(line):
                blob = match.group()
                # two or more of digit, upper, lower, so rulers like ----- pass
                if sum(any(map(kind, blob)) for kind in _BLOB_KINDS) >= 2:
                    yield rel, number, line
                    break


_HIDDEN = re.compile(
    "[­᠎​-‍‪-‮⁠-⁤⁦-⁩︀-️﻿"
    "\U000e0000-\U000e007f\U000e0100-\U000e01ef]"
)
_FLAG = "\U0001f3f4"


# emoji or emoji modifier, which zero-width joiners legitimately join
def _emoji(char):
    return bool(char) and (char == "️" or unicodedata.category(char) in ("So", "Sk"))


# non-ascii letter or mark, which zero-width non-joiners legitimately sit between
def _script_letter(char):
    return bool(char) and ord(char) > 0x7F and unicodedata.category(char)[0] in "LM"


# true when the invisible char at i has a legitimate use in context
def _hidden_ok(line, i, first):
    char, prev, after = line[i], line[i - 1] if i else "", line[i + 1:i + 2]
    if char == "﻿":
        return first and i == 0
    if char == "‍":
        return _emoji(prev) and _emoji(after)
    if char == "‌":
        return _script_letter(prev) and _script_letter(after)
    if "︀" <= char <= "️" or "\U000e0100" <= char <= "\U000e01ef":
        return bool(prev) and not _HIDDEN.match(prev)
    if "\U000e0000" <= char <= "\U000e007f":
        start = i
        while start and "\U000e0000" <= line[start - 1] <= "\U000e007f":
            start -= 1
        end = i
        while end < len(line) and "\U000e0000" <= line[end] <= "\U000e007f":
            end += 1
        return start > 0 and line[start - 1] == _FLAG and end - start <= 8
    return False


# zero-width, bidi override and tag chars that change what the agent reads unseen
def _hidden_unicode(skill, texts):
    for rel, lines in texts.items():
        for number, line in enumerate(lines, 1):
            bad = [m.start() for m in _HIDDEN.finditer(line)
                   if not _hidden_ok(line, m.start(), number == 1)]
            if bad:
                shown = "".join(f"<U+{ord(c):04X}>" if i in bad else c for i, c in enumerate(line))
                yield rel, number, shown


_CREDENTIALS = re.compile(
    r"(?<![\w.-])\.(?:ssh|aws|azure|gnupg|netrc|git-credentials|pypirc)\b"
    r"|(?<![\w.-])\.(?:kube/config|docker/config\.json|config/gcloud)\b"
    r"|\bid_(?:rsa|dsa|ecdsa|ed25519)\b"
    r"|(?<![\w.$-])\.env\b(?!\.(?:example|sample|template|dist)\b)"
    r"|\bsecurity\s+(?:find-(?:generic|internet)-password|dump-keychain)\b"
    r"|Library/Keychains|\.keychain(?:-db)?\b|\bsecret-tool\s+lookup\b|\bkeyring\.get_password\b"
    r"|Google[\\/]+Chrome|google-chrome|chromium[\\/]|BraveSoftware|Microsoft[\\/]+Edge"
    r"|\.mozilla[\\/]firefox|Firefox[\\/]+Profiles|\bLogin Data\b|cookies\.sqlite|logins\.json"
    r"|\bkey4\.db\b",
    re.I
)


# reads of ssh keys, env files, cloud credentials, keychains and browser data
def _credential_read(skill, texts):
    yield from _grep(_CREDENTIALS, texts)


_URL = re.compile(r"\b(?:https?|wss?|ftp)://(?:[^@/\s'\"`<>]*@)?(\[[0-9a-f:]+\]|[\w.-]+)", re.I)
_SCRIPT_SUFFIXES = {".sh", ".bash", ".zsh", ".fish", ".py", ".js", ".mjs", ".cjs", ".ts",
                    ".rb", ".pl", ".php", ".ps1", ".go", ".rs", ".lua"}
_LOCAL_HOST = re.compile(
    r"localhost|.*\.localhost|127(?:\.\d+){3}|\[::1\]|0\.0\.0\.0|(?:.*\.)?example\.(?:com|org|net)"
)
# hosts whose urls are xml namespaces and schema ids, not network calls
_IDENTIFIER_HOST = re.compile(
    r"(?:.*\.)?(?:w3\.org|openxmlformats\.org|purl\.org|openoffice\.org|xmlns\.com"
    r"|json-schema\.org)|schemas\.(?:microsoft|android)\.com|ns\.adobe\.com"
)


# script files by suffix or shebang
def _is_script(rel, lines):
    return Path(rel).suffix in _SCRIPT_SUFFIXES or bool(lines and lines[0].startswith("#!"))


# true when SKILL.md names the host or a parent domain of it
def _named(host, skill_text):
    labels = host.split(".")
    candidates = [host] + [".".join(labels[i:]) for i in range(1, len(labels) - 1)]
    return any(re.search(rf"(?<![\w-]){re.escape(c)}(?![\w-])", skill_text) for c in candidates)


# hosts called from scripts that SKILL.md never mentions, reported once per host
def _unknown_host(skill, texts):
    skill_text = skill.text.lower()
    seen = set()
    for rel, lines in texts.items():
        if not _is_script(rel, lines):
            continue
        for number, line in enumerate(lines, 1):
            for match in _URL.finditer(line):
                host = match.group(1).lower().rstrip(".")
                if host in seen or _LOCAL_HOST.fullmatch(host) or _IDENTIFIER_HOST.fullmatch(host):
                    continue
                seen.add(host)
                if not _named(host, skill_text):
                    yield rel, number, line


_TAMPER = re.compile(
    r"\bignore\s+(?:all\s+|any\s+)?(?:the\s+|your\s+)?(?:previous|prior|above|earlier|preceding"
    r"|system)\s+(?:instructions|prompts?|rules|messages)"
    r"|\b(?:do\s+not|don't|never)\s+(?:tell|inform|notify|alert|show)\s+the\s+user(?!\s+(?:to|how)\b)"
    r"|\b(?:do\s+not|don't|never)\s+(?:mention|reveal|disclose)\b[^.]*\bto\s+the\s+user"
    r"|\b(?:do\s+not|don't|never)\s+(?:ask|wait\s+for)\s+(?:the\s+user\s+)?(?:for\s+)?"
    r"(?:confirmation|permission|approval)"
    r"|\bwithout\s+the\s+user(?:'s)?\s+(?:knowing|noticing|knowledge|seeing)"
    r"|\bignore\s+(?:what\s+)?the\s+user(?:['\u2019]s\s+(?:instructions|requests?|wishes|objections"
    r"|answers?))?(?!['\u2019\w])",
    re.I
)
_TAMPER_UNLESS_NEGATED = re.compile(
    r"\bwithout\s+(?:(?:asking|confirming|prompting)(?!\s+you\b)|confirmation|permission|approval"
    r"|the\s+user['\u2019]s\s+(?:consent|permission|approval))"
    r"|\b(?:hide|conceal)\s+(?:this|these|the|your|all|any)\s+(?:actions?|changes?|commands?"
    r"|steps?|output|activity|tool\s+calls?)"
    r"|\bskip\s+(?:all\s+|any\s+|the\s+)?(?:confirmations?|permission|approval)"
    r"|--dangerously-skip-permissions",
    re.I
)


# true when the match is quoted, so the line mentions the phrase rather than says it
def _quoted(line, start):
    return start > 0 and line[start - 1] in "\"'`\u201c\u2018"


# instructions to skip confirmations, hide actions or ignore the user
def _instruction_tampering(skill, texts):
    for rel, lines in texts.items():
        for number, line in enumerate(lines, 1):
            hits = [m for m in _TAMPER.finditer(line) if not _quoted(line, m.start())]
            hits += [m for m in _TAMPER_UNLESS_NEGATED.finditer(line)
                     if not _quoted(line, m.start()) and not _negated_before(line, m.start())]
            if hits:
                yield rel, number, line


_GENERIC = re.compile(
    r"\b(?:any|every|all)\s+(?:tasks?|requests?|questions?|conversations?|messages?|prompts?)\b"
    r"|\balways\s+use\b|\buse\s+(?:this|it)\s+(?:for\s+)?(?:everything|anything|always)\b"
    r"|\bgeneral[-\s]purpose\b",
    re.I
)


# a description so short, or short and generic, it would trigger on almost anything
def _broad_description(skill, texts):
    words = len(skill.description.split())
    if 0 < words < 5 or (words < 20 and _GENERIC.search(skill.description)):
        yield "SKILL.md", *_key_line(texts, "description")


RULES = [
    Rule("remote-code-run", "block", _remote_run, negatable=True, mentionable=True),
    Rule("unpinned-install", "warn", _unpinned_install),
    Rule("encoded-blob", "warn", _encoded_blob),
    Rule("hidden-unicode", "block", _hidden_unicode),
    Rule("credential-read", "block", _credential_read, negatable=True, mentionable=True),
    Rule("unknown-host", "warn", _unknown_host),
    Rule("instruction-tampering", "block", _instruction_tampering, mentionable=True),
    Rule("broad-description", "warn", _broad_description),
    Rule("hooks-in-frontmatter", "warn", _hooks)
]
