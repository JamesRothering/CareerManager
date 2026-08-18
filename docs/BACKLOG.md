# CareerManager backlog

Overnight agents pick only GitHub issues labeled **`ready`**. Do not merge PRs.
James reviews PRs and is the only person who marks a story green.

**Board:** [Issues](https://github.com/JamesRothering/CareerManager/issues) with labels `ready` / `in-review` / `backlog` / `blocked`. Milestones: [TE-0 Stand-up](https://github.com/JamesRothering/CareerManager/milestone/1), [Product backlog](https://github.com/JamesRothering/CareerManager/milestone/2).

**Judge:** Given/When/Then on the story, shown red then green, full suite still green, PR open.

**Default branch:** `master`.

## Ready — Enabler epic TE-0: Stand up the inherited AutoApply fork

This is the technical/enabler epic: **make the forked product actually run on this machine**, then record what already exists so CareerManager is built on a living system, not a dead checkout. No new career-management product features until James marks TE-0 green.

Handoff: [docs/handoffs/fork-audit.md](handoffs/fork-audit.md).

What already exists (must be brought up, not rewritten): job discovery, fit scoring, applicant memory / story bank, materials workspace, document library, automation plans, human-gated review/submit, tracking, Vue+FastAPI console, Postgres+pgvector, Redis/Celery.

| ID | Issue | Story | Size | Status |
|---|---|---|---|---|
| TE-0.1 | [#3](https://github.com/JamesRothering/CareerManager/issues/3) | Tooling check: Python 3.12+, uv, Docker, Node. Report gaps. | S | in-review (PR #1) |
| TE-0.2 | [#4](https://github.com/JamesRothering/CareerManager/issues/4) | `uv sync`, Playwright Chromium, `.env` with `AUTOAPPLY_DB_PASSWORD` | M | in-review (PR #1) |
| TE-0.3 | [#5](https://github.com/JamesRothering/CareerManager/issues/5) | `uv run autoapply init` then `uv run autoapply start --check` | M | in-review (PR #1) |
| TE-0.4 | [#6](https://github.com/JamesRothering/CareerManager/issues/6) | Full local stack: Postgres + Redis + migrations + web console | L | in-review (PR #1) |
| TE-0.5 | [#7](https://github.com/JamesRothering/CareerManager/issues/7) | Full test suite baseline — report only; do not change tests/source to force a pass | M | in-review (PR #1) |
| TE-0.6 | [#8](https://github.com/JamesRothering/CareerManager/issues/8) | `frontend` `npm run build` if needed; note SPA already shipped under `src/web/static/spa` | S | in-review (PR #1) |
| TE-0.7 | [#9](https://github.com/JamesRothering/CareerManager/issues/9) | Capability inventory: what works vs explicit `not_implemented` vs CareerManager gaps | M | in-review (PR #1) |
| TE-0.8 | [#10](https://github.com/JamesRothering/CareerManager/issues/10) | Branch audit (`master`, `dev`); recommend cherry-pick / leave / delete; do not merge | S | in-review (PR #1) |
| TE-0.9 | [#11](https://github.com/JamesRothering/CareerManager/issues/11) | Review `docs/DECISIONS.md`; flag contradictions | M | in-review (PR #1) |
| TE-0.10 | [#12](https://github.com/JamesRothering/CareerManager/issues/12) | Add fork decision **D031** (do not renumber existing entries) | S | in-review (PR #1) |
| TE-0.11 | [#13](https://github.com/JamesRothering/CareerManager/issues/13) | GitHub Actions CI on `master` + PRs: uv, Postgres, Redis, full suite | M | in-review (PR #1; CI red) |
| TE-0.12 | [#14](https://github.com/JamesRothering/CareerManager/issues/14) | Make CI green: inherited packaging failures (`template.docx` gitignore + Windows venv path) | M | **Ready** |

## Backlog — not Ready

### Epic 1 — Experience corpus (extend story bank / bullet pool; no parallel DB)

- **US-1.1** [#15](https://github.com/JamesRothering/CareerManager/issues/15) Record an experience (client, role, dates, problem, actions, outcomes, skills). M. Missing client/dates rejected.
- **US-1.2** [#16](https://github.com/JamesRothering/CareerManager/issues/16) Tag skills and domains. S.
- **US-1.3** [#17](https://github.com/JamesRothering/CareerManager/issues/17) List/retrieve newest-first. S.
- **US-1.4** [#18](https://github.com/JamesRothering/CareerManager/issues/18) Idempotent import from Codojo about/work/stories (TransUnion, Hulu, McDonald's, BNP Paribas, Codojo Inc.). M.

### Epic 2 — Job matching (build on Job Index / fit scoring)

- **US-2.1** [#19](https://github.com/JamesRothering/CareerManager/issues/19) Ingest a job posting. M.
- **US-2.2** [#20](https://github.com/JamesRothering/CareerManager/issues/20) Rank experiences against a job. L.
- **US-2.3** [#21](https://github.com/JamesRothering/CareerManager/issues/21) Explain match with tags + evidence spans. M.
- **US-2.4** [#22](https://github.com/JamesRothering/CareerManager/issues/22) Pursue / Skip / Later. S.

### Epic 3 — Communications

- **US-3.1** [#23](https://github.com/JamesRothering/CareerManager/issues/23) Log a communication against a job. M.
- **US-3.2** [#24](https://github.com/JamesRothering/CareerManager/issues/24) Pipeline stages with valid transitions. M.
- **US-3.3** [#25](https://github.com/JamesRothering/CareerManager/issues/25) Overdue follow-ups in America/Los_Angeles. S.

### Epic 4 — Accomplishments and status reports

- **US-4.1** [#26](https://github.com/JamesRothering/CareerManager/issues/26) Log an accomplishment. M.
- **US-4.2** [#27](https://github.com/JamesRothering/CareerManager/issues/27) Weekly status report from in-range accomplishments. L.
- **US-4.3** [#28](https://github.com/JamesRothering/CareerManager/issues/28) Accomplishments belong to an engagement. S.

### Epic 5 — Condensed narratives

- **US-5.1** [#29](https://github.com/JamesRothering/CareerManager/issues/29) Highlight for a target job; no invented metrics. L.
- **US-5.2** [#30](https://github.com/JamesRothering/CareerManager/issues/30) Engagement close-out pack. M.

### Epic 6 — LinkedIn (blocked until James pastes the design)

- **US-6.1** [#31](https://github.com/JamesRothering/CareerManager/issues/31) Map profile fields to corpus. M.
- **US-6.2** [#32](https://github.com/JamesRothering/CareerManager/issues/32) Diff profile vs corpus. L.
- **US-6.3** [#33](https://github.com/JamesRothering/CareerManager/issues/33) Draft update, no auto-post. M.

### Epic 7 — Lifecycle

- **US-7.1** [#34](https://github.com/JamesRothering/CareerManager/issues/34) Search vs on-contract mode. S.

## Working agreements

- TDD: fail first, then green.
- One Ready story → one PR. Do not merge.
- Immutable Alembic migrations.
- Overnight hard stop **06:30 America/Los_Angeles** so 08:00 ad-hoc work still has quota.
- If James says "done for the day", start immediately instead of waiting for 22:00.
