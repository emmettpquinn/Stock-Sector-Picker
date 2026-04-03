import json
import re
import requests
from datetime import datetime, timezone
from pathlib import Path

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