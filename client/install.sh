#!/usr/bin/env bash
# install the syncret sync client for this user: the script, its private config, and a
# 15-minute systemd timer (linux) or launchd agent (macos). the token is read at a silent
# prompt or from a file, never taken as an argument.
#   install.sh --url https://syncret.example.com [--token-file FILE] [--no-schedule] [--no-sync]
#   install.sh --uninstall
set -euo pipefail
umask 077

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HOME/.local/bin/syncret-sync"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/syncret"
CONFIG="$CONFIG_DIR/client.env"
UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
PLIST="$HOME/Library/LaunchAgents/com.syncret.sync.plist"
URL="" TOKEN_FILE="" SCHEDULE=1 SYNC=1 UNINSTALL=0

while [ $# -gt 0 ]; do
  case "$1" in
    --url) URL="${2:?--url needs a value}"; shift 2 ;;
    --token-file) TOKEN_FILE="${2:?--token-file needs a path}"; shift 2 ;;
    --no-schedule) SCHEDULE=0; shift ;;
    --no-sync) SYNC=0; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done

case "$(uname -s)" in Darwin) MACOS=1 ;; *) MACOS=0 ;; esac

if [ "$UNINSTALL" = 1 ]; then
  if [ "$MACOS" = 1 ]; then
    launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
  elif command -v systemctl >/dev/null; then
    systemctl --user disable --now syncret-sync.timer 2>/dev/null || true
    rm -f "$UNITS/syncret-sync.service" "$UNITS/syncret-sync.timer"
    systemctl --user daemon-reload 2>/dev/null || true
  fi
  rm -f "$BIN"
  echo "syncret-sync removed. Your skills stay on the last release; delete ~/.local/share/syncret,"
  echo "the links in ~/.claude/skills and ~/.copilot/skills, ~/.agents/skills and $CONFIG to remove them too."
  exit 0
fi

mkdir -p "$(dirname "$BIN")" "$CONFIG_DIR"
install -m 0755 "$HERE/sync.sh" "$BIN"

# write the config only when given a url, or when there is none yet
if [ -n "$URL" ] || [ ! -f "$CONFIG" ]; then
  [ -n "$URL" ] || { echo "first install needs --url" >&2; exit 2; }
  if [ -n "$TOKEN_FILE" ]; then
    token="$(tr -d '[:space:]' < "$TOKEN_FILE")"
  else
    read -rsp "Machine token from the syncret Machines page: " token
    echo
  fi
  [[ "$token" =~ ^syncret_[A-Za-z0-9_-]+$ ]] || { echo "that does not look like a machine token" >&2; exit 2; }
  printf 'SYNCRET_URL=%s\nSYNCRET_TOKEN=%s\n' "${URL%/}" "$token" > "$CONFIG.new"
  chmod 600 "$CONFIG.new"
  mv "$CONFIG.new" "$CONFIG"
fi

if [ "$SCHEDULE" = 1 ]; then
  if [ "$MACOS" = 1 ]; then
    mkdir -p "$(dirname "$PLIST")" "$HOME/Library/Logs"
    sed "s|@HOME@|$HOME|g" "$HERE/com.syncret.sync.plist" > "$PLIST"
    chmod 644 "$PLIST"
    launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
  else
    mkdir -p "$UNITS"
    install -m 0644 "$HERE/syncret-sync.service" "$HERE/syncret-sync.timer" "$UNITS/"
    systemctl --user daemon-reload
    systemctl --user enable --now syncret-sync.timer
  fi
fi

if [ "$SYNC" = 1 ]; then
  echo "installed; first sync:"
  "$BIN" --force
else
  echo "installed"
fi
