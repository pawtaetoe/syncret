---
name: hooks-bad
description: Lint Markdown files in the docs folder before a commit.
hooks:
  PreToolUse:
    - command: ./scripts/check.sh
---

# Hooks bad

Lint every Markdown file under `docs/`.
