"""
ENHANCED VERSION: Async Background Processing
This version implements background OCR processing so large PDFs don't timeout.
Replace the standard app.py with this for production-grade performance on Render free tier.
"""

import os
import uuid
import time
import logging
from threading import Thread
import queue
import json

from flask import Flask, request, render_template, send_file, jsonify, after_this_request

from engine import pipeline, jobs, ner, ocr

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")
JOBS_DIR = os.path.join(BASE_DIR, "jobs")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(JOBS_DIR, exist_ok=True)

MAX_CONTENT_LENGTH = 25 * 1024 * 1024  # 25 MB upload limit

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

# Background job queue
processing_queue = queue.Queue()
ocr_languages = None


def background_ocr_worker():
    """
    Worker thread that processes OCR jobs in the background.
    This allows the HTTP request to return immediately while processing continues.
    """
    logger.info("OCR Worker thread started")
    while True:
        try:
            job_id, input_path = processing_queue.get(timeout=1)
            logger.info(f"[{job_id}] Starting OCR processing")
            start_time = time.time()
            
            try:
                # Run the actual OCR processing
                page_images, instances, ocr_cache = pipeline.extract_fields(input_path)
                elapsed = time.time() - start_time
                logger.info(f"[{job_id}] OCR complete in {elapsed:.1f}s, {len(page_images)} pages")
                
                # Save results to job folder
                jobs.create_job(BASE_DIR, job_id)
                for idx, img in enumerate(page_images):
                    jobs.save_page_image(BASE_DIR, job_id, idx, img)
                jobs.save_ocr_data(BASE_DIR, job_id, ocr_cache)
                jobs.save_instances(BASE_DIR, job_id, instances, len(page_images))
                
                # Mark job as complete
                job_status_path = os.path.join(JOBS_DIR, job_id, "status.json")
                with open(job_status_path, 'w') as f:
                    json.dump({
                        "status": "complete",
                        "completed_at": time.time(),
                        "processing_time": elapsed,
                        "page_count": len(page_images)
                    }, f)
                
                logger.info(f"[{job_id}] Job complete, marked for download")
                
            except Exception as e:
                elapsed = time.time() - start_time
                logger.error(f"[{job_id}] OCR failed after {elapsed:.1f}s: {str(e)}")
                
                # Save error status
                job_status_path = os.path.join(JOBS_DIR, job_id, "status.json")
                with open(job_status_path, 'w') as f:
                    json.dump({
                        "status": "error",
                        "error": str(e),
                        "failed_at": time.time(),
                        "processing_time": elapsed
                    }, f)
                
            finally:
                # Clean up uploaded file
                if os.path.exists(input_path):
                    os.remove(input_path)
                    logger.info(f"[{job_id}] Uploaded file cleaned up")
                
                processing_queue.task_done()
                
        except queue.Empty:
            # No jobs available, continue waiting
            pass
        except Exception as e:
            logger.error(f"Worker error: {e}")


def get_job_status(job_id):
    """Get the current status of a job"""
    job_status_path = os.path.join(JOBS_DIR, job_id, "status.json")
    
    if os.path.exists(job_status_path):
        try:
            with open(job_status_path, 'r') as f:
                return json.load(f)
        except:
            return None
    
    return None


def _encode_page_preview(image, width=900):
    """Encode a page image as a base64 PNG"""
    from io import BytesIO
    import base64
    
    aspect = image.height / image.width
    new_height = int(width * aspect)
    thumb = image.resize((width, new_height), image.Resampling.LANCZOS)
    
    buffer = BytesIO()
    thumb.save(buffer, format="PNG")
    b64 = base64.b64encode(buffer.getvalue()).decode()
    return f"data:image/png;base64,{b64}"


def build_document_groups(instances):
    """Group instances by document type"""
    documents = {}
    for inst in instances:
        doc_type = inst.get("document_type", "general")
        if doc_type not in documents:
            documents[doc_type] = {"count": 0, "instances": []}
        documents[doc_type]["count"] += 1
        documents[doc_type]["instances"].append(inst["id"])
    return documents


@app.route("/")
def index():
    global ocr_languages
    if ocr_languages is None:
        ocr_languages = ocr.active_ocr_langs()
    
    return render_template("index.html", ner_active=ner.ner_available(),
                            ocr_languages=ocr_languages)


@app.route("/extract", methods=["POST"])
def extract():
    """
    FAST endpoint - accepts file and queues it for processing.
    Returns immediately with a job_id for polling.
    """
    logger.info("Extract endpoint called")
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
    
    try:
        # Save file (should be fast)
        file.save(input_path)
        logger.info(f"[{job_id}] File saved, queued for processing")
        
        # Create job folder
        job_folder = os.path.join(JOBS_DIR, job_id)
        os.makedirs(job_folder, exist_ok=True)
        
        # Save initial status
        status_path = os.path.join(job_folder, "status.json")
        with open(status_path, 'w') as f:
            json.dump({
                "status": "processing",
                "queued_at": time.time(),
                "filename": file.filename
            }, f)
        
        # Queue for background processing - THIS RETURNS IMMEDIATELY
        processing_queue.put((job_id, input_path))
        
        return jsonify({
            "job_id": job_id,
            "status": "processing",
            "message": "File received. Processing started. Use /extract/status to check progress."
        }), 202  # 202 = Accepted (not complete yet)
        
    except Exception as e:
        logger.error(f"[{job_id}] Upload failed: {str(e)}")
        if os.path.exists(input_path):
            os.remove(input_path)
        return jsonify({"error": f"Failed to upload: {str(e)}"}), 500


@app.route("/extract/status/<job_id>", methods=["GET"])
def extract_status(job_id):
    """
    Poll the status of an extraction job.
    Client should call this repeatedly until status is 'complete' or 'error'.
    """
    status = get_job_status(job_id)
    
    if status is None:
        return jsonify({"status": "not_found"}), 404
    
    if status["status"] == "error":
        return jsonify({
            "status": "error",
            "error": status.get("error", "Processing failed")
        }), 500
    
    if status["status"] == "complete":
        # Job is complete, load and return the data
        try:
            job_data = jobs.load_job_data(BASE_DIR, job_id)
            if job_data is None:
                return jsonify({"status": "error", "error": "Job data not found"}), 500
            
            groups = pipeline.group_for_ui(job_data["instances"])
            documents = build_document_groups(job_data["instances"])
            
            # Generate page previews
            page_previews = []
            try:
                for i in range(job_data["num_pages"]):
                    img = jobs.load_page_image(BASE_DIR, job_id, i)
                    page_previews.append(_encode_page_preview(img, width=900))
            except Exception as e:
                logger.warning(f"[{job_id}] Failed to generate previews: {e}")
            
            return jsonify({
                "status": "complete",
                "job_id": job_id,
                "num_pages": job_data["num_pages"],
                "groups": groups,
                "documents": documents,
                "page_previews": page_previews,
                "ner_active": ner.ner_available(),
                "ocr_languages": ocr.active_ocr_langs(),
                "gemini_active": ocr.gemini_available(),
                "processing_time": status.get("processing_time", 0)
            })
        except Exception as e:
            logger.error(f"[{job_id}] Error loading job data: {e}")
            return jsonify({"status": "error", "error": "Failed to load results"}), 500
    
    # Still processing
    return jsonify({
        "status": "processing",
        "job_id": job_id,
        "message": "Processing PDF. Check back in a few seconds.",
        "processing_time": time.time() - status.get("queued_at", time.time())
    }), 202


@app.route("/job-preview/<job_id>/<int:page_idx>")
def job_preview(job_id, page_idx):
    image_path = os.path.join(BASE_DIR, "jobs", job_id, f"page_{page_idx}.png")
    if not os.path.exists(image_path):
        return jsonify({"error": "Preview not found"}), 404
    return send_file(image_path, mimetype="image/png")


@app.route("/mask", methods=["POST"])
def mask():
    """Create masked PDF from selected fields"""
    body = request.get_json(silent=True) or {}
    job_id = body.get("job_id")
    selected_group_ids = set(body.get("group_ids", []))
    instructions = (body.get("instructions") or "").strip()

    if not job_id:
        return jsonify({"error": "Missing job_id — please re-upload the document"}), 400

    job_data = jobs.load_job_data(BASE_DIR, job_id)
    if job_data is None:
        return jsonify({"error": "This session has expired — please re-upload the document"}), 400

    all_instances = job_data["instances"]
    num_pages = job_data["num_pages"]

    # Re-derive each instance's group_id the same way group_for_ui does
    selected_instances = [
        inst for inst in all_instances
        if f"{inst['category']}::{inst['field_type']}::{inst['display_label']}" in selected_group_ids
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
    return jsonify({
        "status": "ok",
        "ner_active": ner.ner_available(),
        "ocr_languages": ocr.active_ocr_langs(),
        "queue_size": processing_queue.qsize()
    })


if __name__ == "__main__":
    # Start background OCR worker thread
    worker_thread = Thread(target=background_ocr_worker, daemon=True)
    worker_thread.start()
    logger.info("Application started with background worker thread")
    
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
