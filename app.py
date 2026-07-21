import os
import uuid
import shutil

from flask import Flask, request, render_template, send_file, jsonify, after_this_request

from mask_utils import mask_pdf, parse_text_instructions, ALL_FIELDS

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

MAX_CONTENT_LENGTH = 20 * 1024 * 1024  # 20 MB upload limit

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH


@app.route("/")
def index():
    return render_template("index.html", fields=ALL_FIELDS)


@app.route("/mask", methods=["POST"])
def mask():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No file selected"}), 400

    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Only PDF files are supported"}), 400

    # Checkbox selections are the base config; free-text instructions ADD
    # to them — both standard fields it mentions (e.g. "also mask dob")
    # and any custom name/entity it names (e.g. "mask all Unnati's record").
    config = {field: request.form.get(field) == "on" for field in ALL_FIELDS}
    instructions = request.form.get("instructions", "").strip()
    custom_targets = []
    if instructions:
        text_config, custom_targets = parse_text_instructions(instructions)
        for field in ALL_FIELDS:
            config[field] = config[field] or text_config.get(field, False)

    if not any(config.values()) and not custom_targets:
        return jsonify({"error": "Select at least one field, or describe what to mask"}), 400

    job_id = uuid.uuid4().hex
    input_path = os.path.join(UPLOAD_DIR, f"{job_id}.pdf")
    output_path = os.path.join(OUTPUT_DIR, f"{job_id}_masked.pdf")

    file.save(input_path)

    try:
        log = mask_pdf(input_path, output_path, config, custom_targets)
    except Exception as exc:
        return jsonify({"error": f"Masking failed: {exc}"}), 500
    finally:
        # Always clean up the uploaded original — we don't keep source PDFs
        if os.path.exists(input_path):
            os.remove(input_path)

    @after_this_request
    def cleanup(response):
        # Remove the masked output after it's been sent to the client
        try:
            if os.path.exists(output_path):
                os.remove(output_path)
        except Exception:
            pass
        return response

    return send_file(
        output_path,
        as_attachment=True,
        download_name="masked_output.pdf",
        mimetype="application/pdf",
    )


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
