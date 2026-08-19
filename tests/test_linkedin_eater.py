"""LI_eater: flatten LinkedIn's mixed zip/folder dump into one ingest file."""

from __future__ import annotations

import csv
import io
import json
import zipfile
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "linkedin"
WRAPPER = "Complete_LinkedInDataExport_08-18-2026"


def _zip_with_nested_mess(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.write(FIXTURES / "Connections.csv", f"{WRAPPER}/Connections.csv")
        zf.write(FIXTURES / "Profile.csv", f"{WRAPPER}/Profile.csv")
        zf.write(
            FIXTURES / "Jobs" / "Job Applications.csv",
            f"{WRAPPER}/Jobs/Job Applications.csv",
        )
        zf.write(
            FIXTURES / "Rich Media" / "photo.bin",
            f"{WRAPPER}/Rich Media/photo.bin",
        )
        zf.writestr(f"{WRAPPER}/__MACOSX/._Connections.csv", b"junk")
        zf.writestr(f"{WRAPPER}/.DS_Store", b"junk")
    return path


def test_default_downloads_dir_is_home_downloads() -> None:
    from src.memory.linkedin_eater import default_downloads_dir

    downloads = default_downloads_dir()
    assert downloads.name == "Downloads"
    assert downloads.parent == Path.home()


def test_flatten_zip_nested_folders_and_skips_junk(tmp_path: Path) -> None:
    from src.memory.linkedin_eater import eat_linkedin_sources

    zip_path = _zip_with_nested_mess(tmp_path / "Complete_LinkedInDataExport_08-18-2026.zip")
    records = eat_linkedin_sources([zip_path])
    paths = {row.path for row in records}
    assert "Connections.csv" in paths
    assert "Profile.csv" in paths
    assert "Jobs/Job Applications.csv" in paths
    assert "Rich Media/photo.bin" in paths
    assert not any(p.lower().startswith("__macosx") for p in paths)
    assert ".DS_Store" not in paths
    jobs = next(r for r in records if r.path == "Jobs/Job Applications.csv")
    assert jobs.payload["Job Title"] == "Staff engineer"


def test_second_chunk_adds_files_and_updates_profile(tmp_path: Path) -> None:
    from src.memory.linkedin_eater import eat_linkedin_sources

    first = tmp_path / "Basic_LinkedInDataExport.zip"
    with zipfile.ZipFile(first, "w") as zf:
        zf.write(FIXTURES / "Connections.csv", "Connections.csv")
        zf.write(FIXTURES / "Profile.csv", "Profile.csv")

    second = tmp_path / "Complete_LinkedInDataExport.zip"
    later_profile = tmp_path / "Profile.csv"
    later_profile.write_text(
        "First Name,Last Name,Maiden Name,Created Date,Address,Industry\n"
        "James,Updated,,01 Jan 2010,,Software\n",
        encoding="utf-8",
    )
    with zipfile.ZipFile(second, "w") as zf:
        zf.write(FIXTURES / "messages.csv", "messages.csv")
        zf.write(later_profile, "Profile.csv")

    records = eat_linkedin_sources([first, second])
    paths = {row.path for row in records}
    assert "Connections.csv" in paths
    assert "messages.csv" in paths
    profile = next(r for r in records if r.path == "Profile.csv")
    assert profile.payload["Last Name"] == "Updated"


def test_write_li_csv_roundtrip(tmp_path: Path) -> None:
    from src.memory.linkedin_eater import eat_linkedin_sources, write_li_csv

    zip_path = _zip_with_nested_mess(tmp_path / "archive.zip")
    records = eat_linkedin_sources([zip_path])
    out = tmp_path / "LI-2026-08-18.csv"
    write_li_csv(out, records)
    text = out.read_text(encoding="utf-8")
    assert text.startswith("archive,path,kind,row_key,row_index,payload")
    rows = list(csv.DictReader(io.StringIO(text)))
    jobs = next(r for r in rows if r["path"] == "Jobs/Job Applications.csv")
    payload = json.loads(jobs["payload"])
    assert payload["Company"] == "Acme"


def test_li_eater_script_no_gui(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from LI_eater import main

    zip_path = _zip_with_nested_mess(tmp_path / "Complete_LinkedInDataExport.zip")
    out = tmp_path / "LI-2026-08-18.csv"
    result = CliRunner().invoke(
        main,
        ["--no-gui", "--source", str(zip_path), "--output", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert out.is_file()
    assert "Jobs/Job Applications.csv" in out.read_text(encoding="utf-8")
