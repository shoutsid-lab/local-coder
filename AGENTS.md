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
4. `docs/CONVENTIONS.md` — coding, evidence, and roadmap conventions.

Treat these as the living required-reading set. Completed programme records are indexed by
`docs/HISTORY.md` and are consulted only when a task touches their subsystem or rationale.
Keep this file short and use deeper documents for detail.

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
- Do not expand GEPA, campaign, or prompt-deployment machinery merely because the change
  is easy to formalize. Follow the active priorities in `ROADMAP.md` and prefer direct
  capability work over another control-plane abstraction.
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
- `./local-coder.py repair ...`
- `./local-coder.py review ...`

LiteLLM must already be listening on `127.0.0.1:4000`. The trusted runtime owns the
single llama.cpp endpoint on `127.0.0.1:8080` and synchronously starts or switches the
qualification-bound physical model profile for each role. Do not start a competing
llama-server process or send direct concurrent requests around the route lease.

## Roadmap work-item labels

- Use descriptive programme names; track letters are optional navigation aids.
- Before assigning a track label, check `ROADMAP.md`, `docs/roadmaps/`, and
  `docs/HISTORY.md` for a collision. Do not reuse a label that would make current or
  retained records ambiguous.
- Do not reconstruct the repository's entire Git history merely to prove a letter is
  globally unused. The indexed roadmap and history documents are the naming registry.
- Root queue identifiers such as `R1` and `S1` are local to `ROADMAP.md` and are not
  programme-track claims.
- A detailed programme roadmap must state its status, relationship to the root queue,
  evidence requirements, and completion condition.
