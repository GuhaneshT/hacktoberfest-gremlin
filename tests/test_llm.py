from __future__ import annotations

import pytest

from agent.llm import validate_test_code


def test_validate_accepts_a_plain_pytest_test() -> None:
    code = """
import pytest


def test_division_is_guarded():
    target = _load_target()
    with pytest.raises(ZeroDivisionError):
        target.divide(1, 0)
"""
    assert validate_test_code(code) is None


@pytest.mark.parametrize(
    "code, expected",
    [
        ("def helper():\n    return 1\n", "no test_ function"),
        (
            "import subprocess\n\ndef test_bad():\n    subprocess.run(['echo'])\n",
            "blocked module",
        ),
        (
            "def test_bad():\n    eval('1 + 1')\n",
            "blocked function",
        ),
        (
            "def test_bad():\n    open('target.py', 'w').write('x')\n",
            "opens a file for writing",
        ),
        (
            "def test_bad(tmp_path):\n    (tmp_path / 'x').write_text('x')\n",
            "blocked filesystem operation",
        ),
        (
            "def _load_target():\n    return None\n\ndef test_bad():\n    assert True\n",
            "must not redefine",
        ),
    ],
)
def test_validate_rejects_unsafe_or_invalid_tests(code: str, expected: str) -> None:
    error = validate_test_code(code)
    assert error is not None
    assert expected in error


def test_validate_rejects_test_without_a_test_function() -> None:
    assert validate_test_code("assert 1 + 1 == 2") is not None
