from dataclasses import dataclass


@dataclass
class TxCost:
    commission_bps: float
    spread_bps: float
    slippage_bps: float

    @property
    def total_bps(self) -> float:
        return self.commission_bps + self.spread_bps + self.slippage_bps

    def round_trip_bps(self) -> float:
        return self.total_bps * 2


TX_COSTS: dict[str, TxCost] = {
    "th":        TxCost(commission_bps=15, spread_bps=5,  slippage_bps=5),
    "us":        TxCost(commission_bps=1,  spread_bps=2,  slippage_bps=3),
    "au":        TxCost(commission_bps=10, spread_bps=5,  slippage_bps=5),
    "crypto":    TxCost(commission_bps=10, spread_bps=5,  slippage_bps=10),
    "commodity": TxCost(commission_bps=5,  spread_bps=10, slippage_bps=10),
}


def cost_adjust_rr(entry: float, sl: float, tp: float, market: str) -> float:
    cost = TX_COSTS.get(market, TxCost(10, 5, 5))
    bps = cost.total_bps / 10_000
    adj_entry = entry * (1 + bps)
    adj_tp = tp * (1 - bps)
    sl_dist = adj_entry - sl
    tp_dist = adj_tp - adj_entry
    if sl_dist <= 0:
        return 0.0
    return tp_dist / sl_dist
