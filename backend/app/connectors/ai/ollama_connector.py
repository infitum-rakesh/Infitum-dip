"""Ollama connector — real HTTP calls to a local/remote Ollama server.
No API key — Ollama has no auth by default. This is the connector
type used for live verification in this build (no external API cost)."""
from __future__ import annotations

import time

import httpx

from app.connectors.base import BaseConnector, ConnectionTestResult, ConnectorError

DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_MODEL = "llama3.2"
TIMEOUT = 20.0
# Local CPU inference can be genuinely slow for a large completion — a
# comprehensive canonical-taxonomy extraction (INVOICE's 45+ fields, each
# needing its own {value,confidence,page} object) real-world timed out at
# 120s on this machine's hardware once max_tokens scaled up to match the
# schema size (see extraction_service.extract_fields()). Raising the
# ceiling respects that local inference is slow, not a call to cut it off
# before it finishes a genuine (if slow) real response.
GENERATE_TIMEOUT = 240.0

# Model-name substrings for Ollama models actually capable of accepting
# image input (Ollama's /api/generate `images` field only works for these
# — passing it to a text-only model like llama3.2 is silently ignored by
# the server, which would look like vision support without genuinely
# being it, so this is checked, not assumed). Not exhaustive of every
# vision model Ollama could ever host, but every entry here is a real,
# documented multimodal Ollama model as of this build — live-verified in
# this build against a real pulled `moondream` instance.
_VISION_MODEL_HINTS = (
    "llava", "bakllava", "moondream", "llama3.2-vision", "llama-vision",
    "minicpm-v", "qwen2-vl", "qwen-vl", "vision",
)


class OllamaConnector(BaseConnector):
    connection_type = "OLLAMA"

    def _base_url(self) -> str:
        return (self.config.get("base_url") or DEFAULT_BASE_URL).rstrip("/")

    def _model(self) -> str:
        return self.config.get("model") or DEFAULT_MODEL

    def test_connection(self) -> ConnectionTestResult:
        t0 = time.monotonic()
        try:
            with httpx.Client(timeout=TIMEOUT) as client:
                resp = client.get(f"{self._base_url()}/api/tags")
            latency_ms = (time.monotonic() - t0) * 1000
            if resp.status_code >= 400:
                return ConnectionTestResult(False, self._scrub(f"Ollama returned HTTP {resp.status_code}: {resp.text[:200]}"), latency_ms)
            data = resp.json()
            model_names = {m.get("name") for m in data.get("models", [])}
            target = self._model()
            # Ollama tags include the ":latest" suffix — match with or without it.
            matched = target in model_names or f"{target}:latest" in model_names or any(n.split(":")[0] == target for n in model_names if n)
            if not matched:
                return ConnectionTestResult(
                    True, f"Connected, but model '{target}' isn't pulled on this Ollama server (run `ollama pull {target}`) — generation will fail until it is.",
                    latency_ms, server_version=f"{len(model_names)} model(s) available",
                )
            return ConnectionTestResult(True, "Connection successful.", latency_ms, server_version=f"model: {target}")
        except httpx.RequestError as exc:
            return ConnectionTestResult(False, self._scrub(f"Could not reach Ollama at {self._base_url()}: {exc}"))

    def generate(self, prompt: str, *, system: str | None = None, max_tokens: int = 2000) -> str:
        body = {"model": self._model(), "prompt": prompt, "stream": False, "options": {"num_predict": max_tokens}}
        if system:
            body["system"] = system
        try:
            with httpx.Client(timeout=GENERATE_TIMEOUT) as client:
                resp = client.post(f"{self._base_url()}/api/generate", json=body)
            if resp.status_code >= 400:
                raise ConnectorError(self._scrub(f"Ollama generation failed: HTTP {resp.status_code} {resp.text[:300]}"))
            data = resp.json()
            return data.get("response", "")
        except httpx.RequestError as exc:
            raise ConnectorError(self._scrub(f"Could not reach Ollama at {self._base_url()}: {exc}")) from exc

    def supports_vision(self) -> bool:
        model = self._model().lower()
        return any(hint in model for hint in _VISION_MODEL_HINTS)

    def generate_vision(
        self, prompt: str, *, image_b64: str, mime_type: str, system: str | None = None, max_tokens: int = 2000,
    ) -> str:
        # mime_type is accepted for interface parity with the other
        # connectors but unused here — Ollama's /api/generate `images`
        # field takes raw base64 with no MIME wrapper (unlike Anthropic's
        # base64 source block or OpenAI's data: URI), so there's nothing
        # to embed it into.
        body = {"model": self._model(), "prompt": prompt, "images": [image_b64], "stream": False, "options": {"num_predict": max_tokens}}
        if system:
            body["system"] = system
        try:
            with httpx.Client(timeout=GENERATE_TIMEOUT) as client:
                resp = client.post(f"{self._base_url()}/api/generate", json=body)
            if resp.status_code >= 400:
                raise ConnectorError(self._scrub(f"Ollama vision generation failed: HTTP {resp.status_code} {resp.text[:300]}"))
            data = resp.json()
            return data.get("response", "")
        except httpx.RequestError as exc:
            raise ConnectorError(self._scrub(f"Could not reach Ollama at {self._base_url()}: {exc}")) from exc
