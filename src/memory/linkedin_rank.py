"""Rank ``linkedin_network`` rows for prune-vs-keep.

When Messages rows exist in the archive, people with no thread score
higher (worse). Otherwise documented proxies only. Higher ``prune_score``
means a stronger candidate to remove.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy.orm import Session

from src.core.models import (
    TENANT_DEFAULT,
    LinkedInExportRow,
    LinkedInNetwork,
)
from src.memory.linkedin_network import identity_key

_OLD_YEARS = 8
_STALE_MESSAGE_YEARS = 3
_URL_FIELDS = (
    "sender profile url",
    "recipient profile urls",
    "profile url",
    "url",
)


@dataclass(frozen=True)
class PruneCandidate:
    identity_key: str
    kind: str
    first_name: str | None
    last_name: str | None
    company: str | None
    prune_score: int
    reasons: tuple[str, ...]


@dataclass
class _MsgStats:
    count: int = 0
    last: date | None = None


def rank_for_prune(
    session: Session,
    *,
    tenant_id: str = TENANT_DEFAULT,
    as_of: date | None = None,
) -> list[PruneCandidate]:
    as_of = as_of or date.today()
    messages = _message_stats(session, tenant_id)
    have_messages = bool(messages)
    rows = session.query(LinkedInNetwork).filter_by(tenant_id=tenant_id).all()
    ranked = [_score(row, as_of, messages, have_messages) for row in rows]
    ranked.sort(key=lambda item: (-item.prune_score, item.last_name or "", item.first_name or ""))
    return ranked


def _score(
    row: LinkedInNetwork,
    as_of: date,
    messages: dict[str, _MsgStats],
    have_messages: bool,
) -> PruneCandidate:
    reasons: list[str] = []
    score = 0
    stats = messages.get(row.identity_key)
    if have_messages:
        if stats is None or stats.count == 0:
            score += 8
            reasons.append("No messages in the archive")
        else:
            reasons.append(f"{stats.count} message(s) in the archive")
            if stats.last is not None:
                years = (as_of - stats.last).days / 365.25
                reasons.append(f"Last message {stats.last.isoformat()}")
                if years >= _STALE_MESSAGE_YEARS:
                    score += 3
                    reasons.append(f"Last message {years:.0f} years ago")
    else:
        reasons.append("No Messages file imported yet; scoring proxies only")
    if not (row.email or "").strip():
        score += 3
        reasons.append("No email on the export")
    if not (row.company or "").strip() and not (row.position or "").strip():
        score += 4
        reasons.append("No company or title")
    if row.kind == "follower" and not (row.headline or "").strip():
        score += 3
        reasons.append("Follower with empty headline")
    if row.kind == "connection" and row.connected_on is not None:
        years = (as_of - row.connected_on).days / 365.25
        if years >= _OLD_YEARS:
            score += 2
            reasons.append(f"Connected {years:.0f} years ago")
    return PruneCandidate(
        identity_key=row.identity_key,
        kind=row.kind,
        first_name=row.first_name,
        last_name=row.last_name,
        company=row.company,
        prune_score=score,
        reasons=tuple(reasons),
    )


def _message_stats(session: Session, tenant_id: str) -> dict[str, _MsgStats]:
    rows = (
        session.query(LinkedInExportRow)
        .filter(
            LinkedInExportRow.tenant_id == tenant_id,
            LinkedInExportRow.relative_path.ilike("%messages.csv"),
        )
        .all()
    )
    stats: dict[str, _MsgStats] = {}
    for row in rows:
        payload = row.payload or {}
        lookup = {
            str(k).strip().lower(): "" if v is None else str(v).strip()
            for k, v in payload.items()
        }
        when = _parse_message_date(lookup.get("date", ""))
        for raw in _urls_from_payload(lookup):
            try:
                key = identity_key(raw, None)
            except ValueError:
                continue
            bucket = stats.setdefault(key, _MsgStats())
            bucket.count += 1
            if when is not None and (bucket.last is None or when > bucket.last):
                bucket.last = when
    return stats


def _urls_from_payload(lookup: dict[str, str]) -> list[str]:
    found: list[str] = []
    for field in _URL_FIELDS:
        value = lookup.get(field, "")
        if not value:
            continue
        for part in value.replace(";", ",").split(","):
            part = part.strip()
            if "linkedin.com/in/" in part.lower():
                found.append(part)
    return found


def _parse_message_date(value: str) -> date | None:
    text = value.strip()
    if not text:
        return None
    for fmt in (
        "%Y-%m-%d %H:%M:%S UTC",
        "%Y-%m-%d %H:%M UTC",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None
