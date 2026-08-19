"""Import official LinkedIn data-export CSVs into ``linkedin_network``.

This is not the Playwright job scraper. Input is Settings → Get a copy of
your data (Connections.csv, Followers.csv). LinkedIn often prepends a Notes
block; we scan until the real header row.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from src.core.models import LinkedInNetwork, TENANT_DEFAULT

logger = logging.getLogger("autoapply.memory.linkedin_network")

_CONNECTION_DATE_FORMATS = ("%d %b %Y", "%b %d, %Y", "%Y-%m-%d")


@dataclass(frozen=True)
class ImportReport:
    inserted: int = 0
    updated: int = 0
    invalid: int = 0


def identity_key(profile_url: str | None, email: str | None) -> str:
    """Stable upsert key: normalized profile URL, else lowercased email."""
    url = _normalize_url(profile_url)
    if url:
        return url
    mail = (email or "").strip().lower()
    if mail:
        return mail
    raise ValueError("LinkedIn row needs a profile URL or an email")


def import_linkedin_csv(
    session: Session,
    path: Path,
    *,
    kind: str,
    tenant_id: str = TENANT_DEFAULT,
    commit: bool = True,
) -> ImportReport:
    if kind not in ("connection", "follower"):
        raise ValueError(f"kind must be connection or follower, got {kind!r}")
    path = Path(path)
    rows = _read_export_rows(path)
    report = upsert_network_payloads(session, rows, kind=kind, tenant_id=tenant_id)
    if commit:
        session.commit()
    logger.info(
        "Imported %s from %s: inserted=%d updated=%d invalid=%d",
        kind,
        path,
        report.inserted,
        report.updated,
        report.invalid,
    )
    return report


def upsert_network_payloads(
    session: Session,
    rows: list[dict[str, str]],
    *,
    kind: str,
    tenant_id: str = TENANT_DEFAULT,
) -> ImportReport:
    inserted = updated = invalid = 0
    for raw in rows:
        try:
            payload = _row_to_fields(raw, kind=kind)
            key = identity_key(payload.get("profile_url"), payload.get("email"))
        except ValueError:
            invalid += 1
            continue
        existing = (
            session.query(LinkedInNetwork)
            .filter_by(tenant_id=tenant_id, kind=kind, identity_key=key)
            .one_or_none()
        )
        if existing is None:
            session.add(
                LinkedInNetwork(
                    tenant_id=tenant_id,
                    kind=kind,
                    identity_key=key,
                    **payload,
                )
            )
            inserted += 1
        else:
            for field, value in payload.items():
                setattr(existing, field, value)
            updated += 1
    return ImportReport(inserted=inserted, updated=updated, invalid=invalid)


def _read_export_rows(path: Path) -> list[dict[str, str]]:
    text = path.read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    header_idx = 0
    for i, line in enumerate(lines):
        lower = line.lower()
        if "first name" in lower and ("url" in lower or "profile" in lower):
            header_idx = i
            break
    parsed = csv.DictReader(lines[header_idx:])
    if parsed.fieldnames is None:
        return []
    rows: list[dict[str, str]] = []
    for raw in parsed:
        rows.append({(k or "").strip(): (v or "").strip() for k, v in raw.items()})
    return rows


def _row_to_fields(raw: dict[str, str], *, kind: str) -> dict[str, Any]:
    lookup = {k.lower(): v for k, v in raw.items()}
    profile_url = _first(lookup, "url", "profile url", "profileurl")
    email = _first(lookup, "email address", "email")
    return {
        "profile_url": _normalize_url(profile_url) or None,
        "email": (email.strip().lower() or None) if email else None,
        "first_name": _first(lookup, "first name") or None,
        "last_name": _first(lookup, "last name") or None,
        "company": _first(lookup, "company") or None,
        "position": _first(lookup, "position") or None,
        "headline": _first(lookup, "headline") or None,
        "connected_on": _parse_connected_on(_first(lookup, "connected on")) if kind == "connection" else None,
        "raw": raw,
    }


def _first(lookup: dict[str, str], *names: str) -> str:
    for name in names:
        value = lookup.get(name)
        if value:
            return value
    return ""


def _normalize_url(value: str | None) -> str:
    url = (value or "").strip().rstrip("/").lower()
    if url.startswith("http://"):
        url = "https://" + url[len("http://") :]
    return url


def _parse_connected_on(value: str) -> date | None:
    if not value:
        return None
    for fmt in _CONNECTION_DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None
