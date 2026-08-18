# Overnight loop

Runs on this machine. Do not ask James for command approval. Do not merge PRs.

## Schedule

- Start at **22:00 America/Los_Angeles**, or immediately if James says he is done for the day.
- Keep taking the next **Ready** story until (a) none remain, (b) usage is limited, or (c) **06:30 America/Los_Angeles**.
- Hard stop at 06:30 so James can do ad-hoc work at 08:00.

## Each story

1. Read `docs/BACKLOG.md`. Pick the first Ready story.
2. Write tests. Prove they are red.
3. Implement. Prove they are green. Full suite must stay green.
4. Open one PR. Do not merge. Do not approve.
5. Leave the story Ready until James says it is green.

## Sprint 0

Until James marks Sprint 0 green, only S0.1–S0.6. Follow `docs/handoffs/fork-audit.md`. No product features.

## After 06:30

Stop. Do not re-arm until the next 22:00 start or James says he is done for the day.
