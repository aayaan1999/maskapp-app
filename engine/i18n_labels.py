"""
engine/i18n_labels.py
GCC identity documents are frequently bilingual — English printed
alongside Urdu (and often Arabic, since Urdu shares its script and a
lot of expat-labor documentation in the Gulf carries both). This module
is the single place that maps a *concept* (date of birth, passport
number, ...) to every keyword we recognize for it, in every supported
language, so every detector stays in sync.

Matching is done as a plain substring test against OCR'd line text, not
word-by-word — this is what lets it work regardless of reading
direction. Tesseract lays LTR and RTL words out left-to-right by pixel
position; we never rely on "the value is after the label", only on
"the value is somewhere else on this same line" (see
detectors.line_value_excluding_label).
"""

LABELS = {
    "name": {
        "en": ["name"],
        "ur": ["نام"],
        "ar": ["الاسم", "اسم"],
    },
    "father_name": {
        "en": ["father's name", "father name", "s/o"],
        "ur": ["والد کا نام", "ولد"],
        "ar": ["اسم الأب"],
    },
    "dob": {
        "en": ["date of birth", "birth date", "dob", "d.o.b"],
        "ur": ["تاریخ پیدائش"],
        "ar": ["تاريخ الميلاد"],
    },
    "date_of_issue": {
        "en": ["date of issue", "issue date", "issued on", "issuing date"],
        "ur": ["تاریخ اجراء", "اجراء کی تاریخ"],
        "ar": ["تاريخ الإصدار"],
    },
    "date_of_expiry": {
        "en": ["date of expiry", "expiry date", "expiration date",
               "date of validation", "valid until", "validity", "valid till"],
        "ur": ["تاریخ اختتام", "میعاد ختم", "تاریخ ختم"],
        "ar": ["تاريخ الانتهاء", "تاريخ الانتهاء الصلاحية"],
    },
    "nationality": {
        "en": ["nationality"],
        "ur": ["قومیت"],
        "ar": ["الجنسية"],
    },
    "gender": {
        "en": ["gender", "sex"],
        "ur": ["جنس"],
        "ar": ["الجنس"],
    },
    "address": {
        "en": ["address"],
        "ur": ["پتہ", "پتا"],
        "ar": ["العنوان"],
    },
    "passport_no": {
        "en": ["passport no", "passport number", "passport num"],
        "ur": ["پاسپورٹ نمبر"],
        "ar": ["رقم الجواز", "رقم جواز السفر"],
    },
    "national_id": {
        "en": ["national id", "id number", "id no", "identity number",
               "identity no", "civil id", "emirates id", "resident id",
               "iqama no", "iqama number", "qid"],
        "ur": ["شناختی کارڈ", "قومی شناختی کارڈ", "شناختی نمبر"],
        "ar": ["رقم الهوية", "الهوية الوطنية", "رقم الإقامة"],
    },
}


def all_keywords(concept: str):
    """Flat list of every keyword (all languages) for a concept."""
    langs = LABELS.get(concept, {})
    out = []
    for kws in langs.values():
        out.extend(kws)
    return out


def line_matches_concept(line_text: str, concept: str) -> bool:
    tl = line_text.lower()
    return any(kw.lower() in tl for kw in all_keywords(concept))


def matched_concepts(line_text: str):
    """Every concept whose keyword appears in this line."""
    tl = line_text.lower()
    return [c for c, langs in LABELS.items()
            if any(kw.lower() in tl for kw in [k for kws in langs.values() for k in kws])]
