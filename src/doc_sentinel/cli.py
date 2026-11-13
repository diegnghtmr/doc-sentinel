"""doc-sentinel command line (PLAN §6).

Exit codes: 0 ok/clean, 1 findings/residual/rolled_back, 2 error or invalid usage.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from doc_sentinel import __version__
from doc_sentinel.adapters.base import repo_root
from doc_sentinel.intents import classify
from doc_sentinel.pins import checkout_sha, load_lock
from doc_sentinel.pipeline import UsageError, default_output, run_clean, run_detect
from doc_sentinel.report import (
    PinnedTarget,
    UnsafePathError,
    pin_target,
    same_file,
    to_human_summary,
    write_report,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="doc-sentinel",
        description="Clean deterministic AI-tool traces and detect prompt injection in "
                    "documents by orchestrating pinned sub-harnesses.")
    sub = parser.add_subparsers(dest="command", required=True)

    clean = sub.add_parser("clean", help="write a cleaned copy (never modifies the input)")
    clean.add_argument("file", type=Path)
    clean.add_argument("-o", "--output", type=Path,
                       help="destination (default: <stem>.cleaned<suffix> next to the input)")
    clean.add_argument("--force-output", action="store_true",
                       help="replace an existing output file (never the input)")
    clean.add_argument("--json", action="store_true", help="print the JSON report on stdout")
    clean.add_argument("--report", type=Path,
                       help="write the report to PATH (.md = Markdown, otherwise JSON)")

    detect = sub.add_parser("detect", help="read-only scan for prompt injection / hidden text")
    detect.add_argument("file", type=Path)
    detect.add_argument("--json", action="store_true", help="print the JSON report on stdout")
    detect.add_argument("--report", type=Path,
                        help="write the report to PATH (.md = Markdown, otherwise JSON)")
    detect.add_argument("--allow-network", action="store_true",
                        help="allow sub-harnesses to use the network (off by default)")
    detect.add_argument("--fail-on", choices=("high", "medium"), default="high",
                        help="lowest severity that makes the exit code 1 (default: high)")

    update = sub.add_parser("update", help="move pins to the latest main, roll back on failure")
    update.add_argument("--dry-run", action="store_true",
                        help="report what would change without moving any pin")
    update.add_argument("--only", metavar="ID", help="update only this pin id")
    update.add_argument("--json", action="store_true", help="print the JSON report on stdout")

    sub.add_parser("version", help="print version and pinned sub-harness SHAs")

    intent = sub.add_parser("intent", help="classify a natural-language request into a mode")
    intent.add_argument("text", nargs="+")
    return parser


def _pin_report(args: argparse.Namespace) -> PinnedTarget | None:
    """Validate --report before any untrusted work runs (see report.PinnedTarget)."""
    if not args.report:
        return None
    protect = [args.file]
    if args.command == "clean":
        protect.append(args.output or default_output(args.file))
    for item in protect:
        if same_file(args.report, item):
            raise UsageError("report path must differ from the input and the output document")
    return pin_target(args.report, protect=protect)


def _emit(report: dict, args: argparse.Namespace, report_target: PinnedTarget | None) -> None:
    if report_target is not None:
        write_report(report, report_target)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        print(to_human_summary(report), file=sys.stderr)
    else:
        print(to_human_summary(report))


def _version() -> int:
    print(f"doc-sentinel {__version__}")
    root = repo_root()
    try:
        lock = load_lock(root)
    except (OSError, ValueError):
        print("pins.lock.json not found", file=sys.stderr)
        return 0
    for pin in lock.get("pins", []):
        actual = checkout_sha(root, pin["path"])
        state = "ok" if actual == pin["sha"] else f"MISMATCH checkout={actual}"
        print(f"  {pin['id']:<26} {pin['sha'][:12]}  {state}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "version":
            return _version()
        if args.command == "intent":
            mode = classify(" ".join(args.text))
            if mode is None:
                print("unknown: ask the user which mode they want", file=sys.stderr)
                return 2
            print(mode)
            return 0
        if args.command == "detect":
            if not args.file.is_file():
                raise UsageError(f"input not found: {args.file}")
            report_target = _pin_report(args)
            report, code = run_detect(args.file, allow_network=args.allow_network,
                                      fail_on=args.fail_on)
            _emit(report, args, report_target)
            return code
        if args.command == "clean":
            if not args.file.is_file():
                raise UsageError(f"input not found: {args.file}")
            report_target = _pin_report(args)
            report, code = run_clean(args.file, output=args.output,
                                     force_output=args.force_output, report_path=args.report)
            _emit(report, args, report_target)
            return code
        if args.command == "update":
            from doc_sentinel.update import run_update

            result, code = run_update(repo_root(), dry_run=args.dry_run, only=args.only)
            if args.json:
                print(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                for item in result.get("pins", []):
                    print(f"{item['id']}: {item['status']}")
            return code
    except UnsafePathError as exc:
        print(f"doc-sentinel: unsafe write refused: {exc}", file=sys.stderr)
        return 2
    except UsageError as exc:
        print(f"doc-sentinel: {exc}", file=sys.stderr)
        return 2
    except ImportError as exc:
        print(f"doc-sentinel: not available: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
