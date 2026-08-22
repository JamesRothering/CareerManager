"""Upsert a flattened LinkedIn archive (zip, folder, or LI_eater CSV) into Postgres.

Later chunks merge: new rows are inserted, changed rows are updated, and
files omitted from this chunk are left alone.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from src.core.models import (
    TENANT_DEFAULT,
    LinkedInExportFile,
    LinkedInExportRow,
    LinkedInExportRun,
)
from src.memory.linkedin_eater import EatenRow, eat_linkedin_sources, make_row_key
from src.memory.linkedin_network import upsert_network_payloads

logger = logging.getLogger("autoapply.memory.linkedin_archive")

_CONNECTION_FILES = {"connections.csv"}
_FOLLOWER_FILES = {"followers.csv"}
_COMMIT_EVERY = 200


@dataclass(frozen=True)
class ArchiveImportReport:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    file_count: int = 0
    invalid: int = 0
    already_imported: bool = False


def import_linkedin_archive(
    session: Session,
    path: Path,
    *,
    tenant_id: str = TENANT_DEFAULT,
) -> ArchiveImportReport:
    path = Path(path)
    if path.is_dir():
        source_kind = "directory"
    elif path.suffix.lower() == ".csv":
        source_kind = "csv"
    else:
        source_kind = "zip"
    digest = _source_digest(path)
    if digest:
        existing_run = (
            session.query(LinkedInExportRun)
            .filter_by(tenant_id=tenant_id, source_sha256=digest)
            .one_or_none()
        )
        if existing_run is not None:
            return ArchiveImportReport(
                already_imported=True,
                file_count=existing_run.file_count,
                unchanged=existing_run.row_count,
            )

    records = eat_linkedin_sources([path])
    run = LinkedInExportRun(
        tenant_id=tenant_id,
        source_name=path.name,
        source_kind=source_kind,
        source_sha256=digest,
        extra={"source_path": str(path)},
    )
    session.add(run)
    session.flush()

    inserted = updated = unchanged = 0
    files_touched: dict[str, LinkedInExportFile] = {}
    by_path: dict[str, list[EatenRow]] = {}
    for row in records:
        by_path.setdefault(row.path, []).append(row)

    for relative_path, rows in by_path.items():
        file_row = _upsert_file(session, run, tenant_id, relative_path, rows)
        files_touched[relative_path] = file_row
        for index, eaten in enumerate(rows, start=1):
            status = _upsert_row(session, run, file_row, tenant_id, eaten)
            if status == "inserted":
                inserted += 1
            elif status == "updated":
                updated += 1
            else:
                unchanged += 1
            if index % _COMMIT_EVERY == 0:
                session.commit()
        session.commit()

    network_invalid = _project_network(session, tenant_id, records)
    run.file_count = len(files_touched)
    run.row_count = inserted + updated + unchanged
    session.commit()
    logger.info(
        "Archive %s: inserted=%d updated=%d unchanged=%d files=%d",
        path,
        inserted,
        updated,
        unchanged,
        len(files_touched),
    )
    return ArchiveImportReport(
        inserted=inserted,
        updated=updated,
        unchanged=unchanged,
        file_count=len(files_touched),
        invalid=network_invalid,
        already_imported=existing_run is not None and inserted == 0 and updated == 0,
    )


def _upsert_file(
    session: Session,
    run: LinkedInExportRun,
    tenant_id: str,
    relative_path: str,
    rows: list[EatenRow],
) -> LinkedInExportFile:
    kind = rows[0].kind if rows else "other"
    header = sorted({key for row in rows for key in row.payload})
    hashes = sorted(_payload_hash(row.payload) for row in rows)
    digest = hashlib.sha256("".join(hashes).encode()).hexdigest()
    existing = (
        session.query(LinkedInExportFile)
        .filter_by(tenant_id=tenant_id, relative_path=relative_path)
        .one_or_none()
    )
    if existing is None:
        existing = LinkedInExportFile(
            run_id=run.id,
            tenant_id=tenant_id,
            relative_path=relative_path,
            media_kind=kind,
            sha256=digest,
            header=header,
            row_count=len(rows),
            byte_size=None,
        )
        session.add(existing)
        session.flush()
        return existing
    existing.run_id = run.id
    existing.media_kind = kind
    existing.sha256 = digest
    existing.header = header
    existing.row_count = len(rows)
    return existing


def _upsert_row(
    session: Session,
    run: LinkedInExportRun,
    file_row: LinkedInExportFile,
    tenant_id: str,
    eaten: EatenRow,
) -> str:
    digest = _payload_hash(eaten.payload)
    key = eaten.row_key or make_row_key(eaten.path, eaten.payload)
    existing = (
        session.query(LinkedInExportRow)
        .filter_by(tenant_id=tenant_id, relative_path=eaten.path, row_key=key)
        .one_or_none()
    )
    if existing is None:
        session.add(
            LinkedInExportRow(
                file_id=file_row.id,
                run_id=run.id,
                tenant_id=tenant_id,
                relative_path=eaten.path,
                row_key=key,
                row_index=eaten.row_index,
                content_hash=digest,
                payload=eaten.payload,
            )
        )
        return "inserted"
    if existing.content_hash == digest:
        existing.run_id = run.id
        return "unchanged"
    existing.payload = eaten.payload
    existing.content_hash = digest
    existing.row_index = eaten.row_index
    existing.file_id = file_row.id
    existing.run_id = run.id
    return "updated"


def _project_network(session: Session, tenant_id: str, records: list[EatenRow]) -> int:
    invalid = 0
    connections = [
        {str(k): "" if v is None else str(v) for k, v in row.payload.items()}
        for row in records
        if Path(row.path).name.lower() in _CONNECTION_FILES and row.kind == "csv"
    ]
    followers = [
        {str(k): "" if v is None else str(v) for k, v in row.payload.items()}
        for row in records
        if Path(row.path).name.lower() in _FOLLOWER_FILES and row.kind == "csv"
    ]
    if connections:
        invalid += upsert_network_payloads(
            session, connections, kind="connection", tenant_id=tenant_id
        ).invalid
    if followers:
        invalid += upsert_network_payloads(
            session, followers, kind="follower", tenant_id=tenant_id
        ).invalid
    return invalid


def _payload_hash(payload: dict) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _source_digest(path: Path) -> str | None:
    if path.is_file():
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
        return hasher.hexdigest()
    if path.is_dir():
        hasher = hashlib.sha256()
        for file_path in sorted(p for p in path.rglob("*") if p.is_file()):
            hasher.update(file_path.relative_to(path).as_posix().encode())
            hasher.update(file_path.read_bytes())
        return hasher.hexdigest()
    return None
