# CareerManager backlog

Overnight agents pick only GitHub issues labeled **`ready`**. Do not merge PRs.
James reviews PRs and is the only person who marks a story green.

**Board:** [Issues](https://github.com/JamesRothering/CareerManager/issues) with labels `ready` / `in-review` / `backlog` / `blocked`. Milestones: [TE-0 Stand-up](https://github.com/JamesRothering/CareerManager/milestone/1) (done), [Product backlog](https://github.com/JamesRothering/CareerManager/milestone/2).

**Judge:** Given/When/Then on the story, shown red then green, full suite still green, PR open.

**Default branch:** `master`.

## Done — Enabler epic TE-0

Stood up the inherited AutoApply fork. Merged in [PR #1](https://github.com/JamesRothering/CareerManager/pull/1). Issues #3–#14 closed.

Handoff: [docs/handoffs/fork-audit.md](handoffs/fork-audit.md). Report: [docs/handoffs/te-0-standup.md](handoffs/te-0-standup.md).

Already in the product (extend, do not rebuild): job discovery / Job Index ingest, fit scoring, applicant memory / story bank / work_experiences / bullet pool, materials workspace, document library, automation plans, human-gated review, tracking, Vue+FastAPI console, Postgres+pgvector, Redis/Celery.

## Backlog — not Ready unless labeled `ready`

### Epic 1 — Experience corpus (extend story bank / work_experiences / bullet pool; no parallel DB)

STAR (`story_bank`) stays — interview anecdotes. Client vs vendor on `work_experiences` is not a design issue; treat `company` as the employer name.

- **US-1.1** [#15](https://github.com/JamesRothering/CareerManager/issues/15) Record an experience (role, dates, problem, actions, outcomes, skills). M. Missing dates rejected.
- **US-1.2** [#16](https://github.com/JamesRothering/CareerManager/issues/16) Tag skills and domains. S.
- **US-1.3** [#17](https://github.com/JamesRothering/CareerManager/issues/17) List/retrieve newest-first. S.
- **US-1.4** [#18](https://github.com/JamesRothering/CareerManager/issues/18) Idempotent import from Codojo about/work/stories (TransUnion, Hulu, McDonald's, BNP Paribas, Codojo Inc.). M.

### Epic 2 — Job matching (build on Job Index / fit scoring)

Job ingest is **already shipped** in the fork (search, ATS, LinkedIn jobs, manual apply-target). Former US-2.1 closed as duplicate of that capability ([#19](https://github.com/JamesRothering/CareerManager/issues/19)).

- **US-2.2** [#20](https://github.com/JamesRothering/CareerManager/issues/20) Rank experiences against a job. L.
- **US-2.3** [#21](https://github.com/JamesRothering/CareerManager/issues/21) Explain match with tags + evidence spans. M.
- **US-2.4** [#22](https://github.com/JamesRothering/CareerManager/issues/22) Pursue / Skip / Later. S.

### Epic 3 — Communications

- **US-3.1** [#23](https://github.com/JamesRothering/CareerManager/issues/23) Log a communication against a job. M.
- **US-3.2** [#24](https://github.com/JamesRothering/CareerManager/issues/24) Pipeline stages with valid transitions. M.
- **US-3.3** [#25](https://github.com/JamesRothering/CareerManager/issues/25) Overdue follow-ups in America/Los_Angeles. S.
- **US-3.4** [#38](https://github.com/JamesRothering/CareerManager/issues/38) Right to Represent (recruiter, exclusive or not, which roles). M.
- **US-3.5** [#39](https://github.com/JamesRothering/CareerManager/issues/39) Email discussion trail on that recruiter/RTR. M.
- **US-3.6** [#40](https://github.com/JamesRothering/CareerManager/issues/40) Submission rate: how often they submitted me, to whom, outcome. M.

### Epic 4 — Accomplishments and status reports

- **US-4.1** [#26](https://github.com/JamesRothering/CareerManager/issues/26) Log an accomplishment. M.
- **US-4.2** [#27](https://github.com/JamesRothering/CareerManager/issues/27) Weekly status report from in-range accomplishments. L.
- **US-4.3** [#28](https://github.com/JamesRothering/CareerManager/issues/28) Accomplishments belong to an engagement. S.

### Epic 5 — Condensed narratives

- **US-5.1** [#29](https://github.com/JamesRothering/CareerManager/issues/29) Highlight for a target job; no invented metrics. L.
- **US-5.2** [#30](https://github.com/JamesRothering/CareerManager/issues/30) Engagement close-out pack. M.

### Epic 6 — LinkedIn (still blocked)

The file `cursor-handoff-autoapply-setup.md` is the **TE-0 fork-audit** brief (copied to `docs/handoffs/fork-audit.md`). It does **not** specify LinkedIn profile fields, mapping, or a no-auto-post draft UX. Epic 6 stays blocked until James pastes that design.

- **US-6.1** [#31](https://github.com/JamesRothering/CareerManager/issues/31) Map profile fields to corpus. M.
- **US-6.2** [#32](https://github.com/JamesRothering/CareerManager/issues/32) Diff profile vs corpus. L.
- **US-6.3** [#33](https://github.com/JamesRothering/CareerManager/issues/33) Draft update, no auto-post. M.

### Epic 7 — Lifecycle

- **US-7.1** [#34](https://github.com/JamesRothering/CareerManager/issues/34) Search vs on-contract mode. S.

### TE-1 — LinkedIn official export into Postgres (no scrape)

Extend the existing database. Input is LinkedIn **Get a copy of your data** CSVs (Connections + Followers), not Playwright.

- **TE-1.1** Schema: connections and followers tables (idempotent key: LinkedIn member URL or email). M.
- **TE-1.2** Import the two CSVs; merge, don’t duplicate. M.

### Epic 8 — Network hygiene (depends on TE-1)

- **US-8.1** Rank connections/followers for prune-vs-keep with a written reason. L. Standard Connections.csv has no interaction counts — Messages export or weak proxies.

### Epic 10 — Interview demand and SRS (JD text already on `job_snapshots`)

- **US-10.1** From JDs of submitted applications, extract topics and a demand score 0–10 (10 = hottest across that set). L.
- **US-10.2** SRS quiz on those topics. L.
- **US-10.3** Topic list: display order, user can change priority. M.
- **US-10.4** Mock interview from the same topic bank. L.

## Working agreements

- TDD: fail first, then green.
- One Ready story → one PR. Do not merge.
- Immutable Alembic migrations.
- Overnight hard stop **06:30 America/Los_Angeles** so 08:00 ad-hoc work still has quota.
- If James says "done for the day", start immediately instead of waiting for 22:00.
