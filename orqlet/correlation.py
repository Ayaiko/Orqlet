"""Offline metadata correlation gated by explicit, identity-bound clock anchors."""
from collections import defaultdict
import hashlib
import json
from pathlib import Path

from .profile import InputError, length, number
from .resources import CLOCK_TOLERANCE_MS, encode_report, integer


CORRELATION_SCHEMA = "orqlet.correlation.v1"
SCHEMA_ROOT = Path(__file__).resolve().parent.parent / "schemas"


def read_metadata(path, maximum, schema_file):
    try:
        from jsonschema import Draft202012Validator, ValidationError
    except ImportError:
        raise InputError("correlation_requires_jsonschema") from None

    def reject_constant(_value):
        raise InputError("invalid_metadata_number")

    def parse_number(token):
        if len(token) > 64:
            raise InputError("invalid_metadata_number")
        value = float(token) if any(c in token for c in ".eE") else int(token)
        if not number(value):
            raise InputError("invalid_metadata_number")
        return value

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise InputError("duplicate_metadata_key")
            result[key] = value
        return result

    try:
        path = Path(path)
        if not path.is_file():
            raise InputError("not_a_regular_metadata_file")
        with path.open("rb") as stream:
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise InputError("metadata_limit_exceeded")
        value = json.loads(data, parse_constant=reject_constant, parse_int=parse_number,
                           parse_float=parse_number, object_pairs_hook=unique_object)
        schema = json.loads((SCHEMA_ROOT / schema_file).read_text())
        Draft202012Validator(schema).validate(value)
        return value, hashlib.sha256(data).hexdigest()
    except InputError:
        raise
    except (OSError, ValueError, UnicodeError, RecursionError, ValidationError):
        raise InputError("invalid_metadata_contract") from None


def validate_capture_clock(capture):
    samples = capture["samples"]
    if capture["clock_status"] != "continuous_observed" or len(samples) != capture["requested_duration_s"] + 1:
        raise InputError("capture_clock_not_validated")
    first = samples[0]
    previous = None
    for index, sample in enumerate(samples):
        if (sample["id"] != "s%d" % (index + 1) or
                abs((sample["unix_ms"] - first["unix_ms"]) -
                    (sample["monotonic_ms"] - first["monotonic_ms"])) > CLOCK_TOLERANCE_MS):
            raise InputError("capture_clock_not_validated")
        if previous:
            interval = sample["monotonic_ms"] - previous["monotonic_ms"]
            if (interval <= 0 or sample["unix_ms"] <= previous["unix_ms"] or
                    sample["clock_quality"] != "continuous_observed" or
                    sample["interval_ms"] is None or abs(sample["interval_ms"] - interval) > 0.001):
                raise InputError("capture_clock_not_validated")
        elif sample["clock_quality"] != "baseline" or sample["interval_ms"] is not None:
            raise InputError("capture_clock_not_validated")
        previous = sample
    elapsed = samples[-1]["monotonic_ms"] - first["monotonic_ms"]
    if abs(elapsed - capture["actual_elapsed_ms"]) > 0.001:
        raise InputError("capture_clock_not_validated")


def anchor_mapping(capture, timeline, alignment, capture_digest, timeline_digest, input_index):
    if alignment is None:
        raise InputError("clock_alignment_required")
    if (alignment["capture_id"] != capture["capture_id"] or
            alignment["capture_sha256"] != capture_digest or
            alignment["timeline_sha256"] != timeline_digest or alignment["input_index"] != input_index):
        raise InputError("clock_alignment_identity_mismatch")
    if timeline["clock"]["correlation_status"] != "requires_clock_validation" or timeline["clock"]["record_time_regressions"]:
        raise InputError("timeline_clock_not_validated")
    validate_capture_clock(capture)
    samples = {sample["id"]: sample for sample in capture["samples"]}
    previous_source = previous_mono = None
    offsets = []
    anchor_samples = []
    for anchor in alignment["anchors"]:
        sample = samples.get(anchor["host_sample_id"])
        if (sample is None or sample["read_duration_ms"] > CLOCK_TOLERANCE_MS or
                sample["timing_quality"] not in {"baseline", "available"}):
            raise InputError("invalid_clock_anchor")
        source = anchor["source_unix_ms"]
        if previous_source is not None and (source <= previous_source or sample["monotonic_ms"] <= previous_mono):
            raise InputError("invalid_clock_anchor_order")
        offsets.append(sample["unix_ms"] - source)
        anchor_samples.append(sample)
        previous_source, previous_mono = source, sample["monotonic_ms"]
    uncertainty = alignment["uncertainty_ms"]
    origin = capture["samples"][0]
    known_uncertainty = max(sample["read_duration_ms"] +
                            abs((sample["unix_ms"] - origin["unix_ms"]) -
                                (sample["monotonic_ms"] - origin["monotonic_ms"]))
                            for sample in capture["samples"])
    if uncertainty < known_uncertainty:
        raise InputError("clock_uncertainty_understated")
    if len(offsets) < 2 or max(offsets) - min(offsets) > uncertainty * 2:
        raise InputError("inconsistent_clock_anchors")
    offset = (min(offsets) + max(offsets)) / 2
    start = anchor_samples[0]["unix_ms"] + uncertainty
    end = anchor_samples[-1]["unix_ms"] - uncertainty
    if end <= start:
        raise InputError("insufficient_anchor_coverage")
    return offset, uncertainty, start, end


def correlate(capture, timeline_input, alignment, capture_digest, timeline_digest, input_index=1):
    """Core accepts contract-validated data; file entrypoint below enforces schemas."""
    offset, uncertainty, start, end = anchor_mapping(
        capture, timeline_input, alignment, capture_digest, timeline_digest, input_index)
    groups = defaultdict(list)
    untimed = 0
    for event in timeline_input["events"]:
        if event["timing_quality"] != "usable":
            untimed += 1
            continue
        left, right = event["start_unix_ms"], event["end_unix_ms"]
        if left is None or right is None or right < left or event["duration_ms"] is None or abs(right - left - event["duration_ms"]) > 0.001:
            raise InputError("inconsistent_timeline_timing")
        if event["category"] == "task_window":
            continue
        left, right = max(start, left + offset + uncertainty), min(end, right + offset - uncertainty)
        if right > left:
            groups[(event["category"], event["evidence"], event["source"])].append((left, right))
    rows = []
    total_coverage = 0.0
    for previous, sample in zip(capture["samples"], capture["samples"][1:]):
        left, right = max(start, previous["unix_ms"]), min(end, sample["unix_ms"])
        usable = (sample["timing_quality"] == "available" and
                  previous["read_duration_ms"] <= CLOCK_TOLERANCE_MS)
        span = max(0, right - left) if usable else 0
        activity, covered, evidence = [], [], []
        if span:
            for (category, level, source), intervals in sorted(groups.items()):
                clipped = [(max(left, a), min(right, b)) for a, b in intervals if b > left and a < right]
                count = length(clipped)
                if count:
                    evidence.append({"category": category, "evidence": level, "source": source, "union_ms": count})
                    covered.extend(clipped)
                    if category != "unattributed":
                        activity.extend(clipped)
        coverage = length(covered)
        total_coverage += coverage
        rows.append({
            "sample_id": sample["id"], "interval_start_unix_ms": previous["unix_ms"],
            "interval_end_unix_ms": sample["unix_ms"], "aligned_window_ms": span,
            "unaligned_or_unusable_ms": max(0, sample["interval_ms"] - span),
            "metadata_coverage_ms": coverage, "missing_metadata_ms": max(0, span - coverage),
            "activity_union_ms": length(activity), "evidence_unions": evidence,
            "cpu_busy_percent": sample["cpu"]["busy_percent"], "cpu_quality": sample["cpu"]["quality"],
            "swap_in_bytes": sample["swap"]["in_bytes"], "swap_out_bytes": sample["swap"]["out_bytes"],
            "swap_delta_quality": sample["swap"]["delta_quality"],
            "pressure_state_at_sample": sample["pressure"]["state"],
            "pressure_evidence": sample["pressure"]["quality"],
        })
    if not total_coverage:
        raise InputError("no_correlatable_metadata_coverage")
    result = {"schema": CORRELATION_SCHEMA, "capture_id": capture["capture_id"],
              "input_index": input_index, "alignment_evidence": "caller_supplied_anchors",
              "source_clock_continuity": "unverified",
              "alignment_uncertainty_ms": uncertainty,
              "metadata_coverage_ms": total_coverage, "untimed_events": untimed, "intervals": rows,
              "interpretation": "Temporal correlation only, not causation or execution concurrency. Anchor evidence is caller-supplied, not authenticated. Overlapping evidence unions must not be added. Pressure is the last observed event, not interval duration. Unrelated builds/tests are not observed."}
    encode_report(result)
    return result


def correlate_files(capture_path, timeline_path, alignment_path, input_index=1):
    if not integer(input_index, 16) or input_index < 1:
        raise InputError("invalid_timeline_input_index")
    capture, capture_digest = read_metadata(capture_path, 1024 * 1024, "resources-v1.schema.json")
    timelines, timeline_digest = read_metadata(timeline_path, 16 * 1024 * 1024, "session-timeline-v1.schema.json")
    alignment, _ = read_metadata(alignment_path, 64 * 1024, "clock-alignment-v1.schema.json")
    if input_index < 1 or input_index > len(timelines["inputs"]):
        raise InputError("invalid_timeline_input_index")
    return correlate(capture, timelines["inputs"][input_index - 1], alignment,
                     capture_digest, timeline_digest, input_index)
