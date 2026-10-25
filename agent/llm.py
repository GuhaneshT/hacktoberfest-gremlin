from __future__ import annotations

import ast
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.prompts import GREMLIN_SYSTEM_PROMPT
from agent.repo import RepoFile, RepoIndex

_BANNED_IMPORTS = {
    "aiohttp",
    "ctypes",
    "ftplib",
    "http",
    "httpx",
    "importlib",
    "marshal",
    "multiprocessing",
    "paramiko",
    "pickle",
    "pty",
    "requests",
    "runpy",
    "shutil",
    "smtplib",
    "socket",
    "subprocess",
    "telnetlib",
    "urllib",
    "webbrowser",
    "websocket",
    "websockets",
}

_BANNED_CALLS = {
    "os.chdir",
    "os.chmod",
    "os.environ",
    "os.fdopen",
    "os.getenv",
    "os.getenvb",
    "os.kill",
    "os.link",
    "os.listdir",
    "os.makedirs",
    "os.mkdir",
    "os.open",
    "os.popen",
    "os.read",
    "os.remove",
    "os.rename",
    "os.replace",
    "os.rmdir",
    "os.scandir",
    "os.symlink",
    "os.system",
    "os.truncate",
    "os.unlink",
    "os.utime",
    "os.walk",
    "os.write",
}

_BANNED_METHODS = {
    "chmod",
    "glob",
    "hardlink_to",
    "iterdir",
    "mkdir",
    "read_bytes",
    "read_text",
    "rename",
    "rglob",
    "rmdir",
    "symlink_to",
    "touch",
    "unlink",
    "write_bytes",
    "write_text",
}

_BANNED_TEXT = (
    re.compile(r"\b(?:subprocess|socket|requests|httpx|urllib|shutil|ctypes)\b"),
    re.compile(r"\bos\.(?:system|popen|remove|unlink|rmdir|rename|replace|kill)\b"),
    re.compile(r"\bos\.(?:environ|getenv|getenvb)\b"),
    re.compile(r"\b(?:eval|exec|compile|__import__)\s*\("),
    re.compile(r"\brm\s+-rf\b"),
    re.compile(r"\bgit\s+push\b"),
    re.compile(r"\bopen\s*\(", re.IGNORECASE),
)

_JSON_SYSTEM = (
    GREMLIN_SYSTEM_PROMPT
    + """

You are now the planning component inside a Python API. Return exactly one JSON
object and no prose or markdown fences. Generated tests must be pure pytest
tests: no network access, no subprocesses, no shell commands, no file writes,
and no modifications to the target repository. Each code value must contain
one or more functions whose names start with test_. Do not import anything from
the API project itself.
"""
)


class LLMError(RuntimeError):
    """Raised when the configured LLM cannot produce a usable plan."""


@dataclass(frozen=True)
class GeneratedTest:
    failure_mode: str
    code: str
    function_name: str | None = None
    target_module: str | None = None
    target_file: str | None = None


@dataclass(frozen=True)
class FilePlan:
    hypotheses: tuple[str, ...]
    tests: tuple[GeneratedTest, ...]


@dataclass(frozen=True)
class RepoTarget:
    rel_path: str
    module: str | None
    function: str | None
    failure_mode: str


@dataclass(frozen=True)
class RepoPlan:
    hypotheses: tuple[str, ...]
    targets: tuple[RepoTarget, ...]


class LLMClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: int = 90,
        max_tokens: int = 4096,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens

    @classmethod
    def from_env(cls) -> LLMClient | None:
        if os.getenv("GREMLIN_LLM_ENABLED", "1") == "0":
            return None

        api_key = (
            os.getenv("GREMLIN_LLM_API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or ""
        ).strip()
        explicit_base_url = (
            os.getenv("GREMLIN_LLM_BASE_URL")
            or os.getenv("OPENAI_BASE_URL")
            or ""
        ).strip()
        if not api_key and not explicit_base_url:
            return None

        base_url = explicit_base_url or "https://api.openai.com/v1"
        model = (
            os.getenv("GREMLIN_LLM_MODEL")
            or os.getenv("OPENAI_MODEL")
            or "gpt-4o-mini"
        ).strip()
        return cls(
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout=_env_int("GREMLIN_LLM_TIMEOUT", 90),
            max_tokens=_env_int("GREMLIN_LLM_MAX_TOKENS", 4096),
        )

    def plan_file(
        self,
        *,
        filename: str,
        source: str,
        intensity: str,
        max_tests: int,
    ) -> FilePlan:
        prompt = f"""Analyze this single Python target and design up to {max_tests}
adversarial pytest tests. Intensity: {intensity}.

The test runner prepends this helper, so use it to access the target:

    def _load_target():
        ...

Return JSON with this exact shape:
{{
  "hypotheses": ["3-5 short failure hypotheses"],
  "tests": [
    {{
      "failure_mode": "one sentence",
      "function": "function name or null",
      "code": "one complete pytest test function using _load_target()"
    }}
  ]
}}

Rules:
- Each tests[] entry must contain exactly one test_ function.
- Use only pytest and the Python standard library.
- Tests should fail when the hypothesis is true; they should not simply assert imports.
- Do not include the helper definition; it is already provided.
- Do not use absolute paths.

<target filename="{filename}">
{source}
</target>
"""
        data = self._chat_json(
            prompt,
            max_tokens=min(max(self.max_tokens, max_tests * 1200), 12000),
        )
        hypotheses = _string_list(data.get("hypotheses"))[:5]
        tests: list[GeneratedTest] = []
        for item in _dict_list(data.get("tests")):
            code = _clean_code(item.get("code"))
            if not code:
                continue
            error = validate_test_code(code)
            if error:
                continue
            function = _optional_text(item.get("function"))
            failure_mode = (
                _optional_text(item.get("failure_mode"))
                or f"The generated test exposed a failure in {function or filename}."
            )
            tests.append(
                GeneratedTest(
                    failure_mode=failure_mode,
                    code=code,
                    function_name=function,
                )
            )
            if len(tests) >= max_tests:
                break

        if not tests:
            raise LLMError("The LLM returned no valid pytest tests.")
        return FilePlan(hypotheses=tuple(hypotheses), tests=tuple(tests))

    def plan_repo(
        self,
        *,
        index: RepoIndex,
        intensity: str,
        max_tests: int,
    ) -> RepoPlan:
        module_map = index.prompt_map()
        prompt = f"""Choose up to {max_tests} high-value chaos targets from this
repository map. Intensity: {intensity}.

Use only paths that appear exactly in the map. Prefer public functions with
interesting branching, parsing, arithmetic, state, or boundary conditions.
Avoid trivial getters and generated code.

Return JSON with this exact shape:
{{
  "hypotheses": ["3-5 short cross-repository failure hypotheses"],
  "targets": [
    {{
      "path": "relative/path/to/file.py",
      "module": "importable.module.name or null",
      "function": "function name or null",
      "failure_mode": "one sentence describing what could break"
    }}
  ]
}}

Repository map:
{module_map}
"""
        data = self._chat_json(prompt, max_tokens=min(self.max_tokens, 3000))
        hypotheses = _string_list(data.get("hypotheses"))[:5]
        targets: list[RepoTarget] = []
        for item in _dict_list(data.get("targets")):
            file = self._resolve_repo_file(index, item.get("path"))
            if file is None:
                continue
            function = _optional_text(item.get("function"))
            known = {candidate.name for candidate in file.functions}
            if function and function not in known:
                function = None
            failure_mode = (
                _optional_text(item.get("failure_mode"))
                or f"{file.rel_path} may fail on malformed or boundary input."
            )
            targets.append(
                RepoTarget(
                    rel_path=file.rel_path,
                    module=file.module,
                    function=function,
                    failure_mode=failure_mode,
                )
            )
            if len(targets) >= max_tests:
                break

        if not targets:
            raise LLMError("The LLM selected no valid repository targets.")
        return RepoPlan(hypotheses=tuple(hypotheses), targets=tuple(targets))

    def generate_repo_test(
        self,
        *,
        file: RepoFile,
        source: str,
        target: RepoTarget,
    ) -> GeneratedTest:
        if file.module:
            import_instruction = (
                f"The repository root is already on sys.path. Import the target as "
                f"'{file.module}', or call _load_target() which imports that module."
            )
        else:
            import_instruction = (
                "The selected file is not importable as a module. Use _load_target(), "
                "which loads GREMLIN_TARGET_PATH directly."
            )

        prompt = f"""Write one adversarial pytest test for this target.
Target file: {target.rel_path}.
Failure mode to probe: {target.failure_mode}
Selected function: {target.function or "choose the most interesting public function"}

{import_instruction}

Return JSON:
{{
  "failure_mode": "one sentence",
  "code": "one complete pytest test function"
}}

Rules:
- Exactly one test_ function.
- Use only pytest and the Python standard library.
- The hypothesis must be exercised, not just imported.
- No file writes, no subprocesses, no network, no shell commands.
- Do not include _load_target; the runner prepends it.

<target path="{target.rel_path}">
{source}
</target>
"""
        data = self._chat_json(prompt, max_tokens=self.max_tokens)
        code = _clean_code(data.get("code"))
        if not code:
            raise LLMError("The LLM returned no test code.")
        error = validate_test_code(code)
        if error:
            raise LLMError(error)
        failure_mode = (
            _optional_text(data.get("failure_mode")) or target.failure_mode
        )
        return GeneratedTest(
            failure_mode=failure_mode,
            code=code,
            function_name=target.function,
            target_module=file.module,
            target_file=file.rel_path,
        )

    def _chat_json(self, user_prompt: str, *, max_tokens: int) -> dict[str, Any]:
        text = self._chat(user_prompt, max_tokens=max_tokens)
        data = _extract_json(text)
        if not isinstance(data, dict):
            raise LLMError("The LLM response was not a JSON object.")
        return data

    def _chat(self, user_prompt: str, *, max_tokens: int) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _JSON_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "GremlinAPI/0.2",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        url = (
            self.base_url
            if self.base_url.endswith("/chat/completions")
            else f"{self.base_url}/chat/completions"
        )
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(5_000_000)
        except urllib.error.HTTPError as exc:
            detail = exc.read(1200).decode("utf-8", errors="replace")
            raise LLMError(f"LLM HTTP {exc.code}: {detail[:600]}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"LLM connection failed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise LLMError("LLM request timed out.") from exc

        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise LLMError("LLM returned invalid JSON.") from exc

        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMError("LLM response contained no choices.")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            content = "".join(
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict)
            )
        if not isinstance(content, str) or not content.strip():
            raise LLMError("LLM response contained no text.")
        return content

    @staticmethod
    def _resolve_repo_file(index: RepoIndex, raw_path: Any) -> RepoFile | None:
        if not isinstance(raw_path, str) or not raw_path.strip():
            return None
        value = raw_path.strip().replace("\\", "/")
        candidate = Path(value)
        if candidate.is_absolute():
            try:
                value = candidate.resolve().relative_to(index.root).as_posix()
            except ValueError:
                return None
        return index.get(value)


def validate_test_code(code: str) -> str | None:
    if not code.strip():
        return "The LLM returned empty test code."
    if len(code) > 60_000:
        return "The generated test is too large."

    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"The generated test is invalid Python: {exc.msg} (line {exc.lineno})."

    test_functions = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    ]
    if not test_functions:
        return "The generated test has no test_ function."
    if re.search(r"\bdef\s+_load_target\s*\(", code):
        return "The generated test must not redefine the _load_target helper."

    allowed_top_level = (
        ast.Import,
        ast.ImportFrom,
        ast.FunctionDef,
        ast.AsyncFunctionDef,
        ast.ClassDef,
        ast.Assign,
        ast.AnnAssign,
    )
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        if not isinstance(node, allowed_top_level):
            return "The generated test contains an unexpected top-level statement."

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            for name in names:
                if name.split(".")[0] in _BANNED_IMPORTS:
                    return f"The generated test imports a blocked module: {name}."

        if not isinstance(node, ast.Call):
            continue

        name = _call_name(node.func)
        if name in {"eval", "exec", "compile", "__import__"}:
            return f"The generated test calls a blocked function: {name}."
        if name in _BANNED_CALLS:
            return f"The generated test calls a blocked function: {name}."
        if name.rsplit(".", 1)[-1] in _BANNED_METHODS:
            return f"The generated test performs a blocked filesystem operation: {name}."
        if (name == "open" or name.endswith(".open")) and _open_call_writes(node):
            return "The generated test opens a file for writing."

    for pattern in _BANNED_TEXT:
        if pattern.search(code):
            return "The generated test contains a blocked operation."

    return None


def _open_call_writes(node: ast.Call) -> bool:
    mode: str | None = None
    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
        if isinstance(node.args[1].value, str):
            mode = node.args[1].value
    for keyword in node.keywords:
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
            if isinstance(keyword.value.value, str):
                mode = keyword.value.value
    return bool(mode and any(flag in mode for flag in ("w", "a", "x", "+")))


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _extract_json(text: str) -> Any:
    cleaned = _strip_fence(text).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end <= start:
            raise LLMError("The LLM did not return a JSON object.")
        try:
            return json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"The LLM returned malformed JSON: {exc.msg}.") from exc


def _strip_fence(text: str) -> str:
    value = text.strip()
    if not value.startswith("```"):
        return value
    lines = value.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines)


def _clean_code(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return _strip_fence(value).strip()


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _string_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    result: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            result.append(item.strip())
    return tuple(result)


def _optional_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default
