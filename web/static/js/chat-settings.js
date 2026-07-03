/* ============================================================
   chat-settings.js — 设置弹窗 / 配置表单 / 保存 / 重启
   currentConfig 由 utils.js 全局声明
   ============================================================ */

const settingsBodyEl = document.getElementById('settingsBody');

// ========== 打开设置 ==========
async function openSettings() {
  openModal('settingsModal');
  try {
    const data = await api('/config');
    currentConfig = data.config;
    renderSettingsForm(currentConfig);
  } catch (e) {
    settingsBodyEl.innerHTML = '<div style="color: var(--danger);">加载配置失败: ' + escapeHtml(e.message) + '</div>';
  }
}

// ========== 渲染配置表单 ==========
function renderSettingsForm(config) {
  const llm = config.llm || {};
  const memory = config.memory || {};
  const condenser = memory.condenser || {};
  const tools = config.tools || {};
  const storage = config.storage || {};
  const files = config.files || {};
  const security = config.security || {};
  const interrupt = config.interrupt || {};
  const securityEnabled = security.enabled === true || security.enabled === 'true';
  const rulesCount = security.rules && Array.isArray(security.rules) ? security.rules.length : 0;

  settingsBodyEl.innerHTML = `
    <div class="form-section">
      <div class="form-section-title">LLM 配置<span class="config-tag restart">需重启</span></div>
      <div class="form-row">
        <div class="form-group">
          <label>主对话 Provider</label>
          <select class="form-select" data-cfg="llm.main_provider">
            <option value="deepseek" ${llm.main_provider === 'deepseek' ? 'selected' : ''}>DeepSeek</option>
            <option value="qwen" ${llm.main_provider === 'qwen' ? 'selected' : ''}>Qwen (通义千问)</option>
            <option value="openai" ${llm.main_provider === 'openai' ? 'selected' : ''}>OpenAI</option>
            <option value="anthropic" ${llm.main_provider === 'anthropic' ? 'selected' : ''}>Anthropic</option>
          </select>
        </div>
        <div class="form-group">
          <label>主对话 Model</label>
          <input class="form-input" data-cfg="llm.main_model" value="${llm.main_model || ''}" placeholder="deepseek-chat">
        </div>
      </div>
      <div class="form-group">
        <label>主对话 API Key</label>
        <input class="form-input" type="password" data-cfg="llm.main_api_key" value="${llm.main_api_key || ''}" placeholder="sk-...">
      </div>
      <div class="form-row">
        <div class="form-group">
          <label>Consolidation Provider</label>
          <select class="form-select" data-cfg="llm.consolidation_provider">
            <option value="deepseek" ${llm.consolidation_provider === 'deepseek' ? 'selected' : ''}>DeepSeek</option>
            <option value="qwen" ${llm.consolidation_provider === 'qwen' ? 'selected' : ''}>Qwen</option>
            <option value="openai" ${llm.consolidation_provider === 'openai' ? 'selected' : ''}>OpenAI</option>
            <option value="anthropic" ${llm.consolidation_provider === 'anthropic' ? 'selected' : ''}>Anthropic</option>
          </select>
        </div>
        <div class="form-group">
          <label>Consolidation Model</label>
          <input class="form-input" data-cfg="llm.consolidation_model" value="${llm.consolidation_model || ''}" placeholder="deepseek-chat">
        </div>
      </div>
      <div class="form-group">
        <label>Consolidation API Key</label>
        <input class="form-input" type="password" data-cfg="llm.consolidation_api_key" value="${llm.consolidation_api_key || ''}" placeholder="留空则同主对话 Key">
      </div>
    </div>

    <div class="form-section">
      <div class="form-section-title">记忆系统<span class="config-tag hot">即时生效</span></div>
      <div class="form-row">
        <div class="form-group">
          <label>历史轮数上限</label>
          <input class="form-input" type="number" data-cfg="memory.history_max_turns" value="${memory.history_max_turns || 20}">
        </div>
        <div class="form-group">
          <label>沉淀阈值</label>
          <input class="form-input" type="number" data-cfg="memory.consolidation_threshold" value="${memory.consolidation_threshold || 10}">
          <div class="hint">达到此消息数触发记忆沉淀（切换会话时也会 flush）</div>
        </div>
      </div>
      <div class="form-row">
        <div class="form-group">
          <label>检索 Top K</label>
          <input class="form-input" type="number" data-cfg="memory.retrieval_top_k" value="${memory.retrieval_top_k || 5}">
        </div>
        <div class="form-group">
          <label>去重相似度阈值</label>
          <input class="form-input" type="number" step="0.01" data-cfg="memory.dedup_similarity_threshold" value="${memory.dedup_similarity_threshold || 0.85}">
        </div>
      </div>
    </div>

    <div class="form-section">
      <div class="form-section-title">工具配置<span class="config-tag hot">即时生效</span></div>
      <div class="form-row">
        <div class="form-group">
          <label>延迟加载阈值</label>
          <input class="form-input" type="number" data-cfg="tools.defer_loading_threshold" value="${tools.defer_loading_threshold || 20}">
          <div class="hint">工具数超过此值启用 stub 模式</div>
        </div>
        <div class="form-group">
          <label>React 循环上限</label>
          <input class="form-input" type="number" data-cfg="tools.max_react_loops" value="${tools.max_react_loops || 10}">
        </div>
      </div>
    </div>

    <div class="form-section">
      <div class="form-section-title">存储路径<span class="config-tag restart">需重启</span></div>
      <div class="form-group">
        <label>SQLite 路径</label>
        <input class="form-input" data-cfg="storage.sqlite_path" value="${storage.sqlite_path || 'data/sessions.db'}">
      </div>
    </div>

    <div class="form-section">
      <div class="form-section-title">安全配置</div>
      <div class="form-group">
        <label>启用审批机制<span class="config-tag restart">需重启</span></label>
        <select class="form-select" data-cfg="security.enabled" data-type="boolean">
          <option value="true" ${securityEnabled ? 'selected' : ''}>启用</option>
          <option value="false" ${!securityEnabled ? 'selected' : ''}>禁用</option>
        </select>
        <div class="hint">开启后高风险工具调用需用户审批</div>
      </div>
      <div class="form-group">
        <label>审批超时秒数<span class="config-tag hot">即时生效</span></label>
        <input class="form-input" type="number" data-cfg="security.approval_timeout_seconds" value="${security.approval_timeout_seconds || 60}">
        <div class="hint">超时后自动拒绝审批（热更新，无需重启）</div>
      </div>
      <div class="form-group">
        <label>审批规则<span class="config-tag restart">需重启</span></label>
        <div class="hint" style="padding:8px 10px;background:var(--bg-primary);border:1px solid var(--border);border-radius:var(--radius-sm);color:var(--text-secondary);">
          当前规则数：${rulesCount} 条。规则配置需编辑 config.yaml 后重启服务生效，暂不支持页面编辑。
        </div>
      </div>
    </div>

    <div class="form-section">
      <div class="form-section-title">文件上传<span class="config-tag restart">需重启</span></div>
      <div class="form-group">
        <label>允许图片上传 (OCR)<span class="config-tag restart">需重启</span></label>
        <select class="form-select" data-cfg="files.ocr_enabled" data-type="boolean">
          <option value="true" ${files.ocr_enabled === true ? 'selected' : ''}>开启</option>
          <option value="false" ${files.ocr_enabled === false ? 'selected' : ''}>关闭</option>
        </select>
        <div class="hint">关闭时 .jpg/.png/.gif 等图片文件将被拒绝上传</div>
      </div>
      <div class="form-row">
        <div class="form-group">
          <label>单文件上限 (MB)<span class="config-tag restart">需重启</span></label>
          <input class="form-input" type="number" data-cfg="files.max_upload_size_mb" value="${files.max_upload_size_mb || 50}">
        </div>
        <div class="form-group">
          <label>每会话文件数<span class="config-tag restart">需重启</span></label>
          <input class="form-input" type="number" data-cfg="files.max_files_per_session" value="${files.max_files_per_session || 50}">
        </div>
      </div>
      <div class="form-row">
        <div class="form-group">
          <label>分块大小<span class="config-tag restart">需重启</span></label>
          <input class="form-input" type="number" data-cfg="files.chunk_size" value="${files.chunk_size || 512}">
        </div>
        <div class="form-group">
          <label>分块重叠<span class="config-tag restart">需重启</span></label>
          <input class="form-input" type="number" data-cfg="files.chunk_overlap" value="${files.chunk_overlap || 64}">
        </div>
      </div>
      <div class="form-row">
        <div class="form-group">
          <label>LLM 摘要兜底<span class="config-tag restart">需重启</span></label>
          <select class="form-select" data-cfg="files.llm_fallback_enabled" data-type="boolean">
            <option value="true" ${files.llm_fallback_enabled === true ? 'selected' : ''}>开启</option>
            <option value="false" ${files.llm_fallback_enabled === false ? 'selected' : ''}>关闭</option>
          </select>
          <div class="hint">ETL 解析失败时使用 LLM 生成摘要</div>
        </div>
        <div class="form-group">
          <label>ETL 队列上限<span class="config-tag restart">需重启</span></label>
          <input class="form-input" type="number" data-cfg="files.etl_max_queue" value="${files.etl_max_queue || 100}">
        </div>
      </div>
    </div>

    <div class="form-section">
      <div class="form-section-title">LLM 进阶<span class="config-tag restart">需重启</span></div>
      <div class="form-row">
        <div class="form-group">
          <label>主对话 Base URL<span class="config-tag restart">需重启</span></label>
          <input class="form-input" data-cfg="llm.main_base_url" value="${llm.main_base_url || ''}" placeholder="https://api.deepseek.com">
        </div>
        <div class="form-group">
          <label>Consolidation Base URL<span class="config-tag restart">需重启</span></label>
          <input class="form-input" data-cfg="llm.consolidation_base_url" value="${llm.consolidation_base_url || ''}" placeholder="https://api.deepseek.com">
        </div>
      </div>
      <div class="form-row">
        <div class="form-group">
          <label>最大上下文 Tokens</label>
          <input class="form-input" type="number" data-cfg="llm.max_context_tokens" value="${llm.max_context_tokens || 200000}">
        </div>
        <div class="form-group">
          <label>上下文阈值</label>
          <input class="form-input" type="number" step="0.01" data-cfg="llm.context_threshold" value="${llm.context_threshold || 0.8}">
        </div>
      </div>
    </div>

    <div class="form-section">
      <div class="form-section-title">记忆系统进阶</div>
      <div class="form-section-title" style="margin-top:16px;font-size:11px">记忆衰减</div>
      <div class="form-row">
        <div class="form-group">
          <label>衰减速率<span class="config-tag hot">即时生效</span></label>
          <input class="form-input" type="number" step="0.001" data-cfg="memory.decay_rate" value="${memory.decay_rate || 0.01}">
        </div>
        <div class="form-group">
          <label>频率权重<span class="config-tag hot">即时生效</span></label>
          <input class="form-input" type="number" step="0.1" data-cfg="memory.frequency_weight" value="${memory.frequency_weight || 0.5}">
        </div>
      </div>
      <div class="form-section-title" style="margin-top:16px;font-size:11px">惊喜记忆</div>
      <div class="form-row">
        <div class="form-group">
          <label>惊喜记忆开关<span class="config-tag hot">即时生效</span></label>
          <select class="form-select" data-cfg="memory.surprise_gate_enabled" data-type="boolean">
            <option value="true" ${memory.surprise_gate_enabled === true || memory.surprise_gate_enabled === undefined ? 'selected' : ''}>开启</option>
            <option value="false" ${memory.surprise_gate_enabled === false ? 'selected' : ''}>关闭</option>
          </select>
        </div>
        <div class="form-group">
          <label>相似度阈值<span class="config-tag hot">即时生效</span></label>
          <input class="form-input" type="number" step="0.01" data-cfg="memory.surprise_similarity_threshold" value="${memory.surprise_similarity_threshold || 0.85}">
        </div>
      </div>
      <div class="form-row">
        <div class="form-group">
          <label>跳过阈值<span class="config-tag hot">即时生效</span></label>
          <input class="form-input" type="number" step="0.01" data-cfg="memory.surprise_skip_threshold" value="${memory.surprise_skip_threshold || 0.92}">
        </div>
      </div>
      <div class="form-section-title" style="margin-top:16px;font-size:11px">对话压缩 (Condenser)</div>
      <div class="form-row">
        <div class="form-group">
          <label>启用压缩<span class="config-tag hot">即时生效</span></label>
          <select class="form-select" data-cfg="memory.condenser.enabled" data-type="boolean">
            <option value="true" ${condenser.enabled === true ? 'selected' : ''}>开启</option>
            <option value="false" ${condenser.enabled === false ? 'selected' : ''}>关闭</option>
          </select>
        </div>
        <div class="form-group">
          <label>保留最近 N 轮<span class="config-tag hot">即时生效</span></label>
          <input class="form-input" type="number" data-cfg="memory.condenser.keep_recent_n" value="${condenser.keep_recent_n || 6}">
        </div>
      </div>
      <div class="form-row">
        <div class="form-group">
          <label>保留开头轮数<span class="config-tag hot">即时生效</span></label>
          <input class="form-input" type="number" data-cfg="memory.condenser.keep_first" value="${condenser.keep_first || 2}">
        </div>
        <div class="form-group">
          <label>LLM 摘要阈值<span class="config-tag hot">即时生效</span></label>
          <input class="form-input" type="number" data-cfg="memory.condenser.llm_summary_threshold" value="${condenser.llm_summary_threshold || 100000}">
        </div>
      </div>
    </div>

    <div class="form-section">
      <div class="form-section-title">工具进阶</div>
      <div class="form-group">
        <label>Bash 超时(秒)<span class="config-tag hot">即时生效</span></label>
        <input class="form-input" type="number" data-cfg="tools.bash_timeout" value="${tools.bash_timeout || 120}">
      </div>
    </div>

    <div class="form-section">
      <div class="form-section-title">流中断<span class="config-tag hot">即时生效</span></div>
      <div class="form-row">
        <div class="form-group">
          <label>断点检测阈值</label>
          <input class="form-input" type="number" data-cfg="interrupt.breakpoint_threshold" value="${interrupt.breakpoint_threshold || 40}">
          <div class="hint">优雅中断模式下，评分达到此值触发断点</div>
        </div>
        <div class="form-group">
          <label>优雅中断超时(秒)</label>
          <input class="form-input" type="number" step="0.1" data-cfg="interrupt.graceful_timeout_seconds" value="${interrupt.graceful_timeout_seconds || 5.0}">
          <div class="hint">超过此时间未检测到断点时强制中断</div>
        </div>
      </div>
    </div>

    <div class="form-section">
      <div class="form-section-title">存储进阶<span class="config-tag restart">需重启</span></div>
      <div class="form-row">
        <div class="form-group">
          <label>会话保留天数</label>
          <input class="form-input" type="number" data-cfg="storage.session_ttl_days" value="${storage.session_ttl_days || 30}">
        </div>
        <div class="form-group">
          <label>清理间隔(小时)</label>
          <input class="form-input" type="number" data-cfg="storage.cleanup_interval_hours" value="${storage.cleanup_interval_hours || 24}">
        </div>
      </div>
    </div>
  `;
}

// ========== 保存配置 ==========
async function saveConfig() {
  const newConfig = JSON.parse(JSON.stringify(currentConfig));
  settingsBodyEl.querySelectorAll('[data-cfg]').forEach(el => {
    const path = el.dataset.cfg.split('.');
    let obj = newConfig;
    for (let i = 0; i < path.length - 1; i++) {
      if (!obj[path[i]]) obj[path[i]] = {};
      obj = obj[path[i]];
    }
    let val = el.value;
    if (el.type === 'number') val = parseFloat(val) || 0;
    if (el.dataset.type === 'boolean') val = (val === 'true');
    obj[path[path.length - 1]] = val;
  });

  try {
    const data = await api('/config', { method: 'PUT', body: { config: newConfig } });
    showToast(data.message, data.needs_restart ? '' : 'success');
    if (data.needs_restart) {
      setTimeout(() => showToast('请重启服务使配置生效', ''), 3000);
    }
    closeModal('settingsModal');
  } catch (e) {
    showToast('保存失败: ' + e.message, 'error');
  }
}

// ========== 重启服务 ==========
async function restartServer() {
  if (!confirm('确认重启服务？未保存的配置将丢失。')) return;
  try {
    await api('/restart', { method: 'POST' });
    showToast('服务正在重启...', '');
    setTimeout(() => window.location.reload(), 3000);
  } catch (e) {
    showToast('重启失败: ' + e.message, 'error');
  }
}

// 暴露给其他模块
window.HermesChatSettings = {
  openSettings,
  renderSettingsForm,
  saveConfig,
  restartServer,
};
