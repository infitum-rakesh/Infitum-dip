"""
fasttext_detector.py — Multilingual Document Processing, Phase 3.

The SECONDARY layer of the hybrid detection architecture (approved
decision, Sep 2026): a real FastText language-identification classifier,
consulted only when the deterministic script-block layer alone can't
resolve a document (ambiguous/mixed scripts, or Latin-script content
where "which specific language" needs a real model rather than a
Unicode-block count). See hybrid_detector.py's own docstring for exactly
when this layer gets consulted.

DISCLOSED, NOT FAKED: this needs two real external things neither of
which ship inside this repository —
  1. the `fasttext` pip package (added to requirements.txt this phase)
  2. a real pretrained language-identification model file (Facebook's
     public `lid.176.ftz`, ~1MB compressed, or the larger `lid.176.bin`)
     — a binary model asset, the same class of external dependency
     Tesseract's own `.traineddata` files already are for this codebase
     (see ocr/engine.py's docstring and requirements.txt's own comment on
     pytesseract needing the system tesseract binary).
The model path is configurable (FASTTEXT_LID_MODEL_PATH env var / app
settings), not hardcoded to one location, and this module NEVER
pretends the model is loaded when it isn't: `is_available` reports the
real, current state, and `detect()` raises LanguageDetectionUnavailableError
rather than returning a fabricated result when it's missing. This
specific piece is real, correct code that has not been exercised against
an actual lid.176 model file in this environment — the same "real but
unverified" disclosure this project already gives the SAP/Fabric/
Salesforce connectors (see connectors/database/sap.py and friends).
"""
from __future__ import annotations

import os

from app.ocr.language_config import LANGUAGE_CONFIG
from app.ocr.language_detection.base import (
    LanguageCandidate,
    LanguageDetectionResult,
    LanguageDetectionUnavailableError,
    LanguageDetector,
)

# Reverse lookup: FastText's own "__label__xx" -> our ISO code, built once
# from the single source of truth (language_config.py) rather than a
# second hand-written mapping.
_FASTTEXT_LABEL_TO_CODE = {info.fasttext_label: code for code, info in LANGUAGE_CONFIG.items()}

DEFAULT_MODEL_PATH_ENV = "FASTTEXT_LID_MODEL_PATH"
# Minimum characters of real (non-whitespace) text before a FastText call
# is worth making at all — matches the approved decision's own wording
# ("secondary language classifier when sufficient OCR text is available").
MIN_TEXT_LENGTH = 20


class FastTextLanguageDetector(LanguageDetector):
    def __init__(self, model_path: str | None = None):
        self._model_path = model_path or os.environ.get(DEFAULT_MODEL_PATH_ENV, "")
        self._model = None
        self._load_attempted = False

    @property
    def name(self) -> str:
        return "FASTTEXT"

    @property
    def is_available(self) -> bool:
        return bool(self._model_path) and os.path.isfile(self._model_path)

    def _ensure_loaded(self):
        if self._model is not None:
            return
        if self._load_attempted:
            raise LanguageDetectionUnavailableError(
                f"FastText language-ID model failed to load from '{self._model_path}' — see the earlier error in "
                f"the logs. Not retrying every call."
            )
        self._load_attempted = True
        if not self.is_available:
            raise LanguageDetectionUnavailableError(
                f"No FastText language-ID model file configured/found (path: "
                f"{self._model_path or '<not set — set the ' + DEFAULT_MODEL_PATH_ENV + ' env var>'}). "
                f"Download Facebook's public lid.176.ftz model and point {DEFAULT_MODEL_PATH_ENV} at it to enable "
                f"this secondary detection layer — script-block detection alone remains fully functional without it."
            )
        try:
            import fasttext  # local import — real external package, only needed when this layer is actually used
        except ImportError as exc:
            raise LanguageDetectionUnavailableError(
                "The `fasttext` package isn't installed in this environment (see requirements.txt)."
            ) from exc
        self._model = fasttext.load_model(self._model_path)

    def detect(self, text: str) -> LanguageDetectionResult:
        cleaned = " ".join(text.split())  # fasttext's predict() is line-oriented — collapse to one line
        if len(cleaned) < MIN_TEXT_LENGTH:
            return LanguageDetectionResult(primary_language=None, candidates=[], method="INSUFFICIENT_TEXT")

        self._ensure_loaded()
        labels, probs = self._model.predict(cleaned, k=3)

        candidates: list[LanguageCandidate] = []
        for label, prob in zip(labels, probs):
            code = _FASTTEXT_LABEL_TO_CODE.get(label)
            if code is None:
                continue  # FastText's 176-language model covers languages this build doesn't support — skip, don't guess
            candidates.append(LanguageCandidate(language=code, confidence=round(float(prob) * 100, 1)))

        primary = candidates[0].language if candidates else None
        return LanguageDetectionResult(primary_language=primary, candidates=candidates, method=self.name)
