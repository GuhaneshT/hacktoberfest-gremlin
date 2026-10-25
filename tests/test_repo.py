from __future__ import annotations

from pathlib import Path

import pytest

from agent.repo import TargetPathError, resolve_target_path, scan_repository


def test_scan_repository_finds_modules_and_skips_noise(tmp_path) -> None:
    root = tmp_path / "repo"
    package = root / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "service.py").write_text(
        "import os\n"
        "from typing import Any\n\n\n"
        "class Service:\n"
        "    pass\n\n\n"
        "def compute(a, b):\n"
        "    return a / b\n\n\n"
        "async def fetch(value):\n"
        "    return value\n\n\n"
        "def _private():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    (root / "tests" / "chaos").mkdir(parents=True)
    (root / "tests" / "test_service.py").write_text(
        "def test_example():\n    assert True\n", encoding="utf-8"
    )
    (root / "tests" / "chaos" / "test_generated.py").write_text(
        "def test_generated():\n    assert True\n", encoding="utf-8"
    )
    (root / "venv").mkdir()
    (root / "venv" / "ignored.py").write_text(
        "def ignored():\n    pass\n", encoding="utf-8"
    )
    (root / ".hidden").mkdir()
    (root / ".hidden" / "ignored.py").write_text(
        "def ignored():\n    pass\n", encoding="utf-8"
    )
    (root / "broken.py").write_text("def broken(\n", encoding="utf-8")

    index = scan_repository(root)

    assert {file.rel_path for file in index.files} == {
        "pkg/__init__.py",
        "pkg/service.py",
    }
    service = index.get("pkg/service.py")
    assert service is not None
    assert service.module == "pkg.service"
    assert {function.name for function in service.functions} == {"compute", "fetch"}
    assert service.classes == 1
    assert "os" in service.imports
    assert "typing" in service.imports
    assert any("broken.py" in item for item in index.skipped)
    assert "pkg/service.py" in index.prompt_map()
    assert "broken.py" not in index.prompt_map()


def test_resolve_target_path_enforces_allowed_root(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GREMLIN_TARGET_ROOT", str(tmp_path))
    monkeypatch.delenv("GREMLIN_ALLOW_ANY_TARGET", raising=False)

    inside = tmp_path / "inside.py"
    inside.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}_outside.py"
    outside.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    assert resolve_target_path(str(inside)) == inside.resolve()
    with pytest.raises(TargetPathError, match="outside the allowed roots"):
        resolve_target_path(str(outside))

    outside.unlink()


def test_resolve_target_path_rejects_missing_path(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GREMLIN_TARGET_ROOT", str(tmp_path))

    with pytest.raises(TargetPathError, match="does not exist"):
        resolve_target_path(str(tmp_path / "missing.py"))


def test_resolve_target_path_rejects_non_python_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GREMLIN_TARGET_ROOT", str(tmp_path))
    text_file = tmp_path / "notes.txt"
    text_file.write_text("not python", encoding="utf-8")

    with pytest.raises(TargetPathError, match="must be a Python file"):
        resolve_target_path(str(text_file))


def test_module_metadata_for_package_init(tmp_path) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")

    index = scan_repository(Path(tmp_path))

    init = index.get("pkg/__init__.py")
    assert init is not None
    assert init.module == "pkg"
