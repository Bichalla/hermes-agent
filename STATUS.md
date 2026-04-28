---
schema: pkm-frontmatter/v1
document_id: "hermes-agent-status-20260428"
title: "Hermes Agent Status"
subtitle: null
created: "2026-04-28T10:24:31+09:00"
updated: "2026-04-28T10:24:31+09:00"
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

- Tier 1 graph quality review.
- Watch strategy decision after seeing initial graph characteristics.

### Latest Tier 1 run evidence

- Graphify dry-run after `.graphifyignore`:
  - 323 supported files
  - ~944,824 words
  - 291 code, 32 documents, 0 media
  - 3 sensitive-looking files skipped by Graphify
  - excluded-folder spot check: `tests/`, `website/`, `ui-tui/`, `web/`, `node_modules/`, `skills/`, `optional-skills/` all had 0 detected hits
- Tier 1 graph output location: `/Users/honbul/.hermes/hermes-agent/graphify-out/`.
- Generated outputs:
  - `graphify-out/graph.json`
  - `graphify-out/GRAPH_REPORT.md`
  - `graphify-out/manifest.json`
- HTML visualization was intentionally skipped because the graph has more than 5,000 nodes.
- Initial graph stats:
  - 11,829 nodes
  - 46,698 edges
  - 127 communities
- Extraction mix:
  - 11,635 AST nodes from 291 code files
  - 250 semantic document nodes from 32 docs
  - 53,895 pre-build merged edges before graph normalization
- Benchmark output:
  - naive corpus estimate: ~788,600 tokens
  - average graph query cost: ~298,134 tokens
  - reduction: 2.6x fewer tokens per query
- Initial quality finding:
  - Graph captures real Hermes gateway/runtime hubs (`Platform`, `BasePlatformAdapter`, `SessionDB`, `AIAgent`) but is still too AST-heavy for polished human browsing.
  - `graph.html` is not viable at this size; querying/report/path traversal are the useful modes for this first graph.
  - Next improvement should be either a narrower runtime/doc graph, AST node filtering for builtins/common primitives, or a separate docs/skills graph.

### Next steps

1. Run Graphify detection with `.graphifyignore` applied.
2. Confirm detected file/word counts and absence of excluded folders.
3. Initialize Tier 1 graph.
4. Verify outputs:
   - `graphify-out/graph.json`
   - `graphify-out/GRAPH_REPORT.md`
   - `graphify-out/graph.html` if node count permits
   - `graphify-out/manifest.json`
5. Run benchmark and sample queries.
6. Decide whether to patch Graphify watch before enabling root-level watch.
7. Decide whether to add `skills/` as Tier 2.

## Watch-mode decision

Graphify `detect()` respects `.graphifyignore`, but the current watch event handler can still notice ignored Markdown changes and write `graphify-out/needs_update`. Therefore:

- Do **not** start root-level `graphify watch .` as an always-on watcher until watch ignore behavior is patched or explicitly accepted.
- Short-term safe watch scope should be curated docs folders only, for example `docs/`, `plans/`, `.plans/`, and possibly `skills/` after Tier 2 approval.

## Verification discipline

Before marking this workstream complete:

- Verify project-local docs have PKM frontmatter.
- Verify `.graphifyignore` excludes intended noise folders.
- Verify Graphify output lands under `/Users/honbul/.hermes/hermes-agent/graphify-out/`, not `/Users/honbul/graphify/graphify-out/`.
- Run at least one benchmark and two practical queries.
- Update this `STATUS.md` with results and next decisions.
