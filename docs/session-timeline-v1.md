# Optional privacy-safe session timeline v1

Issue #12 prerequisite for future host correlation in #3. This slice does not
collect host resources or implement Docker Guardian. The existing `profile`
command, aggregate schema, wait unions and overlap calculations remain unchanged.

## Usage and version

Python 3.9+ and the standard library only:

```sh
python3 -m orqlet timeline selected-session.jsonl
# To save locally with restricted file permissions:
umask 077
python3 -m orqlet timeline selected-session.jsonl > timeline.json
```

Output is JSON with `schema: "orqlet.timeline.v1"` and a separate entry per input.
The authoritative machine-readable contract is
[`schemas/session-timeline-v1.schema.json`](../schemas/session-timeline-v1.schema.json),
using JSON Schema Draft 2020-12. Incompatible changes require a new version.
Timeline exports are not accepted as raw session input to the current profiler.

`--format auto|codex|claude|trace` and input bounds match `profile`:
128 MiB/file, 8 MiB/line, 250,000 records/file, at most 16 explicitly selected
files. `--max-events` defaults to 50,000 events per input; limits may be reduced
or explicitly raised as with the reader limits. There is no discovery or network
access. Malformed input or any exceeded bound exits 2 with a fixed diagnostic
and no partial stdout, including when a later input fails. Invalid native timing
can be represented as unknown; invalid caller-declared trace records continue to
fail as specified by the existing trace contract.

## Allowlisted event metadata

Every event has exactly these fields:

| Field | Meaning |
|---|---|
| `id` | Generated `e1`, `e2`, etc., local to one exported input; never a provider ID |
| `category` | Fixed activity category, `task_window`, or `unattributed` |
| `source` | Fixed adapter/provenance label; no arbitrary input strings |
| `evidence` | `verified_metadata`, `inferred`, `caller_declared`, or `unknown` |
| `start_unix_ms`, `end_unix_ms` | UTC Unix milliseconds, or null for an unavailable/unusable endpoint |
| `duration_ms` | End minus start when usable, otherwise null; never guessed from timeout arguments |
| `timing_quality` | `usable`, `missing_timestamp`, `reversed_timestamp`, `out_of_range_timestamp`, `conflicting_records`, or `unmatched_boundary` |

Supported epoch range is -62135596800000 through 253402300799999 milliseconds.
Nonfinite values and booleans are not timestamps. Reversed finite endpoints are
retained for diagnosis with null duration; they are never swapped or clamped.
Zero-length spans are valid evidence, distinct from unknown duration.

`verified_metadata` means an interval recorded by provider metadata, not a
verified CPU measurement or independently authenticated event. Paired tool calls
and results, including recognized agent waits, remain `inferred`. Declared trace
spans/windows remain `caller_declared`. Unusable timing always has `unknown`
evidence while preserving its source label. Open/conflicting calls and orphan
results preserve any known endpoint without inventing the missing one.

`unattributed` intervals are the complement of usable activity inside the union
of completed task windows. Their endpoints/duration can be known while their
activity evidence is unknown. They never become reasoning. Invalid out-of-range
spans cannot conceal that residual. Open windows do not claim a completed task.
Fixed diagnostic counts expose rejected IDs, duplicate/conflicting records and
unmatched boundaries without including their original values.

## Clocks and overlapping evidence

Each input declares `source_wall_clock`, `monotonic_available: false`, counts of
missing record timestamps/regressions, and a correlation status:

- `requires_clock_validation`: wall-clock endpoints exist, but continuity and
  synchronization with any future host capture have not been verified.
- `blocked_record_order_regression`: native record timestamps moved backward.
  This can be clock adjustment or record reordering; do not automatically join
  this input to a host timeline, sort away the anomaly, or repair timestamps.
- `no_usable_timestamps`: no event endpoint can support a join.

Caller-declared trace intervals are allowed in arbitrary record order; their
order is not a native clock observation. They still require clock validation.
Forward jumps cannot be distinguished reliably from idle time using these logs
alone. No continuity, monotonic timestamps, current-time substitutes or host
correlation are fabricated. A future join must validate clock alignment and
actual capture-window coverage; missing historical host samples stay missing.

Events preserve separate evidence, including overlapping tool, inferred-wait and
verified-wait representations of one operation. Event order is extraction order,
not a sorted execution timeline. Union usable intervals for elapsed duration;
never sum overlapping events, task windows and residuals, or infer concurrency
from their overlap. PR #11's agent-wait union remains canonical and unchanged.

## Privacy and verification

The export is constructed from numeric timing and fixed labels, not redacted raw
records. It excludes prompts, reasoning text, code, secrets, arguments, output/file
contents, tool names, workload commands, paths, original session/call/turn IDs,
argument hashes and raw-input content hashes. It never executes input content,
changes logs, installs hooks, edits settings or sends telemetry. Original logs
may contain private data; no real user logs are included in tests or PR evidence.

Exact timestamps and category patterns remain sensitive metadata. Local output
is not permission to publish it. The CLI writes stdout only; the user controls
redirection/access and must not overwrite an input with shell redirection.

The stdlib regression suite covers preserved aggregate results, overlapping wait
evidence, missing/invalid clocks, open/orphan/conflicting calls, declared versus
provider evidence, unknown residuals, bounds and non-disclosure sentinels. Extra
schema tests use the development-only `requirements-schema-test.txt` in a
disposable environment; neither the CLI nor the normal suite needs that package.
