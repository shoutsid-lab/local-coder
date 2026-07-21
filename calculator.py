def divide(a: int | float, b: int | float) -> int | float:
    """
    Divide two numbers.

    Args:
        a (int | float): The dividend.
        b (int | float): The divisor.

    Returns:
        int | float: The result of the division.

    Raises:
        ValueError: If b is zero.
    """
    if b == 0:
        raise ValueError("Cannot divide by zero")
    return a / b
