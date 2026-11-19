const ALL_SCOPES = ['admin', 'mcp:read', 'mcp:call', 'ws:connect', 'servers:read', 'tokens:read'];
const state = { user: null, section: 'overview', editingServer: null, editingBridge: null, editingToken: null, editingUser: null };

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[char]);
}

function toast(message, error = false) {
  const node = $('#toast');
  node.textContent = message;
  node.className = `toast${error ? ' error' : ''}`;
  clearTimeout(node._timer);
  node._timer = setTimeout(() => node.classList.add('hidden'), 3200);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: 'same-origin',
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) }
  });
  const text = await response.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch (_) { data = { detail: text }; }
  }
  if (!response.ok) {
    const error = new Error(data?.detail || `请求失败 (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return data;
}

function showLogin() {
  $('#loginView').classList.remove('hidden');
  $('#appView').classList.add('hidden');
}

function showApp(user) {
  state.user = user;
  $('#loginView').classList.add('hidden');
  $('#appView').classList.remove('hidden');
  $('#currentUser').textContent = user.username;
  $('#endpointSnippet').textContent =
    `POST  ${location.origin}/mcp\nGET   ${location.origin}/sse\nWS    ${location.origin.replace(/^http/, 'ws')}/ws/xiaozhi`;
  loadSection('overview');
}

function setSection(name) {
  state.section = name;
  $$('.section').forEach((section) => section.classList.toggle('active', section.id === `section-${name}`));
  $$('.nav-item').forEach((item) => item.classList.toggle('active', item.dataset.section === name));
  const titles = {
    overview: ['概览', '掌握网关运行状态'],
    servers: ['MCP 服务', '注册、测试与管理上游 MCP 服务'],
    bridges: ['小智云端', '配置多个小智云端 MCP WebSocket 桥接'],
    tokens: ['访问令牌', '为客户端分配最小权限'],
    users: ['用户管理', '管理后台登录账号'],
    settings: ['系统设置', '自定义网关行为与授权器'],
    audit: ['审计日志', '查看管理操作记录']
  };
  $('#pageTitle').textContent = titles[name][0];
  $('#pageSubtitle').textContent = titles[name][1];
}

function loadSection(name) {
  setSection(name);
  const loaders = {
    overview: loadOverview,
    servers: loadServers,
    bridges: loadBridges,
    tokens: loadTokens,
    users: loadUsers,
    settings: loadSettings,
    audit: loadAudit
  };
  loaders[name]?.().catch((error) => toast(error.message, true));
}

async function loadOverview() {
  const data = await api('/api/overview');
  const bridges = data.xiaozhiBridge || [];
  const connectedBridges = bridges.filter((item) => item.connected).length;
  const cards = [
    ['启用的服务', `${data.enabledServers}/${data.servers}`],
    ['聚合工具', data.tools],
    ['有效 Token', data.tokens],
    ['活跃连接', data.registry.activeClients],
    ['小智云端', `${connectedBridges}/${bridges.length} 已连接`],
    ['后台用户', data.users]
  ];
  $('#statsGrid').innerHTML = cards.map(([label, value]) =>
    `<div class="stat-card"><span>${label}</span><strong>${escapeHtml(value)}</strong></div>`).join('');
  $('#recentAudits').innerHTML = data.recentAudits.length
    ? data.recentAudits.map((item) => `<div class="timeline-item"><time>${escapeHtml(item.created_at)}</time><strong>${escapeHtml(item.actor)}</strong>${escapeHtml(item.action)}<div class="muted">${escapeHtml(item.target || '')}</div></div>`).join('')
    : '<p class="muted">暂无操作记录</p>';
}

function formatServices(value, names = []) {
  if (value === '*' || (Array.isArray(value) && value.includes('*'))) return '<span class="badge info">全部服务</span>';
  if (Array.isArray(value) && names.length) {
    return names.map((name) => `<span class="badge info">${escapeHtml(name)}</span>`).join(' ');
  }
  if (Array.isArray(value)) return value.map((id) => `<span class="badge info">#${escapeHtml(id)}</span>`).join(' ');
  return escapeHtml(value);
}

async function loadServers() {
  const rows = await api('/api/servers');
  $('#serversBody').innerHTML = rows.length ? rows.map((row) => {
    const tools = row.tools || [];
    const chips = tools.length
      ? tools.map((tool) => `<span class="tool-chip" title="${escapeHtml(tool.description || '')}">${escapeHtml(tool.displayName || tool.name)}</span>`).join('')
      : '<span class="muted">未发现能力</span>';
    return `<tr>
      <td><strong>${escapeHtml(row.name)}</strong><span class="sub">ID ${row.id}</span></td>
      <td><span class="badge info">${escapeHtml(row.transport)}</span></td>
      <td>${escapeHtml(row.transport === 'stdio' ? `${row.command} ${(row.args || []).join(' ')}` : row.url)}</td>
      <td><span class="badge ${row.enabled ? 'on' : 'off'}">${row.enabled ? '启用' : '停用'}</span></td>
      <td>
        <button class="link" data-action="toggle-server-capabilities" data-id="${row.id}">${Number(row.toolCount || 0)} 个能力</button>
        <div class="tool-list hidden" id="server-capabilities-${row.id}">${chips}</div>
      </td>
      <td>${escapeHtml(row.timeout)}s</td>
      <td><div class="row-actions">
        <button class="ghost" data-action="test-server" data-id="${row.id}">测试</button>
        <button class="ghost" data-action="server-tools" data-id="${row.id}">工具</button>
        <button class="ghost" data-action="edit-server" data-id="${row.id}">编辑</button>
        <button class="danger" data-action="delete-server" data-id="${row.id}">删除</button>
      </div></td>
    </tr>`;
  }).join('') : '<tr><td colspan="7" class="muted">尚未注册 MCP 服务</td></tr>';
  window._servers = rows;
}

async function loadBridges() {
  const rows = await api('/api/bridges');
  window._bridges = rows;
  $('#bridgesBody').innerHTML = rows.length ? rows.map((row) => {
    const statusClass = row.connected ? 'on' : (row.enabled ? 'warn' : 'off');
    const statusText = row.connected ? '已连接' : (row.enabled ? '重连中' : '未启用');
    const error = row.lastError ? String(row.lastError).slice(0, 60) : '-';
    return `<tr>
      <td><strong>${escapeHtml(row.name)}</strong><span class="sub">ID ${row.id}</span></td>
      <td><code title="${escapeHtml(row.displayUrl)}">${escapeHtml(row.displayUrl)}</code></td>
      <td><span class="badge ${statusClass}">${statusText}</span></td>
      <td><span class="badge ${row.enabled ? 'on' : 'off'}">${row.enabled ? '启用' : '停用'}</span></td>
      <td>${escapeHtml(error)}</td>
      <td><div class="row-actions">
        <button class="ghost" data-action="reconnect-bridge" data-id="${row.id}">重连</button>
        <button class="ghost" data-action="toggle-bridge" data-id="${row.id}" data-enabled="${row.enabled}">${row.enabled ? '停用' : '启用'}</button>
        <button class="ghost" data-action="edit-bridge" data-id="${row.id}">编辑</button>
        <button class="danger" data-action="delete-bridge" data-id="${row.id}">删除</button>
      </div></td>
    </tr>`;
  }).join('') : '<tr><td colspan="6" class="muted">尚未配置小智云端桥接</td></tr>';
}

async function loadTokens() {
  const rows = await api('/api/tokens');
  $('#tokensBody').innerHTML = rows.length ? rows.map((row) => `
    <tr>
      <td><strong>${escapeHtml(row.name)}</strong></td>
      <td><code>${escapeHtml(row.prefix)}</code></td>
      <td>${(row.scopes || []).map((scope) => `<span class="scope">${escapeHtml(scope)}</span>`).join('')}</td>
      <td>${formatServices(row.allowed_services, row.allowed_service_names || [])}</td>
      <td><span class="badge ${row.is_active ? 'on' : 'off'}">${row.is_active ? '有效' : '已停用'}</span></td>
      <td>${escapeHtml(row.last_used_at || '从未使用')}</td>
      <td><div class="row-actions">
        <button class="ghost" data-action="copy-token-prefix" data-id="${row.id}">复制前缀</button>
        <button class="ghost" data-action="regenerate-token" data-id="${row.id}">重新生成</button>
        <button class="ghost" data-action="toggle-token" data-id="${row.id}" data-active="${row.is_active}">${row.is_active ? '停用' : '启用'}</button>
        <button class="ghost" data-action="edit-token" data-id="${row.id}">编辑</button>
        <button class="danger" data-action="delete-token" data-id="${row.id}">删除</button>
      </div></td>
    </tr>`).join('') : '<tr><td colspan="7" class="muted">尚未创建 Token</td></tr>';
  window._tokens = rows;
}

async function loadUsers() {
  const rows = await api('/api/users');
  $('#usersBody').innerHTML = rows.map((row) => `
    <tr>
      <td><strong>${escapeHtml(row.username)}</strong></td>
      <td><span class="badge info">${escapeHtml(row.role)}</span></td>
      <td><span class="badge ${row.is_active ? 'on' : 'off'}">${row.is_active ? '正常' : '停用'}</span></td>
      <td>${escapeHtml(row.created_at)}</td>
      <td><div class="row-actions">
        <button class="ghost" data-action="edit-user" data-id="${row.id}">编辑</button>
        ${row.username === state.user.username ? '' : `<button class="danger" data-action="delete-user" data-id="${row.id}">删除</button>`}
      </div></td>
    </tr>`).join('');
  window._users = rows;
}

async function loadSettings() {
  const data = await api('/api/settings');
  const s = data.settings;
  $('#settingsForm').innerHTML = `
    <div class="form-grid">
      <label>网关名称<input name="server_name" value="${escapeHtml(s.server_name || '')}" /></label>
      <label>请求超时（秒）<input name="request_timeout" type="number" min="1" max="600" value="${escapeHtml(s.request_timeout || 30)}" /></label>
      <label>工具名分隔符<input name="tool_name_separator" value="${escapeHtml(s.tool_name_separator || '__')}" /></label>
      <label>工具缓存时间（秒）<input name="tool_cache_ttl" type="number" min="0" max="3600" value="${escapeHtml(s.tool_cache_ttl || 60)}" /></label>
      <label class="full">WebSocket 欢迎语<input name="xiaozhi_welcome" value="${escapeHtml(s.xiaozhi_welcome || '')}" /></label>
      <label class="full">自定义 Authorizer<span class="form-help">格式 module:Class，实现 authorize(context)；返回 False 拒绝。留空则使用内置 scopes + 服务白名单。</span><input name="auth_authorizer" value="${escapeHtml(s.auth_authorizer || '')}" placeholder="examples.custom_authorizer:AdvancedAuthorizer" /></label>
    </div>
    <div class="form-actions"><button class="primary" type="submit">保存设置</button></div>`;
}

async function loadAudit() {
  const rows = await api('/api/audit');
  $('#auditBody').innerHTML = rows.length ? rows.map((row) => `
    <tr><td>${escapeHtml(row.created_at)}</td><td>${escapeHtml(row.actor)}</td><td>${escapeHtml(row.action)}</td><td>${escapeHtml(row.target || '-')}</td><td>${escapeHtml(row.detail || '-')}</td></tr>`
  ).join('') : '<tr><td colspan="5" class="muted">暂无记录</td></tr>';
}

function openModal(title, html) {
  $('#modalTitle').textContent = title;
  $('#modalBody').innerHTML = html;
  $('#modalBackdrop').classList.remove('hidden');
}

function closeModal() {
  $('#modalBackdrop').classList.add('hidden');
  $('#modalBody').innerHTML = '';
}

const selected = (value, current) => value === current ? 'selected' : '';

function serverModal(row = null) {
  state.editingServer = row;
  const transport = row?.transport || 'stdio';
  openModal(row ? `编辑服务：${row.name}` : '注册 MCP 服务', `
    <form id="serverForm" class="form-grid">
      <label>服务名称<input name="name" required value="${escapeHtml(row?.name || '')}" /></label>
      <label>传输方式<select name="transport" id="transportSelect">
        <option value="stdio" ${selected('stdio', transport)}>本地 stdio</option>
        <option value="http" ${selected('http', transport)}>HTTP Streamable</option>
        <option value="sse" ${selected('sse', transport)}>HTTP SSE</option>
      </select></label>
      <label class="stdio-only">启动命令<input name="command" value="${escapeHtml(row?.command || '')}" placeholder="python 或 npx" /></label>
      <label class="stdio-only">工作目录<input name="cwd" value="${escapeHtml(row?.cwd || '')}" /></label>
      <label class="stdio-only full">启动参数（JSON 数组）<textarea name="args">${escapeHtml(JSON.stringify(row?.args || [], null, 2))}</textarea></label>
      <label class="stdio-only full">环境变量（JSON 对象）<textarea name="env">${escapeHtml(JSON.stringify(row?.env || {}, null, 2))}</textarea></label>
      <label class="http-only full">服务 URL<input name="url" value="${escapeHtml(row?.url || '')}" placeholder="http://127.0.0.1:8000/mcp" /></label>
      <label class="http-only full">请求头（JSON 对象）<textarea name="headers">${escapeHtml(JSON.stringify(row?.headers || {}, null, 2))}</textarea></label>
      <label>超时（秒）<input name="timeout" type="number" min="1" max="600" value="${escapeHtml(row?.timeout || 30)}" /></label>
      <label style="display:flex;align-items:center;gap:8px"><input type="checkbox" name="enabled" ${row?.enabled === false ? '' : 'checked'} style="width:auto" /> 启用服务</label>
      <div class="form-actions full"><button type="button" class="ghost" data-action="close-modal">取消</button><button class="primary" type="submit">保存</button></div>
    </form>`);
  const toggleTransport = () => {
    const isStdio = $('#transportSelect').value === 'stdio';
    $$('.stdio-only').forEach((node) => node.classList.toggle('hidden', !isStdio));
    $$('.http-only').forEach((node) => node.classList.toggle('hidden', isStdio));
  };
  $('#transportSelect').addEventListener('change', toggleTransport);
  toggleTransport();
  $('#serverForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = new FormData(event.target);
    const parseJson = (key, fallback) => {
      try { const value = JSON.parse(form.get(key) || ''); return value ?? fallback; } catch (_) { throw new Error(`${key} 必须是合法 JSON`); }
    };
    const payload = {
      name: form.get('name').trim(),
      transport: form.get('transport'),
      command: form.get('command') || null,
      cwd: form.get('cwd') || null,
      args: parseJson('args', []),
      env: parseJson('env', {}),
      url: form.get('url') || null,
      headers: parseJson('headers', {}),
      timeout: Number(form.get('timeout') || 30),
      enabled: form.has('enabled')
    };
    try {
      if (state.editingServer) await api(`/api/servers/${state.editingServer.id}`, { method: 'PUT', body: JSON.stringify(payload) });
      else await api('/api/servers', { method: 'POST', body: JSON.stringify(payload) });
      closeModal(); loadServers(); toast('服务已保存');
    } catch (error) { toast(error.message, true); }
  });
}

function bridgeModal(row = null) {
  state.editingBridge = row;
  openModal(row ? `编辑桥接：${row.name}` : '新增小智云端桥接', `
    <form id="bridgeForm" class="form-grid">
      <label class="full">桥接名称<input name="name" required value="${escapeHtml(row?.name || '')}" placeholder="例如 我的小智助手" /></label>
      <label class="full">WebSocket 地址<span class="form-help">以 ws:// 或 wss:// 开头。建议用 \${XIAOZHI_MCP_TOKEN} 占位符代替明文 token。</span><input name="url" required value="${escapeHtml(row?.url || '')}" placeholder="wss://api.xiaozhi.me/mcp/?token=\${XIAOZHI_MCP_TOKEN}" /></label>
      <label style="display:flex;align-items:center;gap:8px"><input type="checkbox" name="enabled" style="width:auto" ${row?.enabled === false ? '' : 'checked'} /> 启用桥接</label>
      <div class="form-actions full"><button type="button" class="ghost" data-action="close-modal">取消</button><button class="primary" type="submit">保存</button></div>
    </form>`);
  $('#bridgeForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = new FormData(event.target);
    const payload = {
      name: form.get('name').trim(),
      url: form.get('url').trim(),
      enabled: form.has('enabled')
    };
    try {
      if (state.editingBridge) await api(`/api/bridges/${state.editingBridge.id}`, { method: 'PUT', body: JSON.stringify(payload) });
      else await api('/api/bridges', { method: 'POST', body: JSON.stringify(payload) });
      closeModal(); loadBridges(); toast('桥接已保存');
    } catch (error) { toast(error.message, true); }
  });
}

async function tokenModal(row = null) {
  state.editingToken = row;
  const scopes = row?.scopes || ['mcp:read', 'mcp:call'];
  let services = [];
  try { services = await api('/api/servers'); } catch (error) { toast(error.message, true); }

  const allowedRaw = row?.allowed_services ?? '*';
  const isAll = allowedRaw === '*' || (Array.isArray(allowedRaw) && allowedRaw.includes('*'));
  const allowedIds = new Set(
    Array.isArray(allowedRaw)
      ? allowedRaw.map(String)
      : String(allowedRaw).split(',').map((item) => item.trim()).filter(Boolean)
  );
  const knownIds = new Set(services.map((item) => String(item.id)));
  const serviceOptions = services.map((item) => `
    <label><input type="checkbox" name="allowed_service" value="${item.id}" ${allowedIds.has(String(item.id)) ? 'checked' : ''}/> ${escapeHtml(item.name)}</label>
  `).join('');
  const unknownIds = [...allowedIds].filter((id) => id !== '*' && !knownIds.has(id));
  const unknownOptions = unknownIds.map((id) => `
    <label><input type="checkbox" name="allowed_service" value="${escapeHtml(id)}" checked/> #${escapeHtml(id)}（服务已删除）</label>
  `).join('');

  openModal(row ? `编辑 Token：${row.name}` : '创建 API Token', `
    <form id="tokenForm" class="form-grid">
      <label class="full">名称<input name="name" required value="${escapeHtml(row?.name || '')}" /></label>
      <div class="full checkbox-row">${ALL_SCOPES.map((scope) => `<label><input type="checkbox" name="scopes" value="${scope}" ${scopes.includes(scope) ? 'checked' : ''}/> ${scope}</label>`).join('')}</div>
      <div class="full">
        <label style="font-size:13px;font-weight:600">可访问的 MCP 服务</label>
        <div class="checkbox-row" id="tokenServices" style="margin-top:8px">
          <label><input type="checkbox" name="allowed_all" value="*" ${isAll ? 'checked' : ''}/> 全部服务</label>
          ${serviceOptions || '<span class="muted">暂无已注册服务</span>'}
          ${unknownOptions}
        </div>
        <div class="form-help" style="margin-top:6px">勾选“全部服务”可访问当前及后续新增的所有服务；否则只可访问勾选的服务。</div>
      </div>
      ${row ? '' : '<label class="full">有效期（天，留空表示永久）<input name="expires_in_days" type="number" min="1" max="3650" /></label>'}
      <div class="form-actions full"><button type="button" class="ghost" data-action="close-modal">取消</button><button class="primary" type="submit">保存</button></div>
    </form>`);

  const allBox = $('input[name="allowed_all"]');
  const serviceBoxes = $$('input[name="allowed_service"]');
  const syncServiceBoxes = () => {
    serviceBoxes.forEach((box) => {
      box.disabled = allBox.checked;
      if (allBox.checked) box.checked = true;
    });
  };
  allBox.addEventListener('change', syncServiceBoxes);
  serviceBoxes.forEach((box) => box.addEventListener('change', () => {
    if (box.checked && allBox.checked) allBox.checked = false;
    syncServiceBoxes();
  }));
  syncServiceBoxes();

  $('#tokenForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = new FormData(event.target);
    const allServices = form.has('allowed_all');
    const selected = form.getAll('allowed_service');
    if (!allServices && selected.length === 0) {
      toast('请至少选择一个服务，或勾选“全部服务”', true);
      return;
    }
    const payload = {
      name: form.get('name').trim(),
      scopes: form.getAll('scopes'),
      allowed_services: allServices ? '*' : selected
    };
    if (!state.editingToken && form.get('expires_in_days')) payload.expires_in_days = Number(form.get('expires_in_days'));
    try {
      const result = state.editingToken
        ? await api(`/api/tokens/${state.editingToken.id}`, { method: 'PUT', body: JSON.stringify(payload) })
        : await api('/api/tokens', { method: 'POST', body: JSON.stringify(payload) });
      if (result.token) {
        $('#modalBody').innerHTML = `<h3>Token 已创建</h3><p class="muted">请立即复制并妥善保存，关闭后无法再次查看明文。如果忘记，可以在列表里“重新生成”。</p><div class="token-result" id="newToken">${escapeHtml(result.token)}</div><div class="form-actions"><button class="ghost" data-action="close-modal">完成</button><button class="primary" data-action="copy-token">复制 Token</button></div>`;
      } else {
        closeModal();
      }
      loadTokens(); toast('Token 已保存');
    } catch (error) { toast(error.message, true); }
  });
}

function userModal(row = null) {
  state.editingUser = row;
  openModal(row ? `编辑用户：${row.username}` : '新增用户', `
    <form id="userForm" class="form-grid">
      <label>用户名<input name="username" required value="${escapeHtml(row?.username || '')}" ${row ? 'disabled' : ''} /></label>
      <label>角色<input name="role" value="${escapeHtml(row?.role || 'admin')}" /></label>
      <label class="full">${row ? '新密码（留空表示不修改）' : '密码'}<input name="password" type="password" ${row ? '' : 'required minlength="6"'} /></label>
      ${row ? `<label style="display:flex;align-items:center;gap:8px"><input type="checkbox" name="is_active" style="width:auto" ${row.is_active ? 'checked' : ''}/> 启用账号</label>` : ''}
      <div class="form-actions full"><button type="button" class="ghost" data-action="close-modal">取消</button><button class="primary" type="submit">保存</button></div>
    </form>`);
  $('#userForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = new FormData(event.target);
    const payload = { username: form.get('username'), role: form.get('role') || 'admin', password: form.get('password') };
    if (row) payload.is_active = form.has('is_active');
    try {
      if (row) await api(`/api/users/${row.id}`, { method: 'PUT', body: JSON.stringify(payload) });
      else await api('/api/users', { method: 'POST', body: JSON.stringify(payload) });
      closeModal(); loadUsers(); toast('用户已保存');
    } catch (error) { toast(error.message, true); }
  });
}

async function copyText(value) {
  if (!value) {
    toast('没有可复制的内容', true);
    return;
  }
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(value);
      toast('已复制到剪贴板');
      return;
    }
  } catch (_) {
    // 继续尝试兼容模式
  }
  try {
    const textarea = document.createElement('textarea');
    textarea.value = value;
    textarea.setAttribute('readonly', '');
    textarea.style.position = 'fixed';
    textarea.style.top = '-1000px';
    textarea.style.opacity = '0';
    document.body.appendChild(textarea);
    textarea.focus();
    textarea.select();
    textarea.setSelectionRange(0, value.length);
    const ok = document.execCommand('copy');
    document.body.removeChild(textarea);
    toast(ok ? '已复制到剪贴板' : '复制失败，请手动复制', !ok);
  } catch (_) {
    toast('复制失败，请手动复制', true);
  }
}

document.addEventListener('click', async (event) => {
  const nav = event.target.closest('[data-section], [data-section-link]');
  if (nav) { loadSection(nav.dataset.section || nav.dataset.sectionLink); return; }
  const action = event.target.closest('[data-action]')?.dataset.action;
  const id = Number(event.target.closest('[data-id]')?.dataset.id || 0);
  if (!action) return;
  if (action === 'close-modal') return closeModal();
  if (action === 'copy-endpoint') return copyText(`${location.origin}/mcp`);
  if (action === 'copy-token') return copyText($('#newToken')?.textContent || '');
  if (action === 'edit-server') return serverModal(window._servers.find((item) => item.id === id));
  if (action === 'edit-bridge') return bridgeModal(window._bridges.find((item) => item.id === id));
  if (action === 'edit-token') return await tokenModal(window._tokens.find((item) => item.id === id));
  if (action === 'edit-user') return userModal(window._users.find((item) => item.id === id));
  if (action === 'delete-server' && confirm('确认删除该 MCP 服务？')) {
    await api(`/api/servers/${id}`, { method: 'DELETE' }); loadServers(); toast('已删除');
  }
  if (action === 'delete-bridge' && confirm('确认删除该小智云端桥接？')) {
    await api(`/api/bridges/${id}`, { method: 'DELETE' }); loadBridges(); toast('已删除');
  }
  if (action === 'toggle-bridge') {
    const row = window._bridges.find((item) => item.id === id);
    await api(`/api/bridges/${id}`, { method: 'PUT', body: JSON.stringify({ enabled: event.target.dataset.enabled !== 'true' }) });
    loadBridges(); toast(row?.enabled ? '桥接已停用' : '桥接已启用');
  }
  if (action === 'reconnect-bridge') {
    toast('正在重连...');
    await api(`/api/bridges/${id}/reconnect`, { method: 'POST' });
    loadBridges(); toast('已触发重连');
  }
  if (action === 'copy-token-prefix') {
    const row = window._tokens.find((item) => item.id === id);
    return copyText(row?.prefixFull || row?.prefix || '');
  }
  if (action === 'regenerate-token' && confirm('重新生成后，旧 Token 会立即失效。确认继续？')) {
    const result = await api(`/api/tokens/${id}/regenerate`, { method: 'POST' });
    openModal('Token 已重新生成', `
      <p class="muted">旧 Token 已立即失效。请立即复制新 Token，关闭后无法再次查看明文。</p>
      <div class="token-result" id="newToken">${escapeHtml(result.token)}</div>
      <div class="form-actions"><button class="ghost" data-action="close-modal">完成</button><button class="primary" data-action="copy-token">复制 Token</button></div>`);
    loadTokens(); toast('新 Token 已生成');
  }
  if (action === 'delete-token' && confirm('确认删除该 Token？')) {
    await api(`/api/tokens/${id}`, { method: 'DELETE' }); loadTokens(); toast('已删除');
  }
  if (action === 'delete-user' && confirm('确认删除该用户？')) {
    await api(`/api/users/${id}`, { method: 'DELETE' }); loadUsers(); toast('已删除');
  }
  if (action === 'toggle-token') {
    await api(`/api/tokens/${id}`, { method: 'PUT', body: JSON.stringify({ is_active: event.target.dataset.active !== 'true' }) });
    loadTokens(); toast('状态已更新');
  }
  if (action === 'toggle-server-capabilities') {
    const panel = document.getElementById(`server-capabilities-${id}`);
    if (panel) panel.classList.toggle('hidden');
  }
  if (action === 'test-server') {
    toast('正在测试服务连接...');
    const result = await api(`/api/servers/${id}/test`, { method: 'POST' });
    toast(result.ok ? `连接成功，发现 ${result.tool_count} 个工具（${result.latency_ms}ms）` : `连接失败：${result.error}`, !result.ok);
  }
  if (action === 'server-tools') {
    const result = await api(`/api/servers/${id}/tools`);
    openModal('工具列表', `<pre>${escapeHtml(JSON.stringify(result.tools, null, 2))}</pre><div class="form-actions"><button class="ghost" data-action="close-modal">关闭</button></div>`);
  }
});

document.addEventListener('DOMContentLoaded', () => {
  $('#loginForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    $('#loginError').textContent = '';
    try {
      const user = await api('/api/auth/login', {
        method: 'POST',
        body: JSON.stringify({ username: $('#loginUsername').value, password: $('#loginPassword').value })
      });
      showApp(user);
    } catch (error) { $('#loginError').textContent = error.message; }
  });
  $('#logoutBtn').addEventListener('click', async () => {
    await api('/api/auth/logout', { method: 'POST' });
    state.user = null; showLogin();
  });
  $('#modalClose').addEventListener('click', closeModal);
  $('#modalBackdrop').addEventListener('click', (event) => { if (event.target.id === 'modalBackdrop') closeModal(); });
  $('#addServerBtn').addEventListener('click', () => serverModal());
  $('#addBridgeBtn').addEventListener('click', () => bridgeModal());
  $('#addTokenBtn').addEventListener('click', () => tokenModal());
  $('#addUserBtn').addEventListener('click', () => userModal());
  $('#refreshToolsBtn').addEventListener('click', async () => {
    const result = await api('/api/servers/refresh-tools', { method: 'POST' });
    toast(`工具缓存已刷新，共 ${result.tools} 个工具`);
    loadServers();
  });
  $('#settingsForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = new FormData(event.target);
    const values = {};
    ['server_name', 'auth_authorizer', 'tool_name_separator', 'xiaozhi_welcome'].forEach((key) => values[key] = String(form.get(key) || ''));
    ['request_timeout', 'tool_cache_ttl'].forEach((key) => values[key] = String(Number(form.get(key) || 0)));
    try {
      const result = await api('/api/settings', { method: 'PUT', body: JSON.stringify({ values }) });
      toast(result.xiaozhiBridge?.connected ? '设置已保存，小智云端已连接' : '设置已保存');
      loadSettings();
    } catch (error) { toast(error.message, true); }
  });
  api('/api/auth/me').then(showApp).catch(showLogin);
});
