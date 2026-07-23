"""
engine/ocr.py
Converts a PDF into page images and runs Tesseract OCR, normalizing the
raw parallel-array output into easy-to-work-with `Word` / `Line` objects
that every detector in this package builds on.

Runs multilingual: English + Urdu + Arabic by default, so GCC identity
documents (which are commonly bilingual, and whose expat-labor
paperwork often carries Urdu alongside English/Arabic) are actually
legible to OCR instead of coming back blank/garbled for the non-Latin
script. Requires the corresponding Tesseract language packs — see
Dockerfile (`tesseract-ocr-urd`, `tesseract-ocr-ara`). If they aren't
installed, this falls back to English-only automatically rather than
failing the whole request.
"""

import json
import logging
import os
import re
import urllib.error
import urllib.request

import pytesseract
from pdf2image import convert_from_path

logger = logging.getLogger(__name__)

DPI = 300
PREFERRED_LANGS = ["eng", "urd", "ara"]

_active_lang_string = None


def _installed_langs():
    try:
        return set(pytesseract.get_languages(config=""))
    except Exception:
        return {"eng"}


def active_ocr_langs():
    """
    Returns the '+'-joined language string Tesseract will actually use,
    computed once and cached — e.g. 'eng+urd+ara', or just 'eng' if the
    extra language packs aren't installed in this environment.
    """
    global _active_lang_string
    if _active_lang_string is None:
        installed = _installed_langs()
        usable = [l for l in PREFERRED_LANGS if l in installed] or ["eng"]
        _active_lang_string = "+".join(usable)
    return _active_lang_string


def pdf_to_images(pdf_path: str, dpi: int = DPI):
    """Returns a list of PIL Images, one per page."""
    return convert_from_path(pdf_path, dpi=dpi)


def gemini_available():
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    return bool(api_key)


def _normalize_text(text):
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def _slugify_label(label):
    slug = re.sub(r"[^a-z0-9]+", "_", _normalize_text(label)).strip("_")
    return slug or "field"


def _guess_category(label):
    label_l = _normalize_text(label)
    if any(k in label_l for k in ["name", "dob", "birth", "passport", "aadhaar", "pan", "national", "id"]):
        return "identity"
    if any(k in label_l for k in ["phone", "email", "address", "mobile", "contact"]):
        return "contact"
    if any(k in label_l for k in ["account", "bank", "branch", "ifsc", "iban", "balance", "amount", "statement", "credit", "debit"]):
        return "financial"
    return "generic"


def _line_bbox(line, img_w, img_h, pad=6):
    if not line:
        return (0, 0, img_w, img_h)
    return pad_bbox(line["left"], line["top"], line["right"], line["bottom"], img_w, img_h, pad=pad)


def _extract_gemini_fields(page_idx, lines, img_w, img_h):
    if not gemini_available():
        return []

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    text = "\n".join(line.get("text", "") for line in lines if line.get("text"))
    prompt = (
        "You are a document field extractor. Read the OCR text from a single page and return ONLY valid JSON.\n"
        "Return an array of objects with the keys 'label' and 'value'.\n"
        "Use concise field names like Name, Date of Birth, Address, Phone Number, Email, Passport Number, PAN, Aadhaar, Account Number, IFSC, Bank Name, Statement Date.\n"
        "Do not include explanations or markdown. If a field is uncertain, omit it.\n\n"
        f"Page {page_idx + 1} OCR text:\n{text[:6000]}"
    )

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
    }

    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("Gemini field extraction failed: %s", exc)
        return []

    try:
        raw_text = body["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return []

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        cleaned = raw_text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].lstrip()
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            return []

    if isinstance(parsed, dict):
        parsed = parsed.get("fields") or parsed.get("results") or []
    if not isinstance(parsed, list):
        return []

    results = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        label = (item.get("label") or item.get("field") or item.get("name") or "").strip()
        value = (item.get("value") or item.get("text") or "").strip()
        if not label or not value:
            continue

        best_line = None
        label_norm = _normalize_text(label)
        value_norm = _normalize_text(value)
        for line in lines:
            line_text = _normalize_text(line.get("text", ""))
            if value_norm and value_norm in line_text:
                best_line = line
                break
            if label_norm and label_norm in line_text:
                best_line = line

        if best_line is None:
            best_line = lines[0] if lines else None

        results.append({
            "label": label,
            "value": value,
            "bbox": _line_bbox(best_line, img_w, img_h) if best_line else (0, 0, img_w, img_h),
        })
    return results


def extract_fields_with_gemini(page_idx, lines, img_w, img_h, counter, claimed):
    extracted = _extract_gemini_fields(page_idx, lines, img_w, img_h)
    if not extracted:
        return []

    instances = []
    for item in extracted:
        label = item["label"]
        value = item["value"]
        category = _guess_category(label)
        field_type = _slugify_label(label)
        display_label = label
        instances.append({
            "id": counter.next(),
            "field_type": field_type,
            "display_label": display_label,
            "category": category,
            "value": value,
            "page": page_idx,
            "bbox": item["bbox"],
        })
        claimed.add(field_type)
    return instances


def _cluster_into_lines(words):
    """
    Groups word indices into visual rows by y-position, rather than
    trusting Tesseract's own block/par/line numbering. In sparse-text
    mode (psm 11, used here because ID-card layouts scatter text in
    boxes) Tesseract frequently assigns words that are visibly on the
    same printed row to different "line" numbers, which silently broke
    every row-aware feature (table headers, same-line name/value
    pairs, and RTL label/value pairing). Clustering by vertical center
    is layout-mode- and reading-direction-independent.
    """
    if not words:
        return []
    order = sorted(range(len(words)), key=lambda i: (words[i]["top"], words[i]["left"]))
    clusters, current = [], [order[0]]
    for i in order[1:]:
        avg_top = sum(words[j]["top"] for j in current) / len(current)
        avg_h = sum(words[j]["height"] for j in current) / len(current)
        if abs(words[i]["top"] - avg_top) <= max(8, avg_h * 0.6):
            current.append(i)
        else:
            clusters.append(current)
            current = [i]
    clusters.append(current)
    return clusters


def ocr_page(image):
    """
    Runs Tesseract on a single page image and returns:
      words: list of dicts {text,left,top,width,height,right,bottom,conf,line_key}
      lines: list of dicts {key,text,left,top,right,bottom,word_idxs}
    line_key groups words by visual row (see _cluster_into_lines).

    Note on line `text`: words are joined left-to-right by pixel
    position for *display* purposes only. For a right-to-left Urdu/
    Arabic line this is not correct reading order — detectors that need
    to know "does this line mention concept X" or "mask everything on
    this line except the label" don't care about reading order and are
    unaffected; only human-facing preview text may read reversed.
    """
    raw = pytesseract.image_to_data(
        image, output_type=pytesseract.Output.DICT,
        lang=active_ocr_langs(), config="--psm 11 --oem 3",
    )
    words = []
    n = len(raw["text"])
    for i in range(n):
        text = raw["text"][i]
        if not text or not text.strip():
            continue
        left, top = raw["left"][i], raw["top"][i]
        width, height = raw["width"][i], raw["height"][i]
        words.append({
            "text": text,
            "left": left, "top": top, "width": width, "height": height,
            "right": left + width, "bottom": top + height,
            "conf": int(float(raw["conf"][i])) if raw["conf"][i] not in ("-1", -1) else -1,
        })

    clusters = _cluster_into_lines(words)
    for line_idx, idxs in enumerate(clusters):
        for i in idxs:
            words[i]["line_key"] = line_idx

    lines = []
    for line_idx, idxs in enumerate(clusters):
        idxs = sorted(idxs, key=lambda i: words[i]["left"])
        text = " ".join(words[i]["text"] for i in idxs)
        left = min(words[i]["left"] for i in idxs)
        top = min(words[i]["top"] for i in idxs)
        right = max(words[i]["right"] for i in idxs)
        bottom = max(words[i]["bottom"] for i in idxs)
        lines.append({
            "key": line_idx, "text": text, "left": left, "top": top,
            "right": right, "bottom": bottom, "word_idxs": idxs,
        })
    lines.sort(key=lambda l: (l["top"], l["left"]))

    return words, lines


def pad_bbox(left, top, right, bottom, img_w, img_h, pad=6):
    return (
        max(0, left - pad),
        max(0, top - pad),
        min(img_w, right + pad),
        min(img_h, bottom + pad),
    )


def words_bbox(words, idxs, img_w, img_h, pad=6):
    left = min(words[i]["left"] for i in idxs)
    top = min(words[i]["top"] for i in idxs)
    right = max(words[i]["right"] for i in idxs)
    bottom = max(words[i]["bottom"] for i in idxs)
    return pad_bbox(left, top, right, bottom, img_w, img_h, pad)
