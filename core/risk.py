from __future__ import annotations
import pandas as pd
from core.signal import Signal
from config import GLOBAL_HEAT_CAP, CORR_PENALTY, CORR_THRESHOLD


def apply_heat_limit(
    signals: list[Signal],
    current_heat: float,
    correlation_matrix: pd.DataFrame | None = None,
    max_heat_pct: float = GLOBAL_HEAT_CAP,
    corr_penalty: float = CORR_PENALTY,
) -> list[Signal]:
    """Filter signals to stay within heat cap. Penalise correlated pairs."""
    approved: list[Signal] = []
    heat_used = current_heat

    for sig in signals:
        effective_risk = sig.risk_pct

        if correlation_matrix is not None and not correlation_matrix.empty:
            for existing in approved:
                s1, s2 = sig.symbol, existing.symbol
                if s1 in correlation_matrix.index and s2 in correlation_matrix.columns:
                    corr = abs(float(correlation_matrix.loc[s1, s2]))
                    if corr > CORR_THRESHOLD:
                        effective_risk *= (1 + corr_penalty)
                        break

        if heat_used + effective_risk <= max_heat_pct:
            heat_used += effective_risk
            approved.append(sig)

    return approved
