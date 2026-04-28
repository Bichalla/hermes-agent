---
schema: pkm-frontmatter/v1
document_id: "hermes-agent-status-20260428"
title: "Hermes Agent Status"
subtitle: null
created: "2026-04-28T10:24:31+09:00"
updated: "2026-04-28T14:51:59+09:00"
authors:
  - Hermes
owners:
  - honbul
status: active
lifecycle: sprout
document_type: status
audience:
  - honbul
  - Hermes
language: ko
visibility: private
sensitivity: internal
priority: high
confidence: high
summary: >-
  Hermes Agent 프로젝트의 현재 상태, 진행 중인 Graphify knowledge corpus 작업, 운영 문서 구조를 추적한다.
tags:
  - hermes-agent
  - status
  - graphify
  - project-ops
aliases:
  - Hermes Agent current state
projects:
  - hermes-agent
areas:
  - agent-ops
  - knowledge-ops
resources: []
entities:
  people:
    - honbul
  organizations: []
  brands:
    - Hermes
  products:
    - Hermes Agent
    - Graphify
  systems:
    - Hermes
    - Graphify
sources:
  - type: user_request
    title: "Graphify corpus initialization approval"
    url: null
    date: "2026-04-28"
links:
  canonical: /Users/honbul/.hermes/hermes-agent/STATUS.md
  source: null
  related:
    - /Users/honbul/.hermes/hermes-agent/.hermes/plans/hermes-agent-graphify-knowledge-plan.md
    - /Users/honbul/.hermes/hermes-agent/.graphifyignore
relations:
  parent: null
  children: []
  depends_on:
    - /Users/honbul/.hermes/hermes-agent/AGENTS.md
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

# Hermes Agent Status

## Current focus

- **Workstream:** Graphify knowledge corpus initialization for Hermes Agent.
- **Approved approach:** Stepwise, safe Tier 1 initialization before adding broader skill/document corpora.
- **Primary goal:** Improve document/code search and reuse by indexing core knowledge-bearing artifacts, not TDD byproducts or generated/public noise.

## Current repository state notes

- Project-local operating structure was incomplete at the start of this work:
  - `AGENTS.md` existed.
  - `STATUS.md`, `ROADMAP.md`, `TODO.md`, `.hermes/`, and several `docs/` subfolders were missing.
- This status file now acts as the project-local current-state source of truth.
- There are unrelated pre-existing code changes in the working tree. Graphify/project-ops docs should be staged separately from those changes.

## Graphify workstream state

### Completed

- Verified intended source root: `/Users/honbul/.hermes/hermes-agent`.
- Verified previous Graphify run targeted the wrong repo: `/Users/honbul/graphify`.
- Measured full Hermes source detection:
  - 2,297 supported files
  - ~3,371,415 words
  - 21 sensitive-looking files skipped by Graphify
- Defined Tier 1 corpus as core runtime + operational docs, excluding tests, generated outputs, public docs site, frontend dependency trees, and other noise.
- Drafted and approved the Graphify knowledge corpus plan.
- Created `.graphifyignore` baseline for the approved Tier 1 corpus policy.
  - Tier 1 currently excludes `skills/` and `optional-skills/`; `skills/` should only be re-enabled after Tier 1 quality is verified.

### In progress

- Graph quality review follow-up: Graphify now supports `.graphifyinclude` hidden-path allowlisting, so `.hermes/plans/**/*.md`, `.hermes/reports/**/*.md`, and `.hermes/reviews/**/*.md` can remain SSOT in `.hermes/` while being indexed.
- Remaining graph quality decision: split a docs/business-PKM graph from the runtime/AST graph, or patch Graphify AST extraction to suppress common primitive nodes.
- Watch strategy decision remains open: detect/indexing allowlists work, but watch event filtering still needs separate verification/patching before root watch.

### Latest Tier 1 run evidence

#### Initial Tier 1 graph, before install-doc pruning

- Graphify dry-run after first `.graphifyignore`:
  - 323 supported files
  - ~944,824 words
  - 291 code, 32 documents, 0 media
  - 3 sensitive-looking files skipped by Graphify
  - excluded-folder spot check: `tests/`, `website/`, `ui-tui/`, `web/`, `node_modules/`, `skills/`, `optional-skills/` all had 0 detected hits
- Initial graph stats:
  - 11,829 nodes
  - 46,698 edges
  - 127 communities
- Initial quality finding:
  - Graph captured real Hermes gateway/runtime hubs (`Platform`, `BasePlatformAdapter`, `SessionDB`, `AIAgent`) but was still too AST-heavy for polished human browsing.
  - Detected document list contained upstream/install-baseline docs such as release notes, `README.md`, `CONTRIBUTING.md`, `SECURITY.md`, plugin READMEs, and upstream plans that are useful for Hermes users but low-signal for honbul's business PKM search.

#### Refined Tier 1 graph, after install-doc pruning and hidden-path allowlisting

- Updated `.graphifyignore` to exclude baseline install/upstream docs while keeping project-local operating docs in scope.
- Added `.graphifyinclude` after patching Graphify `detect()` so curated hidden operating documents remain in their SSOT location under `.hermes/`.
- Refined detect result after `.graphifyinclude`:
  - 296 supported files
  - ~905,602 words
  - 291 code, 5 documents, 0 media
  - remaining detected documents: `AGENTS.md`, `STATUS.md`, `ROADMAP.md`, `TODO.md`, `.hermes/plans/hermes-agent-graphify-knowledge-plan.md`
  - `.graphifyinclude` patterns loaded: 3 (`.hermes/plans/**/*.md`, `.hermes/reports/**/*.md`, `.hermes/reviews/**/*.md`)
  - excluded install-doc spot check: `README.md`, `CONTRIBUTING.md`, `SECURITY.md`, `RELEASE_*.md`, plugin READMEs, root `plans/*`, `docs/plans/*`, and `gateway/platforms/ADDING_A_PLATFORM.md` had 0 detected hits
- Refined graph output location: `/Users/honbul/.hermes/hermes-agent/graphify-out/`.
- Refined graph stats:
  - 11,608 nodes
  - 46,436 edges
  - 130 communities
  - 221 low-signal document-derived nodes pruned from the previous graph
- Benchmark output after pruning:
  - naive corpus estimate: ~1,204,338 tokens
  - average graph query cost: ~298,134 tokens
  - reduction: 4.0x fewer tokens per query
- Query quality finding:
  - The refined graph is cleaner on the document side, but business-PKM questions still get pulled toward runtime hubs (`Platform`, `BasePlatformAdapter`, gateway adapters) because AST nodes dominate the graph.
  - `graphify query "How should Hermes Agent Graphify corpus be operated for honbul's business PKM?"` still returned mostly gateway/runtime nodes, which means install-doc pruning alone is not sufficient for business-document search quality.
  - Next improvement should be either a separate docs/business-PKM graph, or a Graphify AST/noise filter for primitive/common nodes such as `.get()`, `str`, `.print()`, `.items()`, and `.set()`.

### Next steps

1. Decide graph split strategy:
   - **Recommended:** keep the current runtime/AST graph for engineering architecture, and create a separate docs/business-PKM graph for honbul-facing operating knowledge.
   - Alternative: patch Graphify AST extraction/export to suppress common primitive nodes before reusing one combined graph.
2. Patch Graphify watch ignore/include event filtering before enabling root-level watch, or run a curated docs-only watch.
3. After split/filtering, rerun practical queries against the intended use case before adding `skills/` as Tier 2.
4. Decide whether to add `skills/` as a separate skill-discovery graph rather than merging it into the runtime graph.

## Watch-mode decision

Graphify `detect()` now respects `.graphifyinclude` for curated hidden-path indexing, so `.hermes/` can remain the SSOT for project-local operating artifacts. The current allowlist indexes `.hermes/plans/**/*.md`, `.hermes/reports/**/*.md`, and `.hermes/reviews/**/*.md` while preserving Graphify's sensitive-file hard skips. However, the current watch event handler may still notice ignored or non-allowlisted Markdown changes and write `graphify-out/needs_update`. Therefore:

- Do **not** start root-level `graphify watch .` as an always-on watcher until watch ignore/include event filtering is patched or explicitly accepted.
- Short-term safe watch scope should be a docs/business-PKM graph or explicitly curated visible docs folders. Manual detect/build/update runs can include the allowlisted `.hermes/` operating docs through `.graphifyinclude`.

## Verification discipline

Before marking this workstream complete:

- Verify project-local docs have PKM frontmatter.
- Verify `.graphifyignore` excludes intended noise folders.
- Verify Graphify output lands under `/Users/honbul/.hermes/hermes-agent/graphify-out/`, not `/Users/honbul/graphify/graphify-out/`.
- Run at least one benchmark and two practical queries.
- Update this `STATUS.md` with results and next decisions.
