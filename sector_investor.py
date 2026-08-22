import json
import os
import re
import requests
from datetime import datetime, timezone

from sectors import us_sector_etfs

class SectorInvestor:
    def __init__(self):
        self.api_key = os.getenv("PERPLEXITY_API_KEY")

    def _build_prompt(self, excluded_tickers=None):
        sector_map = json.dumps(us_sector_etfs, indent=2)
        excluded_tickers = excluded_tickers or []
        excluded_note = ""
        if excluded_tickers:
            excluded_note = (
                " Do not use any of these tickers because they are not tradable right now: "
                + ", ".join(sorted(set(excluded_tickers)))
                + "."
            )

        return (
            "You are selecting exactly one sector ETF that is best positioned right now based on current market conditions. "
            "Return JSON only with keys: sector, ticker, confidence, rationale. "
            "Rules: sector must be an exact key from the dictionary; ticker must match that key; confidence must be 0-100 integer; "
            f"rationale must be under 40 words.{excluded_note}\n\n"
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

    def get_best_sector(self, excluded_tickers=None):
        if not self.api_key:
            raise RuntimeError("PERPLEXITY_API_KEY is missing")

        # Define the headers for authentication.
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        prompt = self._build_prompt(excluded_tickers=excluded_tickers)

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
        validated_pick = self._validate_pick(parsed_payload)
        validated_pick["perplexity_raw_response"] = raw_content
        return validated_pick