#!/usr/bin/env python3
"""Beacon: pick LinkedIn dump zip(s) and ship them into CareerManager.

Save this in Downloads. Run it from Downloads. It lists the newest
*LinkedIn* files in the current folder, checkboxes to pick chunks, and
ingests them. Later zips merge (add/update only).

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


def _python_with_tk() -> str | None:
    """Homebrew python3 often has no Tk. macOS /usr/bin/python3 usually does."""
    try:
        import tkinter  # noqa: F401
    except ImportError:
        pass
    else:
        return sys.executable
    for python in ("/usr/bin/python3", "/usr/local/bin/python3"):
        if python == sys.executable or not Path(python).is_file():
            continue
        try:
            result = subprocess.run(
                [python, "-c", "import tkinter"],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except OSError:
            continue
        if result.returncode == 0:
            return python
    return None


def _reexec_with_tk() -> None:
    if os.environ.get("LI_EATER_TK_REEXEC"):
        return
    python = _python_with_tk()
    if python is None or Path(python).resolve() == Path(sys.executable).resolve():
        return
    env = os.environ.copy()
    env["LI_EATER_TK_REEXEC"] = "1"
    os.execve(python, [python, str(Path(__file__).resolve()), *sys.argv[1:]], env)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Eat LinkedIn export zip(s) into CareerManager.")
    parser.add_argument("--no-gui", action="store_true", help="No window.")
    parser.add_argument("--source", action="append", type=Path, default=[], help="Zip or folder.")
    parser.add_argument("--mask", default="*LinkedIn*", help="Glob in the search directory.")
    parser.add_argument("--output", type=Path, default=None, help="Optional LI-YYYY-MM-DD.csv.")
    parser.add_argument("--ingest", action="store_true", default=True)
    parser.add_argument("--no-ingest", action="store_false", dest="ingest")
    args = parser.parse_args(argv)
    if not args.no_gui and argv is None:
        _reexec_with_tk()
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
        raise ValueError("Nothing to do: check Ingest or Also save CSV.")
    return "\n".join(notes)


def _ingest(paths: list[Path]) -> str:
    repo = _repo_root()
    uv = repo / ".tools" / "uv-x86_64-apple-darwin" / "uv"
    uv_bin = str(uv) if uv.is_file() else "uv"
    env = os.environ.copy()
    env.setdefault("UV_PROJECT_ENVIRONMENT", str(repo / ".aa-env"))
    chunks: list[str] = []
    for path in paths:
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


def _run_beacon(*, mask: str, ingest_default: bool, save_csv_default: bool) -> int:
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except ImportError:
        print(
            "No window toolkit on this Python. On a Mac use:\n"
            "  /usr/bin/python3 LI_eater.py",
            file=sys.stderr,
        )
        return 1

    from src.memory.linkedin_eater import (
        default_output_path,
        initial_search_dir,
        list_export_candidates,
    )

    root = tk.Tk()
    root.title("LI eater")
    try:
        root.attributes("-topmost", True)
    except tk.TclError:
        pass

    folder = tk.StringVar(value=str(initial_search_dir()))
    glob_mask = tk.StringVar(value=mask)
    ingest_var = tk.BooleanVar(value=ingest_default)
    save_csv_var = tk.BooleanVar(value=save_csv_default)
    show_all_var = tk.BooleanVar(value=False)
    status = tk.StringVar(value="Check the LinkedIn zip(s), then Eat.")
    file_vars: list[tuple[Path, tk.BooleanVar]] = []

    ttk.Label(root, text="Folder (current directory by default)").grid(
        row=0, column=0, sticky="w", padx=8, pady=(8, 2)
    )
    ttk.Entry(root, textvariable=folder, width=56).grid(row=1, column=0, padx=8, sticky="ew")

    def browse() -> None:
        picked = filedialog.askdirectory(parent=root, initialdir=folder.get() or str(Path.cwd()))
        if picked:
            folder.set(picked)
            refresh()

    ttk.Button(root, text="Browse…", command=browse).grid(row=1, column=1, padx=8, pady=2)

    ttk.Label(root, text="Glob mask").grid(row=2, column=0, sticky="w", padx=8, pady=(8, 2))
    ttk.Entry(root, textvariable=glob_mask, width=24).grid(row=3, column=0, padx=8, sticky="w")

    list_frame = ttk.Frame(root)
    list_frame.grid(row=4, column=0, columnspan=2, sticky="nsew", padx=8, pady=8)
    root.columnconfigure(0, weight=1)
    root.rowconfigure(4, weight=1)

    def refresh() -> None:
        for child in list_frame.winfo_children():
            child.destroy()
        file_vars.clear()
        search = Path(folder.get() or ".")
        rows = list_export_candidates(
            search,
            glob_mask.get() or "*LinkedIn*",
            include_other_zips=show_all_var.get(),
        )
        if not rows:
            ttk.Label(
                list_frame,
                text=f"Nothing matching {glob_mask.get()} in {search}",
            ).pack(anchor="w")
            return
        for path in rows:
            checked = "linkedin" in path.name.lower()
            var = tk.BooleanVar(value=checked)
            file_vars.append((path, var))
            when = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            label = f"{path.name}    {when}"
            ttk.Checkbutton(list_frame, variable=var, text=label).pack(anchor="w")

    def eat() -> None:
        picked = [path for path, var in file_vars if var.get()]
        if not picked:
            messagebox.showwarning("LI eater", "Check at least one file.", parent=root)
            return
        dest = default_output_path(date.today()) if save_csv_var.get() else None
        try:
            summary = _run_selected(picked, ingest=ingest_var.get(), output=dest)
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as err:
            messagebox.showerror("LI eater", str(err), parent=root)
            return
        status.set(summary.splitlines()[-1] if summary else "Done")
        messagebox.showinfo("LI eater", summary or "Done", parent=root)
        root.destroy()

    opts = ttk.Frame(root)
    opts.grid(row=5, column=0, columnspan=2, sticky="w", padx=8)
    ttk.Checkbutton(opts, text="Ingest into CareerManager", variable=ingest_var).pack(
        anchor="w"
    )
    ttk.Checkbutton(opts, text="Also save LI-YYYY-MM-DD.csv", variable=save_csv_var).pack(
        anchor="w"
    )
    ttk.Checkbutton(
        opts,
        text="Show other recent *.zip in this folder",
        variable=show_all_var,
        command=refresh,
    ).pack(anchor="w")

    btns = ttk.Frame(root)
    btns.grid(row=6, column=0, columnspan=2, sticky="ew", padx=8, pady=8)
    ttk.Button(btns, text="Refresh", command=refresh).pack(side="left")
    ttk.Button(btns, text="Eat and ingest", command=eat).pack(side="right")
    ttk.Label(root, textvariable=status).grid(
        row=7, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 8)
    )

    glob_mask.trace_add("write", lambda *_: refresh())
    refresh()
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
