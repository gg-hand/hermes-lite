/* ============================================================
   chat-files.js — 文件上传 / ETL 轮询 / 上传队列 / 粘贴图片
   ============================================================ */

// ========== DOM 引用 ==========
const attachBtnEl = document.getElementById('attachBtn');
const fileListEl = document.getElementById('fileList');
const fileInputEl = document.getElementById('fileInput');
const uploadQueueEl = document.getElementById('uploadQueue');
let filePollTimer = null;
let uploadQueue = [];

// ========== 文件上传 ==========
async function uploadFile(file) {
  if (!currentSessionId) { showToast('请先新建或选择一个会话', 'error'); return; }
  const formData = new FormData();
  formData.append('file', file);
  formData.append('session_id', currentSessionId);
  try {
    const res = await fetch('/files/upload?' + new URLSearchParams({ session_id: currentSessionId }), {
      method: 'POST',
      body: formData,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || data.message || 'HTTP ' + res.status);
    showToast(data.is_dup ? '文件已在知识库中' : '上传成功: ' + file.name, data.is_dup ? '' : 'success');
    addToQueue(file);
    loadFiles();
  } catch (e) {
    showToast('上传失败: ' + e.message, 'error');
  }
}

// ========== 文件列表加载 + ETL 轮询 ==========
async function loadFiles() {
  try {
    const sid = currentSessionId || '__none__';
    const res = await fetch('/sessions/' + sid + '/files');
    if (!res.ok) {
      fileListEl.innerHTML = '<div class="file-empty">获取文件列表失败</div>';
      return;
    }
    const data = await res.json();
    const files = data.files || [];
    if (!files.length) {
      fileListEl.innerHTML = '<div class="file-empty">无已上传文件</div>';
      stopFilePolling();
      return;
    }
    const statusLabels = { done: '完成', failed: '失败', processing: '处理中', pending: '等待中', disk_expired: '已过期' };
    const iconMap = { '.pdf': 'PDF', '.docx': 'DOC', '.txt': 'TXT', '.md': 'MD', '.png': 'IMG', '.jpg': 'IMG', '.jpeg': 'IMG', '.gif': 'IMG' };
    let html = '';
    let hasPending = false;
    for (const f of files) {
      const status = f.etl_status || 'pending';
      const statusLabel = statusLabels[status] || status;
      const icon = iconMap[f.type] || 'FILE';
      const sizeLabel = f.size > 1048576 ? (f.size / 1048576).toFixed(1) + 'MB' : (f.size / 1024).toFixed(1) + 'KB';
      html += '<div class="file-item">' +
        '<span class="file-icon">' + icon + '</span>' +
        '<span class="file-name" title="' + escapeHtml(f.original_name) + '">' + escapeHtml(f.original_name) + '</span>' +
        '<span class="file-size">' + sizeLabel + '</span>' +
        '<span class="file-status ' + status + '">' + statusLabel + '</span>' +
      '</div>';
      if (status === 'pending' || status === 'processing') hasPending = true;
    }
    fileListEl.innerHTML = html;
    if (hasPending) startFilePolling(); else stopFilePolling();
  } catch (e) {
    if (currentSessionId) fileListEl.innerHTML = '<div class="file-empty">加载失败</div>';
  }
}

function startFilePolling() {
  if (!filePollTimer) filePollTimer = setInterval(loadFiles, 5000);
}

function stopFilePolling() {
  if (filePollTimer) { clearInterval(filePollTimer); filePollTimer = null; }
}

// ========== 上传队列（前端 chip） ==========
function addToQueue(file) {
  uploadQueue.push({ id: Date.now() + '_' + Math.random().toString(36).slice(2, 8), name: file.name });
  renderQueue();
}

function removeFromQueue(id) {
  uploadQueue = uploadQueue.filter(f => f.id !== id);
  renderQueue();
}

function renderQueue() {
  if (!uploadQueue.length) { uploadQueueEl.innerHTML = ''; return; }
  uploadQueueEl.innerHTML = uploadQueue.map(f =>
    '<div class="queue-chip"><span class="chip-name">' + escapeHtml(f.name) + '</span><span class="chip-close" data-id="' + f.id + '">×</span></div>'
  ).join('');
}

// ========== 事件绑定 ==========
if (attachBtnEl) {
  attachBtnEl.addEventListener('click', () => {
    if (!currentSessionId) { showToast('请先新建或选择一个会话', 'error'); return; }
    fileInputEl.click();
  });
}

if (fileInputEl) {
  fileInputEl.addEventListener('change', (e) => {
    for (const f of e.target.files) uploadFile(f);
    e.target.value = '';
  });
}

if (uploadQueueEl) {
  uploadQueueEl.addEventListener('click', (e) => {
    const closeBtn = e.target.closest('.chip-close');
    if (!closeBtn) return;
    const id = closeBtn.dataset.id;
    if (id) removeFromQueue(id);
  });
}

// 粘贴图片上传
if (messageInputEl) {
  messageInputEl.addEventListener('paste', (e) => {
    const items = e.clipboardData.items;
    for (const item of items) {
      if (item.type.startsWith('image/')) {
        e.preventDefault();
        let file = item.getAsFile();
        if (!file) continue;
        file = new File([file], 'pasted-image.png', { type: file.type });
        uploadFile(file);
        break;
      }
    }
  });
}

// 会话切换 / 新建时同步文件列表（包装原函数）
const _origSelectSession = selectSession;
selectSession = async function (sessionId) {
  await _origSelectSession(sessionId);
  setTimeout(loadFiles, 200);
};

const _origNewSession = newSession;
newSession = function () {
  _origNewSession();
  setTimeout(loadFiles, 500);
};

// 暴露给其他模块
window.HermesChatFiles = {
  uploadFile,
  loadFiles,
  startFilePolling,
  stopFilePolling,
  addToQueue,
  removeFromQueue,
  renderQueue,
};
