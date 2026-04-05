import json
import time
from datetime import datetime, timezone

from trading_212_api import Trading212API
from vv_signals import VectorVestSignalMonitor


def monitor_market(investor, poll_seconds=900, max_cycles=None):
    signal_monitor = VectorVestSignalMonitor()
    trading_api = None
    cycle = 0

    while True:
        cycle += 1
        try:
            previous_statuses = signal_monitor.load_previous_statuses()
            current_statuses = signal_monitor.fetch_statuses()
            should_trigger = signal_monitor.statuses_changed(current_statuses, previous_statuses)

            signal_snapshot = {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "vectorvest_statuses": current_statuses,
                "triggered": should_trigger,
            }

            if should_trigger:
                pick = investor.get_best_sector()
                signal_snapshot["sector_pick"] = pick
                if trading_api is None:
                    trading_api = Trading212API()
                signal_snapshot["trade_execution"] = trading_api.rebalance_to_ticker(
                    pick["ticker"]
                )
                signal_monitor.save_statuses(current_statuses)
            else:
                signal_snapshot["message"] = "No signal change detected. Skipping sector search."

            print(json.dumps(signal_snapshot, indent=2))
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                        "error": str(exc),
                    },
                    indent=2,
                )
            )

        if max_cycles is not None and cycle >= max_cycles:
            break
        time.sleep(poll_seconds)