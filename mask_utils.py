"""
mask_utils.py
Core PII detection & masking logic (adapted from the original MaskDocument
Colab script). No Colab / ipywidgets dependencies — pure functions that a
web backend (Flask) can call directly.
"""

import re
import pytesseract
from pdf2image import convert_from_path
from PIL import ImageDraw

DPI = 300
MASK_COLOR = (0, 0, 0)   # Black. Change to (255, 0, 0) for red.
PADDING = 8

# ── Regex patterns ──
AADHAAR_4DIGIT = re.compile(r'^\d{4}$')
AADHAAR_8DIGIT = re.compile(r'^\d{8}$')
AADHAAR_12 = re.compile(r'^\d{12}$')
DOB_PATTERN = re.compile(r'\b\d{1,2}[\/\-\.]\d{2}[\/\-\.]\d{2,4}\b')
PAN_PATTERN = re.compile(r'\b[A-Z]{5}\d{4}[A-Z]\b')
PHONE_PATTERN = re.compile(r'\b[6-9]\d{9}\b|\b\+91[-\s]?\d{10}\b')
EMAIL_PATTERN = re.compile(r'\b[\w._%+-]+@[\w.-]+\.\w{2,}\b')
NAME_KEYWORDS = ["name", "नाम"]
ADDR_KEYWORDS = ["address", "पता", "addr", "s/o", "w/o", "d/o", "house",
                  "village", "dist", "pin", "state", "road", "nagar", "colony"]

# ── Keyword-based text instruction parser ──
FIELD_KEYWORDS = {
    "aadhaar_number": ["aadhaar number", "aadhar number", "aadhaar no", "uid", "uidai"],
    "aadhaar_name": ["aadhaar name", "aadhar name"],
    "pan_number": ["pan number", "pan no", "permanent account"],
    "pan_name": ["pan name"],
    "dob": ["date of birth", "dob", "birth date", "d.o.b"],
    "address": ["address", "addr"],
    "credit_card_number": ["credit card", "card number", "cc number", "debit card"],
    "phone_number": ["phone", "mobile", "contact number", "cell"],
    "email": ["email", "e-mail", "mail id"],
}

ALL_FIELDS = list(FIELD_KEYWORDS.keys())

# Words that shouldn't be mistaken for a person's name after "all"
# (e.g. "mask all fields" should NOT be read as a name "Fields")
_ALL_STOPWORDS = {
    "fields", "pii", "records", "data", "information", "details",
    "aadhaar", "aadhar", "pan", "kyc", "documents", "entries",
}

# Words that indicate the match should expand to the WHOLE ROW/LINE it's
# found in (useful for tabular documents like bank statements), rather
# than just the matched word itself.
_ROW_SCOPE_WORDS = re.compile(
    r'record|transaction|entr|statement|row|line|detail', re.I
)


def extract_custom_targets(text: str):
    """
    Pulls out specific names/terms the user wants masked wherever they
    appear in the document (not just in a labelled field), e.g.:
        "mask all Unnati's record"        -> [("Unnati", "row")]
        "hide Rohan's transactions"       -> [("Rohan", "row")]
        'redact "Acme Corp" entries'      -> [("Acme Corp", "row")]
        "mask all Priya"                  -> [("Priya", "row")]
    Returns a list of (term, mode) tuples where mode is "row" (mask the
    whole line the term appears in) or "token" (mask just that word/phrase).
    """
    targets = []
    scope = "row" if _ROW_SCOPE_WORDS.search(text) else "token"

    # 1) Quoted terms always win — most explicit signal
    for pat in (re.compile(r'"([^"]+)"'), re.compile(r"'([^']+)'") ):
        for m in pat.finditer(text):
            term = m.group(1).strip()
            if term and not term.lower().endswith("s"):
                targets.append((term, scope))
    if targets:
        return targets

    # 2) Possessive form: "Unnati's record", "Rohan Mehta's transactions"
    m = re.search(
        r"\b([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)\'s\s+"
        r"(record|transaction|entr|detail|data|statement)", text
    )
    if m:
        return [(m.group(1), "row")]

    # 3) "record(s)/transactions of/for X"
    m = re.search(
        r'(?:record[s]?|transaction[s]?|entries)\s+(?:of|for)\s+'
        r'([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)', text
    )
    if m:
        return [(m.group(1), "row")]

    # 4) "mask/hide/redact all X" where X is a capitalised word not a
    #    known field keyword (e.g. "mask all Unnati")
    m = re.search(r'\ball\s+([A-Z][a-zA-Z]+)\b', text)
    if m and m.group(1).lower() not in _ALL_STOPWORDS:
        return [(m.group(1), scope)]

    return targets


def parse_text_instructions(text: str):
    """
    Parses free-text instructions into:
      - config: dict of standard field flags (aadhaar_number, dob, etc.)
      - custom_targets: list of (term, mode) for arbitrary name/entity masking
    """
    text_lower = text.lower()
    config = {k: False for k in FIELD_KEYWORDS}

    if any(w in text_lower for w in ["everything", "all fields", "all pii", "redact all"]):
        config = {k: True for k in FIELD_KEYWORDS}
    else:
        if re.search(r'\bname\b', text_lower) and "aadhaar name" not in text_lower and "pan name" not in text_lower:
            config["aadhaar_name"] = True
            config["pan_name"] = True

        for field, keywords in FIELD_KEYWORDS.items():
            for kw in keywords:
                if kw in text_lower:
                    config[field] = True
                    break

    custom_targets = extract_custom_targets(text)
    return config, custom_targets


# ── Helpers ──

def pad_bbox(x, y, w, h, img_w, img_h, p=PADDING):
    return (max(0, x - p), max(0, y - p), min(img_w, x + w + p), min(img_h, y + h + p))


def draw_mask(draw, bbox):
    draw.rectangle(bbox, fill=MASK_COLOR)


def ocr_data(image):
    return pytesseract.image_to_data(
        image,
        output_type=pytesseract.Output.DICT,
        config="--psm 11 --oem 3",
    )


# ── Field detectors ──

def find_aadhaar_number_bboxes(data, img_w, img_h):
    texts, bboxes, seen = data["text"], [], set()
    for i, t in enumerate(texts):
        if i in seen:
            continue
        try:
            conf = int(data["conf"][i])
        except Exception:
            conf = 0

        # Straight 12-digit token -> tight box
        if AADHAAR_12.match(t) and conf > 50:
            x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
            bboxes.append(pad_bbox(x, y, w, h, img_w, img_h, p=6))
            seen.add(i)
            continue

        # 4 + 8 digit grouping
        if (i < len(texts) - 1 and AADHAAR_4DIGIT.match(t)
                and AADHAAR_8DIGIT.match(texts[i + 1]) and conf > 70):
            j = i + 1
            tops = [data["top"][m] for m in (i, j)]
            heights = [data["height"][m] for m in (i, j)]
            if max(tops) - min(tops) < 30:
                x0 = min(data["left"][i], data["left"][j])
                x1 = max(data["left"][i] + data["width"][i], data["left"][j] + data["width"][j])
                # if grouping would create a very wide box, prefer token-level boxes
                if x1 - x0 > img_w * 0.45:
                    for m in (i, j):
                        x, y, w, h = data["left"][m], data["top"][m], data["width"][m], data["height"][m]
                        bboxes.append(pad_bbox(x, y, w, h, img_w, img_h, p=6))
                        seen.add(m)
                else:
                    y0 = min(tops)
                    y1 = max(tops[m] + heights[m] for m in (0, 1))
                    bboxes.append(pad_bbox(x0, y0, x1 - x0, y1 - y0, img_w, img_h, p=6))
                    seen.update([i, j])
                continue

        # 4+4+4 grouping
        if (i < len(texts) - 2 and AADHAAR_4DIGIT.match(t)
                and AADHAAR_4DIGIT.match(texts[i + 1])
                and AADHAAR_4DIGIT.match(texts[i + 2]) and conf > 70):
            j, k = i + 1, i + 2
            tops = [data["top"][m] for m in (i, j, k)]
            heights = [data["height"][m] for m in (i, j, k)]
            x0 = min(data["left"][i], data["left"][j], data["left"][k])
            x1 = max(data["left"][i] + data["width"][i], data["left"][j] + data["width"][j], data["left"][k] + data["width"][k])
            # if the combined box would be too wide, create tight boxes per token
            if x1 - x0 > img_w * 0.45 or max(tops) - min(tops) > 30:
                for m in (i, j, k):
                    x, y, w, h = data["left"][m], data["top"][m], data["width"][m], data["height"][m]
                    bboxes.append(pad_bbox(x, y, w, h, img_w, img_h, p=6))
                    seen.add(m)
            else:
                y0 = min(tops)
                y1 = max(tops[m] + heights[m] for m in range(3))
                bboxes.append(pad_bbox(x0, y0, x1 - x0, y1 - y0, img_w, img_h, p=6))
                seen.update([i, j, k])
            continue

    return bboxes


def find_name_bboxes(data, img_w, img_h, label=""):
    bboxes = []
    texts = data["text"]
    confs = data.get("conf", [])

    # stop words as single tokens (split phrases)
    stop_words = set()
    for kws in FIELD_KEYWORDS.values():
        for kw in kws:
            stop_words.update(w.strip().lower() for w in kw.split())
    stop_words.update(w.lower() for w in ADDR_KEYWORDS)

    def is_name_token(tok):
        tok_s = tok.strip().strip(',:')
        if not tok_s:
            return False
        # avoid numeric tokens
        if re.match(r'^\d+$', tok_s):
            return False
        # avoid common stop words
        if tok_s.lower() in stop_words:
            return False
        # prefer alphabetic tokens longer than 1
        return any(c.isalpha() for c in tok_s) and len(tok_s) > 1

    for i, t in enumerate(texts):
        try:
            c = int(confs[i])
        except Exception:
            c = 0
        # lower the label confidence threshold so weak OCR labels still trigger
        if any(kw.lower() in t.lower() for kw in NAME_KEYWORDS) and c > 5:
            # search both before and after the label for name tokens
            name_tokens = []
            # look ahead
            for j in range(i + 1, min(i + 6, len(texts))):
                nt = texts[j].strip()
                if not nt:
                    continue
                if not is_name_token(nt):
                    break
                name_tokens.append(j)
                if len(name_tokens) == 3:
                    break
            # if nothing ahead, look behind (some layouts put name before label)
            if not name_tokens:
                for j in range(max(0, i - 5), i):
                    nt = texts[j].strip()
                    if not nt:
                        continue
                    if not is_name_token(nt):
                        continue
                    name_tokens.insert(0, j)
                    if len(name_tokens) == 3:
                        break
            if name_tokens:
                x0 = min(data["left"][k] for k in name_tokens)
                y0 = min(data["top"][k] for k in name_tokens)
                x1 = max(data["left"][k] + data["width"][k] for k in name_tokens)
                y1 = max(data["top"][k] + data["height"][k] for k in name_tokens)
                bboxes.append(pad_bbox(x0, y0, x1 - x0, y1 - y0, img_w, img_h))

            # if still nothing, search the entire OCR line containing the label
            if not name_tokens:
                try:
                    key = _line_key(data, i)
                    # collect name-like tokens on same line, ordered by position
                    line_idxs = [j for j in range(len(texts)) if _line_key(data, j) == key and is_name_token(texts[j])]
                    if line_idxs:
                        # pick up to 3 tokens closest to label
                        line_idxs.sort(key=lambda j: abs(j - i))
                        sel = sorted(line_idxs[:3])
                        x0 = min(data["left"][k] for k in sel)
                        y0 = min(data["top"][k] for k in sel)
                        x1 = max(data["left"][k] + data["width"][k] for k in sel)
                        y1 = max(data["top"][k] + data["height"][k] for k in sel)
                        bboxes.append(pad_bbox(x0, y0, x1 - x0, y1 - y0, img_w, img_h))
                except Exception:
                    pass

    # Fallback: if no name found but aadhaar_name requested, look for long uppercase sequences
    if not bboxes:
        for i, t in enumerate(texts):
            tok = t.strip()
            if len(tok) >= 4 and tok.replace(' ', '').isupper() and any(c.isalpha() for c in tok):
                # capture up to 3 adjacent uppercase tokens
                name_tokens = [i]
                for j in range(i + 1, min(i + 4, len(texts))):
                    nt = texts[j].strip()
                    if nt and nt.replace(' ', '').isupper():
                        name_tokens.append(j)
                    else:
                        break
                x0 = min(data["left"][k] for k in name_tokens)
                y0 = min(data["top"][k] for k in name_tokens)
                x1 = max(data["left"][k] + data["width"][k] for k in name_tokens)
                y1 = max(data["top"][k] + data["height"][k] for k in name_tokens)
                bboxes.append(pad_bbox(x0, y0, x1 - x0, y1 - y0, img_w, img_h))
                break

    return bboxes


def find_aadhaar_name_bboxes(data, img_w, img_h):
    texts = data["text"]
    confs = data.get("conf", [])

    aadhaar_boxes = find_aadhaar_number_bboxes(data, img_w, img_h)
    if not aadhaar_boxes:
        return []

    def is_aadhaar_name_token(tok):
        text = tok.strip().strip(',:')
        if not text:
            return False
        if re.match(r'^\d+$', text):
            return False
        if text.lower() in {
            "male", "female", "m", "f", "dob", "date", "birth", "year",
            "month", "address", "addr", "uid", "uidai", "aadhaar", "aadhar",
            "card", "number", "name", "government", "republic", "india",
            "unique", "identity", "authority", "department"
        }:
            return False
        if len(text) <= 1:
            return False
        return any(c.isalpha() for c in text)

    candidates = []
    # Anchor search region above the Aadhaar number and near its horizontal span.
    ax0, ay0, ax1, ay1 = min(aadhaar_boxes, key=lambda b: b[1])
    search_top = max(0, ay0 - max(120, (ay1 - ay0) * 4))
    search_bottom = ay0 + 20
    search_left = max(0, ax0 - 60)
    search_right = min(img_w, ax1 + 60)

    for i, tok in enumerate(texts):
        if not tok.strip():
            continue
        if not is_aadhaar_name_token(tok):
            continue
        top = data["top"][i]
        left = data["left"][i]
        if not (search_top <= top <= search_bottom):
            continue
        if not (search_left <= left <= search_right):
            continue
        candidates.append(i)

    if not candidates:
        return []

    lines = {}
    for i in candidates:
        key = _line_key(data, i)
        lines.setdefault(key, []).append(i)

    scored_lines = []
    for key, idxs in lines.items():
        if len(idxs) < 2:
            continue
        scored_lines.append((len(idxs), min(data["top"][i] for i in idxs), key))

    if not scored_lines:
        # fallback to any single candidate line if no two-token line exists
        scored_lines = [(1, min(data["top"][i] for i in idxs), key)
                        for key, idxs in lines.items()]

    _, _, best_key = max(scored_lines)
    tokens = sorted(lines[best_key], key=lambda i: data["left"][i])[:4]

    x0 = min(data["left"][i] for i in tokens)
    y0 = min(data["top"][i] for i in tokens)
    x1 = max(data["left"][i] + data["width"][i] for i in tokens)
    y1 = max(data["top"][i] + data["height"][i] for i in tokens)
    return [pad_bbox(x0, y0, x1 - x0, y1 - y0, img_w, img_h, p=6)]


def find_dob_bboxes(data, img_w, img_h, dob_only_index=None):
    all_found = []
    for i, t in enumerate(data["text"]):
        if DOB_PATTERN.search(t) and int(data["conf"][i]) > 30:
            x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
            all_found.append((pad_bbox(x, y, w, h, img_w, img_h), t))
    if dob_only_index is not None:
        sorted_found = sorted(all_found, key=lambda b: b[0][1])
        if len(sorted_found) > dob_only_index:
            return [sorted_found[dob_only_index][0]]
        return []
    return [b[0] for b in all_found]


def find_pan_number_bboxes(data, img_w, img_h):
    bboxes = []
    for i, t in enumerate(data["text"]):
        if PAN_PATTERN.search(t) and int(data["conf"][i]) > 40:
            x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
            bboxes.append(pad_bbox(x, y, w, h, img_w, img_h))
    return bboxes


def find_phone_bboxes(data, img_w, img_h):
    bboxes = []
    for i, t in enumerate(data["text"]):
        if PHONE_PATTERN.search(t) and int(data["conf"][i]) > 40:
            x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
            bboxes.append(pad_bbox(x, y, w, h, img_w, img_h))
    return bboxes


def find_email_bboxes(data, img_w, img_h):
    bboxes = []
    for i, t in enumerate(data["text"]):
        if EMAIL_PATTERN.search(t) and int(data["conf"][i]) > 40:
            x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
            bboxes.append(pad_bbox(x, y, w, h, img_w, img_h))
    return bboxes


def find_address_bboxes(data, img_w, img_h):
    bboxes = []
    texts = data["text"]

    for i, t in enumerate(texts):
        if any(kw.lower() in t.lower() for kw in ["address", "पता"]) and int(data["conf"][i]) > 20:
            addr_tokens = []
            ref_y = data["top"][i]
            for j in range(i + 1, min(i + 25, len(texts))):
                nt = texts[j].strip()
                if not nt:
                    continue
                if data["top"][j] > ref_y + 250:
                    break
                addr_tokens.append(j)
            if addr_tokens:
                x0 = min(data["left"][k] for k in addr_tokens)
                y0 = min(data["top"][k] for k in addr_tokens)
                x1 = max(data["left"][k] + data["width"][k] for k in addr_tokens)
                y1 = max(data["top"][k] + data["height"][k] for k in addr_tokens)
                bboxes.append(pad_bbox(x0, y0, x1 - x0, y1 - y0, img_w, img_h))

    PIN_PATTERN = re.compile(r'\b\d{6}\b')
    for i, t in enumerate(texts):
        tl = t.lower()
        if (any(kw in tl for kw in ["s/o", "w/o", "d/o", "village", "dist", "taluk"]) 
                or PIN_PATTERN.search(t)) and int(data["conf"][i]) > 30:
            x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
            bboxes.append(pad_bbox(x, y, w, h, img_w, img_h))

    return bboxes


# ── Layout fallbacks ──

def get_pan_layout_bboxes(page_img, page_num):
    if page_num != 0:
        return []
    img_w, img_h = page_img.size
    pan_top = 0.52
    regions = [
        (0.04, pan_top + 0.215, 0.42, pan_top + 0.255),
        (0.04, pan_top + 0.285, 0.28, pan_top + 0.325),
    ]
    bboxes = []
    for x0f, y0f, x1f, y1f in regions:
        bboxes.append((int(x0f * img_w), int(y0f * img_h), int(x1f * img_w), int(y1f * img_h)))
    return bboxes


def get_credit_card_layout_bbox(page_img, page_num):
    if page_num != 1:
        return []
    img_w, img_h = page_img.size
    return [(int(0.04 * img_w), int(0.30 * img_h), int(0.72 * img_w), int(0.41 * img_h))]


# ── Custom name / entity masking (for free-form documents like bank statements) ──

def _line_key(data, i):
    """Tesseract groups OCR words by (block, paragraph, line) — this lets
    us treat all words on the same printed line as one 'row'."""
    return (data["block_num"][i], data["par_num"][i], data["line_num"][i])


def _line_bbox(data, i, img_w, img_h):
    """Bounding box spanning every word on the same OCR line as token i."""
    key = _line_key(data, i)
    idxs = [j for j in range(len(data["text"]))
            if data["text"][j].strip() and _line_key(data, j) == key]
    if not idxs:
        return None
    x0 = min(data["left"][j] for j in idxs)
    y0 = min(data["top"][j] for j in idxs)
    x1 = max(data["left"][j] + data["width"][j] for j in idxs)
    y1 = max(data["top"][j] + data["height"][j] for j in idxs)
    return pad_bbox(x0, y0, x1 - x0, y1 - y0, img_w, img_h)


def _find_term_token_runs(data, term):
    """Finds every place `term` (1 or more words) appears in the OCR'd
    text, case-insensitively. Returns a list of index-lists, one per match."""
    term_words = [w.strip(",.:;()").lower() for w in term.split() if w.strip()]
    if not term_words:
        return []
    words = [t.strip(",.:;()").lower() for t in data["text"]]
    n = len(term_words)
    runs = []
    for i in range(len(words) - n + 1):
        if words[i:i + n] == term_words:
            runs.append(list(range(i, i + n)))
    return runs


def find_custom_target_bboxes(data, img_w, img_h, term, mode="token"):
    """
    Locates every occurrence of `term` in the OCR'd page.
    mode="token" -> mask just the matched word(s)
    mode="row"   -> mask the entire printed line/row the match sits on
                    (best for tabular data like bank statement transactions)
    """
    bboxes = []
    for run in _find_term_token_runs(data, term):
        if mode == "row":
            bbox = _line_bbox(data, run[0], img_w, img_h)
            if bbox:
                bboxes.append(bbox)
        else:
            x0 = min(data["left"][j] for j in run)
            y0 = min(data["top"][j] for j in run)
            x1 = max(data["left"][j] + data["width"][j] for j in run)
            y1 = max(data["top"][j] + data["height"][j] for j in run)
            bboxes.append(pad_bbox(x0, y0, x1 - x0, y1 - y0, img_w, img_h))
    return bboxes


# ── Page processor ──

def process_page(page_img, page_num, config, log, custom_targets=None):
    img_w, img_h = page_img.size
    masked = page_img.copy()
    draw = ImageDraw.Draw(masked)
    data = ocr_data(page_img)
    bboxes = []

    for term, mode in (custom_targets or []):
        found = find_custom_target_bboxes(data, img_w, img_h, term, mode=mode)
        if found:
            log.append(f"  matched '{term}' ({mode}) — {len(found)} occurrence(s) on this page")
        bboxes += found

    if config.get("aadhaar_number"):
        bboxes += find_aadhaar_number_bboxes(data, img_w, img_h)

    if config.get("aadhaar_name") or config.get("pan_name"):
        name_bboxes = find_name_bboxes(data, img_w, img_h)
        if config.get("aadhaar_name") and not name_bboxes:
            name_bboxes += find_aadhaar_name_bboxes(data, img_w, img_h)
        bboxes += name_bboxes

    if config.get("dob"):
        if page_num == 1:
            bboxes += find_dob_bboxes(data, img_w, img_h, dob_only_index=1)
        else:
            bboxes += find_dob_bboxes(data, img_w, img_h)

    if config.get("pan_number"):
        pan_ocr = find_pan_number_bboxes(data, img_w, img_h)
        bboxes += pan_ocr if pan_ocr else get_pan_layout_bboxes(page_img, page_num)

    if config.get("credit_card_number"):
        bboxes += get_credit_card_layout_bbox(page_img, page_num)

    if config.get("phone_number"):
        bboxes += find_phone_bboxes(data, img_w, img_h)

    if config.get("email"):
        bboxes += find_email_bboxes(data, img_w, img_h)

    if config.get("address"):
        bboxes += find_address_bboxes(data, img_w, img_h)

    log.append(f"Page {page_num + 1}: applied {len(bboxes)} mask(s)")
    for bbox in bboxes:
        draw_mask(draw, bbox)
    return masked


# ── Public entry point ──

def mask_pdf(input_pdf_path: str, output_pdf_path: str, config: dict, custom_targets=None) -> list:
    """
    Runs the full pipeline: PDF -> images -> OCR + PII detection -> mask -> save PDF.
    custom_targets: list of (term, mode) tuples for arbitrary name/entity
    masking, e.g. [("Unnati", "row")] to black out every row mentioning
    "Unnati" — useful for bank statements or any tabular document where the
    fields you want masked aren't fixed labels like Aadhaar/PAN/DOB.
    Returns a list of log strings describing what was masked (useful for a
    frontend "activity log" panel, optional).
    """
    log = []
    pages = convert_from_path(input_pdf_path, dpi=DPI)
    log.append(f"Converted PDF to {len(pages)} page image(s)")

    masked_pages = [process_page(p, i, config, log, custom_targets) for i, p in enumerate(pages)]

    first = masked_pages[0].convert("RGB")
    rest = [p.convert("RGB") for p in masked_pages[1:]]
    first.save(output_pdf_path, save_all=True, append_images=rest, resolution=DPI)
    log.append("Saved masked PDF")
    return log
