from __future__ import annotations

import ast
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from agent.failproof import FailproofSession, PolicyViolation, format_command

BASE_DIR = Path(__file__).resolve().parent.parent
TARGETS_DIR = BASE_DIR / "targets"
SAMPLE_TARGET = TARGETS_DIR / "calculator.py"
CHAOS_ROOT = BASE_DIR / "tests" / "chaos"
PYTEST_TIMEOUT_SECONDS = 30

Intensity = Literal["mild", "spicy", "nuclear"]

GREMLIN_SYSTEM_PROMPT = """
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
""".strip()

_TEST_HEADER = """\
import importlib.util
import os

import pytest


def _load_target():
    target_path = os.environ.get("GREMLIN_TARGET_PATH")
    if not target_path:
        pytest.fail("GREMLIN_TARGET_PATH is not set")
    spec = importlib.util.spec_from_file_location("gremlin_target", target_path)
    if spec is None or spec.loader is None:
        pytest.fail(f"Unable to load target from {target_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
"""


class TargetAnalysisError(ValueError):
    """Raised when the supplied target cannot be analyzed."""


@dataclass(frozen=True)
class FunctionInfo:
    name: str
    args: tuple[str, ...]
    has_division: bool
    is_async: bool


@dataclass(frozen=True)
class TargetAnalysis:
    summary: str
    functions: tuple[FunctionInfo, ...]


@dataclass(frozen=True)
class ChaosCandidate:
    function_name: str | None
    slug: str
    failure_mode: str
    priority: int
    body: str


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def run_chaos(
    *,
    target_code: str | None,
    intensity: Intensity,
    max_tests: int,
) -> dict[str, Any]:
    source_code = (
        target_code
        if target_code is not None
        else SAMPLE_TARGET.read_text(encoding="utf-8")
    )

    with tempfile.TemporaryDirectory(prefix="gremlin-target-") as temporary_directory:
        target_path = Path(temporary_directory) / "target.py"
        target_path.write_text(source_code, encoding="utf-8")
        agent = GremlinAgent(
            target_path=target_path,
            intensity=intensity,
            max_tests=max_tests,
        )
        return agent.run()


class GremlinAgent:
    def __init__(
        self,
        *,
        target_path: Path,
        intensity: Intensity,
        max_tests: int,
    ) -> None:
        self.target_path = target_path.resolve()
        self.intensity = intensity
        self.max_tests = max_tests
        self.session_id = f"fp_{uuid.uuid4().hex[:12]}"
        self.session_dir = CHAOS_ROOT / f"session_{self.session_id}"

    def run(self) -> dict[str, Any]:
        CHAOS_ROOT.mkdir(parents=True, exist_ok=True)
        source_digest = hashlib.sha256(self.target_path.read_bytes()).hexdigest()
        notes: list[str] = []

        with FailproofSession(
            workspace_root=BASE_DIR,
            chaos_root=CHAOS_ROOT,
            target_path=self.target_path,
            session_id=self.session_id,
        ) as failproof:
            source = failproof.invoke_tool(
                "Read",
                {"file_path": str(self.target_path)},
                lambda: self.target_path.read_text(encoding="utf-8"),
            )
            failproof.invoke_tool(
                "ListDir",
                {"path": str(CHAOS_ROOT)},
                lambda: sorted(path.name for path in CHAOS_ROOT.iterdir()),
            )
            analysis = analyze_target(source)
            hypotheses = build_hypotheses(analysis.functions, self.intensity)
            candidates = select_candidates(
                build_candidates(analysis.functions, self.intensity),
                self.max_tests,
            )

            test_results: list[dict[str, str]] = []
            for index, candidate in enumerate(candidates, start=1):
                test_path = self._test_path(index, candidate)
                content = f"{_TEST_HEADER}\n\n{candidate.body.strip()}\n"
                try:
                    failproof.invoke_tool(
                        "Write",
                        {"file_path": str(test_path), "content": content},
                        lambda path=test_path, text=content: self._write_file(path, text),
                    )
                except PolicyViolation as exc:
                    notes.append(f"Failproof blocked a generated test write: {exc.reason}")
                    continue

                test_results.append(
                    self._run_test_file(
                        failproof,
                        test_path=test_path,
                        failure_mode=candidate.failure_mode,
                    )
                )

            after_digest = hashlib.sha256(self.target_path.read_bytes()).hexdigest()
            if after_digest == source_digest:
                notes.append(
                    "No workspace source files were written; the target copy remained "
                    "unchanged (SHA-256 verified)."
                )
            else:
                notes.append(
                    "SOURCE INTEGRITY ALERT: the temporary target copy changed during the run."
                )

            if failproof.blocks:
                notes.append(
                    f"Failproof blocked {failproof.blocks} unsafe action(s): "
                    + " | ".join(failproof.blocked_events[:3])
                )
            if failproof.loops_stopped:
                notes.append(
                    f"Failproof stopped {failproof.loops_stopped} repeated loop(s)."
                )
            if failproof.sdk_active:
                notes.append("Failproof SDK tracing was active for this session.")
                if failproof.sdk_error:
                    notes.append(f"Failproof SDK reported: {failproof.sdk_error}")
            elif failproof.sdk_error:
                notes.append(f"Failproof SDK trace unavailable: {failproof.sdk_error}")
            else:
                notes.append(
                    "Failproof SDK was not importable; embedded policy enforcement was active."
                )

            notes.append(f"Generated tests are retained in {self.session_dir.relative_to(BASE_DIR)}.")
            metrics = failproof.metrics()

        failures_triggered = sum(
            1 for test in test_results if test["result"] == "FAILED"
        )

        return {
            "summary": analysis.summary,
            "hypotheses": hypotheses,
            "tests": test_results,
            "failures_triggered": failures_triggered,
            "failproof": metrics,
            "notes": " ".join(notes),
        }

    def _test_path(self, index: int, candidate: ChaosCandidate) -> Path:
        target = candidate.function_name or "module"
        safe_target = re.sub(r"[^a-zA-Z0-9_]+", "_", target).strip("_").lower()
        return self.session_dir / f"test_{index:02d}_{safe_target}_{candidate.slug}.py"

    @staticmethod
    def _write_file(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise FileExistsError(f"Refusing to overwrite an existing test: {path}")
        path.write_text(content, encoding="utf-8")

    def _run_test_file(
        self,
        failproof: FailproofSession,
        *,
        test_path: Path,
        failure_mode: str,
    ) -> dict[str, str]:
        argv = [
            sys.executable,
            "-m",
            "pytest",
            str(test_path),
            "-q",
            "--tb=short",
            "--disable-warnings",
            "-p",
            "no:cacheprovider",
        ]
        payload = {
            "command": format_command(argv),
            "argv": argv,
            "cwd": str(BASE_DIR),
        }

        try:
            result = failproof.invoke_tool(
                "Bash",
                payload,
                lambda: self._execute_pytest(argv),
            )
            passed = result.returncode == 0
            output = combine_output(result)
        except PolicyViolation as exc:
            passed = False
            output = f"Blocked by Failproof: {exc.reason}"
        except Exception as exc:
            passed = False
            output = f"Test execution failed: {type(exc).__name__}: {exc}"

        return {
            "file": test_path.name,
            "failure_mode": failure_mode,
            "result": "PASSED" if passed else "FAILED",
            "output": trim_output(output),
        }

    def _execute_pytest(self, argv: list[str]) -> CommandResult:
        env = os.environ.copy()
        env["GREMLIN_TARGET_PATH"] = str(self.target_path)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONHASHSEED"] = "0"
        try:
            completed = subprocess.run(
                argv,
                cwd=BASE_DIR,
                capture_output=True,
                text=True,
                timeout=PYTEST_TIMEOUT_SECONDS,
                env=env,
                check=False,
            )
            return CommandResult(
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else exc.stdout
            stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else exc.stderr
            return CommandResult(
                returncode=124,
                stdout=stdout or "",
                stderr=(stderr or "")
                + f"\nTimed out after {PYTEST_TIMEOUT_SECONDS} seconds.",
            )


def analyze_target(source: str) -> TargetAnalysis:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise TargetAnalysisError(
            f"Target is not valid Python: {exc.msg} (line {exc.lineno})"
        ) from exc

    functions: list[FunctionInfo] = []
    classes = 0

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            classes += 1
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name.startswith("_"):
            continue

        args = [arg.arg for arg in node.args.posonlyargs + node.args.args]
        if args and args[0] in {"self", "cls"}:
            args = args[1:]

        has_division = any(
            isinstance(child, ast.BinOp)
            and isinstance(child.op, (ast.Div, ast.FloorDiv, ast.Mod))
            for child in ast.walk(node)
        )
        functions.append(
            FunctionInfo(
                name=node.name,
                args=tuple(args),
                has_division=has_division,
                is_async=isinstance(node, ast.AsyncFunctionDef),
            )
        )

    function_names = [function.name for function in functions]
    summary_parts = [
        f"Python module with {len(functions)} public function(s)"
        + (f": {', '.join(function_names)}." if function_names else ".")
    ]
    if classes:
        summary_parts.append(f"It also defines {classes} class(es).")
    docstring = ast.get_docstring(tree)
    if docstring:
        summary_parts.append(docstring.splitlines()[0].strip())

    return TargetAnalysis(summary=" ".join(summary_parts), functions=tuple(functions))


def build_hypotheses(
    functions: tuple[FunctionInfo, ...], intensity: Intensity
) -> list[str]:
    callable_functions = [function for function in functions if not function.is_async]

    if not callable_functions:
        hypotheses = [
            "The module may fail or hang while being imported.",
            "Import-time state may make repeated test runs non-deterministic.",
            "No public synchronous functions are available for call-level chaos.",
        ]
        return hypotheses

    hypotheses: list[str] = []
    if any(function.has_division for function in callable_functions):
        hypotheses.append(
            "A zero denominator can escape as an unhandled ZeroDivisionError."
        )
    hypotheses.append("None inputs may leak a raw TypeError instead of a domain error.")
    hypotheses.append("Wrong argument types may be accepted silently or fail inconsistently.")
    hypotheses.append("Empty values may bypass validation and produce invalid results.")

    if intensity in {"spicy", "nuclear"}:
        hypotheses.append("Large numeric inputs may overflow to infinity without a guard.")
    if intensity == "nuclear":
        hypotheses.append("Concurrent calls may expose shared-state or re-entrancy failures.")

    return hypotheses[:5]


def build_candidates(
    functions: tuple[FunctionInfo, ...], intensity: Intensity
) -> list[ChaosCandidate]:
    candidates: list[ChaosCandidate] = []
    synchronous = [function for function in functions if not function.is_async]

    for function in synchronous:
        if not function.args:
            continue
        if function.has_division:
            candidates.append(_division_zero_candidate(function))
        candidates.append(_none_candidate(function))
        candidates.append(_wrong_type_candidate(function))
        candidates.append(_empty_candidate(function))

        if intensity in {"spicy", "nuclear"}:
            candidates.append(_large_candidate(function))
            candidates.append(_nested_candidate(function))
            candidates.append(_repeated_candidate(function))
        if intensity == "nuclear":
            candidates.append(_concurrency_candidate(function))

    if not candidates:
        candidates.append(
            ChaosCandidate(
                function_name=None,
                slug="imports_cleanly",
                failure_mode="The module may fail to import cleanly.",
                priority=99,
                body="""\
def test_target_imports_cleanly():
    target = _load_target()
    assert target is not None
""",
            )
        )

    return candidates


def select_candidates(
    candidates: list[ChaosCandidate], max_tests: int
) -> list[ChaosCandidate]:
    ordered = sorted(
        candidates,
        key=lambda candidate: (
            candidate.priority,
            candidate.function_name or "",
            candidate.slug,
        ),
    )

    selected: list[ChaosCandidate] = []
    covered: set[str | None] = set()
    for candidate in ordered:
        if candidate.function_name in covered:
            continue
        selected.append(candidate)
        covered.add(candidate.function_name)
        if len(selected) >= max_tests:
            return selected

    for candidate in ordered:
        if candidate in selected:
            continue
        selected.append(candidate)
        if len(selected) >= max_tests:
            break

    return selected[:max_tests]


def _division_zero_candidate(function: FunctionInfo) -> ChaosCandidate:
    arguments = ", ".join(["1"] * (len(function.args) - 1) + ["0"])
    call = f"target.{function.name}({arguments})"
    body = f"""\
def test_{function.name}_survives_zero_denominator():
    target = _load_target()
    try:
        result = {call}
    except Exception as exc:
        pytest.fail(
            f"{function.name} raised {{type(exc).__name__}} on a zero denominator: {{exc}}"
        )
    assert result == 0
"""
    return ChaosCandidate(
        function_name=function.name,
        slug="division_by_zero",
        failure_mode=f"{function.name}() crashes on a zero denominator instead of returning a controlled fallback.",
        priority=0,
        body=body,
    )


def _none_candidate(function: FunctionInfo) -> ChaosCandidate:
    arguments = ", ".join("None" for _ in function.args)
    call = f"target.{function.name}({arguments})"
    body = f"""\
def test_{function.name}_handles_none_inputs():
    target = _load_target()
    try:
        result = {call}
    except Exception as exc:
        pytest.fail(
            f"{function.name} raised {{type(exc).__name__}} on None inputs: {{exc}}"
        )
    assert result is not None
"""
    return ChaosCandidate(
        function_name=function.name,
        slug="none_inputs",
        failure_mode=f"{function.name}() leaks an exception when given None inputs.",
        priority=1,
        body=body,
    )


def _wrong_type_candidate(function: FunctionInfo) -> ChaosCandidate:
    arguments = ", ".join('"chaos"' for _ in function.args)
    call = f"target.{function.name}({arguments})"
    body = f"""\
def test_{function.name}_rejects_wrong_types():
    target = _load_target()
    with pytest.raises((TypeError, ValueError)):
        {call}
"""
    return ChaosCandidate(
        function_name=function.name,
        slug="wrong_types",
        failure_mode=f"{function.name}() may accept malformed types instead of rejecting them explicitly.",
        priority=2,
        body=body,
    )


def _empty_candidate(function: FunctionInfo) -> ChaosCandidate:
    arguments = ", ".join('""' for _ in function.args)
    call = f"target.{function.name}({arguments})"
    body = f"""\
def test_{function.name}_rejects_empty_values():
    target = _load_target()
    with pytest.raises((TypeError, ValueError)):
        {call}
"""
    return ChaosCandidate(
        function_name=function.name,
        slug="empty_values",
        failure_mode=f"{function.name}() may accept empty values without explicit validation.",
        priority=3,
        body=body,
    )


def _large_candidate(function: FunctionInfo) -> ChaosCandidate:
    arguments = ", ".join("1e308" for _ in function.args)
    call = f"target.{function.name}({arguments})"
    body = f"""\
def test_{function.name}_bounds_large_numbers():
    import math

    target = _load_target()
    try:
        result = {call}
    except Exception as exc:
        pytest.fail(
            f"{function.name} raised {{type(exc).__name__}} on large inputs: {{exc}}"
        )
    assert isinstance(result, (int, float))
    assert math.isfinite(result), f"{function.name} produced a non-finite result: {{result}}"
"""
    return ChaosCandidate(
        function_name=function.name,
        slug="large_inputs",
        failure_mode=f"{function.name}() can overflow to a non-finite value on very large inputs.",
        priority=4,
        body=body,
    )


def _nested_candidate(function: FunctionInfo) -> ChaosCandidate:
    arguments = ", ".join('{"nested": [1, 2, 3]}' for _ in function.args)
    call = f"target.{function.name}({arguments})"
    body = f"""\
def test_{function.name}_rejects_nested_structures():
    target = _load_target()
    with pytest.raises((TypeError, ValueError)):
        {call}
"""
    return ChaosCandidate(
        function_name=function.name,
        slug="nested_structures",
        failure_mode=f"{function.name}() may accept nested structures instead of rejecting them.",
        priority=5,
        body=body,
    )


def _repeated_candidate(function: FunctionInfo) -> ChaosCandidate:
    arguments = ", ".join("1" for _ in function.args)
    call = f"target.{function.name}({arguments})"
    body = f"""\
def test_{function.name}_is_stable_across_repeated_calls():
    target = _load_target()
    results = [{call} for _ in range(200)]
    assert all(result is not None for result in results)
"""
    return ChaosCandidate(
        function_name=function.name,
        slug="repeated_calls",
        failure_mode=f"{function.name}() may become unstable across repeated calls.",
        priority=6,
        body=body,
    )


def _concurrency_candidate(function: FunctionInfo) -> ChaosCandidate:
    arguments = ", ".join("1" for _ in function.args)
    call = f"target.{function.name}({arguments})"
    body = f"""\
def test_{function.name}_survives_concurrent_calls():
    from concurrent.futures import ThreadPoolExecutor

    target = _load_target()

    def invoke(_):
        return {call}

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(invoke, range(32)))
    assert all(result is not None for result in results)
"""
    return ChaosCandidate(
        function_name=function.name,
        slug="concurrency",
        failure_mode=f"{function.name}() may expose re-entrancy or shared-state failures under concurrency.",
        priority=7,
        body=body,
    )


def combine_output(result: CommandResult) -> str:
    return "\n".join(part for part in (result.stdout, result.stderr) if part).strip()


def trim_output(output: str) -> str:
    cleaned = re.sub(r"\x1b\[[0-9;]*m", "", output).strip()
    if not cleaned:
        return "pytest produced no output."
    if len(cleaned) <= 1800:
        return cleaned
    return "...\n" + cleaned[-1797:]
