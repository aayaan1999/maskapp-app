const dropzone = document.getElementById('dropzone');
const fileInput = document.getElementById('file-input');
const fileNameEl = document.getElementById('file-name');
const form = document.getElementById('mask-form');
const submitBtn = document.getElementById('submit-btn');
const statusEl = document.getElementById('status');

// ── Dropzone interactions ──
dropzone.addEventListener('click', () => fileInput.click());

dropzone.addEventListener('dragover', (e) => {
  e.preventDefault();
  dropzone.classList.add('dragover');
});

dropzone.addEventListener('dragleave', () => dropzone.classList.remove('dragover'));

dropzone.addEventListener('drop', (e) => {
  e.preventDefault();
  dropzone.classList.remove('dragover');
  if (e.dataTransfer.files.length) {
    fileInput.files = e.dataTransfer.files;
    updateFileName();
  }
});

fileInput.addEventListener('change', updateFileName);

function updateFileName() {
  fileNameEl.textContent = fileInput.files.length ? fileInput.files[0].name : '';
}

// ── Live preview: checkbox -> redaction bar ──
function syncPreview() {
  document.querySelectorAll('.field-toggle input').forEach((cb) => {
    const target = document.querySelector(`.id-row__value[data-field="${cb.name}"]`);
    if (target) {
      if (cb.checked) {
        target.classList.add('masked');
      } else {
        target.classList.remove('masked');
      }
    }
  });
}

document.querySelectorAll('.field-toggle input').forEach((cb) => {
  cb.addEventListener('change', syncPreview);
});

// Apply initial state on load
setTimeout(syncPreview, 0);

// ── Form submit ──
form.addEventListener('submit', async (e) => {
  e.preventDefault();

  if (!fileInput.files.length) {
    setStatus('Please choose a PDF file first.', 'error');
    return;
  }

  const formData = new FormData(form);
  submitBtn.disabled = true;
  setStatus('Processing — running OCR and masking, this can take a moment…', '');

  try {
    const res = await fetch('/mask', { method: 'POST', body: formData });

    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.error || 'Something went wrong while masking the document.');
    }

    const blob = await res.blob();
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'masked_output.pdf';
    document.body.appendChild(a);
    a.click();
    a.remove();
    window.URL.revokeObjectURL(url);

    setStatus('Done — your masked PDF has been downloaded.', 'success');
  } catch (err) {
    setStatus(err.message, 'error');
  } finally {
    submitBtn.disabled = false;
  }
});

function setStatus(msg, kind) {
  statusEl.textContent = msg;
  statusEl.className = 'status' + (kind ? ' ' + kind : '');
}
