"""OpenAI connector — real HTTP calls to the OpenAI Chat Completions API
(or any OpenAI-compatible endpoint via a custom base_url, e.g. Azure
OpenAI's compatible mode or a self-hosted gateway)."""
from __future__ import annotations

import time

import httpx

from app.connectors.base import BaseConnector, ConnectionTestResult, ConnectorError

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
TIMEOUT = 20.0

# Real OpenAI model-name substrings that accept image input via Chat
# Completions' image_url content part. gpt-3.5* and base gpt-4 (non-o/
# non-turbo/non-vision) are text-only and deliberately excluded.
_VISION_MODEL_HINTS = ("gpt-4o", "gpt-4-turbo", "gpt-4-vision", "o1", "o3", "o4")


class OpenAiConnector(BaseConnector):
    connection_type = "OPENAI"

    def _base_url(self) -> str:
        return (self.config.get("base_url") or DEFAULT_BASE_URL).rstrip("/")

    def _model(self) -> str:
        return self.config.get("model") or DEFAULT_MODEL

    def _headers(self) -> dict:
        api_key = self.secrets.get("api_key")
        if not api_key:
            raise ConnectorError("No API key configured.")
        return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    def test_connection(self) -> ConnectionTestResult:
        t0 = time.monotonic()
        try:
            with httpx.Client(timeout=TIMEOUT) as client:
                resp = client.get(f"{self._base_url()}/models", headers=self._headers())
            latency_ms = (time.monotonic() - t0) * 1000
            if resp.status_code == 401:
                return ConnectionTestResult(False, "Authentication failed — check the API key.", latency_ms)
            if resp.status_code >= 400:
                return ConnectionTestResult(False, self._scrub(f"OpenAI API returned HTTP {resp.status_code}: {resp.text[:200]}"), latency_ms)
            data = resp.json()
            model_ids = {m.get("id") for m in data.get("data", [])}
            target = self._model()
            if model_ids and target not in model_ids:
                return ConnectionTestResult(
                    True, f"Connected, but model '{target}' wasn't found in this account's model list — generation may fail.",
                    latency_ms, server_version=f"{len(model_ids)} models available",
                )
            return ConnectionTestResult(True, "Connection successful.", latency_ms, server_version=f"model: {target}")
        except httpx.RequestError as exc:
            return ConnectionTestResult(False, self._scrub(f"Could not reach OpenAI: {exc}"))

    def generate(self, prompt: str, *, system: str | None = None, max_tokens: int = 2000) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        body = {"model": self._model(), "messages": messages, "max_tokens": max_tokens}
        try:
            with httpx.Client(timeout=TIMEOUT * 3) as client:
                resp = client.post(f"{self._base_url()}/chat/completions", headers=self._headers(), json=body)
            if resp.status_code >= 400:
                raise ConnectorError(self._scrub(f"OpenAI generation failed: HTTP {resp.status_code} {resp.text[:300]}"))
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except httpx.RequestError as exc:
            raise ConnectorError(self._scrub(f"Could not reach OpenAI: {exc}")) from exc

    def supports_vision(self) -> bool:
        model = self._model().lower()
        return any(hint in model for hint in _VISION_MODEL_HINTS)

    def generate_vision(
        self, prompt: str, *, image_b64: str, mime_type: str, system: str | None = None, max_tokens: int = 2000,
    ) -> str:
        content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_b64}"}},
        ]
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": content})
        body = {"model": self._model(), "messages": messages, "max_tokens": max_tokens}
        try:
            with httpx.Client(timeout=TIMEOUT * 3) as client:
                resp = client.post(f"{self._base_url()}/chat/completions", headers=self._headers(), json=body)
            if resp.status_code >= 400:
                raise ConnectorError(self._scrub(f"OpenAI vision generation failed: HTTP {resp.status_code} {resp.text[:300]}"))
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except httpx.RequestError as exc:
            raise ConnectorError(self._scrub(f"Could not reach OpenAI: {exc}")) from exc
