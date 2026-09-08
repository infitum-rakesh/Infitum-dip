"""
Real OCR engine for the Documents module (Phase: "real OCR extraction").

Replaces the fabricated OCR stage in document_service.process_document()
(which returned the same "Document processed via OCR engine..." canned
sentence for every file). This module actually opens the uploaded file,
rasterizes PDF pages with PyMuPDF (no poppler dependency), and runs real
text recognition via Tesseract (pytesseract) — genuine extracted text,
per-word confidence scores, and bounding boxes computed from the file
that was actually uploaded.

Requires the `tesseract` binary on PATH (`brew install tesseract` on
macOS — not pip-installable; pytesseract just shells out to it). Raises
OcrEngineError with a real, specific message on any failure (missing
binary, corrupt file, empty page) rather than ever falling back to
fabricated output — a document that can't be OCR'd should end up
FAILED with a real error, not silently "succeed" with fake text.

Only PDF and image documents are OCR'd — Word/Excel/text files carry
their content natively and don't need image-to-text recognition; see
OcrNotApplicableError.
"""
from dataclasses import dataclass
from io import BytesIO

from PIL import Image
import pytesseract

from app.models.document import DocumentType
from app.ocr.language_errors import LanguageErrorCode, LanguageProcessingError

RENDER_DPI = 220  # good balance of OCR accuracy vs. memory/time for A4-ish pages

DEFAULT_LANG = "eng"  # pre-Multilingual-Phase-4 behavior — every existing caller that
# doesn't pass lang= gets byte-identical output to before this parameter existed.


class OcrEngineError(Exception):
    """Real OCR failure — missing tesseract binary, corrupt/unreadable file, etc."""


class OcrNotApplicableError(Exception):
    """Raised for document types OCR doesn't apply to (Word/Excel/text/other)."""


@dataclass
class _PageImage:
    page_num: int
    image: "Image.Image"


def _check_tesseract_available() -> None:
    try:
        pytesseract.get_tesseract_version()
    except Exception as exc:  # pragma: no cover - environment issue, not a code bug
        raise OcrEngineError(
            "The tesseract OCR binary is not available on this server "
            "(install it, e.g. `brew install tesseract` on macOS, and ensure it's on PATH)."
        ) from exc


def get_installed_languages() -> set[str]:
    """Real Tesseract language packs actually installed on this server
    (reads the tessdata directory via `tesseract --list-langs` under the
    hood — pytesseract.get_languages()). Not cached: this is one cheap
    subprocess call, and a server could have a language pack installed
    or removed between calls (e.g. an admin running `brew install
    tesseract-lang` without restarting) — caching a stale "missing"
    result would be exactly the kind of silent wrong-state this feature
    is required to avoid."""
    try:
        return set(pytesseract.get_languages(config=""))
    except Exception as exc:  # pragma: no cover - environment issue, not a code bug
        raise OcrEngineError(f"Could not list installed Tesseract language packs: {exc}") from exc


def verify_language_available(lang: str) -> None:
    """Real Tesseract language-model-availability verification (Multilingual
    Document Processing, Phase 4/5) — `lang` is a Tesseract lang string like
    "kan+eng"; each '+'-joined code must have a real .traineddata file
    installed. Per the user's explicit requirement: a missing model raises
    a real LanguageProcessingError(OCR_LANGUAGE_MODEL_MISSING) — this
    function NEVER silently drops the missing code and falls back to
    whatever else is installed."""
    requested = [code for code in lang.split("+") if code]
    installed = get_installed_languages()
    missing = [code for code in requested if code not in installed]
    if missing:
        raise LanguageProcessingError(
            LanguageErrorCode.OCR_LANGUAGE_MODEL_MISSING,
            f"Tesseract language model(s) not installed on this server: {', '.join(missing)} "
            f"(requested OCR lang=\"{lang}\"). Install the missing traineddata file(s) — e.g. "
            f"`brew install tesseract-lang` on macOS installs all packs, or download the specific "
            f".traineddata file(s) from https://github.com/tesseract-ocr/tessdata_fast into your "
            f"TESSDATA_PREFIX directory — then retry.",
            requested=requested, missing=missing, installed=sorted(installed),
        )


def _load_page_images(file_bytes: bytes, doc_type: DocumentType) -> list[_PageImage]:
    if doc_type == DocumentType.IMAGE:
        try:
            img = Image.open(BytesIO(file_bytes))
            img.load()
        except Exception as exc:
            raise OcrEngineError(f"Could not open image file: {exc}") from exc
        return [_PageImage(page_num=1, image=img.convert("RGB"))]

    if doc_type == DocumentType.PDF:
        try:
            import pymupdf  # local import — only needed for the PDF path
        except ImportError as exc:  # pragma: no cover
            raise OcrEngineError("pymupdf is not installed — required to render PDF pages for OCR.") from exc
        try:
            pdf = pymupdf.open(stream=file_bytes, filetype="pdf")
        except Exception as exc:
            raise OcrEngineError(f"Could not open PDF file: {exc}") from exc
        if pdf.page_count == 0:
            pdf.close()
            raise OcrEngineError("PDF has no pages.")
        pages = []
        zoom = RENDER_DPI / 72.0  # PDF units are 72 DPI by definition
        matrix = pymupdf.Matrix(zoom, zoom)
        for i in range(pdf.page_count):
            pix = pdf[i].get_pixmap(matrix=matrix)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            pages.append(_PageImage(page_num=i + 1, image=img))
        pdf.close()
        return pages

    raise OcrNotApplicableError(
        f"OCR does not apply to {doc_type.value} documents — this format carries its content natively."
    )


def _extract_page(page: _PageImage, lang: str = DEFAULT_LANG) -> dict:
    """Runs Tesseract on one page image. Returns word-level data grouped
    into lines (block_num/par_num/line_num), since one region per single
    word would be too noisy for a bounding-box UI overlay.

    `lang` is a real Tesseract lang string (e.g. "eng", "kan+eng") —
    Multilingual Document Processing, Phase 4/5. Callers must have already
    verified every code in it is installed (verify_language_available());
    this function does not re-check, so it stays a pure, cheap Tesseract
    call usable from a tight per-page loop."""
    data = pytesseract.image_to_data(page.image, lang=lang, output_type=pytesseract.Output.DICT)
    n = len(data["text"])

    lines: dict[tuple, dict] = {}
    for i in range(n):
        word = data["text"][i].strip()
        if not word:
            continue
        conf = float(data["conf"][i])
        if conf < 0:  # Tesseract's sentinel for "not a text detection"
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        left, top, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
        if key not in lines:
            lines[key] = {"words": [], "left": left, "top": top, "right": left + w, "bottom": top + h, "confs": []}
        line = lines[key]
        line["words"].append(word)
        line["left"] = min(line["left"], left)
        line["top"] = min(line["top"], top)
        line["right"] = max(line["right"], left + w)
        line["bottom"] = max(line["bottom"], top + h)
        line["confs"].append(conf)

    regions = []
    for line in lines.values():
        text = " ".join(line["words"])
        avg_conf = round(sum(line["confs"]) / len(line["confs"]), 1)
        regions.append({
            "page": page.page_num,
            "text": text,
            "bbox": [line["left"], line["top"], line["right"] - line["left"], line["bottom"] - line["top"]],
            "confidence": avg_conf,
        })

    full_text = "\n".join(r["text"] for r in regions)
    return {
        "regions": regions,
        "full_text": full_text,
        "width": page.image.width,
        "height": page.image.height,
    }


def render_pages_as_png(file_bytes: bytes, doc_type: DocumentType) -> list[tuple[int, bytes, int, int]]:
    """Renders each page as PNG bytes — (page_num, png_bytes, width, height)
    — for the AI-vision OCR fallback (services/document_service.py). Reuses
    the exact same _load_page_images() PDF rasterization / image-loading
    path run_ocr() uses for Tesseract, so a vision model sees pixel-
    identical pages to what OCR actually attempted, not a separately
    re-rendered/re-encoded version. Raises the same OcrNotApplicableError/
    OcrEngineError run_ocr() would for a non-PDF/IMAGE doc type or a
    corrupt file — the fallback only ever applies where OCR conceptually
    applies too."""
    pages = _load_page_images(file_bytes, doc_type)
    out = []
    for p in pages:
        buf = BytesIO()
        p.image.save(buf, format="PNG")
        out.append((p.page_num, buf.getvalue(), p.image.width, p.image.height))
    return out


def load_pages(file_bytes: bytes, doc_type: DocumentType) -> list[_PageImage]:
    """Public wrapper around _load_page_images() — lets a multi-pass caller
    (app/ocr/multilingual_ocr_service.py's real "detect, then re-OCR the
    pages that need a different language model" flow) rasterize a PDF /
    open an image exactly once and run Tesseract against the same
    in-memory pages more than once, instead of re-rendering the whole
    file per pass."""
    return _load_page_images(file_bytes, doc_type)


def extract_pages(pages: list[_PageImage], lang: str = DEFAULT_LANG) -> dict:
    """Runs Tesseract (with the given lang string) over already-loaded
    pages and aggregates the result into the same shape run_ocr() returns.
    Does NOT verify the language model is installed — callers doing
    multi-pass/per-page language selection call verify_language_available()
    themselves once per distinct lang string they use, rather than paying
    for the check again on every page."""
    all_regions = []
    all_texts = []
    all_confs = []
    page_dims = []
    for page in pages:
        result = _extract_page(page, lang=lang)
        all_regions.extend(result["regions"])
        if result["full_text"]:
            all_texts.append(result["full_text"])
        all_confs.extend(r["confidence"] for r in result["regions"])
        page_dims.append({"page": page.page_num, "width": result["width"], "height": result["height"]})

    overall_confidence = round(sum(all_confs) / len(all_confs), 1) if all_confs else None
    return {
        "full_text": "\n\n".join(all_texts),
        "confidence": overall_confidence,
        "regions": all_regions,
        "pages": page_dims,
        "page_count": len(pages),
    }


def extract_one_page(page: _PageImage, lang: str = DEFAULT_LANG) -> dict:
    """Re-runs Tesseract on a single already-loaded page with a different
    lang string — the real per-page "re-OCR with the detected language"
    step (Multilingual Document Processing, Phase 4/5). Returns the same
    per-page shape _extract_page() does: {"regions", "full_text", "width",
    "height"}."""
    return _extract_page(page, lang=lang)


def run_ocr(file_bytes: bytes, doc_type: DocumentType, lang: str = DEFAULT_LANG) -> dict:
    """
    Real, synchronous OCR — call via asyncio.to_thread() from async code
    (Tesseract is a blocking subprocess call under the hood).

    `lang` is a real Tesseract lang string (default "eng" — pre-Multilingual
    -Phase-4 behavior, unchanged for every caller that doesn't pass one).
    Multi-language/multi-page selection lives in
    app/ocr/multilingual_ocr_service.py, which calls load_pages() +
    extract_pages()/extract_one_page() directly instead of this
    single-pass entry point — this function stays the simple "OCR this
    whole file in one language" building block it always was.

    Returns: {
      "full_text": str, "confidence": float | None,
      "regions": [{"page", "text", "bbox": [x,y,w,h], "confidence"}],
      "pages": [{"page", "width", "height"}],
      "page_count": int,
    }
    Raises OcrEngineError / OcrNotApplicableError / LanguageProcessingError
    (OCR_LANGUAGE_MODEL_MISSING) on failure.
    """
    _check_tesseract_available()
    verify_language_available(lang)
    pages = _load_page_images(file_bytes, doc_type)
    return extract_pages(pages, lang=lang)
