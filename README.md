# syncret

Vet agent skills before they reach your agent, find duplicates and overlaps across skill folders, and merge overlapping skills without losing their rules.

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

With an [LLM provider](#llm-providers-optional) configured, `vet` also has the model read the whole skill and adds `llm-review` findings for what patterns can't see. A review can only add findings, so a skill a rule blocks stays blocked whatever the model says.

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

`scan` only reports. It never edits, moves or deletes anything.

With an [LLM provider](#llm-providers-optional) configured, `scan` labels each overlap candidate, and each pair of skills sharing a name, as duplicate, overlapping, conflicting or independent, with a one-line summary and the lines that disagree:

```text
overlaps: 2
  independent 0.47  .../built-in-browser
                    .../chrome-browser
      Skill A covers the desktop app's built-in browser pane and Skill B covers the Chrome
      extension; each one explicitly excludes the other's tools, so they never apply to the
      same situation.
```

Without one, or with `--no-llm`, candidates stay unconfirmed as shown above.

Both outputs above come from the test fixtures in this repo, so you can reproduce them from a clone:

```bash
uv run syncret vet tests/fixtures/skills/remote-run-bad
uv run syncret scan tests/fixtures/scan-a tests/fixtures/scan-b --no-llm
```

## Diff rules

```bash
syncret diff-rules old-skill/ new-skill/
```

Lists every rule kept, changed, dropped or added between two versions of a skill (folders or `SKILL.md` files). A rule is any sentence that says always, never, must, only, don't or do not. Code blocks and frontmatter are skipped. A changed rule whose meaning flips, such as "Never push to main" becoming "Always push to main", is marked, since its text barely changes:

```text
kept: 1
changed: 2
  0.80  old:9  - Never push directly to main.  meaning flipped
        new:9  - Always push directly to main.
  0.92  old:10  - Only deploy from a tagged commit.
        new:10  - Only deploy from a signed, tagged commit.
dropped: 1
  old:10  Do not deploy on Fridays.
added: 1
  new:11  - Must notify #releases when done.
```

It exits 1 when a rule is dropped or flipped, so it can check an update in CI. It needs no LLM.

## Merge

```bash
syncret merge ~/.claude/skills/git-hygiene https://skills.sh/acme/skills/deploy \
  --name ship-safely --out ./ship-safely
```

Merges two or more skills (folders, or links to one skill) into a new one with an [LLM provider](#llm-providers-optional):

- The model writes the merged instructions. syncret writes the frontmatter itself, `name` and `description` only, so a source's `hooks` never carry over.
- Each source's other files (scripts, references) are copied unchanged under `sources/<source>/`, and the merged instructions point there. Sources are never edited.
- Every rule from every source must survive, or the model must list it as dropped with a reason. A rule lost or reversed without one refuses the merge, and nothing is written.
- Sentences in the merged skill that trace to no source are listed, so you can check anything the model added.
- `sources.json` records each source's path or link, content hash and commit, for rebuilding the merge when a source changes.
- The result is vetted. A blocked result exits 1.

`--out` must not exist yet, or be empty. The merge is built beside it and moved into place in one step.

## LLM providers (optional)

`vet`, `scan` and `diff-rules` work without any model. An LLM adds what patterns can't do: reviewing a skill's full content, labelling overlaps, and building merged skills with `merge`.

Providers live in `~/.config/syncret/config.toml` (or pass `--config`). They are tried in order until one answers:

```toml
[providers.claude]
type = "claude-code"     # runs `claude -p` on your Claude plan, with no tools
can_see_own = true       # may receive your own skills (default false)

[providers.opus]
type = "anthropic"       # needs `pip install "syncret[anthropic]"`
model = "claude-opus-5-5"
api_key_env = "ANTHROPIC_API_KEY"   # optional; otherwise the SDK's usual lookup, such as `ant auth login`
can_see_own = true
fallbacks = true         # server-side refusal fallbacks

[providers.gemini]
type = "gemini"          # Google's OpenAI-compatible endpoint, key in GEMINI_API_KEY
model = "gemini-3.8-flash"

[providers.local]
type = "openai-compatible"           # OpenRouter, a local Ollama, Meta's Model API, ...
base_url = "http://localhost:11434/v1"
model = "llama3.3"
json_mode = "prompt"     # "schema" (default), "object", or "prompt" for servers without JSON schema support
can_see_own = true

[jobs]                   # optional: providers per job, in order
review = ["gemini", "claude"]
overlap = ["gemini", "claude"]
merge = ["opus", "claude"]
```

- **Own skills stay private by default.** Local skills count as your own. They are only sent to a provider whose `can_see_own` is `true`. If none is allowed, that step is skipped, never sent anyway.
- **No secrets in the file.** API providers name the environment variable that holds their key.
- **Rate limits and failures** are retried, then the next provider is tried.
- **Plain http only to your own machine.** `base_url` must use `https`, except for `localhost`.
- `vet`, `scan` and `merge` take `--config <file>`, and `--public` to treat local skills as public. `vet` and `scan` take `--no-llm` to skip the model entirely.

### What is sent, and what is trusted

- A skill goes to the model between boundary lines carrying a random token, as data the model is told never to obey. Text that tries to instruct the reviewer is itself reported.
- Every line the model quotes must be found in the skill, or the finding is dropped and counted.
- Schema files (`.xsd`, `.dtd`, `.rng`, `.rnc`) are named but not sent; the rules still check them. A skill still over 400,000 characters is skipped with a note, never cut short.
- The Claude Code provider runs with no tools and no MCP servers, so a skill under review cannot make it act.

Check the setup with one tiny request per provider. It sends no skill content:

```bash
syncret llm check
```

```text
ok    claude  claude-code, default model, may see own skills
```

### Claude Code provider

The `claude-code` provider needs the Claude Code CLI on your `PATH`. Install it with the [native installer](https://code.claude.com/docs/en/setup), then log in once by running `claude`:

```bash
curl -fsSL https://claude.ai/install.sh | bash
claude --version
```

The installer puts `claude` in `~/.local/bin`. On many Linux systems `~/.profile` adds that folder to `PATH` at login only if it already exists, so after a first install either log in again or run `export PATH="$HOME/.local/bin:$PATH"`.

syncret runs it as `claude -p --output-format json --json-schema <schema> --tools "" --strict-mcp-config --no-session-persistence`, with the skill on stdin. No tools and no MCP servers means a skill under review can't make it act. If `claude` is not on your `PATH`, set `command = "/full/path/to/claude"` on the provider.

### Sharing the CLI with your editor

Zed's agent panel and the VS Code extension each run their own bundled copy of Claude Code. You can point both at the CLI you installed, so your editor and syncret run one binary, updated in one place. Login, settings and skills in `~/.claude` are shared either way, and usage counts against the same Claude plan.

In both editors, use the full path to `claude`. With remote development (Zed remote projects, VS Code Remote-SSH), give the path on the remote machine, where the agent runs. The change applies to agents started after it; restart the editor, or reconnect to the remote.

#### Zed

Add this to Zed's `settings.json`:

```json
{
  "agent_servers": {
    "claude-acp": {
      "type": "registry",
      "env": { "CLAUDE_CODE_EXECUTABLE": "/home/<you>/.local/bin/claude" }
    }
  }
}
```

`type` is required: `"registry"` is for agents installed from Zed's agent registry, which take `env` but no `command`. Zed's Claude adapter runs whatever `CLAUDE_CODE_EXECUTABLE` names instead of its bundled copy.

#### VS Code

The Claude Code extension has no environment variable for this. Its `claudeCode.claudeProcessWrapper` setting names the program that launches Claude, and the extension passes its bundled binary's path to that program as an argument. Point the setting at a small wrapper that skips that path and runs your CLI:

```bash
mkdir -p ~/.local/bin
cat > ~/.local/bin/claude-vscode <<'EOF'
#!/bin/sh
# VS Code passes its bundled claude first; skip it and run the installed CLI
case "$1" in
  */claude|*/claude.exe) shift ;;
esac
exec "$HOME/.local/bin/claude" "$@"
EOF
chmod +x ~/.local/bin/claude-vscode
```

Then in VS Code's `settings.json` (on the remote, for Remote-SSH):

```json
{
  "claudeCode.claudeProcessWrapper": "/home/<you>/.local/bin/claude-vscode"
}
```

Per the extension's docs, setting a wrapper also skips the "conversation still open elsewhere" check, turns off plan-mode restore on resume, and lists `claude -p` sessions alongside the extension's own. This setup follows the extension's documentation and has not been tested by syncret itself. On Windows, use a `.cmd` wrapper with the same logic.

## Server

`syncret` also runs as a small server for one person, in one Docker container: a git-backed skills repo where every change is a proposal you approve in a web dashboard, an MCP endpoint for agents, and releases that the [sync client](#sync-client) pulls onto your machines.

### First run

```bash
git clone https://github.com/pawtaetoe/syncret && cd syncret
docker compose up -d --build
docker compose exec syncret syncret set-password
docker compose exec syncret syncret set-claude-token   # optional, for the claude-code provider
```

Then open `http://127.0.0.1:8080` and log in. The port is published on `127.0.0.1` only. To reach it from other machines, put your own reverse proxy or tunnel in front. `compose.yaml` reads these variables (from the shell or an `.env` file next to it):

| Variable | Default | Use |
|---|---|---|
| `SYNCRET_BIND` | `127.0.0.1` | Host address to publish the port on |
| `SYNCRET_PORT` | `8080` | Host port |
| `SYNCRET_HTTPS` | `0` | Set `1` when your proxy serves https, so the session cookie is marked Secure |
| `SYNCRET_ALLOWED_HOSTS` | | Your proxy's hostname, comma-separated; the MCP endpoint refuses other `Host` headers |

The Claude token comes from `claude setup-token`, run once on any machine with a browser. It lasts a year and only makes model requests, so the container needs no Claude login. The Settings page shows its age and warns before it ends. Paste a new one there or with `set-claude-token`.

Everything lives in the `syncret-data` volume: the skills repo, the job database, settings, releases, and a `secrets/` folder (password hash, session key, Claude token, MCP token hash) readable only by the container's user. The container runs as a non-root user on a read-only filesystem with all capabilities dropped.

### Dashboard

| Page | Shows |
|---|---|
| Proposals | Pending changes with their vetting results, and a getting-started checklist until the first machine has synced |
| Proposal | The diff, vetting report, scan findings, a suggested merge, and Approve or Reject |
| Add | A GitHub or skills.sh link (or a folder on the server), then which skills to add and where to ship them |
| Skills | Every published skill with its kind, source, version and where it is sent; edit own skills, remove any |
| Scan | Duplicates, collisions and overlaps across published skills |
| Activity | Background jobs, running and finished, with their results and logs |
| Machines | One release token per machine, the install commands for it, its last sync and release |
| Claude account | Skills targeted at the Claude apps, a zip of each to upload, and which are out of date |
| Settings | Sync intervals, the Claude token, the MCP token and LLM providers (as `llm.toml`) |

- **Every change is a proposal.** Adding, updating, merging, editing or removing a skill opens a proposal on its own branch, vetted and scanned against every published skill. Nothing reaches your machines until you approve it.
- **Stale approvals are refused.** Approving checks that nothing else changed since the proposal was built. If something did, the proposal is rebuilt.
- **Blocked proposals cannot be approved.**
- **Overlaps suggest merges.** With an LLM configured, adding a skill that overlaps a published one also opens a merge proposal. Pick one, or reject both.
- **One writer.** A single background worker makes every change, one job at a time. The dashboard and MCP only queue jobs.
- **Upstream skills are tracked.** Every few hours (6 by default, set on the Settings page) the server fetches each upstream repo at the branch or tag a skill was added from. When a skill's content changed, it opens an update proposal. Settings also has "Check upstream now".
- **Merges follow their sources.** Approving an update to a skill that a merged skill was built from opens a proposal rebuilding that merge, checked like any merge.

### MCP

Make a token on the Settings page, then add the endpoint to an agent, for example Claude Code:

```bash
claude mcp add --transport http syncret https://syncret.example.com/mcp \
  --header "Authorization: Bearer syncret_mcp_..."
```

Tools: `list_skills`, `list_proposals`, `show_proposal`, `approve(id, base)`, `reject`, `add_skill`, `remove_skill`, `propose_edit`, `propose_merge`, `scan` and `job_status`. Writes queue jobs and return a job id. `approve` only goes through while `base` is still current, and its description tells agents to use it only when you say so. `show_proposal` marks skill content as untrusted.

### Releases

Each approval publishes a release: a tarball holding only the skills meant for local agents, one folder each, and a `manifest.json` (plus `links.tsv` for shell clients) saying which agents link each one. A skill also targeted at the Claude account is not linked for Claude Code, which already loads it from the account. Machines fetch `/release/latest` (or `?format=text`) and `/release/<id>.tar.gz` with their token from the Machines page.

## Sync client

A small client on each machine pulls approved releases every 15 minutes and links each skill into Claude Code (`~/.claude/skills`), Copilot (`~/.copilot/skills`) and Zed (`~/.agents/skills`). Make a token for the machine on the dashboard's Machines page first.

**Linux** (bash, curl, tar and a systemd user timer):

```bash
git clone https://github.com/pawtaetoe/syncret && cd syncret
client/install.sh --url https://syncret.example.com    # asks for the machine token
```

**macOS** (the same script with a launchd agent; not yet tested on a Mac):

```bash
client/install.sh --url https://syncret.example.com
```

**Windows** (PowerShell, built-in `tar.exe`, directory junctions and a scheduled task; no admin rights needed; not yet tested on Windows):

```powershell
powershell -ExecutionPolicy Bypass -File client\install.ps1 -Url https://syncret.example.com
```

What it does on each run:

1. Takes a lock, so only one sync runs at a time, and skips the run until the interval set on the server has passed.
2. Asks `/release/latest` for the newest release and its SHA-256. The machine token is never passed on a command line.
3. Downloads a new release, checks its SHA-256, unpacks it into `~/.local/share/syncret/releases/<id>/` (Windows: `%LOCALAPPDATA%\syncret\releases`) and makes it read-only.
4. Points `~/.agents/skills` at the release in one atomic rename, so an agent sees the old release or the new one, never a mix. On Windows the junction swap takes two steps, so an agent starting at that instant may briefly find no folder.
5. Links each skill into `~/.claude/skills` and `~/.copilot/skills` as the release says, through `~/.agents/skills`, so the next swap moves them too.
6. Keeps the last 3 releases.

It only ever changes or removes links it made. Your own skill folders, links to elsewhere, and anything already at a skill's name are left alone. It refuses to replace `~/.agents/skills` if that is a real folder.

The config (server URL and machine token) lives in `~/.config/syncret/client.env` (Windows: `%APPDATA%\syncret\client.env`), readable only by you. Run `syncret-sync --force` (Windows: `sync.ps1 -Force`) to sync now. `install.sh --uninstall` (Windows: `install.ps1 -Uninstall`) removes the timer and script and leaves your skills on the last release.

On a Linux server or any machine you are often logged out of, let the user timer run without a session:

```bash
sudo loginctl enable-linger $USER
```

### Moving existing skills into syncret

If a machine already has skills in `~/.agents/skills` (for example from `npx skills add`), the client will not replace that folder. Move the skills into syncret first:

1. Add each skill on the dashboard's Add page, by its GitHub or skills.sh link if it has one (so syncret tracks upstream updates), or else as a folder on the server. For a server in a container, copy the folder in first, for example `tar -C ~/.agents -cf - skills | docker compose exec -T syncret sh -c 'mkdir -p /tmp/import && tar -x -C /tmp/import'`, then add `/tmp/import/skills/<name>`.
2. Approve the proposals. Approving one makes the others stale; each is rebuilt on the new main, so approve them one at a time.
3. A blocked skill cannot be published. To keep using it on this machine, copy it as a real folder into `~/.claude/skills` (or `~/.copilot/skills`). The client never touches real folders.
4. Move the old folder aside and remove the links that pointed into it, then install the client:

```bash
mv ~/.agents/skills ~/.agents/skills.before-syncret
find ~/.claude/skills ~/.copilot/skills -maxdepth 1 -type l -lname "$HOME/.agents/skills/*" -delete 2>/dev/null
~/path/to/syncret/client/install.sh --url https://syncret.example.com
```

Run `install.sh` by its path inside the cloned repo; it is not on your `PATH` until it has installed `syncret-sync`. Once the synced skills show up, delete `~/.agents/skills.before-syncret`.


## Exit codes

| Code | Meaning |
|---|---|
| 0 | nothing blocked (warnings, duplicates, collisions and overlaps do not fail) |
| 1 | at least one skill is blocked |
| 2 | nothing to check: no such path, no `SKILL.md` found, an unsupported link, or a repo, folder or skill that does not exist |

`diff-rules` exits 1 when a rule is dropped or flipped. `merge` exits 1 when it refuses (rules lost without a reason) or its result is blocked, and 2 when it cannot run (no LLM, a bad source, `--out` not empty).

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

`scan --json` adds `duplicates`, `collisions` and `overlaps`. With an LLM, both add `notes` on how each review went, `diff-rules --json` gives `kept`, `changed`, `dropped`, `added` and `ok`, and `merge --json` gives `dropped`, `untraced` and the merged skill's `vet` report.

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
uv sync --all-extras
uv run pytest
uv run ruff check
uv run ty check src/
SYNCRET_CONTAINER_TEST=1 uv run pytest tests/container   # builds and runs the image
```

## Roadmap

1. Core CLI with `vet` and `scan --no-llm` (0.1.0)
2. LLM backends, conflict checks, `merge` and `diff-rules` (next release)
3. A single-user server with a dashboard, an MCP endpoint and an approval queue (this branch)
4. A sync client, upstream tracking and merge rebuilds (this branch)

## License

MIT, see [LICENSE](LICENSE).
