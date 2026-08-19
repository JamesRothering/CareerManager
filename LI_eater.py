#!/usr/bin/env python3
"""Beacon: pick LinkedIn dump zip(s) and ship them into CareerManager.

Save this in Downloads. Run it from Downloads. It lists the newest
*LinkedIn* files in the current folder. Type Enter to ingest them.
Later zips merge (add/update only).

    python3 LI_eater.py
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

_REPO_CANDIDATES = (
    Path(os.environ["CAREERMANAGER_ROOT"]) if os.environ.get("CAREERMANAGER_ROOT") else None,
    Path("/Users/macbook/Documents/CareerManager"),
    Path.home() / "Documents" / "CareerManager",
    Path(__file__).resolve().parent,
)


def _repo_root() -> Path:
    for candidate in _REPO_CANDIDATES:
        if candidate is None:
            continue
        marker = candidate / "src" / "memory" / "linkedin_archive.py"
        if marker.is_file():
            return candidate.resolve()
    raise FileNotFoundError(
        "CareerManager not found. Set CAREERMANAGER_ROOT to the repo path."
    )


def _ensure_repo_on_path() -> Path:
    repo = _repo_root()
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    return repo


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Eat LinkedIn export zip(s) into CareerManager.")
    parser.add_argument(
        "--no-gui",
        action="store_true",
        help="Non-interactive: glob/source and ingest.",
    )
    parser.add_argument("--source", action="append", type=Path, default=[], help="Zip or folder.")
    parser.add_argument("--mask", default="*LinkedIn*", help="Glob in the search directory.")
    parser.add_argument("--output", type=Path, default=None, help="Optional LI-YYYY-MM-DD.csv.")
    parser.add_argument("--ingest", action="store_true", default=True)
    parser.add_argument("--no-ingest", action="store_false", dest="ingest")
    args = parser.parse_args(argv)
    _ensure_repo_on_path()
    if not args.no_gui:
        return _run_beacon(
            mask=args.mask,
            ingest_default=args.ingest,
            save_csv_default=args.output is not None,
        )

    from src.memory.linkedin_eater import initial_search_dir, list_export_candidates

    picked = list(args.source)
    if not picked:
        picked = list_export_candidates(initial_search_dir(), args.mask)
    if not picked:
        print(f"No files matching {args.mask} in {initial_search_dir()}", file=sys.stderr)
        return 2
    try:
        print(_run_selected(picked, ingest=args.ingest, output=args.output))
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as err:
        print(err, file=sys.stderr)
        return 1
    return 0


def _run_selected(picked: list[Path], *, ingest: bool, output: Path | None) -> str:
    from src.memory.linkedin_eater import eat_linkedin_sources, write_li_csv

    ordered = sorted(picked, key=lambda item: item.stat().st_mtime)
    notes: list[str] = []
    if output is not None:
        write_li_csv(output, eat_linkedin_sources(ordered))
        notes.append(f"Wrote {output}")
    if ingest:
        notes.append(_ingest(ordered))
    elif output is None:
        raise ValueError("Nothing to do: turn on ingest or save CSV.")
    return "\n".join(notes)


def _ingest(paths: list[Path]) -> str:
    repo = _repo_root()
    uv = repo / ".tools" / "uv-x86_64-apple-darwin" / "uv"
    uv_bin = str(uv) if uv.is_file() else "uv"
    env = os.environ.copy()
    env.setdefault("UV_PROJECT_ENVIRONMENT", str(repo / ".aa-env"))
    chunks: list[str] = []
    for path in paths:
        print(f"Ingesting {path.name} ...")
        result = subprocess.run(
            [uv_bin, "run", "autoapply", "network", "import", "--archive", str(path)],
            cwd=str(repo),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        text = (result.stdout or "").strip() or (result.stderr or "").strip()
        chunks.append(f"{path.name}: {text or result.returncode}")
        if result.returncode != 0:
            raise RuntimeError(f"Ingest failed for {path}\n{result.stderr or result.stdout}")
    return "\n".join(chunks)


def _box(on: bool) -> str:
    return "[x]" if on else "[ ]"


def _choose_folder_macos(initial: Path) -> Path | None:
    quoted = str(initial).replace("\\", "\\\\").replace('"', '\\"')
    script = (
        f'set theFolder to choose folder with prompt "LinkedIn export folder" '
        f'default location POSIX file "{quoted}"\n'
        "return POSIX path of theFolder"
    )
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    text = result.stdout.strip()
    return Path(text) if text else None


def _run_beacon(*, mask: str, ingest_default: bool, save_csv_default: bool) -> int:
    """Terminal picker. Tk aborts on this Mac (needs 12.7, have 12.6)."""
    from src.memory.linkedin_eater import (
        default_output_path,
        initial_search_dir,
        list_export_candidates,
    )

    folder = initial_search_dir()
    glob_mask = mask
    ingest_on = ingest_default
    save_csv = save_csv_default
    show_all = False
    checked: dict[Path, bool] = {}

    def rows() -> list[Path]:
        return list_export_candidates(folder, glob_mask, include_other_zips=show_all)

    def sync_checked(paths: list[Path]) -> None:
        keep = {path: checked.get(path, "linkedin" in path.name.lower()) for path in paths}
        checked.clear()
        checked.update(keep)

    while True:
        paths = rows()
        sync_checked(paths)
        print()
        print("LI eater")
        print(f"Folder: {folder}")
        print(f"Glob:   {glob_mask}")
        print()
        if not paths:
            print(f"  nothing matching {glob_mask}")
        for index, path in enumerate(paths, start=1):
            when = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            kind = "folder" if path.is_dir() else "zip"
            print(f"  {_box(checked[path])} {index:2d}  {path.name}    {when}  ({kind})")
        print()
        print(f"  {_box(ingest_on)} i  Ingest into CareerManager")
        print(f"  {_box(save_csv)} s  Also save LI-YYYY-MM-DD.csv")
        print(f"  {_box(show_all)} z  Show other recent *.zip")
        print()
        print("Enter = eat  |  1 2 = toggle files  |  f = folder  |  g = glob  |  q = quit")
        try:
            reply = input("> ").strip()
        except EOFError:
            return 1
        if reply == "" or reply.lower() in {"e", "eat", "go"}:
            picked = [path for path, on in checked.items() if on]
            if not picked:
                print("Check at least one file (type its number).")
                continue
            dest = default_output_path(date.today()) if save_csv else None
            try:
                summary = _run_selected(picked, ingest=ingest_on, output=dest)
            except (FileNotFoundError, OSError, RuntimeError, ValueError) as err:
                print(err, file=sys.stderr)
                return 1
            print(summary)
            return 0
        lower = reply.lower()
        if lower in {"q", "quit"}:
            return 0
        if lower == "i":
            ingest_on = not ingest_on
            continue
        if lower == "s":
            save_csv = not save_csv
            continue
        if lower == "z":
            show_all = not show_all
            continue
        if lower == "f":
            picked_dir = _choose_folder_macos(folder)
            if picked_dir is None:
                typed = input("Folder path: ").strip()
                picked_dir = Path(typed).expanduser() if typed else None
            if picked_dir is not None and picked_dir.is_dir():
                folder = picked_dir
            continue
        if lower == "g":
            typed = input("Glob mask: ").strip()
            if typed:
                glob_mask = typed
            continue
        for token in reply.replace(",", " ").split():
            if token.isdigit():
                index = int(token) - 1
                if 0 <= index < len(paths):
                    path = paths[index]
                    checked[path] = not checked[path]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
