#!/usr/bin/env bash
# baton setup — ONE command to set up this machine as a worker and/or producer. Idempotent.
#
#   ./setup.sh                                  # interactive: asks role, prompts for tokens
#   non-interactive worker:  BATON_ROLE=worker BATON_PAT=<gh-pat> BATON_CLAUDE_TOKEN=<token> ./setup.sh
#   options:                 BATON_WORKER_USER=baton (default) · BATON_REPO=<url>
#
# WORKER setup is admin-run (uses sudo): it creates the dedicated unprivileged worker user and does
# the WHOLE cross-user setup itself — installs Claude Code + gh, writes the auth token + git creds,
# clones the engine, deploys the root-owned sandbox/fence, starts the drain timer, runs doctor.
# Jobs never run as you. PRODUCER setup installs the handoff skill into your ~/.claude. Both is fine.
#
# Claude auth is a long-lived token (run `claude setup-token` once on any machine you can log in
# from) — so the worker authenticates headless with NO browser /login on this box and no re-run.
set -uo pipefail
WU="${BATON_WORKER_USER:-baton}"
WHOME="/home/$WU"
ENGINE="$WHOME/baton"                         # the worker's own clone (BATON_HOME at runtime)
REPO_URL="${BATON_REPO:-https://github.com/pvolpe0/baton}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

b=$'\e[1m'; g=$'\e[32m'; y=$'\e[33m'; r=$'\e[31m'; z=$'\e[0m'
say(){ printf '\n%s%s%s\n' "$b" "$*" "$z"; }
ok(){ printf '  %s\xe2\x9c\x93%s %s\n' "$g" "$z" "$*"; }
warn(){ printf '  %s!%s %s\n' "$y" "$z" "$*"; }
bad(){ printf '  %s\xe2\x9c\x97%s %s\n' "$r" "$z" "$*"; }
ask(){ local a; read -r -p "  $1 " a; printf '%s' "$a"; }
asksecret(){ local a; read -r -s -p "  $1 " a; echo >&2; printf '%s' "$a"; }
yesno(){ local a; read -r -p "  $1 [y/N] " a; [[ "$a" =~ ^[Yy] ]]; }
asb(){ sudo -u "$WU" sh -c 'cd "$1" 2>/dev/null || cd /tmp; shift; exec "$@"' _ "$WHOME" "$@"; }  # as worker, from a readable cwd
asb_env(){ sudo -u "$WU" env "XDG_RUNTIME_DIR=/run/user/$(id -u "$WU")" "$@"; }

say "baton setup — autonomous coding handoff"
cat <<EOF
  Hand off in-progress work from your laptop to an always-on box; a sandboxed Claude Code agent
  finishes it and opens a draft PR for review.

  This sets up:
    • WORKER (admin): a dedicated unprivileged '$WU' user, Claude Code + gh, a root-owned OS sandbox
      (job writes confined to the project; no escalation; network open), and a drain timer.
    • PRODUCER: the 'hand this off' skill in your ~/.claude.

  You'll provide (worker): sudo · a GitHub fine-grained PAT (Contents + Pull requests, NO admin) ·
  a Claude auth token from 'claude setup-token'. Notifications are GitHub-native; SMTP is optional.
EOF
[ -t 0 ] && { read -r -p "  Press Enter to continue, or Ctrl-C to cancel... " _ || true; }

# ROLE -----------------------------------------------------------------------
ROLE="${BATON_ROLE:-}"
if [ -z "$ROLE" ]; then
  if [ -t 0 ]; then case "$(ask 'set up this machine as w)orker / p)roducer / b)oth :')" in p|P) ROLE=producer;; b|B) ROLE=both;; *) ROLE=worker;; esac
  else ROLE=worker; fi
fi
ok "role = $ROLE"

# PRODUCER (runs as you) -----------------------------------------------------
if [ "$ROLE" = producer ] || [ "$ROLE" = both ]; then
  say "producer — install skills (handoff + add-project)"
  for s in handoff add-project; do
    mkdir -p "$HOME/.claude/skills/$s" && cp "$HERE"/engine/skill/"$s"/* "$HOME/.claude/skills/$s/"
  done
  ok 'installed — say "hand this off: <task>" to run work, or "add this project" to register a repo'
fi

# WORKER (admin-run; creates + configures the dedicated user) -----------------
if [ "$ROLE" = worker ] || [ "$ROLE" = both ]; then
  sudo -v || { bad "worker setup needs sudo on this machine"; exit 1; }

  # tokens up front — both are needed before the clone/auth below.
  PAT="${BATON_PAT:-$(sudo grep -m1 '^GH_TOKEN=' "$WHOME/.baton.env" 2>/dev/null | cut -d= -f2-)}"
  if [ -z "$PAT" ] && [ -t 0 ]; then
    say "worker — GitHub token"
    echo "  Fine-grained PAT (Contents + Pull requests; NO admin): https://github.com/settings/personal-access-tokens"
    printf '  Paste the PAT (hidden): '; read -rs PAT; echo
  fi
  [ -z "$PAT" ] && warn "no PAT — private-repo clone + pushes will fail until GH_TOKEN is set in $WHOME/.baton.env"
  CTOK="${BATON_CLAUDE_TOKEN:-$(sudo grep -m1 '^CLAUDE_CODE_OAUTH_TOKEN=' "$WHOME/.baton.env" 2>/dev/null | cut -d= -f2-)}"
  if [ -z "$CTOK" ] && [ -t 0 ]; then
    say "worker — Claude auth token"
    echo "  On any machine where you can log in to Claude, run:  claude setup-token"
    echo "  (one browser login; prints a ~1-year token). Paste it here so the worker runs headless."
    printf '  Paste the Claude token (hidden, blank to skip): '; read -rs CTOK; echo
  fi

  # 1. worker user + lingering
  if id "$WU" >/dev/null 2>&1; then ok "user $WU exists"; else sudo useradd -m "$WU" && ok "created user $WU"; fi
  sudo loginctl enable-linger "$WU" && ok "lingering enabled"

  # 2. git credential (PAT) + git identity + engine clone
  if [ -n "$PAT" ]; then
    printf 'https://%s:%s@github.com\n' "$WU" "$PAT" | sudo tee "$WHOME/.git-credentials" >/dev/null
    sudo chown "$WU:$WU" "$WHOME/.git-credentials"; sudo chmod 600 "$WHOME/.git-credentials"
    asb git config --global credential.helper store && ok "git credential written"
  fi
  asb git config --global user.name  "baton worker"    # without an identity, `git commit` fails and
  asb git config --global user.email "baton@$(hostname -s 2>/dev/null || echo baton).local"  # job state can't push
  ok "git identity set"
  if asb test -d "$ENGINE/.git"; then asb git -C "$ENGINE" pull -q && ok "engine clone updated"; else
    asb git clone -q "$REPO_URL" "$ENGINE" && ok "engine cloned to $ENGINE" || bad "clone failed (PAT access to the repo?)"; fi

  # 3. per-user tools: claude + gh (no sudo)
  asb mkdir -p "$WHOME/.local/bin"
  if asb bash -lc 'command -v claude >/dev/null' || asb test -x "$WHOME/.local/bin/claude"; then ok "claude present"; else
    warn "installing claude for $WU"; asb bash -lc 'curl -fsSL https://claude.ai/install.sh | bash' && ok "claude installed" || bad "claude install failed"; fi
  if asb bash -lc 'command -v gh >/dev/null' || asb test -x "$WHOME/.local/bin/gh"; then ok "gh present"; else
    warn "installing gh for $WU"
    arch="$(dpkg --print-architecture 2>/dev/null || echo arm64)"
    rel="$(curl -fsSL https://api.github.com/repos/cli/cli/releases/latest)"
    ver="$(printf '%s' "$rel" | grep -m1 '"tag_name"' | cut -d'"' -f4 | tr -d v)"
    tmp="$(mktemp -d)"
    if curl -fsSL -o "$tmp/gh.tgz" "https://github.com/cli/cli/releases/download/v${ver}/gh_${ver}_linux_${arch}.tar.gz" && tar xzf "$tmp/gh.tgz" -C "$tmp"; then
      sudo install -o "$WU" -g "$WU" -D "$tmp/gh_${ver}_linux_${arch}/bin/gh" "$WHOME/.local/bin/gh" && ok "gh installed"
    else warn "gh install failed"; fi
    rm -rf "$tmp"; fi

  # 4. worker env: GH_TOKEN + Claude token + optional SMTP (preserved across re-runs)
  MAIL="$(sudo grep -hE '^(SMTP_|NOTIFY_EMAIL=)' "$WHOME/.baton.env" 2>/dev/null)"
  if [ -z "$MAIL" ] && [ -t 0 ]; then
    say "worker — notifications"
    ok "GitHub-native by default (the PR emails you; enable 'email about your own activity' in GitHub settings)."
    if yesno "optionally add a DIRECT email notifier (SMTP)?"; then
      EM="$(ask 'your email (sends + receives; From/To default to it):')"
      H="$(ask 'SMTP host [smtp.gmail.com]:')"; H="${H:-smtp.gmail.com}"
      [ "$H" = "smtp.gmail.com" ] && echo "       Gmail: use a 16-char App Password (myaccount.google.com/apppasswords; needs 2FA), not your login password."
      SP="$(asksecret 'SMTP password / app-password:')"
      MAIL="$(printf 'SMTP_HOST=%s\nSMTP_PORT=587\nSMTP_USER=%s\nNOTIFY_EMAIL=%s\nSMTP_PASS=%s' "$H" "$EM" "$EM" "$SP")"
    fi
  fi
  { [ -n "$PAT" ]  && printf 'GH_TOKEN=%s\n' "$PAT"
    [ -n "$CTOK" ] && printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\n' "$CTOK"
    [ -n "$MAIL" ] && printf '%s\n' "$MAIL"; } | sudo tee "$WHOME/.baton.env" >/dev/null
  sudo chown "$WU:$WU" "$WHOME/.baton.env"; sudo chmod 600 "$WHOME/.baton.env"
  ok "env written (${PAT:+GH_TOKEN }${CTOK:+CLAUDE_CODE_OAUTH_TOKEN }${MAIL:+SMTP }set)"

  # 5. root-owned engine + fence (the worker cannot edit ANY code it runs UNCONFINED). The executable
  #    engine is deployed to /opt/baton and tick/doctor run FROM there — never from the worker-writable
  #    clone, so a job that overwrote ~/baton/runner/tick.py can't make the next tick run poisoned code
  #    unconfined while building the fence. (Job state stays in the clone; the job's writable set is
  #    narrowed to running/<jid>, so it can't reach the engine there either.)
  sudo mkdir -p /opt/baton          # parent must exist — `cp -r src /opt/baton/runner` can't create it
  sudo rm -rf /opt/baton/runner /opt/baton/lib /opt/baton/bin /opt/baton/profile /opt/baton/guard /opt/baton/projects
  for d in runner lib bin profile guard; do sudo cp -r "$ENGINE/$d" "/opt/baton/$d"; done
  sudo find /opt/baton -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
  sudo install -D -m0644 "$ENGINE/profile/denied.json" /opt/baton/denied.json   # guard reads ../denied.json
  sudo install -D -m0644 "$ENGINE/profile/managed-settings.json" /etc/claude-code/managed-settings.json
  printf '%s' "$WU" | sudo tee /opt/baton/worker-user >/dev/null
  sudo chown -R root:root /opt/baton                                            # worker can't own/edit it
  sudo find /opt/baton -type d -exec chmod 0755 {} +                            # worker may traverse/read
  sudo find /opt/baton -type f -exec chmod 0644 {} +                            # worker may read, NOT write
  ok "engine + fence deployed root-owned to /opt/baton (worker-user=$WU; worker cannot edit it)"

  # 5b. claude-agent-sdk for the SDK worker engine — the DEFAULT (manifest.engine='sdk'; 'cli' is the
  #     per-job fallback). Installed in a ROOT-OWNED venv so worker.py runs it with `python3 -s` — the
  #     job-writable ~/.local is off its import path, so a job can't poison the worker's imports.
  #     Non-fatal here, but doctor FAILS without it (default jobs would block), so the operator is told.
  [ -x /opt/baton-sdk/bin/python ] || sudo python3 -m venv /opt/baton-sdk
  if sudo /opt/baton-sdk/bin/pip install -q --upgrade pip >/dev/null 2>&1 \
     && sudo /opt/baton-sdk/bin/pip install -q claude-agent-sdk >/dev/null 2>&1; then
    sudo chown -R root:root /opt/baton-sdk
    ok "claude-agent-sdk ready (/opt/baton-sdk; SDK worker engine available)"
  else
    warn "claude-agent-sdk install failed — SDK engine unavailable; the CLI engine still works"
  fi

  # 6. drain timer (worker user-systemd)
  asb mkdir -p "$WHOME/.config/systemd/user"
  asb cp "$ENGINE"/systemd/baton-tick.service "$ENGINE"/systemd/baton-tick.timer "$WHOME/.config/systemd/user/"
  asb_env systemctl --user daemon-reload && asb_env systemctl --user enable --now baton-tick.timer \
    && ok "drain timer enabled (~90s)" || warn "timer enable failed (check lingering / XDG_RUNTIME_DIR)"

  # 7. auth check — the token (in env) or a prior interactive login is required to run jobs
  if [ -n "$CTOK" ] || asb test -f "$WHOME/.claude/.credentials.json"; then ok "claude auth present"; else
    warn "no Claude auth yet — jobs can't run. On any machine you can log in from:  claude setup-token"
    echo "      then re-run with that token, or add CLAUDE_CODE_OAUTH_TOKEN=<token> to $WHOME/.baton.env"
  fi

  # 8. validate (registers the node; inert until green)
  say "validate"
  asb env BATON_STATE="$ENGINE" python3 -B /opt/baton/bin/baton install worker && say "baton worker is READY" \
    || { bad "doctor not green — see above"; exit 1; }
fi

say "done"
[ "$ROLE" != worker ]   && echo '  Producer: in any Claude Code session, say "hand this off: <task>".'
[ "$ROLE" != producer ] && echo "  Worker: draining every ~90s.  Pause: systemctl --user disable --now baton-tick.timer (as $WU)."
echo "  Add a project: see instructions.md.   Remove everything: ./teardown.sh"
