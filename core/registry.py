from __future__ import annotations


class StrategyRegistry:
    _strategies: dict[str, type] = {}

    @classmethod
    def register(cls, strategy_cls: type) -> type:
        cls._strategies[strategy_cls.id] = strategy_cls
        return strategy_cls

    @classmethod
    def get(cls, strategy_id: str) -> type:
        if strategy_id not in cls._strategies:
            raise KeyError(
                f"Strategy '{strategy_id}' not registered. "
                f"Available: {list(cls._strategies)}"
            )
        return cls._strategies[strategy_id]

    @classmethod
    def all(cls) -> dict[str, type]:
        return dict(cls._strategies)

    @classmethod
    def for_market(cls, market_id: str) -> dict[str, type]:
        from config import MARKET_CONFIGS
        cfg = MARKET_CONFIGS.get(market_id)
        if not cfg:
            return {}
        return {
            sid: s
            for sid, s in cls._strategies.items()
            if sid in cfg.enabled_strategies
        }

    @classmethod
    def instances_for_market(cls, market_id: str) -> dict[str, object]:
        return {sid: klass() for sid, klass in cls.for_market(market_id).items()}
