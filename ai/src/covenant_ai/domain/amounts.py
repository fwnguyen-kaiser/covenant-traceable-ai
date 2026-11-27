"""Exact finite financial inputs at text and JSON boundaries."""
import re
from decimal import Decimal

NUMBER = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"

def parse_amount(value: object) -> Decimal:
    if not isinstance(value, (str, int, Decimal)) or isinstance(value, bool):
        raise ValueError("Use an exact financial amount as a decimal string or integer.")
    if isinstance(value, Decimal):
        if value.is_finite():
            return value
        raise ValueError("A finite amount is required.")
    text = str(value).strip()
    if not re.fullmatch(NUMBER, text):
        raise ValueError(f"Invalid amount or thousands grouping: {text!r}")
    return Decimal(text.replace(",", ""))
