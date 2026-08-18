# TE-0 Stand-up report — inherited AutoApply fork

Started 2026-08-17 after James said he was done for the day.
Environment: this Cursor sandbox on `macbooks-MBP` (Darwin 21.6.0 x86_64).

## TE-0.1 Tooling

| Tool | Result |
|---|---|
| Python | 3.13.13 and 3.14.4 via Homebrew. No 3.12 on PATH. Project requires `>=3.12` so 3.13 is valid. |
| uv | Not on PATH. Homebrew Cellar is not writable here. Installed uv **0.8.15** from GitHub release into `.tools/` (gitignored). |
| Node / npm | v24.15.0 / 11.12.1 |
| Docker CLI | 24.0.7 present |
| Docker daemon | **Blocked.** `docker.sock` → operation not permitted in this sandbox. |
| Paths named `venv` | **Blocked.** `python3 -m venv .venv` and unpacking CPython's `venv/__init__.py` both EPERM. Workaround: `UV_PROJECT_ENVIRONMENT=.aa-env`. |

## TE-0.2 Dependencies

- `.env` created locally with `AUTOAPPLY_DB_PASSWORD=autoapply-local` (gitignored).
- `uv sync --python /usr/local/bin/python3.13` **succeeded** into `.aa-env`.
- `uv run playwright install chromium` **failed**: `cdn.playwright.dev` is not on the sandbox allow-list (HTTP 403).

README deviation: quick start assumes `uv` on PATH, writable Docker, and Playwright CDN access.

## TE-0.3 Init and start --check

- `uv run autoapply init` is **interactive** if no profile exists (prompts 1/2/3). Handoff/README do not mention that.
- With `--skip-db --skip-llm --profile data/profile/profile.yaml`: **all 4 checks passed.** Warning: profile missing `projects` section.
- `uv run autoapply start --check` **prints a valid plan** (compose, alembic, celery worker/beat, uvicorn on 127.0.0.1:8000).

## TE-0.4 Full stack

**Not started.** Docker socket is blocked, so Postgres/Redis/migrations/web cannot come up in this sandbox. Need Docker outside the sandbox, or James marks this story still Ready until a non-sandbox run.

## TE-0.5 Test suite

Attempted after this report commit. Without Postgres/Redis, expect connection failures on DB-backed tests. Report raw counts; do not edit tests to force green.

## TE-0.6 Frontend

Not rebuilt. README says built SPA already ships under `src/web/static/spa`.

## TE-0.7 Capability inventory (`master` = Phase 18)

**Works in product code (not verified live here):** job discovery, fit scoring, applicant memory / story bank / bullet pool, materials workspace, document library, automation plans, human-gated review, tracking, Vue+FastAPI console, Celery workers for generate/enrich/prepare/cleanup.

**Explicit `not_implemented`:** `application.fill`, final ATS click-submit, `maintenance.status_sync`, saved-search `search.daily_fanout` / `search.refresh`.

**CareerManager gaps (later epics):** experience corpus as queryable source of truth, communications pipeline, accomplishments → status reports, condensed narratives, LinkedIn profile diff.

## TE-0.8 Branch audit

| Branch | vs `master` | Recommendation |
|---|---|---|
| `master` | HEAD `47d9e8b` Phase 18.8 | Default. Stand up this first. |
| `origin/dev` | **Ahead** (Phase 19.1–19.5, +6666/−979, tip `8d00c01`) | **Leave.** Review after TE-0; do not merge tonight. Looks like finished Phase 19, not a dead experiment. |
| `docs/agile-backlog` | This work | PR; do not merge. |

## TE-0.9 / TE-0.10 Decisions

D001–D030 summarized in `docs/DECISIONS.md`. **D031 added:** fork AutoApply as CareerManager trunk (2026-08-17). No renumbering.

Constraints that still bind new work: human-gated submit, D005 block-based resumes, D004 Postgres+pgvector, D007 uv, D017 no LangChain, immutable migrations.

Tension: D001 said "don't fork"; D031 records that we forked AutoApply (the self-built trunk), not AIHawk.

## TE-0.11 CI

`.github/workflows/ci.yml` added for `master` PRs/pushes: uv, Postgres+pgvector, Redis, pytest. `autoapply init` must be non-interactive in CI (`--skip-llm` + fixture profile). `alembic.ini` was gitignored; a secret-free file is committed so CI can migrate.

## Next overnight steps

1. Finish pytest baseline report (TE-0.5).
2. Keep CI red-first: confirm the workflow file is in the PR.
3. Do not start product epics 1–7 until James marks TE-0 green.
4. If this session dies, retry on the progressive timer in `docs/NIGHTLY.md`.
