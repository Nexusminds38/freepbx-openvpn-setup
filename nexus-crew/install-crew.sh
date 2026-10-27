#!/usr/bin/env bash
# Nexus Crew installer — run on the Nexus box after `git pull`.
# Copies each agent's SOUL.md + IDENTITY.md into its OpenClaw workspace.
# Stock files are backed up ONCE to *.stock-backup and never clobbered again.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for a in main architect angel sleuth tape canvas switchboard lens vault omerta; do
  if [ "$a" = "main" ]; then ws="$HOME/.openclaw/workspace"; else ws="$HOME/.openclaw/workspaces/$a"; fi
  mkdir -p "$ws"
  for kind in SOUL IDENTITY; do
    src="$HERE/souls/$a-SOUL.md"; [ "$kind" = "IDENTITY" ] && src="$HERE/identities/$a-IDENTITY.md"
    dst="$ws/$kind.md"
    if [ -f "$src" ]; then
      if [ -f "$dst" ] && [ ! -f "$dst.stock-backup" ]; then cp "$dst" "$dst.stock-backup"; fi
      cp "$src" "$dst"
      echo "installed: $a -> $dst"
    fi
  done
  if [ "$a" = "main" ]; then
    for kind in USER MEMORY; do
      src="$HERE/user/main-USER.md"; [ "$kind" = "MEMORY" ] && src="$HERE/memory/main-MEMORY.md"
      dst="$ws/$kind.md"
      if [ -f "$src" ]; then
        if [ -f "$dst" ] && [ ! -f "$dst.stock-backup" ]; then cp "$dst" "$dst.stock-backup"; fi
        cp "$src" "$dst"
        echo "installed: main -> $dst"
      fi
    done
  fi
done
echo "Crew installed. Restarting the gateway so every agent wakes up as itself..."
systemctl --user restart openclaw-gateway
echo "Done. Check with: openclaw agents list"
