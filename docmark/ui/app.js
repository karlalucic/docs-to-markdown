// DocMark UI — drop styling, job list, settings modal.
// File ingestion happens in Python via webview.dom.DOMEventHandler; this
// module owns only the visual side of drag-drop and the rendering of updates
// pushed back from Python via window.onJobUpdate.

(() => {
  const dropzone = document.getElementById('dropzone');
  const folderBtn = document.getElementById('folder-btn');
  const jobList = document.getElementById('job-list');
  const emptyHint = document.getElementById('empty-hint');
  const settingsBtn = document.getElementById('settings-btn');
  const modal = document.getElementById('settings-modal');
  const conversionModal = document.getElementById('conversion-modal');
  const conversionCount = document.getElementById('conversion-count');
  const conversionList = document.getElementById('conversion-list');
  const conversionOverflow = document.getElementById('conversion-overflow');
  const conversionConfirm = document.getElementById('conversion-confirm');
  const conversionCancel = document.getElementById('conversion-cancel');

  const jobRows = new Map();
  let pendingSelectionId = null;

  // ---------------------------------------------------------------------
  // Drag styling (Python owns the actual drop event)
  // ---------------------------------------------------------------------
  let dragDepth = 0;

  window.addEventListener('dragenter', (e) => {
    e.preventDefault();
    dragDepth++;
    dropzone.classList.add('is-hover');
  });

  window.addEventListener('dragleave', (e) => {
    e.preventDefault();
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) dropzone.classList.remove('is-hover');
  });

  window.addEventListener('dragover', (e) => {
    e.preventDefault();
  });

  window.addEventListener('drop', (e) => {
    e.preventDefault();
    dragDepth = 0;
    dropzone.classList.remove('is-hover');
  });

  // ---------------------------------------------------------------------
  // Click-to-browse (native file picker, opened on the Python side)
  // ---------------------------------------------------------------------
  dropzone.addEventListener('click', async () => {
    if (!window.pywebview || !window.pywebview.api) return;
    await pickBatch(() => window.pywebview.api.pick_files());
  });

  folderBtn.addEventListener('click', async () => {
    if (!window.pywebview || !window.pywebview.api) return;
    await pickBatch(() => window.pywebview.api.pick_folder());
  });

  async function pickBatch(action) {
    const selection = await action();
    showSelectionPreview(selection);
  }

  window.onSelectionPreview = (payload) => showSelectionPreview(payload);

  conversionModal.addEventListener('click', (e) => {
    if (e.target.closest('[data-conversion-close]')) {
      cancelSelection();
    }
  });
  conversionCancel.addEventListener('click', cancelSelection);
  conversionConfirm.addEventListener('click', confirmSelection);
  window.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (!conversionModal.hidden) {
      cancelSelection();
      return;
    }
    if (!modal.hidden) {
      closeModal();
    }
  });

  function showSelectionPreview(selection) {
    if (!selection || !selection.selection_id || !selection.total) {
      if (jobRows.size === 0) {
        emptyHint.textContent = 'No supported documents found.';
      }
      return;
    }

    if (pendingSelectionId && pendingSelectionId !== selection.selection_id && window.pywebview && window.pywebview.api) {
      window.pywebview.api.cancel_selection(pendingSelectionId);
    }
    pendingSelectionId = selection.selection_id;
    conversionCount.textContent = `${selection.total} file${selection.total === 1 ? '' : 's'} will be converted to Markdown.`;
    conversionList.replaceChildren();

    (selection.files || []).forEach((file) => {
      const li = document.createElement('li');
      const name = document.createElement('span');
      name.className = 'preview-name';
      name.textContent = file.filename;
      name.title = file.path || file.filename;
      li.appendChild(name);
      conversionList.appendChild(li);
    });

    conversionOverflow.textContent = selection.hidden_count > 0
      ? `And ${selection.hidden_count} more.`
      : '';
    conversionConfirm.textContent = `Convert ${selection.total}`;
    conversionModal.hidden = false;
  }

  async function confirmSelection() {
    if (!pendingSelectionId || !window.pywebview || !window.pywebview.api) return;
    const selectionId = pendingSelectionId;
    closeConversionModal();
    await window.pywebview.api.confirm_selection(selectionId);
  }

  async function cancelSelection() {
    const selectionId = pendingSelectionId;
    closeConversionModal();
    if (selectionId && window.pywebview && window.pywebview.api) {
      await window.pywebview.api.cancel_selection(selectionId);
    }
  }

  function closeConversionModal() {
    pendingSelectionId = null;
    conversionModal.hidden = true;
    conversionList.replaceChildren();
    conversionOverflow.textContent = '';
  }

  // ---------------------------------------------------------------------
  // Job updates pushed from Python
  // ---------------------------------------------------------------------
  window.onJobUpdate = (payload) => {
    if (!payload || !payload.job_id) return;
    let row = jobRows.get(payload.job_id);
    if (!row) {
      row = createJobRow(payload);
      jobRows.set(payload.job_id, row);
      jobList.appendChild(row.el);
    }
    updateJobRow(row, payload);
    emptyHint.classList.toggle('hidden', jobRows.size > 0);
  };

  function createJobRow(payload) {
    const li = document.createElement('li');
    li.className = 'job';

    const filename = document.createElement('div');
    filename.className = 'filename';
    filename.textContent = payload.filename;
    filename.title = payload.path || payload.filename;

    const status = document.createElement('div');
    status.className = 'status';

    const progress = document.createElement('div');
    progress.className = 'progress';
    const bar = document.createElement('div');
    bar.className = 'progress-bar';
    progress.appendChild(bar);

    const action = document.createElement('button');
    action.type = 'button';
    action.className = 'action';
    action.textContent = 'Show';
    action.hidden = true;
    action.addEventListener('click', () => {
      const path = row.outputPath || payload.path;
      if (!path) return;
      if (window.pywebview && window.pywebview.api) {
        window.pywebview.api.reveal_in_finder(path);
      }
    });

    li.appendChild(filename);
    li.appendChild(status);
    li.appendChild(progress);
    li.appendChild(action);

    const row = { el: li, status, bar, action, outputPath: null };
    return row;
  }

  function updateJobRow(row, payload) {
    row.el.classList.remove('done', 'failed');
    row.status.classList.remove('done', 'failed');

    if (payload.status === 'queued') {
      row.status.textContent = 'queued';
    } else if (payload.status === 'converting') {
      row.status.textContent = payload.message || 'converting…';
    } else if (payload.status === 'done') {
      row.el.classList.add('done');
      row.status.classList.add('done');
      row.status.textContent = 'done';
      row.action.hidden = false;
      row.outputPath = payload.output || null;
    } else if (payload.status === 'failed') {
      row.el.classList.add('failed');
      row.status.classList.add('failed');
      row.status.textContent = payload.error || payload.message || 'failed';
    }
    const pct = Math.max(0, Math.min(100, Number(payload.percent) || 0));
    row.bar.style.width = pct + '%';
  }

  // ---------------------------------------------------------------------
  // Settings modal
  // ---------------------------------------------------------------------
  const fields = {
    apiKey: document.getElementById('api-key'),
    apiKeyStatus: document.getElementById('api-key-status'),
    model: document.getElementById('model'),
    dpi: document.getElementById('dpi'),
    detail: document.getElementById('detail'),
    forceVision: document.getElementById('force-vision'),
    pptxVision: document.getElementById('pptx-vision'),
    docxImageDesc: document.getElementById('docx-image-desc'),
    cacheEnabled: document.getElementById('cache-enabled'),
  };

  settingsBtn.addEventListener('click', () => openSettings());
  modal.addEventListener('click', (e) => {
    if (e.target.closest('[data-close]')) closeModal();
  });
  document.getElementById('settings-save').addEventListener('click', saveSettings);

  async function openSettings() {
    if (!window.pywebview || !window.pywebview.api) return;
    const s = await window.pywebview.api.get_settings();
    fields.apiKey.value = '';
    fields.apiKey.placeholder = s.has_api_key ? '••••••••  (stored in keychain)' : 'sk-…';
    fields.apiKeyStatus.textContent = s.has_api_key
      ? 'A key is currently saved. Leave blank to keep it.'
      : 'No key saved yet.';
    fields.model.value = s.model || 'gpt-4.1-mini';
    fields.dpi.value = s.dpi || 200;
    fields.detail.value = s.detail || 'high';
    fields.forceVision.checked = !!s.force_vision;
    fields.pptxVision.checked = !!s.office_vision_pptx_enabled;
    fields.docxImageDesc.checked = !!s.docx_image_description_enabled;
    fields.cacheEnabled.checked = s.cache_enabled !== false;
    modal.hidden = false;
  }

  function closeModal() {
    modal.hidden = true;
  }

  async function saveSettings() {
    if (!window.pywebview || !window.pywebview.api) return;
    const payload = {
      model: fields.model.value.trim() || 'gpt-4.1-mini',
      dpi: Number(fields.dpi.value) || 200,
      detail: fields.detail.value,
      force_vision: fields.forceVision.checked,
      office_vision_pptx_enabled: fields.pptxVision.checked,
      docx_image_description_enabled: fields.docxImageDesc.checked,
      cache_enabled: fields.cacheEnabled.checked,
    };
    const newKey = fields.apiKey.value.trim();
    if (newKey) {
      payload.openai_api_key = newKey;
    }
    await window.pywebview.api.save_settings(payload);
    closeModal();
  }
})();
