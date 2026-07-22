import os
import uuid
import base64
from io import BytesIO

from flask import Flask, request, render_template, send_file, jsonify, after_this_request

from engine import pipeline, jobs, ner

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(os.path.join(BASE_DIR, "jobs"), exist_ok=True)

MAX_CONTENT_LENGTH = 25 * 1024 * 1024  # 25 MB upload limit

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH


def infer_doc_label(instances):
    field_types = {inst["field_type"] for inst in instances}
    if "aadhaar_number" in field_types or "aadhaar_name" in field_types:
        return "Aadhaar Card"
    if "pan_number" in field_types or "pan_name" in field_types:
        return "PAN Card"
    if "credit_card_number" in field_types:
        return "Credit Card"
    if "dob" in field_types and "person_name" in field_types:
        return "Identity Document"
    return "Document"


def build_document_groups(instances):
    pages = {}
    for inst in instances:
        pages.setdefault(inst["page"], []).append(inst)

    documents = []
    for page in sorted(pages):
        page_insts = pages[page]
        fields = {}
        for inst in page_insts:
            key = (inst["category"], inst["field_type"], inst["display_label"])
            if key not in fields:
                fields[key] = {
                    "group_id": f"{page}::{inst['category']}::{inst['field_type']}::{inst['display_label']}",
                    "category": inst["category"],
                    "display_label": inst["display_label"],
                    "count": 0,
                    "sample_values": [],
                }
            fields[key]["count"] += 1
            if len(fields[key]["sample_values"]) < 3:
                fields[key]["sample_values"].append(inst["value"])

        documents.append({
            "page": page,
            "label": infer_doc_label(page_insts),
            "fields": sorted(fields.values(), key=lambda f: (f["category"], f["display_label"])),
        })
    return documents


def _encode_page_preview(image, width=900):
    """Resize a PIL image and return a data URL (PNG) for frontend previews."""
    try:
        ratio = width / image.width
        preview = image.resize((int(width), int(image.height * ratio)))
    except Exception:
        preview = image
    buf = BytesIO()
    preview.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


@app.route("/")
def index():
    return render_template("index.html", ner_active=ner.ner_available())


@app.route("/extract", methods=["POST"])
def extract():
    jobs.cleanup_stale_jobs(BASE_DIR)

    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No file selected"}), 400
    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Only PDF files are supported"}), 400

    job_id = uuid.uuid4().hex
    input_path = os.path.join(UPLOAD_DIR, f"{job_id}.pdf")
    file.save(input_path)

    try:
        page_images, instances, ocr_cache = pipeline.extract_fields(input_path)
    except Exception as exc:
        if os.path.exists(input_path):
            os.remove(input_path)
        return jsonify({"error": f"Could not read this PDF: {exc}"}), 500

    if os.path.exists(input_path):
        os.remove(input_path)  # never keep the original once it's been OCR'd

    jobs.create_job(BASE_DIR, job_id)
    for idx, img in enumerate(page_images):
        jobs.save_page_image(BASE_DIR, job_id, idx, img)
    jobs.save_ocr_data(BASE_DIR, job_id, ocr_cache)
    jobs.save_instances(BASE_DIR, job_id, instances, len(page_images))

    groups = pipeline.group_for_ui(instances)
    documents = build_document_groups(instances)
    # small PNG previews for frontend overlay; encoded as data URLs
    try:
        page_previews = [_encode_page_preview(img, width=900) for img in page_images]
    except Exception:
        page_previews = []

    if not groups and not documents:
        return jsonify({
            "job_id": job_id,
            "num_pages": len(page_images),
            "groups": [],
            "documents": [],
            "page_previews": page_previews,
            "ner_active": ner.ner_available(),
            "message": "No standard fields were detected automatically. "
                       "You can still describe what to mask in plain text below.",
        })

    return jsonify({
        "job_id": job_id,
        "num_pages": len(page_images),
        "groups": groups,
        "documents": documents,
        "page_previews": page_previews,
        "ner_active": ner.ner_available(),
    })


@app.route("/mask", methods=["POST"])
def mask():
    body = request.get_json(silent=True) or {}
    job_id = body.get("job_id")
    selected_group_ids = set(body.get("group_ids", []))
    selected_instance_ids = set(body.get("instance_ids", []) or body.get("instance_ids", []))
    instructions = (body.get("instructions") or "").strip()

    if not job_id:
        return jsonify({"error": "Missing job_id — please re-upload the document"}), 400

    job_data = jobs.load_job_data(BASE_DIR, job_id)
    if job_data is None:
        return jsonify({"error": "This session has expired — please re-upload the document"}), 400

    all_instances = job_data["instances"]
    num_pages = job_data["num_pages"]

    # If explicit instance ids were passed (user clicked boxes), prefer those.
    if selected_instance_ids:
        selected_instances = [inst for inst in all_instances if inst.get("id") in selected_instance_ids]
    else:
        # Re-derive each instance's group_id the same way group_for_ui does,
        # so a selected checkbox maps back to every matching instance.
        selected_instances = [
            inst for inst in all_instances
            if f"{inst['page']}::{inst['category']}::{inst['field_type']}::{inst['display_label']}" in selected_group_ids
            or f"{inst['category']}::{inst['field_type']}::{inst['display_label']}" in selected_group_ids
        ]

    if instructions:
        ocr_cache = jobs.load_ocr_data(BASE_DIR, job_id)
        if ocr_cache:
            selected_instances += pipeline.run_custom_search(ocr_cache, instructions)

    if not selected_instances:
        return jsonify({"error": "Select at least one field, or describe what to mask"}), 400

    try:
        page_images = [jobs.load_page_image(BASE_DIR, job_id, i) for i in range(num_pages)]
    except Exception as exc:
        return jsonify({"error": f"Session data missing: {exc}"}), 400

    output_path = os.path.join(OUTPUT_DIR, f"{job_id}_masked.pdf")
    try:
        pipeline.render_masked_pdf(page_images, selected_instances, output_path)
    except Exception as exc:
        return jsonify({"error": f"Masking failed: {exc}"}), 500
    finally:
        jobs.cleanup_job(BASE_DIR, job_id)

    @after_this_request
    def cleanup(response):
        try:
            if os.path.exists(output_path):
                os.remove(output_path)
        except Exception:
            pass
        return response

    return send_file(
        output_path, as_attachment=True,
        download_name="masked_output.pdf", mimetype="application/pdf",
    )


@app.route("/health")
def health():
    return jsonify({"status": "ok", "ner_active": ner.ner_available()})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
