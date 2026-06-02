#!/usr/bin/env bash
# baton teardown — remove baton from this machine. The reverse of setup.sh.
#
#   ./teardown.sh           full decommission: stop the worker, remove the fence + the baton user/home
#   ./teardown.sh --soft    non-destructive: just stop the drain timer + deregister (keeps everything)
#   option:                 BATON_WORKER_USER=baton   (default baton)
#
# Stops the worker FIRST (nothing runs unfenced mid-teardown), then removes the root-owned fence and
# the dedicated worker account + its home (instance clone, work/ repo clones, ~/.baton.env,
# ~/.git-credentials). Touches ONLY baton-owned paths — never your real repos or your own account.
set -uo pipefail
WU="${BATON_WORKER_USER:-baton}"
WHOME="/home/$WU"
b=$'\e[1m'; g=$'\e[32m'; y=$'\e[33m'; r=$'\e[31m'; z=$'\e[0m'
say(){ printf '\n%s%s%s\n' "$b" "$*" "$z"; }
ok(){ printf '  %s\xe2\x9c\x93%s %s\n' "$g" "$z" "$*"; }
warn(){ printf '  %s!%s %s\n' "$y" "$z" "$*"; }
bad(){ printf '  %s\xe2\x9c\x97%s %s\n' "$r" "$z" "$*"; }

say "baton teardown  (worker user: $WU)"
sudo -v || { bad "this must run as a user with sudo"; exit 1; }
id "$WU" >/dev/null 2>&1 || warn "user $WU doesn't exist — will still clean the fence + units"

# --soft: non-destructive — just stop the worker + deregister (keeps the user, fence, clones).
if [ "${1:-}" = "--soft" ]; then
  say "soft teardown — stop + deregister (user, fence, clones kept)"
  if id "$WU" >/dev/null 2>&1; then
    sudo -u "$WU" env "XDG_RUNTIME_DIR=/run/user/$(id -u "$WU")" systemctl --user disable --now baton-tick.timer 2>/dev/null || true
    sudo -u "$WU" rm -f "$WHOME/baton/nodes/$(hostname).json" 2>/dev/null || true
    ok "drain timer stopped + node deregistered"
  fi
  echo "  Re-enable: sudo -u $WU XDG_RUNTIME_DIR=/run/user/\$(id -u $WU) systemctl --user enable --now baton-tick.timer"
  echo "  Full removal: ./teardown.sh"
  exit 0
fi

cat <<EOF

This PERMANENTLY removes this baton worker:
  - stops + removes the drain timer (user systemd units)
  - deletes the root-owned fence:  /opt/baton  and  /etc/claude-code/managed-settings.json
  - deletes the '$WU' account and its home $WHOME
       (instance clone, work/ repo clones, ~/.baton.env, ~/.git-credentials)

It does NOT touch your real repos or your own account. The GitHub PAT lives in $WHOME/.baton.env and
is deleted with the account — REVOKE it on GitHub afterward.
EOF
read -r -p "Type 'destroy' to proceed: " ans
[ "$ans" = "destroy" ] || { echo "aborted."; exit 1; }

# 1. Stop the worker FIRST — no unfenced execution during teardown.
if id "$WU" >/dev/null 2>&1; then
  sudo -u "$WU" env "XDG_RUNTIME_DIR=/run/user/$(id -u "$WU")" \
    systemctl --user disable --now baton-tick.timer 2>/dev/null || true
  sudo rm -f "$WHOME/.config/systemd/user/baton-tick.service" \
             "$WHOME/.config/systemd/user/baton-tick.timer" 2>/dev/null || true
  ok "worker timer stopped + units removed"
fi

# 2. Remove the root-owned fence.
sudo rm -rf /opt/baton /etc/claude-code/managed-settings.json && ok "fence removed (/opt/baton + managed-settings)"
sudo rmdir /etc/claude-code 2>/dev/null || true   # only if now empty

# 3. Remove the worker account + its home (clones, creds, env).
if id "$WU" >/dev/null 2>&1; then
  sudo loginctl disable-linger "$WU" 2>/dev/null || true
  sudo pkill -u "$WU" 2>/dev/null || true; sleep 1
  if sudo userdel -r "$WU" 2>/dev/null; then ok "user $WU + home $WHOME removed"
  else warn "userdel had trouble — check for running $WU processes, then: sudo userdel -r $WU"; fi
fi

say "teardown complete"
echo "  • REVOKE the GitHub PAT on GitHub (it lived in $WHOME/.baton.env)."
echo "  • This node may still be registered in the shared instance repo (nodes/$(hostname).json) —"
echo "    remove it there if you keep using baton from another machine."
