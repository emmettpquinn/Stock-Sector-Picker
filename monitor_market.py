import json
import os
import time
from datetime import datetime, timezone

from sectors import us_sector_etfs
from trading_212_api import FALLBACK_MARKET_TICKERS, Trading212API
from vv_signals import VectorVestSignalMonitor


def _compact_snapshot(snapshot):
    output = {
        "timestamp_utc": snapshot.get("timestamp_utc"),
        "order_status": snapshot.get("order_status"),
        "triggered": snapshot.get("triggered"),
        "vectorvest_statuses": snapshot.get("vectorvest_statuses"),
    }

    if snapshot.get("message"):
        output["message"] = snapshot["message"]

    if snapshot.get("trading_environment"):
        output["trading_environment"] = snapshot["trading_environment"]

    sector_pick = snapshot.get("sector_pick")
    if sector_pick:
        output["sector_pick"] = {
            "sector": sector_pick.get("sector"),
            "ticker": sector_pick.get("ticker"),
            "confidence": sector_pick.get("confidence"),
            "rationale": sector_pick.get("rationale"),
        }

    trade_execution = snapshot.get("trade_execution")
    if trade_execution:
        compact_trade = {
            "action": trade_execution.get("action"),
            "sold_ticker": trade_execution.get("sold_ticker"),
            "bought_ticker": trade_execution.get("bought_ticker"),
            "quantity": trade_execution.get("quantity"),
            "available_capital": trade_execution.get("available_capital"),
        }

        buy_result = trade_execution.get("buy_result") or {}
        if isinstance(buy_result, dict):
            compact_trade["order"] = {
                "id": buy_result.get("id"),
                "status": buy_result.get("status"),
                "ticker": buy_result.get("ticker"),
                "side": buy_result.get("side"),
                "filledQuantity": buy_result.get("filledQuantity"),
                "createdAt": buy_result.get("createdAt"),
            }

        output["trade_execution"] = compact_trade

    if snapshot.get("resolved_trade_target"):
        output["resolved_trade_target"] = snapshot["resolved_trade_target"]

    if snapshot.get("excluded_untradable_tickers"):
        output["excluded_untradable_tickers"] = snapshot["excluded_untradable_tickers"]

    return output


def monitor_market(investor, poll_seconds=900, max_cycles=None):
    signal_monitor = VectorVestSignalMonitor()
    trading_api = None
    cycle = 0

    while True:
        cycle += 1
        verbose_output = (os.getenv("OUTPUT_VERBOSE") or "false").strip().lower() == "true"
        signal_snapshot = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "order_status": "not_started",
        }
        try:
            previous_statuses = signal_monitor.load_previous_statuses()
            current_statuses = signal_monitor.fetch_statuses()
            should_trigger = signal_monitor.statuses_changed(current_statuses, previous_statuses)

            signal_snapshot["vectorvest_statuses"] = current_statuses
            signal_snapshot["triggered"] = should_trigger

            if should_trigger:
                if trading_api is None:
                    trading_api = Trading212API()

                signal_snapshot["trading_environment"] = trading_api.environment
                excluded_tickers = []
                pick = None
                resolved_symbol = None
                resolved_price = None
                last_pick_error = None

                for _ in range(3):
                    try:
                        pick = investor.get_best_sector(excluded_tickers=excluded_tickers)
                    except ValueError as exc:
                        last_pick_error = str(exc)
                        pick = None
                        continue

                    try:
                        resolved_symbol, resolved_price = trading_api.resolve_trade_target(
                            pick["ticker"]
                        )
                        break
                    except RuntimeError as exc:
                        last_pick_error = str(exc)
                        excluded_tickers.append(pick["ticker"])

                signal_snapshot["excluded_untradable_tickers"] = excluded_tickers

                if resolved_symbol is None:
                    fallback_pick = None
                    for sector_name, ticker in us_sector_etfs.items():
                        if ticker in excluded_tickers:
                            continue
                        try:
                            resolved_symbol, resolved_price = trading_api.resolve_trade_target(
                                ticker
                            )
                            fallback_pick = {
                                "sector": sector_name,
                                "ticker": ticker,
                                "confidence": 0,
                                "rationale": "Fallback selected because initial picks were not tradable.",
                            }
                            break
                        except RuntimeError:
                            excluded_tickers.append(ticker)

                    signal_snapshot["excluded_untradable_tickers"] = excluded_tickers

                    if fallback_pick is None:
                        fallback_target = trading_api.resolve_first_tradable(
                            FALLBACK_MARKET_TICKERS
                        )
                        if fallback_target is None:
                            raise RuntimeError(
                                "Unable to find a tradable sector pick after retries. "
                                + (last_pick_error or "")
                            )

                        pick = {
                            "sector": "Fallback Market ETF",
                            "ticker": fallback_target["input_ticker"],
                            "confidence": 0,
                            "rationale": "Sector tickers were not tradable in current environment.",
                        }
                        resolved_symbol = fallback_target["resolved_symbol"]
                        resolved_price = fallback_target["price"]
                    else:
                        pick = fallback_pick

                signal_snapshot["excluded_untradable_tickers"] = excluded_tickers

                signal_snapshot["sector_pick"] = pick
                signal_snapshot["resolved_trade_target"] = {
                    "input_ticker": pick["ticker"],
                    "resolved_symbol": resolved_symbol,
                    "price": resolved_price,
                }

                trade_execution = trading_api.rebalance_to_ticker(pick["ticker"])
                signal_snapshot["trade_execution"] = trade_execution
                action = trade_execution.get("action", "submitted")
                signal_snapshot["order_status"] = (
                    f"{trading_api.environment.lower()}_{action}"
                )
                signal_monitor.save_statuses(current_statuses)
            else:
                signal_snapshot["order_status"] = "not_triggered"
                signal_snapshot["message"] = "No signal change detected. Skipping sector search."

            # Log a compact summary unless verbose output is explicitly enabled.
            print(json.dumps(signal_snapshot if verbose_output else _compact_snapshot(signal_snapshot), indent=4))

        except Exception as e:
            error_payload = {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "error": str(e),
                "order_status": "failed",
                "snapshot": signal_snapshot if verbose_output else _compact_snapshot(signal_snapshot),
            }
            print(json.dumps(error_payload, indent=4))

        if max_cycles and cycle >= max_cycles:
            break

        time.sleep(poll_seconds)
