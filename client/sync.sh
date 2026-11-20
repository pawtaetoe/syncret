#!/usr/bin/env bash
# syncret sync client: pull the latest approved release and link its skills into each agent.
# needs only bash, curl and tar; run every 15 minutes by a systemd timer or launchd agent.
set -euo pipefail
umask 077

CONFIG="${SYNCRET_CLIENT_ENV:-${XDG_CONFIG_HOME:-$HOME/.config}/syncret/client.env}"
DATA="${SYNCRET_CLIENT_DATA:-${XDG_DATA_HOME:-$HOME/.local/share}/syncret}"
AGENTS="${SYNCRET_AGENTS_SKILLS:-$HOME/.agents/skills}"
CLAUDE="${SYNCRET_CLAUDE_SKILLS:-$HOME/.claude/skills}"
COPILOT="${SYNCRET_COPILOT_SKILLS:-$HOME/.copilot/skills}"
RELEASES="$DATA/releases"
KEEP=3
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

log() { printf 'syncret-sync: %s\n' "$*" >&2; }
die() { log "$*"; exit 1; }

case "$(uname -s)" in
  Darwin) MACOS=1 ;;
  *) MACOS=0 ;;
esac

sha256_of() {
  if [ "$MACOS" = 1 ]; then shasum -a 256 "$1" | cut -d' ' -f1; else sha256sum "$1" | cut -d' ' -f1; fi
}

# replace a symlink in one rename, so readers see the old target or the new, never neither
swap_link() {
  local target=$1 link=$2 temp="$2.syncret-new"
  rm -f "$temp"
  ln -s "$target" "$temp"
  if [ "$MACOS" = 1 ]; then mv -fh "$temp" "$link"; else mv -T "$temp" "$link"; fi
}

# the config is parsed for two values, never sourced as code
[ -f "$CONFIG" ] || die "no config at $CONFIG; run install.sh"
URL="" TOKEN=""
while IFS='=' read -r key value || [ -n "$key" ]; do
  case "$key" in
    SYNCRET_URL) URL="${value%/}" ;;
    SYNCRET_TOKEN) TOKEN="$value" ;;
  esac
done < "$CONFIG"
[ -n "$URL" ] && [ -n "$TOKEN" ] || die "$CONFIG needs SYNCRET_URL and SYNCRET_TOKEN"

mkdir -p "$RELEASES"

# one run at a time: flock on linux, a lock folder on macos (no flock there)
if [ "$MACOS" = 1 ]; then
  LOCK="$DATA/lock.d"
  if ! mkdir "$LOCK" 2>/dev/null; then
    if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +60 2>/dev/null)" ]; then
      rm -rf "$LOCK" && mkdir "$LOCK"
    else
      log "another sync is running"; exit 0
    fi
  fi
  trap 'rm -rf "$LOCK"' EXIT
else
  exec 9>"$DATA/lock"
  flock -n 9 || { log "another sync is running"; exit 0; }
fi

WORK=$(mktemp -d "$DATA/.work.XXXXXX")
trap 'rm -rf "$WORK"; [ "$MACOS" = 1 ] && rm -rf "$LOCK"; true' EXIT

# skip until the interval the server last gave has passed
now=$(date +%s)
last=$(cat "$DATA/last_check" 2>/dev/null || echo 0)
interval=$(cat "$DATA/interval" 2>/dev/null || echo 0)
if [ "$FORCE" = 0 ] && [ $((now - last)) -lt $((interval * 60)) ]; then
  exit 0
fi

# the token goes to curl in a private header file, never on the command line
printf 'Authorization: Bearer %s\n' "$TOKEN" > "$WORK/auth"
fetch() {
  curl -sS --proto '=https,http' --max-time "$3" -H @"$WORK/auth" -o "$2" -w '%{http_code}' "$URL$1"
}

status=$(fetch "/release/latest?format=text" "$WORK/latest" 30) || die "cannot reach $URL"
case "$status" in
  200) ;;
  401) die "the server refused this machine's token; make a new one on the Machines page" ;;
  404) log "nothing published yet"; printf '%s\n' "$now" > "$DATA/last_check"; exit 0 ;;
  *) die "unexpected answer $status from $URL/release/latest" ;;
esac
read -r id sha interval < "$WORK/latest" || true
[[ "$id" =~ ^[0-9a-f]{12}$ ]] || die "bad release id from the server"
[[ "$sha" =~ ^[0-9a-f]{64}$ ]] || die "bad sha256 from the server"
[[ "$interval" =~ ^[0-9]+$ ]] || interval=60
printf '%s\n' "$interval" > "$DATA/interval"
printf '%s\n' "$now" > "$DATA/last_check"

# download, check and unpack a release we do not have yet; it only appears once complete
if [ ! -d "$RELEASES/$id" ]; then
  status=$(fetch "/release/$id.tar.gz" "$WORK/release.tar.gz" 300) || die "download failed"
  [ "$status" = 200 ] || die "download of release $id answered $status"
  [ "$(sha256_of "$WORK/release.tar.gz")" = "$sha" ] || die "release $id failed its sha256 check; not installed"
  mkdir "$WORK/unpacked"
  tar -xzf "$WORK/release.tar.gz" -C "$WORK/unpacked"
  [ -d "$WORK/unpacked/skills" ] && [ -f "$WORK/unpacked/links.tsv" ] || die "release $id is incomplete"
  # moved into place first (a directory needs write permission to change parents), then
  # made read-only; nothing points at it until the swap below
  mv "$WORK/unpacked" "$RELEASES/$id"
  chmod -R a-w "$RELEASES/$id"
  log "installed release $id"
fi

# point ~/.agents/skills at the release; refuse to replace a real folder
mkdir -p "$(dirname "$AGENTS")"
if [ -e "$AGENTS" ] && [ ! -L "$AGENTS" ]; then
  die "$AGENTS is a real folder; move it away so syncret can manage it"
fi
if [ "$(readlink "$AGENTS" 2>/dev/null || true)" != "$RELEASES/$id/skills" ]; then
  swap_link "$RELEASES/$id/skills" "$AGENTS"
  log "now on release $id"
fi
printf '%s\n' "$id" > "$DATA/current"

# link skills into claude code and copilot as links.tsv says, through ~/.agents/skills so
# the next swap moves them too; only links this script made are ever changed or removed
touch "$DATA/links"
: > "$WORK/wanted"
while IFS=$'\t' read -r folder claude copilot _zed; do
  [[ "$folder" =~ ^[A-Za-z0-9._-]+$ ]] || continue
  [ "$claude" = 1 ] && printf '%s\n' "$CLAUDE/$folder" >> "$WORK/wanted"
  [ "$copilot" = 1 ] && printf '%s\n' "$COPILOT/$folder" >> "$WORK/wanted"
done < "$RELEASES/$id/links.tsv"

ours() { [ -L "$1" ] && case "$(readlink "$1")" in "$AGENTS"/*) return 0 ;; esac; return 1; }

: > "$WORK/links"
while IFS= read -r link; do
  [ -n "$link" ] || continue
  target="$AGENTS/$(basename "$link")"
  if ours "$link"; then
    [ "$(readlink "$link")" = "$target" ] || swap_link "$target" "$link"
  elif [ -e "$link" ] || [ -L "$link" ]; then
    log "leaving $link alone: it is not a syncret link"
    continue
  else
    mkdir -p "$(dirname "$link")"
    ln -s "$target" "$link"
  fi
  printf '%s\n' "$link" >> "$WORK/links"
done < "$WORK/wanted"

while IFS= read -r link; do
  [ -n "$link" ] || continue
  grep -qxF "$link" "$WORK/links" && continue
  ours "$link" && rm -f "$link"
done < "$DATA/links"
cp "$WORK/links" "$DATA/links"

# keep the current release and the newest others, up to KEEP in all
others=()
for dir in "$RELEASES"/*/; do
  dir="${dir%/}"
  [ -d "$dir" ] && [ "$(basename "$dir")" != "$id" ] && others+=("$dir")
done
if [ "${#others[@]}" -ge "$KEEP" ]; then
  # shellcheck disable=SC2012 # release folder names are 12 hex digits, safe for ls
  ls -dt "${others[@]}" | tail -n +"$KEEP" | while IFS= read -r old; do
    chmod -R u+w "$old" && rm -rf "$old"
  done
fi
exit 0
