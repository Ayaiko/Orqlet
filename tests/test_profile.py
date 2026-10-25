from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from orqlet.profile import InputError, profile_file, workload


def ts(seconds):
    return (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)).isoformat()


def span(key, start, end, kind="tool", **extra):
    return dict(schema="orqlet.trace.v1", type="span", id=key, category=kind,
                start=ts(start), end=ts(end), **extra)


def window(key, start, end):
    return dict(schema="orqlet.trace.v1", type="window", id=key, start=ts(start), end=ts(end))


def cx(seconds, kind, **payload):
    return dict(timestamp=ts(seconds), type="response_item", payload=dict(type=kind, **payload))


def event(seconds, kind, **payload):
    return dict(timestamp=ts(seconds), type="event_msg", payload=dict(type=kind, **payload))


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "private-session.jsonl"

    def write(self, records):
        self.path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))
        return self.path

    def profile(self, records, **kwargs):
        return profile_file(self.write(records), **kwargs)

    def test_parallel_calls_are_unioned_and_categories_are_not_summed(self):
        r = self.profile([window("w", 0, 20), span("a", 0, 10), span("b", 5, 15),
                          span("thinking", 10, 20, "reasoning")])
        self.assertEqual(r["category_union_ms"]["tool"], 15000)
        self.assertEqual(r["category_union_ms"]["reasoning"], 10000)
        self.assertEqual(r["observed_activity_union_ms"], 20000)
        self.assertEqual(r["overlap_ms"], 10000)
        self.assertEqual(r["unattributed_within_completed_tasks_ms"], 0)

    def test_user_gap_is_outside_task_windows_not_ai_work(self):
        r = self.profile([window("w1", 0, 10), span("a", 2, 5),
                          window("w2", 100, 110), span("b", 101, 102)])
        self.assertEqual(r["log_window_ms"], 110000)
        self.assertEqual(r["completed_task_windows_ms"], 20000)
        self.assertEqual(r["outside_completed_task_windows_ms"], 90000)
        self.assertEqual(r["unattributed_within_completed_tasks_ms"], 16000)
        self.assertIsNone(r["category_union_ms"]["reasoning"])

    def test_codex_pairs_calls_and_keeps_opaque_outcome_unknown(self):
        r = self.profile([event(0, "task_started", turn_id="turn"),
            cx(1, "function_call", call_id="c", name="functions.exec_command", arguments=json.dumps({"cmd": "git status"})),
            cx(3, "function_call_output", call_id="c", output="PASS exit_code=0"),
            event(5, "task_complete", turn_id="turn")])
        self.assertEqual(r["tool_calls"]["completed"], 1)
        self.assertEqual(r["category_union_ms"]["tool"], 2000)
        self.assertEqual(r["top_tools"][0]["workload_hint"], "git")
        self.assertEqual(r["top_tools"][0]["unknown_outcomes"], 1)
        self.assertEqual(r["top_tools"][0]["explicit_errors"], 0)
        self.assertEqual(r["unattributed_within_completed_tasks_ms"], 3000)

    def test_codex_metadata_reasoning_and_command_duration_have_provenance(self):
        start = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        r = self.profile([event(0, "task_started", turn_id="t"),
            event(2, "item_completed", started_at_ms=start + 500, completed_at_ms=start + 2000,
                  item={"type": "Reasoning", "id": "r", "raw_content": "DO_NOT_EXPORT"}),
            event(3, "item_completed", started_at_ms=start + 3000, completed_at_ms=start + 3000,
                  item={"type": "CommandExecution", "id": "cmd", "command": ["git", "status"], "duration": {"secs": 0, "nanos": 500000000}}),
            event(4, "task_complete", turn_id="t")])
        self.assertEqual(r["category_union_ms"]["reasoning"], 1500)
        self.assertEqual(r["category_union_ms"]["command"], 500)
        self.assertEqual(r["reported_command_workloads"][0]["workload_hint"], "git")
        self.assertEqual(r["timing_provenance"]["codex_item_metadata"], 2)
        self.assertNotIn("DO_NOT_EXPORT", json.dumps(r))

    def test_claude_ignores_blob_replays_and_labels_reported_thinking(self):
        call = {"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "node --test tests/a.mjs"}}
        r = self.profile([
            {"type": "user", "timestamp": ts(0), "uuid": "turn", "message": {"content": "private prompt"}},
            {"type": "assistant", "timestamp": ts(1), "uuid": "thinking", "thinkingDurationMs": 500, "message": {"content": [{"type": "thinking", "thinking": "private"}]}},
            {"type": "assistant", "timestamp": ts(1), "message": {"content": [call]}},
            {"type": "api-request-blob", "timestamp": ts(1), "message": {"content": [call]}},
            {"type": "user", "timestamp": ts(3), "message": {"content": [{"type": "tool_result", "tool_use_id": "a", "content": "λ", "is_error": True}]}},
            {"type": "system", "subtype": "stop_hook_summary", "timestamp": ts(4), "preventedContinuation": False}])
        self.assertEqual(r["tool_calls"]["started"], 1)
        self.assertEqual(r["completed_task_windows_ms"], 4000)
        self.assertEqual(r["category_union_ms"]["reasoning"], 500)
        self.assertEqual(r["top_tools"][0]["output_bytes"], 2)
        self.assertEqual(r["top_tools"][0]["explicit_errors"], 1)
        self.assertEqual(r["top_tools"][0]["workload_hint"], "test")
        self.assertIn("claude_reported_thinking_duration", r["timing_provenance"])

    def test_missing_calls_and_missing_sizes_do_not_become_zero_durations(self):
        r = self.profile([cx(1, "function_call", call_id="open", name="Read"),
                          cx(3, "function_call_output", call_id="orphan", output="secret")])
        self.assertEqual(r["tool_calls"]["open_or_invalid"], 1)
        self.assertEqual(r["tool_calls"]["orphan_outputs"], 1)
        self.assertIsNone(r["category_union_ms"]["tool"])
        self.assertIsNone(r["top_tools"][0]["output_bytes"])
        self.assertIsNone(r["repeat_signature_calls"])

    def test_reversed_clocks_are_explicit_and_never_negative(self):
        r = self.profile([cx(3, "function_call", call_id="a", name="Read"),
                          cx(1, "function_call_output", call_id="a", output="x")])
        self.assertEqual(r["warnings"]["invalid_call_timing"], 1)
        self.assertEqual(r["tool_calls"]["completed"], 0)
        self.assertIsNone(r["category_union_ms"]["tool"])

    def test_duplicate_records_and_out_of_order_pairing(self):
        start = cx(1, "function_call", call_id="a", name="Read", arguments='{"path":"private"}')
        end = cx(3, "function_call_output", call_id="a", output="x")
        r = self.profile([end, start, start, end])
        self.assertEqual(r["category_union_ms"]["tool"], 2000)
        self.assertEqual(r["tool_calls"]["started"], 1)
        self.assertEqual(r["warnings"]["duplicate_call_start"], 1)
        self.assertEqual(r["warnings"]["duplicate_call_end"], 1)

    def test_repeated_calls_and_large_outputs_are_leads_not_waste_claims(self):
        records = []
        for i in range(2):
            records += [cx(i * 4, "function_call", call_id=str(i), name="Bash", arguments='{"command":"rg needle source"}'),
                        cx(i * 4 + 1, "function_call_output", call_id=str(i), output="X" * 65536)]
        r = self.profile(records)
        self.assertEqual(r["repeat_signature_calls"], 1)
        self.assertEqual(r["large_output_calls_at_least_64kib"], 2)
        self.assertEqual(r["largest_outputs"][0]["output_bytes"], 131072)
        self.assertIn("not CPU or proof of waste", r["interpretation"])

    def test_conflicting_call_replays_do_not_manufacture_a_valid_duration(self):
        r = self.profile([cx(1, "function_call", call_id="a", name="Read"),
                          cx(2, "function_call", call_id="a", name="Read"),
                          cx(3, "function_call_output", call_id="a", output="x")])
        self.assertEqual(r["warnings"]["conflicting_call_start"], 1)
        self.assertEqual(r["tool_calls"]["completed"], 0)
        self.assertIsNone(r["category_union_ms"]["tool"])

    def test_open_task_does_not_claim_a_complete_active_window(self):
        r = self.profile([event(0, "task_started", turn_id="a"), cx(1, "function_call", call_id="a", name="Read")])
        self.assertEqual(r["open_task_windows"], 1)
        self.assertIsNone(r["completed_task_windows_ms"])
        self.assertIsNone(r["unattributed_within_completed_tasks_ms"])

    def test_secret_values_arbitrary_tool_names_and_paths_are_not_exported(self):
        secret = "SENTINEL_PRIVATE_TOKEN_AND_PATH"
        r = self.profile([cx(1, "function_call", call_id=secret, name=secret, arguments=json.dumps({"command": secret, "token": secret})),
                          cx(2, "function_call_output", call_id=secret, output=secret)])
        output = json.dumps(r)
        self.assertNotIn(secret, output)
        self.assertNotIn(str(self.path), output)
        self.assertEqual(r["top_tools"][0]["tool"], "other")

    def test_unknown_schema_malformed_records_and_limits_fail_closed(self):
        self.write([{"something": "secret"}])
        with self.assertRaisesRegex(InputError, "unsupported_or_empty_log"):
            profile_file(self.path)
        self.path.write_text('{"secret":INVALID}\n')
        with self.assertRaisesRegex(InputError, "line_1:invalid_record"):
            profile_file(self.path)
        self.write([span("a", 0, 1)])
        for kwargs in [{"max_bytes": 1}, {"max_line_bytes": 4}, {"max_records": 0}]:
            with self.assertRaises(InputError):
                profile_file(self.path, **kwargs)

    def test_naive_timestamp_and_negative_trace_are_rejected(self):
        for record in [dict(span("a", 0, 1), start="2026-01-01T00:00:00"), span("a", 3, 1)]:
            with self.assertRaisesRegex(InputError, "invalid_trace_timing"):
                self.profile([record])

    def test_command_hints_do_not_assign_compound_shell_to_git(self):
        self.assertEqual(workload("git status && rg value source"), "mixed")
        self.assertEqual(workload("git status\nnode --test a.mjs"), "mixed")
        self.assertEqual(workload("MANGROVE_NODE_TEST_CONCURRENCY=2 mise exec node@22 -- node scripts/verify_unit_ratchet.mjs"), "test")
        self.assertEqual(workload(["/bin/zsh", "-lc", "rg pattern src"]), "filesystem")

    def test_cli_end_to_end_json_and_text_without_install_or_external_services(self):
        self.write([window("w", 0, 4), span("a", 1, 3, tool="test", workload="test", output_bytes=128)])
        original_bytes = self.path.read_bytes()
        command = [sys.executable, "-m", "orqlet", "profile", str(self.path)]
        r = subprocess.run(command + ["--json"], capture_output=True, text=True, timeout=5)
        self.assertEqual(r.returncode, 0, r.stderr)
        report = json.loads(r.stdout)
        self.assertEqual(report["schema"], "orqlet.profile.v1")
        self.assertEqual(report["inputs"][0]["category_union_ms"]["tool"], 2000)
        self.assertNotIn(str(self.path), r.stdout)
        text = subprocess.run(command, capture_output=True, text=True, timeout=5)
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("Unattributed time is not reasoning", text.stdout)
        self.assertEqual(self.path.read_bytes(), original_bytes)

    def test_cli_never_prints_a_partial_success_report_or_bad_input(self):
        good = self.write([span("a", 0, 1)])
        bad = Path(self.temp.name) / "SECRET_FILENAME.jsonl"
        bad.write_text("SECRET_RAW_TRANSCRIPT\n")
        r = subprocess.run([sys.executable, "-m", "orqlet", "profile", str(good), str(bad), "--json"],
                           capture_output=True, text=True, timeout=5)
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stdout, "")
        self.assertNotIn("SECRET", r.stderr)
        self.assertIn("input_2:line_1:invalid_record", r.stderr)

    def test_separate_inputs_are_not_added_as_elapsed_time(self):
        path = self.write([window("w", 0, 10), span("a", 0, 5)])
        r = subprocess.run([sys.executable, "-m", "orqlet", "profile", str(path), str(path), "--json"],
                           capture_output=True, text=True, timeout=5)
        self.assertEqual(r.returncode, 0, r.stderr)
        report = json.loads(r.stdout)
        self.assertEqual(len(report["inputs"]), 2)
        self.assertNotIn("total_elapsed_ms", report)
        self.assertEqual([x["log_window_ms"] for x in report["inputs"]], [10000, 10000])

    def test_cli_argument_errors_do_not_echo_private_values(self):
        r = subprocess.run([sys.executable, "-m", "orqlet", "profile", "input", "--format", "SECRET_ARG"],
                           capture_output=True, text=True, timeout=5)
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("SECRET", r.stderr)
        self.assertEqual(r.stdout, "")


if __name__ == "__main__":
    unittest.main()
