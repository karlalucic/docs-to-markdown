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
  const defaultSettings = {
    has_api_key: false,
    model: 'gpt-4.1-mini',
    dpi: 200,
    detail: 'high',
    force_vision: false,
    office_vision_pptx_enabled: false,
    docx_image_description_enabled: false,
    cache_enabled: true,
  };
  let pendingSelectionId = null;
  let pickInFlight = false;
  let settingsSnapshot = { ...defaultSettings };
  let settingsLoadToken = 0;
  let settingsDirty = false;

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
    await pickBatch(() => window.pywebview.api.pick_files(), dropzone);
  });

  folderBtn.addEventListener('click', async () => {
    if (!window.pywebview || !window.pywebview.api) return;
    await pickBatch(() => window.pywebview.api.pick_folder(), folderBtn);
  });

  async function pickBatch(action, button) {
    if (pickInFlight) return;
    pickInFlight = true;
    setBusy(button, true);
    try {
      const selection = await action();
      showSelectionPreview(selection);
    } finally {
      setBusy(button, false);
      pickInFlight = false;
    }
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
    const skippedCount = Number(selection && selection.skipped_count) || 0;
    if (!selection || !selection.selection_id || !selection.total) {
      if (skippedCount > 0) {
        emptyHint.textContent = `${skippedCount} already converted document${skippedCount === 1 ? '' : 's'} skipped.`;
        emptyHint.classList.remove('hidden');
        return;
      }
      if (jobRows.size === 0) {
        emptyHint.textContent = 'No supported documents found.';
      }
      return;
    }

    if (pendingSelectionId && pendingSelectionId !== selection.selection_id && window.pywebview && window.pywebview.api) {
      window.pywebview.api.cancel_selection(pendingSelectionId);
    }
    pendingSelectionId = selection.selection_id;
    conversionCount.textContent = `${selection.total} file${selection.total === 1 ? '' : 's'} will be converted to Markdown.`
      + (skippedCount > 0 ? ` ${skippedCount} already converted will be skipped.` : '');
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
    conversionConfirm.disabled = false;
    conversionCancel.disabled = false;
    conversionModal.hidden = false;
  }

  async function confirmSelection() {
    if (!pendingSelectionId || !window.pywebview || !window.pywebview.api || conversionConfirm.disabled) return;
    const selectionId = pendingSelectionId;
    setBusy(conversionConfirm, true);
    closeConversionModal();
    await window.pywebview.api.confirm_selection(selectionId);
  }

  async function cancelSelection() {
    if (conversionCancel.disabled) return;
    const selectionId = pendingSelectionId;
    setBusy(conversionCancel, true);
    closeConversionModal();
    if (selectionId && window.pywebview && window.pywebview.api) {
      await window.pywebview.api.cancel_selection(selectionId);
    }
  }

  function closeConversionModal() {
    pendingSelectionId = null;
    conversionModal.hidden = true;
    conversionConfirm.disabled = false;
    conversionCancel.disabled = false;
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
    action.addEventListener('click', async () => {
      if (!window.pywebview || !window.pywebview.api) return;
      if (action.disabled) return;
      if (row.actionMode === 'retry') {
        setBusy(action, true);
        await window.pywebview.api.retry_job(row.jobId);
        return;
      }
      const path = row.outputPath || row.sourcePath;
      if (!path) return;
      setBusy(action, true);
      window.pywebview.api.reveal_in_finder(path);
      window.setTimeout(() => setBusy(action, false), 250);
    });

    li.appendChild(filename);
    li.appendChild(status);
    li.appendChild(progress);
    li.appendChild(action);

    const row = {
      el: li,
      status,
      bar,
      action,
      actionMode: 'show',
      jobId: payload.job_id,
      outputPath: null,
      sourcePath: payload.path || null,
    };
    return row;
  }

  function updateJobRow(row, payload) {
    row.el.classList.remove('done', 'failed');
    row.status.classList.remove('done', 'failed');
    row.jobId = payload.job_id || row.jobId;
    row.sourcePath = payload.path || row.sourcePath;
    row.action.disabled = false;

    if (payload.status === 'queued') {
      row.status.textContent = 'queued';
      row.action.hidden = true;
    } else if (payload.status === 'converting') {
      row.status.textContent = payload.message || 'converting…';
      row.action.hidden = true;
    } else if (payload.status === 'done') {
      row.el.classList.add('done');
      row.status.classList.add('done');
      row.status.textContent = payload.message || 'done';
      row.action.hidden = false;
      row.action.textContent = 'Show';
      row.actionMode = 'show';
      row.outputPath = payload.output || null;
    } else if (payload.status === 'failed') {
      row.el.classList.add('failed');
      row.status.classList.add('failed');
      row.status.textContent = payload.error || payload.message || 'failed';
      row.action.hidden = false;
      row.action.textContent = 'Continue';
      row.actionMode = 'retry';
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
    cacheClear: document.getElementById('cache-clear'),
    cacheClearStatus: document.getElementById('cache-clear-status'),
  };

  settingsBtn.addEventListener('click', () => openSettings());
  modal.addEventListener('click', (e) => {
    if (e.target.closest('[data-close]')) closeModal();
  });
  document.getElementById('settings-save').addEventListener('click', saveSettings);
  fields.cacheClear.addEventListener('click', clearCache);
  modal.addEventListener('input', () => {
    if (!modal.hidden) settingsDirty = true;
  });

  function openSettings() {
    if (!window.pywebview || !window.pywebview.api) return;
    settingsDirty = false;
    applySettings(settingsSnapshot);
    fields.cacheClear.disabled = false;
    fields.cacheClearStatus.textContent = 'Remove cached page results from this device.';
    modal.hidden = false;

    const token = ++settingsLoadToken;
    window.pywebview.api.get_settings()
      .then((s) => {
        settingsSnapshot = { ...defaultSettings, ...s };
        if (token === settingsLoadToken && !settingsDirty && !modal.hidden) {
          applySettings(settingsSnapshot);
        }
      })
      .catch(() => {
        if (!settingsDirty && !modal.hidden) {
          fields.apiKeyStatus.textContent = 'Could not refresh settings.';
        }
      });
  }

  function closeModal() {
    modal.hidden = true;
  }

  function saveSettings() {
    if (!window.pywebview || !window.pywebview.api || document.getElementById('settings-save').disabled) return;
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

    settingsSnapshot = {
      ...settingsSnapshot,
      ...payload,
      has_api_key: !!newKey || settingsSnapshot.has_api_key,
    };
    closeModal();
    window.pywebview.api.save_settings(payload)
      .then((s) => {
        settingsSnapshot = { ...defaultSettings, ...s };
      })
      .catch(() => {
        emptyHint.textContent = 'Settings could not be saved.';
        emptyHint.classList.remove('hidden');
      });
  }

  async function clearCache() {
    if (!window.pywebview || !window.pywebview.api) return;
    if (!window.confirm('Clear DocMark cached conversion results from this device?')) return;

    setBusy(fields.cacheClear, true);
    fields.cacheClearStatus.textContent = 'Clearing cache...';
    try {
      const result = await window.pywebview.api.clear_cache();
      const deleted = Number(result && result.deleted) || 0;
      const bytesDeleted = Number(result && result.bytes_deleted) || 0;
      fields.cacheClearStatus.textContent = deleted
        ? `Deleted ${deleted} cache file${deleted === 1 ? '' : 's'} (${formatBytes(bytesDeleted)}).`
        : 'No cache files to delete.';
    } catch (e) {
      fields.cacheClearStatus.textContent = 'Could not clear cache.';
    } finally {
      setBusy(fields.cacheClear, false);
    }
  }

  function formatBytes(bytes) {
    if (!bytes) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB'];
    let value = bytes;
    let unitIndex = 0;
    while (value >= 1024 && unitIndex < units.length - 1) {
      value /= 1024;
      unitIndex++;
    }
    return `${value.toFixed(value >= 10 || unitIndex === 0 ? 0 : 1)} ${units[unitIndex]}`;
  }

  function applySettings(s) {
    fields.apiKey.value = '';
    fields.apiKey.placeholder = s.has_api_key ? '••••••••  (stored in keychain)' : 'sk-…';
    fields.apiKeyStatus.textContent = s.has_api_key
      ? 'A key is currently saved. Leave blank to keep it.'
      : 'No key saved yet.';
    fields.model.value = s.model || defaultSettings.model;
    fields.dpi.value = s.dpi || defaultSettings.dpi;
    fields.detail.value = s.detail || defaultSettings.detail;
    fields.forceVision.checked = !!s.force_vision;
    fields.pptxVision.checked = !!s.office_vision_pptx_enabled;
    fields.docxImageDesc.checked = !!s.docx_image_description_enabled;
    fields.cacheEnabled.checked = s.cache_enabled !== false;
  }

  function setBusy(button, busy) {
    if (!button) return;
    button.disabled = busy;
    button.classList.toggle('is-busy', busy);
    button.setAttribute('aria-busy', busy ? 'true' : 'false');
  }
})();
