import base64
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

from sectors import us_sector_etfs


FALLBACK_MARKET_TICKERS = [
    "SPY",
    "VOO",
    "IVV",
    "VUSA",
    "CSP1",
    "VUAA",
    "SXR8",
    "IUSA",
    "QQQ",
    "EQQQ",
]

# Trading 212's public API has no live quote endpoint, so reference prices for
# sizing/validation come from Yahoo Finance instead. Try the bare ticker first
# (US-listed), then common European exchange suffixes for the LSE/Xetra-listed
# iShares ETFs used elsewhere in this project.
YFINANCE_SUFFIXES = ["", ".L", ".DE", ".AS", ".PA", ".MI"]


class Trading212API:
    def __init__(self, timeout=15, state_file="trading_state.json", order_quantity=1):
        load_dotenv()
        self.environment = self._clean_env_value(os.getenv("ENVIRONMENT")) or "DEMO"
        self.environment = self.environment.upper()
        if self.environment not in {"DEMO", "LIVE"}:
            raise RuntimeError("ENVIRONMENT must be either 'DEMO' or 'LIVE'")

        scoped_api_key = self._clean_env_value(
            os.getenv(f"{self.environment}_TRADING_212_API_KEY")
        )
        scoped_app_secret = self._clean_env_value(
            os.getenv(f"{self.environment}_TRADING_212_APP_SECRET")
        )

        self.api_key = scoped_api_key or self._clean_env_value(os.getenv("TRADING212_API_KEY"))
        self.app_secret = scoped_app_secret or self._clean_env_value(
            os.getenv("TRADING212_APP_SECRET")
        )
        self.base_url = (
            "https://live.trading212.com/api/v0/equity"
            if self.environment == "LIVE"
            else "https://demo.trading212.com/api/v0/equity"
        )
        self.timeout = timeout
        self.state_file = Path(__file__).resolve().parent / state_file
        self._instrument_tickers = None

        env_quantity = os.getenv("TRADING212_ORDER_QUANTITY")
        raw_quantity = int(env_quantity) if env_quantity else order_quantity
        self.order_quantity = self._validate_quantity(raw_quantity)

        initial_capital_env = self._clean_env_value(os.getenv("INITIAL_CAPITAL"))
        self.initial_capital = float(initial_capital_env) if initial_capital_env else 0.0

        missing = []
        if not self.api_key:
            missing.append(
                f"{self.environment}_TRADING_212_API_KEY (or TRADING212_API_KEY)"
            )
        if not self.app_secret:
            missing.append(
                f"{self.environment}_TRADING_212_APP_SECRET (or TRADING212_APP_SECRET)"
            )

        if missing:
            raise RuntimeError(
                "Missing Trading 212 configuration: " + ", ".join(missing)
            )

    @staticmethod
    def _clean_env_value(value):
        if value is None:
            return None

        cleaned = value.strip()
        if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {'"', "'"}:
            cleaned = cleaned[1:-1].strip()
        return cleaned

    def _orders_url(self):
        return f"{self.base_url}/orders/market"

    def _headers(self):
        basic_token = base64.b64encode(
            f"{self.api_key}:{self.app_secret}".encode("utf-8")
        ).decode("ascii")
        return {
            "Authorization": f"Basic {basic_token}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _ticker_candidates(ticker):
        base = str(ticker).strip().upper()
        candidates = [base]

        # UK Trading 212 format commonly appends lowercase 'l' before _EQ (e.g., IUITl_EQ).
        if not base.endswith("_EQ") and not base.endswith("_US_EQ"):
            candidates.append(f"{base}l_EQ")
            candidates.append(f"{base}_EQ")
            candidates.append(f"{base}_US_EQ")

        # Preserve order while removing duplicates.
        unique = []
        for symbol in candidates:
            if symbol not in unique:
                unique.append(symbol)
        return unique

    def _metadata_url(self, page=0, limit=500):
        return f"{self.base_url}/metadata/instruments?limit={limit}&page={page}"

    @staticmethod
    def _reference_price(ticker):
        """Look up a reference price from Yahoo Finance.

        Trading 212's public API has no live quote endpoint, so this is used
        purely for capital-based order sizing, not for order execution.
        """
        try:
            import yfinance as yf
        except ImportError as exc:
            raise RuntimeError(
                "yfinance is required for price lookups; install it with 'poetry add yfinance'"
            ) from exc

        base = str(ticker).strip().upper()
        for suffix in YFINANCE_SUFFIXES:
            symbol = f"{base}{suffix}"
            try:
                fast_info = yf.Ticker(symbol).fast_info
                price = fast_info.get("last_price") or fast_info.get("lastPrice")
            except Exception:
                continue

            if price and price > 0:
                return float(price)

        return None

    def _match_known_instrument(self, ticker):
        """Return the Trading 212 symbol for `ticker` if it's a known tradable instrument."""
        candidates = self._ticker_candidates(ticker)
        available = self._load_instrument_tickers()
        if not available:
            return candidates[0]

        available_by_upper = {sym.upper(): sym for sym in available}
        for candidate in candidates:
            resolved = available_by_upper.get(candidate.upper())
            if resolved:
                return resolved

        base = str(ticker).strip().upper().replace("_US_EQ", "").replace("_EQ", "")
        fuzzy = [
            sym for sym in available
            if sym.upper().startswith(base) and sym.upper().endswith("_EQ")
        ]
        return fuzzy[0] if fuzzy else None

    def _resolve_ticker_and_price(self, ticker):
        symbol = self._match_known_instrument(ticker)
        if symbol is None:
            raise RuntimeError(f"'{ticker}' is not a tradable Trading212 instrument")

        price = self._reference_price(ticker)
        if price is None:
            raise RuntimeError(f"Unable to resolve a reference price for '{ticker}'")

        return symbol, price

    def resolve_trade_target(self, ticker):
        """Resolve ticker to a tradable Trading212 symbol and current price."""
        return self._resolve_ticker_and_price(ticker)

    def resolve_first_tradable(self, tickers):
        for ticker in tickers:
            try:
                resolved_symbol, price = self._resolve_ticker_and_price(ticker)
                return {
                    "input_ticker": ticker,
                    "resolved_symbol": resolved_symbol,
                    "price": price,
                }
            except RuntimeError:
                continue
        return None

    def _available_capital(self, state_payload):
        value = state_payload.get("available_capital")
        if value is None:
            if self.initial_capital <= 0:
                raise RuntimeError(
                    "INITIAL_CAPITAL must be set to a positive number for first-time sizing"
                )
            return float(self.initial_capital)

        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("available_capital in trading_state.json must be numeric") from exc

        if parsed < 0:
            return 0.0
        return parsed

    @staticmethod
    def _max_quantity_from_capital(available_capital, instrument_price):
        return int(available_capital // instrument_price)

    @staticmethod
    def _round_money(amount):
        return round(float(amount), 2)

    def _load_instrument_tickers(self):
        if self._instrument_tickers is not None:
            return self._instrument_tickers

        tickers = set()
        for page in range(0, 40):
            try:
                response = requests.get(
                    self._metadata_url(page=page, limit=500),
                    headers=self._headers(),
                    timeout=self.timeout,
                )
            except requests.RequestException:
                break

            if response.status_code != 200:
                break

            try:
                data = response.json()
            except ValueError:
                break

            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("items") or data.get("results") or []
            else:
                items = []

            if not items:
                break

            for item in items:
                ticker = item.get("ticker") if isinstance(item, dict) else None
                if ticker:
                    tickers.add(str(ticker).strip())

            if len(items) < 500:
                break

        self._instrument_tickers = tickers
        return tickers

    def _resolve_ticker_candidates(self, ticker):
        candidates = self._ticker_candidates(ticker)
        available = self._load_instrument_tickers()
        if not available:
            return candidates

        available_by_upper = {sym.upper(): sym for sym in available}

        direct = []
        for candidate in candidates:
            resolved = available_by_upper.get(candidate.upper())
            if resolved:
                direct.append(resolved)
        if direct:
            return direct

        base = str(ticker).strip().upper().replace("_US_EQ", "").replace("_EQ", "")
        fuzzy = [
            sym for sym in available
            if sym.upper().startswith(base) and sym.upper().endswith("_EQ")
        ]
        if fuzzy:
            return fuzzy

        return candidates

    @staticmethod
    def _order_payload(symbol, signed_quantity, extended_hours=True):
        return {
            "quantity": signed_quantity,
            "ticker": symbol,
            "extendedHours": extended_hours,
        }

    def _post_order(self, ticker, signed_quantity):
        url = self._orders_url()
        resolved_tickers = self._resolve_ticker_candidates(ticker)
        last_response = None
        headers = self._headers()

        for resolved in resolved_tickers:
            payload_variants = [
                self._order_payload(resolved, signed_quantity, extended_hours=True),
                self._order_payload(resolved, signed_quantity, extended_hours=False),
            ]

            move_to_next_symbol = False
            for payload in payload_variants:

                response = None
                for attempt in range(3):
                    try:
                        response = requests.post(
                            url,
                            headers=headers,
                            json=payload,
                            timeout=self.timeout,
                        )
                        break
                    except requests.Timeout:
                        if attempt == 2:
                            raise
                        time.sleep(0.5 * (attempt + 1))

                if response is None:
                    continue

                if response.status_code in (200, 201):
                    return response

                body_lower = (response.text or "").lower()

                # Retry with next ticker candidate on symbol errors.
                if response.status_code == 404 and "ticker does not exist" in body_lower:
                    last_response = response
                    move_to_next_symbol = True
                    break

                # Retry alternate payload variants on payload/extended-hours validation errors.
                if response.status_code == 400 and (
                    "invalid payload" in body_lower
                    or "extended hours trading is not allowed" in body_lower
                ):
                    last_response = response
                    continue

                body = response.text or ""
                raise RuntimeError(
                    f"Trading 212 order request failed ({response.status_code}): {body}"
                )

            if move_to_next_symbol:
                continue

        if last_response is not None:
            body = last_response.text or ""
            raise RuntimeError(
                f"Trading 212 order request failed ({last_response.status_code}): {body}"
            )

        raise RuntimeError("Trading 212 order request failed before receiving a response")

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
        if state_payload.get("holds_position") is False:
            return None

        explicit_ticker = state_payload.get("current_ticker")
        if explicit_ticker:
            return explicit_ticker

        current_sector = state_payload.get("current_sector")
        if not current_sector:
            raise ValueError("Current sector not found in trading_state.json")

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

    @staticmethod
    def _is_not_owned_error(error_message):
        text = str(error_message).lower()
        return (
            "selling-equity-not-owned" in text
            or "selling more equities than owned" in text
        )

    @staticmethod
    def _is_unresolvable_symbol_error(error_message):
        text = str(error_message).lower()
        return "unable to resolve a tradable priced symbol" in text

    def sell_sector_etf(self, ticker, quantity=None):
        order_quantity = self._validate_quantity(
            self.order_quantity if quantity is None else quantity
        )

        # Place a sell order for the current sector ETF.
        response = self._post_order(ticker=ticker, signed_quantity=-order_quantity)
        return self._response_payload(response)
        
    def buy_sector_etf(self, ticker, quantity=None):
        order_quantity = self._validate_quantity(
            self.order_quantity if quantity is None else quantity
        )

        # Place a buy order for the new sector ETF.
        response = self._post_order(ticker=ticker, signed_quantity=order_quantity)
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
        state_payload["current_sector"] = new_sector or f"Custom:{expected_new_ticker}"
        state_payload["current_ticker"] = expected_new_ticker
        state_payload["holds_position"] = True
        state_payload["position_quantity"] = quantity
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

        state_payload = self._load_state()
        available_capital = self._available_capital(state_payload)
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
                "available_capital": self._round_money(available_capital),
            }

        if current_ticker is None:
            buy_symbol, buy_price = self._resolve_ticker_and_price(new_ticker)
            buy_quantity = self._max_quantity_from_capital(available_capital, buy_price)
            if buy_quantity < 1:
                raise RuntimeError(
                    f"Insufficient available capital ({available_capital:.2f}) for {buy_symbol}"
                )

            buy_result = self.buy_sector_etf(buy_symbol, quantity=buy_quantity)
            available_capital -= buy_quantity * buy_price
            state_payload["current_sector"] = new_sector or f"Custom:{new_ticker}"
            state_payload["current_ticker"] = new_ticker
            state_payload["holds_position"] = True
            state_payload["position_quantity"] = buy_quantity
            state_payload["available_capital"] = self._round_money(available_capital)
            state_payload.pop("pending_rebalance", None)
            self._save_state(state_payload)

            return {
                "action": "opened_new_position",
                "sold_ticker": None,
                "bought_ticker": new_ticker,
                "quantity": buy_quantity,
                "estimated_buy_price": buy_price,
                "available_capital": self._round_money(available_capital),
                "sell_result": {
                    "action": "skipped",
                    "reason": "State indicates no existing position",
                },
                "buy_result": buy_result,
            }

        sold_existing_position = True
        sell_quantity = self._validate_quantity(
            state_payload.get("position_quantity") or self.order_quantity
        )
        try:
            sell_symbol, sell_price = self._resolve_ticker_and_price(current_ticker)
            sell_result = self.sell_sector_etf(sell_symbol, quantity=sell_quantity)
            available_capital += sell_quantity * sell_price
            state_payload["pending_rebalance"] = {
                "from_ticker": current_ticker,
                "to_ticker": new_ticker,
                "quantity": sell_quantity,
                "sell_result": sell_result,
                "started_at_utc": datetime.now(timezone.utc).isoformat(),
            }
            self._save_state(state_payload)
        except RuntimeError as exc:
            if not (
                self._is_not_owned_error(exc)
                or self._is_unresolvable_symbol_error(exc)
            ):
                raise

            sold_existing_position = False
            sell_result = {
                "action": "skipped",
                "reason": "No sellable current holding",
                "detail": str(exc),
            }

        try:
            buy_symbol, buy_price = self._resolve_ticker_and_price(new_ticker)
            buy_quantity = self._max_quantity_from_capital(available_capital, buy_price)
            if buy_quantity < 1:
                raise RuntimeError(
                    f"Insufficient available capital ({available_capital:.2f}) for {buy_symbol}"
                )

            buy_result = self.buy_sector_etf(buy_symbol, quantity=buy_quantity)
            available_capital -= buy_quantity * buy_price
        except requests.RequestException as exc:
            if sold_existing_position:
                raise RuntimeError(
                    f"Sell order for '{current_ticker}' succeeded, but buy order for '{new_ticker}' failed"
                ) from exc
            raise RuntimeError(
                f"Buy order for '{new_ticker}' failed while opening a new position"
            ) from exc

        state_payload = self._load_state()
        state_payload["current_sector"] = new_sector or f"Custom:{new_ticker}"
        state_payload["current_ticker"] = new_ticker
        state_payload["holds_position"] = True
        state_payload["position_quantity"] = buy_quantity
        state_payload["available_capital"] = self._round_money(available_capital)
        state_payload.pop("pending_rebalance", None)
        self._save_state(state_payload)

        return {
            "action": "rebalanced" if sold_existing_position else "opened_new_position",
            "sold_ticker": current_ticker,
            "bought_ticker": new_ticker,
            "quantity": buy_quantity,
            "estimated_buy_price": buy_price,
            "available_capital": self._round_money(available_capital),
            "sell_result": sell_result,
            "buy_result": buy_result,
        }
        