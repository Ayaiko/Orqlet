import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from orqlet.profile import InputError
from orqlet.resources import capture, encode_report, project_samples, read_helper, select_sdk, stop_owned, write_private


def native(index=0, **changes):
    row = {"schema": "orqlet.native-sample.v1", "unix_ms": 1767225600000 + index * 1000,
           "monotonic_ms": 10000 + index * 1000, "read_duration_ms": 0.2,
           "cpu_ticks": [100 + index * 20, 200 + index * 10, 300 + index * 70, 0],
           "cpu_quality": "available", "page_size_bytes": 16384,
           "swap_in_pages": 100 + index * 2, "swap_out_pages": 50 + index,
           "vm_quality": "available", "swap_used_bytes": 1024 * 1024,
           "swap_quality": "available", "pressure": "unknown",
           "pressure_quality": "awaiting_event", "pressure_event_monotonic_ms": None}
    return dict(row, **changes)


SUMMARY = {"schema": "orqlet.native-summary.v1", "cpu_user_ms": 0.2,
           "cpu_system_ms": 0.3, "peak_rss_bytes": 1000000}


class ResourceTests(unittest.TestCase):
    def test_first_deltas_are_unknown_and_units_use_actual_page_size(self):
        first, second = project_samples([native(), native(1)])
        self.assertIsNone(first["cpu"]["busy_percent"])
        self.assertEqual(first["swap"]["delta_quality"], "baseline")
        self.assertIsNone(first["swap"]["in_bytes"])
        self.assertEqual(second["cpu"]["busy_percent"], 30)
        self.assertEqual(second["swap"]["in_bytes"], 32768)
        self.assertEqual(second["swap"]["out_bytes"], 16384)
        self.assertEqual(second["interval_ms"], 1000)
        smaller = project_samples([native(page_size_bytes=4096), native(1, page_size_bytes=4096)])
        self.assertEqual(smaller[1]["swap"]["in_bytes"], 8192)

    def test_pressure_starts_unknown_and_only_native_events_supply_state(self):
        first, second = project_samples([native(), native(1, pressure="warning",
            pressure_quality="event_observed", pressure_event_monotonic_ms=10500)])
        self.assertEqual(first["pressure"]["state"], "unknown")
        self.assertEqual(second["pressure"]["state"], "warning")
        self.assertEqual(second["pressure"]["last_event_monotonic_ms"], 10500)
        with self.assertRaisesRegex(InputError, "invalid_native_pressure"):
            project_samples([native(pressure="normal")])

    def test_coalesced_pressure_flags_cannot_claim_the_latest_state(self):
        sample = project_samples([native(1, pressure_quality="coalesced_events",
                                         pressure_event_monotonic_ms=10500)])[0]
        self.assertEqual(sample["pressure"]["state"], "unknown")
        self.assertEqual(sample["pressure"]["quality"], "coalesced_events")

    def test_permission_denied_and_missing_counters_never_become_zero(self):
        denied = native(1, cpu_ticks=None, cpu_quality="denied", swap_in_pages=None,
                        swap_out_pages=None, vm_quality="denied", swap_used_bytes=None,
                        swap_quality="denied")
        result = project_samples([native(), denied, native(2)])
        for row in result[1:]:
            self.assertEqual(row["cpu"]["quality"], "denied")
            self.assertIsNone(row["cpu"]["busy_percent"])
            self.assertIsNone(row["swap"]["in_bytes"])
        self.assertIsNone(result[1]["swap"]["used_bytes"])
        baseline = project_samples([denied])[0]
        self.assertEqual(baseline["cpu"]["read_quality"], "denied")

    def test_counter_reset_page_change_and_no_progress_are_explicit(self):
        for change, quality in [({"swap_in_pages": 0}, "counter_reset"),
                                ({"page_size_bytes": 4096}, "page_size_changed")]:
            row = project_samples([native(), native(1, **change)])[1]
            self.assertEqual(row["swap"]["delta_quality"], quality)
            self.assertIsNone(row["swap"]["in_bytes"])
        for ticks, quality in [([0, 0, 0, 0], "counter_reset"), ([100, 200, 300, 0], "no_counter_progress")]:
            row = project_samples([native(), native(1, cpu_ticks=ticks)])[1]
            self.assertEqual(row["cpu"]["quality"], quality)
            self.assertIsNone(row["cpu"]["busy_percent"])

    def test_missed_and_slow_samples_do_not_interpolate_metrics(self):
        for rows in [[native(), native(2)], [native(read_duration_ms=300), native(1)],
                     [native(), native(1, read_duration_ms=300)]]:
            result = project_samples(rows)[1]
            self.assertEqual(result["cpu"]["quality"], "stale_interval")
            self.assertIsNone(result["swap"]["in_bytes"])

    def test_backward_forward_and_cumulative_clock_changes_block_deltas(self):
        for clock in [1767225599000, 1767225602000]:
            row = project_samples([native(), native(1, unix_ms=clock)])[1]
            self.assertEqual(row["clock_quality"], "discontinuous")
            self.assertIsNone(row["cpu"]["busy_percent"])
        rows = [native(i, unix_ms=1767225600000 + i * 1200) for i in range(3)]
        self.assertEqual(project_samples(rows)[2]["clock_quality"], "discontinuous")
        self.assertEqual(project_samples([native(), native(1, monotonic_ms=10000)])[1]["clock_quality"], "discontinuous")

    def test_protocol_rejects_private_fields_nonfinite_values_and_invalid_units(self):
        for changes in [{"prompt": "SECRET"}, {"cpu_quality": "SECRET"},
                        {"cpu_quality": {}}, {"pressure_quality": []},
                        {"monotonic_ms": float("nan")}, {"page_size_bytes": 123},
                        {"swap_used_bytes": True}, {"cpu_ticks": [1, 2, 3]},
                        {"pressure_event_monotonic_ms": 20000}]:
            with self.assertRaises(InputError) as error:
                project_samples([native(**changes)])
            self.assertNotIn("SECRET", str(error.exception))

    def test_read_helper_handles_valid_protocol_and_rejects_partial_or_extra_output(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "fixture"
            for values, error in [([native(), native(1), SUMMARY], None),
                                  ([native(), SUMMARY], "incomplete_capture"),
                                  ([native(), native(1), dict(SUMMARY, prompt="SECRET")], "invalid_native_summary"),
                                  ([native(), native(1), SUMMARY, SUMMARY], "native_output_limit_exceeded")]:
                text = "".join(json.dumps(v) + "\n" for v in values)
                script.write_text("#!" + sys.executable + "\nimport sys\nsys.stdout.write(" + repr(text) + ")\n")
                script.chmod(0o700)
                if error:
                    with self.assertRaisesRegex(InputError, error):
                        read_helper(script, 1)
                else:
                    rows, summary = read_helper(script, 1)
                    self.assertEqual(rows, values[:-1])
                    self.assertEqual(summary, SUMMARY)

    def test_timeout_and_keyboard_interrupt_stop_only_owned_child(self):
        for failure in [InputError("capture_timeout"), KeyboardInterrupt()]:
            read_fd, write_fd = os.pipe()
            child = Mock(stdout=os.fdopen(read_fd, "rb"))
            child.poll.return_value = None
            try:
                with patch("orqlet.resources.subprocess.Popen", return_value=child), \
                     patch("orqlet.resources.time.monotonic", side_effect=[0, 5] if isinstance(failure, InputError) else [0, 0]), \
                     patch("orqlet.resources.selectors.DefaultSelector") as selectors:
                    selectors.return_value.__enter__.return_value.select.side_effect = failure
                    with self.assertRaises(type(failure)):
                        read_helper("unused", 1)
                child.terminate.assert_called_once()
                child.wait.assert_called_once_with(timeout=1)
                self.assertTrue(child.stdout.closed)
            finally:
                os.close(write_fd)

    def test_stuck_owned_child_is_killed_after_terminate_timeout(self):
        child = Mock()
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired("owned", 1), -9]
        stop_owned(child)
        child.terminate.assert_called_once()
        child.kill.assert_called_once()

    def test_exit_race_during_cancel_is_reaped_without_changing_cancel_reason(self):
        child = Mock()
        child.poll.return_value = None
        child.terminate.side_effect = ProcessLookupError()
        stop_owned(child)
        child.wait.assert_called_once_with(timeout=1)
        child.kill.assert_not_called()

    def test_private_output_is_complete_0600_exclusive_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.json"
            write_private(path, "complete\n")
            self.assertEqual(path.read_text(), "complete\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with self.assertRaisesRegex(InputError, "cannot_create_private_output"):
                write_private(path, "replacement")
            self.assertEqual(path.read_text(), "complete\n")
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_limits_unsupported_host_and_compile_failure_fail_with_safe_codes(self):
        for bad in [0, 301, True, 1.5]:
            with self.assertRaisesRegex(InputError, "invalid_capture_duration"):
                capture(bad)
        with patch("orqlet.resources.platform.system", return_value="Linux"):
            with self.assertRaisesRegex(InputError, "unsupported_host"):
                capture(1)
        with patch("orqlet.resources.platform.system", return_value="Darwin"), \
             patch("orqlet.resources.platform.machine", return_value="arm64"), \
             patch("orqlet.resources.select_sdk", return_value=(Path("/fixture/sdk"), "26.5")), \
             patch("orqlet.resources.subprocess.run", return_value=Mock(returncode=1)):
            with self.assertRaisesRegex(InputError, "native_compiler_or_sdk_unavailable"):
                capture(1)
        with self.assertRaisesRegex(InputError, "report_limit_exceeded"):
            encode_report({"fixed": "x" * (1024 * 1024)})

    def test_sdk_selection_uses_existing_host_major_without_global_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            default, compatible = root / "MacOSX27.0.sdk", root / "MacOSX26.5.sdk"
            default.mkdir(); compatible.mkdir()
            with patch("orqlet.resources.platform.mac_ver", return_value=("26.6", (), "")), \
                 patch("orqlet.resources.subprocess.run", return_value=Mock(returncode=0, stdout=str(default))) as run:
                selected, version = select_sdk()
            self.assertEqual(selected, compatible.resolve())
            self.assertEqual(version, "26.5")
            self.assertNotIn("SDKROOT", run.call_args.kwargs["env"])


if __name__ == "__main__":
    unittest.main()
