"""CLI: flatten-and-import LinkedIn archives, including LI_eater.py output."""

from __future__ import annotations

from pathlib import Path

import click


@click.group(name="network")
def network_cmd() -> None:
    """LinkedIn official data archive (not scrape)."""


@network_cmd.command("import")
@click.option(
    "--archive",
    "archive_path",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Zip, unpacked folder, or LI-<date>.csv from LI_eater.py. Repeat as chunks arrive.",
)
@click.option(
    "--connections",
    "connections_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Connections.csv from LinkedIn's data archive.",
)
@click.option(
    "--followers",
    "followers_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Followers.csv from LinkedIn's data archive.",
)
def import_cmd(
    archive_path: Path | None,
    connections_path: Path | None,
    followers_path: Path | None,
) -> None:
    """Merge an archive chunk into Postgres. Adds and updates; does not delete."""
    if archive_path is None and connections_path is None and followers_path is None:
        raise click.UsageError("Provide --archive and/or --connections/--followers")

    from src.core.config import load_config  # noqa: PLC0415
    from src.core.database import get_session_factory  # noqa: PLC0415
    from src.memory.linkedin_archive import import_linkedin_archive  # noqa: PLC0415
    from src.memory.linkedin_network import import_linkedin_csv  # noqa: PLC0415

    session_factory = get_session_factory(load_config())
    with session_factory() as session:
        if archive_path is not None:
            report = import_linkedin_archive(session, archive_path)
            click.echo(
                f"Archive  inserted={report.inserted}  updated={report.updated}  "
                f"unchanged={report.unchanged}  files={report.file_count}"
            )
        if connections_path is not None:
            report = import_linkedin_csv(session, connections_path, kind="connection")
            click.echo(
                f"Connections  inserted={report.inserted}  "
                f"updated={report.updated}  invalid={report.invalid}"
            )
        if followers_path is not None:
            report = import_linkedin_csv(session, followers_path, kind="follower")
            click.echo(
                f"Followers    inserted={report.inserted}  "
                f"updated={report.updated}  invalid={report.invalid}"
            )
