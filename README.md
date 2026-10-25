# GremlinAPI

GremlinAPI is an API-first chaos engineering agent. Send it Python code, and **Gremlin** writes adversarial pytest cases, runs them, and returns an honest report of what broke. **Failproof AI** observes every tool call and enforces the safety boundary around the run.

## What it does

1. Accepts source code through `POST /chaos` (or uses the bundled calculator target).
2. Parses the target and forms failure hypotheses.
3. Writes small, focused pytest files to `tests/chaos/session_<id>/`.
4. Executes each test with a timeout and records the real pytest output.
5. Returns hypotheses, test outcomes, failure count, and Failproof session metrics.

The service never writes to the original source. Target code executes from a temporary copy, and a SHA-256 check verifies that working copy before and after every run.

## Quickstart

Requires Python 3.10+.

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

On macOS/Linux use `source .venv/bin/activate`.

Health check:

```bash
curl http://localhost:8000/health
```

Run chaos against the bundled calculator:

```bash
curl -X POST http://localhost:8000/chaos ^
  -H "Content-Type: application/json" ^
  -d "{\"intensity\":\"spicy\",\"max_tests\":5,\"language\":\"python\"}"
```

On macOS/Linux, use `\` instead of `^` for line continuation.

Attack your own code:

```bash
curl -X POST http://localhost:8000/chaos ^
  -H "Content-Type: application/json" ^
  -d "{\"target_code\":\"def average(values):\n    return sum(values) / len(values)\",\"intensity\":\"nuclear\",\"max_tests\":5}"
```

Example shape:

```json
{
  "summary": "Python module with 3 public function(s): add, divide, multiply.",
  "hypotheses": [
    "A zero denominator can escape as an unhandled ZeroDivisionError.",
    "None inputs may leak a raw TypeError instead of a domain error.",
    "Wrong argument types may be accepted silently or fail inconsistently.",
    "Empty values may bypass validation and produce invalid results.",
    "Large numeric inputs may overflow to infinity without a guard."
  ],
  "tests": [
    {
      "file": "test_01_divide_division_by_zero.py",
      "failure_mode": "divide() crashes on a zero denominator instead of returning a controlled fallback.",
      "result": "FAILED",
      "output": "FAILED test_... - Failed: divide raised ZeroDivisionError..."
    }
  ],
  "failures_triggered": 5,
  "failproof": {
    "tool_calls": 12,
    "blocks": 0,
    "loops_stopped": 0,
    "session_id": "fp_..."
  },
  "notes": "No workspace source files were written; the target copy remained unchanged (SHA-256 verified). ..."
}
```

## Failproof integration

Failproof is the runtime safety layer, not a demo wrapper. Each run creates a real Failproof session and every Gremlin tool call is gated before it executes.

### Tracing

`agent/failproof.py` binds a `failproofai-sdk` session and emits:

- `agent_start` / `agent_end` for Gremlin,
- `tool_use` / `tool_result` for every Read, ListDir, Write, and Bash call,
- error results when a policy blocks an action.

Install the SDK separately if it is not already present:

```bash
pip install failproofai-sdk
failproofai config
```

The SDK is imported optionally, so the API still starts if the daemon or SDK is missing. In that case the embedded policy gate still runs, and the response notes that the Failproof trace was unavailable.

### Policies

The shipped policy file is `.failproofai/policies/gremlin-policies.ts`. When Gremlin is run inside a Failproof-instrumented harness such as OpenCode, Claude Code, Codex, or Cursor, Failproof loads it and enforces the rules before the tool executes:

- Writes are only allowed under `tests/chaos/` and must be `test_*.py` or `conftest.py`.
- Writes to `targets/` are denied explicitly.
- Writes containing subprocess, network, eval, or destructive file operations are denied.
- `rm`, `rmdir`, `sudo`, force-push, disk formatting, shutdown, and similar shell commands are denied.
- Repeated identical shell calls are stopped after three attempts.

Install the project policy explicitly if your Failproof setup does not auto-load convention files:

```bash
failproofai policies --install --custom ./.failproofai/policies/gremlin-policies.ts --scope project
failproofai policies
```

### Harness mode

The same Gremlin role is also available as an OpenCode subagent in `.opencode/agents/gremlin.md`, with edits restricted to `tests/chaos/**` and shell access restricted to pytest. Start OpenCode in this repository and invoke `@gremlin` to run the agent directly inside a supported, Failproof-instrumented harness.

```text
@gremlin Attack targets/calculator.py with spicy chaos. Write tests to tests/chaos/ and report the results.
```

### Embedded enforcement

The HTTP service runs Gremlin as a custom agent runtime. Because Failproof cannot automatically intercept tool calls in an arbitrary Python process, `FailproofSession.invoke_tool()` applies the same pre-execution checks directly:

- path containment and test-file naming,
- source immutability,
- dangerous-test-content scanning,
- destructive command detection,
- pytest-only command execution,
- repeated identical failing-command detection,
- loop detection for repeated tool calls.

The response metrics (`tool_calls`, `blocks`, `loops_stopped`, `session_id`) are produced by this session and share the same ID as the Failproof trace.

## Intensity levels

| Level | What Gremlin tries |
| --- | --- |
| `mild` | `None`, empty values, wrong types, zero denominators |
| `spicy` | Large floats, nested structures, repeated calls |
| `nuclear` | Everything above plus concurrent invocation of each function |

`max_tests` is capped at 20. Each generated file targets one failure mode and is executed in its own bounded pytest subprocess.

## Project structure

```text
.
├── main.py                         # FastAPI app and /chaos, /health
├── agent/
│   ├── failproof.py                # Failproof session, policies, metrics
│   └── runner.py                   # Gremlin analysis, planning, execution
├── targets/
│   └── calculator.py               # Sample target
├── tests/
│   ├── test_api.py                 # Service tests
│   └── chaos/                      # Gremlin writes generated tests here
├── .failproofai/policies/
│   └── gremlin-policies.ts         # Harness-level Failproof policies
├── .opencode/agents/
│   └── gremlin.md                  # Same Gremlin role inside OpenCode
├── requirements.txt
├── pytest.ini
└── README.md
```

Generated chaos files are ignored by git and excluded from the project test suite by `norecursedirs = chaos`. They remain on disk after a run for inspection.

## Tests

```bash
pytest
```

The API tests exercise `/health`, a real `POST /chaos` run, source immutability, Failproof policy blocking, and invalid-code handling.

## Gremlin system prompt

The runtime uses the following role definition (also exported as `GREMLIN_SYSTEM_PROMPT` in `agent/runner.py`):

```text
You are Gremlin, a specialized Chaos Engineering agent.

Your only job is to find ways to make the provided target code fail.

Rules:
- You may only write test files inside the /tests/chaos/ directory.
- You must NEVER modify the original source code.
- You must NEVER run destructive shell commands (rm -rf, git push --force, sudo, etc.).
- Prefer small, precise tests over large complicated ones.
- Each test should target one clear failure mode.
- After writing tests, execute them and report the results honestly.
- If a policy blocks you, accept it and try a safer alternative.

Intensity levels:
- mild: normal edge cases (None, empty, zero, wrong types)
- spicy: larger values, nested structures, repeated calls
- nuclear: concurrency ideas, very large inputs, timeouts (still stay safe)

Always follow this process:
1. Summarize what the target does in 1-2 sentences.
2. List 3-5 interesting failure hypotheses.
3. Write the corresponding test files inside /tests/chaos/.
4. Run the tests using pytest.
5. Return a structured report.
```

## Safety notes

Gremlin executes the target code indirectly through pytest, so submitted code is still code. For untrusted targets, run the service in a disposable container with CPU, memory, and filesystem limits. The built-in policy layer prevents Gremlin from modifying source or running destructive commands, but it is not a replacement for OS-level sandboxing.
