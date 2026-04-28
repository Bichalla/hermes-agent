---
schema: pkm-frontmatter/v1
document_id: "hermes-agent-todo-20260428"
title: "Hermes Agent TODO"
subtitle: null
created: "2026-04-28T10:24:31+09:00"
updated: "2026-04-28T14:23:30+09:00"
authors:
  - Hermes
owners:
  - honbul
status: active
lifecycle: sprout
document_type: todo
audience:
  - honbul
  - Hermes
language: ko
visibility: private
sensitivity: internal
priority: high
confidence: high
summary: >-
  Hermes Agent 프로젝트의 현재 작업 큐와 Graphify knowledge corpus 초기화 작업을 추적한다.
tags:
  - hermes-agent
  - todo
  - graphify
aliases: []
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
    title: "Graphify stepwise approval and docs upkeep"
    url: null
    date: "2026-04-28"
links:
  canonical: /Users/honbul/.hermes/hermes-agent/TODO.md
  source: null
  related:
    - /Users/honbul/.hermes/hermes-agent/STATUS.md
    - /Users/honbul/.hermes/hermes-agent/ROADMAP.md
relations:
  parent: null
  children: []
  depends_on: []
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

# Hermes Agent TODO

## Graphify knowledge corpus

- [x] Confirm intended corpus is Hermes Agent knowledge-bearing artifacts, not the Graphify repo itself.
- [x] Draft and approve a stepwise Graphify corpus plan.
- [x] Create project-local operating docs needed to preserve context.
- [x] Create `.graphifyignore` baseline for curated Tier 1 corpus.
- [x] Run Tier 1 Graphify detect dry-run with `.graphifyignore` applied.
- [x] Confirm excluded folders are absent from detected files.
- [x] Initialize Tier 1 graph under `/Users/honbul/.hermes/hermes-agent/graphify-out/`.
- [x] Run benchmark and sample queries.
- [x] Update `STATUS.md` with graph stats, query evidence, and watch risk.
- [x] Review initial graph quality and prune baseline install/upstream docs from Tier 1.
- [ ] Decide whether to split docs/business-PKM graph from runtime/AST graph.
- [ ] Decide whether to patch Graphify AST extraction/export to suppress primitive/common nodes.
- [ ] Decide whether to patch Graphify watch ignore handling.
- [ ] Decide whether to add `skills/` as Tier 2 after Tier 1 quality check, preferably as a separate skill-discovery graph.

## Project operating docs

- [x] Create `STATUS.md`.
- [x] Create `ROADMAP.md`.
- [x] Create `TODO.md`.
- [x] Add or preserve PKM frontmatter on materially edited project docs.
- [x] Create project-local `.hermes/` subfolders for plans, reports, reviews, handoffs, and context.
- [x] Add a Graphify section to `AGENTS.md` so future agents do not lose the corpus policy.
