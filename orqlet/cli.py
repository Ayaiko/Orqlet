"""Small local CLI. Raw inputs and exception values never enter reports."""
import argparse
import json
import sys
import time

from .profile import InputError, profile_file
from .timeline import SCHEMA as TIMELINE_SCHEMA, timeline_file


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "orqlet: invalid_arguments (use --help)\n")


def positive(value):
    try:
        result = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("requires a positive integer") from None
    if result < 1:
        raise argparse.ArgumentTypeError("requires a positive integer")
    return result


def duration(value):
    return "unknown" if value is None else "%.3f s" % (value / 1000)


def text_report(reports):
    lines = ["Orqlet AI session timing (local, read-only)"]
    for i, r in enumerate(reports, 1):
        c = r["category_union_ms"]
        wait = r["agent_wait_attribution"]
        lines += ["", "Input %d [%s]" % (i, r["format"]),
                  "  Log window: %s (includes idle; not AI working time)" % duration(r["log_window_ms"]),
                  "  Completed task windows: %s; open windows: %d" % (duration(r["completed_task_windows_ms"]), r["open_task_windows"]),
                  "  Tool round trips: %s; command metadata: %s" % (duration(c["tool"]), duration(c["command"])),
                  "  Reported reasoning: %s; response: %s" % (duration(c["reasoning"]), duration(c["response"])),
                  "  Agent wait union: %s; user wait: %s" % (duration(c["agent_wait"]), duration(c["user_wait"])),
                  "    Verified metadata: %s; inferred call/result: %s" % (duration(wait["verified_metadata_union_ms"]), duration(wait["inferred_round_trip_union_ms"])),
                  "    Inferred outside verified spans: %s; caller-declared: %s" % (duration(wait["inferred_only_ms"]), duration(wait["caller_declared_union_ms"])),
                  "    Wait calls without usable round-trip timing: %d%s" % (wait["unmeasured_round_trip_calls"], " (duration unknown)" if wait["unmeasured_round_trip_calls"] else ""),
                  "  Unattributed inside completed tasks: %s" % duration(r["unattributed_within_completed_tasks_ms"]),
                  "  Calls: %d complete / %d started; %d open/invalid; %d orphan results" %
                  (r["tool_calls"]["completed"], r["tool_calls"]["started"], r["tool_calls"]["open_or_invalid"], r["tool_calls"]["orphan_outputs"]),
                  "  Repeated signatures: %s (may be valid retries/rechecks)" % (r["repeat_signature_calls"] if r["repeat_signature_calls"] is not None else "unknown"),
                  "  Large output calls (at least 64 KiB): %d" % r["large_output_calls_at_least_64kib"],
                  "  Top tools (durations overlap; workload hints may be inferred):"]
        for tool in r["top_tools"]:
            lines.append("    %s [%s]: %s, %d calls, %s known output bytes, %d unknown outcomes" % (
                tool["tool"], tool["workload_hint"], duration(tool["round_trip_union_ms"]),
                tool["calls"], tool["output_bytes"] if tool["output_bytes"] is not None else "unknown", tool["unknown_outcomes"]))
        if r["largest_outputs"]:
            lines.append("  Largest output groups:")
            for tool in r["largest_outputs"]:
                lines.append("    %s [%s]: %s known bytes; %d unknown sizes" % (
                    tool["tool"], tool["workload_hint"], tool["output_bytes"], tool["unknown_output_sizes"]))
        if r["warnings"]:
            lines.append("  Data-quality counts: " + json.dumps(r["warnings"], sort_keys=True))
    lines += ["", "Unattributed time is not reasoning. Reported model timing is not CPU or proof of waste.",
              "Evidence overlap is not execution concurrency; several spans may describe one operation.",
              "Verified metadata and inferred agent waits can overlap; inferred intervals include transport/queue overhead.",
              "Do not add overlapping categories or elapsed time across independent inputs."]
    return "\n".join(lines)


def main(argv=None):
    parser = SafeParser(prog="orqlet", description="Local read-only diagnostics; no provider API or background collector.")
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("profile", help="Analyze explicitly selected JSONL logs")
    p.add_argument("files", nargs="+")
    p.add_argument("--format", choices=["auto", "codex", "claude", "trace"], default="auto")
    p.add_argument("--json", action="store_true")
    p.add_argument("--top", type=positive, default=5)
    t = commands.add_parser("timeline", help="Export privacy-safe timestamp metadata as JSON (opt-in)")
    t.add_argument("files", nargs="+")
    t.add_argument("--format", choices=["auto", "codex", "claude", "trace"], default="auto")
    t.add_argument("--max-events", type=positive, default=50_000)
    for command in (p, t):
        command.add_argument("--max-file-mib", type=positive, default=128)
        command.add_argument("--max-line-mib", type=positive, default=8)
        command.add_argument("--max-records", type=positive, default=250_000)
    args = parser.parse_args(argv)
    if len(args.files) > 16 or getattr(args, "top", 5) > 20:
        print("orqlet: input_or_output_count_limit", file=sys.stderr)
        return 2
    started = time.perf_counter()
    reports = []
    for i, path in enumerate(args.files, 1):
        try:
            if args.command == "timeline":
                reports.append(timeline_file(path, args.format,
                                             args.max_file_mib * 1024 * 1024,
                                             args.max_line_mib * 1024 * 1024,
                                             args.max_records, args.max_events))
            else:
                reports.append(profile_file(path, args.format, args.top,
                                            args.max_file_mib * 1024 * 1024,
                                            args.max_line_mib * 1024 * 1024,
                                            args.max_records))
        except InputError as error:
            print("orqlet: input_%d:%s" % (i, error), file=sys.stderr)
            return 2
    if args.command == "timeline":
        print(json.dumps({"schema": TIMELINE_SCHEMA, "inputs": reports}, ensure_ascii=False,
                         indent=2, allow_nan=False))
        return 0
    result = {"schema": "orqlet.profile.v1", "inputs": reports,
              "collector_elapsed_ms": round((time.perf_counter() - started) * 1000, 3)}
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else text_report(reports))
    return 0
