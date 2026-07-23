(() => {
  const dropzone = document.getElementById("dropzone");
  const fileInput = document.getElementById("file-input");
  const fileNameEl = document.getElementById("file-name");
  const uploadStatus = document.getElementById("upload-status");

  const stepUpload = document.getElementById("step-upload");
  const stepReview = document.getElementById("step-review");
  const reviewSubhead = document.getElementById("review-subhead");
  const previewContainer = document.getElementById("preview-container");
  const groupsContainer = document.getElementById("groups-container");
  const instructionsEl = document.getElementById("instructions");
  const maskBtn = document.getElementById("mask-btn");
  const backBtn = document.getElementById("back-btn");
  const maskStatus = document.getElementById("mask-status");

  let currentJobId = null;

  // Categories that are safe to pre-check — clearly identifying fields.
  // Table columns / generic "other" fields / AI-guessed entities are
  // left unchecked by default so a bank statement isn't fully blacked
  // out until the user actually asks for that.
  const DEFAULT_ON_CATEGORIES = new Set(["identity", "contact"]);

  // ---------- dropzone ----------
  dropzone.addEventListener("click", () => fileInput.click());
  dropzone.addEventListener("dragover", (e) => { e.preventDefault(); dropzone.classList.add("dropzone--drag"); });
  dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dropzone--drag"));
  dropzone.addEventListener("drop", (e) => {
    e.preventDefault();
    dropzone.classList.remove("dropzone--drag");
    if (e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0]);
  });
  fileInput.addEventListener("change", () => {
    if (fileInput.files.length) handleFile(fileInput.files[0]);
  });

  function handleFile(file) {
    if (!file.name.toLowerCase().endsWith(".pdf")) {
      setStatus(uploadStatus, "Only PDF files are supported.", "error");
      return;
    }
    fileNameEl.textContent = file.name;
    extractFields(file);
  }

  function setStatus(el, message, kind) {
    el.textContent = message || "";
    el.className = "status" + (kind ? ` status--${kind}` : "");
  }

  // ---------- step 1: extract ----------
  async function extractFields(file) {
    setStatus(uploadStatus, "Scanning document and detecting fields…", "loading");
    dropzone.classList.add("dropzone--busy");

    const formData = new FormData();
    formData.append("file", file);

    try {
      const res = await fetch("/extract", { method: "POST", body: formData });
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
      setStatus(uploadStatus, "Network error — please try again.", "error");
      dropzone.classList.remove("dropzone--busy");
    }
  }

  function renderPreviews(data) {
    previewContainer.innerHTML = "";
    previewContainer.hidden = !data.num_pages;
    if (!data.num_pages || !currentJobId) return;

    const pagesWrap = document.createElement("div");
    pagesWrap.className = "preview-pages";

    for (let idx = 0; idx < data.num_pages; idx += 1) {
      const pageCard = document.createElement("div");
      pageCard.className = "preview-page";

      const img = document.createElement("img");
      img.className = "preview-page__image";
      img.src = `/job-preview/${currentJobId}/${idx}`;
      img.alt = `Page ${idx + 1}`;

      const label = document.createElement("span");
      label.className = "preview-page__label";
      label.textContent = `PAGE ${idx + 1}`;

      pageCard.appendChild(img);
      pageCard.appendChild(label);
      pagesWrap.appendChild(pageCard);
    }

    previewContainer.appendChild(pagesWrap);
  }

  // ---------- step 2: render detected field groups ----------
  function renderGroups(data) {
    groupsContainer.innerHTML = "";
    reviewSubhead.textContent = data.num_pages
      ? `${data.num_pages} page(s) scanned. Select what to mask — checking one masks every occurrence.`
      : "Select what to mask — checking one masks every occurrence.";

    if (!data.groups || data.groups.length === 0) {
      const empty = document.createElement("p");
      empty.className = "subhead";
      empty.textContent = data.message || "No standard fields were detected automatically. Describe what to mask below instead.";
      groupsContainer.appendChild(empty);
      return;
    }

    const byCategory = {};
    for (const g of data.groups) {
      (byCategory[g.category_label] = byCategory[g.category_label] || []).push(g);
    }

    for (const [categoryLabel, groups] of Object.entries(byCategory)) {
      const section = document.createElement("div");
      section.className = "group-section";

      const legend = document.createElement("div");
      legend.className = "fields__legend";
      legend.textContent = categoryLabel.toUpperCase();
      section.appendChild(legend);

      const grid = document.createElement("div");
      grid.className = "fields__grid";

      for (const g of groups) {
        const label = document.createElement("label");
        label.className = "field-toggle field-toggle--rich";

        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.value = g.group_id;
        checkbox.checked = DEFAULT_ON_CATEGORIES.has(g.category);
        checkbox.dataset.groupId = g.group_id;

        const box = document.createElement("span");
        box.className = "field-toggle__box";

        const textWrap = document.createElement("span");
        textWrap.className = "field-toggle__text";

        const title = document.createElement("span");
        title.className = "field-toggle__label";
        title.textContent = `${g.display_label} (${g.count} found)`;

        const sample = document.createElement("span");
        sample.className = "field-toggle__sample";
        const preview = (g.sample_values || []).map(truncate).join(" · ");
        sample.textContent = preview;

        textWrap.appendChild(title);
        if (preview) textWrap.appendChild(sample);

        label.appendChild(checkbox);
        label.appendChild(box);
        label.appendChild(textWrap);
        grid.appendChild(label);
      }

      section.appendChild(grid);
      groupsContainer.appendChild(section);
    }
  }

  function truncate(s, n = 42) {
    if (!s) return "";
    return s.length > n ? s.slice(0, n) + "…" : s;
  }

  // ---------- step 3: mask & download ----------
  backBtn.addEventListener("click", () => {
    stepReview.hidden = true;
    stepUpload.hidden = false;
    fileInput.value = "";
    fileNameEl.textContent = "";
    instructionsEl.value = "";
    previewContainer.innerHTML = "";
    previewContainer.hidden = true;
    setStatus(maskStatus, "", null);
    currentJobId = null;
  });

  maskBtn.addEventListener("click", async () => {
    if (!currentJobId) return;
    const selected = Array.from(groupsContainer.querySelectorAll("input[type=checkbox]:checked"))
      .map((cb) => cb.dataset.groupId);
    const instructions = instructionsEl.value.trim();

    if (selected.length === 0 && !instructions) {
      setStatus(maskStatus, "Select at least one field, or describe what to mask.", "error");
      return;
    }

    setStatus(maskStatus, "Applying redactions…", "loading");
    maskBtn.disabled = true;

    try {
      const res = await fetch("/mask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: currentJobId, group_ids: selected, instructions }),
      });

      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        setStatus(maskStatus, data.error || "Masking failed.", "error");
        maskBtn.disabled = false;
        return;
      }

      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "masked_output.pdf";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);

      setStatus(maskStatus, "Done — your masked PDF has downloaded.", "success");
      maskBtn.disabled = false;
    } catch (err) {
      setStatus(maskStatus, "Network error — please try again.", "error");
      maskBtn.disabled = false;
    }
  });
})();
