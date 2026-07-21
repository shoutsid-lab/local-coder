def divide(a: int | float, b: int | float) -> float:
    """Return a divided by b, raising ValueError when b is zero."""
    if a == 0:
        raise ValueError("Cannot divide by zero")
    return a / b
