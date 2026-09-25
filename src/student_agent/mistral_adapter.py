import json
import logging
import os
import urllib.request
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

LOG = logging.getLogger(__name__)


class MinistralClient:
    def __init__(self, api_key: str | None = None):
        load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")
        self.api_key = api_key or os.getenv("MISTRAL_API_KEY", "").strip()
        self.model = "ministral-8b-latest"
        self.url = "https://api.mistral.ai/v1/chat/completions"

    def is_available(self) -> bool:
        return bool(self.api_key and not self.api_key.startswith("your_"))

    def analyze_semantic(self, prompt: str, system_prompt: str = "") -> dict[str, Any] | None:
        """Gọi ministral-8b-latest để phân tích ngữ nghĩa các case mơ hồ, trả về dict JSON."""
        if not self.is_available():
            return None

        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        data = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": 150,
            "response_format": {"type": "json_object"},
        }

        try:
            req = urllib.request.Request(
                self.url, data=json.dumps(data).encode("utf-8"), headers=headers
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                res = json.loads(response.read().decode("utf-8"))
                content = res["choices"][0]["message"]["content"]
                return json.loads(content)
        except Exception as exc:
            LOG.warning("Ministral-8B API call failed: %s", exc)
            return None
