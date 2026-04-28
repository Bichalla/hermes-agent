---
schema: pkm-frontmatter/v1
document_id: "hermes-agent-graphify-knowledge-plan-20260428"
title: "Hermes Agent Graphify Knowledge Corpus Plan"
subtitle: "핵심 산출물·문서 중심 Graphify 초기화와 watch 운영 계획"
created: "2026-04-28T10:15:56+09:00"
updated: "2026-04-28T14:51:59+09:00"
authors:
  - Hermes
owners:
  - honbul
status: approved
lifecycle: sprout
document_type: plan
audience:
  - honbul
language: ko
visibility: private
sensitivity: internal
priority: high
confidence: high
summary: >-
  Hermes Agent 전체 저장소를 무차별 Graphify하는 대신, TDD 부산물과 generated/vendor/public-docs 노이즈를 제외하고
  핵심 런타임, 운영 문서, 스킬 지식, 향후 생성 Markdown 문서를 지속적으로 색인하기 위한 계획이다.
tags:
  - hermes-agent
  - graphify
  - knowledge-ops
  - graph-rag
  - watch
aliases:
  - Hermes Graphify 계획
projects:
  - hermes-agent
areas:
  - knowledge-ops
  - agent-ops
resources: []
entities:
  people:
    - honbul
  organizations:
    - Nous Research
  brands:
    - Hermes
    - JÖKL
  products:
    - Hermes Agent
    - Graphify
  systems:
    - Hermes
    - Graphify
sources:
  - type: user_request
    title: "Hermes Agent Graphify knowledge corpus planning"
    url: null
    date: "2026-04-28"
links:
  canonical: /Users/honbul/.hermes/hermes-agent/.hermes/plans/hermes-agent-graphify-knowledge-plan.md
  source: null
  related:
    - /Users/honbul/.hermes/hermes-agent/AGENTS.md
    - /Users/honbul/.hermes/templates/pkm-frontmatter-template.md
relations:
  parent: null
  children: []
  depends_on:
    - /Users/honbul/.hermes/skills/graphify/SKILL.md
  supersedes: []
  superseded_by: null
review:
  cadence: on_change
  last_reviewed: "2026-04-28"
  next_review: null
governance:
  pkm_required: true
  frontmatter_required: true
  approval_required_for_external_publish: true
automation:
  indexing: true
  extract_tasks: true
  sync_targets:
    - local_markdown
version: 0.1.0
---

# Hermes Agent Graphify Knowledge Corpus Plan

> **For Hermes:** Use the `graphify` skill and this plan before initializing or watching the Hermes Agent knowledge graph.

## Goal

Build a Graphify corpus for Hermes Agent that improves codebase/document navigation without indexing TDD byproducts, generated/vendor material, or noisy public documentation trees.

## Current facts verified

- Incorrect previous target: `/Users/honbul/graphify` — Graphify's own repo, 143 files / ~167k words.
- Intended Hermes source root: `/Users/honbul/.hermes/hermes-agent`.
- Full Hermes detection without additional exclusions:
  - 2,297 supported files
  - ~3,371,415 words
  - 21 sensitive-looking files skipped
  - categories: 1,465 code, 813 document, 7 paper, 11 image, 1 video
- Largest noisy folders by file count:
  - `tests` 757
  - `skills` 389
  - `website` 269
  - `ui-tui` 254 detected by Graphify, but 16k total files on disk
  - `optional-skills` 186

## Design principle

Graphify should index **knowledge-bearing artifacts**:

1. runtime architecture and load-bearing source files,
2. operational and developer documents,
3. active skills / skill docs,
4. future Markdown docs produced by Hermes,
5. feedback-loop memory under `graphify-out/memory/`.

Graphify should not index routine byproducts:

1. tests and fixtures,
2. generated caches/build outputs,
3. package dependencies,
4. public website docs when they duplicate internal docs,
5. large frontend/generated trees unless explicitly needed,
6. credentials/secrets, which Graphify already skips heuristically.

## Recommended corpus tiers

### Tier 1 — Core runtime + operational docs

Use for first high-quality graph.

Approximate candidate size after baseline install/upstream doc pruning and `.graphifyinclude` hidden-path allowlisting:

- 296 files
- ~905,230 words
- 291 code files, 5 project-local operating documents

Include:

- project-local operating docs: `AGENTS.md`, `STATUS.md`, `ROADMAP.md`, `TODO.md`, plus curated `.hermes/plans/**/*.md`, `.hermes/reports/**/*.md`, and `.hermes/reviews/**/*.md` via `.graphifyinclude`
- root runtime files: `run_agent.py`, `model_tools.py`, `toolsets.py`, `cli.py`, `hermes_state.py`, `hermes_constants.py`, `hermes_logging.py`, `batch_runner.py`, `trajectory_compressor.py`, `mcp_serve.py`
- runtime directories: `agent/`, `hermes_cli/`, `tools/`, `gateway/`, `cron/`, `plugins/`, `scripts/`, `acp_adapter/`, `tui_gateway/`

Exclude:

- `tests/`, `website/`, `ui-tui/`, `web/`, `node_modules/`, generated/build/cache/vendor dirs.
- baseline install/upstream docs that are not useful for honbul's business PKM search: `README.md`, `CONTRIBUTING.md`, `SECURITY.md`, `RELEASE_*.md`, bundled plugin READMEs, upstream `plans/`/`docs/plans/`, and platform-addition guide docs.

### Tier 2 — Core + built-in skills

Use after Tier 1 validates well.

Approximate candidate size:

- 723 files
- ~1,620,807 words

Adds:

- `skills/`

Reasoning: active bundled skills are knowledge-bearing, but they add many Markdown files and can dominate graph communities. Add after core graph labels are acceptable.

### Tier 3 — Knowledge docs only

Use as a separate graph if the question is “which skill/doc should Hermes use?” rather than “how does Hermes runtime work?”

Approximate candidate size:

- 644 files
- ~1,039,023 words

Includes:

- `skills/`, `optional-skills/`, `plugins/`, `docs/`, `plans/`, `.plans/`, selected root docs.

## Proposed `.graphifyignore` baseline

Place at `/Users/honbul/.hermes/hermes-agent/.graphifyignore` only after confirming scope.

```gitignore
# Graphify corpus hygiene for Hermes Agent
# Keep knowledge-bearing source/docs; skip TDD byproducts, generated outputs, deps, public-site duplicates.

# Always-noisy development byproducts
tests/
**/tests/
test/
**/test/
__pycache__/
.pytest_cache/
.mypy_cache/
.ruff_cache/
.tox/
.eggs/
*.egg-info/

# Dependencies / package caches / generated frontend trees
node_modules/
ui-tui/node_modules/
package-lock.json
yarn.lock
pnpm-lock.yaml

# Build / generated outputs
dist/
build/
out/
target/
coverage/
htmlcov/
*.pyc
*.pyo
*.min.js
*.map

# Tier controls
# Tier 1 excludes bundled skill corpora. Remove skills/ only when enabling Tier 2.
skills/
optional-skills/

# Baseline install / upstream project docs that are useful for Hermes users but low-signal for honbul's business PKM search.
# Keep project-local operating docs (AGENTS.md, STATUS.md, ROADMAP.md, TODO.md, docs/operations/**) in scope.
README.md
CONTRIBUTING.md
SECURITY.md
RELEASE_*.md
constraints-termux.txt
hermes-already-has-routines.md
plans/*
docs/plans/*
plugins/**/README.md
gateway/platforms/ADDING_A_PLATFORM.md

# Public docs site and large UI/static assets: often duplicate internal knowledge or explode graph size
website/
web/
ui-tui/
assets/

# Sample/generated media data
neutts_samples/
**/neutts_samples/

# Infra/package metadata usually not useful for knowledge search
docker/
nix/
packaging/
datagen-config-examples/
environments/
tinker-atropos/
acp_registry/

# Graphify output; Graphify already skips this, keep explicit for readers
graphify-out/
```

## Proposed `.graphifyinclude` allowlist

Place at `/Users/honbul/.hermes/hermes-agent/.graphifyinclude` after using a Graphify version that supports hidden-path allowlisting.

```gitignore
# Graphify hidden-path allowlist for Hermes Agent
# Keep SSOT documents in project-local hidden operating folders while indexing only curated markdown.
# Sensitive-looking files are still hard-skipped by Graphify even when allowlisted.

.hermes/plans/**/*.md
.hermes/reports/**/*.md
.hermes/reviews/**/*.md
```

This preserves SSOT: project operating artifacts stay in `.hermes/`; Graphify indexes only the curated Markdown paths needed for retrieval.

## Watch-mode caveat

Graphify's current `watch.py` does **not** fully apply `.graphifyignore` in the filesystem event handler. Verified behavior from source:

- `detect()` respects `.graphifyignore` and, after the local Graphify patch, `.graphifyinclude` for curated hidden paths.
- `_rebuild_code()` calls `detect()`, so code rebuilds respect ignore rules.
- But the watch event handler only skips:
  - hidden path parts,
  - `graphify-out`,
  - unsupported extensions.
- Therefore a changed `.md` inside an ignored folder can still create `graphify-out/needs_update`, even though a later detect/update may ignore it.

Operational implication:

- Do not blindly run `graphify watch /Users/honbul/.hermes/hermes-agent` as the only governance layer unless this watch-ignore gap is patched or accepted.

## Watch strategy options

### Option A — Patch Graphify watch to respect `.graphifyignore` and `.graphifyinclude` before using root watch

Best long-term behavior.

Implementation idea:

1. Add ignore/include loading to `graphify/watch.py`.
2. In `Handler.on_any_event`, call `_is_ignored(path, watch_path.resolve(), ignore_patterns)` and the hidden-path allowlist check before setting `pending = True`.
3. Add regression tests:
   - ignored Markdown change does not create `needs_update`,
   - ignored code change does not rebuild,
   - non-ignored Markdown change creates `needs_update`,
   - non-ignored code change rebuilds graph.

Pros:

- One root watcher can safely monitor future `.md` docs.
- `.graphifyignore` becomes the single policy file.

Cons:

- Requires changing Graphify itself or waiting for upstream.

### Option B — Watch only curated knowledge directories

Good short-term behavior without patching Graphify.

Run separate watchers or a launch wrapper for curated directories only:

- `/Users/honbul/.hermes/hermes-agent/docs`
- `/Users/honbul/.hermes/hermes-agent/.hermes/plans` if using a Graphify watch version that applies `.graphifyinclude` at event time
- `/Users/honbul/.hermes/hermes-agent/.hermes/reports` if using a Graphify watch version that applies `.graphifyinclude` at event time
- `/Users/honbul/.hermes/hermes-agent/.hermes/reviews` if using a Graphify watch version that applies `.graphifyinclude` at event time
- `/Users/honbul/.hermes/hermes-agent/skills` if Tier 2 is enabled
- optionally `/Users/honbul/.hermes/skills` for installed/local skills

Pros:

- Avoids watch false positives from ignored folders.
- Very aligned with future `.md` document tracking.

Cons:

- Does not automatically rebuild when root runtime code changes unless separate code watcher is added.
- Multiple watcher processes or a wrapper script needed.

### Option C — Curated corpus mirror under `.hermes/graphify-corpora/` — rejected for now

Create a dedicated corpus directory and copy/sync selected docs/source snapshots there.

Pros:

- Closest to Graphify's original raw-folder model.
- Very clean graph.

Cons:

- Requires sync automation.
- Source paths become less direct unless metadata is preserved.
- Violates the preferred SSOT principle if not implemented as a generated cache.

Current decision: prefer `.graphifyinclude` hidden-path allowlisting over mirrors so project documents stay in one canonical location.

## Recommended plan

1. Start with refined Tier 1 corpus, `.graphifyignore` exclusion policy, and `.graphifyinclude` hidden-path allowlist.
2. Validate that detect includes `.hermes/plans/hermes-agent-graphify-knowledge-plan.md` without mirroring it into visible docs.
3. Validate the runtime/AST graph separately from docs/business-PKM search:
   - god nodes should be Hermes core concepts, not tests/fixtures/vendor nodes.
   - query “How does tool dispatch work?” should traverse `model_tools.py`, `tools/registry.py`, `toolsets.py`, and `run_agent.py`.
   - query “How does gateway session routing work?” should traverse `gateway/` and session/platform code.
   - query “How should Hermes Agent Graphify corpus be operated for honbul's business PKM?” should not be dominated by gateway/runtime hubs; if it is, split a separate docs/business-PKM graph.
4. If the runtime graph is clean, decide whether to patch AST primitive filtering for common nodes such as `.get()`, `str`, `.print()`, `.items()`, and `.set()`.
5. Add `skills/` only as Tier 2 after comparing report quality, preferably as a separate skill-discovery graph.
6. Do not add `optional-skills/` to the core graph unless the goal is skill discovery; keep it as a separate knowledge-docs graph if needed.
7. For watch:
   - Preferred: patch Graphify watch ignore/include event filtering, then run one root watcher.
   - Short term: run manual Graphify update for semantic changes or watch only explicitly curated visible docs folders.
8. Store the final commands and policy in a repo-local runbook after validation.

## Concrete next execution sequence

### Step 1 — Dry-run candidate scope

Run a detection simulation after creating temporary ignore rules, without committing anything.

Expected acceptance criteria:

- total supported files under 1,000,
- total words under 2M,
- no `tests/`, `website/`, `ui-tui/`, `node_modules/` in detected paths,
- sensitive skip count remains nonzero but filenames are not reported in chat.

### Step 2 — Create `.graphifyignore` and `.graphifyinclude`

Only after dry-run acceptance.

Files:

- `/Users/honbul/.hermes/hermes-agent/.graphifyignore`
- `/Users/honbul/.hermes/hermes-agent/.graphifyinclude`

### Step 3 — Initialize Tier 1 graph

Run Graphify from:

```bash
cd /Users/honbul/.hermes/hermes-agent
graphify .
```

If Graphify still warns about corpus size, proceed because Tier 1 is intentionally curated and under 2M words, but report the warning honestly.

### Step 4 — Verify graph usefulness

Run at least:

```bash
graphify benchmark graphify-out/graph.json
graphify query "How does tool dispatch work?" --budget 2500 --graph graphify-out/graph.json
graphify query "How does gateway session routing work?" --budget 2500 --graph graphify-out/graph.json
graphify path "AIAgent" "tool registry" --graph graphify-out/graph.json
```

### Step 5 — Watch setup

Short-term safe watcher should not rely on root watch until Graphify watch event filtering is patched for both `.graphifyignore` and `.graphifyinclude`. Manual detect/build/update now indexes curated `.hermes/` documents through `.graphifyinclude`; no visible-doc mirror is required.

Example visible-doc watcher after creating a curated docs corpus:

```bash
cd /Users/honbul/.hermes/hermes-agent
graphify watch docs --debounce 5
```

If Tier 2 is approved:

```bash
graphify watch skills --debounce 5
```

Long-term preferred watcher after patching Graphify watch ignore:

```bash
cd /Users/honbul/.hermes/hermes-agent
graphify watch . --debounce 5
```

## Decision needed

Proceed with either:

1. **Split docs/business-PKM graph next** — recommended for honbul-facing document search and reuse.
2. **Patch AST primitive filtering next** — recommended if one combined runtime graph must also answer business-PKM questions.
3. **Patch watch first** — fix Graphify watch ignore/include behavior before any always-on watcher.

Recommended: **split docs/business-PKM graph next**, then patch watch, then consider `skills/` as a separate skill-discovery graph.
