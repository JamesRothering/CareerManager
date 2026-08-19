# Overnight loop

Runs on this machine. Do not ask James for command approval. Do not merge PRs.

## Schedule

- Start at **22:00 America/Los_Angeles**, or immediately if James says he is done for the day.
- Keep taking the next **Ready** story until (a) none remain, (b) usage is limited, or (c) **06:30 America/Los_Angeles**.
- Hard stop at 06:30 so James can do ad-hoc work at 08:00.

## Progressive retry (do not wait for James)

If a command dies, the sandbox blocks something, or this session is interrupted: **re-arm a timer and retry**. Do not stop for a human resume.

- First retry: **3 minutes**
- Each later retry: double the previous delay (**6m, 12m, 24m**)
- Cap: **30 minutes**
- On each wake, continue the same Ready GitHub issue
- Stop retrying only at 06:30, when usage is limited, or when no Ready stories remain

## Each story

1. List GitHub issues labeled `ready` on JamesRothering/CareerManager. Pick the lowest issue number. `docs/BACKLOG.md` is the mirror.
2. Write tests. Prove they are red.
3. Implement. Prove they are green. Full suite must stay green.
4. Open one PR. Do not merge. Do not approve.
5. Leave the story Ready until James says it is green.

## Enabler epic TE-0

TE-0 is done (PR #1 merged, issues #3–#14 closed). Product work may proceed from issues labeled `ready`. Do not rebuild Job Index ingest (US-2.1 closed). Epic 6 stays blocked until James pastes a LinkedIn **profile** design — the fork-audit handoff is not that design.

## After 06:30

Stop. Do not re-arm until the next 22:00 start or James says he is done for the day.
