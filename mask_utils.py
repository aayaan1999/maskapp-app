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
    for pat in (re.compile(r'"([^"]+)"'), re.compile(r"'([^']+)'")):
        for m in pat.finditer(text):
            term = m.group(1).strip()
            if term and not term.lower().endswith("s"):  # skip stray "'s" captures
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
        conf = int(data["conf"][i])

        if AADHAAR_12.match(t) and conf > 50:
            x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
            bboxes.append(pad_bbox(x, y, w, h, img_w, img_h))
            seen.add(i)
            continue

        if (i < len(texts) - 1 and AADHAAR_4DIGIT.match(t)
                and AADHAAR_8DIGIT.match(texts[i + 1]) and conf > 70):
            j = i + 1
            x0 = data["left"][i]
            y0 = min(data["top"][i], data["top"][j])
            x1 = data["left"][j] + data["width"][j]
            y1 = max(data["top"][i] + data["height"][i], data["top"][j] + data["height"][j])
            bboxes.append(pad_bbox(x0, y0, x1 - x0, y1 - y0, img_w, img_h))
            seen.update([i, j])
            continue

        if (i < len(texts) - 2 and AADHAAR_4DIGIT.match(t)
                and AADHAAR_4DIGIT.match(texts[i + 1])
                and AADHAAR_4DIGIT.match(texts[i + 2]) and conf > 70):
            j, k = i + 1, i + 2
            x0 = data["left"][i]
            y0 = min(data["top"][i], data["top"][j], data["top"][k])
            x1 = data["left"][k] + data["width"][k]
            y1 = max(data["top"][m] + data["height"][m] for m in [i, j, k])
            bboxes.append(pad_bbox(x0, y0, x1 - x0, y1 - y0, img_w, img_h))
            seen.update([i, j, k])
    return bboxes


def find_name_bboxes(data, img_w, img_h, label=""):
    """Finds a name value following a 'Name:'-style label. Handles bilingual
    labels like 'नाम / Name' by treating adjacent keyword/punctuation tokens
    as one label unit, rather than stopping at the second keyword."""
    bboxes = []
    texts = data["text"]
    n = len(texts)
    i = 0
    while i < n:
        t = texts[i]
        if any(kw.lower() in t.lower() for kw in NAME_KEYWORDS) and int(data["conf"][i]) > 15:
            # Skip past adjacent label-continuation tokens: punctuation like
            # '/' or ':', or a second "name" keyword right next to this one
            # (bilingual labels print both scripts back to back).
            j = i + 1
            while j < n and (
                texts[j].strip() in ("/", "-", ":", "|", "")
                or any(kw.lower() in texts[j].lower() for kw in NAME_KEYWORDS)
            ):
                j += 1

            if j >= n:
                i = j
                continue

            # Stay strictly on the same OCR line as the first value token —
            # otherwise a short value (e.g. "RAHUL KUMAR") can spill into
            # the NEXT label's tokens (e.g. "PAN:"), which both corrupts
            # the box width (computed from the wrong last token) and masks
            # unrelated text.
            value_line = _line_key(data, j)
            name_tokens = []
            for k in range(j, min(j + 8, n)):
                nt = texts[k].strip()
                if not nt:
                    continue
                if _line_key(data, k) != value_line:
                    break
                if any(kw2.lower() in nt.lower() for kw2 in NAME_KEYWORDS):
                    break
                if re.match(r'^\d+$', nt) and len(nt) > 4:
                    break
                name_tokens.append(k)
                if len(name_tokens) == 4:
                    break
            if name_tokens:
                # min/max across ALL selected tokens — not just the first/last
                # by list order — so the box is correct regardless of order.
                x0 = min(data["left"][k] for k in name_tokens)
                y0 = min(data["top"][k] for k in name_tokens)
                x1 = max(data["left"][k] + data["width"][k] for k in name_tokens)
                y1 = max(data["top"][k] + data["height"][k] for k in name_tokens)
                bboxes.append(pad_bbox(x0, y0, x1 - x0, y1 - y0, img_w, img_h))
            i = j
        else:
            i += 1
    return bboxes


def find_name_above_dob_bboxes(data, img_w, img_h):
    """
    Fallback for ID cards that print the name with NO 'Name:' label at all
    — this is how real Aadhaar cards work (the name just appears as plain
    text). By Aadhaar's standard layout, the name sits on the line(s)
    immediately above the date-of-birth line, so we use that position as
    the signal instead of a keyword.
    Only used when the keyword-based find_name_bboxes finds nothing.
    """
    line_groups = {}
    for i, t in enumerate(data["text"]):
        if not t.strip():
            continue
        key = _line_key(data, i)
        line_groups.setdefault(key, []).append(i)

    lines = []
    for key, idxs in line_groups.items():
        idxs.sort()
        text = " ".join(data["text"][i] for i in idxs)
        top = min(data["top"][i] for i in idxs)
        lines.append({"idxs": idxs, "text": text, "top": top})
    lines.sort(key=lambda l: l["top"])

    dob_line_idx = None
    for li, line in enumerate(lines):
        if DOB_PATTERN.search(line["text"]) or re.search(r'\bDOB\b|जन्म', line["text"], re.I):
            dob_line_idx = li
            break
    if dob_line_idx is None or dob_line_idx == 0:
        return []

    # Take up to 2 lines immediately above the DOB line — real Aadhaar
    # cards print the name in Hindi then English right above the DOB row.
    # Skip lines that look like section headings (fully uppercase, e.g.
    # "AADHAAR CARD") — real names are printed in Title Case / mixed
    # script, not all-caps, so this reliably tells them apart.
    candidate_lines = lines[max(0, dob_line_idx - 2):dob_line_idx]
    bboxes = []
    for line in candidate_lines:
        letters_only = re.sub(r'[^A-Za-z]', '', line["text"])
        if letters_only and letters_only == letters_only.upper() and len(letters_only) > 3:
            continue  # looks like a heading, not a name
        idxs = line["idxs"]
        x0 = min(data["left"][k] for k in idxs)
        y0 = min(data["top"][k] for k in idxs)
        x1 = max(data["left"][k] + data["width"][k] for k in idxs)
        y1 = max(data["top"][k] + data["height"][k] for k in idxs)
        bboxes.append(pad_bbox(x0, y0, x1 - x0, y1 - y0, img_w, img_h))
    return bboxes


def find_dob_bboxes(data, img_w, img_h, label="", dob_only_index=None):
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
        # Lowered confidence threshold — PAN's format (5 letters+4 digits+1
        # letter) is distinctive enough that false positives are rare, and
        # stylised card fonts often get read with lower OCR confidence.
        if PAN_PATTERN.search(t) and int(data["conf"][i]) > 15:
            x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
            bboxes.append(pad_bbox(x, y, w, h, img_w, img_h))
    return bboxes


CARD_GROUP_4 = re.compile(r'^\d{4}$')
CARD_FULL = re.compile(r'^\d{13,19}$')


def find_card_number_bboxes(data, img_w, img_h):
    """
    Finds credit/debit card numbers via OCR — either as one solid run of
    13-19 digits, or as 3-4 separate groups of 4 digits on the same line
    (how card numbers are usually printed, e.g. '4111 2222 3333 4444').
    Replaces the old fixed-rectangle guess, which blacked out a hardcoded
    region regardless of what was actually there.
    """
    texts = data["text"]
    n = len(texts)
    bboxes = []
    seen = set()

    for i in range(n):
        if i in seen:
            continue
        conf = int(data["conf"][i])
        t = texts[i]

        if CARD_FULL.match(t) and conf > 20:
            x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
            bboxes.append(pad_bbox(x, y, w, h, img_w, img_h))
            seen.add(i)
            continue

        if CARD_GROUP_4.match(t) and conf > 30:
            group = [i]
            j = i + 1
            while (j < n and len(group) < 4
                   and CARD_GROUP_4.match(texts[j])
                   and int(data["conf"][j]) > 30
                   and _line_key(data, j) == _line_key(data, i)):
                group.append(j)
                j += 1
            # Require at least 3 groups (12+ digits) so we don't mistake
            # unrelated 4-digit numbers (e.g. a year) for a card number.
            if len(group) >= 3:
                x0 = data["left"][group[0]]
                y0 = min(data["top"][k] for k in group)
                x1 = data["left"][group[-1]] + data["width"][group[-1]]
                y1 = max(data["top"][k] + data["height"][k] for k in group)
                bboxes.append(pad_bbox(x0, y0, x1 - x0, y1 - y0, img_w, img_h))
                seen.update(group)

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


# NOTE: the old get_pan_layout_bboxes() / get_credit_card_layout_bbox()
# fixed-percentage fallbacks have been removed. They guessed a mask
# position using hardcoded page percentages tuned to one specific test
# document, and drew boxes in unrelated places on any other layout. Real
# OCR-based detection (find_pan_number_bboxes, find_card_number_bboxes) is
# used instead — if it can't confidently find the number, nothing is
# masked for that field rather than guessing wrong.


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
        # Run both detectors and take the union rather than "fallback only
        # if page-wide empty" — on documents with multiple ID sections
        # (e.g. Aadhaar + PAN on one page), a labelled "Name:" match in one
        # section would otherwise wrongly suppress the DOB-relative
        # fallback needed for an unlabelled Aadhaar name elsewhere on the
        # same page.
        bboxes += find_name_bboxes(data, img_w, img_h, label=f"P{page_num + 1}")
        bboxes += find_name_above_dob_bboxes(data, img_w, img_h)

    if config.get("dob"):
        # Note: this matches ANY date-shaped text (dd/mm/yyyy etc.), not
        # specifically a birthdate — on documents with many dates (bank
        # statements, invoices) it will mask every date it finds. Best
        # suited to ID-card-style documents with one or two dates.
        bboxes += find_dob_bboxes(data, img_w, img_h)

    if config.get("pan_number"):
        bboxes += find_pan_number_bboxes(data, img_w, img_h)

    if config.get("credit_card_number"):
        bboxes += find_card_number_bboxes(data, img_w, img_h)

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
