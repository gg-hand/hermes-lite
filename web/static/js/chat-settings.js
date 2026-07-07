/* ============================================================
   chat-settings.js — 设置弹出菜单（flyout） + 高级设置弹窗
   currentConfig 由 utils.js 全局声明
   ============================================================ */

const settingsBodyEl = document.getElementById('settingsBody');
const flyoutEl = document.getElementById('settingsFlyout');
const flyoutBodyEl = document.getElementById('settingsFlyoutBody');

// ========== 设置弹出菜单（flyout） ==========
async function toggleSettingsFlyout() {
  if (!flyoutEl) return;
  const isOpen = flyoutEl.classList.contains('show');
  if (isOpen) {
    closeSettingsFlyout();
    return;
  }
  // 定位：设置按钮上方
  const btn = document.getElementById('btnSettingsFlyout');
  if (btn) {
    const rect = btn.getBoundingClientRect();
    const flyoutWidth = 280;
    let left = rect.right - flyoutWidth;
    if (left < 8) left = 8;
    const bottom = window.innerHeight - rect.top + 8;
    flyoutEl.style.left = left + 'px';
    flyoutEl.style.bottom = bottom + 'px';
    flyoutEl.style.minWidth = flyoutWidth + 'px';
  }
  flyoutEl.classList.add('show');
  await renderQuickSettings();
}

function closeSettingsFlyout() {
  if (flyoutEl) flyoutEl.classList.remove('show');
}

// 渲染快捷开关到 flyout body
async function renderQuickSettings() {
  if (!flyoutBodyEl) return;
  try {
    const data = await api('/config');
    currentConfig = data.config;
  } catch (e) {
    flyoutBodyEl.innerHTML = '<div style="color: var(--danger); padding: 12px;">加载失败: ' + escapeHtml(e.message) + '</div>';
    return;
  }

  const security = currentConfig.security || {};
  const guardrails = currentConfig.guardrails || {};
  const securityEnabled = security.enabled === true || security.enabled === 'true';
  const inputScanEnabled = (guardrails.input_scan || {}).enabled !== false;
  const sanitizerEnabled = (guardrails.sanitizer || {}).enabled !== false;
  const outputFilterEnabled = (guardrails.output_filter || {}).enabled !== false;

  const toggleCard = (path, checked, label, hint) => `
    <div class="flyout-toggle-card">
      <div class="flyout-toggle-info">
        <div class="flyout-toggle-label">${label}</div>
        <div class="flyout-toggle-hint">${hint}</div>
      </div>
      <label class="switch"><input type="checkbox" data-guardrail="${path}" ${checked ? 'checked' : ''}><span class="slider"></span></label>
    </div>`;

  flyoutBodyEl.innerHTML = `
    <div class="flyout-section-title">防护系统</div>
    ${toggleCard('security.enabled', securityEnabled, 'HIL 审批', '高危操作弹确认卡片')}
    ${toggleCard('guardrails.input_scan.enabled', inputScanEnabled, '输入扫描', '检测 Prompt 注入')}
    ${toggleCard('guardrails.sanitizer.enabled', sanitizerEnabled, '工具结果脱敏', '隔离注入内容')}
    ${toggleCard('guardrails.output_filter.enabled', outputFilterEnabled, 'PII 过滤', '脱敏手机号/邮箱')}
    <div class="flyout-section-title">推理模式</div>
    <div class="flyout-toggle-card">
      <div class="flyout-toggle-info">
        <div class="flyout-toggle-label">思考模式</div>
        <div class="flyout-toggle-hint">深度推理，质量更高</div>
      </div>
      <label class="switch"><input type="checkbox" data-reasoning-toggle="main"><span class="slider"></span></label>
    </div>`;

  bindGuardrailToggles();
  bindReasoningToggle();
  syncReasoningStatus();
}

// ========== 打开高级设置弹窗 ==========
async function openSettings() {
  closeSettingsFlyout();
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
  const ocrCfg = (files.ocr) || {};
  const paddleCfg = ocrCfg.paddle || {};
  const tesseractCfg = ocrCfg.tesseract || {};
  const visionCfg = ocrCfg.vision_llm || {};
  const security = config.security || {};
  const interrupt = config.interrupt || {};
  const securityEnabled = security.enabled === true || security.enabled === 'true';
  const rulesCount = security.rules && Array.isArray(security.rules) ? security.rules.length : 0;
  const guardrails = config.guardrails || {};
  const inputScanCfg = guardrails.input_scan || {};
  const sanitizerCfg = guardrails.sanitizer || {};
  const outputFilterCfg = guardrails.output_filter || {};
  // 缺失 enabled 字段时默认 true（安全默认，defense in depth）
  const inputScanEnabled = inputScanCfg.enabled !== false;
  const sanitizerEnabled = sanitizerCfg.enabled !== false;
  const outputFilterEnabled = outputFilterCfg.enabled !== false;
  const readPaths = security.read_paths || {};
  const readPathsMode = readPaths.mode || 'deny_first';
  const readPathsDeny = Array.isArray(readPaths.deny) ? readPaths.deny.join('\n') : '';
  const readPathsAllow = Array.isArray(readPaths.allow) ? readPaths.allow.join('\n') : '';
  const readPathsWs = Array.isArray(readPaths.workspace_dirs) ? readPaths.workspace_dirs.join('\n') : '';

  // ---------- 基础分类 sections ----------
  const secLLM = `
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
    </div>`;

  const secMemory = `
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
    </div>`;

  const secTools = `
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
    </div>`;

  // ---------- 安全分类 sections ----------
  const secSecurity = `
    <div class="form-section">
      <div class="form-section-title">安全配置</div>
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
    </div>`;

  const secGuardrail = `
    <div class="form-section">
      <div class="form-section-title">防护系统<span class="config-tag hot">即时生效</span></div>
      <div class="switch-row">
        <div>
          <div class="switch-label">HIL 审批（工具调用确认）</div>
          <div class="hint">关闭后所有工具调用直接执行，不再弹审批卡片</div>
        </div>
        <label class="switch"><input type="checkbox" data-guardrail="security.enabled" ${securityEnabled?'checked':''}><span class="slider"></span></label>
      </div>
      <div class="switch-row">
        <div>
          <div class="switch-label">输入扫描（Prompt 注入检测）</div>
          <div class="hint">关闭后用户输入中的注入指令不再被检测告警</div>
        </div>
        <label class="switch"><input type="checkbox" data-guardrail="guardrails.input_scan.enabled" ${inputScanEnabled?'checked':''}><span class="slider"></span></label>
      </div>
      <div class="switch-row">
        <div>
          <div class="switch-label">工具结果脱敏（外部内容隔离）</div>
          <div class="hint">关闭后外部工具返回的注入内容将直接进入 LLM 上下文</div>
        </div>
        <label class="switch"><input type="checkbox" data-guardrail="guardrails.sanitizer.enabled" ${sanitizerEnabled?'checked':''}><span class="slider"></span></label>
      </div>
      <div class="switch-row">
        <div>
          <div class="switch-label">PII 输出过滤（手机号/身份证/邮箱脱敏）</div>
          <div class="hint">关闭后 LLM 响应中的 PII 将直接展示给用户</div>
        </div>
        <label class="switch"><input type="checkbox" data-guardrail="guardrails.output_filter.enabled" ${outputFilterEnabled?'checked':''}><span class="slider"></span></label>
      </div>
      <div class="switch-row">
        <div>
          <div class="switch-label">思考模式（LLM 推理增强）</div>
          <div class="hint">开启后 LLM 在回复前进行深度思考，质量更高但耗时更长。下一条消息生效</div>
        </div>
        <label class="switch"><input type="checkbox" data-reasoning-toggle="main"><span class="slider"></span></label>
      </div>
    </div>`;

  const secReadPaths = `
    <div class="form-section">
      <div class="form-section-title">路径策略<span class="config-tag hot">即时生效</span></div>
      <div class="form-group">
        <label>拦截模式</label>
        <select class="form-select" data-cfg="security.read_paths.mode">
          <option value="deny_first" ${readPathsMode === 'deny_first' ? 'selected' : ''}>deny_first（黑名单优先，未列出默认允许）</option>
          <option value="whitelist_only" ${readPathsMode === 'whitelist_only' ? 'selected' : ''}>whitelist_only（严格白名单，未列出默认拒绝）</option>
        </select>
        <div class="hint">对 file_read/file_listdir/file_glob/file_grep/file_query 生效，热更新即时生效</div>
      </div>
      <div class="form-group">
        <label>工作空间目录<span class="config-tag hot">即时生效</span></label>
        <textarea class="settings-textarea" data-cfg="security.read_paths.workspace_dirs" data-list="true" placeholder="每行一条路径，如 /home/user/myproject">${escapeHtml(readPathsWs)}</textarea>
        <div class="hint">该目录下文件完全可读，优先级高于黑名单。便于 agent 读取用户项目代码</div>
      </div>
      <div class="form-group">
        <label>黑名单 deny<span class="config-tag hot">即时生效</span></label>
        <textarea class="settings-textarea" data-cfg="security.read_paths.deny" data-list="true" placeholder="每行一条，如 src/ 或 *.pyc">${escapeHtml(readPathsDeny)}</textarea>
        <div class="hint">命中即拦截，支持目录前缀(src/)、文件名(config.yaml)和通配(*.pyc)。绝对路径与相对路径均生效</div>
      </div>
      <div class="form-group">
        <label>白名单 allow<span class="config-tag hot">即时生效</span></label>
        <textarea class="settings-textarea" data-cfg="security.read_paths.allow" data-list="true" placeholder="每行一条，如 data/ 或 web/">${escapeHtml(readPathsAllow)}</textarea>
        <div class="hint">whitelist_only 模式下仅这些路径可读；deny_first 模式下此项不强制</div>
      </div>
    </div>`;

  // ---------- 存储分类 sections ----------
  const secStorage = `
    <div class="form-section">
      <div class="form-section-title">存储路径<span class="config-tag restart">需重启</span></div>
      <div class="form-group">
        <label>SQLite 路径</label>
        <input class="form-input" data-cfg="storage.sqlite_path" value="${storage.sqlite_path || 'data/sessions.db'}">
      </div>
    </div>`;

  const secFiles = `
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
    </div>`;

  const secOCR = `
    <div class="form-section">
      <div class="form-section-title">OCR 分层配置<span class="config-tag hot">分层降级</span></div>
      <div class="hint" style="margin-bottom:12px;padding:8px 10px;background:var(--bg-primary);border:1px solid var(--border);border-radius:var(--radius-sm);">
        图片走 PaddleOCR(L1) → Tesseract+预处理(L2) → 视觉LLM(L3) 三层降级通道，前层失败自动降级到下层
      </div>
      <div class="form-group">
        <label>主引擎选择<span class="config-tag restart">需重启</span></label>
        <select class="form-select" data-cfg="files.ocr.primary_engine">
          <option value="paddle" ${ocrCfg.primary_engine === 'paddle' ? 'selected' : ''}>PaddleOCR（中文优先）</option>
          <option value="tesseract" ${ocrCfg.primary_engine === 'tesseract' ? 'selected' : ''}>Tesseract（轻量兜底）</option>
          <option value="none" ${ocrCfg.primary_engine === 'none' ? 'selected' : ''}>none（禁用 OCR）</option>
        </select>
        <div class="hint">缺依赖时自动降级；切换主引擎需重启重建 PaddleOCR 实例</div>
      </div>
      <div class="form-section-title" style="margin-top:16px;font-size:11px">PaddleOCR（L1 主引擎）</div>
      <div class="form-row">
        <div class="form-group">
          <label>语言<span class="config-tag restart">需重启</span></label>
          <input class="form-input" data-cfg="files.ocr.paddle.lang" value="${paddleCfg.lang || 'ch'}" placeholder="ch / en / korean / japan">
          <div class="hint">PaddleOCR 模型语言，切换需重启重建实例</div>
        </div>
        <div class="form-group">
          <label>GPU 加速<span class="config-tag restart">需重启</span></label>
          <select class="form-select" data-cfg="files.ocr.paddle.use_gpu" data-type="boolean">
            <option value="true" ${paddleCfg.use_gpu === true ? 'selected' : ''}>开启</option>
            <option value="false" ${paddleCfg.use_gpu === false ? 'selected' : ''}>关闭</option>
          </select>
          <div class="hint">需安装 GPU 版 paddlepaddle</div>
        </div>
      </div>
      <div class="form-row">
        <div class="form-group">
          <label>最低置信度<span class="config-tag hot">即时生效</span></label>
          <input class="form-input" type="number" step="0.01" min="0" max="1" data-cfg="files.ocr.paddle.min_confidence" value="${paddleCfg.min_confidence !== undefined ? paddleCfg.min_confidence : 0.6}">
          <div class="hint">低于此值触发 L2 降级（0-1）</div>
        </div>
        <div class="form-group">
          <label>推理软超时(秒)<span class="config-tag hot">即时生效</span></label>
          <input class="form-input" type="number" data-cfg="files.ocr.paddle.infer_timeout" value="${paddleCfg.infer_timeout !== undefined ? paddleCfg.infer_timeout : 30}">
          <div class="hint">超时仅记 warning 不强制终止（避免 Lock 死锁）</div>
        </div>
      </div>
      <div class="form-section-title" style="margin-top:16px;font-size:11px">Tesseract（L2 兜底）</div>
      <div class="form-row">
        <div class="form-group">
          <label>语言<span class="config-tag hot">即时生效</span></label>
          <input class="form-input" data-cfg="files.ocr.tesseract.lang" value="${tesseractCfg.lang || 'chi_sim+eng'}" placeholder="chi_sim+eng / eng / chi_sim">
          <div class="hint">需提前安装对应语言包；多语言用 + 连接</div>
        </div>
        <div class="form-group">
          <label>预处理<span class="config-tag hot">即时生效</span></label>
          <select class="form-select" data-cfg="files.ocr.tesseract.preprocess" data-type="boolean">
            <option value="true" ${tesseractCfg.preprocess !== false ? 'selected' : ''}>开启</option>
            <option value="false" ${tesseractCfg.preprocess === false ? 'selected' : ''}>关闭</option>
          </select>
          <div class="hint">灰度+Otsu 二值化+中值滤波（需 opencv-python）</div>
        </div>
      </div>
      <div class="form-section-title" style="margin-top:16px;font-size:11px">视觉 LLM（L3 终极兜底）</div>
      <div class="form-group">
        <label>启用视觉 LLM<span class="config-tag hot">即时生效</span></label>
        <select class="form-select" data-cfg="files.ocr.vision_llm.enabled" data-type="boolean">
          <option value="true" ${visionCfg.enabled === true ? 'selected' : ''}>开启</option>
          <option value="false" ${visionCfg.enabled !== true ? 'selected' : ''}>关闭</option>
        </select>
        <div class="hint">开启后 L1/L2 均失败时调用视觉模型识别；当前默认关闭</div>
      </div>
      <div class="form-row">
        <div class="form-group">
          <label>Provider<span class="config-tag restart">需重启</span></label>
          <input class="form-input" data-cfg="files.ocr.vision_llm.provider" value="${visionCfg.provider || 'qwen'}" placeholder="qwen / openai">
        </div>
        <div class="form-group">
          <label>Model<span class="config-tag restart">需重启</span></label>
          <input class="form-input" data-cfg="files.ocr.vision_llm.model" value="${visionCfg.model || 'qwen-vl-max'}" placeholder="qwen-vl-max / gpt-4o">
        </div>
      </div>
      <div class="form-group">
        <label>API Key<span class="config-tag restart">需重启</span></label>
        <input class="form-input" type="password" data-cfg="files.ocr.vision_llm.api_key" value="${visionCfg.api_key || ''}" placeholder="sk-...">
      </div>
      <div class="form-group">
        <label>Base URL<span class="config-tag restart">需重启</span></label>
        <input class="form-input" data-cfg="files.ocr.vision_llm.base_url" value="${visionCfg.base_url || ''}" placeholder="https://dashscope.aliyuncs.com/compatible-mode/v1">
      </div>
    </div>`;

  const secStorageAdv = `
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
    </div>`;

  // ---------- 进阶分类 sections ----------
  const secLLMAdv = `
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
    </div>`;

  const secMemoryAdv = `
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
    </div>`;

  const secToolsAdv = `
    <div class="form-section">
      <div class="form-section-title">工具进阶</div>
      <div class="form-group">
        <label>Bash 超时(秒)<span class="config-tag hot">即时生效</span></label>
        <input class="form-input" type="number" data-cfg="tools.bash_timeout" value="${tools.bash_timeout || 120}">
      </div>
    </div>`;

  const secInterrupt = `
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
    </div>`;

  // ---------- 分类配置 ----------
  const categories = [
    { key: 'basic', label: '基础', icon: iconBasic(), sections: [secLLM, secMemory, secTools] },
    { key: 'security', label: '安全', icon: iconShield(), sections: [secSecurity, secGuardrail, secReadPaths] },
    { key: 'storage', label: '存储', icon: iconStorage(), sections: [secStorage, secFiles, secOCR, secStorageAdv] },
    { key: 'advanced', label: '进阶', icon: iconAdvanced(), sections: [secLLMAdv, secMemoryAdv, secToolsAdv, secInterrupt] },
  ];

  // 分类图标（统一 SVG，无 emoji）
  function iconBasic() {
    return '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>';
  }
  function iconShield() {
    return '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>';
  }
  function iconStorage() {
    return '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>';
  }
  function iconAdvanced() {
    return '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/></svg>';
  }

  const navHtml = categories.map((c, i) => `
    <div class="settings-nav-item ${i === 0 ? 'active' : ''}" data-pane="${c.key}">
      <span class="settings-nav-icon">${c.icon}</span><span>${c.label}</span>
    </div>`).join('');

  const panesHtml = categories.map((c, i) => `
    <div class="settings-pane ${i === 0 ? 'active' : ''}" data-pane="${c.key}">${c.sections.join('')}</div>`).join('');

  settingsBodyEl.innerHTML = `
    <div class="settings-layout">
      <nav class="settings-nav">${navHtml}</nav>
      <div class="settings-content">${panesHtml}</div>
    </div>`;

  bindGuardrailToggles();
  bindReasoningToggle();
  syncReasoningStatus();
  bindSettingsNav();
}

// ========== 设置导航切换 ==========
function bindSettingsNav() {
  document.querySelectorAll('.settings-nav-item').forEach(item => {
    item.addEventListener('click', () => {
      const target = item.dataset.pane;
      document.querySelectorAll('.settings-nav-item').forEach(n => n.classList.remove('active'));
      document.querySelectorAll('.settings-pane').forEach(p => p.classList.remove('active'));
      item.classList.add('active');
      const pane = document.querySelector(`.settings-pane[data-pane="${target}"]`);
      if (pane) pane.classList.add('active');
    });
  });
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
    // textarea 标记为 data-list 时，按行拆分为数组（空行/空白行过滤）
    if (el.dataset.list === 'true') {
      val = val.split('\n').map(s => s.trim()).filter(Boolean);
    } else if (el.type === 'number') {
      val = parseFloat(val) || 0;
    } else if (el.dataset.type === 'boolean') {
      val = (val === 'true');
    }
    obj[path[path.length - 1]] = val;
  });

  try {
    const data = await api('/config', { method: 'PUT', body: { config: newConfig } });
    // 同步 currentConfig，保证后续开关切换/再次保存基于最新值
    currentConfig = newConfig;
    showToast(data.message, data.needs_restart ? '' : 'success');
    if (data.needs_restart) {
      setTimeout(() => showToast('请重启服务使配置生效', ''), 3000);
    } else {
      // 路径策略等热更新项即时生效提示
      setTimeout(() => showToast('配置已即时生效', 'success'), 1500);
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

// ========== 防护系统开关（即时生效，独立于 saveConfig 全量保存） ==========

// 关闭风险提示文案
function buildRiskMsg(path) {
  const msgs = {
    'security.enabled': '关闭 HIL 审批后，文件删除、shell 执行等高危操作将直接执行不再弹确认卡片。确认关闭？',
    'guardrails.input_scan.enabled': '关闭输入扫描后，Prompt 注入攻击将不再被检测告警。确认关闭？',
    'guardrails.sanitizer.enabled': '关闭工具结果脱敏后，外部工具返回的注入内容将直接进入 LLM 上下文。确认关闭？',
    'guardrails.output_filter.enabled': '关闭 PII 过滤后，LLM 响应中的手机号/身份证/邮箱等将直接展示给用户。确认关闭？',
  };
  return msgs[path] || `确认关闭 ${path}？`;
}

// 将点分路径转为嵌套 dict，如 nestPath('a.b.c', true) → {a:{b:{c:true}}}
function nestPath(path, value) {
  const parts = path.split('.');
  const root = {};
  let cur = root;
  for (let i = 0; i < parts.length - 1; i++) {
    cur[parts[i]] = {};
    cur = cur[parts[i]];
  }
  cur[parts[parts.length - 1]] = value;
  return root;
}

// 开关切换处理：关闭时弹二次确认，确认后局部 PUT /config 即时生效
async function onGuardrailToggle(el) {
  const path = el.dataset.guardrail;
  const enabled = el.checked;
  // 关闭动作弹二次确认
  if (!enabled && !confirm(buildRiskMsg(path))) {
    el.checked = true;  // 用户取消，回滚到开启
    return;
  }
  // 局部 PUT：_deep_merge_config 保证只改这一项，不覆盖其他段
  const cfg = nestPath(path, enabled);
  try {
    await api('/config', { method: 'PUT', body: { config: cfg } });
    // 同步 currentConfig，避免后续 saveConfig 全量保存时用旧值覆盖
    const parts = path.split('.');
    let obj = currentConfig;
    for (let i = 0; i < parts.length - 1; i++) {
      if (!obj[parts[i]]) obj[parts[i]] = {};
      obj = obj[parts[i]];
    }
    obj[parts[parts.length - 1]] = enabled;
    const label = path.split('.').pop();
    showToast(`${label} 已${enabled ? '开启' : '关闭'}`, 'success');
  } catch (e) {
    el.checked = !enabled;  // 失败回滚
    showToast('更新失败: ' + e.message, 'error');
  }
}

// 绑定所有 [data-guardrail] 开关的 change 事件
function bindGuardrailToggles() {
  document.querySelectorAll('[data-guardrail]').forEach(el => {
    el.addEventListener('change', () => onGuardrailToggle(el));
  });
}

// ========== 思考模式开关（spec integrate-llm-reasoning-mode Task 18）==========

const _REASONING_TOGGLE_KEY = 'hermes_reasoning_enabled';

// SubTask 18.6：页面加载/设置弹窗打开时调 GET /reasoning/status 同步开关状态
async function syncReasoningStatus() {
  try {
    const data = await api('/reasoning/status');
    const enabled = data && data.main && data.main.enabled === true;
    const el = document.querySelector('[data-reasoning-toggle]');
    if (el) el.checked = enabled;
    try { localStorage.setItem(_REASONING_TOGGLE_KEY, enabled ? '1' : '0'); } catch {}
  } catch {
    // /reasoning/status 不可用时默认关闭
    const el = document.querySelector('[data-reasoning-toggle]');
    if (el) el.checked = false;
  }
}

// SubTask 18.3/18.4：开关切换 → POST /reasoning/toggle，关闭时二次确认
async function onReasoningToggle(el) {
  const enabled = el.checked;
  // SubTask 18.4：关闭时二次确认
  if (!enabled && !confirm('关闭思考模式后，LLM 将不再进行深度推理，回复质量可能降低。确认关闭？')) {
    el.checked = true;
    return;
  }
  try {
    await api('/reasoning/toggle', {
      method: 'POST',
      body: { enabled: enabled },
    });
    try { localStorage.setItem(_REASONING_TOGGLE_KEY, enabled ? '1' : '0'); } catch {}
    showToast(`思考模式已${enabled ? '开启' : '关闭'}，下一条消息生效`, 'success');
  } catch (e) {
    el.checked = !enabled;
    showToast('更新思考模式失败: ' + e.message, 'error');
  }
}

// 绑定 [data-reasoning-toggle] 开关的 change 事件
function bindReasoningToggle() {
  document.querySelectorAll('[data-reasoning-toggle]').forEach(el => {
    el.addEventListener('change', () => onReasoningToggle(el));
  });
}

// SubTask 18.7：多 tab 实时同步 —— 监听 localStorage 变化
window.addEventListener('storage', (e) => {
  if (e.key === _REASONING_TOGGLE_KEY) {
    const enabled = e.newValue === '1';
    document.querySelectorAll('[data-reasoning-toggle]').forEach(el => {
      el.checked = enabled;
    });
  }
});

// 暴露给其他模块
window.HermesChatSettings = {
  openSettings,
  toggleSettingsFlyout,
  closeSettingsFlyout,
  renderQuickSettings,
  renderSettingsForm,
  saveConfig,
  restartServer,
};
