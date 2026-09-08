# Multilingual Document Processing

Real OCR, AI extraction, and language detection across 13 languages/scripts,
preserving the original script verbatim — this platform never translates,
transliterates, or romanizes extracted text. This document covers what was
built, how to deploy/configure it, and how to operate and troubleshoot it.

## 1. What this is (and isn't)

Documents can arrive in any of 13 supported languages, often mixed within
the same file (English boilerplate on a Kannada invoice, a Hindi cover page
followed by English tables, ...). This feature:

1. Detects the real language of each page using a hybrid detector (Unicode
   script-block analysis, escalating to FastText only when the script alone
   is ambiguous — e.g. Devanagari is shared by Hindi and Marathi).
2. Runs Tesseract OCR with the correct per-page language model, re-OCRing
   only the pages where a narrower/different language measurably improves
   real recognition confidence over the initial combined-language pass.
3. Validates OCR quality (empty result, Unicode corruption, low confidence,
   unsupported/mismatched language) and routes genuine problems to the
   Review Queue — never silently accepts a bad result.
4. Instructs the AI extraction prompt to copy every value byte-for-byte in
   its original script — never translate, transliterate, or romanize.
5. Surfaces detected language, quality flags, and (new) real-time OCR
   diagnostics in the Administration UI.

What it deliberately does **not** do:

- Translate document content. The "Enable translation" setting in
  Administration → Document Intelligence → Languages is present but
  disabled — reserved for a future phase, not implemented.
- Reimplement OCR or AI extraction as a parallel pipeline — this is the
  same `document_service.process_document()` every upload path uses; the
  only change is *which* Tesseract language string and AI prompt wording
  get used.
- Guarantee cloud OCR providers (AWS Textract / Google Vision / Azure Form
  Recognizer) exercise this same two-pass language-selection logic — those
  providers do their own multilingual recognition internally, so the
  pipeline only detects and reports the language of what comes back, it
  doesn't drive a second Tesseract-style re-OCR pass.

## 2. Supported languages

The single source of truth is `backend/app/ocr/language_config.py`'s
`LANGUAGE_CONFIG`. Every part of the codebase (OCR, detection, Settings UI,
Custom Extraction Engine) reads from this table — nothing hardcodes a
language mapping elsewhere.

| Code | Language  | Tesseract pack | Script      |
|------|-----------|-----------------|-------------|
| en   | English   | `eng`           | Latin       |
| hi   | Hindi     | `hin`           | Devanagari  |
| kn   | Kannada   | `kan`           | Kannada     |
| ta   | Tamil     | `tam`           | Tamil       |
| te   | Telugu    | `tel`           | Telugu      |
| ml   | Malayalam | `mal`           | Malayalam   |
| mr   | Marathi   | `mar`           | Devanagari  |
| bn   | Bengali   | `ben`           | Bengali     |
| gu   | Gujarati  | `guj`           | Gujarati    |
| pa   | Punjabi   | `pan`           | Gurmukhi    |
| or   | Odia      | `ori`           | Oriya       |
| as   | Assamese  | `asm`           | Bengali     |
| ur   | Urdu      | `urd`           | Arabic      |

English is always implicitly OCR'd alongside whatever else is configured —
mixed-language documents commonly carry English labels/boilerplate even
when the body text is in another language.

`en`, `hi`, `kn`, `ta` ship real, curated Custom Extraction Engine label
dictionaries (`backend/app/extraction/label_dictionaries/`) and test
fixtures. Every other language is fully functional for OCR/detection but
falls back to English-only labels + script-agnostic regex patterns in the
no-AI Custom Extraction Engine path — disclosed, not a bug.

## 3. Server-side deployment: installing Tesseract language packs

The OCR engine (`backend/app/ocr/engine.py`) requires the real `tesseract`
binary on `PATH` — it is not pip-installable; `pytesseract` just shells out
to it. Language recognition requires that language's `.traineddata` file to
be installed in the tessdata directory Tesseract is configured to use.

### macOS (development)

```bash
brew install tesseract tesseract-lang
```

`tesseract-lang` installs every official language pack (100+), covering
all 13 languages in the table above in one step.

### Ubuntu / Debian (production)

```bash
sudo apt-get update
sudo apt-get install -y tesseract-ocr
# Install only the packs this deployment actually needs, e.g. Kannada + Hindi + Tamil:
sudo apt-get install -y tesseract-ocr-kan tesseract-ocr-hin tesseract-ocr-tam
# Or install every language pack Debian ships:
sudo apt-get install -y tesseract-ocr-all
```

### Docker

```dockerfile
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    tesseract-ocr-eng tesseract-ocr-hin tesseract-ocr-kan tesseract-ocr-tam \
    tesseract-ocr-tel tesseract-ocr-mal tesseract-ocr-mar tesseract-ocr-ben \
    tesseract-ocr-guj tesseract-ocr-pan tesseract-ocr-ori tesseract-ocr-asm \
    tesseract-ocr-urd \
    && rm -rf /var/lib/apt/lists/*
```

Trim this list to only the languages an org will actually opt into —
Tesseract's per-pack `.traineddata` files are 5–15MB each, and installing
all 13 adds well under 200MB to an image.

### Manual install (any platform)

Download the specific `.traineddata` file(s) from
[tesseract-ocr/tessdata_fast](https://github.com/tesseract-ocr/tessdata_fast)
into the directory `TESSDATA_PREFIX` points at (or Tesseract's compiled-in
default, typically `/usr/share/tesseract-ocr/*/tessdata/` or
`/opt/homebrew/share/tessdata/` on macOS Homebrew).

## 4. Verifying the deployment

Never assume a language pack is installed — check it. Two ways:

**A. The real health-check endpoint** (Administration → Document
Intelligence → OCR Diagnostics, or directly):

```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost:8080/api/documents/ocr-health
```

Returns, for every language the org has configured in Settings:

```json
{
  "tesseract_available": true,
  "tesseract_error": null,
  "installed_language_count": 126,
  "all_configured_languages_ready": true,
  "languages": [
    {"code": "en", "name": "English", "tesseract_code": "eng", "installed": true, "reason": null},
    {"code": "kn", "name": "Kannada", "tesseract_code": "kan", "installed": false,
     "reason": "Tesseract traineddata for \"kan\" is not installed on this server."}
  ]
}
```

This is a real, live subprocess check (`tesseract --list-langs` under the
hood via `pytesseract.get_languages()`), not a cached or assumed value —
run it again after installing a pack rather than trusting a stale result.

**B. The admin OCR sandbox** (same page, "Test OCR"): upload a real sample
image/PDF and see the actual recognized text, confidence, and detected
language before rolling a language change out to real documents. Scoped to
the local Tesseract engine only — it does not exercise a configured cloud
OCR provider.

## 5. Organization configuration

Administration → Document Intelligence → Languages (Settings section
`document_intelligence`):

| Field | Meaning |
|---|---|
| Supported Languages | Which languages this org's documents may be OCR'd in. English is always implicitly included. |
| Primary Language | `auto` (real per-page hybrid detection) or a specific pinned language (every page OCRs in that language + English; detection is skipped). |
| OCR Confidence Threshold | Below this percent, a document is flagged `OCR_LOW_CONFIDENCE` and routed to Review Queue. |
| Language Mismatch Threshold | Below this percent, language detection is treated as ambiguous (`OCR_LANGUAGE_MISMATCH`) rather than accepted. |
| Enable translation | Disabled — not implemented. |

`OCR Provider` (same tab, separate card) selects Tesseract vs. a cloud
provider connection. The multilingual two-pass detection/re-OCR flow
described in §1 only runs for Tesseract; a cloud provider does its own
recognition and this pipeline only reports the language of the result.

## 6. Error codes

`backend/app/ocr/language_errors.py`'s `LanguageErrorCode` — every failure
state this feature can produce is one of these, never an ad-hoc string:

| Code | Meaning | Where it surfaces |
|---|---|---|
| `OCR_LANGUAGE_MODEL_MISSING` | A requested Tesseract language's `.traineddata` isn't installed. | Real error, fails the document — see §3/§4 to fix. |
| `OCR_EMPTY_RESULT` | OCR produced no readable text at all (after any AI-vision fallback). | Review Queue (OCR type). |
| `OCR_UNICODE_ERROR` | Recognized text contains a Unicode replacement character or fails a UTF-8 round-trip. | Review Queue. |
| `OCR_LOW_CONFIDENCE` | Overall OCR confidence below the org's `ocr_confidence_threshold`. | Review Queue. |
| `OCR_LANGUAGE_NOT_SUPPORTED` | Detected language isn't one the org opted into (only checked when Primary Language is `auto`). | Review Queue. |
| `OCR_LANGUAGE_MISMATCH` | Detection ran but wasn't decisive (below `language_mismatch_threshold`). | Review Queue. |
| `LANGUAGE_DETECTION_FAILED` | Reserved — detector-internal failure. | — |
| `TRANSLATION_FAILED` | Reserved for the not-yet-implemented translation feature. | — |

## 7. Font rendering

Extracted/OCR'd text can be in any of the 13 scripts above (Devanagari,
Kannada, Tamil, Telugu, Malayalam, Bengali, Gujarati, Gurmukhi, Oriya,
Arabic). The frontend loads Noto Sans font families for every script
(`frontend/index.html`) as fallbacks after Inter in `--font-sans`
(`frontend/src/styles/variables.css`) — CSS font-family fallback is
per-character, so Latin/ASCII text still renders in Inter and script text
automatically falls through to the matching Noto Sans family. No
per-deployment font installation is needed; fonts are loaded from Google
Fonts over HTTPS. A fully offline/air-gapped deployment would need to
self-host these font files and update the `<link>` tags in `index.html`
accordingly — not done here, disclosed as a gap for that scenario.

## 8. Known limitations (disclosed)

- **No translation.** Explicitly out of scope for this build — see §1.
- **Cloud OCR providers bypass the two-pass re-OCR logic** — see §5.
- **Only `en`/`hi`/`kn`/`ta` have curated Custom Extraction Engine label
  dictionaries** — every other language works for OCR/detection but the
  no-AI extraction fallback uses generic English labels for those. See §2.
- **Air-gapped deployments need self-hosted fonts** — see §7.
- **The OCR sandbox doesn't test cloud OCR providers** — see §4.
