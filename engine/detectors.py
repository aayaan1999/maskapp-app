"""
engine/detectors.py
Detects specific, well-known PII fields (Aadhaar, PAN, DOB, phone, email,
card numbers, address, names) plus a generic "any Label: Value" detector
that catches fields the specific detectors don't know about (Father's
Name, Account Number, IFSC Code, Policy No, ...). Every detector returns
a list of `instance` dicts with a common shape so the pipeline can treat
them uniformly.
"""

import json
import re
from .bank_statement import load_bank_statement_config
from .ocr import words_bbox

config = load_bank_statement_config()
regex_fields = config.get("regex_fields", {})
IBAN_PATTERN = re.compile(regex_fields.get("iban", r'\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b'))
ACCOUNT_NO_PATTERN = re.compile(regex_fields.get("account_number", r'\b\d{9,18}\b'))
PHONE_PATTERN = re.compile(regex_fields.get("phone_number", r'\b(?:\+91|0)?[6-9]\d{9}\b'))
EMAIL_PATTERN = re.compile(regex_fields.get("email", r'\b[\w._%+-]+@[\w.-]+\.\w{2,}\b'))

AADHAAR_12 = re.compile(r'^\d{12}$')
AADHAAR_4DIGIT = re.compile(r'^\d{4}$')
AADHAAR_8DIGIT = re.compile(r'^\d{8}$')
DOB_PATTERN = re.compile(r'\b\d{1,2}[\/\-\.]\d{2}[\/\-\.]\d{2,4}\b')
PAN_PATTERN = re.compile(r'\b[A-Z]{5}\d{4}[A-Z]\b')
CARD_FULL = re.compile(r'^\d{13,19}$')
CARD_GROUP_4 = re.compile(r'^\d{4}$')
PIN_PATTERN = re.compile(r'\b\d{6}\b')
PIN_FULLTOKEN = re.compile(r'^\d{6}$')  # whole-token match, so "105000.00" doesn't qualify
IFSC_PATTERN = re.compile(r'\b[A-Z]{4}0[A-Z0-9]{6}\b')

NAME_KEYWORDS = ["name", "नाम"]
ADDR_KEYWORDS = ["address", "पता", "addr", "s/o", "w/o", "d/o", "house",
                 "village", "dist", "pin", "state", "road", "nagar", "colony"]

# Labels the specific detectors already own — the generic detector skips
# these so a field isn't reported twice under two different names.
_OWNED_LABEL_PATTERNS = re.compile(
    r'aadhaar|aadhar|uid|pan\b|permanent account|date of birth|\bdob\b|'
    r'd\.o\.b|phone|mobile|contact no|e-?mail|address|card number|'
    r'card no|debit card|credit card',
    re.I,
)

_LABEL_LINE = re.compile(r'^\s*([A-Za-z][A-Za-z .\'/]{1,40}?)\s*[:\-]\s*(.+)$')


def _mk(field_type, display_label, category, value, page, bbox, iid):
    return {
        "id": iid, "field_type": field_type, "display_label": display_label,
        "category": category, "value": value, "page": page, "bbox": bbox,
    }


class InstanceCounter:
    def __init__(self):
        self.n = 0

    def next(self):
        self.n += 1
        return f"i{self.n}"


_ACCOUNT_CONTEXT = re.compile(r'account|a/c\b|acct', re.I)


def detect_aadhaar_number(words, lines, page, img_w, img_h, counter):
    out, seen = [], set()
    line_text_by_key = {l["key"]: l["text"] for l in lines}
    for i, w in enumerate(words):
        if i in seen or w["conf"] < 40:
            continue
        t = w["text"]
        # A bare 12-digit number is ambiguous with a bank account number —
        # if this word's own row talks about an account, treat it as one
        # rather than an Aadhaar number.
        if _ACCOUNT_CONTEXT.search(line_text_by_key.get(w.get("line_key"), "")):
            continue
        if AADHAAR_12.match(t):
            out.append(_mk("aadhaar_number", "Aadhaar Number", "identity", t,
                            page, words_bbox(words, [i], img_w, img_h), counter.next()))
            seen.add(i)
        elif (i < len(words) - 1 and AADHAAR_4DIGIT.match(t)
              and AADHAAR_8DIGIT.match(words[i + 1]["text"])
              and words[i]["line_key"] == words[i + 1]["line_key"]):
            idxs = [i, i + 1]
            val = " ".join(words[k]["text"] for k in idxs)
            out.append(_mk("aadhaar_number", "Aadhaar Number", "identity", val,
                            page, words_bbox(words, idxs, img_w, img_h), counter.next()))
            seen.update(idxs)
        elif (i < len(words) - 2 and AADHAAR_4DIGIT.match(t)
              and AADHAAR_4DIGIT.match(words[i + 1]["text"])
              and AADHAAR_4DIGIT.match(words[i + 2]["text"])
              and words[i]["line_key"] == words[i + 1]["line_key"] == words[i + 2]["line_key"]):
            idxs = [i, i + 1, i + 2]
            val = " ".join(words[k]["text"] for k in idxs)
            out.append(_mk("aadhaar_number", "Aadhaar Number", "identity", val,
                            page, words_bbox(words, idxs, img_w, img_h), counter.next()))
            seen.update(idxs)
    return out, seen


def detect_pan_number(words, lines, page, img_w, img_h, counter):
    out, seen = [], set()
    for i, w in enumerate(words):
        if PAN_PATTERN.search(w["text"]) and w["conf"] > 10:
            out.append(_mk("pan_number", "PAN Number", "identity", w["text"],
                            page, words_bbox(words, [i], img_w, img_h), counter.next()))
            seen.add(i)
    return out, seen


def detect_dob(words, lines, page, img_w, img_h, counter):
    out, seen = [], set()
    for i, w in enumerate(words):
        if DOB_PATTERN.search(w["text"]) and w["conf"] > 25:
            out.append(_mk("dob", "Date of Birth", "identity", w["text"],
                            page, words_bbox(words, [i], img_w, img_h), counter.next()))
            seen.add(i)
    return out, seen


def detect_phone(words, lines, page, img_w, img_h, counter):
    out, seen = [], set()
    for i, w in enumerate(words):
        if PHONE_PATTERN.search(w["text"]) and w["conf"] > 35:
            out.append(_mk("phone_number", "Phone Number", "contact", w["text"],
                            page, words_bbox(words, [i], img_w, img_h), counter.next()))
            seen.add(i)
    return out, seen


def detect_email(words, lines, page, img_w, img_h, counter):
    out, seen = [], set()
    for i, w in enumerate(words):
        if EMAIL_PATTERN.search(w["text"]) and w["conf"] > 35:
            out.append(_mk("email", "Email Address", "contact", w["text"],
                            page, words_bbox(words, [i], img_w, img_h), counter.next()))
            seen.add(i)
    return out, seen


def detect_iban(words, lines, page, img_w, img_h, counter):
    out, seen = [], set()
    for i, w in enumerate(words):
        if IBAN_PATTERN.search(w["text"]):
            out.append(_mk("iban", "IBAN", "financial", w["text"],
                            page, words_bbox(words, [i], img_w, img_h), counter.next()))
            seen.add(i)
    return out, seen


def detect_account_number(words, lines, page, img_w, img_h, counter):
    out, seen = [], set()
    for i, w in enumerate(words):
        if ACCOUNT_NO_PATTERN.search(w["text"]) and w["conf"] > 20:
            out.append(_mk("account_number", "Account Number", "financial", w["text"],
                            page, words_bbox(words, [i], img_w, img_h), counter.next()))
            seen.add(i)
    return out, seen


def detect_card_number(words, lines, page, img_w, img_h, counter):
    out, seen = [], set()
    n = len(words)
    for i in range(n):
        if i in seen:
            continue
        w = words[i]
        if CARD_FULL.match(w["text"]) and w["conf"] > 15:
            out.append(_mk("credit_card_number", "Card Number", "financial", w["text"],
                            page, words_bbox(words, [i], img_w, img_h), counter.next()))
            seen.add(i)
            continue
        if CARD_GROUP_4.match(w["text"]) and w["conf"] > 25:
            group = [i]
            j = i + 1
            while (j < n and len(group) < 4 and CARD_GROUP_4.match(words[j]["text"])
                   and words[j]["conf"] > 25 and words[j]["line_key"] == w["line_key"]):
                group.append(j)
                j += 1
            if len(group) >= 3:
                val = " ".join(words[k]["text"] for k in group)
                out.append(_mk("credit_card_number", "Card Number", "financial", val,
                                page, words_bbox(words, group, img_w, img_h), counter.next()))
                seen.update(group)
    return out, seen


def detect_address(words, lines, page, img_w, img_h, counter):
    """
    Line-based: an address label pulls in the rest of its own printed
    row, plus at most one following row if that row doesn't look like
    the start of a *different* labelled field (so a 2-line address
    wrapped without a repeated label is still fully covered, without
    also sweeping up the next unrelated field).
    """
    out, seen = [], set()
    claimed_lines = set()
    for li, line in enumerate(lines):
        tl = line["text"].lower()
        if not any(kw.lower() in tl for kw in ["address", "पता"]):
            continue
        if li in claimed_lines:
            continue
        idxs = list(line["word_idxs"])
        claimed_lines.add(li)

        if li + 1 < len(lines):
            nxt = lines[li + 1]
            gap = nxt["top"] - line["bottom"]
            avg_h = max(1, line["bottom"] - line["top"])
            if gap < avg_h * 1.5 and not _LABEL_LINE.match(nxt["text"]):
                idxs += list(nxt["word_idxs"])
                claimed_lines.add(li + 1)

        val = " ".join(words[j]["text"] for j in idxs)[:80]
        out.append(_mk("address", "Address", "contact", val,
                        page, words_bbox(words, idxs, img_w, img_h), counter.next()))
        seen.update(idxs)

    for i, w in enumerate(words):
        if i in seen:
            continue
        tl = w["text"].lower()
        if (any(kw in tl for kw in ["s/o", "w/o", "d/o", "village", "dist", "taluk"])
                or PIN_FULLTOKEN.match(w["text"])) and w["conf"] > 25:
            out.append(_mk("address", "Address", "contact", w["text"],
                            page, words_bbox(words, [i], img_w, img_h), counter.next()))
            seen.add(i)
    return out, seen


def detect_name(words, lines, page, img_w, img_h, counter):
    """Value following a 'Name:' style label (handles bilingual labels)."""
    out, seen = [], set()
    n = len(words)
    i = 0
    while i < n:
        w = words[i]
        if any(kw in w["text"].lower() for kw in NAME_KEYWORDS) and w["conf"] > 10:
            j = i + 1
            while j < n and (words[j]["text"].strip() in ("/", "-", ":", "|", "")
                              or any(kw in words[j]["text"].lower() for kw in NAME_KEYWORDS)):
                j += 1
            if j >= n:
                i = j
                continue
            value_line = words[j]["line_key"]
            name_idxs = []
            for k in range(j, min(j + 8, n)):
                if words[k]["line_key"] != value_line:
                    break
                if any(kw in words[k]["text"].lower() for kw in NAME_KEYWORDS):
                    break
                if re.match(r'^\d+$', words[k]["text"]) and len(words[k]["text"]) > 4:
                    break
                name_idxs.append(k)
                if len(name_idxs) == 4:
                    break
            if name_idxs:
                val = " ".join(words[k]["text"] for k in name_idxs)
                out.append(_mk("person_name", "Name", "identity", val,
                                page, words_bbox(words, name_idxs, img_w, img_h), counter.next()))
                seen.update(name_idxs)
            i = j
        else:
            i += 1
    return out, seen


def detect_generic_labels(words, lines, page, img_w, img_h, counter, already_claimed):
    """
    Catches any "Label: Value" line the specific detectors above don't
    already own — e.g. "Father's Name: ...", "Account No: ...",
    "IFSC Code: ...", "Policy No: ...", "Employee ID: ...". This is what
    makes the tool cover documents beyond the fixed Aadhaar/PAN field set.
    """
    out = []
    for line in lines:
        if any(idx in already_claimed for idx in line["word_idxs"]):
            continue
        m = _LABEL_LINE.match(line["text"])
        if not m:
            continue
        label, value = m.group(1).strip(), m.group(2).strip()
        if _OWNED_LABEL_PATTERNS.search(label) or len(value) < 2:
            continue
        if len(label) < 2 or label.lower() in {"note", "important", "instructions"}:
            continue
        display = " ".join(w.capitalize() for w in label.split())
        out.append(_mk(
            f"label:{label.lower()}", display, "generic", value, page,
            (line["left"], line["top"], line["right"], line["bottom"]),
            counter.next(),
        ))
    return out


ALL_KNOWN_FIELD_TYPES = [
    "aadhaar_number", "person_name", "pan_number", "dob", "address",
    "credit_card_number", "phone_number", "email",
]

FIELD_TYPE_LABELS = {
    "aadhaar_number": "Aadhaar Number",
    "person_name": "Name",
    "pan_number": "PAN Number",
    "dob": "Date of Birth",
    "address": "Address",
    "credit_card_number": "Card Number",
    "phone_number": "Phone Number",
    "email": "Email Address",
}


def run_known_detectors(words, lines, page, img_w, img_h, counter):
    """Runs every specific detector and returns (instances, claimed_word_idxs)."""
    instances = []
    claimed = set()
    for fn in (detect_aadhaar_number, detect_pan_number, detect_dob, detect_phone,
               detect_email, detect_iban, detect_account_number,
               detect_card_number, detect_address, detect_name):
        found, seen = fn(words, lines, page, img_w, img_h, counter)
        instances += found
        claimed |= seen
    return instances, claimed
