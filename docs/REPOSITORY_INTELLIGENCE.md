# Repository Intelligence

## Purpose

The repository-intelligence layer gives Explorer and Planner ranked, bounded, current source
context before their existing one-shot DSPy call. It is read-only and does not widen edit
scope, source-write authority, model routes, or worktree isolation.

The implementation combines:

- ripgrep JSON search for current worktree text and regex matches;
- Zoekt JSONL search for reusable committed-state indexes;
- Universal Ctags JSON output for exact symbol definitions and file outlines;
- Git commit identity and porcelain-v2 state for dirty, deleted, renamed, and untracked
  overlays; and
- the Repository Context Compiler for deterministic query routing, ranking, current-byte
  rereads, clipping, and audit lineage.

Zoekt and Ctags are optional accelerators. Missing or stale persistent indexes degrade to
ripgrep. Missing ripgrep degrades further to a bounded Git/Python fallback so repository
search does not block the coding pipeline.

## Install and check

The committed tool names, limits, and accepted Zoekt module pin are in
`profiles/repository-search-v1.json`.

Install system packages first:

```bash
sudo apt install ripgrep universal-ctags golang-go plocate
```

Then build the pinned Zoekt commands and inspect readiness:

```bash
make search-install
make search-check
```

`make search-install` does not start a service. The implementation uses command-based Zoekt
search and isolated per-repository index directories.

## External state

Derived state is stored outside source repositories:

```text
~/.local/share/local-coder/search/
├── registry.json
├── zoekt/<repository-id>/
├── ctags/<repository-id>.jsonl
├── locks/<repository-id>.lock
└── status/<repository-id>.json
```

Set `LOCAL_CODER_SEARCH_HOME` to override this root for tests or an isolated deployment.
Indexes are disposable and rebuildable. They must not be committed or copied into a coding
worktree.

A registry entry grants search capability only. `edit_enabled` remains false and is not
inferred from repository registration.

## Repository registration and indexes

```bash
./local-coder.py index add ~/code/local-coder --id local-coder
./local-coder.py index build local-coder
./local-coder.py index refresh local-coder
./local-coder.py index status local-coder
./local-coder.py index remove local-coder
```

Omit the repository ID from `build`, `refresh`, or `status` to operate on every registered
repository.

Query-time reconciliation compares the current `HEAD` with `last_indexed_commit`. A commit or
branch change rebuilds the committed Zoekt index. Dirty and untracked files are searched from
current bytes immediately. Deleted paths suppress stale committed results. Ctags rows are
generated transiently for dirty paths while the persistent cache remains tied to the
committed snapshot.

## Search CLI

Search the active repository:

```bash
./local-coder.py search 'RepositoryContextCompiler'
./local-coder.py search 'RepositoryContextCompiler' --mode symbol
./local-coder.py search 'runtime/search' --mode filename
./local-coder.py search 'class .*Backend' --mode regex --glob 'runtime/search/*.py'
./local-coder.py search 'changed files' --mode changed
```

Search one or more registered repositories without changing edit authority:

```bash
./local-coder.py search 'ModelServiceManager' \
  --repo local-coder \
  --repo ascendant-automaton
```

Attach registered repositories to Explorer and Planner for one coding run:

```bash
./local-coder.py run 'Update the shared model-service contract' \
  --search-repo ascendant-automaton

./local-coder.py run-plan-step task-plan.json STEP_ID \
  --approve-plan-hash SHA256_FROM_VALIDATE_PLAN \
  --search-repo ascendant-automaton
```

Attached repositories remain read-only. The active task plan and isolated worktree still
define the complete edit scope. Unknown or search-disabled repository IDs fail before model
services are started.

Output is structured JSON containing normalized hits, backend failures, and degraded-mode
status. Agents do not receive raw Zoekt query syntax and cannot invoke search binaries
through a shell.

## Host and repository discovery

Repository discovery is metadata-only:

```bash
./local-coder.py index discover ~/code --depth 5
./local-coder.py index locate AGENTS.md
```

`discover` finds Git repositories under explicit roots but does not register them. `locate`
uses plocate when available and returns only files already contained by registered
repositories. Neither command grants content access or edit scope.

Windows Everything remains an operator-side equivalent for host filename discovery. It is
not invoked by the WSL runtime and its results must still be registered before content
search.

## Context compiler behavior

The compiler derives a bounded deterministic query plan from task text:

- named paths and basenames route to filename search;
- quoted code or errors route to fixed-string or regex search;
- CamelCase and snake_case identifiers route to Ctags and Zoekt symbol search;
- changed-file language adds Git overlay results; and
- remaining behavior terms route to Zoekt Boolean search plus current-worktree ripgrep.

Ranking favors exact paths, exact symbols, dirty files, source files, relevant tests, and
active-repository results. Generated and fixture paths receive a penalty. Selected ranges
are reread from the active filesystem immediately before model use and hashed after the
reread.

Explorer uses broader recall with more short ranges. Planner uses fewer, longer ranges for
definitions, likely editable files, tests, and current changes. The native editor remains
the only source-writing component.

Each audited run records repository-context lineage as an artifact:

- per-repository commit, tree, and dirty-state hashes;
- normalized queries and the weighted query plan;
- selected repository IDs, paths, and line ranges;
- content hashes and ranking reasons;
- unresolved terms, clipping, timings, backend versions, and degraded-mode failures.

Repository content itself is not duplicated in the SQLite lineage record.

## Failure and recovery behavior

- A missing Zoekt or Ctags binary reports degraded mode and continues with ripgrep.
- A stale Zoekt index is refreshed at query time; current dirty bytes still win immediately.
- A corrupt Ctags JSONL cache is rejected and rebuilt rather than migrated.
- A stale index lock older than ten minutes is replaced; an active lock fails the explicit
  index operation rather than allowing concurrent writers.
- Search results are hints only. The compiler rejects path escape and trusted evaluator
  prefixes and rereads selected current files before model use.

## Verification

Run the focused and repository-wide checks:

```bash
make repository-search-check
make verify
make agent-smoke
make skills-lint
git diff --check
```
