# Orqlet

Small, local-first developer workflow diagnostics. The first executable slice
profiles AI session logs to identify expensive tool round trips, large outputs,
repeated calls, reported model activity and time the logs cannot explain.

## Run without installing anything

Python 3.9+; standard library only. From this checkout:

```sh
python3 -m orqlet profile /path/to/selected-session.jsonl
python3 -m orqlet profile codex-session.jsonl claude-session.jsonl --json > report.json
python3 -m unittest discover -s tests -v
```

Inputs are explicit local files. Formats: observed Codex JSONL, Claude Code JSONL,
or provider-neutral `orqlet.trace.v1`; `--format auto` is the default. See the
[versioned contract](docs/session-trace-v1.md) for examples and acceptance cases.

The report distinguishes:

- Tool call-to-result wall time; conservative test/build/Git/filesystem hints.
- Explicit command-runtime and reasoning/response metadata where the provider records it.
- Explicit waits, overlapping activity and incomplete/orphaned calls.
- Unattributed time inside completed task windows. Long gaps between user turns
  are not silently called AI reasoning or wasted time.
- Output bytes and repeated call signatures as candidates for investigation.

Reports never include message/reasoning text, command arguments, private source
paths or arbitrary tool names. Only fixed labels, numeric aggregates and a source
content hash are emitted. Nothing is uploaded. Input logs remain unchanged;
profiling never executes their commands. Redirect stdout explicitly to save a report.

## Interpretation and limits

Provider timing is not model CPU time. Missing timing is **unknown**, not zero.
Categories can overlap: their sum is not elapsed time. Separate inputs are not
summed as task duration or presented as a controlled model comparison. Workload
hints are heuristic; repeated calls may be necessary. Output bytes are not tokens.
Open turns/calls and format limitations remain visible in the report.

Default bounds: 128 MiB/file, 8 MiB/line, 250,000 records/file, 16 inputs and five
tool groups. Limits can be reduced or explicitly raised for file/line/record size;
`--top` is capped at 20. Malformed/oversized input exits 2 without a partial report.

This slice references [observability issue #3](https://github.com/Ayaiko/Orqlet/issues/3).
It does not finish the macOS resource collector or declare the broader stack/
bootstrap dependencies complete. The Python CLI is a portable reference slice;
no daemon, scheduler, GUI, automatic process control or Colima change is introduced.
