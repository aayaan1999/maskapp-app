(() => {
  const dropzone = document.getElementById("dropzone");
  const fileInput = document.getElementById("file-input");
  const fileNameEl = document.getElementById("file-name");
  const uploadStatus = document.getElementById("upload-status");

  const stepUpload = document.getElementById("step-upload");
  const stepReview = document.getElementById("step-review");
  const reviewSubhead = document.getElementById("review-subhead");
  const groupsContainer = document.getElementById("groups-container");
  const previewContainer = document.getElementById("preview-container");
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

  // ---------- step 2: render detected field groups ----------
  function renderGroups(data) {
    groupsContainer.innerHTML = "";
    reviewSubhead.textContent = data.num_pages
      ? `${data.num_pages} page(s) scanned. Select what to mask — checking one masks every occurrence.`
      : "Select what to mask — checking one masks every occurrence.";

    if (data.documents && data.documents.length) {
      const template = document.getElementById("document-template");
      for (const doc of data.documents) {
        const section = template.content.firstElementChild.cloneNode(true);
        section.querySelector(".document-panel__label").textContent = doc.label;
        section.querySelector(".document-panel__page").textContent = `Page ${doc.page + 1}`;
        const fieldsGrid = section.querySelector(".document-panel__fields");
        for (const g of doc.fields) {
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
          fieldsGrid.appendChild(label);
        }
        groupsContainer.appendChild(section);
      }
      renderPreview(data);
      return;
    }

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
    renderPreview(data);
  }

  function truncate(s, n = 42) {
    if (!s) return "";
    return s.length > n ? s.slice(0, n) + "…" : s;
  }

  // ---------- preview rendering and interaction ----------
  function renderPreview(data) {
    if (!previewContainer) return;
    previewContainer.innerHTML = "";
    const previews = data.page_previews || [];
    const groups = data.groups || [];
    // maps for selection syncing
    const groupToInstanceIds = {};
    const instanceToGroup = {};
    if (!previews.length) return;

    for (let pageIndex = 0; pageIndex < previews.length; pageIndex++) {
      const src = previews[pageIndex];
      const pageEl = document.createElement("div");
      pageEl.className = "preview-page";
      const img = document.createElement("img");
      img.className = "preview-image";
      img.src = src;
      pageEl.appendChild(img);

      img.addEventListener("load", () => {
        const scale = img.clientWidth / img.naturalWidth || 1;
        for (const g of groups) {
          const bboxes = g.bboxes || [];
          const pages = g.pages || [];
          const instIds = g.instance_ids || [];
          // store mapping
          groupToInstanceIds[g.group_id] = instIds.slice();
          for (const iid of instIds) instanceToGroup[iid] = g.group_id;
          if (!pages.includes(pageIndex)) continue;
          for (let i = 0; i < bboxes.length; i++) {
            const bbox = bboxes[i];
            const instanceId = instIds[i];
            const [x0, y0, x1, y1] = bbox;
            const box = document.createElement("button");
            box.type = "button";
            box.className = "preview-box";
            box.dataset.groupId = g.group_id;
            box.dataset.instanceId = instanceId;
            box.style.left = `${x0 * scale}px`;
            box.style.top = `${y0 * scale}px`;
            box.style.width = `${(x1 - x0) * scale}px`;
            box.style.height = `${(y1 - y0) * scale}px`;
            box.addEventListener("click", (ev) => { ev.stopPropagation(); toggleInstance(instanceId); });
            pageEl.appendChild(box);
          }
        }
        updatePreviewSelection();
      });

      previewContainer.appendChild(pageEl);
    }
    // expose maps for later sync
    previewContainer._groupToInstanceIds = groupToInstanceIds;
    previewContainer._instanceToGroup = instanceToGroup;
  }

  // explicit per-instance selection set
  const selectedInstanceIds = new Set();

  function toggleInstance(instanceId) {
    if (!instanceId) return;
    if (selectedInstanceIds.has(instanceId)) selectedInstanceIds.delete(instanceId);
    else selectedInstanceIds.add(instanceId);
    // update group checkbox to reflect whether any instance in that group is selected
    const groupId = previewContainer._instanceToGroup && previewContainer._instanceToGroup[instanceId];
    if (groupId) {
      const checkbox = groupsContainer.querySelector(`input[data-group-id="${groupId}"]`);
      if (checkbox) checkbox.checked = groupMatchesSelected(groupId);
    }
    updatePreviewSelection();
  }

  function groupMatchesSelected(groupId) {
    const insts = (previewContainer._groupToInstanceIds && previewContainer._groupToInstanceIds[groupId]) || [];
    return insts.some(iid => selectedInstanceIds.has(iid));
  }

  function updatePreviewSelection() {
    if (!previewContainer) return;
    const checkedGroups = new Set(Array.from(groupsContainer.querySelectorAll('input[type=checkbox]:checked')).map(cb => cb.dataset.groupId));
    previewContainer.querySelectorAll('.preview-box').forEach(box => {
      const iid = box.dataset.instanceId;
      const gid = box.dataset.groupId;
      const selected = (iid && selectedInstanceIds.has(iid)) || checkedGroups.has(gid);
      box.classList.toggle('preview-box--selected', !!selected);
    });
  }

  groupsContainer.addEventListener('change', (e) => {
    if (!(e.target && e.target.matches('input[type=checkbox]'))) return;
    const groupId = e.target.dataset.groupId;
    const checked = e.target.checked;
    // when a group's checkbox changes, select/deselect all instances in that group
    const insts = (previewContainer._groupToInstanceIds && previewContainer._groupToInstanceIds[groupId]) || [];
    for (const iid of insts) {
      if (checked) selectedInstanceIds.add(iid);
      else selectedInstanceIds.delete(iid);
    }
    updatePreviewSelection();
  });

  // ---------- step 3: mask & download ----------
  backBtn.addEventListener("click", () => {
    stepReview.hidden = true;
    stepUpload.hidden = false;
    fileInput.value = "";
    fileNameEl.textContent = "";
    instructionsEl.value = "";
    setStatus(maskStatus, "", null);
    currentJobId = null;
  });

  maskBtn.addEventListener("click", async () => {
    if (!currentJobId) return;
    const selectedGroupIds = Array.from(groupsContainer.querySelectorAll("input[type=checkbox]:checked")).map((cb) => cb.dataset.groupId);
    const selectedInstanceIdsArr = Array.from(selectedInstanceIds);
    const instructions = instructionsEl.value.trim();
    if (selectedGroupIds.length === 0 && selectedInstanceIdsArr.length === 0 && !instructions) {
      setStatus(maskStatus, "Select at least one field, or describe what to mask.", "error");
      return;
    }

    setStatus(maskStatus, "Applying redactions…", "loading");
    maskBtn.disabled = true;

    try {
      const payload = { job_id: currentJobId, instructions };
      if (selectedInstanceIdsArr.length) payload.instance_ids = selectedInstanceIdsArr;
      else payload.group_ids = selectedGroupIds;
      const res = await fetch("/mask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
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
