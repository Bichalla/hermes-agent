---
schema: pkm-frontmatter/v1
document_id: "hermes-agent-roadmap-20260428"
title: "Hermes Agent Roadmap"
subtitle: null
created: "2026-04-28T10:24:31+09:00"
updated: "2026-04-28T14:23:30+09:00"
authors:
  - Hermes
owners:
  - honbul
status: active
lifecycle: sprout
document_type: roadmap
audience:
  - honbul
  - Hermes
language: ko
visibility: private
sensitivity: internal
priority: high
confidence: medium
summary: >-
  Hermes Agent 프로젝트의 운영·지식관리·개발 방향을 추적하는 로드맵이다.
tags:
  - hermes-agent
  - roadmap
  - graphify
  - knowledge-ops
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
    title: "Maintain project operating docs during Graphify initialization"
    url: null
    date: "2026-04-28"
links:
  canonical: /Users/honbul/.hermes/hermes-agent/ROADMAP.md
  source: null
  related:
    - /Users/honbul/.hermes/hermes-agent/STATUS.md
    - /Users/honbul/.hermes/hermes-agent/TODO.md
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

# Hermes Agent Roadmap

## Near-term: Graphify knowledge layer

### Milestone 1 — Curated Tier 1 graph

Objective: Build a high-signal Graphify graph over Hermes core runtime and project-local operating docs, excluding baseline install/upstream documentation that is not useful for honbul's business PKM search.

Acceptance criteria:

- `.graphifyignore` excludes TDD byproducts, generated outputs, dependency trees, website/public docs noise, large frontend trees, and baseline install/upstream docs such as release notes and bundled plugin READMEs.
- Graphify outputs are created under `/Users/honbul/.hermes/hermes-agent/graphify-out/`.
- God nodes and communities reflect Hermes runtime/tool/gateway concepts, not tests/fixtures/vendor concepts.
- Project-local operating docs remain searchable without upstream documentation dominating document semantics.
- Benchmark and practical queries demonstrate useful traversal for the intended graph type.
- If business-PKM queries still route mostly through runtime hubs, split a separate docs/business-PKM graph instead of forcing one graph to serve both architecture and business-document search.

### Milestone 2 — Watch governance

Objective: Safely track future Markdown knowledge updates without false positives from ignored folders.

Acceptance criteria:

- Either Graphify watch is patched to respect `.graphifyignore` at event time, or curated-directory watch is used intentionally.
- Root-level watch is not enabled until the ignore behavior is safe.
- `STATUS.md` records the chosen watch mode and verification evidence.

### Milestone 3 — Tier 2 knowledge expansion

Objective: Add bundled skill knowledge only after Tier 1 graph quality is verified.

Acceptance criteria:

- `skills/` inclusion is compared against Tier 1 graph quality.
- `optional-skills/` remains separate unless the purpose is skill discovery.
- Any broader corpus expansion is documented with before/after graph stats and query evidence.

## Project operating system hardening

Objective: Keep Hermes Agent resumable and agent-readable.

Acceptance criteria:

- Project root has `AGENTS.md`, `STATUS.md`, `ROADMAP.md`, `TODO.md`, and `README.md`.
- Project-local `.hermes/` folders exist for plans, reports, reviews, handoffs, and context.
- Meaningful work updates `STATUS.md` before final reporting.
- New or materially edited Markdown docs use PKM frontmatter.
