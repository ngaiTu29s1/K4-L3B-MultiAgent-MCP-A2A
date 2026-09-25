import os
import json
import urllib.request
import logging
from typing import Dict, Any, Optional
from pathlib import Path
from dotenv import load_dotenv

LOG = logging.getLogger(__name__)

class GeminiClient:
    def __init__(self, api_key: Optional[str] = None):
        load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")
        self.api_key = api_key or os.getenv("GEMINI_API_KEY", "").strip()
        self.model = "gemini-3.1-flash-lite"

    def is_available(self) -> bool:
        return bool(self.api_key and not self.api_key.startswith("your_"))

    def analyze_semantic(self, prompt: str, system_prompt: str = "") -> Optional[Dict[str, Any]]:
        """Gọi Google Gemini API (gemini-2.5-flash) để phân tích ngữ nghĩa phản hồi JSON."""
        if not self.is_available():
            return None

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"
        headers = {"Content-Type": "application/json"}

        contents = []
        if system_prompt:
            contents.append({"role": "user", "parts": [{"text": f"SYSTEM INSTRUCTION: {system_prompt}"}]})
            contents.append({"role": "model", "parts": [{"text": "Understood. I will follow your instructions and output valid JSON only."}]})

        contents.append({"role": "user", "parts": [{"text": prompt}]})

        payload = {
            "contents": contents,
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.1,
                "maxOutputTokens": 200
            }
        }

        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
            with urllib.request.urlopen(req, timeout=12) as response:
                res = json.loads(response.read().decode("utf-8"))
                text = res["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text)
        except Exception as exc:
            LOG.warning("Gemini API call failed: %s", exc)
            return None
