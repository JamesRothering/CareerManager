"""US-8.2: list ranked LinkedIn prune suggestions and persist decisions."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy.orm import Session

from src.core.models import TENANT_DEFAULT, LinkedInNetwork, LinkedInNetworkDecision
from src.memory.linkedin_rank import PruneCandidate, rank_for_prune

VALID_DECISIONS = frozenset({"keep", "kill", "later"})
VALID_FILTERS = frozenset({"suggested", "keep", "kill", "later", "all"})
EMPTY_HINT = "Import the official LinkedIn archive first."


class NetworkDecisionError(ValueError):
    """Keep / kill / later (or a list filter) was not a known value."""


class UnknownNetworkPersonError(LookupError):
    """Decision targeted an identity that is not in linkedin_network."""


def list_prune_suggestions(
    session: Session,
    *,
    tenant_id: str = TENANT_DEFAULT,
    decision: str | None = None,
    as_of: date | None = None,
) -> dict[str, Any]:
    filter_key = (decision or "all").strip().lower() or "all"
    if filter_key not in VALID_FILTERS:
        raise NetworkDecisionError(filter_key)

    ranked = rank_for_prune(session, tenant_id=tenant_id, as_of=as_of)
    stored = {
        (row.kind, row.identity_key): row
        for row in session.query(LinkedInNetworkDecision).filter_by(tenant_id=tenant_id).all()
    }

    counts = {"suggested": 0, "keep": 0, "kill": 0, "later": 0, "all": 0}
    items: list[dict[str, Any]] = []
    for candidate in ranked:
        record = stored.get((candidate.kind, candidate.identity_key))
        current = record.decision if record is not None else None
        bucket = current or "suggested"
        counts[bucket] += 1
        counts["all"] += 1
        if filter_key == "all" or bucket == filter_key:
            items.append(_serialize_suggestion(candidate, record))

    empty = counts["all"] == 0
    return {
        "items": items,
        "empty": empty,
        "hint": EMPTY_HINT if empty else "",
        "counts": counts,
        "decision": filter_key,
    }


def set_network_decision(
    session: Session,
    *,
    identity_key: str,
    kind: str,
    decision: str,
    tenant_id: str = TENANT_DEFAULT,
    commit: bool = True,
) -> dict[str, Any]:
    value = (decision or "").strip().lower()
    if value not in VALID_DECISIONS:
        raise NetworkDecisionError(decision)
    kind_value = (kind or "").strip().lower()
    if kind_value not in ("connection", "follower"):
        raise NetworkDecisionError(kind)
    key = (identity_key or "").strip()
    if not key:
        raise NetworkDecisionError("identity_key")

    person = (
        session.query(LinkedInNetwork)
        .filter_by(tenant_id=tenant_id, identity_key=key, kind=kind_value)
        .one_or_none()
    )
    if person is None:
        raise UnknownNetworkPersonError(key)

    now = datetime.now(UTC)
    row = (
        session.query(LinkedInNetworkDecision)
        .filter_by(tenant_id=tenant_id, identity_key=key, kind=kind_value)
        .one_or_none()
    )
    if row is None:
        row = LinkedInNetworkDecision(
            tenant_id=tenant_id,
            identity_key=key,
            kind=kind_value,
            decision=value,
            decided_at=now,
        )
        session.add(row)
    else:
        row.decision = value
        row.decided_at = now

    if commit:
        session.commit()
        session.refresh(row)

    return {
        "identity_key": row.identity_key,
        "kind": row.kind,
        "decision": row.decision,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
    }


def _serialize_suggestion(
    candidate: PruneCandidate,
    record: LinkedInNetworkDecision | None,
) -> dict[str, Any]:
    return {
        "identity_key": candidate.identity_key,
        "kind": candidate.kind,
        "first_name": candidate.first_name,
        "last_name": candidate.last_name,
        "company": candidate.company,
        "position": candidate.position,
        "headline": candidate.headline,
        "profile_url": candidate.profile_url,
        "prune_score": candidate.prune_score,
        "reasons": list(candidate.reasons),
        "decision": record.decision if record is not None else None,
        "decided_at": record.decided_at.isoformat() if record is not None else None,
    }
