# Indexed Repository Intelligence

**Target repository:** `shoutsid-lab/local-coder`
**Status:** Complete — retained design and implementation record
**Root queue:** `ROADMAP.md` P1
**Primary role:** Explorer, followed by Planner and Reviewer context policies


## Completion record

IR1 through IR7 are implemented. The runtime now provides typed search contracts,
structured ripgrep current-worktree search, external repository registration, persistent
Zoekt indexes, Universal Ctags symbol caches, deterministic query routing, role-specific
context compilation, Explorer and Planner integration, query-time Git reconciliation, and
registered-only host discovery.

The operational contract is documented in
[`../REPOSITORY_INTELLIGENCE.md`](../REPOSITORY_INTELLIGENCE.md). IR8 IDE/LSP relationship
queries remain optional future capability work and are not required for this programme's
completion conditions.

## 1. Decision

Build repository intelligence by integrating established local search engines rather than
creating another content database or embedding platform inside local-coder.

The adopted stack is:

| Responsibility | Tool |
| --- | --- |
| Live current-worktree text and regex search | ripgrep |
| Persistent committed-state code and filename index | Zoekt |
| Symbol definitions and symbol-aware ranking | Universal Ctags |
| Repository snapshots, changes, and cache identity | Git |
| Host-wide filename/repository discovery | plocate on Linux/WSL; Everything on Windows |
| Query routing, result merging, clipping, and agent context | local-coder Repository Context Compiler |

Zoekt is the persistent search engine. ripgrep is the current-worktree truth and fallback.
Universal Ctags provides cheap structural information. Git connects persistent indexes to
committed source while allowing dirty worktrees to override them.

"All files" is implemented as two layers:

- every host filename and path is available through the existing plocate or Everything
  database;
- source and supported text content under registered roots is indexed by Zoekt and Ctags.

Binary files, model weights, virtual environments, build trees, databases, container layers,
and secrets remain metadata-only or excluded. This preserves system-wide discovery without
turning arbitrary host data into model context.

Do not build a custom SQLite full-text or vector index for source content. SQLite remains
the local-coder audit and run-state store.

## 2. Why this programme exists

The current `ReadOnlyEvidenceAgent` performs weak localisation:

1. extract filename-looking strings from the authoritative and delegated tasks;
2. read at most three of those files;
3. when none resolve, return the complete tracked-file list;
4. invoke the typed DSPy programme once.

The current `ToolContext.search_repository` implementation is also a repeated linear scan:
it calls `git ls-files`, reads every matching UTF-8 file in Python, checks one case-folded
substring, and stops after 100 lines.

This causes predictable failures:

- tasks that describe behaviour rather than filenames receive little useful context;
- filename, path, exact-code, symbol, and conceptual searches are not routed differently;
- repeated searches reread unchanged source;
- results have no ranking beyond filesystem order;
- the persistent committed state is not reused across runs or worktrees;
- the Explorer often receives a repository map instead of implementation evidence.

The target is not a general document assistant. It is a repository-intelligence layer for
local coding agents.

## 3. Proven components

### 3.1 Zoekt

Zoekt is a maintained source-code search engine used as Sourcegraph's indexed search
backend. It supports fast substring and regular-expression matching, Boolean queries,
filename filters, multi-repository indexes, and code-oriented ranking. It can operate
through local command-line tools before any long-running service is introduced.

Install the accepted Zoekt module version through the committed target:

```bash
make search-install
```

Optional service after the CLI integration is stable:

```bash
go install github.com/sourcegraph/zoekt/cmd/zoekt-webserver@latest
```

The project wraps these commands. Agents do not generate raw Zoekt query syntax or invoke
the binaries directly. The committed search profile pins the accepted Zoekt module version
rather than depending on `@latest`.

### 3.2 ripgrep

ripgrep is the live search path because it searches the current filesystem, respects ignore
rules, skips binary files by default, and provides structured JSON output.

Use it for:

- dirty and untracked worktree files;
- exact text and regex queries;
- stale-index fallback;
- repositories not yet indexed;
- focused searches over path filters.

Do not parse human-formatted terminal output. Invoke `rg --json` without a shell and parse
match records into typed results.

### 3.3 Universal Ctags

Universal Ctags emits machine-readable JSON Lines describing symbols, paths, languages,
kinds, scopes, and source patterns. Zoekt can also use Ctags information as a ranking
signal.

Use Ctags for the first structural layer:

- file outlines;
- exact symbol definitions;
- classes, functions, methods, variables, and headings where supported;
- qualified names and parent scopes;
- symbol-aware result ranking.

Do not initially build a custom AST or cross-language parser framework.

### 3.4 Git

Git is the saving and invalidation mechanism:

- repository path and remote identity identify the searchable project;
- commit and tree OIDs identify committed snapshots;
- blob OIDs identify unchanged content across branches and worktrees;
- `git status --porcelain=v2` identifies modified, added, deleted, renamed, and untracked
  worktree paths;
- commit, checkout, branch-switch, and worktree changes trigger index refresh.

The persistent index is derived cache data. It is not committed.

### 3.5 plocate and Everything

Host-wide discovery is separate from repository content search.

- `plocate` uses the host's existing filename database on Linux/WSL.
- Everything provides equivalent NTFS filename discovery on Windows.

These backends locate candidate repositories or files. Their results do not automatically
become readable by an agent. A repository must be registered or explicitly attached to a
run before the Repository Context Compiler can inspect its content.

## 4. Target architecture

```text
Configured roots / plocate / Everything
                 │
                 ▼
        Repository registry
                 │
       explicit search capability
                 │
                 ▼
┌─────────────────────────────────────────────────────────────┐
│                  Repository search plane                    │
│                                                             │
│  Zoekt committed index       Git + ripgrep live overlay     │
│            │                            │                    │
│            └──────────────┬─────────────┘                    │
│                           ▼                                  │
│                 Universal Ctags symbols                     │
│                           │                                  │
│                           ▼                                  │
│              Repository Context Compiler                    │
│                           │                                  │
│              bounded, current source ranges                 │
└───────────────────────────┬─────────────────────────────────┘
                            ▼
               Explorer / Planner / Reviewer
```

### Authority order

When sources disagree, use this order:

```text
current worktree bytes
    > dirty/untracked/deleted Git state
    > Zoekt committed-state result
    > repository registry metadata
```

A cached snippet is never authoritative. The compiler must reread selected ranges from the
active worktree immediately before supplying them to an agent.

## 5. Repository registry and persistent state

Store derived search state outside the repository:

```text
~/.local/share/local-coder/search/
├── registry.json
├── zoekt/
├── ctags/
├── locks/
└── status/
```

A registry entry contains:

```json
{
  "id": "local-coder",
  "path": "/home/jsumm/code/local-coder",
  "search_enabled": true,
  "symbol_enabled": true,
  "edit_enabled": false,
  "last_indexed_commit": "<git-oid>",
  "zoekt_index": "~/.local/share/local-coder/search/zoekt"
}
```

`edit_enabled` is not inferred from `search_enabled`. The active worktree and task plan
continue to define source-write authority.

The run-state database should record only search lineage and selected context:

- repository ID;
- active base commit;
- dirty-diff hash;
- backend and version;
- normalized query plan;
- selected paths and ranges;
- content hashes;
- truncation and fallback flags;
- timings and backend failures.

Do not store duplicate repository contents in SQLite.

## 6. Query routing

The Repository Context Compiler receives a task, role, worktree, repository capabilities,
and a hard context budget. It derives a small deterministic query plan.

| Signal | Route |
| --- | --- |
| Exact basename or extension | Zoekt filename search, then registry/host lookup |
| Relative path or path segment | Zoekt filename/path filter |
| Quoted error or exact code | ripgrep exact search, then Zoekt |
| CamelCase or snake_case identifier | Ctags symbol lookup, then Zoekt |
| Regex-like expression | ripgrep or Zoekt regex query |
| Natural-language behaviour | term expansion, Zoekt Boolean search, symbol reranking |
| Changed-file request | Git status and diff first |
| Definition/class/function request | Ctags first |
| Cross-repository request | only registered repositories attached to the run |

The model should not decide which backend to call in the first implementation. The compiler
handles routing before the existing one-shot DSPy call.

### Ranking signals

Use a transparent weighted score:

- exact filename or path-segment match;
- exact symbol definition;
- current dirty-file match;
- Zoekt rank;
- number and proximity of task terms;
- definition over incidental reference;
- source file over generated or fixture content;
- source/test pairing;
- active repository over attached secondary repositories.

Return a concise reason with every selected range.

## 7. Typed contracts

Implemented backend-neutral contracts:

```python
@dataclass(frozen=True)
class RepositorySearchRequest:
    query: str
    repository_ids: tuple[str, ...]
    worktree: Path
    mode: Literal[
        "auto", "filename", "text", "regex", "symbol", "changed"
    ] = "auto"
    path_globs: tuple[str, ...] = ()
    limit: int = 20
    timeout_seconds: float = 5.0
    active_repository_id: str | None = None


@dataclass(frozen=True)
class RepositorySearchHit:
    backend: Literal["ripgrep", "zoekt", "ctags", "git"]
    repository_id: str
    path: str
    start_line: int | None
    end_line: int | None
    score: float
    match_kind: str
    reason: str
    snippet: str = ""
    symbol_name: str | None = None
    stale: bool = False


@dataclass(frozen=True)
class RepositoryContextRange:
    repository_id: str
    path: str
    start_line: int
    end_line: int
    content: str
    content_sha256: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class RepositoryContextPack:
    base_commit: str
    worktree_diff_sha256: str
    queries: tuple[str, ...]
    ranges: tuple[RepositoryContextRange, ...]
    unresolved_terms: tuple[str, ...]
    truncated: bool
    selected_paths: tuple[str, ...] = ()
    backend_failures: tuple[str, ...] = ()
    query_plan: tuple[QueryCandidate, ...] = ()
    repository_states: dict[str, dict[str, str]] = field(default_factory=dict)
    backend_versions: dict[str, str] = field(default_factory=dict)
    degraded: bool = False
```

Keep backend-specific fields behind adapters. DSPy programmes receive the compiled context,
not backend responses.

## 8. Role integration

### Explorer

Replace the filename-regex/list fallback inside `ReadOnlyEvidenceAgent` with the Repository
Context Compiler.

Explorer policy:

- broad recall across architecture, implementation, tests, and documentation;
- several short ranges rather than complete files;
- explicit unresolved terms;
- one existing DSPy call after retrieval;
- no direct search tool loop in the first implementation.

### Planner

Use the same compiler with a narrower policy:

- exact definitions and contracts;
- likely editable files;
- nearby tests;
- current changed paths;
- fewer, longer authoritative ranges.

### Reviewer

A later policy should prioritize:

- changed files and complete diff;
- definitions and call sites related to changed symbols;
- relevant tests and invariants;
- unrelated changed paths.

### Implementer and repairer

Do not widen write authority. They may receive better planner context, but the native exact
editor remains the only source-writing boundary.

## 9. Implementation slices

### IR1 — Complete — Structured live search

Add:

```text
runtime/search/__init__.py
runtime/search/contracts.py
runtime/search/ripgrep_backend.py
runtime/search/query_router.py
tests/test_repository_search.py
```

Work:

- replace the Python file-by-file substring loop with `rg --json`;
- retain `ToolContext.search_repository` compatibility;
- add filename, fixed-string, regex, glob, result-count, and timeout options;
- normalize paths through the existing trusted worktree boundary;
- cover ignored, binary, malformed UTF-8, empty, and limited-result cases.

### IR2 — Complete — Repository registry and Zoekt CLI backend

Add:

```text
runtime/search/registry.py
runtime/search/zoekt_backend.py
runtime/search/index_manager.py
profiles/repository-search-v1.json
tests/test_search_registry.py
tests/test_zoekt_backend.py
```

Add CLI commands:

```bash
./local-coder.py index add PATH [--id NAME]
./local-coder.py index remove NAME
./local-coder.py index build [NAME]
./local-coder.py index refresh [NAME]
./local-coder.py index status
./local-coder.py search QUERY [--repo NAME] [--mode MODE]
```

The backend must support a clean fallback when Zoekt is not installed or an index is stale.

### IR3 — Complete — Symbol intelligence

Add:

```text
runtime/search/ctags_backend.py
tests/test_ctags_backend.py
```

Work:

- invoke Universal Ctags JSON output over tracked/current files;
- index definitions by repository, path, language, kind, scope, and line;
- support file outline and exact symbol queries;
- configure Zoekt to use Ctags symbol data where available;
- generate dirty-path symbols transiently without regenerating unrelated committed data.

### IR4 — Complete — Repository Context Compiler

Add:

```text
runtime/search/context_compiler.py
runtime/search/policies.py
tests/test_repository_context.py
```

Work:

- derive deterministic query candidates from task text;
- route to filename, text, regex, symbol, and changed-file backends;
- merge and rank hits;
- reread current authoritative source ranges;
- enforce repository capabilities and context budgets;
- record context lineage in the existing run store.

### IR5 — Complete — Explorer integration

Modify:

```text
runtime/agents.py
runtime/tools.py
tests/test_repository_context.py
```

Work:

- replace the three-filename/list fallback;
- keep the current one-shot `ExplorerProgram` contract;
- preserve grounding checks against supplied context;
- include selected-path and query metadata in DSPy traces;
- retain the current fallback when external search tools are unavailable.

After Explorer is stable, add role-specific Planner and Reviewer policies without changing
their source-write authority.

### IR6 — Complete — Automatic refresh and Git-aware saving

Work:

- compare `HEAD` with the registered `last_indexed_commit` at startup and before query;
- refresh Zoekt after commit or branch changes;
- use the ripgrep/Git overlay immediately for dirty and untracked paths;
- suppress committed-index results for deleted or replaced paths;
- use file hashes for changed Ctags entries;
- rebuild corrupt or incompatible derived indexes rather than migrating arbitrary shards;
- add an optional lightweight watcher only after query-time reconciliation works.

### IR7 — Complete — Host and cross-repository discovery

Work:

- discover repositories under configured roots;
- optionally query `plocate` or Everything for exact filenames;
- require explicit repository registration before content access;
- allow Explorer to search attached repositories while keeping edit scope on the active
  worktree;
- display repository IDs on every cross-repository result.

### IR8 — Deferred optional IDE-grade relationships

After the core search stack is operational, add a read-only semantic navigation adapter
inspired by Serena's language-server-backed tools:

- find referencing symbols;
- find implementations;
- diagnostics;
- dependency and external-project navigation.

Prefer a narrow adapter or read-only Serena configuration. Do not import Serena's editing,
refactoring, memory, or debugging authority into local-coder's Explorer.

## 10. Installation and operator experience

Add an installation target such as:

```bash
make search-install
make search-check
```

`search-check` should report:

```text
ripgrep           OK
Zoekt CLI         OK
Universal Ctags   OK
registry           3 repositories
indexes            current
```

Normal local-coder startup should not fail solely because Zoekt or Ctags is absent. It
should fall back to ripgrep and report the degraded search mode. A configured deployment
may choose to require all backends.

Do not start a persistent Zoekt webserver by default. Begin with command-based usage. Add a
managed local service only when repeated CLI startup becomes a material cost.

## 11. Completion conditions

This programme completed with the following conditions satisfied:

- [x] exact filename, path, text, regex, and symbol-definition queries are supported;
- [x] registered repositories reuse a persistent Zoekt index across runs;
- [x] dirty, deleted, renamed, and untracked worktree files are represented correctly;
- [x] selected context is reread from current files before model use;
- [x] Explorer receives ranked bounded context rather than an unranked file list;
- [x] Planner can use a narrower context policy without changing route or write authority;
- [x] host-wide discovery cannot bypass repository registration;
- [x] search failures degrade to ripgrep rather than blocking the coding pipeline;
- [x] index files are external, disposable, and rebuildable;
- [x] indexing is command-based, bounded, and does not start a persistent service;
- [x] focused retrieval tests, Python compilation, CLI checks, and diff checks pass.

No separate benchmark campaign or holdout programme is required before implementation.
Focused retrieval fixtures and real manual repository searches are sufficient to confirm
that the integration works. Broader performance comparison is optional tuning after the
capability is in regular use.

## 12. Non-goals

- replacing Git with a search database;
- indexing model weights, virtual environments, build trees, container layers, browser
  data, credentials, or arbitrary binary files;
- committing Zoekt shards, Ctags caches, or machine-specific registry paths;
- making a cloud service part of the core loop;
- giving Explorer shell access;
- giving search backends source-write authority;
- changing the one-shot Explorer into an open-ended search agent in the first delivery;
- building a custom vector database before lexical and symbolic search are operational;
- deploying the full Sourcegraph platform.

## 13. Upstream references

- Zoekt: <https://github.com/sourcegraph/zoekt>
- ripgrep: <https://github.com/BurntSushi/ripgrep>
- Universal Ctags: <https://github.com/universal-ctags/ctags>
- Universal Ctags JSON output: <https://docs.ctags.io/en/latest/man/ctags-json-output.5.html>
- Serena: <https://github.com/oraios/serena>
- plocate manual: <https://manpages.ubuntu.com/manpages/noble/man8/updatedb.plocate.8.html>
- Everything: <https://www.voidtools.com/>
