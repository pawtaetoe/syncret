from dataclasses import asdict, dataclass

from syncret.core import rules
from syncret.core.rules import SEVERITIES, Finding


# vetting result for one skill
@dataclass(frozen=True)
class Report:
    name: str
    path: str
    hash: str
    verdict: str
    findings: list[Finding]

    def to_dict(self):
        return asdict(self)


# worst severity among findings, pass when there are none
def verdict(severities):
    return max(severities, key=SEVERITIES.index, default="pass")


# text files as lines; files with a nul byte are binary and skipped
def text_files(skill):
    texts = {}
    for rel, data in skill.files.items():
        if b"\0" in data:
            continue
        text = data.decode("utf-8", errors="replace")
        texts[rel] = [line.removesuffix("\r") for line in text.split("\n")]
    return texts


# findings worst first, then by file and line
def _ordered(findings):
    return sorted(findings, key=lambda f: (-SEVERITIES.index(f.severity), f.file, f.line))


# a report with more findings added; findings are only ever added, so the verdict can only rise
def with_findings(report, extra):
    findings = _ordered([*report.findings, *extra])
    return Report(report.name, report.path, report.hash,
                  verdict(f.severity for f in findings), findings)


# run every rule over a skill; a load error is a warn finding of its own
def vet(skill):
    texts = text_files(skill)
    findings = []
    if skill.error:
        findings.append(Finding("load-error", "warn", "SKILL.md", 1, skill.error))
    for rule in rules.RULES:
        findings.extend(rule.run(skill, texts))
    findings = _ordered(findings)
    return Report(
        skill.name,
        str(skill.path),
        skill.hash,
        verdict(f.severity for f in findings),
        findings
    )
