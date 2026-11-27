# Harbourline Search

Build access-controlled search and quote-backed answers for the fictional
Harbourline Payments. Read [CONTRACT.md](CONTRACT.md) first. It is the behavioural
authority. The supplied application is intentionally unfinished: valid search
and answer requests return 501; `ingest round1` exits 3 until implemented.

## Preflight and installation

Use Python 3.14 and uv >=0.12.0 on macOS arm64, Windows x86_64 PowerShell, or
Linux x86_64/aarch64. Install uv using its
[official installation instructions](https://docs.astral.sh/uv/getting-started/installation/).
If uv was installed with WinGet while PowerShell was already open, open a new PowerShell window (or refresh PATH) if `uv` is not recognized.
From the unpacked repository root, these commands work in both macOS/Linux
shells and PowerShell:

```sh
uv --version
uv --no-config python install 3.14
uv --no-config sync --frozen
uv --no-config run --frozen --no-sync test -q
uv --no-config run --frozen --no-sync check
```

On a fresh Windows install, antivirus may delay Python imports on the first test run; rerun if it is interrupted during imports.

Keep the supplied dependency/build metadata and complete lock unchanged. The
runtime and development packages are already fixed; there are no optional
downloads or services to add. Qdrant is optional, not a grading preference.
After installation, commands use the installed interpreter and dependencies.
Tests/subprocesses must use `sys.executable`, not nested `uv run` or downloads.

Before implementing, make a dummy export from your chosen AI tool and confirm
that it contains prompts and replies. Resolve setup/export problems through the
contact in your invitation; setup is reported separately from active task time.

Commit the untouched starter using your own Git identity:

```sh
git init
git add .
git commit -m "Untouched starter"
```

## Run

The following commands are identical in macOS/Linux and PowerShell. Start the
simulator in one terminal. In another, ingest with the service stopped, then
start the service against the same store:

```sh
uv --no-config run --frozen --no-sync sim
```

```sh
uv --no-config run --frozen --no-sync ingest round1
uv --no-config run --frozen --no-sync serve
```

Only `round1` is supplied. Stop the service with Ctrl-C before re-ingesting,
then restart it. `GET /health` is public and requires no simulator or data.
Both implemented POST endpoints require one known bearer token.

macOS/Linux requests:

```sh
curl -sS http://127.0.0.1:8080/health
curl -sS http://127.0.0.1:8080/search -H 'Content-Type: application/json' -H 'Authorization: Bearer guest-token-TOKEN_CANARY_7f3' -d '{"query":"When do refund requests expire?","top_k":5}'
curl -sS http://127.0.0.1:8080/answer -H 'Content-Type: application/json' -H 'Authorization: Bearer guest-token-TOKEN_CANARY_7f3' -d '{"query":"When do refund requests expire?","top_k":5}'
```

PowerShell requests:

```powershell
Invoke-RestMethod -Uri 'http://127.0.0.1:8080/health'
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8080/search' -ContentType 'application/json' -Headers @{Authorization='Bearer guest-token-TOKEN_CANARY_7f3'} -Body '{"query":"When do refund requests expire?","top_k":5}'
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8080/answer' -ContentType 'application/json' -Headers @{Authorization='Bearer guest-token-TOKEN_CANARY_7f3'} -Body '{"query":"When do refund requests expire?","top_k":5}'
```

These are public fixture credentials, not real secrets. All identities and
groups are in [identities.json](identities.json).

## Settings

Settings read environment variables only, never `.env` or secret directories.
Configuration failures print one line naming the variable, never its value,
and exit 2. Unknown rounds also exit 2. Successful ingestion may skip invalid
documents; operational failures exit 1 and embedding 401 exits 2.

| Variable | Default | Purpose |
| --- | --- | --- |
| `PORT` | `8080` | Application loopback port. |
| `DATA_DIR` | `.store` | Persistent candidate store. |
| `SIM_PORT` | `8090` | Simulator port. |
| `EMBED_URL` | `http://127.0.0.1:$SIM_PORT` | Shared model base URL. |
| `EMBED_API_KEY` | `test-embed-key` | Fixture model key. |

The simulator separately accepts `SIM_API_KEY` (same default) and `SIM_FAULTS`
(empty by default). Match the client key when changing the simulator key.
Ports accept ASCII digits from 1 to 65535. Keys are non-empty ASCII without
whitespace. URL credentials, queries and fragments are rejected.

Example custom application port, macOS/Linux:

```sh
PORT=8081 uv --no-config run --frozen --no-sync serve
```

PowerShell:

```powershell
$env:PORT='8081'
uv --no-config run --frozen --no-sync serve
Remove-Item Env:PORT
```

## Supplied helpers and fixture

`manifest.load_manifest(round_dir: Path)` yields `.row` metadata and `.content`
bytes. It rejects manifest BOMs, blank lines, absolute/traversing paths and paths
resolving outside the snapshot. Document groups and usable text remain per-record
checks, with the helper owning all decoding/markup/heading mechanics:

```python
from pathlib import Path
from harbourline_search.manifest import load_manifest
from harbourline_search.text import prepare_text

for document in load_manifest(Path("data/round1")):
    try:
        prepared = prepare_text(document.content or b"")
    except ValueError as error:
        reason = str(error)
        continue
    text = prepared.text
    first_section = prepared.section_at(0)
```

Offsets index the returned text, not the original bytes. The nearest preceding
heading is returned as a plain label, or `""`. UTF-8 BOMs are stripped; invalid
UTF-8 and empty prepared text raise the fixed reasons above. Markdown/HTML
formatting is flattened. Instructions and URLs left in the text are inert data.

`Settings.embedding_url` and `Settings.answer_url` append the model routes while
preserving base paths. Strict request/response models and `install_error_handlers`
in the starter provide safe HTTP envelopes, including 400 validation, 404, 405,
415 and 500. Implement authentication, access filtering and model validation.

The public pytest `simulator` fixture starts a free-port model server and cleans
it up. It has `.url`, `.key`, `.flags(*names)`, `.reset()` and `.stats()`. See
[tests/test_simulator.py](tests/test_simulator.py) for both model wire examples.
For a non-pytest check use `with sim.testing.running_simulator() as simulator:`.
Fault delays can be injected for fast deterministic tests.

## Measure and check

Run in either shell:

```sh
uv --no-config run --frozen --no-sync evaluate
uv --no-config run --frozen --no-sync test -q
uv --no-config run --frozen --no-sync check
```

The evaluator owns temporary storage, free-port processes and cleanup. It writes
[submission/evaluation.json](submission/evaluation.json): eight retrieval
questions scored as hit@5, four separate controls, restart/re-ingestion observations
and bounded application-log canary checks. The output file is created by the
command. Service errors are misses; a broken evaluator reports no measurement.
Control observations require human interpretation and do not prove context safety.
Hash embeddings are lexical, not a measure of semantic model selection.

Use `--scenario` to select `clean` (default), `faulted-ingest`, `wrong-key`,
`query-outage`, `llm-outage` or `llm-bad-output`. Use `--output` to retain each
result separately; otherwise each run replaces the default output. Both shells:

```sh
uv --no-config run --frozen --no-sync evaluate --scenario llm-bad-output --output submission/evaluation-llm-bad-output.json
uv --no-config run --frozen --no-sync evaluate --scenario clean --output submission/evaluation.json
```

Chunk IDs must remain stable for identical snapshot data and chunking choices,
including after restart and re-ingestion. The clean evaluation compares selected
`doc_id`/`revision`/`chunk_id` tuples and checks for duplicate results.

`check` runs Ruff format/lint and basedpyright; `fix` applies safe Ruff changes
then checks. Use `uv run test`, never bare `test` (a shell builtin). Tooling does
not sync or download. Tests/checks have a combined 300-second review ceiling;
startup has 30 seconds, ingestion 180 and each API request 10. These are hang
limits, not candidate work timers. Choose bounded retries/timeouts within them.

Use IDs, counts, timings and fixed reasons in application logs. Never log queries,
document/model text, request bodies or tokens. The supplied JSON logger does not
make an arbitrary log message safe. Uvicorn access logs are disabled.

## Fault controls

Both model APIs reject extra fields, including `temperature`, `response_format`,
`encoding_format` and `stream`. See [sim/README.md](sim/README.md) for full wires.
Controls are local test tools; application code must not call them.

macOS/Linux:

```sh
curl -sS http://127.0.0.1:8090/sim/faults -H 'Content-Type: application/json' -d '{"flags":["llm_bad_output"]}'
curl -sS http://127.0.0.1:8090/sim/reset -H 'Content-Type: application/json' -d '{}'
curl -sS http://127.0.0.1:8090/sim/faults -H 'Content-Type: application/json' -d '{"flags":[]}'
```

PowerShell:

```powershell
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8090/sim/faults' -ContentType 'application/json' -Body '{"flags":["llm_bad_output"]}'
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8090/sim/reset' -ContentType 'application/json' -Body '{}'
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8090/sim/faults' -ContentType 'application/json' -Body '{"flags":[]}'
```

Embedding `rate_limit`, `slow` (one second) and `bad_response` do not repeat for
an identical input array between resets/changed flag sets. `outage`, `llm_outage`
and `llm_bad_output` persist across retries. Reset clears counts/rotations but
keeps flags; clear flags explicitly for a clean run. Retry-After is in seconds.
Never retry an unchanged 400 or a 401. Do not force a batching choice to trigger
every transient fault.

## Note and AI exports

Complete the three short items in [submission/decisions.md](submission/decisions.md):
the decision you'd most like to be asked about; one AI output you didn't trust,
how you checked it, and which session file and turn shows it; and gaps and time
spent.

Keep full available prompts and replies, including failed/abandoned approaches,
plans and tool traces exposed by the tool. Do not reconstruct hidden context or
missing inline completions. Start task-only threads and retain native contents.

- VS Code Copilot Chat: open each task chat and run **Chat: Export Chat...** from
  the Command Palette. Keep the native JSON as `session-01.json`, `session-02.json`.
  Check both prompts and replies are present. See the
  [official export recipe](https://code.visualstudio.com/docs/agents/run/sessions/session-history#_export-or-copy-a-conversation).
- Claude Code: run `/export submission/ai-session-history/session-01.txt` in each
  task session. Preserve any available tool details and
  note what the native export does not expose.
- Another tool: use its native task-session export. If it has no export, keep a
  faithful, contemporaneous transcript with speaker labels. Record omissions;
  selected screenshots, a share link or a curated summary do not replace history.

Put files under `submission/ai-session-history/`, with nested folders as useful.
Mark secret/personal redactions; do not upload unrelated chats. Use portable
filenames without colons, trailing dots/spaces, reserved device names or case-only
duplicates. Map renamed exports to their tools in the note; do not rewrite native
contents merely to rename a file. Nested `.log` and `.zip` exports are not ignored.

Check staging and, after pushing, open the files on GitHub:

```sh
git add submission
git diff --cached --stat
git ls-files submission/ai-session-history
```

The panel may read histories and use AI to review them. Reviewer-controlled
copies, including extracts and AI-review sessions, are deleted 180 days after the
hiring decision. Your repository/copies remain yours. Large exports, redactions,
failed capture or access problems go to the hiring lead named in the invitation.

## Handover

Keep development commits without squashing. Push code, tests, the evaluator
output, note and native histories to your own private GitHub repository. Replace
the quoted placeholder with its URL; do not put credentials in commands:

```sh
git add .
git commit -m "Implement search and answers with checks"
git remote add origin "PRIVATE-REPOSITORY-URL"
git push -u origin HEAD
git branch --show-current
git rev-parse HEAD
```

Invite the reviewer named in your invitation. Reply with repository URL, branch,
full SHA and honest time estimate; no separate attachment is needed. Keep the
task, solution and histories confidential except for the panel and chosen AI tools.
Plan for about 2 hours and report gaps. After submission there is a live follow-up that includes a small live change to your code.
