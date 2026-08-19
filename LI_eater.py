#!/usr/bin/env python3
"""Flatten LinkedIn's data-archive mess into one LI-<date>.csv.

LinkedIn zips mix CSVs, nested folders, JSON, and media. Run this, pick the
zip(s) from Downloads (that's the default folder), and get a single file the
CareerManager importer can merge into Postgres — including later chunks.

Usage:
    python LI_eater.py
    python LI_eater.py --no-gui --source ~/Downloads/Complete_LinkedInDataExport.zip
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import click  # noqa: E402

from src.memory.linkedin_eater import (  # noqa: E402
    default_downloads_dir,
    default_output_path,
    discover_linkedin_exports,
    eat_linkedin_sources,
    write_li_csv,
)


@click.command()
@click.option(
    "--no-gui",
    is_flag=True,
    help="Skip the file picker. Use --source, or all LinkedIn zips in Downloads.",
)
@click.option(
    "--source",
    "sources",
    multiple=True,
    type=click.Path(exists=True, path_type=Path),
    help="Zip, unpacked folder, or file. Repeatable. Later sources win on the same row.",
)
@click.option(
    "--output",
    type=click.Path(path_type=Path),
    default=None,
    help="Defaults to ~/Downloads/LI-YYYY-MM-DD.csv (Windows: %%USERPROFILE%%\\Downloads).",
)
def main(no_gui: bool, sources: tuple[Path, ...], output: Path | None) -> None:
    """Read LinkedIn export crap; write one LI-<date>.csv."""
    picked = list(sources)
    if not picked and not no_gui:
        picked = _pick_with_dialog(default_downloads_dir())
    if not picked:
        picked = discover_linkedin_exports(default_downloads_dir())
    if not picked:
        raise click.UsageError(
            f"No LinkedIn zip found. Put the export in {default_downloads_dir()} "
            "or pass --source PATH"
        )
    records = eat_linkedin_sources(picked)
    dest = output or default_output_path(date.today())
    write_li_csv(dest, records)
    click.echo(f"Wrote {len(records)} rows from {len(picked)} source(s) to {dest}")
    click.echo("Ingest with:  uv run autoapply network import --archive " + str(dest))


def _pick_with_dialog(initial: Path) -> list[Path]:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        click.echo("No file-picker available; looking in Downloads instead.", err=True)
        return []

    root = tk.Tk()
    root.withdraw()
    root.update()
    try:
        root.attributes("-topmost", True)
    except tk.TclError:
        pass
    start = str(initial if initial.is_dir() else Path.home())
    files = filedialog.askopenfilenames(
        parent=root,
        title="Select LinkedIn export zip(s). Add the next chunk whenever it arrives.",
        initialdir=start,
        filetypes=[("Zip archives", "*.zip"), ("All files", "*.*")],
    )
    picked = [Path(item) for item in files]
    if not picked:
        folder = filedialog.askdirectory(
            parent=root,
            title="Or pick an unpacked LinkedIn export folder",
            initialdir=start,
        )
        if folder:
            picked = [Path(folder)]
    root.destroy()
    return picked


if __name__ == "__main__":
    main()
