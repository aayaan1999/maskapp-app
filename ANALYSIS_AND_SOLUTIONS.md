# PDF Masking Application - Network Error Analysis & Solutions

## Executive Summary
Your Render free-tier deployment is experiencing network timeouts during file upload due to a combination of **aggressive request timeouts**, **CPU/memory constraints**, **synchronous blocking operations**, and **heavy OCR processing**. The application works fine on local machines or better-resourced servers but struggles on free-tier infrastructure.

---

## 🔴 Critical Issues Identified

### 1. **Gunicorn Worker Timeout (180 seconds)**
**Severity: CRITICAL**

**Location:** `Dockerfile` (line 30)
```dockerfile
CMD gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 180
```

**Problem:**
- Gunicorn kills any request that takes longer than 180 seconds (3 minutes)
- OCR processing on large PDFs easily exceeds this threshold
- The `/extract` endpoint runs OCR on every page (CPU-intensive) which can take 4-10+ minutes for multi-page documents on free-tier infrastructure

**Why it fails on Render:**
- Free tier has CPU throttling and limited resources
- Multi-language OCR (eng+urd+ara) is much slower than English-only
- Large PDFs at 300 DPI consume significant memory and processing time
- With only 2 workers, one slow request blocks others

**Impact Timeline:**
```
Upload → File save (5s) → PDF to images (10s) → OCR page 1 (30-60s) 
→ OCR page 2 (30-60s) → OCR page N (30-60s) ... 
[Total time: 5-15 minutes for 5+ page documents]
↓
@ 180s timeout → Gunicorn kills the worker
↓
Client receives: "Network error"
```

**Solution:**
```dockerfile
CMD gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 600 --keep-alive 75
```
Change timeout from 180 to 600 seconds (10 minutes) and add keep-alive to prevent proxy timeouts.

---

### 2. **Render Platform Timeout Limits**
**Severity: CRITICAL**

**Problem:**
- Render's free tier has platform-level request timeouts of **30 seconds** for HTTP requests
- This is independent of Gunicorn timeout settings
- Even if Gunicorn timeout is increased, Render will still cut off the connection at 30s

**Why this breaks:**
- First page OCR alone can take 20-60 seconds depending on complexity
- Multi-page documents timeout before reaching the end

**Solution:**
- **Upgrade to paid tier** (needed for production): Paid tiers have 300-600 second timeouts
- **Implement async processing** (recommended for all tiers): Move OCR to a background task queue

---

### 3. **No Asynchronous Processing (Blocking Operations)**
**Severity: HIGH**

**Location:** `app.py` lines 28-85 (the `/extract` endpoint)

**Problem:**
```python
@app.route("/extract", methods=["POST"])
def extract():
    # ... file validation ...
    job_id = uuid.uuid4().hex
    input_path = os.path.join(UPLOAD_DIR, f"{job_id}.pdf")
    file.save(input_path)
    
    # ❌ BLOCKS HERE for 5-15+ minutes on large PDFs
    page_images, instances, ocr_cache = pipeline.extract_fields(input_path)
    # ❌ This is synchronous and blocks the entire worker
    
    # ... save results ...
    return jsonify({...})
```

**Why this is problematic:**
- The entire request is blocked waiting for OCR to complete
- During this time, the Gunicorn worker can't serve other requests
- If request takes > 180s, Gunicorn kills it
- If request takes > 30s on Render free tier, the HTTP request dies
- No retry mechanism exists

**Current Flow (Synchronous - BROKEN on Render):**
```
Client Upload → Wait 5-15 min → Get response or timeout
```

**Better Flow (Asynchronous - WORKS everywhere):**
```
Client Upload → Immediate acknowledgment with job_id
                ↓
            Background worker processes OCR in queue
                ↓
            Client polls for status or uses WebSocket
                ↓
            Results ready, client downloads
```

**Solution:**
Implement a job queue (Redis + Celery, or simple threading):
```python
from threading import Thread

@app.route("/extract", methods=["POST"])
def extract():
    job_id = uuid.uuid4().hex
    file.save(input_path)
    
    # Start OCR in background thread
    Thread(target=background_extract, args=(job_id, input_path), daemon=False).start()
    
    # Return immediately
    return jsonify({"job_id": job_id, "status": "processing"})

@app.route("/extract/status/<job_id>", methods=["GET"])
def extract_status(job_id):
    # Poll current status
    job_data = jobs.load_job_data(BASE_DIR, job_id)
    if job_data and "complete" in job_data:
        return jsonify({"status": "complete", "data": job_data})
    return jsonify({"status": "processing"})
```

---

### 4. **Browser Fetch Timeout Issue**
**Severity: MEDIUM**

**Location:** `static/script.js` lines 53-79

**Problem:**
```javascript
async function extractFields(file) {
    setStatus(uploadStatus, "Scanning document and detecting fields…", "loading");
    
    try {
      // ❌ No timeout specified - uses browser default (5-10 min)
      // ❌ If Render kills at 30s, browser doesn't know why
      const res = await fetch("/extract", { method: "POST", body: formData });
      const data = await res.json();
      if (!res.ok) {
        // Generic error message doesn't help user understand timeout
        setStatus(uploadStatus, data.error || "Something went wrong reading that PDF.", "error");
        return;
      }
    } catch (err) {
      // Network error (timeout) gets caught here
      setStatus(uploadStatus, "Network error — please try again.", "error");
    }
}
```

**Why this fails:**
- Fetch has no built-in timeout mechanism
- Browser timeout is browser-dependent (Chrome: ~600s, Firefox: ~90s, Safari: varies)
- Render timeout (30s on free tier) happens first → connection reset
- User sees "Network error" with no context

**Solution:**
```javascript
async function extractFields(file) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 60000); // 60 second timeout
    
    try {
      const res = await fetch("/extract", { 
        method: "POST", 
        body: formData,
        signal: controller.signal
      });
      clearTimeout(timeout);
      // ... rest of code ...
    } catch (err) {
      clearTimeout(timeout);
      if (err.name === 'AbortError') {
        setStatus(uploadStatus, "Processing took too long. Try a smaller PDF or contact support.", "error");
      } else {
        setStatus(uploadStatus, "Network error — please try again.", "error");
      }
    }
}
```

---

### 5. **Memory Constraints on Render Free Tier**
**Severity: HIGH**

**Problem:**
- Free tier: ~512 MB total RAM
- Large PDFs at 300 DPI consume significant memory:
  - Each page at 300 DPI ≈ 15-50 MB in memory
  - 5-page PDF ≈ 75-250 MB just for images
  - Plus OCR cache, Python overhead, Tesseract process
  - Total can easily exceed available memory

**Symptoms:**
- "Network error" after 5-10 minutes (when memory is exhausted)
- Dyno crashes silently
- Slow performance that gets progressively worse

**Solution:**
```python
# Add memory monitoring
import psutil

@app.before_request
def check_memory():
    memory_percent = psutil.virtual_memory().percent
    if memory_percent > 85:
        return jsonify({"error": "Server memory full, please try again later"}), 503

# Add streaming/chunked processing
def extract_fields_streaming(pdf_path):
    """Process one page at a time to minimize memory footprint"""
    page_images = []
    for i, image in enumerate(ocr.pdf_to_images(pdf_path, max_pages=1)):
        # Process and save immediately instead of keeping all in memory
        words, lines = ocr.ocr_page(image)
        # ... save to disk ...
```

---

### 6. **Bug in JavaScript Mask Handler**
**Severity: LOW (Logic Bug)**

**Location:** `static/script.js` lines 316-318

**Problem:**
```javascript
const payload = { job_id: currentJobId, instructions };
if (selectedInstanceIdsArr.length) payload.instance_ids = selectedInstanceIdsArr;  // ❌ NOT DEFINED
else payload.group_ids = selectedGroupIds;  // ❌ NOT DEFINED
const res = await fetch("/mask", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ job_id: currentJobId, group_ids: selected, instructions }),  // Overwrites above
});
```

**Issue:**
- Variables `selectedInstanceIdsArr` and `selectedGroupIds` are never defined
- This causes a runtime error
- But then the code overwrites `payload` completely, so the error is masked

**Fix:**
```javascript
const selected = Array.from(groupsContainer.querySelectorAll("input[type=checkbox]:checked"))
  .map((cb) => cb.dataset.groupId);
const instructions = instructionsEl.value.trim();

const payload = { 
  job_id: currentJobId, 
  group_ids: selected, 
  instructions 
};

const res = await fetch("/mask", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(payload),
});
```

---

### 7. **Missing Error Retry Logic**
**Severity: MEDIUM**

**Problem:**
- No retry mechanism for failed uploads
- No persistent session storage
- User must re-upload entire document if connection drops

**Solution:**
```javascript
async function extractFieldsWithRetry(file, maxRetries = 3) {
  for (let attempt = 1; attempt <= maxRetries; attempt++) {
    try {
      return await extractFields(file);
    } catch (err) {
      if (attempt < maxRetries) {
        setStatus(uploadStatus, `Attempt ${attempt} failed, retrying...`, "loading");
        await new Promise(r => setTimeout(r, 1000 * attempt)); // Exponential backoff
      } else {
        throw err;
      }
    }
  }
}
```

---

### 8. **No Request Size Validation**
**Severity: MEDIUM**

**Location:** `app.py` line 15

**Problem:**
```python
MAX_CONTENT_LENGTH = 25 * 1024 * 1024  # 25 MB limit
```

**Issues:**
- 25 MB limit is okay, but for large PDFs (e.g., 20 pages) at high quality, this can still cause long processing
- No client-side warning before upload
- No file size display before processing

**Solution:**
Add file size check and warn user:
```javascript
function handleFile(file) {
  if (!file.name.toLowerCase().endsWith(".pdf")) {
    setStatus(uploadStatus, "Only PDF files are supported.", "error");
    return;
  }
  
  // Warn about large files
  const sizeMB = (file.size / (1024 * 1024)).toFixed(1);
  if (file.size > 10 * 1024 * 1024) {
    setStatus(uploadStatus, 
      `Large file (${sizeMB} MB) may take 5-10 minutes to process on slow connections. Continue?`, 
      "warning");
    // Add a continue button...
  }
  
  fileNameEl.textContent = `${file.name} (${sizeMB} MB)`;
  extractFields(file);
}
```

---

### 9. **Insufficient Logging**
**Severity: LOW (Debugging)**

**Problem:**
- No timestamp logging for each processing step
- No performance metrics
- Can't identify where exactly the timeout occurs

**Solution:**
```python
import time
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@app.route("/extract", methods=["POST"])
def extract():
    job_id = uuid.uuid4().hex
    start = time.time()
    
    logger.info(f"[{job_id}] Extraction started")
    
    file.save(input_path)
    logger.info(f"[{job_id}] File saved in {time.time()-start:.1f}s")
    
    page_images, instances, ocr_cache = pipeline.extract_fields(input_path)
    logger.info(f"[{job_id}] OCR complete in {time.time()-start:.1f}s, {len(page_images)} pages")
    
    jobs.create_job(BASE_DIR, job_id)
    logger.info(f"[{job_id}] Job saved in {time.time()-start:.1f}s")
    
    return jsonify(...)
```

---

## 🟢 Quick Fixes (Minimal Changes)

### Fix #1: Increase Gunicorn Timeout
**Difficulty:** ⭐ Easy | **Impact:** ⭐⭐⭐⭐⭐ High | **Time:** 2 min

```dockerfile
# Change FROM:
CMD gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 180

# Change TO:
CMD gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 600 --keep-alive 75 --worker-class sync --max-requests 100
```

**Why this helps:**
- Gives OCR up to 10 minutes to complete
- Keep-alive prevents proxy timeout issues
- Max-requests prevents memory leaks from accumulating

---

### Fix #2: Add Request Timeout to JavaScript
**Difficulty:** ⭐ Easy | **Impact:** ⭐⭐⭐ High | **Time:** 5 min

```javascript
// In static/script.js, replace extractFields function (lines 53-79):

async function extractFields(file) {
    setStatus(uploadStatus, "Scanning document and detecting fields…", "loading");
    dropzone.classList.add("dropzone--busy");

    const formData = new FormData();
    formData.append("file", file);

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 630000); // 10.5 minutes

    try {
      const res = await fetch("/extract", { 
        method: "POST", 
        body: formData,
        signal: controller.signal 
      });
      clearTimeout(timeoutId);
      
      const data = await res.json();
      if (!res.ok) {
        setStatus(uploadStatus, data.error || "Something went wrong reading that PDF.", "error");
        dropzone.classList.remove("dropzone--busy");
        return;
      }
      currentJobId = data.job_id;
      renderPreviews(data);
      renderGroups(data);
      setStatus(uploadStatus, "", null);
      dropzone.classList.remove("dropzone--busy");
      stepUpload.hidden = true;
      stepReview.hidden = false;
    } catch (err) {
      clearTimeout(timeoutId);
      if (err.name === 'AbortError') {
        setStatus(uploadStatus, "Processing took too long. Try uploading a smaller PDF.", "error");
      } else {
        setStatus(uploadStatus, "Network error — please try again.", "error");
      }
      dropzone.classList.remove("dropzone--busy");
    }
  }
```

---

### Fix #3: Bug Fix - JavaScript Variable Reference
**Difficulty:** ⭐ Easy | **Impact:** ⭐ Low (Logic bug) | **Time:** 2 min

```javascript
// In static/script.js, replace lines 316-323:

// REPLACE:
const payload = { job_id: currentJobId, instructions };
if (selectedInstanceIdsArr.length) payload.instance_ids = selectedInstanceIdsArr;
else payload.group_ids = selectedGroupIds;
const res = await fetch("/mask", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ job_id: currentJobId, group_ids: selected, instructions }),
});

// WITH:
const payload = {
  job_id: currentJobId,
  group_ids: selected,
  instructions
};
const res = await fetch("/mask", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(payload),
});
```

---

## 🔵 Medium-Term Solutions (Recommended)

### Solution #1: Implement Async Processing
**Difficulty:** ⭐⭐⭐ Medium | **Impact:** ⭐⭐⭐⭐⭐ Critical | **Time:** 2-4 hours

This is the **BEST solution** for production. Allows processing to continue even if request times out.

**Option A: Simple Threading (No Extra Dependencies)**
```python
from threading import Thread
import queue

processing_queue = queue.Queue()

def background_ocr_worker():
    """Worker thread that processes OCR jobs"""
    while True:
        job_id, input_path = processing_queue.get()
        try:
            page_images, instances, ocr_cache = pipeline.extract_fields(input_path)
            jobs.create_job(BASE_DIR, job_id)
            for idx, img in enumerate(page_images):
                jobs.save_page_image(BASE_DIR, job_id, idx, img)
            jobs.save_ocr_data(BASE_DIR, job_id, ocr_cache)
            jobs.save_instances(BASE_DIR, job_id, instances, len(page_images))
            # Mark job as complete
            jobs.mark_job_complete(BASE_DIR, job_id)
        except Exception as e:
            jobs.mark_job_error(BASE_DIR, job_id, str(e))
        finally:
            if os.path.exists(input_path):
                os.remove(input_path)

# Start worker thread on app startup
Thread(target=background_ocr_worker, daemon=False).start()

@app.route("/extract", methods=["POST"])
def extract():
    job_id = uuid.uuid4().hex
    input_path = os.path.join(UPLOAD_DIR, f"{job_id}.pdf")
    file.save(input_path)
    
    # Queue for background processing - return immediately
    processing_queue.put((job_id, input_path))
    
    return jsonify({
        "job_id": job_id,
        "status": "processing",
        "message": "File queued for processing"
    })

@app.route("/extract/status/<job_id>", methods=["GET"])
def extract_status(job_id):
    """Poll job status"""
    job_data = jobs.load_job_data(BASE_DIR, job_id)
    
    if job_data is None:
        return jsonify({"status": "not_found"}), 404
    
    if "error" in job_data:
        return jsonify({
            "status": "error",
            "error": job_data["error"]
        }), 500
    
    if "complete" in job_data and job_data["complete"]:
        groups = pipeline.group_for_ui(job_data["instances"])
        page_previews = []
        try:
            for i in range(job_data["num_pages"]):
                img = jobs.load_page_image(BASE_DIR, job_id, i)
                page_previews.append(_encode_page_preview(img, width=900))
        except:
            pass
        
        return jsonify({
            "status": "complete",
            "data": {
                "job_id": job_id,
                "num_pages": job_data["num_pages"],
                "groups": groups,
                "page_previews": page_previews
            }
        })
    
    return jsonify({"status": "processing"})
```

**Update JavaScript to Poll:**
```javascript
async function extractFields(file) {
    setStatus(uploadStatus, "Uploading document…", "loading");
    dropzone.classList.add("dropzone--busy");

    const formData = new FormData();
    formData.append("file", file);

    try {
      const res = await fetch("/extract", { method: "POST", body: formData });
      const data = await res.json();
      
      if (!res.ok) {
        setStatus(uploadStatus, data.error || "Failed to upload.", "error");
        dropzone.classList.remove("dropzone--busy");
        return;
      }
      
      // File uploaded, now poll for processing status
      currentJobId = data.job_id;
      pollForProcessing();
      
    } catch (err) {
      setStatus(uploadStatus, "Network error — please try again.", "error");
      dropzone.classList.remove("dropzone--busy");
    }
}

async function pollForProcessing() {
    setStatus(uploadStatus, "Scanning document and detecting fields…", "loading");
    
    const maxAttempts = 600; // 10 minutes with 1-second polling
    let attempts = 0;
    
    while (attempts < maxAttempts) {
      try {
        const res = await fetch(`/extract/status/${currentJobId}`);
        const data = await res.json();
        
        if (data.status === "complete") {
          renderPreviews(data.data);
          renderGroups(data.data);
          setStatus(uploadStatus, "", null);
          dropzone.classList.remove("dropzone--busy");
          stepUpload.hidden = true;
          stepReview.hidden = false;
          return;
        } else if (data.status === "error") {
          setStatus(uploadStatus, data.error || "Processing failed.", "error");
          dropzone.classList.remove("dropzone--busy");
          return;
        }
        
        // Still processing, wait and retry
        await new Promise(r => setTimeout(r, 1000));
        attempts++;
        
      } catch (err) {
        setStatus(uploadStatus, "Status check failed, retrying…", "loading");
        await new Promise(r => setTimeout(r, 2000));
        attempts++;
      }
    }
    
    setStatus(uploadStatus, "Processing took too long. Please try again.", "error");
    dropzone.classList.remove("dropzone--busy");
}
```

---

### Solution #2: Upgrade Render Plan
**Difficulty:** ⭐ Easy | **Impact:** ⭐⭐⭐⭐ High | **Cost:** $7-12/month

- Free tier: 30-second timeout
- Starter: $7/month, 300-second timeout, 512 MB RAM
- Standard: $12+/month, 600-second timeout, 1-2 GB RAM

---

### Solution #3: Add Resource Monitoring
**Difficulty:** ⭐⭐ Easy-Medium | **Impact:** ⭐⭐⭐ High | **Time:** 1-2 hours

```bash
# Add to requirements.txt
psutil==5.9.0
```

```python
# Add to app.py
import psutil
import gc

@app.before_request
def check_resources():
    memory_percent = psutil.virtual_memory().percent
    cpu_percent = psutil.cpu_percent(interval=0.1)
    
    if memory_percent > 90:
        gc.collect()  # Force garbage collection
        if memory_percent > 90:
            return jsonify({"error": "Server is under high load. Please try again later."}), 503
    
    if cpu_percent > 95:
        return jsonify({"error": "Server is under high load. Please try again later."}), 503
```

---

## 📋 Priority Action Plan

### Phase 1: Immediate Fixes (Today - 10 minutes)
1. ✅ **Increase Gunicorn timeout** → Dockerfile: change `--timeout 180` to `--timeout 600`
2. ✅ **Fix JavaScript timeout** → static/script.js: add AbortController with 10-minute timeout
3. ✅ **Fix variable bug** → static/script.js: remove undefined variable references
4. ✅ **Redeploy** → Push to Render

**Expected Result:** Will handle most PDFs, but still vulnerable to Render's 30-second HTTP timeout

---

### Phase 2: Better Solution (This week - 2-4 hours)
1. ✅ **Implement async processing** → Add background thread for OCR
2. ✅ **Add polling endpoint** → `/extract/status/<job_id>`
3. ✅ **Update frontend** → Poll status instead of waiting for response
4. ✅ **Add error handling** → Better error messages and retry logic

**Expected Result:** Works reliably on free tier, handles large PDFs gracefully

---

### Phase 3: Production Ready (Next sprint - 4-8 hours)
1. ✅ **Add resource monitoring** → Prevent out-of-memory crashes
2. ✅ **Implement job persistence** → Save progress to disk/database
3. ✅ **Add user notifications** → Email/webhook for job completion
4. ✅ **Upgrade to paid tier** → For guaranteed performance (optional if async is solid)

---

## 🧪 Testing Checklist

After implementing fixes, test with:

- [ ] Small PDF (1 page, 100KB) - Should work instantly
- [ ] Medium PDF (5 pages, 2MB) - Should process in <2 minutes
- [ ] Large PDF (10+ pages, 5MB+) - Should process in 5-15 minutes without timeout
- [ ] Slow network simulation (Chrome DevTools > Network > Slow 3G)
- [ ] Check Render logs for timeout errors
- [ ] Monitor memory usage in Render dashboard

---

## 📊 Performance Expectations

| Factor | Current | After Fixes | Ideal |
|--------|---------|-------------|-------|
| Small PDF | ✅ Works | ✅ Works | ✅ Works (< 30s) |
| Medium PDF | ❌ Times out | ✅ Works | ✅ Works (< 2m) |
| Large PDF | ❌ Times out | ✅ Works (async) | ✅ Works (< 10m) |
| Render free tier support | ❌ No | ⚠️ Partial | ✅ Yes |
| User experience | ❌ Frustrating | ⚠️ Acceptable | ✅ Great |

---

## 💡 Additional Optimizations (Optional)

### 1. Reduce OCR DPI
```python
# In engine/ocr.py, line 29
DPI = 300  # Current: 300 DPI = high quality, slow
DPI = 150  # Change to: 150 DPI = medium quality, 4x faster
```
Trade-off: Slightly lower quality for much faster processing.

### 2. Single-Language OCR
```python
# In engine/ocr.py, line 30
PREFERRED_LANGS = ["eng", "urd", "ara"]  # Current: multi-language, slow
PREFERRED_LANGS = ["eng"]  # Change to: English only, 2-3x faster
```
Only do this if not supporting bilingual documents.

### 3. Add Image Compression
```python
# In engine/pipeline.py
def extract_fields(pdf_path: str, use_ner: bool = True):
    page_images = ocr.pdf_to_images(pdf_path)
    
    # Compress large images to reduce memory
    compressed_images = []
    for img in page_images:
        if img.size[0] > 2000 or img.size[1] > 2000:
            img.thumbnail((2000, 2000), Image.Resampling.LANCZOS)
        compressed_images.append(img)
    
    # ... rest of code ...
```

---

## 🆘 Debugging Network Errors

If you still get "Network error" after applying fixes:

1. **Check Render logs:**
   ```bash
   # In Render dashboard or using Render CLI
   render logs <service-id>
   ```

2. **Look for these patterns:**
   - `Worker killed due to timeout` → Gunicorn timeout (increase it more)
   - `Memory limit exceeded` → Implement async processing or upgrade plan
   - `Connection reset by peer` → Render platform timeout (upgrade or use async)
   - `No such file or directory` → Disk space issue (clean up old uploads)

3. **Add temporary debug logging:**
   ```python
   import time
   import sys
   
   @app.route("/extract", methods=["POST"])
   def extract():
       start = time.time()
       print(f"START: {time.time()}", file=sys.stderr, flush=True)
       # ... code ...
       print(f"COMPLETE: {time.time()-start:.1f}s", file=sys.stderr, flush=True)
   ```

---

## 📞 When to Contact Render Support

- Frequent "out of memory" crashes → Upgrade plan
- Persistent 30-second timeouts after increasing worker timeout → Confirm platform-level limit
- Strange dyno behavior → Check if you're exceeding fair-use policies

---

## 🎯 Recommended Next Steps

1. **Implement Phase 1 fixes TODAY** (10 minutes)
   - Commit and push to Render
   - Test with a medium PDF
   
2. **If Phase 1 works for most users**, implement **Phase 2 (async) this week**
   - More robust long-term solution
   - Free tier friendly
   
3. **If budget allows**, upgrade to **Starter plan** ($7/month)
   - Much better timeout limits
   - More memory
   - Better performance

The **combination of Phase 1 + Phase 2** should completely solve your network error issues on the free tier.
