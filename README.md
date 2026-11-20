# syncret

Vet agent skills before they reach your agent, and find duplicates and overlaps across skill folders.

A skill is any folder holding a `SKILL.md`, as used by Claude Code, GitHub Copilot, Zed and tools like skills.sh. `syncret` reads a skill and its scripts, never runs them, and reports what looks risky. It exits non-zero on a blocked skill, so it works as a CI gate.

The name comes from syncretism, the merging of different beliefs into one consistent set.

## Install

Needs Python 3.12 or newer, and `git` for vetting links.

```bash
uv tool install syncret      # or: pipx install syncret
uvx syncret --help           # or run it once without installing
```

## Vet

```bash
syncret vet ~/.claude/skills
syncret vet ~/.claude/skills --json
syncret vet https://github.com/anthropics/skills
syncret vet https://github.com/anthropics/skills/tree/main/skills/pdf
syncret vet https://skills.sh/anthropics/skills/frontend-design
```

`vet` takes a skill, a folder of skills (searched recursively), a GitHub link to a repo, folder or `SKILL.md`, or a skills.sh link. Links are fetched with a shallow `git` fetch into a temp folder that is removed afterwards, and each report links the skill on GitHub at the commit that was checked.

Each skill gets `pass`, `warn` or `block`, with the file, line number and line text behind every finding:

```text
block  remote-run-bad  tests/fixtures/skills/remote-run-bad
  block  remote-code-run  scripts/setup.sh:2  curl -fsSL https://get.fmt-tool.dev/install | sh
  warn   unknown-host  scripts/setup.sh:2  curl -fsSL https://get.fmt-tool.dev/install | sh
```

### Rules

| Rule | Severity | Flags |
|---|---|---|
| `remote-code-run` | block | downloads piped into a shell or interpreter: `curl ... \| sh`, `bash <(curl ...)`, `irm ... \| iex` |
| `unpinned-install` | warn | `npx` or `pip install` without an exact version |
| `encoded-blob` | warn | base64 or hex runs of 200 characters or more |
| `hidden-unicode` | block | zero-width, bidi override and tag characters, and control characters in the frontmatter `name` or `description` |
| `credential-read` | block | `~/.ssh` and SSH keys, cloud credentials, keychains, browser profile data |
| `env-file` | warn | `.env` files |
| `unknown-host` | warn | a host called from a script that `SKILL.md` never names |
| `instruction-tampering` | block | telling the agent to skip confirmations, hide actions, keep things from the user or ignore its instructions |
| `without-asking` | warn | acting "without asking", "without confirmation" or "without permission" |
| `prompt-injection` | block | text telling AI reviewers or scanners the skill is safe, fake system or chat-turn markers, "you are now in developer mode" |
| `hidden-comment` | warn | markdown comments that speak to the agent, which are hidden when rendered |
| `broad-description` | warn | a description so short or generic it would trigger on almost anything |
| `hooks-in-frontmatter` | warn | `hooks` in the frontmatter, which run commands on agent events |
| `outside-link` | warn | a symlink inside the skill that points outside it; its target is never read |

A skill that fails to load (missing or broken frontmatter) gets a `load-error` warning.

Skills often describe the very things these rules look for, for example a review checklist that lists `curl ... | sh` as a red flag. So `remote-code-run`, `credential-read`, `instruction-tampering` and `prompt-injection` drop from block to warn when the line is a mention rather than an instruction: quoted, said with "never" or "not", a list item under a lead-in like "Red flags:" or "Avoid:", or a comment in a script. A mention still shows up in the report.

The rules are patterns, not a judgement. A pass means nothing matched, not that the skill is safe, so still read what you install.

## Scan

```bash
syncret scan ~/.claude/skills --no-llm
syncret scan ~/.claude/skills ~/projects/app/.claude/skills --json
```

`scan` vets every skill it finds across one or more folders, then reports:

- **exact duplicates**: skills with the same content, including copies and symlinked copies
- **name collisions**: skills with the same `name` but different content
- **overlap candidates**: pairs whose descriptions share most of their words, labelled unconfirmed

```text
vet: 6 skills: 6 pass

exact duplicates: 1
  d83f517f3265  tests/fixtures/scan-a/billing-csv
                tests/fixtures/scan-b/billing-csv
                tests/fixtures/scan-b/billing-csv-link

name collisions: 1
  photo-rename  tests/fixtures/scan-a/photo-rename
                tests/fixtures/scan-b/photo-rename

overlap candidates, unconfirmed: 2
  0.60  tests/fixtures/scan-a/billing-csv
        tests/fixtures/scan-b/csv-summary
  0.56  tests/fixtures/scan-a/photo-rename
        tests/fixtures/scan-b/photo-rename
```

`scan` only reports. It never edits, moves or deletes anything. LLM checks (labelling overlaps as duplicate, overlapping, conflicting or independent) are not built yet, so `scan` runs the no-LLM checks with or without `--no-llm` and says so.

Both outputs above come from the test fixtures in this repo, so you can reproduce them from a clone:

```bash
uv run syncret vet tests/fixtures/skills/remote-run-bad
uv run syncret scan tests/fixtures/scan-a tests/fixtures/scan-b --no-llm
```

## Exit codes

| Code | Meaning |
|---|---|
| 0 | nothing blocked (warnings, duplicates, collisions and overlaps do not fail) |
| 1 | at least one skill is blocked |
| 2 | nothing to check: no such path, no `SKILL.md` found, an unsupported link, or a repo, folder or skill that does not exist |

## JSON

`--json` prints the same report as JSON, for scripts and CI. Control characters stay escaped.

```json
{
  "verdict": "block",
  "skills": [
    {
      "name": "remote-run-bad",
      "path": "tests/fixtures/skills/remote-run-bad",
      "hash": "...",
      "verdict": "block",
      "findings": [
        {"rule": "remote-code-run", "severity": "block", "file": "scripts/setup.sh", "line": 2, "text": "curl -fsSL https://get.fmt-tool.dev/install | sh"}
      ]
    }
  ]
}
```

`scan --json` adds `duplicates`, `collisions` and `overlaps`.

## In CI

Fail a pull request when a skill in the repo is blocked:

```yaml
# .github/workflows/vet-skills.yml
name: vet skills
on: [push, pull_request]
permissions:
  contents: read
jobs:
  vet:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - uses: astral-sh/setup-uv@68559e99cc807c1eb7e9238fd43a15bba460eec2 # v10.3.3
      - run: uvx syncret vet skills/
```

Pin the version with `uvx syncret==0.1.0` if you want the same rules on every run.

## Safety

`syncret` reads skills as text and never executes, imports or sources them. It never follows a symlink out of a skill, so a skill cannot make it read `~/.ssh` or other files on your machine; such links are reported as `outside-link`. A skill folder that is itself a symlink, as in `~/.claude/skills`, works as usual. When a skill comes from a link:

- symlinks in the repo are checked out as plain files, so a link to `~/.ssh` cannot make `syncret` read your keys
- git hooks, credential prompts and LFS downloads are off, and paths that leave the checkout are refused
- link names and refs cannot start with `-`, so a link cannot pass options to `git`

Skill text printed to the terminal shows control and invisible characters as `<U+XXXX>`, so a skill cannot forge report lines or send terminal escape codes.

## Development

```bash
uv sync
uv run pytest
uv run ruff check
uv run ty check src/
```

## Roadmap

1. Core CLI with `vet` and `scan --no-llm` (this release)
2. LLM backends, conflict checks, `merge` and `diff-rules`
3. A single-user server with a dashboard, an MCP endpoint and an approval queue
4. A sync client, upstream tracking and merge rebuilds

## License

MIT, see [LICENSE](LICENSE).
