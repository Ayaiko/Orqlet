# AI timing slice: contract and acceptance

Owner request, 25 October 2026: implement Orqlet observability to show where AI
sessions spend time. AI Session Profiling is tracked in
[issue #12](https://github.com/Ayaiko/Orqlet/issues/12); issue #3 remains open for
host resource observability. Issue #1's long-term stack decision remains open. Python 3.9+
standard library is a dependency-free reference CLI, not a whole-product stack ADR.

One semantic writer owns this contract, adapters and analyzer. Flow:
explicit JSONL files → adapter → spans/windows → overlap-aware report → CLI.
No scheduler, daemon, machine-wide discovery, hooks, provider API or executable job
launcher is required. No private logs or generated real-session reports are committed.

## Shared input

`python3 -m orqlet profile FILE [FILE ...] --format auto|codex|claude|trace [--json]`

Files are explicit; directories, recursive discovery and stdin are not accepted.
Each file is analyzed independently; simultaneous sessions are never added as
elapsed time. Input is bounded by byte, line and record limits. Malformed or
oversized input returns a nonzero exit and no partial report. Live/incomplete calls
are counted as incomplete, with no invented duration or success.

Provider-neutral JSONL v1 uses these two record shapes (timestamps require zones):

```json
{"schema":"orqlet.trace.v1","type":"window","id":"turn-1","start":"2026-01-01T00:00:00Z","end":"2026-01-01T00:00:10Z"}
{"schema":"orqlet.trace.v1","type":"span","id":"call-1","category":"tool","tool":"test","workload":"test","start":"2026-01-01T00:00:01Z","end":"2026-01-01T00:00:03Z","output_bytes":120}
```

Categories: tool, command, reasoning, response, agent_wait, user_wait. Workload tags:
test, build, git, filesystem, network, wait, other, mixed, unknown. Explicit trace
tags are caller declarations. Native command tags are heuristic and labeled so;
compound shell commands are mixed, never attributed to the first executable alone.

## Native adapters and provenance

- Codex: response-item function/custom-tool call IDs pair with their outputs;
  task_started/task_complete delimit windows; item_completed timing covers
  Reasoning, AgentMessage, CommandExecution and collaboration waits when present.
  Paired wait_agent records remain usable when item_completed timing is absent;
  this is inferred attribution, not verified runtime metadata.
- Claude Code: assistant tool_use IDs pair with user tool_result IDs. Explicit
  thinkingDurationMs is provider-reported duration anchored at that record.
  Human user messages and end_turn/stop-hook markers delimit observed turns.
  api-request blobs and progress copies are not additional tool calls.
- Formats are best-effort adapters to observed local records, not guaranteed
  vendor schemas. Unsupported/missing timing remains unknown and is counted.
- Provider reasoning/response durations are reported metadata, not measured
  model compute, CPU time or proof of wasted work. Opaque tool returns have
  unknown outcome; text saying "PASS" never manufactures success.

## Output and interpretation

`orqlet.profile.v1` contains one sanitized report per input. Durations use ms.
Report log window, observed task-window union, tool round-trip union, command
runtime metadata, reported reasoning/response, explicit waits, overlap and
unattributed time. Union spans avoid double-counting parallel calls; per-category
values can overlap and must not be summed. User idle between turns is not AI time.
Absent timing is null, not zero. Open turns are counted without inventing a
completed duration. Call ranking includes counts, duration and output bytes;
missing output size is unknown.
Large/repeated outputs are diagnostic leads, not automatic claims of waste.

### Wait attribution (Draft v1 refinement)

`category_union_ms.agent_wait` is the union of all attributed agent-wait spans.
`agent_wait_attribution` exposes their evidence levels separately:

| Field | Meaning |
|---|---|
| `verified_metadata_union_ms` | Valid explicit Codex CollabAgentToolCall wait/wait_agent item timestamps; verified as recorded metadata, not CPU or proof of runtime blocking. |
| `inferred_round_trip_union_ms` | Paired call/result timestamps for exactly wait_agent, collaboration.wait_agent or collaboration.wait. Includes transport/queue overhead; not confirmed blocked-agent time. |
| `inferred_only_ms` | Inferred union outside the verified metadata union. Do not add the two original representations. |
| `caller_declared_union_ms` | Explicit provider-neutral trace declarations, kept separate from verified provider metadata. |
| `attributed_union_ms` | Union of verified, inferred and caller-declared spans; matches the category total. |
| `unmeasured_round_trip_calls` / `unmeasured_round_trip_ms` | Count of known agent-wait calls without a usable matching result/clock. Duration is null when the count is nonzero; timeout arguments never supply duration. |
| `unusable_metadata_spans` | Wait items whose explicit timestamps are missing, partial or invalid. No zero-duration verification is manufactured. |

Absent evidence for an attribution level is null. Generic functions.wait polling,
unqualified wait, sleep, spawn and arbitrary namespace suffixes do not establish
agent-wait identity. Existing workload hints may still label them as generic wait.
`unattributed_within_completed_tasks_ms` remains the residual with unknown cause;
it is not converted into either reasoning or agent wait. Tool and agent-wait
categories overlap and must not be added as elapsed time.

Default output contains no raw messages, reasoning text, command arguments,
environment values, private paths, arbitrary tool names or identifiers. It uses
fixed tool labels and numeric aggregates. Payload bytes are UTF-8 string bytes
or compact JSON bytes, not tokens or network-transfer measurements. Errors expose
only input/line numbers and fixed diagnostic codes. Reports remain local.

## Focused acceptance

Tests cover parallel overlap, task gaps, missing/orphaned/replayed calls, reversed
clocks, provider blobs, reasoning provenance, unknown outcomes, redaction, bounded
reads and CLI end-to-end output/failure. A real-file smoke run reads only selected
local logs and emits aggregates. No heavy workload, VM change or hosted CI run.

## Remaining gaps

- Native formats can evolve; a missing timestamp cannot be reconstructed from a
  prompt, timeout argument or surrounding gap. Incomplete calls remain unmeasured.
  Wrapped/unnamed waits without a recognized tool identity remain unattributed.
- Inferred wait is a round-trip proxy with transport/queue overhead. Even verified
  provider metadata is not CPU time or proof that an agent was blocked or wasteful.
- Session timing alone does not establish host contention or a controlled model
  comparison. Host memory/CPU/pressure belongs to issue #3, which remains open.
