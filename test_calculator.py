import pytest

from calculator import divide


def test_positive_integer_division():
    result = divide(10, 2)
    assert result == 5.0
    assert isinstance(result, float)

def test_float_division():
    result = divide(10.0, 2.0)
    assert result == 5.0
    assert isinstance(result, float)

def test_negative_division():
    result = divide(-10, 2)
    assert result == -5.0
    assert isinstance(result, float)

def test_zero_dividend():
    result = divide(0, 2)
    assert result == 0.0
    assert isinstance(result, float)

def test_zero_divisor():
    with pytest.raises(ValueError):
        divide(10, 0)
