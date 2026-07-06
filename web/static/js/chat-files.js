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

    // 即时反馈：在聊天区插入上传消息气泡（纯前端 DOM，刷新后由后端消息/合成消息接管）
    if (window.HermesChatCore && window.HermesChatCore.appendMessage) {
      const file_type = '.' + (file.name.split('.').pop() || '').toLowerCase();
      const is_image = ['.png', '.jpg', '.jpeg', '.gif'].includes(file_type);
      window.HermesChatCore.appendMessage(
        'user',
        data.is_dup ? `文件已在知识库中：${file.name}` : `已上传文件：${file.name}`,
        [{ file_id: data.file_id, name: file.name, type: file_type, size: file.size,
           category: is_image ? 'image' : 'document',
           etl_status: data.is_dup ? 'done' : 'pending' }]
      );
    }

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
      // 竞态保护：fetch 完成时会话可能已切换，避免旧会话响应覆盖新会话面板
      if (sid === (currentSessionId || '__none__')) {
        fileListEl.innerHTML = '<div class="file-empty">获取文件列表失败</div>';
      }
      return;
    }
    const data = await res.json();
    // 竞态保护：解析响应时会话可能已切换，丢弃过期响应
    if (sid !== (currentSessionId || '__none__')) return;
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
  // 清空上传队列（旧会话的 chip 不应残留）
  uploadQueue = [];
  renderQueue();
  // 停止旧会话的 ETL 轮询，避免竞态
  stopFilePolling();
  setTimeout(loadFiles, 200);
};

const _origNewSession = newSession;
newSession = function () {
  _origNewSession();
  uploadQueue = [];
  renderQueue();
  stopFilePolling();
  setTimeout(loadFiles, 500);
};

// 删除当前会话时同步清理文件面板/队列/轮询（包装原函数）
const _origDeleteSession = deleteSession;
deleteSession = async function (sessionId) {
  await _origDeleteSession(sessionId);
  // 若删除的是当前会话，清理文件相关状态（与 newSession 行为一致）
  if (!currentSessionId) {
    uploadQueue = [];
    renderQueue();
    stopFilePolling();
    if (fileListEl) fileListEl.innerHTML = '<div class="file-empty">无已上传文件</div>';
  }
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
