"""Anthropic (Claude) connector — real HTTP calls to the Messages API."""
from __future__ import annotations

import time

import httpx

from app.connectors.base import BaseConnector, ConnectionTestResult, ConnectorError

BASE_URL = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-3-5-haiku-20241022"
TIMEOUT = 20.0

# Every Claude 3+ model accepts image input (Messages API image content
# blocks) — real, documented Anthropic capability. Claude 1/2/Instant
# (pre-vision) are excluded by the "claude-3"/"claude-4" prefix check.
_VISION_MODEL_PREFIXES = ("claude-3", "claude-4")


class AnthropicConnector(BaseConnector):
    connection_type = "ANTHROPIC"

    def _model(self) -> str:
        return self.config.get("model") or DEFAULT_MODEL

    def _headers(self) -> dict:
        api_key = self.secrets.get("api_key")
        if not api_key:
            raise ConnectorError("No API key configured.")
        return {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION, "Content-Type": "application/json"}

    def test_connection(self) -> ConnectionTestResult:
        t0 = time.monotonic()
        try:
            with httpx.Client(timeout=TIMEOUT) as client:
                resp = client.get(f"{BASE_URL}/models", headers=self._headers())
            latency_ms = (time.monotonic() - t0) * 1000
            if resp.status_code == 401:
                return ConnectionTestResult(False, "Authentication failed — check the API key.", latency_ms)
            if resp.status_code >= 400:
                return ConnectionTestResult(False, self._scrub(f"Anthropic API returned HTTP {resp.status_code}: {resp.text[:200]}"), latency_ms)
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
            return ConnectionTestResult(False, self._scrub(f"Could not reach Anthropic: {exc}"))

    def generate(self, prompt: str, *, system: str | None = None, max_tokens: int = 2000) -> str:
        body = {"model": self._model(), "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
        if system:
            body["system"] = system
        try:
            with httpx.Client(timeout=TIMEOUT * 3) as client:
                resp = client.post(f"{BASE_URL}/messages", headers=self._headers(), json=body)
            if resp.status_code >= 400:
                raise ConnectorError(self._scrub(f"Anthropic generation failed: HTTP {resp.status_code} {resp.text[:300]}"))
            data = resp.json()
            self.last_usage = self._parse_usage(data)
            parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
            return "".join(parts)
        except httpx.RequestError as exc:
            raise ConnectorError(self._scrub(f"Could not reach Anthropic: {exc}")) from exc

    @staticmethod
    def _parse_usage(data: dict) -> dict | None:
        """The Messages API's own top-level `usage` object — real
        provider-reported counts (see base.py's last_usage docstring)."""
        usage = data.get("usage")
        if not usage:
            return None
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        total = (input_tokens or 0) + (output_tokens or 0) if (input_tokens is not None or output_tokens is not None) else None
        return {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": total}

    def supports_vision(self) -> bool:
        model = self._model().lower()
        return model.startswith(_VISION_MODEL_PREFIXES)

    def generate_vision(
        self, prompt: str, *, image_b64: str, mime_type: str, system: str | None = None, max_tokens: int = 2000,
    ) -> str:
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": image_b64}},
            {"type": "text", "text": prompt},
        ]
        body = {"model": self._model(), "max_tokens": max_tokens, "messages": [{"role": "user", "content": content}]}
        if system:
            body["system"] = system
        try:
            with httpx.Client(timeout=TIMEOUT * 3) as client:
                resp = client.post(f"{BASE_URL}/messages", headers=self._headers(), json=body)
            if resp.status_code >= 400:
                raise ConnectorError(self._scrub(f"Anthropic vision generation failed: HTTP {resp.status_code} {resp.text[:300]}"))
            data = resp.json()
            self.last_usage = self._parse_usage(data)
            parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
            return "".join(parts)
        except httpx.RequestError as exc:
            raise ConnectorError(self._scrub(f"Could not reach Anthropic: {exc}")) from exc
