from calculator import divide


def test_divide():
    assert divide(10, 2) == 5
    assert divide(10.0, 2.0) == 5.0
    assert divide(-10, 2) == -5
    assert divide(10, -2) == -5
    assert divide(-10, -2) == 5
    with pytest.raises(ValueError):
        divide(10, 0)
