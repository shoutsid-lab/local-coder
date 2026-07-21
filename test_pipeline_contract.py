import ast
from pathlib import Path
from typing import get_type_hints

import pytest

from calculator import divide


EXPECTED_DOCSTRING = (
    "Return a divided by b, raising ValueError when b is zero."
)

EXPECTED_TEST_NAMES = [
    "test_positive_integer_division",
    "test_float_division",
    "test_negative_division",
    "test_zero_dividend",
    "test_zero_divisor",
]


def test_divide_annotations_match_contract() -> None:
    hints = get_type_hints(divide)

    assert hints["a"] == int | float
    assert hints["b"] == int | float
    assert hints["return"] is float


def test_divide_docstring_matches_contract() -> None:
    assert divide.__doc__ == EXPECTED_DOCSTRING


def test_integer_division_returns_float() -> None:
    result = divide(10, 2)

    assert result == 5.0
    assert isinstance(result, float)


def test_zero_dividend_is_valid() -> None:
    result = divide(0, 2)

    assert result == 0.0
    assert isinstance(result, float)


def test_zero_divisor_raises_expected_error() -> None:
    with pytest.raises(ValueError, match="^Cannot divide by zero$"):
        divide(10, 0)


def test_editable_test_file_has_expected_tests() -> None:
    source = Path("test_calculator.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    names = [
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name.startswith("test_")
    ]

    assert names == EXPECTED_TEST_NAMES
