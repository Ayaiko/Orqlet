import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from orqlet.cli import main
from orqlet.correlation import correlate
from orqlet.profile import InputError
from orqlet.resources import project_samples
from orqlet.timeline import TimelineTrace
from test_profile import cx, event
from test_resources import native
from test_timeline import EPOCH


def capture_fixture():
    samples = project_samples([native(i, read_duration_ms=0) for i in range(5)])
    return {"schema": "orqlet.resources.v1", "capture_id": "1" * 32,
            "backend": "darwin_mach_dispatch_v1", "os_version": "26.6", "sdk_version": "26.5",
            "requested_duration_s": 4, "sample_interval_ms": 1000,
            "actual_elapsed_ms": 4000, "preparation_elapsed_ms": 1,
            "clock_status": "continuous_observed", "samples": samples,
            "collector": {"python_cpu_ms": 1, "native_cpu_ms": 1,
                          "native_peak_rss_bytes": 1000000,
                          "python_process_lifetime_peak_rss_bytes": 2000000,
                          "wakeups": None, "physical_footprint_bytes": None}}


def timeline_fixture():
    trace = TimelineTrace()
    records = [event(0, "task_started"),
               cx(1, "function_call", call_id="PRIVATE_ID", name="wait_agent", arguments='{"secret":"PRIVATE"}'),
               event(3, "item_completed", started_at_ms=EPOCH + 1000, completed_at_ms=EPOCH + 3000,
                     item={"type": "CollabAgentToolCall", "id": "PRIVATE", "tool": "wait"}),
               cx(3, "function_call_output", call_id="PRIVATE_ID", output="PRIVATE"),
               event(3.5, "item_completed", item={"type": "Reasoning", "raw_content": "PRIVATE"}),
               event(4, "task_complete")]
    for i, record in enumerate(records, 1):
        trace.consume(record, i)
    return trace.export()


def alignment_fixture():
    return {"schema": "orqlet.alignment.v1", "capture_id": "1" * 32,
            "capture_sha256": "a" * 64, "timeline_sha256": "b" * 64, "input_index": 1,
            "uncertainty_ms": 0, "anchors": [
                {"host_sample_id": "s1", "source_unix_ms": EPOCH},
                {"host_sample_id": "s5", "source_unix_ms": EPOCH + 4000}]}


class CorrelationTests(unittest.TestCase):
    def setUp(self):
        self.capture = capture_fixture()
        self.timeline = timeline_fixture()
        self.alignment = alignment_fixture()

    def join(self):
        return correlate(self.capture, self.timeline, self.alignment, "a" * 64, "b" * 64)

    def test_real_timeline_adapter_preserves_overlapping_wait_and_unknown_evidence(self):
        result = self.join()
        self.assertEqual(result["metadata_coverage_ms"], 4000)
        self.assertEqual(sum(r["activity_union_ms"] for r in result["intervals"]), 2000)
        self.assertEqual(result["untimed_events"], 1)
        for level in ["verified_metadata", "inferred"]:
            total = sum(e["union_ms"] for r in result["intervals"] for e in r["evidence_unions"]
                        if e["category"] == "agent_wait" and e["evidence"] == level)
            self.assertEqual(total, 2000)
        self.assertEqual(result["alignment_evidence"], "caller_supplied_anchors")
        self.assertIn("not causation", result["interpretation"])
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_clock_status_alone_never_approves_alignment(self):
        self.alignment = None
        with self.assertRaisesRegex(InputError, "clock_alignment_required"):
            self.join()

    def test_anchor_identity_binds_capture_metadata_timeline_and_selected_input(self):
        for key, value in [("capture_id", "2" * 32), ("capture_sha256", "c" * 64),
                           ("timeline_sha256", "d" * 64), ("input_index", 2)]:
            original = self.alignment[key]
            self.alignment[key] = value
            with self.assertRaisesRegex(InputError, "clock_alignment_identity_mismatch"):
                self.join()
            self.alignment[key] = original

    def test_caller_anchor_cannot_override_blocked_or_unknown_source_clock(self):
        for status in ["blocked_record_order_regression", "no_usable_timestamps"]:
            self.timeline["clock"]["correlation_status"] = status
            with self.assertRaisesRegex(InputError, "timeline_clock_not_validated"):
                self.join()
        self.timeline["clock"]["correlation_status"] = "requires_clock_validation"
        self.timeline["clock"]["record_time_regressions"] = 1
        with self.assertRaisesRegex(InputError, "timeline_clock_not_validated"):
            self.join()

    def test_clock_discontinuity_tampered_sample_ids_and_elapsed_are_rejected(self):
        for change in [lambda c: c.update(clock_status="discontinuous"),
                       lambda c: c["samples"][2].update(unix_ms=EPOCH + 4000),
                       lambda c: c["samples"][2].update(id="s1"),
                       lambda c: c.update(actual_elapsed_ms=5000),
                       lambda c: c["samples"][2].update(interval_ms=1)]:
            original = copy.deepcopy(self.capture)
            change(self.capture)
            with self.assertRaisesRegex(InputError, "capture_clock_not_validated"):
                self.join()
            self.capture = original

    def test_out_of_order_duplicate_missing_and_inconsistent_anchors_fail(self):
        original = copy.deepcopy(self.alignment)
        variants = [[original["anchors"][1], original["anchors"][0]],
                    [original["anchors"][0]] * 2,
                    [{"host_sample_id": "s99", "source_unix_ms": EPOCH}, original["anchors"][1]],
                    [original["anchors"][0], {"host_sample_id": "s5", "source_unix_ms": EPOCH + 3000}]]
        for anchors in variants:
            self.alignment["anchors"] = anchors
            with self.assertRaises(InputError):
                self.join()

    def test_uncertainty_cannot_understate_capture_read_or_clock_uncertainty(self):
        self.capture["samples"][1]["read_duration_ms"] = 0.2
        with self.assertRaisesRegex(InputError, "clock_uncertainty_understated"):
            self.join()

    def test_uncertain_boundaries_are_trimmed_not_reported_as_exact_coverage(self):
        self.alignment["uncertainty_ms"] = 10
        result = self.join()
        self.assertEqual(result["alignment_uncertainty_ms"], 10)
        self.assertEqual(sum(r["aligned_window_ms"] for r in result["intervals"]), 3980)
        self.assertEqual(sum(r["activity_union_ms"] for r in result["intervals"]), 1980)
        self.assertEqual(result["metadata_coverage_ms"], 3940)
        self.assertEqual(sum(r["missing_metadata_ms"] for r in result["intervals"]), 40)

    def test_no_actual_overlap_never_creates_retrospective_correlation(self):
        for event in self.timeline["events"]:
            if event["start_unix_ms"] is not None:
                event["start_unix_ms"] += 1000000
            if event["end_unix_ms"] is not None:
                event["end_unix_ms"] += 1000000
        with self.assertRaisesRegex(InputError, "no_correlatable_metadata_coverage"):
            self.join()

    def test_missed_interval_has_no_interpolated_coverage_or_pressure_duration(self):
        self.capture["samples"][2]["timing_quality"] = "missed_interval"
        result = self.join()
        interval = result["intervals"][1]
        self.assertEqual(interval["aligned_window_ms"], 0)
        self.assertEqual(interval["metadata_coverage_ms"], 0)
        self.assertEqual(interval["unaligned_or_unusable_ms"], 1000)
        self.assertNotIn("pressure_duration_ms", json.dumps(result))

    def test_false_usable_duration_is_rejected(self):
        self.timeline["events"][0]["duration_ms"] = 60000
        with self.assertRaisesRegex(InputError, "inconsistent_timeline_timing"):
            self.join()

    def test_sample_cli_private_output_and_existing_file_fails_before_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "PRIVATE_CAPTURE.json"
            for expected in [0, 2]:
                out, err = io.StringIO(), io.StringIO()
                with patch("orqlet.resources.capture", return_value=self.capture) as capture, \
                     redirect_stdout(out), redirect_stderr(err):
                    self.assertEqual(main(["sample", "--duration", "4", "--output", str(path)]), expected)
                self.assertEqual(out.getvalue(), "")
                self.assertNotIn("PRIVATE", err.getvalue())
                self.assertEqual(capture.call_count, 1 if expected == 0 else 0)
            self.assertEqual(json.loads(path.read_text()), self.capture)

    def test_cli_requires_alignment_and_cancel_returns_130_without_output(self):
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as error:
                main(["correlate", "PRIVATE", "PRIVATE"])
            self.assertEqual(error.exception.code, 2)
        self.assertEqual(out.getvalue(), "")
        self.assertNotIn("PRIVATE", err.getvalue())
        with patch("orqlet.resources.capture", side_effect=KeyboardInterrupt()), \
             redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            self.assertEqual(main(["sample", "--duration", "1"]), 130)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("capture_cancelled", err.getvalue())


if __name__ == "__main__":
    unittest.main()
