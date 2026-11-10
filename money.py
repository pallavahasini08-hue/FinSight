"""Money helpers. All amounts are stored as whole paise (integers).

Rs 10.10 is stored as 1010. Integers add up exactly, unlike decimals
(0.1 + 0.2 gives 0.30000000000000004 in floating point).
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

# Sanity cap on a single amount typed by the user (rupees).
MAX_RUPEES = Decimal("1000000000000")


def to_paise(value):
    """Turn user input like '1,234.50' into whole paise (123450).

    Raises ValueError for anything that is not a finite number in range.
    """
    if value is None:
        raise ValueError("missing amount")
    try:
        d = Decimal(str(value).strip().replace(",", ""))
    except InvalidOperation:
        raise ValueError("not a number")
    if not d.is_finite() or abs(d) > MAX_RUPEES:
        raise ValueError("amount out of range")
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def to_rupees(paise):
    """Paise -> rupees as a plain number, for chart data only."""
    paise = int(paise or 0)
    return paise // 100 if paise % 100 == 0 else paise / 100


def format_inr(paise):
    """Paise -> Indian-grouped rupees: 123456700 -> '12,34,567'.

    Shows paise only when there are some: 123450 -> '1,234.50'.
    Handles negative amounts.
    """
    paise = int(paise or 0)
    sign = "-" if paise < 0 else ""
    rupees, p = divmod(abs(paise), 100)
    s = str(rupees)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    if p:
        s += f".{p:02d}"
    return sign + s


def div_round(a, b):
    """a / b as a whole number, rounded half up, using exact integer maths.

    Returns 0 when b is 0 so callers never divide by zero.
    """
    if not b:
        return 0
    return (a * 2 + b) // (b * 2)


def pct(part, whole):
    """part as a whole-number percentage of whole (24000, 30000 -> 80)."""
    return div_round(part * 100, whole)