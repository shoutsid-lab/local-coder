# Repository Instructions for Primary Actors

## Purpose

These instructions apply to the primary actor working on the repository, whether a
trusted service, a more capable model, or a human operator. This repository is a fully
local, role-separated coding-agent stack designed for a GTX 1660 Ti with 6 GiB VRAM and
8 GiB system RAM. Preserve the architecture described
in `docs/ARCHITECTURE.md`; do not redirect the project into a generic CLI wrapper or
replace the local stack with a cloud-first design.

## Read first

1. `ROADMAP.md` — active implementation direction and next work.
2. `docs/ARCHITECTURE.md` — frozen architecture and component boundaries.
3. `docs/PIPELINE.md` — deterministic workflow and safety gates.
4. `docs/HANDOFF.md` — completed recursive-improvement control-plane record.
5. `docs/CONVENTIONS.md` — coding and editing conventions.

Treat `ROADMAP.md` as the active work queue and the remaining documents as the source
of truth for established behaviour and boundaries. Keep this file short and use the
deeper documents for detail.

## Required development workflow

- Use the project interpreter: `.venv/bin/python`.
- Install agent dependencies with `make agent-install` when needed.
- Run `make verify` after every code change.
- Run `make agent-smoke` after changes to `runtime/`, skills, model routing, or agent
  dependencies.
- Before handing work back, run `make handoff-check` from a committed, clean tree.
- Keep changes narrowly scoped and update or add deterministic tests.

## Architectural constraints

- Keep llama.cpp as the local inference runtime and LiteLLM as the role router.
- Keep logical routes `local-fast`, `local-plan`, and `local-review` stable.
- Keep the validated native atomic editor as the only component authorised to perform
  source edits during local agent runs.
- Keep Git worktrees as the isolation boundary and SQLite as the audit store.
- Never add an automatic commit, merge, push, or destructive worktree cleanup step.
- Never weaken verification, acceptance criteria, or protected tests to make a run pass.
- Do not introduce Claude or a required cloud model dependency.
- Do not download larger models or change hardware profiles unless the task explicitly
  requests it.

## Protected and generated content

- Treat `ROADMAP.md`, `docs/HANDOFF.md`, and every `*_contract.py` file as protected
  unless the requesting actor explicitly authorizes the change.
- Do not edit `.local-coder/state/`, `.local-coder/runs/`, `.worktrees/`, generated
  `REVIEW.json` files, or trusted evaluation holdout/oracle data.
- Treat all of `evaluation/` and `tests/test_evaluation_contract.py` as protected trusted
  controls during candidate runs.
- Do not commit virtual environments, secrets, legacy Aider histories, SQLite databases,
  or generated worktrees.

## Service-dependent commands

`make verify` and unit tests do not require local model services. These commands do:

- `./local-coder.py status`
- `./local-coder.py run "..."`
- direct native editing, planning, repair, and semantic-review commands

They expect llama-server on `127.0.0.1:8080` and LiteLLM on `127.0.0.1:4000`.
Check whether both are already running before starting anything new.

## Roadmap work-item labels

- Every roadmap track and subsection label (`A1`, `B2`, `C3`, ...) is permanent once a
  roadmap document ships, even after that roadmap is completed, retired, or superseded.
  These labels get written into commit messages, PR titles, and the audit trail, so
  reusing one makes `git log --grep`, blame, and `analyze-runs`/GEPA history mining
  return commits from two unrelated pieces of work with no way to tell them apart.
- Never restart labeling at `A1` (or any earlier-used letter/number pair) when writing a
  new roadmap, adding a track to an existing one, or authoring a roadmap that replaces
  an earlier one. Labels are sequential across the repository's entire history, not
  per-document.
- Before assigning new labels, find the highest track letter ever used — check the
  current `ROADMAP.md`, then `docs/HANDOFF.md` and any prior roadmap it references — and
  start the next track one letter after it. Do not infer availability from the current
  `ROADMAP.md` alone if an earlier roadmap has already been retired out of it.
- Every roadmap must open with a **Track ledger** line stating which letter range it
  claims and which earlier ranges are retired, so the next actor doesn't have to
  reconstruct the sequence from `git log`.
- This applies to any primary actor that authors or edits `ROADMAP.md`.