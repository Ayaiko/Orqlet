"""Opt-in metadata projection of the existing adapters, never raw log records."""
from .profile import InputError, Trace, _read_file, number, timestamp, union


SCHEMA = "orqlet.timeline.v1"
# UTC years 0001 through 9999; reject nonsense native epoch metadata.
MIN_TIME = -62135596800000
MAX_TIME = 253402300799999
EVIDENCE = {
    "codex_item_metadata": "verified_metadata",
    "claude_reported_thinking_duration": "verified_metadata",
    "paired_log_timestamps": "inferred",
    "inferred_paired_agent_wait_identity": "inferred",
    "caller_declared_span": "caller_declared",
    "task_boundary": "inferred",
    "unpaired_tool_call": "unknown",
    "unattributed_task_residual": "unknown",
}
DIAGNOSTICS = {
    "invalid_or_missing_timing", "duplicate_record", "invalid_call_id",
    "duplicate_call_start", "conflicting_call_start", "duplicate_call_end",
    "conflicting_call_end", "duplicate_task_start", "unmatched_task_end",
    "missing_agent_wait_item_timing", "invalid_call_timing",
}


def safe_time(value):
    return value if number(value) and MIN_TIME <= value <= MAX_TIME else None


def residuals(windows, activity):
    """Subtract activity union inside task union without interpreting idle gaps."""
    activity = union(activity)
    cursor = 0
    for start, end in union(windows):
        while cursor < len(activity) and activity[cursor][1] <= start:
            cursor += 1
        i, point = cursor, start
        while i < len(activity) and activity[i][0] < end:
            left, right = activity[i]
            if left > point:
                yield point, min(left, end)
            point = max(point, right)
            if point >= end:
                break
            i += 1
        if point < end:
            yield point, end


class TimelineTrace(Trace):
    def __init__(self, provider="auto", max_events=50_000):
        super().__init__(provider)
        self.events = []
        self.max_events = max_events
        self.previous_record_time = None
        self.record_time_regressions = 0
        self.missing_record_timestamps = 0

    def emit(self, start, end, category, source, problem=None):
        if len(self.events) >= self.max_events:
            raise InputError("timeline_event_limit_exceeded")
        left, right = safe_time(start), safe_time(end)
        if problem is None:
            if (number(start) and left is None) or (number(end) and right is None):
                problem = "out_of_range_timestamp"
            elif left is None or right is None:
                problem = "missing_timestamp"
            elif right < left:
                problem = "reversed_timestamp"
        usable = problem is None
        # No source identifiers, text, argument hashes, paths or arbitrary labels.
        self.events.append({
            "id": "e%d" % (len(self.events) + 1),
            "category": category,
            "source": source,
            "evidence": EVIDENCE[source] if usable else "unknown",
            "start_unix_ms": left,
            "end_unix_ms": right,
            "duration_ms": right - left if usable else None,
            "timing_quality": problem or "usable",
        })

    def add_span(self, start, end, kind, source):
        valid = super().add_span(start, end, kind, source)
        self.emit(start, end, kind, source)
        return valid

    def consume(self, obj, line):
        recognized = self.recognized
        closing_key = None
        payload = obj.get("payload") if isinstance(obj, dict) else None
        message = obj.get("message") if isinstance(obj, dict) else None
        if (isinstance(payload, dict) and obj.get("type") == "event_msg" and
                payload.get("type") in {"task_complete", "task_completed", "turn_aborted"}):
            closing_key = payload.get("turn_id", "current")
        elif isinstance(obj, dict) and (
                (obj.get("type") == "assistant" and isinstance(message, dict) and
                 message.get("stop_reason") == "end_turn") or
                (obj.get("type") == "system" and obj.get("subtype") == "stop_hook_summary" and
                 obj.get("preventedContinuation") is False)):
            closing_key = self.last_claude_turn
        had_open = closing_key is not None and closing_key in self.open_windows
        before = self.open_windows.get(closing_key) if had_open else None
        window_count = len(self.windows)
        super().consume(obj, line)
        if self.recognized == recognized or self.provider == "trace":
            return
        ts = timestamp(obj.get("timestamp"))
        if ts is None:
            self.missing_record_timestamps += 1
        else:
            if self.previous_record_time is not None and ts < self.previous_record_time:
                self.record_time_regressions += 1
            self.previous_record_time = ts
        # Closed boundaries discarded by the aggregate adapter still have unknown timing.
        if had_open and closing_key not in self.open_windows and len(self.windows) == window_count:
            self.emit(before, ts, "task_window", "task_boundary",
                      "reversed_timestamp" if number(before) and number(ts) and ts < before
                      else "missing_timestamp")

    def export(self):
        # Exactly one finalization: the original report pairs calls and adds spans.
        # Its return value (including raw-input fingerprints) never enters the export.
        self.report()
        for key, (start, _name, _tag, _signature, agent_wait) in self.starts.items():
            end = self.ends.get(key, (None,))[0]
            if key in self.invalid_calls:
                problem = "conflicting_records"
            elif key not in self.ends:
                problem = "unmatched_boundary"
            elif not number(start) or not number(end):
                problem = "missing_timestamp"
            elif end < start:
                problem = "reversed_timestamp"
            else:
                continue  # valid calls already emitted by the canonical finalization
            self.emit(start, end, "tool", "unpaired_tool_call", problem)
            if agent_wait:
                self.emit(start, end, "agent_wait", "inferred_paired_agent_wait_identity", problem)
        for key in self.ends.keys() - self.starts.keys():
            self.emit(None, self.ends[key][0], "tool", "unpaired_tool_call", "unmatched_boundary")
        for start, end in self.windows:
            source = "caller_declared_span" if self.provider == "trace" else "task_boundary"
            self.emit(start, end, "task_window", source)
        for start in self.open_windows.values():
            self.emit(start, None, "task_window", "task_boundary", "unmatched_boundary")
        activity = [(item["start_unix_ms"], item["end_unix_ms"]) for item in self.events
                    if item["category"] != "task_window" and item["timing_quality"] == "usable"]
        for start, end in residuals(self.windows, activity):
            self.emit(start, end, "unattributed", "unattributed_task_residual")
        known_times = [value for item in self.events for value in
                       (item["start_unix_ms"], item["end_unix_ms"]) if value is not None]
        status = ("blocked_record_order_regression" if self.record_time_regressions else
                  "requires_clock_validation" if known_times else "no_usable_timestamps")
        return {
            "format": self.provider,
            "clock": {"basis": "source_wall_clock", "monotonic_available": False,
                      "correlation_status": status,
                      "record_time_regressions": self.record_time_regressions,
                      "missing_record_timestamps": self.missing_record_timestamps},
            "events": self.events,
            "diagnostics": [{"code": code, "count": self.warnings[code]}
                            for code in sorted(DIAGNOSTICS) if self.warnings[code]],
        }


def timeline_file(path, provider="auto", max_bytes=128 * 1024 * 1024,
                  max_line_bytes=8 * 1024 * 1024, max_records=250_000, max_events=50_000):
    trace = TimelineTrace(provider, max_events)
    _read_file(path, trace, max_bytes, max_line_bytes, max_records)
    return trace.export()
