# On Signal Change, sell the current sector ETF and buy the new one

import json
import os
from pathlib import Path
from datetime import datetime, timezone

import requests
from sectors import us_sector_etfs


class Trading212API:
    def __init__(self, timeout=15, state_file="sample_response.json", order_quantity=1):
        self.api_key = os.getenv("TRADING212_API_KEY")
        self.account_id = os.getenv("TRADING212_ACCOUNT_ID")
        self.base_url = "https://api.trading212.com/api/v1"
        self.timeout = timeout
        self.state_file = Path(__file__).resolve().parent / state_file

        env_quantity = os.getenv("TRADING212_ORDER_QUANTITY")
        raw_quantity = int(env_quantity) if env_quantity else order_quantity
        self.order_quantity = self._validate_quantity(raw_quantity)

        if not self.api_key:
            raise RuntimeError("TRADING212_API_KEY is missing")
        if not self.account_id:
            raise RuntimeError("TRADING212_ACCOUNT_ID is missing")

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _load_state(self):
        if not self.state_file.exists():
            raise RuntimeError(f"State file is missing: {self.state_file}")

        try:
            with self.state_file.open("r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"State file is not valid JSON: {self.state_file}") from exc

    def _save_state(self, state_payload):
        with self.state_file.open("w", encoding="utf-8") as f:
            json.dump(state_payload, f, indent=2)

    @staticmethod
    def _validate_quantity(quantity):
        try:
            normalized = int(quantity)
        except (TypeError, ValueError) as exc:
            raise ValueError("Order quantity must be an integer") from exc

        if normalized <= 0:
            raise ValueError("Order quantity must be greater than 0")

        return normalized

    @staticmethod
    def _current_ticker_from_payload(state_payload):
        current_sector = state_payload.get("current_sector")
        if not current_sector:
            raise ValueError("Current sector not found in sample_response.json")

        current_ticker = us_sector_etfs.get(current_sector)
        if not current_ticker:
            raise ValueError(
                f"Ticker for current sector '{current_sector}' not found in us_sector_etfs"
            )

        return current_ticker

    @staticmethod
    def _sector_from_ticker(ticker):
        for sector_name, sector_ticker in us_sector_etfs.items():
            if sector_ticker == ticker:
                return sector_name
        return None

    @staticmethod
    def _response_payload(response):
        if not response.content:
            return {}

        content_type = response.headers.get("Content-Type", "")
        if "json" in content_type.lower():
            try:
                return response.json()
            except ValueError:
                return {"raw_response": response.text}
        return {"raw_response": response.text}

    def sell_sector_etf(self, ticker, quantity=None):
        order_quantity = self._validate_quantity(
            self.order_quantity if quantity is None else quantity
        )

        # Place a sell order for the current sector ETF
        sell_url = f"{self.base_url}/accounts/{self.account_id}/orders"
        sell_payload = {
            "type": "MARKET",
            "instrument": ticker,
            "quantity": order_quantity,
            "side": "SELL"
        }
        response = requests.post(
            sell_url,
            headers=self._headers(),
            json=sell_payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return self._response_payload(response)
        
    def buy_sector_etf(self, ticker, quantity=None):
        order_quantity = self._validate_quantity(
            self.order_quantity if quantity is None else quantity
        )

        # Place a buy order for the new sector ETF
        buy_url = f"{self.base_url}/accounts/{self.account_id}/orders"
        buy_payload = {
            "type": "MARKET",
            "instrument": ticker,
            "quantity": order_quantity,
            "side": "BUY"
        }
        response = requests.post(
            buy_url,
            headers=self._headers(),
            json=buy_payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return self._response_payload(response)

    def _resume_pending_rebalance(self, pending_rebalance, expected_new_ticker):
        pending_target = pending_rebalance.get("to_ticker")
        quantity = self._validate_quantity(
            pending_rebalance.get("quantity") or self.order_quantity
        )
        sell_result = pending_rebalance.get("sell_result", {})
        if pending_target != expected_new_ticker:
            # The previous cycle already sold; buy the latest signal ticker to avoid a re-sell loop.
            buy_result = self.buy_sector_etf(expected_new_ticker, quantity=quantity)
            action = "resumed_rebalance_to_latest_signal"
        else:
            buy_result = self.buy_sector_etf(expected_new_ticker, quantity=quantity)
            action = "resumed_rebalance"

        state_payload = self._load_state()
        new_sector = self._sector_from_ticker(expected_new_ticker)
        state_payload["current_sector"] = new_sector
        state_payload.pop("pending_rebalance", None)
        self._save_state(state_payload)

        return {
            "action": action,
            "sold_ticker": pending_rebalance.get("from_ticker"),
            "bought_ticker": expected_new_ticker,
            "quantity": quantity,
            "original_pending_target": pending_target,
            "sell_result": sell_result,
            "buy_result": buy_result,
        }

    def rebalance_to_ticker(self, new_ticker):
        new_sector = self._sector_from_ticker(new_ticker)
        if not new_sector:
            raise ValueError(f"Ticker '{new_ticker}' is not a supported sector ETF")

        state_payload = self._load_state()
        pending_rebalance = state_payload.get("pending_rebalance")
        if pending_rebalance:
            return self._resume_pending_rebalance(pending_rebalance, new_ticker)

        current_ticker = self._current_ticker_from_payload(state_payload)
        if current_ticker == new_ticker:
            return {
                "action": "noop",
                "reason": "Current holding already matches requested ticker",
                "current_ticker": current_ticker,
                "new_ticker": new_ticker,
            }

        sell_result = self.sell_sector_etf(current_ticker)
        state_payload["pending_rebalance"] = {
            "from_ticker": current_ticker,
            "to_ticker": new_ticker,
            "quantity": self.order_quantity,
            "sell_result": sell_result,
            "started_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        self._save_state(state_payload)

        try:
            buy_result = self.buy_sector_etf(new_ticker)
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Sell order for '{current_ticker}' succeeded, but buy order for '{new_ticker}' failed"
            ) from exc

        state_payload = self._load_state()
        state_payload["current_sector"] = new_sector
        state_payload.pop("pending_rebalance", None)
        self._save_state(state_payload)

        return {
            "action": "rebalanced",
            "sold_ticker": current_ticker,
            "bought_ticker": new_ticker,
            "quantity": self.order_quantity,
            "sell_result": sell_result,
            "buy_result": buy_result,
        }
        