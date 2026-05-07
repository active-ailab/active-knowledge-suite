"""Command-line entry point for Active Knowledge Server."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from active_knowledge_server import __version__


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level CLI parser.

    C1-01 only guarantees the entry point and version output. Concrete subcommands
    are introduced by later Phase 1 tasks.
    """

    parser = argparse.ArgumentParser(prog="active-kb")
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print active-knowledge-server version and exit.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI."""

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(f"active-knowledge-server {__version__}")
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
