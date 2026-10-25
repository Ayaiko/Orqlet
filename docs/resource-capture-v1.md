# Bounded resource capture v1

Issue #3, based on the reviewed timeline prerequisite in Draft #13. This bounded
native-backend decision does not settle the long-term product-stack decision in
#1 or implement Docker Guardian (#14), inventory (#5), scheduling or a dashboard.

## Shared interface agreed before implementation

- `sample --duration N [--output FILE]`: foreground capture; default 60 seconds,
  integer 1–300 seconds, 1 Hz, baseline plus at most 300 interval samples.
- `orqlet.resources.v1`: local capture nonce; backend/OS capability; samples with
  UTC + monotonic milliseconds, observed interval and quality; host CPU deltas,
  pressure observations, swap allocation and swap-in/out deltas in bytes.
- `correlate CAPTURE TIMELINE --alignment FILE [--input N]`: optional offline
  correlation with one selected `orqlet.timeline.v1` input. Without explicit
  alignment evidence, no join is permitted. No raw AI-log input is accepted.
- `orqlet.alignment.v1`: caller-supplied observation of at least two common clock
  anchors, bound to the capture nonce and the metadata-only timeline digest.
  Each anchor links a capture sample ID with an observed source-clock time.
  These are caller-supplied evidence, not authenticated or vendor-verified proof.
  Validate anchor coverage/consistency, host clock continuity and source status;
  report this evidence quality without upgrading it to verified provider timing.
- `orqlet.correlation.v1`: same-window interval coverage and unioned activity by
  category/evidence beside host samples; correlation only, never causal blame.
- Both capture and report are limited to 1 MiB. Input limits are 1 MiB/capture,
  16 MiB/timeline, 64 KiB/alignment. Fail with safe codes on invalid/oversized data.
- Private output uses exclusive creation with mode 0600; never overwrite a file.
  No daemon, hooks, Docker, global settings, network access or process discovery.

The Python CLI owns bounds, cleanup, math, validation and offline joins. A small
owned C helper uses public macOS SDK structures (no hand-written ctypes ABI),
Mach host counters, read-only `vm.swapusage` and Dispatch pressure notifications.
The helper is compiled in a private temporary directory using existing Apple
Command Line Tools; preparation is excluded from the capture window and reported
separately. No compiler/toolchain is installed. Unsupported hosts fail explicitly.

Pressure initially remains unknown until a native notification. Later states
mean last observed event, not a continuous pressure measurement. No pressure
state is guessed when multiple coalesced flags have unknown order; that state
remains unknown with `coalesced_events` quality. No pressure
duration is interpolated. Missing/denied/stale counters, first deltas, counter
resets, missed intervals and clock jumps remain explicit. RSS is never physical
footprint. Collector wakeups/physical footprint are unavailable in this slice.

## Capture usage, capability and failure behavior

```sh
python3 -m orqlet sample --duration 3 --output capture.json
```

Without `--output`, JSON goes to stdout; shell redirection permissions are then
the user's responsibility. `--output` publishes a complete 0600 file atomically
and exclusively, including against existing/dangling symlinks. It never replaces
an existing file. Exact timestamps and activity metadata remain private data.

Supported backend: macOS Apple Silicon, Python 3.9+, existing Apple Command Line
Tools. The selected SDK directory is inspected nonrecursively; prefer an existing
SDK matching the host OS major, otherwise use Apple's selected SDK. Pass the SDK
only via `-isysroot` for this compilation; never modify SDKROOT or system settings.
The numeric SDK/OS versions are recorded, without developer-directory paths.
Verified locally on macOS 26.6 with SDK 26.5; other versions are capability-checked,
not claimed to have been tested. No toolchain is downloaded or installed.

SDK lookup has a five-second timeout, preparation a twenty-second timeout, and
the owned helper has a duration-plus-three-second deadline. Preparation time is
reported separately from the capture window. The helper permits at most N+1
samples plus one self-usage summary, 4 KiB per protocol line and 1 MiB total.
An incomplete/oversized/invalid protocol fails with a fixed code and no success
report. Cleanup stops and reaps only the helper owned by this invocation. Ctrl-C
returns 130 without publishing a partial file; no persistent service is installed.

CPU is aggregate host busy ticks divided by all host ticks, including nice time;
it is not per-process attribution. Swap-in/out convert lifetime VM page-counter
deltas with the actual Mach page size. These are logical swap page bytes, not a
measurement of physical disk I/O. Swap allocation is a separate point reading.
First deltas, reset counters, denied/unavailable readings and page-size changes
remain null with quality reasons. Intervals over 1.5 seconds or slow reads over
250 ms do not get interpolated CPU/swap deltas.

Paired wall/monotonic samples detect backward movement and deviations over
250 ms, both adjacent and relative to the first sample. Any such discontinuity
blocks correlation for the capture. The tolerance is a guardrail, not a claim
of clock synchronization accuracy.

Collector CPU counters cover the Python capture/projection phase and native
helper separately. Native peak RSS and Python process-lifetime peak RSS are
labelled separately, never added into physical RAM usage. Compilation CPU/RSS
is not included; its elapsed preparation time is. Wakeups/physical footprint
are explicitly unavailable. A short smoke does not establish a universal
overhead budget or pressure/causation baseline.

## Explicit offline clock evidence

Capture/profile/timeline have no Python package dependencies. The optional
correlator reuses `jsonschema` (MIT) for strict Draft 2020-12 input validation:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-correlation.txt
.venv/bin/python -m orqlet correlate capture.json timeline.json --alignment alignment.json
```

The correlator reads only explicitly selected metadata files, not raw AI logs.
All three input contracts reject extra properties, unsupported versions,
nonfinite/oversized numbers and duplicate JSON keys. A capture is limited to
1 MiB, a timeline to 16 MiB, and alignment evidence to 64 KiB. Reports are capped
at 1 MiB. Limits fail explicitly rather than truncating or emitting partial JSON.

Before creating `orqlet.alignment.v1`, obtain at least two observations of common
clock markers that bracket the intended window. Equal-looking UTC values alone
are not evidence. If these observations do not exist, do not create anchors or
claim actual-session correlation. This slice does not install marker hooks or
collect those observations automatically.

The sidecar contains the capture nonce, SHA-256 of the exact **metadata-only**
capture/timeline files, selected input index, uncertainty (0–250 ms), and ordered
anchors `{host_sample_id, source_unix_ms}`. Hashes bind the supplied files only;
they are not raw AI-log fingerprints, are not included in output, and do not
authenticate the observer. Anchors must match real capture sample IDs, increase
on both clocks, have consistent offsets, and declare at least the capture's
observed read/clock uncertainty. Blocked source clocks cannot be overridden.

Even successful checks return `alignment_evidence: caller_supplied_anchors`
and `source_clock_continuity: unverified`. This is an explicitly opted-in,
caller-anchored exploratory correlation, not independently verified alignment.
Unknown forward-jump/source-continuity gaps remain an acceptance limitation.
Formal acceptance of a stronger same-clock proof remains in Issue #3.

Correlation is restricted to anchor coverage and actual capture windows. Event
and coverage edges are conservatively trimmed by the declared uncertainty;
uncovered portions and untimed events remain visible. Missed/stale capture
intervals receive no joined coverage. Union overlapping spans within each fixed
category/evidence/source group; do not add groups as elapsed time. Preserve
unknown residuals and original evidence levels. Pressure is a last-event state
at a sample, never an interpolated pressure duration. No historical data or
unrelated terminal builds/tests are inferred.

## Verification

```sh
python3 -m unittest discover -s tests -v
.venv/bin/python -m unittest discover -s tests -p 'validate_*.py' -v
```

Fixtures cover native counter units, missing/denied/stale data, counter/page
resets, clock anomalies, private file publication, cancellation/exit races,
strict schema boundaries, metadata identity and the real timeline-to-correlator
adapter. No synthetic pressure, workload generation or Docker commands run.
Live native capture is an explicit short read-only smoke, not part of unit tests.
