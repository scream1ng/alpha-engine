def set_tick(price: float) -> float:
    """Return minimum tick size for a given price (SET tick table; 0.01 fallback)."""
    if price < 2:
        return 0.01
    if price < 5:
        return 0.02
    if price < 10:
        return 0.05
    if price < 25:
        return 0.10
    if price < 100:
        return 0.25
    if price < 200:
        return 0.50
    return 1.00