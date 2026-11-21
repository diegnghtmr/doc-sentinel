"""doc-sentinel command line (PLAN §6).

Exit codes: 0 ok/clean, 1 findings/residual/rolled_back, 2 error or invalid usage.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from pathlib import Path

from doc_sentinel import __version__
from doc_sentinel.adapters.base import repo_root
from doc_sentinel.agent_summary import make_summary
from doc_sentinel.intents import classify
from doc_sentinel.pins import checkout_sha, load_lock
from doc_sentinel.pipeline import (
    UsageError,
    default_output,
    default_sanitized_output,
    pin_sanitized_output,
    run_clean,
    run_detect,
    run_sanitize,
)
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
    clean_format = clean.add_mutually_exclusive_group()
    clean_format.add_argument("--json", action="store_true", help="print the JSON report on stdout")
    clean_format.add_argument("--agent-summary", action="store_true",
                              help="print only the orchestrator-safe JSON summary on stdout")
    clean.add_argument("--report", type=Path,
                       help="write the report to PATH (.md = Markdown, otherwise JSON)")

    detect = sub.add_parser("detect", help="read-only scan for prompt injection / hidden text")
    detect.add_argument("file", type=Path)
    detect_format = detect.add_mutually_exclusive_group()
    detect_format.add_argument("--json", action="store_true", help="print the JSON report on stdout")
    detect_format.add_argument("--agent-summary", action="store_true",
                               help="print only the orchestrator-safe JSON summary on stdout")
    detect.add_argument("--sanitize", action="store_true",
                        help="optionally write a separately verified sanitized copy")
    detect.add_argument("-o", "--output", type=Path,
                        help="sanitized destination (requires --sanitize)")
    detect.add_argument("--force-output", action="store_true",
                        help="replace an existing sanitized output after verification")
    detect.add_argument("--review-dir", type=Path,
                        help="write an isolated semantic review plan and batches to DIR")
    detect.add_argument("--report", type=Path,
                        help="write the report to PATH (.md = Markdown, otherwise JSON)")
    detect.add_argument("--allow-network", action="store_true",
                        help="allow sub-harnesses to use the network (off by default)")
    detect.add_argument("--prompt-guard", action="store_true",
                        help="also run the local Prompt Guard 2 classifier (opt-in; requires "
                             "the model already in the Hugging Face cache; never downloads)")
    detect.add_argument("--fail-on", choices=("high", "medium"), default="high",
                        help="lowest severity that makes the exit code 1 (default: high)")

    sanitized = sub.add_parser("sanitize", help="write a checked copy after semantic review")
    sanitized.add_argument("file", type=Path, help="original input, verified against the review plan")
    sanitized.add_argument("--review-dir", required=True, type=Path, help="review plan and answers")
    sanitized.add_argument("-o", "--output", type=Path,
                           help="destination (default: <stem>.sanitized<suffix>)")
    sanitized.add_argument("--force-output", action="store_true",
                           help="replace an existing output only after verification")
    sanitized.add_argument("--report", type=Path, help="write the report to PATH")
    sanitized.add_argument("--agent-summary", action="store_true",
                           help="print only the orchestrator-safe JSON summary on stdout")

    merge = sub.add_parser("review-merge", help="validate reviewer answers and re-aggregate")
    merge.add_argument("--review-dir", required=True, type=Path, help="review plan and answers")
    merge.add_argument("--report", type=Path, help="write the merged report to PATH")
    merge.add_argument("--agent-summary", action="store_true",
                       help="print only the orchestrator-safe JSON summary on stdout")

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
    writing_output = args.command in {"clean", "sanitize"} or getattr(args, "sanitize", False)
    if args.command == "clean":
        protect.append(args.output or default_output(args.file))
    elif writing_output:
        protect.append(args.output or default_sanitized_output(args.file))
    what = ("the input and the output document" if writing_output else "the input document")
    for item in protect:
        if same_file(args.report, item):
            raise UsageError(f"report path must differ from {what}")
    return pin_target(args.report, protect=protect)


def _emit(report: dict, args: argparse.Namespace, report_target: PinnedTarget | None,
          code: int, review: dict | None = None) -> None:
    if report_target is not None:
        write_report(report, report_target)
    if getattr(args, "agent_summary", False):
        input_path = str(args.file) if hasattr(args, "file") else None
        if input_path and input_path.startswith("/work/in/"):
            input_path = os.environ.get("DOC_SENTINEL_HOST_INPUT", input_path)
        output_path = None
        if args.command == "clean" and report.get("output_path"):
            output_path = str(args.output or default_output(args.file))
        elif report.get("sanitize", {}).get("status") == "sanitized":
            output_path = str(args.output or default_sanitized_output(args.file))
        if output_path and output_path.startswith("/work/out/"):
            output_path = os.environ.get("DOC_SENTINEL_HOST_OUTPUT", output_path)
        report_path = str(args.report) if args.report else None
        if report_path and report_path.startswith(("/work/report/", "/work/out/")):
            report_path = os.environ.get("DOC_SENTINEL_HOST_REPORT", report_path)
        if review and str(getattr(args, "review_dir", "")) == "/work/review":
            host = os.environ.get("DOC_SENTINEL_HOST_REVIEW")
            if host:
                review = {**review, "plan_path": str(Path(host) / "plan.json"),
                          "batches": [{**batch, "path": str(Path(host) / "batches" /
                                      f"{batch['batch_id']}.json")} for batch in review["batches"]]}
        summary = make_summary(report, mode=args.command, exit_code=code, input_path=input_path,
                               output_path=output_path, report_path=report_path, review=review)
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        print(to_human_summary(report), file=sys.stderr)
    elif getattr(args, "json", False):
        public_report = copy.deepcopy(report)
        for action in public_report.get("sanitize", {}).get("actions", []):
            action.pop("removed_text", None)
        print(json.dumps(public_report, indent=2, ensure_ascii=False))
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
        if actual is None:
            # Container images and source archives carry no .git metadata to compare.
            state = "pinned (no git checkout to compare)"
        elif actual == pin["sha"]:
            state = "ok"
        else:
            state = f"MISMATCH checkout={actual}"
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
            if not args.sanitize and (args.output is not None or args.force_output):
                raise UsageError("--output and --force-output require --sanitize")
            dest = output_target = None
            if args.sanitize:
                dest, output_target = pin_sanitized_output(args.file, args.output, args.force_output,
                                                            args.report)
            report_target = _pin_report(args)
            targets = None
            if args.review_dir:
                from doc_sentinel.review import prepare_review

                root = args.review_dir.resolve()
                if args.report and root in args.report.resolve().parents:
                    raise UsageError("report path must be outside the review directory")
                if dest is not None and (dest == root or root in dest.parents):
                    raise UsageError("output path must be outside the review directory")
                targets = prepare_review(args.review_dir, args.file)
            options = {"allow_network": args.allow_network, "fail_on": args.fail_on,
                       "prompt_guard": args.prompt_guard}
            if targets is not None:
                options["review_targets"] = targets
            report, code = run_detect(args.file, **options)
            if args.sanitize:
                report, code = run_sanitize(args.file, dest, output_target, report, code)
            _emit(report, args, report_target, code, targets.summary if targets else None)
            return code
        if args.command == "sanitize":
            from doc_sentinel.review import load_plan, merge, public_review

            dest, output_target = pin_sanitized_output(args.file, args.output, args.force_output,
                                                        args.report)
            report_target = _pin_report(args)
            root = args.review_dir.resolve()
            if dest == root or root in dest.parents:
                raise UsageError("output path must be outside the review directory")
            if args.report and (args.report.resolve() == root or root in args.report.resolve().parents):
                raise UsageError("report path must be outside the review directory")
            plan, original = load_plan(root)
            report, code = merge(root, plan, original, include_sanitize_provenance=True)
            report, code = run_sanitize(args.file, dest, output_target, report, code,
                                        plan=plan, review_root=root)
            _emit(report, args, report_target, code, public_review(root, plan))
            return code
        if args.command == "clean":
            if not args.file.is_file():
                raise UsageError(f"input not found: {args.file}")
            report_target = _pin_report(args)
            report, code = run_clean(args.file, output=args.output,
                                     force_output=args.force_output, report_path=args.report)
            _emit(report, args, report_target, code)
            return code
        if args.command == "review-merge":
            from doc_sentinel.review import load_plan, merge, pin_merge_report, public_review

            root = args.review_dir.resolve()
            plan, original = load_plan(root)
            report_target = pin_merge_report(args.report, root, original) if args.report else None
            report, code = merge(root, plan, original)
            _emit(report, args, report_target, code, public_review(root, plan))
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
    except OSError as exc:
        print(f"doc-sentinel: I/O failure: {exc.strerror or 'unavailable'}", file=sys.stderr)
        return 2
    except Exception as exc:
        from doc_sentinel.review import ReviewError

        if not isinstance(exc, ReviewError):
            raise
        print(f"doc-sentinel: {exc}", file=sys.stderr)
        return 2
    except ImportError as exc:
        print(f"doc-sentinel: not available: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
