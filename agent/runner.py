from __future__ import annotations

import ast
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import tokenize
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from agent.failproof import FailproofSession, PolicyViolation, format_command
from agent.llm import GeneratedTest, LLMClient, LLMError, validate_test_code
from agent.prompts import GREMLIN_SYSTEM_PROMPT
from agent.repo import (
    PROJECT_ROOT,
    RepoFile,
    RepoIndex,
    resolve_target_path,
    scan_repository,
)

BASE_DIR = PROJECT_ROOT
TARGETS_DIR = BASE_DIR / "targets"
SAMPLE_TARGET = TARGETS_DIR / "calculator.py"
CHAOS_ROOT = BASE_DIR / "tests" / "chaos"
PYTEST_TIMEOUT_SECONDS = 30

Intensity = Literal["mild", "spicy", "nuclear"]

_TEST_HEADER = """\
import importlib
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
    target_file: str | None = None
    target_module: str | None = None


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
    target_path: str | None = None,
) -> dict[str, Any]:
    if target_path is not None:
        if target_code is not None:
            raise TargetAnalysisError(
                "Provide either target_code or target_path, not both."
            )
        resolved = resolve_target_path(target_path)
        if resolved.is_dir():
            index = scan_repository(resolved)
            if not index.files:
                raise TargetAnalysisError(
                    f"No readable Python modules were found under {resolved}."
                )
            agent = GremlinAgent(
                target_path=None,
                repo_path=resolved,
                repo_index=index,
                intensity=intensity,
                max_tests=max_tests,
            )
        else:
            agent = GremlinAgent(
                target_path=resolved,
                repo_path=resolved.parent,
                repo_index=None,
                intensity=intensity,
                max_tests=max_tests,
            )
        return agent.run()

    source_code = (
        target_code
        if target_code is not None
        else SAMPLE_TARGET.read_text(encoding="utf-8")
    )

    with tempfile.TemporaryDirectory(prefix="gremlin-target-") as temporary_directory:
        copied_target = Path(temporary_directory) / "target.py"
        copied_target.write_text(source_code, encoding="utf-8")
        agent = GremlinAgent(
            target_path=copied_target,
            repo_path=copied_target.parent,
            repo_index=None,
            intensity=intensity,
            max_tests=max_tests,
        )
        return agent.run()


class GremlinAgent:
    def __init__(
        self,
        *,
        target_path: Path | None,
        repo_path: Path | None,
        repo_index: RepoIndex | None,
        intensity: Intensity,
        max_tests: int,
    ) -> None:
        self.target_path = target_path.resolve() if target_path else None
        self.repo_index = repo_index
        if repo_index is not None:
            self.repo_path = repo_index.root.resolve()
        elif repo_path is not None:
            self.repo_path = repo_path.resolve()
        elif self.target_path is not None:
            self.repo_path = self.target_path.parent
        else:
            raise ValueError("A target path or repository index is required.")

        self.intensity = intensity
        self.max_tests = max_tests
        self.session_id = f"fp_{uuid.uuid4().hex[:12]}"
        self.session_dir = CHAOS_ROOT / f"session_{self.session_id}"
        self._integrity: dict[Path, str] = {}

    def run(self) -> dict[str, Any]:
        CHAOS_ROOT.mkdir(parents=True, exist_ok=True)
        notes: list[str] = []
        client = LLMClient.from_env()
        if client is not None:
            notes.append(f"LLM planner active with model '{client.model}'.")
        else:
            notes.append(
                "No LLM is configured; the deterministic planner is active. "
                "Set GREMLIN_LLM_BASE_URL and optionally GREMLIN_LLM_API_KEY to enable it."
            )

        with FailproofSession(
            workspace_root=BASE_DIR,
            chaos_root=CHAOS_ROOT,
            target_path=self.target_path or self.repo_path,
            target_root=self.repo_path,
            session_id=self.session_id,
        ) as failproof:
            failproof.invoke_tool(
                "ListDir",
                {"path": str(CHAOS_ROOT)},
                lambda: sorted(path.name for path in CHAOS_ROOT.iterdir()),
            )

            if self.repo_index is not None:
                summary, hypotheses, test_results = self._run_repo(
                    failproof, client, notes
                )
            else:
                summary, hypotheses, test_results = self._run_single(
                    failproof, client, notes
                )

            self._verify_integrity(notes)
            self._append_failproof_notes(failproof, notes)
            notes.append(
                f"Generated tests are retained in {self.session_dir.relative_to(BASE_DIR)}."
            )
            metrics = failproof.metrics()

        failures_triggered = sum(
            1 for test in test_results if test["result"] == "FAILED"
        )

        return {
            "summary": summary,
            "hypotheses": hypotheses,
            "tests": test_results,
            "failures_triggered": failures_triggered,
            "failproof": metrics,
            "notes": " ".join(notes),
        }

    def _run_single(
        self,
        failproof: FailproofSession,
        client: LLMClient | None,
        notes: list[str],
    ) -> tuple[str, list[str], list[dict[str, str]]]:
        if self.target_path is None:
            raise TargetAnalysisError("A single-file target path is required.")

        source = self._read_source(failproof, self.target_path)
        analysis = analyze_target(source)
        hypotheses = build_hypotheses(analysis.functions, self.intensity)
        test_results: list[dict[str, str]] = []
        used_llm = False

        if client is not None:
            try:
                plan = client.plan_file(
                    filename=self.target_path.name,
                    source=_truncate_source(source),
                    intensity=self.intensity,
                    max_tests=self.max_tests,
                )
            except LLMError as exc:
                notes.append(
                    f"LLM planning failed ({str(exc)[:180]}); "
                    "deterministic fallback used."
                )
            else:
                used_llm = True
                hypotheses = _complete_hypotheses(list(plan.hypotheses), hypotheses)
                for index, generated in enumerate(plan.tests, start=1):
                    result = self._run_generated_test(
                        failproof,
                        generated=generated,
                        index=index,
                        target_path=self.target_path,
                        notes=notes,
                    )
                    if result is not None:
                        test_results.append(result)
                if not test_results:
                    notes.append(
                        "The LLM plan produced no runnable tests; "
                        "deterministic fallback used."
                    )

        if not used_llm or not test_results:
            candidates = select_candidates(
                build_candidates(analysis.functions, self.intensity),
                self.max_tests,
            )
            for index, candidate in enumerate(candidates, start=1):
                result = self._run_candidate(
                    failproof,
                    candidate=candidate,
                    index=index,
                    target_path=self.target_path,
                    notes=notes,
                )
                if result is not None:
                    test_results.append(result)

        return analysis.summary, hypotheses, test_results

    def _run_repo(
        self,
        failproof: FailproofSession,
        client: LLMClient | None,
        notes: list[str],
    ) -> tuple[str, list[str], list[dict[str, str]]]:
        index = self.repo_index
        if index is None:
            raise TargetAnalysisError("A repository index is required.")

        summary = index.summary()
        hypotheses = _repo_hypotheses(index, self.intensity)
        test_results: list[dict[str, str]] = []
        selected_names: set[str] = set()

        if client is not None:
            try:
                plan = client.plan_repo(
                    index=index,
                    intensity=self.intensity,
                    max_tests=self.max_tests,
                )
            except LLMError as exc:
                notes.append(
                    f"LLM repository planning failed ({str(exc)[:180]}); "
                    "deterministic fallback used."
                )
            else:
                hypotheses = _complete_hypotheses(list(plan.hypotheses), hypotheses)
                for index_number, target in enumerate(plan.targets, start=1):
                    file = index.get(target.rel_path)
                    if file is None:
                        continue
                    source = self._read_source(failproof, file.path)
                    try:
                        generated = client.generate_repo_test(
                            file=file,
                            source=_truncate_source(source),
                            target=target,
                        )
                    except LLMError as exc:
                        notes.append(
                            f"LLM test generation failed for {file.rel_path} "
                            f"({str(exc)[:140]})."
                        )
                        continue
                    result = self._run_generated_test(
                        failproof,
                        generated=generated,
                        index=index_number,
                        target_path=file.path,
                        notes=notes,
                    )
                    if result is not None:
                        test_results.append(result)
                        selected_names.add(file.rel_path)

                if test_results:
                    notes.append(
                        f"LLM selected and tested {len(selected_names)} repository file(s)."
                    )
                else:
                    notes.append(
                        "The LLM repository plan produced no runnable tests; "
                        "deterministic fallback used."
                    )

        if not test_results:
            for index_number, (candidate, file) in enumerate(
                _select_repo_candidates(
                    _repo_candidates(index, self.intensity), self.max_tests
                ),
                start=1,
            ):
                self._read_source(failproof, file.path)
                result = self._run_candidate(
                    failproof,
                    candidate=candidate,
                    index=index_number,
                    target_path=file.path,
                    notes=notes,
                )
                if result is not None:
                    test_results.append(result)

        return summary, hypotheses, test_results

    def _read_source(self, failproof: FailproofSession, path: Path) -> str:
        def read() -> str:
            self._integrity[path] = hashlib.sha256(path.read_bytes()).hexdigest()
            with tokenize.open(path) as handle:
                return handle.read()

        return failproof.invoke_tool(
            "Read",
            {"file_path": str(path)},
            read,
        )

    def _run_candidate(
        self,
        failproof: FailproofSession,
        *,
        candidate: ChaosCandidate,
        index: int,
        target_path: Path,
        notes: list[str],
    ) -> dict[str, str] | None:
        test_path = self._test_path(
            index,
            function_name=candidate.function_name,
            slug=candidate.slug,
            target_file=candidate.target_file,
        )
        content = f"{_TEST_HEADER}\n\n{candidate.body.strip()}\n"
        try:
            failproof.invoke_tool(
                "Write",
                {"file_path": str(test_path), "content": content},
                lambda path=test_path, text=content: self._write_file(path, text),
            )
        except PolicyViolation as exc:
            notes.append(f"Failproof blocked a generated test write: {exc.reason}")
            return None

        return self._run_test_file(
            failproof,
            test_path=test_path,
            failure_mode=candidate.failure_mode,
            target_path=target_path,
        )

    def _run_generated_test(
        self,
        failproof: FailproofSession,
        *,
        generated: GeneratedTest,
        index: int,
        target_path: Path,
        notes: list[str],
    ) -> dict[str, str] | None:
        error = validate_test_code(generated.code)
        if error:
            notes.append(f"Discarded an invalid LLM test: {error}")
            return None

        candidate = ChaosCandidate(
            function_name=generated.function_name,
            slug=_slugify(generated.failure_mode),
            failure_mode=generated.failure_mode,
            priority=0,
            body=generated.code,
            target_file=generated.target_file,
            target_module=generated.target_module,
        )
        return self._run_candidate(
            failproof,
            candidate=candidate,
            index=index,
            target_path=target_path,
            notes=notes,
        )

    def _verify_integrity(self, notes: list[str]) -> None:
        if not self._integrity:
            notes.append("No target source files were read; integrity check was skipped.")
            return

        changed = [
            str(path)
            for path, digest in self._integrity.items()
            if not path.exists()
            or hashlib.sha256(path.read_bytes()).hexdigest() != digest
        ]
        if changed:
            notes.append(
                "SOURCE INTEGRITY ALERT: target file(s) changed during the run: "
                + ", ".join(changed[:3])
            )
            return
        notes.append(
            "No target source files were written; all selected target file(s) "
            "remained unchanged (SHA-256 verified)."
        )

    @staticmethod
    def _append_failproof_notes(
        failproof: FailproofSession, notes: list[str]
    ) -> None:
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

    def _test_path(
        self,
        index: int,
        *,
        function_name: str | None,
        slug: str,
        target_file: str | None = None,
    ) -> Path:
        target = function_name or (
            Path(target_file).stem if target_file else "module"
        )
        safe_target = re.sub(r"[^a-zA-Z0-9_]+", "_", target).strip("_").lower()
        return self.session_dir / f"test_{index:02d}_{safe_target}_{slug}.py"

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
        target_path: Path,
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
                lambda: self._execute_pytest(argv, target_path),
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

    def _execute_pytest(self, argv: list[str], target_path: Path) -> CommandResult:
        env = os.environ.copy()
        env["GREMLIN_TARGET_PATH"] = str(target_path)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONHASHSEED"] = "0"
        repo_root = str(self.repo_path)
        existing_path = env.get("PYTHONPATH")
        env["PYTHONPATH"] = (
            repo_root + os.pathsep + existing_path if existing_path else repo_root
        )
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


def _repo_candidates(
    index: RepoIndex, intensity: Intensity
) -> list[tuple[ChaosCandidate, RepoFile]]:
    candidates: list[tuple[ChaosCandidate, RepoFile]] = []
    for file in index.files:
        if not file.functions and not file.classes:
            continue
        functions = tuple(
            FunctionInfo(
                name=function.name,
                args=function.args,
                has_division=function.has_division,
                is_async=function.is_async,
            )
            for function in file.functions
        )
        for candidate in build_candidates(functions, intensity):
            candidates.append(
                (
                    replace(
                        candidate,
                        target_file=file.rel_path,
                        target_module=file.module,
                    ),
                    file,
                )
            )
    return candidates


def _select_repo_candidates(
    candidates: list[tuple[ChaosCandidate, RepoFile]], max_tests: int
) -> list[tuple[ChaosCandidate, RepoFile]]:
    by_file: dict[str, list[tuple[ChaosCandidate, RepoFile]]] = {}
    for candidate, file in candidates:
        by_file.setdefault(file.rel_path, []).append((candidate, file))

    groups = sorted(
        by_file.values(),
        key=lambda group: (
            min(candidate.priority for candidate, _ in group),
            group[0][1].rel_path,
        ),
    )

    selected: list[tuple[ChaosCandidate, RepoFile]] = []
    covered: set[tuple[str, str | None]] = set()

    for group in groups:
        candidate, file = min(
            group,
            key=lambda item: (
                item[0].priority,
                item[0].function_name or "",
                item[0].slug,
            ),
        )
        selected.append((candidate, file))
        covered.add((file.rel_path, candidate.function_name))
        if len(selected) >= max_tests:
            return selected

    for group in groups:
        for candidate, file in sorted(
            group,
            key=lambda item: (
                item[0].priority,
                item[0].function_name or "",
                item[0].slug,
            ),
        ):
            key = (file.rel_path, candidate.function_name)
            if key in covered:
                continue
            selected.append((candidate, file))
            covered.add(key)
            if len(selected) >= max_tests:
                return selected

    return selected


def _repo_hypotheses(index: RepoIndex, intensity: Intensity) -> list[str]:
    functions = tuple(
        FunctionInfo(
            name=function.name,
            args=function.args,
            has_division=function.has_division,
            is_async=function.is_async,
        )
        for file in index.files
        for function in file.functions
    )
    hypotheses = list(build_hypotheses(functions, intensity))
    hypotheses.insert(
        0,
        f"A repository with {len(index.files)} module(s) may hide failures "
        "that are not visible from a single entry point.",
    )
    return hypotheses[:5]


def _complete_hypotheses(primary: list[str], fallback: list[str]) -> list[str]:
    result = list(dict.fromkeys(item for item in primary if item.strip()))
    for item in fallback:
        if len(result) >= 5:
            break
        if item not in result:
            result.append(item)
    return result[:5]


def _slugify(value: str, *, limit: int = 40) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_]+", "_", value).strip("_").lower()
    return slug[:limit].strip("_") or "llm_test"


def _truncate_source(source: str, *, limit: int = 16000) -> str:
    if len(source) <= limit:
        return source
    return source[:limit] + "\n# ... source truncated ..."


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
