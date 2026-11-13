"""Command-line entry point (full surface implemented in T3)."""

from __future__ import annotations

import argparse
import sys

from doc_sentinel import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="doc-sentinel")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("version", help="print version and pinned sub-harness SHAs")
    args = parser.parse_args(argv)
    if args.command == "version":
        print(f"doc-sentinel {__version__}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
