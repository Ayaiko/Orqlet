"""Finite macOS host capture. No process discovery, global configuration or network."""
import json
import os
from pathlib import Path
import platform
import re
import selectors
import subprocess
import tempfile
import time
import uuid

from .profile import InputError, number
from .timeline import safe_time


RESOURCE_SCHEMA = "orqlet.resources.v1"
MAX_OUTPUT = 1024 * 1024
CLOCK_TOLERANCE_MS = 250
RAW_FIELDS = {"schema", "unix_ms", "monotonic_ms", "read_duration_ms", "cpu_ticks",
              "cpu_quality", "page_size_bytes", "swap_in_pages", "swap_out_pages",
              "vm_quality", "swap_used_bytes", "swap_quality", "pressure",
              "pressure_quality", "pressure_event_monotonic_ms"}
READ_QUALITIES = {"available", "denied", "unavailable"}


def integer(value, maximum=2**64 - 1):
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= maximum


def validate_native(row):
    if not isinstance(row, dict) or set(row) != RAW_FIELDS or row["schema"] != "orqlet.native-sample.v1":
        raise InputError("invalid_native_sample")
    if (safe_time(row["unix_ms"]) is None or not number(row["monotonic_ms"]) or
            not 0 <= row["monotonic_ms"] <= 2**53 or not number(row["read_duration_ms"]) or
            not 0 <= row["read_duration_ms"] <= 10000):
        raise InputError("invalid_native_clock")
    for key in ("cpu_quality", "vm_quality", "swap_quality"):
        if not isinstance(row[key], str) or row[key] not in READ_QUALITIES:
            raise InputError("invalid_native_quality")
    ticks = row["cpu_ticks"]
    if ticks is not None and (not isinstance(ticks, list) or len(ticks) != 4 or
                              not all(integer(v, 2**32 - 1) for v in ticks)):
        raise InputError("invalid_native_counter")
    for key in ("page_size_bytes", "swap_in_pages", "swap_out_pages", "swap_used_bytes"):
        if row[key] is not None and not integer(row[key]):
            raise InputError("invalid_native_counter")
    page = row["page_size_bytes"]
    if page is not None and (page < 4096 or page > 65536 or page & (page - 1)):
        raise InputError("invalid_native_page_size")
    state, quality = row["pressure"], row["pressure_quality"]
    if not isinstance(state, str) or not isinstance(quality, str) or state not in {"unknown", "normal", "warning", "critical"} or quality not in {
            "awaiting_event", "event_observed", "coalesced_events", "unavailable"}:
        raise InputError("invalid_native_pressure")
    observed = row["pressure_event_monotonic_ms"]
    if quality in {"event_observed", "coalesced_events"}:
        if ((quality == "event_observed" and state == "unknown") or
                (quality == "coalesced_events" and state != "unknown") or
                not number(observed) or not 0 <= observed <= row["monotonic_ms"]):
            raise InputError("invalid_native_pressure")
    elif state != "unknown" or observed is not None:
        raise InputError("invalid_native_pressure")


def project_samples(rows):
    """Counter math has one owner; unknown readings never become zero."""
    samples = []
    previous = None
    for index, row in enumerate(rows):
        validate_native(row)
        interval = row["monotonic_ms"] - previous["monotonic_ms"] if previous else None
        if previous is None:
            clock = "baseline"
        elif (interval <= 0 or abs((row["unix_ms"] - previous["unix_ms"]) - interval) > CLOCK_TOLERANCE_MS or
              abs((row["unix_ms"] - rows[0]["unix_ms"]) -
                  (row["monotonic_ms"] - rows[0]["monotonic_ms"])) > CLOCK_TOLERANCE_MS):
            clock = "discontinuous"
        else:
            clock = "continuous_observed"
        timing = ("stale_read" if row["read_duration_ms"] > CLOCK_TOLERANCE_MS else
                  "missed_interval" if interval is not None and interval > 1500 else
                  "baseline" if previous is None else "available")
        cpu_value = in_bytes = out_bytes = None
        cpu_quality = delta_quality = "baseline"
        if previous:
            if clock == "discontinuous":
                cpu_quality = delta_quality = "clock_discontinuity"
            elif timing != "available" or previous["read_duration_ms"] > CLOCK_TOLERANCE_MS:
                cpu_quality = delta_quality = "stale_interval"
            else:
                before, after = previous["cpu_ticks"], row["cpu_ticks"]
                if (before is None or after is None or previous["cpu_quality"] != "available" or
                        row["cpu_quality"] != "available"):
                    cpu_quality = "denied" if "denied" in {row["cpu_quality"], previous["cpu_quality"]} else "unavailable"
                elif any(b < a for a, b in zip(before, after)):
                    cpu_quality = "counter_reset"
                else:
                    delta = [b - a for a, b in zip(before, after)]
                    total = sum(delta)
                    cpu_quality = "available" if total else "no_counter_progress"
                    cpu_value = 100.0 * (total - delta[2]) / total if total else None
                page = row["page_size_bytes"]
                keys = ("swap_in_pages", "swap_out_pages")
                if (page is None or any(previous[key] is None or row[key] is None for key in keys) or
                        row["vm_quality"] != "available" or previous["vm_quality"] != "available"):
                    delta_quality = "denied" if "denied" in {row["vm_quality"], previous["vm_quality"]} else "unavailable"
                elif page != previous["page_size_bytes"]:
                    delta_quality = "page_size_changed"
                elif any(row[key] < previous[key] for key in keys):
                    delta_quality = "counter_reset"
                else:
                    delta_quality = "available"
                    in_bytes, out_bytes = [(row[key] - previous[key]) * page for key in keys]
        samples.append({
            "id": "s%d" % (index + 1), "unix_ms": row["unix_ms"],
            "monotonic_ms": row["monotonic_ms"], "interval_ms": interval if interval is not None and interval > 0 else None,
            "read_duration_ms": row["read_duration_ms"], "clock_quality": clock,
            "timing_quality": timing, "page_size_bytes": row["page_size_bytes"],
            "cpu": {"busy_percent": cpu_value, "quality": cpu_quality,
                    "read_quality": row["cpu_quality"]},
            "swap": {"used_bytes": row["swap_used_bytes"] if row["swap_quality"] == "available" else None,
                     "allocation_quality": row["swap_quality"], "in_bytes": in_bytes,
                     "out_bytes": out_bytes, "delta_quality": delta_quality,
                     "counter_read_quality": row["vm_quality"]},
            "pressure": {"state": row["pressure"], "quality": row["pressure_quality"],
                         "last_event_monotonic_ms": row["pressure_event_monotonic_ms"]},
        })
        previous = row
    return samples


def stop_owned(child):
    if child.poll() is None:
        try:
            child.terminate()
        except ProcessLookupError:
            pass  # child exited between poll and terminate; still reap it
        try:
            child.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                child.kill()
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                raise InputError("owned_helper_cleanup_timeout") from None


def read_helper(binary, duration):
    """Bound bytes, records, line size and deadline before parsing native stdout."""
    child = subprocess.Popen([str(binary), str(duration)], stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             env={"PATH": "/usr/bin:/bin"})
    data = bytearray()
    try:
        deadline = time.monotonic() + duration + 3
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise InputError("capture_timeout")
                if not selector.select(min(remaining, 0.2)):
                    continue
                chunk = os.read(child.stdout.fileno(), 8192)
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > MAX_OUTPUT or data.count(b"\n") > duration + 2:
                    raise InputError("native_output_limit_exceeded")
        if child.wait(timeout=max(0.01, deadline - time.monotonic())) != 0:
            raise InputError("native_capture_failed")
    except subprocess.TimeoutExpired:
        raise InputError("capture_timeout") from None
    finally:
        stop_owned(child)
        child.stdout.close()
    try:
        lines = data.splitlines()
        if any(len(line) > 4096 for line in lines):
            raise InputError("native_line_limit_exceeded")
        values = [json.loads(line) for line in lines]
        if len(values) != duration + 2:
            raise InputError("incomplete_capture")
        rows, summary = values[:-1], values[-1]
        if (not isinstance(summary, dict) or set(summary) != {
                "schema", "cpu_user_ms", "cpu_system_ms", "peak_rss_bytes"} or
                summary["schema"] != "orqlet.native-summary.v1" or
                not all(number(summary[key]) and summary[key] >= 0 for key in ("cpu_user_ms", "cpu_system_ms")) or
                not integer(summary["peak_rss_bytes"])):
            raise InputError("invalid_native_summary")
        return rows, summary
    except InputError:
        raise
    except (ValueError, UnicodeError, RecursionError, TypeError):
        raise InputError("invalid_native_output") from None


def select_sdk():
    """Inspect only Apple's selected SDK directory; never change SDKROOT globally."""
    try:
        result = subprocess.run(["/usr/bin/xcrun", "--show-sdk-path"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, timeout=5,
                                env={"PATH": "/usr/bin:/bin"})
        if result.returncode:
            raise InputError("native_sdk_unavailable")
        default = Path(result.stdout.strip())
        if not default.is_absolute() or not default.name.endswith(".sdk"):
            raise InputError("native_sdk_unavailable")
        default = default.resolve()
        if not default.is_dir():
            raise InputError("native_sdk_unavailable")
        host_major = platform.mac_ver()[0].split(".")[0]
        candidates = []
        for entry in default.parent.iterdir():
            resolved = entry.resolve()
            match = re.fullmatch(r"MacOSX([0-9]+(?:\.[0-9]+)*)\.sdk", resolved.name)
            if match and match.group(1).split(".")[0] == host_major and resolved.is_dir():
                candidates.append((tuple(int(part) for part in match.group(1).split(".")), resolved))
        selected = max(candidates)[1] if candidates else default
        match = re.fullmatch(r"MacOSX([0-9]+(?:\.[0-9]+)*)\.sdk", selected.name)
        return selected, match.group(1) if match else "unknown"
    except (OSError, subprocess.TimeoutExpired):
        raise InputError("native_sdk_unavailable") from None


def capture(duration=60):
    if not integer(duration, 300) or duration < 1:
        raise InputError("invalid_capture_duration")
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise InputError("unsupported_host")
    import resource  # macOS-only capture dependency; offline commands remain portable.
    prepared = time.monotonic()
    try:
        sdk, sdk_version = select_sdk()
        with tempfile.TemporaryDirectory(prefix="orqlet-native-") as directory:
            binary = Path(directory) / "sampler"
            source = Path(__file__).parent / "native" / "macos_sampler.c"
            try:
                compiled = subprocess.run(["/usr/bin/clang", "-std=c11", "-O1", "-Wall", "-Wextra",
                                           "-Werror", "-isysroot", str(sdk), str(source), "-o", str(binary)],
                                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL, timeout=20,
                                          env={"PATH": "/usr/bin:/bin"})
            except subprocess.TimeoutExpired:
                raise InputError("native_preparation_timeout") from None
            if compiled.returncode:
                raise InputError("native_compiler_or_sdk_unavailable")
            preparation_ms = (time.monotonic() - prepared) * 1000
            usage_before = resource.getrusage(resource.RUSAGE_SELF)
            rows, summary = read_helper(binary, duration)
            samples = project_samples(rows)
            usage_after = resource.getrusage(resource.RUSAGE_SELF)
    except OSError:
        raise InputError("native_capture_unavailable") from None
    os_version = platform.mac_ver()[0]
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){0,2}", os_version):
        os_version = "unknown"
    result = {
        "schema": RESOURCE_SCHEMA, "capture_id": uuid.uuid4().hex,
        "backend": "darwin_mach_dispatch_v1", "os_version": os_version,
        "sdk_version": sdk_version,
        "requested_duration_s": duration, "sample_interval_ms": 1000,
        "actual_elapsed_ms": max(0, samples[-1]["monotonic_ms"] - samples[0]["monotonic_ms"]),
        "preparation_elapsed_ms": preparation_ms,
        "clock_status": "discontinuous" if any(s["clock_quality"] == "discontinuous" for s in samples) else "continuous_observed",
        "samples": samples,
        "collector": {
            "python_cpu_ms": ((usage_after.ru_utime + usage_after.ru_stime) -
                              (usage_before.ru_utime + usage_before.ru_stime)) * 1000,
            "native_cpu_ms": summary["cpu_user_ms"] + summary["cpu_system_ms"],
            "native_peak_rss_bytes": summary["peak_rss_bytes"],
            "python_process_lifetime_peak_rss_bytes": usage_after.ru_maxrss,
            "wakeups": None, "physical_footprint_bytes": None,
        },
    }
    encode_report(result)
    return result


def encode_report(result):
    encoded = json.dumps(result, ensure_ascii=True, separators=(",", ":"), allow_nan=False) + "\n"
    if len(encoded.encode("ascii")) > MAX_OUTPUT:
        raise InputError("report_limit_exceeded")
    return encoded


def write_private(path, text):
    temporary = None
    try:
        target = Path(path)
        with tempfile.NamedTemporaryFile(mode="w", dir=target.parent,
                                         prefix=".orqlet-", delete=False) as stream:
            temporary = stream.name
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)  # atomic, exclusive publication of a complete 0600 file
    except OSError:
        raise InputError("cannot_create_private_output") from None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            except OSError:
                raise InputError("private_output_cleanup_failed") from None
