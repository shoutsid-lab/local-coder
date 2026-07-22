# Agent Skills integration

## Purpose

`local-coder` treats each directory under `.local-coder/skills/` as an Agent Skill while
preserving the repository's existing model, tool, worktree, editor, and verification
boundaries. The integration follows the open Agent Skills specification at
<https://agentskills.io/specification> and its progressive-disclosure lifecycle.

This document records the non-breaking Track A1 mapping. Portable packaging and optional
resource directories remain Track A2 work.

## Progressive-disclosure mapping

| Agent Skills stage | `local-coder` implementation |
|---|---|
| Discovery | `runtime.skills_loader.discover_skills()` scans each immediate child directory for `SKILL.md`, validates its standard YAML frontmatter, and retains only `name`, `description`, and the file location. The Markdown body is not retained or parsed. |
| Activation | Each managed role receives only its discovered name and description during hierarchy construction. When the manager invokes that role, the role's cached activator reads and validates the complete `SKILL.md` body. Model-backed adapters inject those instructions before execution; the fixed reviewer adapter activates the skill to validate the same lifecycle without expanding its hard-coded review gates. |
| Execution | The existing role adapter follows the activated instructions using only the model route, tool allowlist, and step limit defined by trusted runtime code. Supporting `scripts/`, `references/`, and `assets/` may be loaded on demand when Track A2 introduces them. |

The manager therefore pays the startup context cost of the five short descriptions, not
all five instruction bodies. Repeated invocation of the same role reuses its activated
skill for that run.

## Frontmatter contract

Every `SKILL.md` begins with standard Agent Skills frontmatter:

```yaml
---
name: atomic-implementation
description: Apply one narrow change. Use when an approved step requires exact edits.
---
```

The loader enforces the specification's strict requirements:

- `name` is 1–64 lowercase alphanumeric or hyphen characters, has no leading, trailing,
  or consecutive hyphen, and matches the parent directory name;
- `description` is non-empty and no longer than 1024 characters; repository-owned
  descriptions also state what the skill does and when it should be used;
- optional `license`, `compatibility`, `metadata`, and `allowed-tools` fields have the
  standard types and limits; and
- non-standard top-level frontmatter fields are rejected.

The repository does not put `model`, `tools`, or `max_steps` in portable skill
frontmatter. Those values are local security and resource controls, not instructions for
other clients.

## Trusted runtime binding

`runtime/skills.py` binds each discovered skill name to its established local model route,
tool allowlist, and maximum step count. Moving these controls out of skill-authored YAML
has two effects:

1. the skill files conform to the portable specification; and
2. editing a skill cannot grant a new tool, select an unapproved model route, or increase
   its execution budget.

The stable bindings remain:

| Skill | Route | Write authority |
|---|---|---|
| `explore-repository` | `local-plan` | none |
| `plan-change` | `local-plan` | none |
| `atomic-implementation` | `local-fast` | validated native editor only |
| `test-and-repair` | `local-fast` | validated native editor only |
| `review-change` | `local-review` | none |

`runtime/editor.py` remains the only source-editing component. Skill activation does not
add tools, execute resources, commit changes, or bypass deterministic verification.

## Compatibility API

The production orchestrator and smoke test use the lazy catalog from
`runtime.skills_loader`. `runtime.skills.discover_skills()` remains as an eager
compatibility helper for existing inspection code and contract tests; it activates every
skill and attaches the same trusted runtime bindings.
