from __future__ import annotations

import ast
import hashlib
import io
import os
import tokenize
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_EXCLUDED_DIRS = {
    "build",
    "dist",
    "node_modules",
    "site-packages",
    "vendor",
    "__pycache__",
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    ".tox",
    ".nox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".idea",
    ".vscode",
    ".next",
    "coverage",
    "htmlcov",
}


class TargetPathError(ValueError):
    """Raised when a target path is missing, unsafe, or unsupported."""


@dataclass(frozen=True)
class RepoFunction:
    name: str
    args: tuple[str, ...]
    has_division: bool
    is_async: bool


@dataclass(frozen=True)
class RepoFile:
    path: Path
    rel_path: str
    module: str | None
    functions: tuple[RepoFunction, ...]
    classes: int
    line_count: int
    imports: tuple[str, ...]
    digest: str


@dataclass(frozen=True)
class RepoIndex:
    root: Path
    files: tuple[RepoFile, ...]
    skipped: tuple[str, ...]
    scan_truncated: bool = False

    @property
    def total_functions(self) -> int:
        return sum(len(file.functions) for file in self.files)

    def get(self, rel_path: str) -> RepoFile | None:
        normalized = rel_path.replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        for file in self.files:
            if file.rel_path == normalized:
                return file
        return None

    def summary(self) -> str:
        root_name = self.root.name or str(self.root)
        summary = (
            f"Repository '{root_name}' with {len(self.files)} Python module(s) "
            f"and {self.total_functions} public function(s)."
        )
        if self.scan_truncated:
            summary += " The scan was capped at the configured file limit."
        if self.skipped:
            summary += f" Skipped {len(self.skipped)} unreadable or invalid file(s)."
        return summary

    def prompt_map(self, *, max_files: int = 180, max_chars: int = 16000) -> str:
        ranked = sorted(
            self.files,
            key=lambda file: (
                any(function.has_division for function in file.functions),
                len(file.functions),
                file.rel_path,
            ),
            reverse=True,
        )

        lines: list[str] = []
        used = 0
        omitted = 0
        for file in ranked:
            functions = "; ".join(
                _format_function(function) for function in file.functions[:12]
            )
            if len(file.functions) > 12:
                functions += f"; ... (+{len(file.functions) - 12} more)"
            imports = ", ".join(file.imports[:8]) or "-"
            module = file.module or "<not-importable>"
            line = (
                f"- path={file.rel_path} | module={module} | "
                f"classes={file.classes} | functions={functions or '-'} | "
                f"imports={imports}"
            )
            if len(lines) >= max_files or used + len(line) + 1 > max_chars:
                omitted = len(ranked) - len(lines)
                break
            lines.append(line)
            used += len(line) + 1

        if omitted:
            lines.append(f"- ... {omitted} lower-priority module(s) omitted")
        return "\n".join(lines)

    def read_source(self, file: RepoFile, *, max_chars: int = 16000) -> str:
        with tokenize.open(file.path) as handle:
            text = handle.read()
        if len(text) > max_chars:
            return text[:max_chars] + "\n# ... source truncated ..."
        return text

    def snapshot(self) -> dict[str, str]:
        return {file.rel_path: file.digest for file in self.files}

    def current_snapshot(self) -> dict[str, str]:
        snapshot: dict[str, str] = {}
        for file in self.files:
            try:
                snapshot[file.rel_path] = hashlib.sha256(
                    file.path.read_bytes()
                ).hexdigest()
            except OSError:
                snapshot[file.rel_path] = "<missing>"
        return snapshot


def resolve_target_path(raw_path: str) -> Path:
    raw_path = raw_path.strip()
    if not raw_path:
        raise TargetPathError("target_path cannot be empty.")

    target = Path(raw_path).expanduser()
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    target = target.resolve()

    if not target.exists():
        raise TargetPathError(f"Target path does not exist: {target}")

    roots = _allowed_target_roots()
    allow_any = os.getenv("GREMLIN_ALLOW_ANY_TARGET", "0") == "1"
    if not allow_any and not any(_is_within(target, root) for root in roots):
        joined = os.pathsep.join(str(root) for root in roots)
        raise TargetPathError(
            "target_path is outside the allowed roots. "
            f"Allowed: {joined}. Set GREMLIN_TARGET_ROOT to add a root."
        )

    if target.is_dir():
        return target
    if not target.is_file() or target.suffix != ".py":
        raise TargetPathError("target_path must be a Python file or a directory.")
    return target


def scan_repository(root: Path) -> RepoIndex:
    root = root.resolve()
    max_files = _env_int("GREMLIN_MAX_SCAN_FILES", 4000)
    max_file_bytes = _env_int("GREMLIN_MAX_TARGET_FILE_BYTES", 1_500_000)

    files: list[RepoFile] = []
    skipped: list[str] = []
    truncated = False

    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        if len(files) >= max_files:
            truncated = True
            break

        current = Path(directory)
        rel_dir = current.relative_to(root)
        kept_dirs: list[str] = []
        for dirname in sorted(dirnames):
            child = current / dirname
            rel_child = rel_dir / dirname
            normalized = rel_child.as_posix()
            if (
                dirname.startswith(".")
                or dirname in _EXCLUDED_DIRS
                or dirname.endswith(".egg-info")
                or normalized == "tests/chaos"
                or child.is_symlink()
            ):
                continue
            kept_dirs.append(dirname)
        dirnames[:] = kept_dirs

        for filename in sorted(filenames):
            if len(files) >= max_files:
                truncated = True
                break
            if not filename.endswith(".py"):
                continue
            if filename == "conftest.py" or filename.startswith("test_"):
                continue
            if filename.endswith("_test.py"):
                continue

            path = current / filename
            if path.is_symlink():
                continue
            rel_path = path.relative_to(root).as_posix()
            parsed, reason = _parse_repo_file(path, rel_path, max_file_bytes)
            if parsed is None:
                if reason and len(skipped) < 50:
                    skipped.append(f"{rel_path}: {reason}")
                continue
            files.append(parsed)

    files.sort(key=lambda file: file.rel_path)
    return RepoIndex(
        root=root,
        files=tuple(files),
        skipped=tuple(skipped),
        scan_truncated=truncated,
    )


def _parse_repo_file(
    path: Path, rel_path: str, max_file_bytes: int
) -> tuple[RepoFile | None, str | None]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        return None, f"unreadable ({type(exc).__name__})"

    if len(data) > max_file_bytes:
        return None, f"larger than {max_file_bytes} bytes"
    if b"\x00" in data:
        return None, "binary content"

    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
        text = data.decode(encoding)
    except (SyntaxError, UnicodeDecodeError, LookupError) as exc:
        return None, f"encoding error ({type(exc).__name__})"

    try:
        tree = ast.parse(text, filename=rel_path)
    except SyntaxError as exc:
        return None, f"syntax error on line {exc.lineno}"

    functions: list[RepoFunction] = []
    classes = 0
    imports: list[str] = []

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            classes += 1
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.append(node.module)
            else:
                imports.extend(alias.name for alias in node.names)
            continue
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
            RepoFunction(
                name=node.name,
                args=tuple(args),
                has_division=has_division,
                is_async=isinstance(node, ast.AsyncFunctionDef),
            )
        )

    return (
        RepoFile(
            path=path.resolve(),
            rel_path=rel_path,
            module=_module_name(Path(rel_path)),
            functions=tuple(functions),
            classes=classes,
            line_count=text.count("\n") + 1,
            imports=tuple(dict.fromkeys(imports)),
            digest=hashlib.sha256(data).hexdigest(),
        ),
        None,
    )


def _module_name(rel_path: Path) -> str | None:
    if rel_path.name == "__init__.py":
        parts = rel_path.parent.parts
    else:
        parts = rel_path.with_suffix("").parts
    if not parts or not all(part.isidentifier() for part in parts):
        return None
    return ".".join(parts)


def _format_function(function: RepoFunction) -> str:
    suffix = ""
    if function.has_division:
        suffix += " [division]"
    if function.is_async:
        suffix += " [async]"
    return f"{function.name}({', '.join(function.args)}){suffix}"


def _allowed_target_roots() -> list[Path]:
    raw = os.getenv("GREMLIN_TARGET_ROOT", "").strip()
    if not raw:
        return [PROJECT_ROOT]

    roots: list[Path] = []
    for part in raw.split(os.pathsep):
        part = part.strip()
        if not part:
            continue
        root = Path(part).expanduser()
        if not root.is_absolute():
            root = PROJECT_ROOT / root
        roots.append(root.resolve())
    return roots or [PROJECT_ROOT]


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default
