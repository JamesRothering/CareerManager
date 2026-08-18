# Cursor Task: Verify, Audit, and Prepare Forked AutoApply Repo

## Context

I've forked `Liam-Frost/AutoApply` (archived upstream, PolyForm Noncommercial license,
personal/non-commercial use) into my own repo. It's a local-first job application
automation workspace: Vue/FastAPI console, PostgreSQL, Redis/Celery, Playwright,
provider-agnostic LLM layer (OpenAI/Anthropic/Gemini/etc.), human-gated submission.

I'm going to extend this into my own tool and diverge from upstream over time.
Before I write any new code, I need the repo verified, audited, and instrumented.
Do not add product features. This is setup and due-diligence work only.

## Working conventions (follow these throughout)

- Immutable migrations: never edit an existing Alembic migration file, only add new ones.
- Red-first TDD where you touch anything: if you fix or add code, write/adjust the
  failing test first, show it fail, then make it pass.
- Don't silently "clean up" or refactor code outside of a task below.
- Every non-trivial decision you make gets logged (see Task 5) — not just made.
- If something is ambiguous or you find a real problem, stop and report it in the
  summary rather than guessing and proceeding.

## Tasks

### 1. Environment setup and verification
- Follow the README quick start exactly: `uv sync`, `uv run playwright install chromium`,
  set `AUTOAPPLY_DB_PASSWORD` in `.env`, `uv run autoapply init`.
- Run `uv run autoapply start --check` first to confirm the startup plan resolves
  without starting anything.
- Then bring up the full stack (Docker Compose for Postgres/Redis, Alembic migrations,
  Celery worker + Beat, web app) and confirm it starts cleanly end to end.
- Report any deviation from what the README claims (missing env vars, broken defaults,
  port conflicts, migration errors, etc.).

### 2. Full test suite run
- Run the complete test suite (unit + any integration/eval-style agent tests).
- Report: pass/fail count, any skipped tests and why, and full output for any failures.
- Do NOT modify tests or source to force a pass — report failures as-is. I want the
  real state of the inherited codebase, not a green checkmark you engineered.
- Note test runtime and flag any tests that look flaky (pass/fail inconsistently
  across 2-3 runs) rather than deterministically broken.

### 3. Branch audit
- List every branch in the repo (we forked all branches, not just `main`).
- For each non-main branch, diff it against `main`/`master` and summarize:
  - Is it merged, partially merged, or fully divergent?
  - What does it contain — finished features, WIP, dead experiments?
  - Anything that looks safe and valuable to cherry-pick into `main`/`master` now?
- Do not merge or cherry-pick anything yet — just report findings with your
  recommendation per branch (cherry-pick / leave / delete-eventually).

### 4. Architecture decision log review
- Read `docs/DECISIONS.md` in full.
- Produce a short summary (bullet list, one line per decision) of what's been
  decided and why, especially anything that constrains how new features should
  be built (e.g. the human-gated submission requirement, the rationale in D001
  for not forking AIHawk-derived projects).
- Flag anything in the current code that appears to contradict a documented decision.

### 5. Start my own decision log entries
- Find the last decision number used in `docs/DECISIONS.md` (e.g. D0xx) and
  continue numbering from there — don't renumber or touch existing entries.
- Add a new entry documenting the fork itself: why I forked (archived upstream,
  extending for personal job search / career management, chose this over AIHawk-derived alternatives),
  dated today.
- Set up the log so it's easy for me to keep appending to going forward — confirm
  the format/template being used and note it in your summary.

### 6. Add CI
- There is currently no `.github/workflows` — nothing runs the test suite
  automatically. Add a GitHub Actions workflow that:
  - Runs on push and pull_request against `master` (this fork's default branch).
  - Installs deps via `uv`, spins up Postgres/Redis (services or containers)
    as needed for the test suite to run for real, not just unit tests in isolation.
  - Runs the full test suite from Task 2.
  - Fails the build on any test failure.
- Keep it as simple as possible first — optimize/cache later, correctness now.
- Write a red-first test for the workflow itself if practical (e.g. confirm it
  fails on a deliberately broken test before confirming it passes on the real suite),
  otherwise just verify it directly on GitHub after pushing.

## Deliverable

At the end, give a single written summary covering:
1. Environment setup: worked as documented? deviations?
2. Test suite: pass/fail state, notable failures, flaky tests.
3. Branch audit table (branch → status → recommendation).
4. Decision log summary + any contradictions found.
5. Confirmation the new decision log entry was added, with its number.
6. Confirmation CI is live and passing (or why not).

Don't start building any new product features until James has marked Sprint 0 green.
