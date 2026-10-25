"""Bounded JSONL adapters and overlap-aware timing; never execute log content."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import shlex


class InputError(ValueError):
    """Safe diagnostic code; must not contain input values."""


KINDS = {"tool", "command", "reasoning", "response", "agent_wait", "user_wait"}
WORKLOADS = {"test", "build", "git", "filesystem", "network", "wait", "other", "mixed", "unknown"}
TOOLS = {"exec", "exec_command", "write_stdin", "wait", "wait_agent", "sleep",
         "Bash", "Read", "Grep", "Glob", "Edit", "Write", "Task", "Agent",
         "request_user_input", "request_user_input_async", "test", "git", "filesystem"}
# Keep namespace-sensitive agent waits separate from generic execution polling.
AGENT_WAIT_CALLS = {"wait_agent", "collaboration.wait_agent", "collaboration.wait"}


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() * 1000 if parsed.tzinfo else None
    except (ValueError, OverflowError, OSError):
        return None


def payload_bytes(value):
    if value is None:
        return None
    return len((value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, separators=(",", ":"))).encode("utf-8"))


def tool_label(value):
    if not isinstance(value, str):
        return "other"
    short = value.rsplit(".", 1)[-1]
    return short if short in TOOLS else "other"


def workload(command):
    """Conservative hint, not measured attribution; retain no arguments."""
    if isinstance(command, list):
        if len(command) >= 3 and str(command[0]).split("/")[-1] in {"sh", "bash", "zsh"}:
            command = command[-1]
        else:
            command = " ".join(str(x) for x in command)
    if not isinstance(command, str):
        return "unknown"
    if any(x in command for x in ["\n", ";", "&&", "||", "|", "$(`", "$(", "`"]):
        return "mixed"
    try:
        words = shlex.split(command)
    except ValueError:
        return "unknown"
    while words and "=" in words[0] and not words[0].startswith("-"):
        words.pop(0)
    if words and words[0].split("/")[-1] == "mise" and "--" in words:
        words = words[words.index("--") + 1:]
    if not words:
        return "unknown"
    exe = words[0].split("/")[-1]
    if exe == "git":
        return "git"
    if exe in {"rg", "find", "ls", "cat", "stat", "sed", "wc", "du"}:
        return "filesystem"
    if exe in {"gh", "curl", "wget"}:
        return "network"
    if exe == "sleep":
        return "wait"
    if exe in {"pytest", "vitest", "playwright", "jest"}:
        return "test"
    if exe in {"node", "python", "python3"} and (
        "--test" in words or any(x in words for x in {"unittest", "pytest"}) or
        any(x.endswith(("verify_unit_ratchet.mjs", "run_node_tests.mjs", "ci_affected_playwright.mjs")) for x in words)
    ):
        return "test"
    if exe in {"pnpm", "npm", "yarn"}:
        if any(x == "test" or x.startswith("test:") or x in {"vitest", "playwright", "jest"} for x in words[1:]):
            return "test"
        if any(x == "build" or x.startswith("build:") for x in words[1:]):
            return "build"
    return "other"


def union(intervals):
    merged = []
    for start, end in sorted(intervals):
        if end < start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def length(intervals):
    return sum(end - start for start, end in union(intervals))


def intersection_length(left, right):
    left, right = union(left), union(right)
    i = j = 0
    total = 0.0
    while i < len(left) and j < len(right):
        total += max(0, min(left[i][1], right[j][1]) - max(left[i][0], right[j][0]))
        if left[i][1] < right[j][1]:
            i += 1
        else:
            j += 1
    return total


@dataclass
class Span:
    start: float
    end: float
    kind: str


class Trace:
    def __init__(self, provider="auto"):
        self.provider = provider
        self.recognized = 0
        self.times = []
        self.spans = []
        self.windows = []
        self.open_windows = {}
        self.starts = {}
        self.ends = {}
        self.invalid_calls = set()
        self.seen = set()
        self.warnings = Counter()
        self.provenance = Counter()
        self.explicit_tools = []
        self.command_workloads = defaultdict(list)
        self.verified_agent_wait = []
        self.inferred_agent_wait = []
        self.declared_agent_wait = []
        self.last_claude_turn = None

    def observe(self, ts):
        if number(ts):
            self.times.append(ts)

    def add_span(self, start, end, kind, source):
        if not number(start) or not number(end) or end < start:
            self.warnings["invalid_or_missing_timing"] += 1
            return False
        self.spans.append(Span(start, end, kind))
        self.provenance[source] += 1
        self.observe(start)
        self.observe(end)
        return True

    def unique(self, key):
        if key in self.seen:
            self.warnings["duplicate_record"] += 1
            return False
        self.seen.add(key)
        return True

    def start_call(self, key, ts, name, args):
        if not isinstance(key, str) or not key or len(key) > 512:
            self.warnings["invalid_call_id"] += 1
            return
        raw_name, name = name, tool_label(name)
        command = args.get("command", args.get("cmd")) if isinstance(args, dict) else None
        tag = "wait" if name in {"wait", "wait_agent", "sleep", "request_user_input"} else workload(command)
        signature = hashlib.sha256(json.dumps([raw_name, args], sort_keys=True, ensure_ascii=False).encode()).hexdigest() if args is not None and isinstance(raw_name, str) else None
        value = (ts, name, tag, signature, raw_name in AGENT_WAIT_CALLS if isinstance(raw_name, str) else False)
        if key in self.starts:
            self.warnings["duplicate_call_start"] += 1
            if self.starts[key] != value:
                self.invalid_calls.add(key)
                self.warnings["conflicting_call_start"] += 1
            return
        self.starts[key] = value

    def end_call(self, key, ts, output, is_error=None):
        if not isinstance(key, str) or not key or len(key) > 512:
            self.warnings["invalid_call_id"] += 1
            return
        value = (ts, payload_bytes(output), is_error if isinstance(is_error, bool) else None)
        if key in self.ends:
            self.warnings["duplicate_call_end"] += 1
            if self.ends[key] != value:
                self.invalid_calls.add(key)
                self.warnings["conflicting_call_end"] += 1
            return
        self.ends[key] = value

    def consume(self, obj, line):
        if not isinstance(obj, dict):
            raise InputError("record_not_object")
        detected = None
        if obj.get("schema") == "orqlet.trace.v1":
            detected = "trace"
        elif isinstance(obj.get("payload"), dict) and obj.get("type") in {
            "session_meta", "turn_context", "event_msg", "response_item", "world_state", "token_usage_record"
        }:
            detected = "codex"
        elif obj.get("type") in {"user", "assistant", "system", "queue-operation", "attachment"}:
            detected = "claude"
        if self.provider == "auto" and detected:
            self.provider = detected
        if detected and self.provider != detected:
            raise InputError("mixed_or_wrong_format")
        if self.provider == "trace":
            self.consume_trace(obj, line)
        elif self.provider == "codex":
            self.consume_codex(obj, line)
        elif self.provider == "claude":
            self.consume_claude(obj, line)

    def consume_trace(self, obj, line):
        if obj.get("schema") != "orqlet.trace.v1":
            raise InputError("unsupported_trace_schema")
        start, end = timestamp(obj.get("start")), timestamp(obj.get("end"))
        if start is None or end is None or end < start:
            raise InputError("invalid_trace_timing")
        kind, key = obj.get("type"), obj.get("id")
        if not isinstance(key, str) or not key or len(key) > 512:
            raise InputError("invalid_trace_id")
        self.recognized += 1
        if not self.unique(("trace", key)):
            return
        self.observe(start)
        self.observe(end)
        if kind == "window":
            self.windows.append((start, end))
        elif kind == "span" and obj.get("category") in KINDS:
            category = obj["category"]
            self.add_span(start, end, category, "caller_declared_span")
            if category == "agent_wait":
                self.declared_agent_wait.append((start, end))
            if category == "tool":
                size = obj.get("output_bytes")
                if size is not None and (not isinstance(size, int) or isinstance(size, bool) or size < 0):
                    raise InputError("invalid_output_size")
                tag = obj.get("workload", "unknown")
                self.explicit_tools.append((start, end, tool_label(obj.get("tool")), tag if tag in WORKLOADS else "unknown", size))
        else:
            raise InputError("invalid_trace_record")

    def consume_codex(self, obj, line):
        p = obj.get("payload")
        if not isinstance(p, dict):
            return
        self.recognized += 1
        ts = timestamp(obj.get("timestamp"))
        self.observe(ts)
        kind = p.get("type")
        if obj.get("type") == "event_msg":
            key = p.get("turn_id", "current")
            if kind == "task_started":
                if key in self.open_windows:
                    self.warnings["duplicate_task_start"] += 1
                else:
                    self.open_windows[key] = ts
            elif kind in {"task_complete", "task_completed", "turn_aborted"}:
                start = self.open_windows.pop(key, None)
                if number(start) and number(ts) and ts >= start:
                    self.windows.append((start, ts))
                else:
                    self.warnings["unmatched_task_end"] += 1
            elif kind == "item_completed" and isinstance(p.get("item"), dict):
                item = p["item"]
                if not self.unique(("item", item.get("id") or line, item.get("type"))):
                    return
                category = {"Reasoning": "reasoning", "AgentMessage": "response",
                            "CommandExecution": "command", "CollabAgentToolCall": "agent_wait"}.get(item.get("type"))
                if category:
                    start, end = p.get("started_at_ms"), p.get("completed_at_ms")
                    duration = item.get("duration")
                    if category == "command" and isinstance(duration, dict) and number(end):
                        secs, nanos = duration.get("secs"), duration.get("nanos")
                        if number(secs) and number(nanos) and secs >= 0 and 0 <= nanos < 1_000_000_000:
                            start = end - secs * 1000 - nanos / 1_000_000
                    # Only explicit wait calls are waiting; spawn/send are other activity.
                    if category == "agent_wait" and item.get("tool") not in {"wait", "wait_agent"}:
                        return
                    valid = self.add_span(start, end, category, "codex_item_metadata")
                    if category == "agent_wait":
                        if valid:
                            self.verified_agent_wait.append((start, end))
                        else:
                            self.warnings["missing_agent_wait_item_timing"] += 1
                    if category == "command" and number(start) and number(end) and end >= start:
                        self.command_workloads[workload(item.get("command"))].append((start, end))
        if obj.get("type") == "response_item":
            if kind in {"function_call", "custom_tool_call"}:
                args = p.get("arguments", p.get("input"))
                if isinstance(args, str) and kind == "function_call":
                    try:
                        args = json.loads(args)
                    except (ValueError, RecursionError):
                        args = None
                self.start_call(p.get("call_id"), ts, p.get("name"), args)
            elif kind in {"function_call_output", "custom_tool_call_output"}:
                self.end_call(p.get("call_id"), ts, p.get("output"), p.get("is_error"))

    def consume_claude(self, obj, line):
        if obj.get("type") not in {"assistant", "user", "system", "queue-operation", "attachment"}:
            return  # request blobs/progress copies are not additional calls
        self.recognized += 1
        ts = timestamp(obj.get("timestamp"))
        self.observe(ts)
        message = obj.get("message", {})
        if not isinstance(message, dict):
            return
        blocks = message.get("content", [])
        blocks = blocks if isinstance(blocks, list) else []
        is_result = any(isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks)
        if obj.get("type") == "user" and not is_result and not obj.get("isMeta"):
            self.last_claude_turn = str(obj.get("uuid", line))
            self.open_windows[self.last_claude_turn] = ts
        duration = obj.get("thinkingDurationMs")
        if obj.get("type") == "assistant" and number(duration) and duration >= 0 and number(ts):
            if self.unique(("thinking", obj.get("uuid") or line)):
                self.add_span(ts - duration, ts, "reasoning", "claude_reported_thinking_duration")
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if obj.get("type") == "assistant" and block.get("type") == "tool_use":
                self.start_call(block.get("id"), ts, block.get("name"), block.get("input"))
            elif obj.get("type") == "user" and block.get("type") == "tool_result":
                self.end_call(block.get("tool_use_id"), ts, block.get("content"), block.get("is_error"))
        end_turn = obj.get("type") == "assistant" and message.get("stop_reason") == "end_turn"
        hook_stop = obj.get("type") == "system" and obj.get("subtype") == "stop_hook_summary" and obj.get("preventedContinuation") is False
        if (end_turn or hook_stop) and self.last_claude_turn:
            start = self.open_windows.pop(self.last_claude_turn, None)
            if number(start) and number(ts) and ts >= start:
                self.windows.append((start, ts))
            self.last_claude_turn = None

    def report(self, top=5):
        groups = defaultdict(lambda: {"calls": 0, "completed": 0, "intervals": [], "bytes": 0,
                                      "unknown_output_sizes": 0, "explicit_errors": 0, "unknown_outcomes": 0, "inferred": True})
        signatures = Counter()
        large_outputs = 0
        completed = 0
        unmeasured_agent_wait_calls = 0
        for key, (start, name, tag, signature, agent_wait) in self.starts.items():
            g = groups[(name, tag)]
            g["calls"] += 1
            if signature is not None:
                signatures[signature] += 1
            if key not in self.ends or key in self.invalid_calls:
                unmeasured_agent_wait_calls += agent_wait
                continue
            end, size, error = self.ends[key]
            if not number(start) or not number(end) or end < start:
                self.warnings["invalid_call_timing"] += 1
                unmeasured_agent_wait_calls += agent_wait
                continue
            completed += 1
            self.add_span(start, end, "tool", "paired_log_timestamps")
            if agent_wait:
                self.inferred_agent_wait.append((start, end))
                self.add_span(start, end, "agent_wait", "inferred_paired_agent_wait_identity")
            g["completed"] += 1
            g["intervals"].append((start, end))
            g["bytes"] += size or 0
            g["unknown_output_sizes"] += size is None
            large_outputs += size is not None and size >= 65536
            g["explicit_errors"] += error is True
            g["unknown_outcomes"] += error is None
        for start, end, name, tag, size in self.explicit_tools:
            g = groups[(name, tag)]
            g["calls"] += 1
            g["completed"] += 1
            g["intervals"].append((start, end))
            g["bytes"] += size or 0
            g["unknown_output_sizes"] += size is None
            large_outputs += size is not None and size >= 65536
            g["unknown_outcomes"] += 1
            g["inferred"] = False
        by_kind = defaultdict(list)
        for span in self.spans:
            by_kind[span.kind].append((span.start, span.end))
        activity = [(s.start, s.end) for s in self.spans]
        log_ms = max(self.times) - min(self.times) if self.times else None
        activity_ms = length(activity) if activity else None
        task_ms = length(self.windows) if self.windows else None
        rows = []
        for (name, tag), g in groups.items():
            rows.append({"tool": name, "workload_hint": tag, "workload_is_inferred": g["inferred"],
                         "calls": g["calls"], "completed": g["completed"],
                         "round_trip_union_ms": round(length(g["intervals"]), 3) if g["intervals"] else None,
                         "output_bytes": g["bytes"] if g["completed"] > g["unknown_output_sizes"] else None,
                         "unknown_output_sizes": g["unknown_output_sizes"], "explicit_errors": g["explicit_errors"],
                         "unknown_outcomes": g["unknown_outcomes"]})
        rows.sort(key=lambda r: (-(r["round_trip_union_ms"] or 0), -(r["output_bytes"] or 0), r["tool"], r["workload_hint"]))
        return {
            "format": self.provider,
            "log_window_ms": round(log_ms, 3) if log_ms is not None else None,
            "completed_task_windows_ms": round(task_ms, 3) if task_ms is not None else None,
            "completed_task_windows": len(self.windows), "open_task_windows": len(self.open_windows),
            "observed_activity_union_ms": round(activity_ms, 3) if activity_ms is not None else None,
            "category_union_ms": {kind: round(length(by_kind[kind]), 3) if kind in by_kind else None for kind in sorted(KINDS)},
            "agent_wait_attribution": {
                "verified_metadata_union_ms": round(length(self.verified_agent_wait), 3) if self.verified_agent_wait else None,
                "inferred_round_trip_union_ms": round(length(self.inferred_agent_wait), 3) if self.inferred_agent_wait else None,
                "inferred_only_ms": round(max(0, length(self.inferred_agent_wait) - intersection_length(self.inferred_agent_wait, self.verified_agent_wait)), 3) if self.inferred_agent_wait else None,
                "caller_declared_union_ms": round(length(self.declared_agent_wait), 3) if self.declared_agent_wait else None,
                "attributed_union_ms": round(length(by_kind["agent_wait"]), 3) if "agent_wait" in by_kind else None,
                "verified_span_count": len(self.verified_agent_wait),
                "inferred_call_count": len(self.inferred_agent_wait),
                "unmeasured_round_trip_calls": unmeasured_agent_wait_calls,
                "unusable_metadata_spans": self.warnings.get("missing_agent_wait_item_timing", 0),
                "unmeasured_round_trip_ms": None if unmeasured_agent_wait_calls else 0,
            },
            "unattributed_within_completed_tasks_ms": round(max(0, task_ms - intersection_length(activity, self.windows)), 3) if task_ms is not None else None,
            "outside_completed_task_windows_ms": round(max(0, log_ms - task_ms), 3) if task_ms is not None and log_ms is not None else None,
            "evidence_overlap_ms": round(sum(s.end - s.start for s in self.spans) - (activity_ms or 0), 3),
            "tool_calls": {"started": len(self.starts) + len(self.explicit_tools),
                           "completed": completed + len(self.explicit_tools),
                           "open_or_invalid": len(self.starts) - completed,
                           "orphan_outputs": len(set(self.ends) - set(self.starts))},
            "repeat_signature_calls": sum(n - 1 for n in signatures.values()) if signatures else None,
            "signature_comparable_calls": sum(signatures.values()),
            "large_output_calls_at_least_64kib": large_outputs,
            "largest_outputs": sorted([r for r in rows if r["output_bytes"] is not None], key=lambda r: -(r["output_bytes"] or 0))[:top],
            "reported_command_workloads": [{"workload_hint": tag, "workload_is_inferred": True,
                "executions": len(items), "union_ms": round(length(items), 3)} for tag, items in sorted(self.command_workloads.items())],
            "top_tools": rows[:top], "omitted_tool_groups": max(0, len(rows) - top),
            "warnings": dict(sorted(self.warnings.items())),
            "timing_provenance": dict(sorted(self.provenance.items())),
            "interpretation": "Durations can overlap. evidence_overlap_ms is summed recorded span duration minus activity union: overlapping evidence, not execution concurrency. Multiple spans may describe one operation; this value can exceed elapsed time. Actual execution concurrency is not measured. Agent-wait totals union verified metadata, inferred call/result intervals and caller declarations; do not add these. Inferred wait includes transport/queue overhead, not proven blocked-agent time. Unattributed/idle time is not reasoning. Reported model timing is not CPU or proof of waste. Workload hints are heuristic; output bytes are not tokens."
        }


def profile_file(path, provider="auto", top=5, max_bytes=128 * 1024 * 1024,
                 max_line_bytes=8 * 1024 * 1024, max_records=250_000):
    trace = Trace(provider)
    digest = hashlib.sha256()
    byte_count = records = 0
    try:
        path = Path(path)
        if not path.is_file():
            raise InputError("not_a_regular_file")
        if path.stat().st_size > max_bytes:
            raise InputError("file_limit_exceeded")
        with path.open("rb") as stream:
            while True:
                line = stream.readline(max_line_bytes + 1)
                if not line:
                    break
                byte_count += len(line)
                records += 1
                if len(line) > max_line_bytes:
                    raise InputError("line_limit_exceeded")
                if byte_count > max_bytes or records > max_records:
                    raise InputError("input_limit_exceeded")
                digest.update(line)
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                    trace.consume(obj, records)
                except InputError as error:
                    raise InputError("line_%d:%s" % (records, error)) from None
                except (ValueError, UnicodeError, RecursionError, TypeError, KeyError, OverflowError):
                    raise InputError("line_%d:invalid_record" % records) from None
    except OSError:
        raise InputError("cannot_read_input") from None
    if not trace.recognized:
        raise InputError("unsupported_or_empty_log")
    report = trace.report(top)
    report.update({"source_sha256": digest.hexdigest(), "input_bytes": byte_count, "input_records": records})
    return report
