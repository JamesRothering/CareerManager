"""Flatten LinkedIn's mixed zip/folder export into one ingest-ready table.

LinkedIn ships Complete/Basic archives as a zip of CSVs nested in folders,
plus JSON/TXT/HTML and media. This module walks that mess and yields one
row per record so ``LI_eater.py`` can write ``LI-<date>.csv``.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath

_SKIP_NAMES = {".ds_store", "thumbs.db"}
_TEXT_SUFFIXES = {".txt", ".md", ".log"}
_HTML_SUFFIXES = {".html", ".htm"}
_JSON_SUFFIXES = {".json"}
_CSV_SUFFIXES = {".csv"}
_BINARY_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".pdf",
    ".mp3",
    ".mp4",
    ".mov",
    ".bin",
    ".zip",
    ".docx",
    ".pptx",
}
_EATEN_FIELDS = ("archive", "path", "kind", "row_key", "row_index", "payload")
_TEXT_LIMIT = 2_000_000


@dataclass(frozen=True)
class EatenRow:
    archive: str
    path: str
    kind: str
    row_key: str
    row_index: int
    payload: dict


def default_downloads_dir() -> Path:
    """~/Downloads on macOS/Linux; %USERPROFILE%\\Downloads on Windows."""
    if os.name == "nt":
        home = os.environ.get("USERPROFILE") or str(Path.home())
        return Path(home) / "Downloads"
    return Path.home() / "Downloads"


def default_output_path(day: date | None = None) -> Path:
    stamp = (day or date.today()).isoformat()
    return default_downloads_dir() / f"LI-{stamp}.csv"


def discover_linkedin_exports(folder: Path) -> list[Path]:
    """Zips whose names look like LinkedIn's data archive, oldest first."""
    return list(reversed(list_export_candidates(folder, "*LinkedIn*")))


def initial_search_dir() -> Path:
    """Prefer the current directory (Downloads when you run it from there)."""
    cwd = Path.cwd()
    if list_export_candidates(cwd):
        return cwd
    script_dir = Path(__file__).resolve().parent
    if list_export_candidates(script_dir):
        return script_dir
    downloads = default_downloads_dir()
    if downloads.is_dir():
        return downloads
    return cwd


def list_export_candidates(
    folder: Path,
    mask: str = "*LinkedIn*",
    *,
    include_other_zips: bool = False,
    limit: int = 20,
) -> list[Path]:
    """Newest files/folders in ``folder`` matching a glob mask.

    LinkedIn names the dumps ``Complete_LinkedInDataExport_*.zip`` and
    ``Basic_LinkedInDataExport_*.zip``. The default mask is ``*LinkedIn*``.
    """
    folder = Path(folder)
    if not folder.is_dir():
        return []
    found: set[Path] = set()
    patterns = [mask]
    if mask.lower() != mask:
        patterns.append(mask.lower())
    if "LinkedIn" in mask:
        patterns.append(mask.replace("LinkedIn", "linkedin"))
    for pattern in patterns:
        found.update(folder.glob(pattern))
        if not pattern.endswith(".zip"):
            found.update(folder.glob(f"{pattern}.zip"))
    if include_other_zips:
        found.update(folder.glob("*.zip"))
    candidates: list[Path] = []
    for path in found:
        if path.name.startswith("."):
            continue
        if path.is_file() and path.suffix.lower() == ".zip":
            candidates.append(path)
        elif path.is_dir() and any(path.glob("*.csv")):
            candidates.append(path)
    candidates.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    return candidates[:limit]


def is_eaten_csv(path: Path) -> bool:
    if path.suffix.lower() != ".csv":
        return False
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            header = handle.readline()
    except OSError:
        return False
    return header.lower().startswith("archive,path,kind,row_key,row_index,payload")


def eat_linkedin_sources(sources: Iterable[Path]) -> list[EatenRow]:
    """Walk zips/folders/files. Later sources win on the same (path, row_key)."""
    merged: dict[tuple[str, str], EatenRow] = {}
    for source in sources:
        path = Path(source)
        for row in _eat_one(path):
            merged[(row.path, row.row_key)] = row
    return list(merged.values())


def write_li_csv(path: Path, records: Iterable[EatenRow]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(records, key=lambda r: (r.path, r.row_index, r.row_key))
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_EATEN_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "archive": row.archive,
                    "path": row.path,
                    "kind": row.kind,
                    "row_key": row.row_key,
                    "row_index": row.row_index,
                    "payload": json.dumps(row.payload, ensure_ascii=False, sort_keys=True),
                }
            )
    return path


def read_li_csv(path: Path) -> list[EatenRow]:
    records: list[EatenRow] = []
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            payload = json.loads(raw["payload"] or "{}")
            records.append(
                EatenRow(
                    archive=raw.get("archive") or Path(path).name,
                    path=raw["path"],
                    kind=raw.get("kind") or "csv",
                    row_key=raw["row_key"],
                    row_index=int(raw.get("row_index") or 0),
                    payload=payload if isinstance(payload, dict) else {"_value": payload},
                )
            )
    return records


def _eat_one(path: Path) -> list[EatenRow]:
    if path.is_dir():
        return _eat_directory(path)
    if path.suffix.lower() == ".zip":
        return _eat_zip(path)
    if is_eaten_csv(path):
        return read_li_csv(path)
    data = path.read_bytes()
    return _rows_from_bytes(path.name, data, archive=path.name)


def _eat_directory(root: Path) -> list[EatenRow]:
    records: list[EatenRow] = []
    files = [p for p in root.rglob("*") if p.is_file()]
    rels = [_safe_rel(str(p.relative_to(root)).replace("\\", "/")) for p in files]
    mapping = _strip_wrapper(rels)
    for file_path, original_rel in zip(files, rels, strict=True):
        rel = mapping[original_rel]
        if _should_skip(rel):
            continue
        records.extend(_rows_from_bytes(rel, file_path.read_bytes(), archive=root.name))
    return records


def _eat_zip(path: Path) -> list[EatenRow]:
    records: list[EatenRow] = []
    with zipfile.ZipFile(path) as zf:
        members = [(name, _safe_rel(name)) for name in zf.namelist() if not name.endswith("/")]
        mapping = _strip_wrapper([rel for _, rel in members])
        for member, original_rel in members:
            rel = mapping[original_rel]
            if _should_skip(rel):
                continue
            records.extend(_rows_from_bytes(rel, zf.read(member), archive=path.name))
    return records


def _rows_from_bytes(relative_path: str, data: bytes, *, archive: str) -> list[EatenRow]:
    kind = _media_kind(relative_path, data)
    parsed = _parse_bytes(kind, data)
    rows: list[EatenRow] = []
    for index, payload in enumerate(parsed):
        key = make_row_key(relative_path, payload)
        rows.append(
            EatenRow(
                archive=archive,
                path=relative_path,
                kind=kind,
                row_key=key,
                row_index=index,
                payload=payload,
            )
        )
    if kind == "binary" and not rows:
        payload = {
            "_binary": True,
            "byte_size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        rows.append(
            EatenRow(
                archive=archive,
                path=relative_path,
                kind=kind,
                row_key=make_row_key(relative_path, payload),
                row_index=0,
                payload=payload,
            )
        )
    return rows


def _parse_bytes(kind: str, data: bytes) -> list[dict]:
    if kind == "csv":
        return _parse_csv(data)
    if kind == "json":
        return _parse_json(data)
    if kind in {"text", "html"}:
        text = data.decode("utf-8-sig", errors="replace")
        if len(text) > _TEXT_LIMIT:
            text = text[:_TEXT_LIMIT]
        return [{"_text": text}]
    if kind == "binary":
        return []
    text = data.decode("utf-8-sig", errors="replace")
    return [{"_text": text[:_TEXT_LIMIT]}]


def _parse_csv(data: bytes) -> list[dict]:
    text = data.decode("utf-8-sig", errors="replace")
    lines = text.splitlines()
    header_idx = _csv_header_index(lines)
    reader = csv.reader(lines[header_idx:])
    try:
        raw_header = next(reader)
    except StopIteration:
        return []
    fieldnames = _unique_csv_headers(raw_header)
    rows: list[dict] = []
    for cells in reader:
        payload = {
            fieldnames[i] if i < len(fieldnames) else f"column_{i+1}": _csv_cell(cell)
            for i, cell in enumerate(cells)
        }
        if any(payload.values()):
            rows.append(payload)
    return rows


def _unique_csv_headers(headers: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    unique: list[str] = []
    for raw in headers:
        name = (raw or "").strip() or "column"
        count = seen.get(name, 0) + 1
        seen[name] = count
        unique.append(name if count == 1 else f"{name}_{count}")
    return unique


def _csv_cell(value: object) -> str:
    """DictReader used to yield a list when extra cells outran the header."""
    if value is None:
        return ""
    if isinstance(value, list):
        return " | ".join(part for part in (_csv_cell(item) for item in value) if part)
    return str(value).strip()


def _csv_header_index(lines: list[str]) -> int:
    for i, line in enumerate(lines):
        stripped = line.strip().lstrip("\ufeff")
        if not stripped:
            continue
        if stripped.lower().startswith("notes:"):
            continue
        try:
            fields = [f.strip() for f in next(csv.reader([stripped])) if f.strip()]
        except csv.Error:
            continue
        if len(fields) < 2:
            continue
        if any(len(field) > 80 for field in fields):
            continue
        return i
    return 0


def _parse_json(data: bytes) -> list[dict]:
    parsed = json.loads(data.decode("utf-8-sig"))
    if isinstance(parsed, dict):
        return [parsed]
    if isinstance(parsed, list):
        rows: list[dict] = []
        for item in parsed:
            if isinstance(item, dict):
                rows.append(item)
            else:
                rows.append({"_value": item})
        return rows
    return [{"_value": parsed}]


def _identity_key(profile_url: str | None, email: str | None) -> str:
    url = (profile_url or "").strip().rstrip("/").lower()
    if url.startswith("http://"):
        url = "https://" + url[len("http://") :]
    if url:
        return url
    mail = (email or "").strip().lower()
    if mail:
        return mail
    raise ValueError("LinkedIn row needs a profile URL or an email")


def make_row_key(relative_path: str, payload: dict) -> str:
    name = PurePosixPath(relative_path).name.lower()
    lookup = {
        str(key).strip().lower(): "" if value is None else str(value).strip()
        for key, value in payload.items()
    }
    if name == "profile.csv":
        return "profile"
    if payload.get("_binary"):
        return "binary"
    if payload.get("_text") is not None and set(payload.keys()) <= {"_text"}:
        return "text"
    url = lookup.get("url") or lookup.get("profile url") or lookup.get("sender profile url")
    email = lookup.get("email address") or lookup.get("email")
    if url or email:
        try:
            return _clip(_identity_key(url or None, email or None))
        except ValueError:
            pass
    conversation = lookup.get("conversation id")
    if conversation:
        return _clip(
            "|".join([conversation, lookup.get("date", ""), lookup.get("content", "")[:80]])
        )
    company = lookup.get("company name") or lookup.get("company")
    title = lookup.get("title") or lookup.get("job title")
    started = lookup.get("started on") or lookup.get("applied at") or lookup.get("connected on")
    if company or title:
        return _clip(f"{company}|{title}|{started}")
    return _digest(payload)


def _clip(value: str) -> str:
    if len(value) <= 400:
        return value
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _digest(payload: dict) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _media_kind(relative_path: str, data: bytes) -> str:
    suffix = PurePosixPath(relative_path).suffix.lower()
    if suffix in _CSV_SUFFIXES:
        return "csv"
    if suffix in _JSON_SUFFIXES:
        return "json"
    if suffix in _TEXT_SUFFIXES:
        return "text"
    if suffix in _HTML_SUFFIXES:
        return "html"
    if suffix in _BINARY_SUFFIXES:
        return "binary"
    if not data:
        return "other"
    sample = data[:4096]
    if b"\x00" in sample:
        return "binary"
    return "other"


def _should_skip(rel: str) -> bool:
    lower = rel.replace("\\", "/").lower()
    name = PurePosixPath(lower).name
    if name in _SKIP_NAMES or name.startswith("._"):
        return True
    return lower.startswith("__macosx/") or "/__macosx/" in f"/{lower}"


def _safe_rel(name: str) -> str:
    posix = name.replace("\\", "/").lstrip("/")
    parts = PurePosixPath(posix).parts
    if ".." in parts:
        raise ValueError(f"unsafe archive path: {name}")
    return str(PurePosixPath(*parts)) if parts else posix


def _strip_wrapper(rels: list[str]) -> dict[str, str]:
    if not rels:
        return {}
    first = {item.split("/")[0] for item in rels if item}
    if len(first) == 1 and all("/" in item for item in rels):
        root = next(iter(first))
        return {item: item[len(root) + 1 :] for item in rels}
    return {item: item for item in rels}
