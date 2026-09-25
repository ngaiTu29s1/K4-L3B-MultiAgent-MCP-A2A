import os
import json
import urllib.request
import logging
from typing import Dict, Any, Optional
from pathlib import Path
from dotenv import load_dotenv

LOG = logging.getLogger(__name__)

class LlamaClient:
    def __init__(self, api_key: Optional[str] = None):
        load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")
        self.api_key = api_key or os.getenv("OPENROUTER_API_KEY", "").strip()
        self.model = "meta-llama/llama-3.1-8b-instruct"
        self.url = "https://openrouter.ai/api/v1/chat/completions"

    def is_available(self) -> bool:
        return bool(self.api_key and not self.api_key.startswith("your_"))

    def analyze_semantic(self, prompt: str, system_prompt: str = "") -> Optional[Dict[str, Any]]:
        """Gọi Meta-Llama-3.1-8B-Instruct qua OpenRouter API để phân tích ngữ nghĩa, trả về JSON."""
        if not self.is_available():
            return None

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({
            "role": "user",
            "content": f"{prompt}\nIMPORTANT: You must respond ONLY with a raw JSON object. Do not include markdown codeblocks or other commentary."
        })

        data = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": 150
        }

        try:
            req = urllib.request.Request(self.url, data=json.dumps(data).encode("utf-8"), headers=headers)
            with urllib.request.urlopen(req, timeout=12) as response:
                res = json.loads(response.read().decode("utf-8"))
                text = res["choices"][0]["message"]["content"].strip()
                # Clean possible markdown wrapping
                if text.startswith("```"):
                    lines = text.splitlines()
                    text = "\n".join(lines[1:-1]) if len(lines) > 2 else text
                return json.loads(text)
        except Exception as exc:
            LOG.warning("Llama-3-8B API call failed: %s", exc)
            return None
