from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Optional
from core.signal import Signal


class RiskPolicy(ABC):
    id: str

    @abstractmethod
    def size(self, capital: float, signal: Signal, params: dict, ledger=None) -> int:
        """Return position size in shares/lots. 0 = skip trade."""

    @abstractmethod
    def approve(
        self, signal: Signal, capital: float, current_heat: float, params: dict
    ) -> bool:
        """Return True if heat limits allow this trade."""


class ATRFixedFractional(RiskPolicy):
    """risk_amount = capital × risk_pct; size = risk_amount / sl_distance, rounded to lot."""
    id = "atr_fixed_fractional"

    def size(self, capital: float, signal: Signal, params: dict, ledger=None) -> int:
        from config import MARKET_CONFIGS
        cfg = MARKET_CONFIGS.get(signal.market)
        lot_size = cfg.lot_size if cfg else 1
        min_val = cfg.min_position_value if cfg else 0
        risk_pct = params.get("risk_pct", signal.risk_pct)
        risk_amount = capital * risk_pct
        sl_distance = abs(signal.entry - signal.sl)
        if sl_distance == 0:
            return 0
        raw = risk_amount / sl_distance
        lots = int(raw // lot_size) * lot_size
        if lots > 0 and lots * signal.entry < min_val:
            return 0
        return lots

    def approve(
        self, signal: Signal, capital: float, current_heat: float, params: dict
    ) -> bool:
        from config import MARKET_CONFIGS
        cfg = MARKET_CONFIGS.get(signal.market)
        max_heat = cfg.max_heat_pct if cfg else 0.06
        risk_pct = params.get("risk_pct", signal.risk_pct)
        return current_heat + risk_pct <= max_heat


RISK_POLICIES: dict[str, RiskPolicy] = {
    ATRFixedFractional.id: ATRFixedFractional(),
}


def get_risk_policy(policy_id: str = "atr_fixed_fractional") -> RiskPolicy:
    if policy_id not in RISK_POLICIES:
        raise KeyError(f"Unknown risk policy '{policy_id}'. Available: {list(RISK_POLICIES)}")
    return RISK_POLICIES[policy_id]
