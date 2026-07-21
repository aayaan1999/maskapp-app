# ── Base image ──
FROM python:3.11-slim

# ── System dependencies your masking script needs ──
# tesseract-ocr : OCR engine used by pytesseract
# poppler-utils : used by pdf2image to convert PDF pages to images
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# ── App setup ──
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render (and most PaaS platforms) inject $PORT at runtime.
# Default to 8080 for local/Docker-only testing.
ENV PORT=8080
EXPOSE 8080

# gunicorn = production WSGI server (never use Flask's dev server in prod)
CMD gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120
