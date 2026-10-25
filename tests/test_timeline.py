"""Synthetic logs only: never read the developer's actual AI sessions."""
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from orqlet.profile import InputError, Trace, _read_file, length, profile_file
from orqlet.timeline import SCHEMA, TimelineTrace, timeline_file
from test_profile import cx, event, span, ts, window


EPOCH = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)


def wait_metadata(start=1000, end=4000):
    return event(4, "item_completed", started_at_ms=EPOCH + start, completed_at_ms=EPOCH + end,
                 item={"type": "CollabAgentToolCall", "id": "private-provider-id", "tool": "wait"})


def wait_records():
    return [event(0, "task_started", turn_id="private-turn-id"),
            cx(1, "function_call", call_id="private-call-id", name="collaboration.wait_agent"),
            wait_metadata(),
            cx(4, "function_call_output", call_id="private-call-id", output="private output"),
            event(5, "task_complete", turn_id="private-turn-id")]


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "PRIVATE_PATH.jsonl"

    def write(self, records):
        self.path.write_text("".join(json.dumps(r) + "\n" for r in records))
        return self.path

    def export(self, records, **kwargs):
        return timeline_file(self.write(records), **kwargs)

    def cli(self, *args):
        return subprocess.run([sys.executable, "-m", "orqlet", *args], capture_output=True,
                              text=True, timeout=5)

    def test_single_wait_retains_overlapping_evidence_without_claiming_concurrency(self):
        items = self.export(wait_records())["events"]
        waits = [e for e in items if e["category"] == "agent_wait"]
        self.assertEqual({e["evidence"] for e in waits}, {"verified_metadata", "inferred"})
        self.assertEqual(length([(e["start_unix_ms"], e["end_unix_ms"]) for e in waits]), 3000)
        self.assertEqual(sum(e["duration_ms"] for e in waits), 6000)
        unknown = [e for e in items if e["category"] == "unattributed"]
        self.assertEqual(sum(e["duration_ms"] for e in unknown), 2000)
        self.assertTrue(all(e["evidence"] == "unknown" for e in unknown))
        self.assertFalse(any("concurrency" in key for e in items for key in e))

    def test_export_does_not_change_any_existing_profile_calculation(self):
        fixtures = [wait_records(), [window("w", 0, 10), span("a", 1, 6), span("b", 3, 7)],
                    [cx(4, "function_call_output", call_id="c"),
                     cx(1, "function_call", call_id="c", name="Read")],
                    [event(0, "task_started"), event(3, "item_completed", item={"type": "Reasoning"})]]
        for records in fixtures:
            with self.subTest(records=records):
                self.write(records)
                trace, observed = Trace(), TimelineTrace()
                metadata = _read_file(self.path, trace, 100000, 10000, 100)
                _read_file(self.path, observed, 100000, 10000, 100)
                expected = trace.report()
                self.assertEqual(observed.report(), expected)
                expected.update(metadata)
                before = profile_file(self.path)
                timeline_file(self.path)
                self.assertEqual(before, expected)
                self.assertEqual(profile_file(self.path), before)

    def test_missing_item_timestamps_are_unknown_and_paired_wait_stays_inferred(self):
        for removed in [("started_at_ms",), ("completed_at_ms",), ("started_at_ms", "completed_at_ms")]:
            records = wait_records()
            for name in removed:
                records[2]["payload"].pop(name)
            waits = [e for e in self.export(records)["events"] if e["category"] == "agent_wait"]
            self.assertEqual({e["evidence"] for e in waits}, {"unknown", "inferred"})
            missing = next(e for e in waits if e["evidence"] == "unknown")
            self.assertIsNone(missing["duration_ms"])
            self.assertEqual(missing["timing_quality"], "missing_timestamp")

    def test_unpaired_and_orphan_events_preserve_only_known_endpoint(self):
        items = self.export([cx(1, "function_call", call_id="a", name="wait_agent"),
                             cx(3, "function_call_output", call_id="orphan")])["events"]
        self.assertEqual(len(items), 3)
        self.assertTrue(all(e["duration_ms"] is None and e["evidence"] == "unknown" for e in items))
        self.assertEqual([(e["start_unix_ms"], e["end_unix_ms"]) for e in items],
                         [(EPOCH + 1000, None), (EPOCH + 1000, None), (None, EPOCH + 3000)])

    def test_missing_call_timestamp_never_uses_timeout_argument(self):
        start = cx(1, "function_call", call_id="a", name="wait_agent", arguments='{"timeout_ms":60000}')
        start.pop("timestamp")
        result = self.export([start, cx(4, "function_call_output", call_id="a")])
        self.assertEqual(result["clock"]["missing_record_timestamps"], 1)
        self.assertTrue(all(e["duration_ms"] is None for e in result["events"]))

    def test_reverse_call_and_record_order_are_not_repaired_or_sorted(self):
        result = self.export([cx(3, "function_call", call_id="a", name="Read"),
                              cx(1, "function_call_output", call_id="a")])
        self.assertEqual(result["clock"]["correlation_status"], "blocked_record_order_regression")
        self.assertEqual(result["clock"]["record_time_regressions"], 1)
        item = result["events"][0]
        self.assertEqual(item["timing_quality"], "reversed_timestamp")
        self.assertEqual(item["start_unix_ms"], EPOCH + 3000)
        self.assertEqual(item["end_unix_ms"], EPOCH + 1000)
        self.assertIsNone(item["duration_ms"])

    def test_out_of_order_valid_pair_retains_inferred_evidence_but_blocks_join(self):
        result = self.export([cx(4, "function_call_output", call_id="a"),
                              cx(1, "function_call", call_id="a", name="Read")])
        self.assertEqual(result["clock"]["correlation_status"], "blocked_record_order_regression")
        self.assertEqual(result["events"][0]["duration_ms"], 3000)
        self.assertEqual(result["events"][0]["evidence"], "inferred")

    def test_forward_gap_is_not_reasoning_or_verified_clock_continuity(self):
        result = self.export([event(0, "task_started"), event(86400, "task_complete")])
        self.assertEqual(result["clock"]["correlation_status"], "requires_clock_validation")
        self.assertFalse(result["clock"]["monotonic_available"])
        self.assertEqual([e["category"] for e in result["events"]], ["task_window", "unattributed"])

    def test_out_of_range_nonfinite_and_boolean_metadata_never_export_invalid_numbers(self):
        for bad in [1e300, float("inf"), float("nan"), True]:
            record = wait_metadata()
            record["payload"]["started_at_ms"] = bad
            item = self.export([record])["events"][0]
            self.assertIsNone(item["start_unix_ms"])
            self.assertIsNone(item["duration_ms"])
            self.assertEqual(item["evidence"], "unknown")
            json.dumps(item, allow_nan=False)

    def test_zero_duration_is_usable_and_duplicate_calls_do_not_duplicate_spans(self):
        start = cx(1, "function_call", call_id="a", name="Read")
        end = cx(1, "function_call_output", call_id="a")
        items = self.export([start, start, end, end])["events"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["duration_ms"], 0)
        self.assertEqual(items[0]["timing_quality"], "usable")

    def test_unusable_native_metadata_cannot_hide_unknown_task_time(self):
        record = wait_metadata()
        record["payload"].update(started_at_ms=-1e30, completed_at_ms=1e30)
        result = self.export([event(0, "task_started"), record, event(5, "task_complete")])
        unknown = [e for e in result["events"] if e["category"] == "unattributed"]
        self.assertEqual(sum(e["duration_ms"] for e in unknown), 5000)
        self.assertEqual(result["events"][0]["timing_quality"], "out_of_range_timestamp")

    def test_unmatched_boundaries_and_invalid_ids_have_fixed_diagnostics(self):
        result = self.export([event(1, "task_complete"),
                              cx(2, "function_call", call_id="", name="Read")])
        self.assertEqual(result["diagnostics"], [{"code": "invalid_call_id", "count": 1},
                                                {"code": "unmatched_task_end", "count": 1}])

    def test_conflicting_replays_do_not_export_measured_call_duration(self):
        result = self.export([cx(1, "function_call", call_id="a", name="wait_agent"),
                              cx(2, "function_call", call_id="a", name="wait_agent"),
                              cx(3, "function_call_output", call_id="a")])
        self.assertTrue(all(e["timing_quality"] == "conflicting_records" and
                            e["duration_ms"] is None for e in result["events"]))

    def test_declared_windows_and_waits_never_become_verified(self):
        result = self.export([window("w", 0, 5), span("s", 1, 4, "agent_wait")])
        waits = [e for e in result["events"] if e["category"] == "agent_wait"]
        self.assertEqual(waits[0]["evidence"], "caller_declared")
        self.assertEqual(sum(e["duration_ms"] for e in result["events"] if e["category"] == "unattributed"), 2000)

    def test_unknown_residuals_clip_activity_and_union_overlapping_task_windows(self):
        records = [window("w1", 0, 5), window("w2", 3, 7), window("w3", 10, 12),
                   span("a", -2, 1), span("b", 2, 4), span("c", 6, 11)]
        result = self.export(records)
        gaps = [e for e in result["events"] if e["category"] == "unattributed"]
        self.assertEqual([(e["start_unix_ms"] - EPOCH, e["end_unix_ms"] - EPOCH) for e in gaps],
                         [(1000, 2000), (4000, 6000), (11000, 12000)])
        self.assertEqual(sum(e["duration_ms"] for e in gaps),
                         profile_file(self.path)["unattributed_within_completed_tasks_ms"])

    def test_claude_reported_timing_has_provenance_without_thinking_text(self):
        result = self.export([
            {"type": "user", "timestamp": ts(0), "uuid": "private", "message": {"content": "SECRET_PROMPT"}},
            {"type": "assistant", "timestamp": ts(1), "thinkingDurationMs": 500,
             "message": {"content": [{"type": "thinking", "thinking": "SECRET_CODE"}], "stop_reason": "end_turn"}}])
        thinking = next(e for e in result["events"] if e["category"] == "reasoning")
        self.assertEqual(thinking["duration_ms"], 500)
        self.assertEqual(thinking["evidence"], "verified_metadata")
        self.assertNotIn("SECRET", json.dumps(result))

    def test_unknown_missing_and_reversed_task_boundaries_remain_visible(self):
        fixtures = [[event(0, "task_started")],
                    [dict(event(0, "task_started"), timestamp=None), event(3, "task_complete")],
                    [event(3, "task_started"), event(1, "task_complete")]]
        for records in fixtures:
            result = self.export(records)
            item = next(e for e in result["events"] if e["category"] == "task_window")
            self.assertEqual(item["evidence"], "unknown")
            self.assertIsNone(item["duration_ms"])

    def test_unknown_only_timing_is_exportable_with_explicit_clock_status(self):
        result = self.export([event(4, "item_completed", item={"type": "Reasoning"})])
        self.assertEqual(result["clock"]["correlation_status"], "no_usable_timestamps")
        self.assertEqual(result["events"][0]["evidence"], "unknown")

    def test_no_prompts_code_secrets_raw_args_contents_ids_paths_or_hashes(self):
        secret = "PRIVATE_SENTINEL_0987"
        records = [dict(cx(1, "function_call", call_id=secret, name=secret,
                           arguments=json.dumps({"command": secret, "token": secret, "path": secret})),
                        session_id=secret, cwd=secret, message=secret),
                   cx(3, "function_call_output", call_id=secret, output=secret)]
        self.write(records)
        before = self.path.read_bytes()
        result = self.cli("timeline", str(self.path))
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["schema"], SCHEMA)
        self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertNotIn(str(self.path), result.stdout)
        self.assertNotIn("sha256", result.stdout)
        self.assertEqual(output["inputs"][0]["events"][0]["id"], "e1")
        self.assertEqual(self.path.read_bytes(), before)

    def test_cli_is_opt_in_and_keeps_inputs_separate(self):
        self.write(wait_records())
        original = self.cli("profile", str(self.path), "--json")
        self.assertEqual(json.loads(original.stdout)["schema"], "orqlet.profile.v1")
        self.assertNotIn("events", json.loads(original.stdout)["inputs"][0])
        result = self.cli("timeline", str(self.path), str(self.path))
        parsed = json.loads(result.stdout)
        self.assertEqual(len(parsed["inputs"]), 2)
        self.assertEqual(parsed["inputs"][0], parsed["inputs"][1])

    def test_limits_and_malformed_later_input_fail_without_partial_output(self):
        self.write(wait_records())
        for kwargs in [{"max_events": 1}, {"max_records": 1}, {"max_bytes": 1}, {"max_line_bytes": 1}]:
            with self.assertRaises(InputError):
                timeline_file(self.path, **kwargs)
        bad = Path(self.temp.name) / "PRIVATE_BAD.jsonl"
        bad.write_text("PRIVATE_INVALID_LOG\n")
        for extra in [[str(bad)], ["--max-events", "1"]]:
            result = self.cli("timeline", str(self.path), *extra)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertNotIn("PRIVATE", result.stderr)

    def test_bad_cli_arguments_and_unknown_formats_fail_closed(self):
        self.write([span("a", 0, 1)])
        for extra in [["--format", "PRIVATE"], ["--max-events", "0"], ["--json"]]:
            result = self.cli("timeline", str(self.path), *extra)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertNotIn("PRIVATE", result.stderr)
        self.path.write_text('{"schema":"orqlet.timeline.v1"}\n')
        with self.assertRaisesRegex(InputError, "unsupported_or_empty_log"):
            timeline_file(self.path)


if __name__ == "__main__":
    unittest.main()
