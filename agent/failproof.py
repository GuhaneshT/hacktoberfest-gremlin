from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Callable, TypeVar

try:
    import failproofai_sdk as _sdk
except ImportError:
    _sdk = None

T = TypeVar("T")

_SDK_LOCK = threading.Lock()
_SDK_CONFIGURED = False

_DESTRUCTIVE_SHELL = (
    re.compile(r"\b(?:sudo|doas|su)\b", re.IGNORECASE),
    re.compile(r"\b(?:rm|rmdir|del|erase)\b", re.IGNORECASE),
    re.compile(r"\bgit\s+(?:push|reset\s+--hard|clean\s+-[a-z]*f)", re.IGNORECASE),
    re.compile(r"\b(?:shutdown|reboot|halt|poweroff)\b", re.IGNORECASE),
    re.compile(r"\b(?:mkfs|format|diskpart|bcdedit)\b", re.IGNORECASE),
    re.compile(r"\bdd\s+if=", re.IGNORECASE),
    re.compile(r"\b(?:curl|wget)\b[^|]*\|\s*(?:sh|bash|zsh)\b", re.IGNORECASE),
    re.compile(r"\bremove-item\b.*-recurse", re.IGNORECASE),
)

_DANGEROUS_TEST_CODE = (
    re.compile(r"\b(?:subprocess|socket|requests|urllib|shutil)\b"),
    re.compile(r"\bos\.(?:system|popen|remove|unlink|rmdir|rename|replace)\b"),
    re.compile(r"\b(?:eval|exec|__import__)\s*\("),
    re.compile(r"\b(?:rm\s+-rf|sudo|git\s+push|chmod\s+777)\b"),
    re.compile(r"open\s*\([^)]*['\"](?:w|a|x)", re.IGNORECASE),
)

_SENSITIVE_READS = (
    re.compile(r"(^|[\\/])\.env(\..*)?$", re.IGNORECASE),
    re.compile(r"(^|[\\/])(?:id_rsa|id_ed25519|credentials|secrets)(\..*)?$", re.IGNORECASE),
)


def _ensure_sdk_configured() -> None:
    global _SDK_CONFIGURED
    if _sdk is None or _SDK_CONFIGURED:
        return

    with _SDK_LOCK:
        if _SDK_CONFIGURED:
            return
        environment = (
            os.getenv("AGENTEYE_ENVIRONMENT")
            or os.getenv("FAILPROOFAI_ENVIRONMENT")
            or "gremlinapi"
        ).replace(",", "-")
        _sdk.configure(environment=environment)
        _SDK_CONFIGURED = True


class PolicyViolation(RuntimeError):
    def __init__(self, tool: str, reason: str) -> None:
        super().__init__(reason)
        self.tool = tool
        self.reason = reason


class FailproofSession:
    """Policy enforcement and session metrics for one Gremlin run."""

    def __init__(
        self,
        *,
        workspace_root: Path,
        chaos_root: Path,
        target_path: Path,
        session_id: str,
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        self.chaos_root = chaos_root.resolve()
        self.target_path = target_path.resolve()
        self.session_id = session_id

        self.tool_calls = 0
        self.blocks = 0
        self.loops_stopped = 0
        self.blocked_events: list[str] = []

        self._attempts: Counter[str] = Counter()
        self._failed_commands: Counter[str] = Counter()
        self._max_identical_failures = int(
            os.getenv("FAILPROOF_MAX_IDENTICAL_FAILURES", "2")
        )

        self._sdk_session = None
        self._sdk_agent = None
        self.sdk_active = False
        self.sdk_error: str | None = None

    def __enter__(self) -> FailproofSession:
        if _sdk is None:
            return self

        try:
            _ensure_sdk_configured()
            self._sdk_session = _sdk.session(self.session_id)
            self._sdk_session.__enter__()
            self._sdk_agent = _sdk.agent(
                "gremlin", goal="Find failure modes in the target code"
            )
            self._sdk_agent.__enter__()
            self.sdk_active = True
        except Exception as exc:
            self.sdk_error = f"{type(exc).__name__}: {exc}"
            self._close_sdk()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self._close_sdk(exc_type, exc, tb)
        return False

    def _close_sdk(
        self, exc_type: Any = None, exc: Any = None, tb: Any = None
    ) -> None:
        if self._sdk_agent is not None:
            try:
                self._sdk_agent.__exit__(exc_type, exc, tb)
            except Exception as sdk_exc:
                self.sdk_error = f"{type(sdk_exc).__name__}: {sdk_exc}"
            finally:
                self._sdk_agent = None
        if self._sdk_session is not None:
            try:
                self._sdk_session.__exit__(exc_type, exc, tb)
            except Exception as sdk_exc:
                self.sdk_error = f"{type(sdk_exc).__name__}: {sdk_exc}"
            finally:
                self._sdk_session = None
        self.sdk_active = False

    def _emit(self, method: str, **kwargs: Any) -> None:
        if not self.sdk_active or _sdk is None:
            return
        try:
            getattr(_sdk.event, method)(**kwargs)
        except Exception as exc:
            self.sdk_error = f"{type(exc).__name__}: {exc}"

    def invoke_tool(
        self,
        tool_name: str,
        payload: dict[str, Any],
        fn: Callable[[], T],
    ) -> T:
        self.tool_calls += 1
        call_id = f"call_{uuid.uuid4().hex[:12]}"
        self._emit("tool_use", tool_name=tool_name, tool_call_id=call_id, input=payload)

        try:
            self._authorize(tool_name, payload)
            self._check_loops(tool_name, payload)
            self._attempts[self._key(tool_name, payload)] += 1
        except PolicyViolation as exc:
            self._emit(
                "tool_result",
                tool_name=tool_name,
                tool_call_id=call_id,
                error=exc.reason,
            )
            raise

        try:
            result = fn()
        except Exception as exc:
            self._emit(
                "tool_result",
                tool_name=tool_name,
                tool_call_id=call_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise

        self._emit(
            "tool_result",
            tool_name=tool_name,
            tool_call_id=call_id,
            output=self._preview(result),
        )

        if tool_name == "Bash" and getattr(result, "returncode", 0) != 0:
            self._failed_commands[self._key(tool_name, payload)] += 1

        return result

    def _authorize(self, tool_name: str, payload: dict[str, Any]) -> None:
        if tool_name == "Write":
            self._authorize_write(payload)
        elif tool_name == "Read":
            self._authorize_read(payload)
        elif tool_name == "ListDir":
            self._authorize_list_dir(payload)
        elif tool_name == "Bash":
            self._authorize_bash(payload)
        else:
            self._deny(tool_name, f"Unknown tool: {tool_name}")

    def _authorize_write(self, payload: dict[str, Any]) -> None:
        raw_path = _as_text(payload.get("file_path"))
        if not raw_path:
            self._deny("Write", "A file path is required.")

        path = self._resolve(raw_path)
        if not self._is_within(path, self.chaos_root):
            self._deny(
                "Write",
                f"Writes are only allowed inside {self.chaos_root}. Blocked: {path}",
            )

        if path.suffix != ".py" or not (
            path.name.startswith("test_") or path.name == "conftest.py"
        ):
            self._deny(
                "Write",
                "Gremlin may only write pytest files named test_*.py or conftest.py.",
            )

        content = _as_text(payload.get("content"))
        for pattern in _DANGEROUS_TEST_CODE:
            if pattern.search(content):
                self._deny(
                    "Write",
                    f"Blocked test content containing a dangerous construct ({pattern.pattern}).",
                )

    def _authorize_read(self, payload: dict[str, Any]) -> None:
        raw_path = _as_text(payload.get("file_path"))
        if not raw_path:
            self._deny("Read", "A file path is required.")

        path = self._resolve(raw_path)
        if path != self.target_path and not self._is_within(path, self.workspace_root):
            self._deny("Read", f"Reads are limited to the workspace or target: {path}")

        for pattern in _SENSITIVE_READS:
            if pattern.search(path.name):
                self._deny("Read", f"Blocked a sensitive file read: {path.name}")

    def _authorize_list_dir(self, payload: dict[str, Any]) -> None:
        path = self._resolve(_as_text(payload.get("path")) or ".")
        if not self._is_within(path, self.workspace_root):
            self._deny("ListDir", f"Directory listing is limited to the workspace: {path}")

    def _authorize_bash(self, payload: dict[str, Any]) -> None:
        command = _as_text(payload.get("command"))
        argv = payload.get("argv")

        for pattern in _DESTRUCTIVE_SHELL:
            if pattern.search(command):
                self._deny(
                    "Bash",
                    f"Destructive shell command blocked by Failproof ({pattern.pattern}).",
                )

        if not isinstance(argv, list) or not argv:
            self._deny("Bash", "Shell commands must use an explicit argv list.")

        executable = Path(str(argv[0])).name.lower()
        is_python = executable.startswith("python") or executable in {"py", "py.exe"}
        is_pytest = executable.startswith("pytest")

        if is_pytest:
            pass
        elif is_python:
            if "-m" not in argv or "pytest" not in argv:
                self._deny("Bash", "Gremlin may only run Python with '-m pytest'.")
        else:
            self._deny("Bash", f"Gremlin may only execute pytest, not '{executable}'.")

        if any(token in {"--rootdir", "--confcutdir", "-c"} for token in argv):
            self._deny("Bash", "Overriding pytest configuration is not allowed.")

        for token in argv[1:]:
            if token.startswith("-"):
                continue
            candidate = Path(token)
            if candidate.suffix == ".py" or candidate.is_absolute():
                resolved = self._resolve(token)
                if not self._is_within(resolved, self.chaos_root):
                    self._deny(
                        "Bash",
                        f"pytest may only target files inside {self.chaos_root}.",
                    )

    def _check_loops(self, tool_name: str, payload: dict[str, Any]) -> None:
        key = self._key(tool_name, payload)

        if (
            tool_name == "Bash"
            and self._failed_commands[key] >= self._max_identical_failures
        ):
            self.loops_stopped += 1
            self.blocked_events.append(f"loop: {payload.get('command')}")
            raise PolicyViolation(
                tool_name,
                "Failproof stopped a repeated identical failing command.",
            )

        if self._attempts[key] >= 10:
            self.loops_stopped += 1
            self.blocked_events.append(f"loop: {tool_name}")
            raise PolicyViolation(
                tool_name,
                "Failproof stopped a repeated identical tool call.",
            )

    def _deny(self, tool_name: str, reason: str) -> None:
        self.blocks += 1
        self.blocked_events.append(reason)
        raise PolicyViolation(tool_name, reason)

    def _resolve(self, raw_path: str) -> Path:
        path = Path(raw_path)
        if not path.is_absolute():
            path = self.workspace_root / path
        return path.resolve()

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    @staticmethod
    def _key(tool_name: str, payload: dict[str, Any]) -> str:
        raw = json.dumps(payload, sort_keys=True, default=str)
        return f"{tool_name}:{hashlib.sha256(raw.encode()).hexdigest()}"

    @staticmethod
    def _preview(result: Any) -> str:
        if isinstance(result, str):
            value = result
        elif hasattr(result, "stdout"):
            value = _as_text(getattr(result, "stdout"))
        else:
            value = repr(result)
        return value if len(value) <= 500 else f"{value[:500]}..."

    def metrics(self) -> dict[str, Any]:
        return {
            "tool_calls": self.tool_calls,
            "blocks": self.blocks,
            "loops_stopped": self.loops_stopped,
            "session_id": self.session_id,
        }


def _as_text(value: Any) -> str:
    return "" if value is None else str(value)


def format_command(argv: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(argv)
    return " ".join(argv)
