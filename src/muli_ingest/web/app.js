(() => {
  'use strict';

  const API = '/api/v1';
  const state = {
    page: 'overview', sources: [], batches: [], selectedBatchId: null, status: null, statusLoading: true,
    scan: { sourceId: '', scanId: '', revision: null, roots: ['.'], requestedRoots: ['.'], directories: [], files: [], complete: false },
    settings: null, retention: null, cleanupPlan: null, files: {}, polling: null, modalAction: null, modalTrigger: null,
    loaded: { sources: false, batches: false }, errors: { sources: null, batches: null, status: null },
    session: null, appReady: false, sessionChecking: false, accessPending: false, authEpoch: 0, requests: new Set()
  };
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const make = (tag, className, value) => { const node = document.createElement(tag); if (className) node.className = className; if (value !== undefined) node.textContent = value; return node; };
  const setText = (selector, value) => { const node = $(selector); if (node) node.textContent = value == null ? '' : String(value); return node; };
  const esc = (value) => value == null ? '' : String(value);
  const json = (value) => { try { return JSON.stringify(value); } catch (_) { return ''; } };
  const apiError = (status, detail) => { const error = new Error(detail || `请求失败（${status}）`); error.status = status; return error; };

  async function request(path, options = {}) {
    const protectedRequest = path !== '/session';
    if (protectedRequest && !state.appReady) throw apiError(401, '请先完成访问验证。');
    const epoch = state.authEpoch;
    const controller = new AbortController();
    const headers = new Headers(options.headers || {});
    if (options.body !== undefined || !['GET', 'HEAD'].includes((options.method || 'GET').toUpperCase())) { headers.set('Content-Type', 'application/json'); headers.set('X-Ingest-Request', '1'); }
    if (protectedRequest) state.requests.add(controller);
    const timeout = protectedRequest ? null : window.setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetch(`${API}${path}`, { ...options, headers, credentials: 'same-origin', cache: 'no-store', signal: controller.signal });
      const raw = await response.text();
      let data = null;
      if (raw) { try { data = JSON.parse(raw); } catch (_) { data = raw; } }
      if (protectedRequest && epoch !== state.authEpoch) throw apiError(401, '访问状态已改变，请重新进入接收台。');
      if (protectedRequest && response.status === 401) {
        requireAccess('访问已失效，请重新输入口令。');
        throw apiError(401, '请重新完成访问验证。');
      }
      if (!response.ok) throw apiError(response.status, data && typeof data === 'object' ? (data.detail || data.message) : data);
      return data;
    } finally {
      state.requests.delete(controller);
      if (timeout !== null) window.clearTimeout(timeout);
    }
  }

  function stopPolling() { if (state.polling) window.clearInterval(state.polling); state.polling = null; }
  function suspendWorkspace() {
    state.appReady = false; state.authEpoch += 1; stopPolling();
    state.requests.forEach((controller) => controller.abort()); state.requests.clear();
    const dialog = $('#action-modal'); if (dialog?.open) dialog.close();
    state.modalAction = null; state.modalTrigger = null;
    $('#app-shell').hidden = true; $('#app-shell').inert = true;
    $('.skip-link').hidden = true; $('#toast-stack').replaceChildren();
    $('#mobile-nav').classList.remove('is-open'); $('#mobile-menu').setAttribute('aria-expanded', 'false');
  }
  function accessMessage(message, isError = false) {
    const node = setText('#access-message', message);
    node.classList.toggle('is-error', isError);
    $('#access-code').setAttribute('aria-invalid', String(isError));
  }
  function showAccessGate(message = '口令仅用于本次访问验证。') {
    suspendWorkspace();
    $('#access-connection').hidden = true; $('#access-gate').hidden = false;
    $('#access-code').value = '';
    accessMessage(message);
    setText('#access-transport-note', state.session?.secure_transport ? '当前连接使用 HTTPS。请在同一局域网内的工作设备上访问。' : '建议使用 HTTPS 地址访问，让局域网内的连接更安心。');
    $('#access-code').focus({ preventScroll: true });
  }
  function requireAccess(message) {
    if (!state.appReady && !$('#access-gate').hidden) return;
    state.session = { ...state.session, required: true, authenticated: false };
    state.scan = { sourceId: '', scanId: '', revision: null, roots: ['.'], requestedRoots: ['.'], directories: [], files: [], complete: false };
    state.settings = null; state.retention = null; state.cleanupPlan = null; state.files = {};
    showAccessGate(message);
  }
  function showConnectionProblem(message) {
    suspendWorkspace();
    $('#access-gate').hidden = true; $('#access-connection').hidden = false;
    $('#access-connection').setAttribute('aria-busy', 'false');
    setText('#access-connection-title', '暂时无法连接接收台');
    setText('#access-connection-message', message);
    $('#access-retry').hidden = false; $('#access-retry').focus({ preventScroll: true });
  }
  async function readSession() {
    let session;
    try { session = await request('/session'); }
    catch (error) {
      if (error.status !== 404) throw error;
      // Older local-only services do not expose a session endpoint.
      session = { required: false, authenticated: true, lan_enabled: false, secure_transport: window.location.protocol === 'https:', expires_at: null };
    }
    if (!session || typeof session.required !== 'boolean' || typeof session.authenticated !== 'boolean') throw new Error('访问状态未确认');
    return session;
  }
  async function openWorkspace() {
    state.appReady = true;
    $('#access-gate').hidden = true; $('#access-connection').hidden = true;
    $('#access-code').value = '';
    $('#app-shell').hidden = false; $('#app-shell').inert = false; $('.skip-link').hidden = false;
    $('#access-logout').hidden = state.session?.lan_enabled !== true;
    document.body.classList.toggle('has-lan-access', state.session?.lan_enabled === true);
    go('overview');
    await refreshAll();
    if (state.appReady) beginPolling();
  }
  async function checkSession() {
    if (state.sessionChecking) return;
    state.sessionChecking = true;
    $('#access-retry').hidden = true; $('#access-connection').setAttribute('aria-busy', 'true');
    setText('#access-connection-title', '正在连接接收台');
    setText('#access-connection-message', '正在确认访问状态，请稍候。');
    try {
      state.session = await readSession();
      if (state.session.required && !state.session.authenticated) showAccessGate();
      else await openWorkspace();
    } catch (_) { showConnectionProblem('请确认接收台正在运行、设备位于同一局域网，再重新连接。'); }
    finally { state.sessionChecking = false; $('#access-connection').setAttribute('aria-busy', 'false'); }
  }
  async function signIn(event) {
    event.preventDefault();
    if (state.accessPending) return;
    const field = $('#access-code'); if (!field.value) { field.focus(); return; }
    state.accessPending = true;
    const button = $('#access-submit'); setBusy(button, true, '正在验证…');
    $('#access-form').setAttribute('aria-busy', 'true'); accessMessage('正在验证登录密码…');
    try {
      const pending = request('/session', { method: 'POST', body: JSON.stringify({ access_code: field.value }) });
      field.value = ''; field.disabled = true;
      await pending;
      state.session = await readSession();
      if (state.session.required && !state.session.authenticated) throw apiError(401, '访问未通过');
      await openWorkspace();
    } catch (error) {
      const message = error.status === 401 || error.status === 403 ? '登录密码不正确，请核对后重新输入。' : error.status === 429 ? '尝试次数较多，请稍后再试。' : '未能确认访问状态，请检查连接后重试。';
      accessMessage(message, true);
    } finally {
      field.value = ''; field.disabled = false; state.accessPending = false;
      $('#access-form').setAttribute('aria-busy', 'false'); setBusy(button, false);
      if (!$('#access-gate').hidden) field.focus({ preventScroll: true });
    }
  }
  async function signOut() {
    if (state.accessPending) return;
    state.accessPending = true; stopPolling();
    const button = $('#access-logout'); setBusy(button, true, '正在退出…'); $('#app-shell').inert = true;
    try {
      await request('/session', { method: 'DELETE' });
      if (state.session?.required) requireAccess('已退出访问。再次进入时，请输入登录密码。');
      else await checkSession();
    } catch (_) { showConnectionProblem('退出结果尚未确认。请重新连接，以确认当前访问状态。'); }
    finally { state.accessPending = false; setBusy(button, false); if (state.appReady) $('#app-shell').inert = false; }
  }

  function formatBytes(bytes) {
    const value = Number(bytes);
    if (!Number.isFinite(value)) return '—';
    if (value === 0) return '0 B';
    const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']; const power = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
    return `${(value / Math.pow(1024, power)).toLocaleString('zh-CN', { maximumFractionDigits: power > 1 ? 1 : 0 })} ${units[power]}`;
  }
  function formatSpeed(bytesPerSecond) {
    const value = Number(bytesPerSecond);
    return Number.isFinite(value) && value > 0 ? `${formatBytes(value)}/s` : '—';
  }
  function formatDuration(seconds) {
    const value = Math.max(0, Math.round(Number(seconds)));
    if (!Number.isFinite(value)) return '—';
    if (value < 60) return `${value} 秒`;
    if (value < 3600) return `${Math.floor(value / 60)} 分 ${value % 60} 秒`;
    return `${Math.floor(value / 3600)} 小时 ${Math.floor(value % 3600 / 60)} 分`;
  }
  function formatDate(value) { if (!value) return '—'; const date = new Date(value); return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN', { hour12: false, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }); }
  function formatCount(value) { return Number.isFinite(Number(value)) ? Number(value).toLocaleString('zh-CN') : '—'; }
  function stateLabel(value) { return ({ COMPLETED: '已完成', COPY_VERIFIED: '精准核验通过', COPY_SIZE_VERIFIED: '数量与大小已核验', VERIFIED: '精准核验通过', SIZE_VERIFIED: '大小已核验', COPIED_UNVERIFIED: '已复制待 Hash', ALREADY_INGESTED: '历史引用', SKIPPED_EXISTING: '历史引用', COPIED: '已复制', RUNNING: '复制中', COPYING: '复制中', SCANNING: '扫描中', VERIFYING: '集中核验中', FINAL_SOURCE_CHECK: '来源终检', FINALIZING: '整理中', PREPARING: '准备中', RECOVERING: '恢复中', INTERRUPTED: '待处理', FAILED: '失败', PENDING: '排队中', QUEUED: '排队中', CANCELLED: '已取消', CLEANUP_FAILED: '清理失败', TRASHED: '回收站', PURGED: '已清理', ACTIVE: '留存中' }[String(value || '').toUpperCase()] || value || '未知'); }
  function stateClass(value) { return `status-${String(value || 'unknown').toLowerCase().replace(/[^a-z0-9_-]/g, '-')}`; }
  function isActive(batch) { return ['RUNNING', 'COPYING', 'SCANNING', 'VERIFYING', 'FINALIZING', 'PREPARING', 'RECOVERING', 'PENDING', 'QUEUED'].includes(String(batch?.state || '').toUpperCase()); }
  function batchId(batch) { return batch?.batch_id || batch?.batch_uid || ''; }
  function sourceId(source) { return source?.source_id || source?.id || ''; }
  function notify(message, tone = 'info') {
    if (!state.appReady) return;
    const stack = $('#toast-stack'); if (!stack) return;
    const toast = make('div', `toast toast-${tone}`, message); toast.setAttribute('role', tone === 'error' ? 'alert' : 'status'); stack.append(toast); while (stack.children.length > 2) stack.firstElementChild.remove();
    window.setTimeout(() => toast.remove(), 4800);
  }
  function setBusy(button, busy, busyText = '处理中…') { if (!button) return; button.setAttribute('aria-busy', String(busy)); if (busy) { button.dataset.previousText = button.textContent; button.textContent = busyText; button.disabled = true; } else { button.textContent = button.dataset.previousText || button.textContent; button.disabled = false; } }
  function addField(parent, label, value, className = '') { const item = make('div', className || 'detail-field'); item.append(make('span', 'detail-label', label), make('strong', 'detail-value', value)); parent.append(item); return item; }

  function go(page) {
    if (!state.appReady) return;
    if (!document.querySelector(`[data-page-view="${page}"]`)) return;
    state.page = page;
    $$('.page').forEach((section) => { const active = section.dataset.pageView === page; section.hidden = !active; section.classList.toggle('is-visible', active); });
    $$('.nav-item, .mobile-nav button').forEach((item) => { const active = item.dataset.page === page; item.classList.toggle('is-active', active); if (active) item.setAttribute('aria-current', 'page'); else item.removeAttribute('aria-current'); });
    setText('#page-title', ({ overview: '接收总览', sources: '来源设备', batches: '批次记录', retention: '留存与回收站', settings: '设置' })[page] || page);
    $('#mobile-nav')?.classList.remove('is-open'); $('#mobile-menu')?.setAttribute('aria-expanded', 'false');
    const title = document.querySelector(`[data-page-view="${page}"] h1`); if (title) { title.tabIndex = -1; title.focus({ preventScroll: true }); }
    window.scrollTo({ top: 0, behavior: 'instant' });
    if (page === 'sources') renderSources();
    if (page === 'batches') renderBatches();
    if (page === 'retention') loadRetention();
    if (page === 'settings' && !state.settings) loadSettings();
  }

  function renderStatus() {
    const value = state.status; const connected = Boolean(value);
    $('#connection-dot')?.classList.toggle('is-online', connected);
    setText('#connection-label', connected ? (value.mode === 'demo' ? '演示服务在线' : state.session?.lan_enabled ? '局域网服务在线' : '本地服务在线') : '接收服务不可用');
    setText('#connection-version', connected ? `v${esc(value.version || '—')}` : '等待连接');
    setText('.local-chip', state.session?.lan_enabled ? 'LAN' : 'LOCAL');
    const pill = $('#mode-pill'); if (pill) { pill.className = `status-pill ${connected ? 'is-online' : 'is-offline'}`; pill.replaceChildren(make('i'), document.createTextNode(connected ? (value.mode === 'demo' ? '演示模式' : state.session?.lan_enabled ? '局域网模式' : '本地模式') : '连接失败')); }
    setText('#production-badge', connected ? 'DEV / ' + (value.version || '0.1') : 'OFFLINE');
    const notice = $('#environment-notice'); if (notice) { notice.classList.toggle('notice-warning', connected && value.production_ready === false); const title = $('strong', notice); if (title) title.textContent = connected ? ('开发验证版 · 尚未用于生产') : '无法读取接收环境'; const copy = $('p', notice); if (copy) copy.textContent = connected ? `${value.mode === 'demo' ? '正在使用合成演示素材。' : state.session?.lan_enabled ? '当前通过局域网访问接收台。' : '当前为本机回环服务。'} 不会因刷新自动开始复制。` : '请确认接收服务正在运行，再重试连接。'; }
    const capabilities = connected && value.capabilities ? value.capabilities : {};
    const capabilityMessages = [];
    if (capabilities.auto_cleanup_supported === false) capabilityMessages.push('自动清理未启用');
    if (capabilities.purge_supported === false) capabilityMessages.push('永久删除未启用');
    const capabilityNote = $('#capability-note'); if (capabilityNote) { capabilityNote.hidden = !capabilityMessages.length; capabilityNote.textContent = capabilityMessages.length ? `${capabilityMessages.join('；')}。` : ''; }
    renderMaterialRoute();
    renderStorageLocation();
    renderCleanupControls();
    renderSources();
    renderBatchIdentity();
  }

  function stagingRoot() { const root = state.status?.staging_root; return typeof root === 'string' && root.trim() ? root : ''; }
  function renderStorageLocation() {
    const root = stagingRoot();
    const unavailable = state.errors.status ? '服务离线 · 路径未确认' : '服务未返回中转路径';
    setText('#storage-path-state', state.statusLoading ? '正在读取路径' : root ? '当前服务使用的完整路径' : unavailable);
    const path = setText('#storage-root-path', state.statusLoading ? '等待本地服务返回保存位置。' : root || (state.errors.status ? '请确认本地服务正在运行，再重新读取。' : '当前保存位置未知，请检查服务启动配置。'));
    if (path) path.classList.toggle('is-unavailable', state.statusLoading || !root);
    const button = $('#copy-staging-path');
    if (button && button.getAttribute('aria-busy') !== 'true') button.disabled = state.statusLoading || !root;
  }
  function copyTextFallback(value) {
    const active = document.activeElement;
    const selection = window.getSelection();
    const ranges = selection ? Array.from({ length: selection.rangeCount }, (_, index) => selection.getRangeAt(index).cloneRange()) : [];
    const field = make('textarea', 'clipboard-fallback');
    field.value = value; field.readOnly = true; field.tabIndex = -1;
    field.setAttribute('aria-label', '用于复制的完整文本');
    document.body.append(field);
    try { field.focus({ preventScroll: true }); field.select(); field.setSelectionRange(0, value.length); return typeof document.execCommand === 'function' && document.execCommand('copy'); }
    finally { field.remove(); if (active?.isConnected) active.focus({ preventScroll: true }); if (selection) { selection.removeAllRanges(); ranges.forEach((range) => selection.addRange(range)); } }
  }
  async function copyText(value, button, successMessage, failureMessage) {
    const restoreFocus = document.activeElement === button;
    setBusy(button, true, '复制中…');
    try {
      let copied = false;
      try { if (typeof navigator.clipboard?.writeText === 'function') { await navigator.clipboard.writeText(value); copied = true; } } catch (_) { /* The browser may require the local copy fallback. */ }
      if (!copied) copied = copyTextFallback(value);
      notify(copied ? successMessage : failureMessage, copied ? 'success' : 'error');
    } catch (_) { notify(failureMessage, 'error'); }
    finally { setBusy(button, false); if (restoreFocus && button?.isConnected && !button.disabled) button.focus({ preventScroll: true }); }
  }
  async function copyStagingPath() {
    const root = stagingRoot();
    if (state.statusLoading || !root) { notify('当前中转路径尚未确认，请重新读取后再复制。', 'warning'); return; }
    await copyText(root, $('#copy-staging-path'), '完整中转路径已复制。', '复制失败，请手动选中并复制上方完整路径。');
    renderStorageLocation();
  }

  function identityValue(value) { return typeof value === 'string' && value.trim() ? value : ''; }
  function identityLevel(source) {
    return ({
      high: { label: '高 · 卷 UUID 支持', tone: 'high', description: '介质身份由卷 UUID 支持。' },
      low: { label: '低 · 增量复用受限', tone: 'low', description: '设备身份信息不足或 UUID 冲突，增量复用受限。' },
      local_path: { label: '本地路径识别', tone: 'local', description: '仅按本地路径与目录身份识别，不是相机卡序列号。' }
    })[source?.identity_confidence] || { label: '未知 · 尚无身份依据', tone: 'unknown', description: '身份等级未提供，暂时无法判断识别依据。' };
  }
  function isDemoDirectory(source) { return state.status?.mode === 'demo' && source?.identity_confidence === 'local_path'; }
  function identityField(list, label, value, options = {}) {
    const row = make('div', `identity-field${options.wide ? ' identity-field-wide' : ''}`);
    const term = make('dt', '', label);
    if (options.hint) term.append(make('small', '', options.hint));
    const definition = make('dd');
    const text = identityValue(value);
    const code = make('code', `identity-value${text ? '' : ' is-missing'}`, text || options.empty || '未提供');
    code.setAttribute('aria-label', `${label}：${text || options.empty || '未提供'}`);
    definition.append(code);
    if (options.copy) {
      const button = make('button', 'identity-copy', '复制');
      button.type = 'button'; button.disabled = !text;
      button.dataset.identityKey = options.key;
      button.setAttribute('aria-label', `复制${options.context ? options.context + '的' : ''}${label}`);
      button.title = text ? `复制完整${label}` : `${label}未提供`;
      button.addEventListener('click', () => copyText(text, button, `${label}已复制。`, `复制失败，请手动选中并复制完整${label}。`));
      definition.append(button);
    }
    row.append(term, definition); list.append(row);
  }
  function identityBlock(source, { compact = false, frozen = false, context = '', key = '', demo = false } = {}) {
    const section = make('section', `media-identity${compact ? ' media-identity-compact' : ''}${frozen ? ' media-identity-frozen' : ''}`);
    section.setAttribute('aria-label', frozen ? '批次创建时的来源身份' : compact ? '当前选中来源的身份' : '介质身份');
    const heading = make('div', 'identity-heading');
    heading.append(make('h3', '', frozen ? '来源身份' : compact ? '当前扫描来源' : '介质身份'), make('span', '', frozen ? 'BATCH SNAPSHOT' : compact ? 'SELECTED MEDIA' : 'MEDIA IDENTITY'));
    section.append(heading);
    if (demo) section.append(make('p', 'identity-demo-note', '演示目录，不是实体存储卡'));
    if (compact) section.append(make('strong', 'identity-source-name', source.label || '未命名来源'));
    if (frozen) section.append(make('p', 'identity-snapshot-note', '以下身份在创建批次时保存。'));
    const fields = make('dl', 'identity-fields');
    identityField(fields, '卡 ID', source.source_id, { wide: true, hint: compact ? '' : '介质身份标识', copy: true, context, key: `${key}:card` });
    identityField(fields, '本次连接 ID', source.session_id, { wide: true, hint: compact || frozen ? '' : '重新插卡 / 重新挂载后变化', copy: true, context, key: `${key}:session` });
    if (!compact && !frozen) {
      identityField(fields, '卷 UUID', source.volume_uuid, { wide: true });
      identityField(fields, '文件系统', source.filesystem, { wide: true, empty: '未知' });
    }
    const level = identityLevel(source);
    const levelRow = make('div', `identity-field identity-field-wide identity-confidence identity-confidence-${level.tone}`);
    const definition = make('dd');
    definition.append(make('span', 'identity-grade', level.label));
    if (!compact || level.tone !== 'high') definition.append(make('p', '', level.description));
    levelRow.append(make('dt', '', '身份等级'), definition); fields.append(levelRow); section.append(fields);
    return section;
  }
  function renderScanIdentity() {
    const host = $('#scan-source-identity'); if (!host) return;
    host.replaceChildren(); host.hidden = !state.scan.sourceId;
    if (!state.scan.sourceId) return;
    const source = state.sources.find((item) => sourceId(item) === state.scan.sourceId);
    if (!source) { host.append(make('p', 'identity-connection-note', state.errors.sources ? '来源读取失败，当前扫描身份未确认。请刷新来源。' : '当前选中的来源已不可见，请重新选择。')); return; }
    host.append(identityBlock(source, { compact: true, context: '当前扫描来源', key: `scan:${sourceId(source)}`, demo: isDemoDirectory(source) }));
    if (!source.connected) host.append(make('p', 'identity-connection-note', '来源已失联，当前不能扫描。'));
  }
  function renderBatchIdentity() {
    const host = $('#batch-source-identity'); const batch = getSelectedBatch(); if (!host || !batch) return;
    const focusKey = document.activeElement?.dataset.identityKey;
    const saved = batch.source && typeof batch.source === 'object' ? batch.source : {};
    const source = { ...saved, source_id: identityValue(saved.source_id) || identityValue(batch.source_id), session_id: identityValue(saved.session_id) || identityValue(batch.source_session_id) };
    const section = identityBlock(source, { frozen: true, context: '批次创建时', key: `batch:${batchId(batch)}`, demo: batch.mode === 'demo' });
    const current = source.source_id ? state.sources.find((item) => sourceId(item) === source.source_id) : null;
    let note = '';
    if (!source.session_id) note = '此批次未记录连接 ID，无法比较当前连接会话。';
    else if (!state.loaded.sources) note = '当前来源状态未确认，暂时无法比较连接会话。';
    else if (!current || !current.connected) note = '当前来源未连接；上方保留批次创建时的身份。';
    else if (identityValue(current.session_id) && current.session_id !== source.session_id) note = '当前重新连接过 · 批次保留的是创建时的连接 ID。';
    else if (!identityValue(current.session_id)) note = '当前来源未提供连接 ID，暂时无法比较连接会话。';
    if (note) section.append(make('p', 'identity-connection-note', note));
    host.replaceChildren(section);
    if (focusKey) $$('[data-identity-key]', host).find((node) => node.dataset.identityKey === focusKey)?.focus({ preventScroll: true });
  }

  function renderOverview() {
    const active = state.batches.filter(isActive); const complete = state.batches.filter((batch) => ['COMPLETED', 'COPY_VERIFIED'].includes(String(batch.state || '').toUpperCase()));
    setText('#metric-active', state.loaded.batches ? formatCount(active.length) : '—'); setText('#metric-active-note', active.length ? '需要关注的进行中任务' : '当前没有进行中任务');
    const verified = state.batches.reduce((sum, batch) => sum + Number(batch.summary?.verified_file_count || 0), 0); setText('#metric-volume', state.loaded.batches ? formatCount(verified) : '—'); setText('#metric-volume-note', '按批次累计，含历史批次');
    const retentionCount = state.batches.filter((batch) => batch.lifecycle?.storage_state === 'ACTIVE' || batch.lifecycle?.storage_state === 'TRASHED').length; setText('#metric-retention', state.loaded.batches ? formatCount(retentionCount) : '—'); setText('#metric-retention-note', '有生命周期记录的批次'); const connectedSourceCount = state.sources.filter((source) => source.connected).length; setText('#metric-sources', state.loaded.sources ? formatCount(connectedSourceCount) : '—'); setText('#metric-sources-note', connectedSourceCount ? '当前已连接来源' : '当前没有连接来源');
    const count = $('#active-count'); if (count) { count.hidden = !active.length; count.textContent = String(active.length); }
    renderMaterialRoute();
    const activeHost = $('#overview-active'); if (activeHost) { activeHost.replaceChildren(); if (!state.loaded.batches) activeHost.append(empty('—', state.errors.batches ? '批次读取失败' : '正在读取批次队列', state.errors.batches || '等待本地服务返回数据。', true)); else if (!active.length) activeHost.append(empty('—', '接收队列空闲', '从来源设备开始扫描，确认范围后手动启动。', true)); else active.slice(0, 3).forEach((batch) => activeHost.append(batchRow(batch, true))); }
    const sourcesHost = $('#overview-sources'); if (sourcesHost) { const connectedSources = state.sources.filter((source) => source.connected); sourcesHost.replaceChildren(); if (!state.loaded.sources) sourcesHost.append(empty('—', state.errors.sources ? '来源读取失败' : '正在读取来源设备', state.errors.sources || '等待本地服务返回数据。', true)); else if (!connectedSources.length) sourcesHost.append(empty('—', '当前没有连接来源', '请连接设备后刷新状态。', true)); else connectedSources.slice(0, 3).forEach((source) => sourcesHost.append(sourceRow(source, false))); }
    const recent = $('#overview-recent'); if (recent) { recent.replaceChildren(); if (!state.loaded.batches) recent.append(make('span', 'muted', state.errors.batches || '正在读取批次记录。')); else if (!complete.length) recent.append(make('span', 'muted', '暂无已完成批次。')); else complete.slice(0, 4).forEach((batch) => recent.append(recentItem(batch))); }
  }
  function renderMaterialRoute() {
    const connected = state.sources.filter((source) => source.connected);
    const active = state.batches.filter(isActive);
    setText('#flow-state', !state.status ? '服务未连接' : !state.loaded.batches ? '读取队列中' : active.length ? `${active.length} 个批次进行中` : '队列空闲 / IDLE');
    setText('#flow-source-title', !state.loaded.sources ? '等待来源数据' : connected.length === 1 ? connected[0].label || '已连接来源' : connected.length ? `${connected.length} 个来源已连接` : '未连接来源');
    setText('#flow-source-note', connected.length ? '只读扫描 · 原始文件保留' : '连接来源后刷新状态');
    setText('#flow-process-title', active.length ? stateLabel(active[0].state) : '先扫描，再手动启动');
    setText('#flow-process-note', active.length ? active[0].progress?.relative_path || '等待服务端进度' : '来源与目标独立核验');
    const root = stagingRoot();
    const target = setText('#flow-target-note', root ? root.split('/').filter(Boolean).slice(-2).join('/') : state.statusLoading ? '正在读取路径' : state.errors.status ? '服务离线 · 路径未确认' : '未返回中转路径');
    if (target) target.title = root || '';
  }
  function empty(icon, title, body, compact = false) { const node = make('div', `empty-state${compact ? ' compact' : ''}`); const symbol = make('span', 'empty-icon', icon); symbol.setAttribute('aria-hidden', 'true'); node.append(symbol, make('strong', '', title), make('p', '', body)); return node; }
  function batchRow(batch, compact = false) {
    const button = make('button', `batch-row ${compact ? 'batch-row-compact' : ''}`, ''); button.type = 'button'; button.dataset.batchId = batchId(batch); button.classList.toggle('is-selected', state.selectedBatchId === batchId(batch)); button.setAttribute('aria-pressed', String(state.selectedBatchId === batchId(batch)));
    const title = batch.source_label || batch.source_id || '未命名来源'; const top = make('div', 'row-top'); top.append(make('strong', 'row-title', title), make('span', `status-tag ${stateClass(batch.state)}`, stateLabel(batch.state)));
    const meta = make('div', 'row-meta'); meta.append(make('span', '', formatDate(batch.created_at)), make('span', '', `${formatCount(batch.summary?.selected_file_count)} 个文件`)); if (isActive(batch)) meta.append(make('span', 'row-speed', formatSpeed(batch.progress?.speed_bps)));
    const bottom = make('div', 'row-bottom'); const progress = make('div', 'progress-track'); const bar = make('span', 'progress-bar'); const pct = progressPercent(batch); bar.style.width = `${pct}%`; progress.setAttribute('role', 'progressbar'); progress.setAttribute('aria-label', '批次复制进度'); progress.setAttribute('aria-valuenow', String(pct)); progress.setAttribute('aria-valuemin', '0'); progress.setAttribute('aria-valuemax', '100'); progress.append(bar); bottom.append(progress, make('span', 'progress-value', `${pct}%`));
    button.append(top, meta, bottom); button.addEventListener('click', () => { selectBatch(batchId(batch)); if (compact) go('batches'); }); return button;
  }
  function sourceRow(source, detailed = true) { const row = make('div', `source-row ${detailed ? 'source-row-detailed' : ''}`); const icon = make('span', 'source-avatar', 'M'); icon.setAttribute('aria-hidden', 'true'); const text = make('div', 'source-row-text'); text.append(make('strong', '', source.label || source.source_id || '未命名来源'), make('small', '', source.path || '路径由服务端配置')); text.title = source.path || ''; const status = make('span', `source-status ${source.connected ? 'is-connected' : ''}`, source.connected ? '已连接' : '曾经连接'); row.append(icon, text, status); if (detailed) { const info = make('div', 'source-row-extra'); info.append(make('span', '', `${({ transfer_storage: '中转介质', camera_card: '相机存储卡', local_directory: '本地目录', capture_media: '拍摄介质' })[source.classification] || source.classification || '未分类'}`), make('span', '', source.connected ? `连接于 ${formatDate(source.connected_at || source.last_seen_at)}` : `最近连接 ${formatDate(source.last_seen_at || source.disconnected_at)}`)); row.append(info); } return row; }
  function recentItem(batch) { const item = make('button', 'recent-item', ''); item.type = 'button'; item.append(make('span', 'recent-dot'), make('strong', '', batch.source_label || batch.source_id || '未命名来源'), make('small', '', `${stateLabel(batch.state)} · ${formatDate(batch.completed_at || batch.created_at)}`)); item.addEventListener('click', () => { selectBatch(batchId(batch)); go('batches'); }); return item; }
  function progressPercent(batch) { const state = String(batch.state || '').toUpperCase(); if (['COMPLETED', 'COPY_VERIFIED'].includes(state)) return 100; const progress = batch.progress || {}; const total = Number(progress.total_bytes || batch.summary?.selected_bytes || 0); const current = Number(progress.bytes || batch.summary?.copied_bytes || 0); if (!total || !Number.isFinite(current)) return 0; return Math.max(0, Math.min(100, Math.round(current / total * 100))); }

  function renderSources() {
    const list = $('#source-list'); if (!list) return;
    const focusKey = document.activeElement?.dataset.identityKey;
    list.replaceChildren();
    if (state.errors.sources) list.append(empty('!', '来源读取失败', state.errors.sources + '，请刷新重试。'));
    else if (!state.loaded.sources) list.append(empty('—', '正在读取来源', '等待本地服务返回介质身份。'));
    else {
      const connected = state.sources.filter((source) => source.connected).sort((a, b) => String(b.connected_at || b.last_seen_at || '').localeCompare(String(a.connected_at || a.last_seen_at || '')));
      const previous = state.sources.filter((source) => !source.connected && source.ever_connected).sort((a, b) => String(b.last_seen_at || b.disconnected_at || '').localeCompare(String(a.last_seen_at || a.disconnected_at || '')));
      if (!connected.length) list.append(empty('—', '当前没有连接设备', '连接设备后点“刷新来源”。曾经连接的设备保留在下方历史中。'));
      else {
        const heading = make('div', 'source-section-heading'); heading.append(make('strong', '', '当前连接'), make('span', 'muted', `${connected.length} 个`)); list.append(heading);
      }
      connected.forEach((source) => {
        const card = make('article', 'source-card');
        card.append(sourceRow(source, true), identityBlock(source, { context: source.label || '来源', key: `source:${sourceId(source)}`, demo: isDemoDirectory(source) }));
        const actions = make('div', 'source-card-actions');
        const available = Boolean(sourceId(source));
        const button = make('button', 'button button-secondary', available ? '选择此来源' : '来源身份未提供');
        button.type = 'button'; button.disabled = !available;
        button.title = available ? '选择此来源后手动扫描' : '来源缺少卡 ID，不能扫描';
        button.addEventListener('click', () => {
          const select = $('#scan-source');
          if (select) { select.value = sourceId(source); select.dispatchEvent(new Event('change')); }
          $('#scan-panel')?.scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth', block: 'start' });
          $('#scan-source')?.focus({ preventScroll: true });
        });
        actions.append(button); card.append(actions); list.append(card);
      });
      if (previous.length) {
        const history = document.createElement('details'); history.className = 'source-history';
        const summary = document.createElement('summary'); summary.append(make('strong', '', '曾经连接'), make('span', 'muted', `${previous.length} 个 · 点击展开`)); history.append(summary);
        const historyList = make('div', 'source-history-list');
        previous.forEach((source) => { const card = make('article', 'source-card source-card-history'); card.append(sourceRow(source, true), identityBlock(source, { context: source.label || '历史来源', key: `history:${sourceId(source)}`, demo: isDemoDirectory(source) })); historyList.append(card); });
        history.append(historyList); list.append(history);
      }
    }
    const select = $('#scan-source');
    if (select) {
      const current = state.scan.sourceId; select.replaceChildren(); select.append(new Option('请选择来源设备', ''));
      state.sources.filter((source) => source.connected).forEach((source) => { const option = new Option(source.label || source.source_id || '未命名来源', sourceId(source)); option.disabled = !sourceId(source); select.append(option); });
      if (current) select.value = current;
    }
    renderScanIdentity();
    updateScanControls();
    if (focusKey) $$('[data-identity-key]').find((node) => node.dataset.identityKey === focusKey)?.focus({ preventScroll: true });
  }
  function scanSourceAvailable() { return Boolean(state.sources.find((source) => sourceId(source) === state.scan.sourceId)?.connected); }
  function updateScanControls() {
    const scanButton = $('#scan-button'); if (scanButton && !scanButton.dataset.busy) { scanButton.disabled = !scanSourceAvailable(); scanButton.title = scanSourceAvailable() ? '' : '请选择已连接的来源设备'; }
    const start = $('#start-batch-button'); if (start && !start.dataset.busy) { const changed = json(state.scan.roots) !== json(state.scan.requestedRoots); start.disabled = changed || !state.scan.complete || !scanSourceAvailable(); if (!scanSourceAvailable() && state.scan.scanId) start.textContent = '来源已失联'; else start.textContent = changed ? '请先重新扫描范围' : '确认范围并开始'; }
  }
  function renderDirectoryPicker() {
    const picker = $('#directory-picker'); if (!picker) return; picker.hidden = !state.scan.scanId; const tree = $('#directory-tree'); if (!tree) return; tree.replaceChildren(); const dirs = Array.from(new Set(['.', ...(state.scan.directories || [])])).sort((a, b) => a === '.' ? -1 : b === '.' ? 1 : a.localeCompare(b, 'zh-CN')); if (!dirs.length) tree.append(make('span', 'muted', '服务端没有返回可选择的目录。')); dirs.forEach((dir) => { const label = make('label', 'directory-option'); const input = document.createElement('input'); input.type = 'checkbox'; input.value = dir; input.checked = state.scan.roots.includes(dir); input.addEventListener('change', () => { state.scan.roots = $$('#directory-tree input:checked').map((item) => item.value); if (!state.scan.roots.length) { input.checked = true; state.scan.roots = [dir]; } const changed = json(state.scan.roots) !== json(state.scan.requestedRoots); const start = $('#start-batch-button'); if (start) { start.disabled = changed || !state.scan.complete || !scanSourceAvailable(); start.textContent = !scanSourceAvailable() && state.scan.scanId ? '来源已失联' : changed ? '请先重新扫描范围' : '确认范围并开始'; } setText('#directory-count', `${state.scan.roots.length} 项范围`); }); label.append(input, make('span', '', dir === '.' ? '全卷（.）' : dir)); tree.append(label); }); setText('#directory-count', `${state.scan.roots.length} 项范围`); const start = $('#start-batch-button'); if (start) { start.disabled = json(state.scan.roots) !== json(state.scan.requestedRoots) || !state.scan.complete || !scanSourceAvailable(); start.textContent = !scanSourceAvailable() && state.scan.scanId ? '来源已失联' : json(state.scan.roots) !== json(state.scan.requestedRoots) ? '请先重新扫描范围' : '确认范围并开始'; }
  }
  function renderScanSummary() { const node = $('#scan-summary'); if (!node) return; node.hidden = !state.scan.scanId; node.replaceChildren(); if (!state.scan.scanId) return; node.append(make('strong', '', state.scan.complete ? '扫描完成，可选择范围' : '扫描未完成'), make('span', '', `${formatCount(state.scan.files.length)} 个文件 · ${formatBytes(state.scan.files.reduce((sum, file) => sum + Number(file.size_bytes || 0), 0))}`)); if (state.scan.errors?.length) node.append(make('small', 'warning-text', `${state.scan.errors.length} 个路径无法读取，开始前请查看服务端结果。`)); updateScanControls(); }

  async function scanSource() {
    const source = $('#scan-source')?.value;
    if (!source) { notify('请先选择来源设备。', 'warning'); return; }
    state.scan.sourceId = source;
    if (!scanSourceAvailable()) { notify('来源设备已失联，不能开始扫描。', 'warning'); updateScanControls(); return; }
    const button = $('#scan-button'); button.dataset.busy = '1'; setBusy(button, true, '扫描中…');
    try { const roots = state.scan.roots?.length ? state.scan.roots : ['.']; const result = await request(`/sources/${encodeURIComponent(source)}/scans`, { method: 'POST', body: JSON.stringify({ selected_roots: roots }) }); state.scan = { ...state.scan, sourceId: source, scanId: result.scan_id, revision: result.revision, requestedRoots: roots, roots, directories: result.directories || [], files: result.files || [], errors: result.errors || [], complete: Boolean(result.complete) }; renderScanSummary(); renderDirectoryPicker(); notify('扫描完成。确认范围后再开始复制。', 'success'); } catch (error) { notify(error.message, 'error'); } finally { setBusy(button, false, '开始扫描'); delete button.dataset.busy; updateScanControls(); }
  }
  async function startBatch() {
    const start = $('#start-batch-button'); if (json(state.scan.roots) !== json(state.scan.requestedRoots)) { notify('范围已改变，请再次扫描后再开始。', 'warning'); return; } if (!state.scan.scanId || !state.scan.complete || !state.scan.sourceId) { notify('请先完成扫描。', 'warning'); return; } if (!scanSourceAvailable()) { notify('来源设备已失联，不能开始批次。', 'warning'); updateScanControls(); return; }
    start.dataset.busy = '1'; setBusy(start, true, '创建批次…'); try { const result = await request('/batches', { method: 'POST', headers: { 'Idempotency-Key': (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`) }, body: JSON.stringify({ source_id: state.scan.sourceId, scan_id: state.scan.scanId, scan_revision: state.scan.revision }) }); const batch = result?.batch || result; if (batch && batchId(batch)) { state.batches = [batch, ...state.batches.filter((item) => batchId(item) !== batchId(batch))]; selectBatch(batchId(batch)); } notify('批次已创建，复制将由服务端执行。', 'success'); go('batches'); await loadBatches(); } catch (error) { notify(error.message, 'error'); } finally { setBusy(start, false, '确认范围并开始'); delete start.dataset.busy; updateScanControls(); }
  }

  function renderBatches() { const focused = document.activeElement; const focusAction = focused?.dataset.action; const focusIdentity = focused?.dataset.identityKey; const focusBatch = focused?.dataset.batchId; const list = $('#batch-list'); if (!list) return; list.replaceChildren(); if (!state.loaded.batches && state.errors.batches) list.append(empty('!', '批次读取失败', state.errors.batches + '，请刷新重试。')); else if (!state.batches.length) list.append(empty('—', '还没有批次', '完成一次扫描后，手动开始的批次会出现在这里。')); else state.batches.forEach((batch) => list.append(batchRow(batch))); renderBatchDetail(); if (focusIdentity) $$('[data-identity-key]', $('#batch-detail')).find((node) => node.dataset.identityKey === focusIdentity)?.focus({ preventScroll: true }); else if (focusAction) $(`[data-action="${CSS.escape(focusAction)}"]`)?.focus({ preventScroll: true }); else if (focusBatch) $(`[data-batch-id="${CSS.escape(focusBatch)}"]`, list)?.focus({ preventScroll: true }); }
  function selectBatch(id) { if (!id) return; state.selectedBatchId = id; renderBatches(); }
  function getSelectedBatch() { return state.batches.find((batch) => batchId(batch) === state.selectedBatchId); }
  function renderBatchDetail() {
    const host = $('#batch-detail'); if (!host) return; const batch = getSelectedBatch(); host.replaceChildren(); if (!batch) { host.append(empty('↗', '选择一个批次', '查看进度、文件结果和留存操作。')); return; }
    const head = make('div', 'detail-heading'); const title = make('div'); title.append(make('p', 'eyebrow', 'BATCH RECEIPT'), make('h2', '', batch.source_label || batch.source_id || '未命名来源')); const tag = make('span', `status-tag status-tag-large ${stateClass(batch.state)}`, stateLabel(batch.state)); head.append(title, tag); host.append(head);
    const idLine = make('p', 'detail-id', `批次 ${batchId(batch)} · 创建于 ${formatDate(batch.created_at)}`); host.append(idLine);
    const identity = make('div', 'batch-source-identity'); identity.id = 'batch-source-identity'; host.append(identity); renderBatchIdentity();
    if (batch.error) { const error = make('div', 'inline-error'); error.append(make('strong', '', '服务端报告错误'), make('span', '', batch.error)); host.append(error); }
    const progressCard = make('div', 'progress-card'); const progressTop = make('div', 'progress-card-top'); progressTop.append(make('strong', '', batch.progress?.relative_path || (isActive(batch) ? '正在等待服务端进度…' : '复制进度')), make('span', 'progress-value', `${progressPercent(batch)}%`)); const track = make('div', 'progress-track progress-track-large'); const bar = make('span', 'progress-bar'); bar.style.width = `${progressPercent(batch)}%`; track.append(bar); progressCard.append(progressTop, track); if (batch.progress?.stage) progressCard.append(make('small', 'muted', `${stateLabel(batch.progress.stage)} / ${batch.progress.stage}`)); const p = batch.progress || {}; const totalBytes = Number(p.total_bytes || batch.summary?.selected_bytes || 0); const processedBytes = Number(p.bytes || 0); const metrics = make('div', 'progress-metrics'); addField(metrics, '实时速度', formatSpeed(p.speed_bps)); addField(metrics, '平均速度', formatSpeed(p.average_speed_bps)); addField(metrics, '已处理', `${formatBytes(processedBytes)} / ${formatBytes(totalBytes)}`); addField(metrics, '预计剩余', p.eta_seconds == null ? (String(batch.state).toUpperCase() === 'COMPLETED' ? '已完成' : '测算中') : formatDuration(p.eta_seconds)); progressCard.append(metrics); host.append(progressCard);
    const summary = make('div', 'summary-grid'); const s = batch.summary || {}; addField(summary, '选中文件', formatCount(s.selected_file_count)); addField(summary, '已复制', formatCount(s.copied_file_count)); addField(summary, '精准核验', formatCount(s.verified_file_count)); addField(summary, '大小核验', formatCount(s.size_verified_file_count)); addField(summary, '等待 Hash', formatCount(s.awaiting_hash_file_count)); addField(summary, '历史引用', formatCount(s.previously_ingested_count)); addField(summary, '失败', formatCount(s.failed_file_count)); addField(summary, '数据量', formatBytes(s.selected_bytes)); host.append(summary);
    const lifecycle = make('div', 'lifecycle-card'); lifecycle.append(make('div', 'subheading', '留存状态')); const lifeGrid = make('div', 'lifecycle-grid'); addField(lifeGrid, '存储状态', stateLabel(batch.lifecycle?.storage_state)); addField(lifeGrid, '中转到期', formatDate(batch.lifecycle?.expires_at)); addField(lifeGrid, '转存确认', batch.lifecycle?.handoff_confirmed ? '人工已确认' : '未确认'); addField(lifeGrid, '保护锁', batch.lifecycle?.hold ? '已开启' : '未开启'); lifecycle.append(lifeGrid); if (batch.lifecycle?.blocked_reasons?.length) { const reasons = make('div', 'blocked-reasons'); reasons.append(make('strong', '', '当前阻塞原因')); batch.lifecycle.blocked_reasons.forEach((reason) => reasons.append(make('span', '', reason))); lifecycle.append(reasons); } host.append(lifecycle);
    const actions = make('div', 'detail-actions'); if (['RUNNING', 'COPYING'].includes(String(batch.state || '').toUpperCase())) actions.append(actionButton('interrupt', '中断复制', 'button-quiet')); if (String(batch.state || '').toUpperCase() === 'INTERRUPTED') actions.append(actionButton('resume', '继续复制', 'button-secondary')); actions.append(actionButton('files', '查看文件结果', 'button-secondary')); actions.append(actionButton('retention', batch.lifecycle?.hold ? '延长 / 解除保护' : '延长留存', 'button-quiet')); if (!batch.lifecycle?.handoff_confirmed && ['ACTIVE', undefined].includes(batch.lifecycle?.storage_state)) actions.append(actionButton('handoff', '确认已转存', 'button-primary')); const manifest = make('div', 'manifest-links'); manifest.append(make('span', 'detail-label', '批次回执')); ['md', 'json'].forEach((format) => { const link = make('a', 'manifest-link', format.toUpperCase()); link.href = `${API}/batches/${encodeURIComponent(batchId(batch))}/manifest?format=${format}`; link.target = '_blank'; link.rel = 'noopener'; manifest.append(link); }); actions.append(manifest); host.append(actions);
    const files = state.files[batchId(batch)]; if (files) host.append(renderFiles(files));
  }
  function actionButton(action, label, style) { const button = make('button', `button ${style}`, label); button.type = 'button'; button.dataset.action = action; button.addEventListener('click', () => handleBatchAction(action)); return button; }
  function renderFiles(payload) { const files = Array.isArray(payload) ? payload : payload.files || []; const section = make('div', 'files-section'); const top = make('div', 'panel-heading'); top.append(make('h3', '', '文件结果'), make('span', 'muted', `${files.length} 项`)); section.append(top); if (!files.length) { section.append(make('p', 'muted', '服务端没有返回文件结果。')); return section; } const table = make('div', 'file-table'); table.setAttribute('role', 'table'); table.setAttribute('aria-label', '批次文件结果'); const head = make('div', 'file-row file-header'); head.setAttribute('role', 'row'); head.append(make('span', '', '相对路径'), make('span', '', '状态'), make('span', '', '大小')); Array.from(head.children).forEach((cell) => cell.setAttribute('role', 'columnheader')); table.append(head); files.slice(0, 500).forEach((file) => { const row = make('div', 'file-row'); row.setAttribute('role', 'row'); row.append(make('span', 'file-name', file.relative_path || '—'), make('span', `file-status ${stateClass(file.copy_status)}`, stateLabel(file.copy_status)), make('span', '', formatBytes(file.size_bytes))); Array.from(row.children).forEach((cell) => cell.setAttribute('role', 'cell')); if (file.error) { const errorText = file.error.message || file.error.type || '文件错误'; const error = make('span', 'file-error', errorText); error.setAttribute('role', 'cell'); row.append(error); } table.append(row); }); section.append(table); if (files.length > 500) section.append(make('small', 'muted', `仅显示前 500 项，共 ${files.length} 项。`)); return section; }

  async function handleBatchAction(action) { const batch = getSelectedBatch(); if (!batch) return; const id = encodeURIComponent(batchId(batch)); if (action === 'files') { try { const result = await request(`/batches/${id}/files`); state.files[batchId(batch)] = result; renderBatchDetail(); } catch (error) { notify(error.message, 'error'); } return; } if (action === 'interrupt' || action === 'resume') { const verb = action === 'interrupt' ? '中断' : '继续'; openConfirm({ eyebrow: '批次操作', title: `${verb}这个批次？`, body: action === 'resume' ? `请先重新连接同一张存储卡并刷新来源。已完成文件会跳过，断开时正在复制的文件会从头安全重拷，其余文件继续。批次 ${batchId(batch)} 完成前仍会重新终检。` : `将对批次 ${batchId(batch)} 发送“中断”请求。`, confirmLabel: `确认${verb}`, onConfirm: async () => { await request(`/batches/${id}/${action}`, { method: 'POST', body: JSON.stringify({}) }); notify(`已发送${verb}请求。`, 'success'); await loadBatches(); } }); return; } if (action === 'handoff') { openConfirm({ eyebrow: '转存凭据', title: '确认你已另行保存这批素材？', body: '这会记录一条人工确认：我已将本批全部素材（含历史引用）另行保存。人工确认不等同于逐文件 hash 验证。', requiresAck: '我已将本批全部素材（含历史引用）另行保存', confirmLabel: '记录转存确认', onConfirm: async () => { await request(`/batches/${id}/handoffs`, { method: 'POST', body: JSON.stringify({ confirmed: true }) }); notify('已记录人工转存确认。', 'success'); await loadBatches(); } }); return; } if (action === 'retention') openRetentionModal(batch); }
  function openRetentionModal(batch) { const body = make('div', 'form-stack'); body.append(make('p', '', '填写批次完成后的总留存天数，只能延长，不能缩短。也可永久保留或设置保护锁。')); const label = make('label', 'field-label', '完成后的总留存天数（只能延长）'); const input = document.createElement('input'); input.className = 'field-control'; input.type = 'number'; input.min = '1'; input.max = '3650'; input.placeholder = '例如 30'; label.append(input); const permanentLabel = make('label', 'check-option'); const permanent = document.createElement('input'); permanent.type = 'checkbox'; permanentLabel.append(permanent, make('span', '', '永久保留（不设置到期日期）')); const holdLabel = make('label', 'check-option'); const hold = document.createElement('input'); hold.type = 'checkbox'; hold.checked = Boolean(batch.lifecycle?.hold); holdLabel.append(hold, make('span', '', '启用批次保护锁（不按到期自动清理）')); permanent.addEventListener('change', () => { input.disabled = permanent.checked; if (permanent.checked) input.value = ''; }); body.append(label, permanentLabel, holdLabel); openConfirm({ eyebrow: '留存策略', title: '更新批次保护', body, confirmLabel: '保存留存设置', onConfirm: async () => { const value = input.value.trim(); const payload = { hold: hold.checked }; if (permanent.checked) payload.retention_days = null; else if (value) payload.retention_days = Number(value); await request(`/batches/${encodeURIComponent(batchId(batch))}/retention`, { method: 'PATCH', body: JSON.stringify(payload) }); notify('批次留存设置已更新。', 'success'); await loadBatches(); } }); }

  async function loadStatus() { state.statusLoading = true; renderStorageLocation(); try { state.status = await request('/status'); state.errors.status = null; } catch (error) { state.status = null; state.errors.status = error.message; notify(`本地服务连接失败：${error.message}`, 'error'); } finally { state.statusLoading = false; renderStatus(); } }
  async function loadSources({ quiet = false } = {}) { try { const result = await request('/sources'); state.sources = result.sources || []; state.loaded.sources = true; state.errors.sources = null; renderSources(); renderOverview(); renderBatchIdentity(); } catch (error) { state.sources = []; state.loaded.sources = false; state.errors.sources = error.message; renderSources(); renderOverview(); renderBatchIdentity(); if (!quiet) notify(`来源读取失败：${error.message}`, 'error'); } }
  async function loadBatches({ quiet = false } = {}) { try { const result = await request('/batches'); const incoming = result.batches || []; const changed = !state.loaded.batches || json(incoming) !== json(state.batches); state.loaded.batches = true; state.errors.batches = null; if (!changed) return; state.batches = incoming; if (state.selectedBatchId && !incoming.some((batch) => batchId(batch) === state.selectedBatchId)) state.selectedBatchId = null; renderOverview(); renderBatches(); } catch (error) { state.errors.batches = error.message; if (!state.loaded.batches) { renderOverview(); renderBatches(); } if (!quiet) notify(`批次读取失败：${error.message}`, 'error'); } }
  async function refreshAll() { if (!state.appReady) return; await Promise.allSettled([loadStatus(), loadSources(), loadBatches({ quiet: true })]); if (state.appReady && state.page === 'retention') await loadRetention(); }

  async function loadRetention() { try { const result = await request('/retention/overview'); state.retention = result; renderRetention(); } catch (error) { if (state.page === 'retention') notify(`留存状态读取失败：${error.message}`, 'error'); } }
  function cleanupExecutionAllowed() { return state.retention?.cleanup_enabled === true && state.status?.capabilities?.cleanup_enabled === true; }
  function renderCleanupControls() { const execute = $('#cleanup-execute-button'); if (!execute || execute.dataset.busy) return; const plan = state.cleanupPlan; execute.disabled = !cleanupExecutionAllowed() || !plan?.plan_id || !plan.eligible?.length; execute.title = state.retention?.cleanup_enabled !== true ? '清理策略未启用' : state.status?.capabilities?.cleanup_enabled !== true ? '当前开发版未启用实际清理能力' : ''; }
  function renderRetention() { const data = state.retention || {}; const batches = data.batches || []; const cards = $('#retention-metrics'); if (cards) { cards.replaceChildren(); [['仍在留存', formatCount(batches.filter((batch) => batch.lifecycle?.storage_state === 'ACTIVE').length), 'ACTIVE 批次'], ['可回收空间', formatBytes(data.eligible_bytes), '预览值，不代表已释放'], ['回收站占用', formatBytes(data.trash_bytes), '回收站仍占用磁盘']].forEach(([label, value, note]) => { const card = make('article', 'metric-card'); card.append(make('span', 'metric-label', label), make('strong', '', value), make('small', '', note)); cards.append(card); }); } const enabled = data.cleanup_enabled; const autoSupported = state.status?.capabilities?.auto_cleanup_supported; setText('#cleanup-state', enabled === false ? '自动清理未启用' : autoSupported === false ? '开发版未启用自动清理' : enabled === true ? '策略已启用' : '状态未知'); renderCleanupControls(); const trash = $('#trash-list'); if (trash) { trash.replaceChildren(); const items = batches.filter((batch) => ['TRASHED', 'PURGED'].includes(String(batch.lifecycle?.storage_state || '').toUpperCase())); if (!items.length) trash.append(empty('⌁', '回收站为空', '已移入回收站的批次会在这里显示。', true)); else items.forEach((batch) => { const row = make('div', 'trash-item'); const info = make('div'); info.append(make('strong', '', batch.source_label || batchId(batch)), make('small', '', `${stateLabel(batch.lifecycle?.storage_state)} · ${formatDate(batch.lifecycle?.expires_at)}`)); const button = make('button', 'button button-quiet button-small', '还原'); button.type = 'button'; button.disabled = batch.lifecycle?.storage_state !== 'TRASHED' || state.status?.capabilities?.cleanup_enabled !== true; button.title = state.status?.capabilities?.cleanup_enabled !== true ? '当前开发版未启用还原能力' : ''; button.addEventListener('click', () => restoreBatch(batch)); row.append(info, button); trash.append(row); }); } }
  async function restoreBatch(batch) { openConfirm({ eyebrow: '回收站', title: '还原这个批次？', body: `将向服务端发送还原请求：${batchId(batch)}。目标路径冲突或依赖未满足时，服务端会保留在回收站并返回原因。`, confirmLabel: '确认还原', onConfirm: async () => { await request(`/batches/${encodeURIComponent(batchId(batch))}/restore`, { method: 'POST', body: JSON.stringify({}) }); notify('已发送还原请求。', 'success'); await loadRetention(); await loadBatches(); } }); }
  async function cleanupPreview() { const button = $('#cleanup-preview-button'); setBusy(button, true, '生成中…'); try { state.cleanupPlan = await request('/cleanup/previews', { method: 'POST', body: JSON.stringify({}) }); renderCleanupPlan(); notify('清理预览已生成，请核对精确批次清单。', 'success'); } catch (error) { notify(`清理预览失败：${error.message}`, 'error'); } finally { setBusy(button, false, '生成清理预览'); } }
  function renderCleanupPlan() { const host = $('#cleanup-plan'); const plan = state.cleanupPlan; if (!host) return; if (!plan) { host.replaceChildren(empty('—', '尚未生成清理预览', '查看候选范围、预计空间与阻塞原因。', true)); renderCleanupControls(); return; } host.replaceChildren(); const eligible = plan.eligible || []; const blocked = plan.blocked || []; const execute = $('#cleanup-execute-button'); if (execute) execute.disabled = !cleanupExecutionAllowed() || !plan.plan_id || !eligible.length; const summary = make('div', 'cleanup-summary'); summary.append(make('strong', '', `${eligible.length} 个批次可移入回收站`), make('span', '', formatBytes(plan.total_bytes))); host.append(summary); if (eligible.length) { const list = make('div', 'cleanup-items'); eligible.forEach((item) => { const row = make('div', 'cleanup-item'); row.append(make('span', 'cleanup-check', '✓'), make('span', 'file-name', item.batch_uid || item.batch_id || '未命名批次'), make('span', '', formatBytes(item.bytes))); list.append(row); }); host.append(list); } if (blocked.length) { const block = make('div', 'blocked-list'); block.append(make('strong', '', `${blocked.length} 个批次暂不可清理`)); blocked.forEach((item) => { const row = make('div', 'blocked-item'); row.append(make('span', 'file-name', item.batch_id || '未命名批次'), make('span', '', (item.reasons || []).join('、') || '服务端未提供原因')); block.append(row); }); host.append(block); } if (!eligible.length && !blocked.length) host.append(make('p', 'muted', '服务端没有返回候选批次。')); renderCleanupControls(); }
  function executeCleanup() { const plan = state.cleanupPlan; if (!cleanupExecutionAllowed() || !plan?.plan_id || !plan.eligible?.length) return; const ids = plan.eligible.map((item) => item.batch_uid || item.batch_id).filter(Boolean); const body = make('div', 'form-stack'); body.append(make('p', '', '请确认以下批次将按当前计划移入同文件系统回收站。回收站仍占磁盘，永久删除不会在此操作中发生。')); const list = make('div', 'confirm-list'); ids.forEach((id) => list.append(make('span', '', id))); body.append(list); openConfirm({ eyebrow: '执行清理', title: '确认移入回收站？', body, requiresAck: `我已核对 ${ids.length} 个批次的清单`, confirmLabel: '确认执行', onConfirm: async () => { await request('/cleanup/runs', { method: 'POST', body: JSON.stringify({ plan_id: plan.plan_id, confirmed: true }) }); notify('已提交清理执行请求。', 'success'); state.cleanupPlan = null; renderCleanupPlan(); await loadRetention(); await loadBatches(); } }); }

  const groupLabels = { retention: '中转与留存', source: '来源与复制', verification: '核验与失败处理', performance: '性能与队列', reporting: '报告与元数据', notifications: '提醒与显示', maintenance: '管理与维护' };
  const policyKeys = new Set(['retention_days', 'cleanup_enabled', 'expiry_action', 'trash_retention_days', 'trash_auto_purge', 'require_handoff_confirmation', 'auto_cleanup_hour', 'cleanup_paused']);
  async function loadSettings() { const form = $('#settings-form'); if (form) form.replaceChildren(empty('⚙', '正在读取设置结构', '页面会按服务端 schema 分组。')); try { state.settings = await request('/settings'); renderSettings(); } catch (error) { if (form) { form.replaceChildren(empty('!', '设置暂不可用', error.message)); } notify(`设置读取失败：${error.message}`, 'error'); } }
  function renderSettings() { const form = $('#settings-form'); const data = state.settings; if (!form || !data) return; form.replaceChildren(); setText('#settings-version', `版本 ${data.version ?? '—'}`); const groups = new Map(); (data.schema || []).forEach((item) => { const key = item.group || 'maintenance'; if (!groups.has(key)) groups.set(key, []); groups.get(key).push(item); }); if (!groups.size) form.append(empty('⚙', '服务端没有设置字段', '当前版本未返回可编辑 schema。')); groups.forEach((items, group) => { const section = make('section', 'settings-group'); section.append(make('h2', 'settings-group-heading', groupLabels[group] || group)); const grid = make('div', 'settings-grid'); items.forEach((schema) => grid.append(settingControl(schema, data.values || {}))); section.append(grid); form.append(section); }); if (groups.size) { const footer = make('div', 'settings-footer'); footer.append(make('span', 'muted', '保存使用服务端版本号，发生冲突时不会覆盖他人修改。')); const save = make('button', 'button button-primary', '保存设置'); save.type = 'submit'; footer.append(save); form.append(footer); } }
  function settingControl(schema, values) { const wrap = make('div', `setting-control ${schema.editable === false ? 'is-disabled' : ''}`); const label = make('label', 'setting-label', schema.label || schema.key); const inputId = `setting-${schema.key.replace(/[^a-zA-Z0-9_-]/g, '-')}`; let input; const hasValue = Object.prototype.hasOwnProperty.call(values, schema.key); const current = hasValue ? values[schema.key] : (schema.default ?? '');
    if (schema.type === 'boolean') { const check = make('label', 'toggle-control'); input = document.createElement('input'); input.type = 'checkbox'; input.checked = Boolean(current); check.append(input, make('span', 'toggle-track'), make('span', 'toggle-text', schema.label || schema.key)); wrap.append(check); }
    else if (schema.type === 'select') { input = document.createElement('select'); input.className = 'field-control'; (schema.options || []).forEach((option) => input.append(new Option(option.label || option.value, option.value))); input.value = current; label.htmlFor = inputId; wrap.append(label, input); }
    else { input = document.createElement('input'); input.className = 'field-control'; input.type = schema.type === 'number' ? 'number' : 'number'; if (schema.type === 'integer') input.step = '1'; else input.step = 'any'; if (schema.min != null) input.min = schema.min; if (schema.max != null) input.max = schema.max; input.value = current === null ? '' : current; label.htmlFor = inputId; wrap.append(label, input); }
    input.id = inputId; input.name = schema.key; input.dataset.settingKey = schema.key; input.dataset.settingType = schema.type; input.disabled = schema.editable === false; if (schema.description) { const hint = make('small', 'field-hint', schema.description); hint.id = `${inputId}-hint`; input.setAttribute('aria-describedby', hint.id); wrap.append(hint); } if (schema.key === 'retention_days') wrap.append(make('small', 'field-hint', '留空表示永久保留，不自动到期。')); if (schema.editable === false) wrap.append(make('small', 'unsupported-label', '预留功能 · 尚未实现')); return wrap;
  }
  function readSettingsValues() { const values = {}; $$('#settings-form [data-setting-key]').forEach((input) => { if (input.disabled) return; const type = input.dataset.settingType; values[input.dataset.settingKey] = type === 'boolean' ? input.checked : (input.value === '' ? null : (type === 'integer' ? Number.parseInt(input.value, 10) : type === 'number' ? Number(input.value) : input.value)); }); return values; }
  function settingDiff() { const next = readSettingsValues(); const old = state.settings?.values || {}; return Object.fromEntries(Object.entries(next).filter(([key, value]) => json(value) !== json(old[key] ?? null))); }
  function saveSettings() { const changed = settingDiff(); if (!Object.keys(changed).length) { notify('没有需要保存的改动。', 'info'); return; } const labels = Object.entries(changed).map(([key, value]) => `${state.settings.schema.find(item => item.key === key)?.label || key}：${value === null ? '永久/未设置' : value}`).join('\n'); const needsPolicy = Object.keys(changed).some((key) => policyKeys.has(key)); const perform = async (confirmPolicy) => { try { const result = await request('/settings', { method: 'PATCH', body: JSON.stringify({ expected_version: state.settings.version, values: changed, confirm_policy_change: confirmPolicy }) }); state.settings = result; renderSettings(); notify('设置已保存。', 'success'); } catch (error) { if (error.status === 409) { const notice = $('#settings-notice'); if (notice) { notice.hidden = false; notice.textContent = '保存冲突：服务端版本已经变化。请重新读取后再次检查改动。'; notice.className = 'settings-notice is-error'; } } notify(`设置保存失败：${error.message}`, 'error'); } };
    if (needsPolicy) { const body = make('div', 'form-stack'); body.append(make('p', '', '这些改动可能影响未来清理或批次留存，请确认变更内容：')); const pre = make('pre', 'change-preview', labels); body.append(pre); openConfirm({ eyebrow: '策略变更', title: '确认保存留存策略？', body, requiresAck: '我已理解这项策略变更的影响', confirmLabel: '确认并保存', onConfirm: () => perform(true) }); } else perform(false); }

  function openConfirm({ eyebrow = '需要确认', title, body, confirmLabel = '确认', requiresAck, onConfirm }) { const dialog = $('#action-modal'); if (!dialog) return; setText('#modal-eyebrow', eyebrow); setText('#modal-title', title); const bodyHost = $('#modal-body'); bodyHost.replaceChildren(); if (typeof body === 'string') bodyHost.append(make('p', '', body)); else bodyHost.append(body); state.modalTrigger = document.activeElement; const confirm = $('#modal-confirm'); confirm.textContent = confirmLabel; confirm.disabled = false; if (requiresAck) { const label = make('label', 'check-option modal-ack'); const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; label.append(checkbox, make('span', '', requiresAck)); bodyHost.append(label); confirm.disabled = true; checkbox.addEventListener('change', () => { confirm.disabled = !checkbox.checked; }); } state.modalAction = onConfirm; dialog.showModal(); (bodyHost.querySelector('input:not(:disabled), select:not(:disabled)') || $('#modal-cancel')).focus(); }
  function closeModal() { const dialog = $('#action-modal'); if (dialog?.open) dialog.close(); state.modalAction = null; if (state.modalTrigger?.isConnected) state.modalTrigger.focus({ preventScroll: true }); state.modalTrigger = null; }

  function bind() {
    $('#access-form')?.addEventListener('submit', signIn);
    $('#access-code')?.addEventListener('input', () => { if ($('#access-code').getAttribute('aria-invalid') === 'true') accessMessage('口令仅用于本次访问验证。'); });
    $('#access-retry')?.addEventListener('click', checkSession);
    $('#access-logout')?.addEventListener('click', signOut);
    window.addEventListener('pagehide', () => { $('#access-code').value = ''; });
    $$('[data-page]').forEach((button) => button.addEventListener('click', () => go(button.dataset.page)));
    $$('[data-go-page]').forEach((button) => button.addEventListener('click', () => go(button.dataset.goPage)));
    $('#mobile-menu')?.addEventListener('click', () => { const nav = $('#mobile-nav'); const open = nav.classList.toggle('is-open'); $('#mobile-menu').setAttribute('aria-expanded', String(open)); });
    $('#refresh-button')?.addEventListener('click', refreshAll); $('#sources-refresh')?.addEventListener('click', () => Promise.allSettled([loadSources(), loadStatus()])); $('#batches-refresh')?.addEventListener('click', () => loadBatches()); $('#retention-refresh')?.addEventListener('click', loadRetention); $('#settings-refresh')?.addEventListener('click', () => Promise.allSettled([loadSettings(), loadStatus()]));
    $('#copy-staging-path')?.addEventListener('click', copyStagingPath);
    $('#scan-source')?.addEventListener('change', (event) => { state.scan = { sourceId: event.target.value, scanId: '', revision: null, roots: ['.'], requestedRoots: ['.'], directories: [], files: [], complete: false }; renderScanIdentity(); renderScanSummary(); renderDirectoryPicker(); updateScanControls(); }); $('#scan-button')?.addEventListener('click', scanSource); $('#start-batch-button')?.addEventListener('click', startBatch); $('#cleanup-preview-button')?.addEventListener('click', cleanupPreview); $('#cleanup-execute-button')?.addEventListener('click', executeCleanup); $('#settings-form')?.addEventListener('submit', (event) => { event.preventDefault(); saveSettings(); });
    $('#action-modal')?.addEventListener('keydown', (event) => {
      if (event.key !== 'Tab') return;
      const nodes = $$('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href], [tabindex="0"]', event.currentTarget).filter((node) => node.getClientRects().length);
      const first = nodes[0]; const last = nodes[nodes.length - 1];
      if (!first) { event.preventDefault(); return; }
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    });
    $('#action-modal')?.addEventListener('cancel', (event) => { event.preventDefault(); closeModal(); });
    document.addEventListener('keydown', (event) => { if (event.key === 'Escape' && $('#mobile-nav')?.classList.contains('is-open')) { $('#mobile-nav').classList.remove('is-open'); $('#mobile-menu').setAttribute('aria-expanded', 'false'); $('#mobile-menu').focus(); } });
    $('#modal-close')?.addEventListener('click', closeModal); $('#modal-cancel')?.addEventListener('click', closeModal); $('#modal-confirm')?.addEventListener('click', async () => { const action = state.modalAction; closeModal(); if (!action) return; try { await action(); } catch (error) { notify(error.message, 'error'); } }); $('#action-modal')?.addEventListener('click', (event) => { if (event.target === event.currentTarget) closeModal(); });
  }
  function beginPolling() { stopPolling(); if (!state.appReady) return; let pending = false; let cycles = 0; state.polling = window.setInterval(async () => { if (document.hidden || !state.appReady || pending) return; pending = true; try { await loadBatches({ quiet: true }); cycles += 1; if (cycles % 5 === 0) await loadSources({ quiet: true }); } finally { pending = false; } }, 2000); }
  async function init() { bind(); await checkSession(); }
  document.addEventListener('DOMContentLoaded', init);
})();
