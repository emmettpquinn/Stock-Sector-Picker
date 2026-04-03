import requests
from sectors import us_sector_etfs
import os
from dotenv import load_dotenv
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

load_dotenv()

VECTORVEST_SIGNALS = [
    {
        "url": "https://www.vectorvest.com/VectorVest7Services/DynamicContentHost/v2/tradingsystemviewer?region=US&tradingSystemId=3653417",
        "rules": ["C/UP", "C/DN"],
    },
    {
        "url": "https://www.vectorvest.com/VectorVest7Services/DynamicContentHost/v2/tradingsystemviewer?region=US&tradingSystemId=2953132",
        "rules": ["Up", "Down"],
    },
    {
        "url": "https://www.vectorvest.com/VectorVest7Services/DynamicContentHost/v2/tradingsystemviewer?region=US&tradingSystemId=3978864",
        "rules": ["Up", "Down", "Neutral"],
    },
    {
        "url": "https://www.vectorvest.com/VectorVest7Services/DynamicContentHost/v2/tradingsystemviewer?region=US&tradingSystemId=3509285",
        "rules": ["Up", "Down"],
    },
    {
        "url": "https://www.vectorvest.com/VectorVest7Services/DynamicContentHost/v2/tradingsystemviewer?region=US&tradingSystemId=4011902",
        "rules": ["Up", "Down", "Neutral"],
    },
]


class VectorVestSignalMonitor:
    def __init__(self, state_file="vv_signal_state.json", timeout=20):
        self.state_file = Path(state_file)
        self.timeout = timeout

    @staticmethod
    def _normalize_status(rule_name):
        if re.fullmatch(r"C/UP|Up", rule_name, flags=re.IGNORECASE):
            return "UP"
        if re.fullmatch(r"C/DN|Down", rule_name, flags=re.IGNORECASE):
            return "DOWN"
        if re.fullmatch(r"Neutral", rule_name, flags=re.IGNORECASE):
            return "Neutral"
        return "Unknown"

    def _extract_status_from_html(self, html_text, rules):
        status = "Unknown"
        containers = re.split(
            r"<div\s+class\s*=\s*['\"]condition-container['\"]",
            html_text,
            flags=re.IGNORECASE,
        )

        for block in containers[1:]:
            for rule_name in rules:
                rule_pattern = rf"Rules:\s*{re.escape(rule_name)}\b"
                has_rule = re.search(rule_pattern, block, flags=re.IGNORECASE)
                is_current = re.search(
                    r"<div\s+class\s*=\s*['\"]current['\"]\s*>\s*Current\s*</div>",
                    block,
                    flags=re.IGNORECASE,
                )

                if has_rule and is_current:
                    status = self._normalize_status(rule_name)
                    break

            if status != "Unknown":
                break

        return status

    def fetch_statuses(self):
        statuses = []
        for signal in VECTORVEST_SIGNALS:
            try:
                response = requests.get(signal["url"], timeout=self.timeout)
                response.raise_for_status()
                status = self._extract_status_from_html(response.text, signal["rules"])
            except requests.RequestException:
                status = "FetchError"
            statuses.append(status)
        return statuses

    def load_previous_statuses(self):
        if not self.state_file.exists():
            return [None] * len(VECTORVEST_SIGNALS)

        try:
            payload = json.loads(self.state_file.read_text(encoding="utf-8"))
            statuses = payload.get("statuses", [])
        except (json.JSONDecodeError, OSError):
            return [None] * len(VECTORVEST_SIGNALS)

        if len(statuses) != len(VECTORVEST_SIGNALS):
            return [None] * len(VECTORVEST_SIGNALS)
        return statuses

    def save_statuses(self, statuses):
        payload = {
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
            "statuses": statuses,
        }
        self.state_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def statuses_changed(self, current_statuses, previous_statuses):
        return current_statuses != previous_statuses

class SectorInvestor:
    def __init__(self):
        self.api_key = os.getenv("PERPLEXITY_API_KEY")

    def _build_prompt(self):
        sector_map = json.dumps(us_sector_etfs, indent=2)
        return (
            "You are selecting exactly one sector ETF that is best positioned right now based on current market conditions. "
            "Return JSON only with keys: sector, ticker, confidence, rationale. "
            "Rules: sector must be an exact key from the dictionary; ticker must match that key; confidence must be 0-100 integer; "
            "rationale must be under 40 words.\n\n"
            "Sector dictionary:\n"
            f"{sector_map}"
        )

    @staticmethod
    def _extract_json(text):
        text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            raise ValueError("No JSON object found in response")
        return json.loads(match.group(0))

    @staticmethod
    def _validate_pick(payload):
        if not isinstance(payload, dict):
            raise ValueError("Response payload is not an object")

        sector = payload.get("sector")
        ticker = payload.get("ticker")
        confidence = payload.get("confidence")
        rationale = payload.get("rationale", "")

        if sector not in us_sector_etfs:
            raise ValueError(f"Unknown sector '{sector}'")

        expected_ticker = us_sector_etfs[sector]
        if ticker != expected_ticker:
            raise ValueError(
                f"Ticker mismatch for sector '{sector}'. Expected '{expected_ticker}', got '{ticker}'"
            )

        if not isinstance(confidence, int) or not (0 <= confidence <= 100):
            raise ValueError("Confidence must be an integer between 0 and 100")

        return {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "sector": sector,
            "ticker": ticker,
            "confidence": confidence,
            "rationale": str(rationale).strip(),
        }

    def get_best_sector(self):
        if not self.api_key:
            raise RuntimeError("PERPLEXITY_API_KEY is missing")

        # Define the headers for authentication.
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        prompt = self._build_prompt()

        # Perplexity uses chat-completions payload format.
        data = {
            "model": "sonar",
            "messages": [
                {
                    "role": "user",
                    "content": prompt
                }
            ]
        }

        # Make a POST request to the Perplexity API.
        response = requests.post(
            "https://api.perplexity.ai/chat/completions",
            headers=headers,
            json=data,
            timeout=30
        )

        # Parse and return the response.
        response.raise_for_status()
        result = response.json()
        raw_content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        parsed_payload = self._extract_json(raw_content)
        return self._validate_pick(parsed_payload)

    def monitor_market(self, poll_seconds=900, max_cycles=None):
        signal_monitor = VectorVestSignalMonitor()
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
                    signal_monitor.save_statuses(current_statuses)
                    pick = self.get_best_sector()
                    signal_snapshot["sector_pick"] = pick
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
        
if __name__ == "__main__":
    investor = SectorInvestor()
    investor.monitor_market(poll_seconds=900, max_cycles=1)

    