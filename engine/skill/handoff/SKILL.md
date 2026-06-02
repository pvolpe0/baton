---
name: handoff
description: Use when the user says "hand this off" or "/handoff" — package the current in-progress work and send it to the baton worker (the always-on Pi) to finish autonomously, then report the job id.
---

# Handoff

Send the current task to the baton worker. The worker finishes it autonomously, opens a
draft PR, and notifies the user (push + email). Steps:

1. **Resolve the project.** Match the current directory against the `roots.mac` of each
   `~/baton/projects/*.json`. Use that project's name.

2. **Propose scope (do NOT sweep).** List candidate repos = repos the conversation
   touched ∪ currently-dirty repos. For each, show `git status --short`. Ask the user to
   confirm the in-scope set. Default is NOT "all dirty repos." For a brand-new task with
   nothing in progress, scope can be empty (a "fresh" handoff — just a brief).
   - If any in-scope repo has **untracked** files the user wants included, `git add` them
     explicitly first (the helper only stages tracked-modified files; never `git add -A`).

3. **Propose model + effort.** Default from the task: mechanical → `sonnet` / `low`;
   gnarly or ambiguous → `opus` / `high`. Confirm or let the user override. (Opus burns
   the monthly autonomous credit faster — lean Sonnet unless it's warranted.)

4. **Write the brief** to a temp file: goal, what's done, what's left, concrete next
   steps, success criteria, and any gotchas. Be specific — this is what the worker reads.

5. **Hand off:**
   ```
   python3 ~/.claude/skills/handoff/handoff.py \
     --project <name> --model <sonnet|opus> --effort <low|medium|high> \
     --brief-file <tmpfile> [--repo <repoA> --repo <repoB> ...]
   ```
   (Omit all `--repo` for a fresh task.) It prints the job id.

6. **Report** the job id + that the worker will pick it up within ~90s and notify when
   done/blocked. Then STOP touching that work in this session — it now belongs to the
   worker. If the user later edits the same files here, flag the divergence.
