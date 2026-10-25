# AI timing slice: contract and acceptance

Owner request, 25 October 2026: implement Orqlet observability to show where AI
sessions spend time. This is a bounded sub-slice of issue #3; host collectors and
issue #1's long-term implementation-stack decision remain open. Python 3.9+
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
