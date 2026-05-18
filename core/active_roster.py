from __future__ import annotations

import json
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ACTIVE_ROSTER_PATH = ROOT / "config" / "active_roster.json"


def active_roster_path() -> Path:
    return ACTIVE_ROSTER_PATH


def normalize_active_roster_payload(raw: dict | None) -> dict:
    payload = raw if isinstance(raw, dict) else {}
    markets = payload.get("markets")
    normalized_markets: dict[str, dict] = {}
    if isinstance(markets, dict):
        for market, row in markets.items():
            key = str(market).strip().lower()
            if not key or not isinstance(row, dict):
                continue
            strategies = row.get("strategies")
            normalized_markets[key] = {
                "generated": row.get("generated"),
                "strategies": strategies if isinstance(strategies, list) else [],
            }
    return {
        "generated": payload.get("generated"),
        "markets": normalized_markets,
    }


def load_active_roster_payload(path: Path | None = None) -> dict:
    roster_path = path or ACTIVE_ROSTER_PATH
    if not roster_path.exists():
        return normalize_active_roster_payload({})
    return normalize_active_roster_payload(json.loads(roster_path.read_text(encoding="utf-8")))


def write_active_roster_snapshot(
    market: str,
    as_of: date,
    strategies: list[dict],
    path: Path | None = None,
) -> dict:
    roster_path = path or ACTIVE_ROSTER_PATH
    payload = load_active_roster_payload(roster_path)
    market_key = market.strip().lower()
    payload["generated"] = str(as_of)
    payload["markets"][market_key] = {
        "generated": str(as_of),
        "strategies": [
            {
                "strategy": str(row.get("strategy") or ""),
                "regime": str(row.get("regime") or ""),
                "source_combo": str(row.get("best_combo") or row.get("source_combo") or ""),
                "params": row.get("params") or {},
                "is_calmar": float(row.get("is_calmar") or 0.0),
            }
            for row in strategies
        ],
    }
    roster_path.parent.mkdir(parents=True, exist_ok=True)
    roster_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return payload


def _active_roster_key(row: dict) -> tuple[str, str]:
    return str(row.get("strategy") or ""), str(row.get("regime") or "")


def _active_roster_signature(row: dict) -> tuple[str, str]:
    combo = str(row.get("source_combo") or row.get("best_combo") or "")
    params = json.dumps(row.get("params") or {}, sort_keys=True, default=str)
    return combo, params


def diff_active_roster(current_rows: list[dict], desired_rows: list[dict]) -> dict[str, list[dict]]:
    current_map = {_active_roster_key(row): row for row in current_rows}
    desired_map = {_active_roster_key(row): row for row in desired_rows}

    added: list[dict] = []
    changed: list[dict] = []
    retired: list[dict] = []
    unchanged: list[dict] = []

    for key in sorted(desired_map):
        desired = desired_map[key]
        current = current_map.get(key)
        strategy, regime = key
        if current is None:
            added.append({
                "strategy": strategy,
                "regime": regime,
                "new_combo": str(desired.get("source_combo") or desired.get("best_combo") or ""),
                "params": desired.get("params") or {},
                "new_calmar": desired.get("is_calmar"),
            })
            continue
        if _active_roster_signature(current) == _active_roster_signature(desired):
            unchanged.append({"strategy": strategy, "regime": regime})
            continue
        changed.append({
            "strategy": strategy,
            "regime": regime,
            "old_combo": str(current.get("source_combo") or current.get("best_combo") or ""),
            "new_combo": str(desired.get("source_combo") or desired.get("best_combo") or ""),
            "params": desired.get("params") or {},
            "new_calmar": desired.get("is_calmar"),
        })

    for key in sorted(current_map):
        if key in desired_map:
            continue
        current = current_map[key]
        strategy, regime = key
        retired.append({
            "strategy": strategy,
            "regime": regime,
            "old_combo": str(current.get("source_combo") or current.get("best_combo") or ""),
        })

    return {
        "added": added,
        "changed": changed,
        "retired": retired,
        "unchanged": unchanged,
    }


def sync_active_roster_payload_to_db(
    payload: dict,
    *,
    as_of: date | None = None,
    source: str = "git-json",
    record_noop: bool = False,
    market_filter: list[str] | str | None = None,
) -> dict[str, dict]:
    from db.models import ActiveStrategyModel, PipelineLog, SessionLocal

    normalized = normalize_active_roster_payload(payload)
    if isinstance(market_filter, str):
        allowed = {market_filter.strip().lower()}
    elif market_filter is None:
        allowed = set(normalized["markets"].keys())
    else:
        allowed = {str(m).strip().lower() for m in market_filter if str(m).strip()}

    run_date = as_of or date.today()
    results: dict[str, dict] = {}
    if not allowed:
        return results

    db = SessionLocal()
    try:
        for market in sorted(allowed):
            market_payload = normalized["markets"].get(market)
            if market_payload is None:
                continue

            all_rows = (
                db.query(ActiveStrategyModel)
                .filter_by(market=market)
                .order_by(ActiveStrategyModel.id.desc())
                .all()
            )
            current_rows = (
                db.query(ActiveStrategyModel)
                .filter_by(market=market, status="active")
                .order_by(ActiveStrategyModel.strategy, ActiveStrategyModel.regime)
                .all()
            )
            same_day_rows = {
                (row.strategy, row.regime): row
                for row in all_rows
                if row.promoted_at == run_date
            }
            current_payload = [
                {
                    "strategy": row.strategy,
                    "regime": row.regime,
                    "params": row.params or {},
                    "source_combo": row.source_combo or "",
                }
                for row in current_rows
            ]
            desired_payload = [
                {
                    "strategy": str(row.get("strategy") or ""),
                    "regime": str(row.get("regime") or ""),
                    "params": row.get("params") or {},
                    "source_combo": str(row.get("source_combo") or row.get("best_combo") or ""),
                    "is_calmar": float(row.get("is_calmar") or 0.0),
                }
                for row in market_payload.get("strategies") or []
            ]

            diff = diff_active_roster(current_payload, desired_payload)
            desired_map = {_active_roster_key(row): row for row in desired_payload}
            touched_keys = {
                (row["strategy"], row["regime"])
                for row in (diff["changed"] + diff["retired"])
            }
            for row in current_rows:
                key = (row.strategy, row.regime)
                if key in touched_keys:
                    row.status = "retired"
                    row.retired_at = run_date

            for row in diff["added"] + diff["changed"]:
                key = (row["strategy"], row["regime"])
                desired = desired_map[key]
                existing_same_day = same_day_rows.get(key)
                if existing_same_day is not None:
                    existing_same_day.params = desired.get("params") or {}
                    existing_same_day.promoted_at = run_date
                    existing_same_day.status = "active"
                    existing_same_day.retired_at = None
                    existing_same_day.source_combo = str(desired.get("source_combo") or "")
                else:
                    db.add(ActiveStrategyModel(
                        market=market,
                        strategy=row["strategy"],
                        regime=row["regime"],
                        params=desired.get("params") or {},
                        promoted_at=run_date,
                        status="active",
                        source_combo=str(desired.get("source_combo") or ""),
                    ))

            active_count = len(desired_payload)
            if diff["added"] or diff["changed"] or diff["retired"] or record_noop:
                db.add(PipelineLog(
                    market=market,
                    stage="active-sync",
                    outcome="ok",
                    details={
                        "source": source,
                        "active_count": active_count,
                        "added": diff["added"],
                        "changed": diff["changed"],
                        "retired": diff["retired"],
                    },
                ))

            results[market] = {
                "market": market,
                "active_count": active_count,
                "added": diff["added"],
                "changed": diff["changed"],
                "retired": diff["retired"],
                "unchanged": diff["unchanged"],
            }
        db.commit()
    finally:
        db.close()
    return results


def sync_active_roster_file_to_db(
    *,
    path: Path | None = None,
    as_of: date | None = None,
    source: str = "git-json",
    record_noop: bool = False,
    market_filter: list[str] | str | None = None,
) -> dict[str, dict]:
    payload = load_active_roster_payload(path)
    return sync_active_roster_payload_to_db(
        payload,
        as_of=as_of,
        source=source,
        record_noop=record_noop,
        market_filter=market_filter,
    )
