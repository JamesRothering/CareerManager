# CareerManager backlog

Overnight agents pick only **Ready** stories. Do not merge PRs.
James reviews PRs and is the only person who marks a story green.

**Judge:** Given/When/Then on the story, shown red then green, full suite still green, PR open.

**Default branch:** `master`.

## Ready — Sprint 0 (fork audit)

No product features until James marks Sprint 0 green.
Handoff: [docs/handoffs/fork-audit.md](handoffs/fork-audit.md).

| ID | Story | Size |
|---|---|---|
| S0.1 | Environment setup and verification (README quick start; report deviations) | M |
| S0.2 | Full test suite run — report only; do not change tests/source to force a pass | M |
| S0.3 | Branch audit (`master`, `dev`); recommend cherry-pick / leave / delete; do not merge | S |
| S0.4 | Review `docs/DECISIONS.md`; flag contradictions | M |
| S0.5 | Add fork decision **D031** (do not renumber existing entries) | S |
| S0.6 | GitHub Actions CI on `master` + PRs: uv, Postgres, Redis, full suite | M |

## Backlog — not Ready

### Epic 1 — Experience corpus (extend story bank / bullet pool; no parallel DB)

- **US-1.1** Record an experience (client, role, dates, problem, actions, outcomes, skills). M. Missing client/dates rejected.
- **US-1.2** Tag skills and domains. S.
- **US-1.3** List/retrieve newest-first. S.
- **US-1.4** Idempotent import from Codojo about/work/stories (TransUnion, Hulu, McDonald's, BNP Paribas, Codojo Inc.). M.

### Epic 2 — Job matching (build on Job Index / fit scoring)

- **US-2.1** Ingest a job posting. M.
- **US-2.2** Rank experiences against a job. L.
- **US-2.3** Explain match with tags + evidence spans. M.
- **US-2.4** Pursue / Skip / Later. S.

### Epic 3 — Communications

- **US-3.1** Log a communication against a job. M.
- **US-3.2** Pipeline stages with valid transitions. M.
- **US-3.3** Overdue follow-ups in America/Los_Angeles. S.

### Epic 4 — Accomplishments and status reports

- **US-4.1** Log an accomplishment. M.
- **US-4.2** Weekly status report from in-range accomplishments. L.
- **US-4.3** Accomplishments belong to an engagement. S.

### Epic 5 — Condensed narratives

- **US-5.1** Highlight for a target job; no invented metrics. L.
- **US-5.2** Engagement close-out pack. M.

### Epic 6 — LinkedIn (blocked until James pastes the design)

- **US-6.1** Map profile fields to corpus. M.
- **US-6.2** Diff profile vs corpus. L.
- **US-6.3** Draft update, no auto-post. M.

### Epic 7 — Lifecycle

- **US-7.1** Search vs on-contract mode. S.

## Working agreements

- TDD: fail first, then green.
- One Ready story → one PR. Do not merge.
- Immutable Alembic migrations.
- Overnight hard stop **06:30 America/Los_Angeles** so 08:00 ad-hoc work still has quota.
- If James says "done for the day", start immediately instead of waiting for 22:00.
