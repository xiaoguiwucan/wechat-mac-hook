const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const state = { config: null, status: null, channels: [], selectedChannelId: '', channelHealth: {}, groupCatalog: [], dirty: false, logs: [], source: 'all', miniSource: 'all', paused: false, eventSource: null };
const pageMeta = {
  overview: ['运行总览', '第二微信、OneBot 与 AI 服务'], ai: ['AI 模型', '模型接口与生成参数'],
  groups: ['群聊策略', '目标群与自动回复规则'], tests: ['测试中心', '验证模型、Hook 与完整回调链路'], logs: ['实时日志', 'AI 回复与 OneBot 运行输出']
};

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...options });
  const payload = await response.json();
  if (!response.ok || !payload.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload.data;
}

function toast(message, type = 'success', timeout = 4200) {
  const el = document.createElement('div'); el.className = `toast ${type}`; el.textContent = message;
  $('#toastStack').append(el); setTimeout(() => el.remove(), timeout);
}

function setBusy(button, busy, label = '') {
  if (busy) {
    button.dataset.originalHtml = button.innerHTML; button.disabled = true;
    const stepLabel = $('b', button); if (stepLabel) stepLabel.textContent = '运行中'; else button.textContent = label || '处理中…';
  } else {
    button.disabled = false; if (button.dataset.originalHtml) button.innerHTML = button.dataset.originalHtml;
  }
}

function showPage(name) {
  $$('.nav-item').forEach(x => x.classList.toggle('active', x.dataset.page === name));
  $$('.page').forEach(x => x.classList.toggle('active', x.id === `page-${name}`));
  $('#pageTitle').textContent = pageMeta[name][0]; $('#pageSubtitle').textContent = pageMeta[name][1];
  window.scrollTo(0, 0);
  if (name === 'logs') requestAnimationFrame(() => { const out = $('#logOutput'); out.scrollTop = out.scrollHeight; });
}

function renderStatus(data) {
  state.status = data;
  for (const name of ['wechat2', 'onebot', 'ai']) {
    const item = data[name], card = $(`.status-card[data-service="${name}"]`);
    const degraded = item.running && item.port && !item.port_open;
    card.classList.toggle('running', item.running && !degraded); card.classList.toggle('degraded', degraded); card.classList.toggle('stopped', !item.running);
    $('.state-text', card).textContent = degraded ? 'DEGRADED' : item.running ? 'RUNNING' : 'STOPPED';
    const pid = $('.pid', card); if (pid) pid.textContent = item.pid || '--';
    const uptime = $('.uptime', card); if (uptime) uptime.textContent = item.uptime || '--';
  }
  $('.hook', $('[data-service="onebot"]')).textContent = data.onebot.hook_ready ? (data.onebot.send_ready ? '通道就绪' : '已附加 / 待唤醒') : '未就绪';
  $('.configured', $('[data-service="ai"]')).textContent = data.ai.configured ? '已配置' : '缺少 Key';
  const all = data.wechat2.running && data.onebot.running && data.onebot.port_open && data.ai.running && data.ai.port_open;
  const chip = $('#healthChip'); chip.textContent = all ? '全部在线' : '需要处理'; chip.className = `health-chip ${all ? 'good' : 'warn'}`;
  $('#testHealth').textContent = all ? '服务链路在线' : '部分服务离线'; $('#testHealth').className = `health-chip ${all ? 'good' : 'warn'}`;
  $('#syncTime').textContent = `${data.time} 已同步`;
  $('#isolationPath').textContent = `${data.isolation.app} · ${data.isolation.bundle_id}`;
}

async function refreshStatus(silent = false) {
  try { renderStatus(await api('/api/status')); }
  catch (e) { if (!silent) toast(`状态获取失败：${e.message}`, 'error'); }
}

function providerKey(raw, base) {
  if (raw === 'deepseek' || base.includes('deepseek')) return 'deepseek';
  if (raw === 'openai' || base.includes('api.openai.com')) return 'openai';
  if (raw === 'openrouter' || base.includes('openrouter')) return 'openrouter';
  if (raw === 'local' || base.includes('127.0.0.1') || base.includes('localhost')) return 'local';
  return 'compatible';
}

function applyProviderPreset(key, target = 'current') {
  const preset = state.config?.presets[key]; if (!preset) return;
  const base = target === 'current' ? $('#baseUrl') : $('#newBaseUrl');
  const model = target === 'current' ? $('#model') : $('#newModel');
  if (preset.base_url) base.value = preset.base_url; if (preset.model) model.value = preset.model;
  if (target === 'current') markDirty();
}

function fillConfig(c) {
  state.config = c;
  state.channels = c.channels.map(x => ({ ...x })); state.selectedChannelId = c.active_channel_id || state.channels[0]?.id || '';
  $('#temperature').value = c.temperature; $('#temperatureValue').value = c.temperature;
  $('#maxTokens').value = c.max_tokens; $('#systemPrompt').value = c.system_prompt;
  $('#promptCount').textContent = c.system_prompt.length; $('#personality').value = c.personality || ''; $('#personalityCount').textContent = (c.personality || '').length;
  $('#autoFailover').checked = c.auto_failover; $('#autoHealthCheck').checked = c.auto_health_check; $('#healthInterval').value = c.health_check_interval_seconds;
  $('#enabled').checked = c.enabled; $('#requireKeyword').checked = c.require_keyword; $('#ignoreSelf').checked = c.ignore_self_messages; $('#dryRun').checked = c.dry_run;
  $('#replyPrefix').value = c.reply_prefix; $('#keywords').value = c.trigger_keywords.join('，'); $('#cooldown').value = c.cooldown;
  $('#maxReplyChars').value = c.max_reply_chars; $('#maxContext').value = c.max_context_messages;
  renderChannelSelect(); fillChannelForm(); renderGroups(c.target_groups); updateRouteSummary(); state.dirty = false; $('#saveState').textContent = `SYNCED · ${c.revision}`;
  refreshChannelHealth(true);
}

function currentChannel() { return state.channels.find(x => x.id === state.selectedChannelId); }
function renderChannelSelect() {
  $('#channelSelect').innerHTML = state.channels.map(x => `<option value="${escapeHtml(x.id)}">${escapeHtml(x.name)}${x.enabled ? '' : '（已停用）'}</option>`).join('');
  $('#channelSelect').value = state.selectedChannelId;
  renderChannelHealth();
}
function fillChannelForm() {
  const c = currentChannel(); if (!c) return;
  $('#channelName').value = c.name; $('#channelProvider').value = providerKey(c.provider, c.base_url);
  $('#baseUrl').value = c.base_url; $('#apiKey').value = c.api_key || ''; $('#model').value = c.model;
  $('#timeout').value = c.timeout_seconds; $('#channelEnabled').checked = c.enabled;
}
function syncChannelForm() {
  const c = currentChannel(); if (!c) return;
  c.name = $('#channelName').value.trim(); c.provider = $('#channelProvider').value; c.base_url = $('#baseUrl').value.trim();
  c.api_key = $('#apiKey').value.trim(); c.model = $('#model').value.trim(); c.timeout_seconds = Number($('#timeout').value); c.enabled = $('#channelEnabled').checked;
}
function selectChannel(id) {
  if (id === state.selectedChannelId) return;
  syncChannelForm(); state.selectedChannelId = id; fillChannelForm(); renderChannelSelect(); markDirty();
}
function healthLabel(status) { return status === 'healthy' ? '畅通' : status === 'unhealthy' ? '故障' : status === 'testing' ? '检测中' : '未检测'; }
function renderChannelHealth() {
  $('#channelStatusList').innerHTML = state.channels.map(c => {
    const h = state.channelHealth[c.id] || { status: 'unknown' };
    const meta = h.status === 'healthy' ? `${h.latency_ms} ms` : h.status === 'unhealthy' ? h.error || '连接失败' : h.status === 'testing' ? '正在验证 Chat Completions' : '等待健康检测';
    return `<button type="button" class="channel-health-item ${c.id === state.selectedChannelId ? 'active' : ''}" data-channel-id="${escapeHtml(c.id)}"><i class="${h.status}"></i><span><strong>${escapeHtml(c.name)}</strong><small>${escapeHtml(meta)}</small></span><b>${healthLabel(h.status)}</b></button>`;
  }).join('');
  $$('.channel-health-item').forEach(x => x.onclick = () => selectChannel(x.dataset.channelId));
}
async function refreshChannelHealth(silent = false) {
  try {
    const data = await api('/api/channels/health'); state.channelHealth = { ...state.channelHealth, ...Object.fromEntries(data.channels.map(x => [x.id, x])) }; renderChannelHealth();
  } catch (e) { if (!silent) toast(`渠道状态获取失败：${e.message}`, 'error'); }
}

function renderGroups(groups) {
  state.groupCatalog = groups.map(x => ({ id: x.id, name: x.name || x.id, selected: true, source: 'config', last_seen: '' }));
  renderGroupPermissions(); loadDiscoveredGroups(true);
}
function mergeGroupCatalog(groups) {
  const existing = new Map(state.groupCatalog.map(x => [x.id, x]));
  groups.forEach(item => {
    const old = existing.get(item.id);
    existing.set(item.id, old ? { ...item, name: old.name && old.name !== old.id ? old.name : item.name, selected: old.selected } : { ...item });
  });
  state.groupCatalog = [...existing.values()].sort((a, b) => Number(b.selected) - Number(a.selected) || a.name.localeCompare(b.name, 'zh-CN'));
}
function renderGroupPermissions() {
  $('#groupList').innerHTML = state.groupCatalog.map(group => `<div class="group-permission-row" data-group-id="${escapeHtml(group.id)}"><label class="permission-check"><input class="group-enabled" type="checkbox" ${group.selected ? 'checked' : ''}><i></i><span>${group.selected ? '已授权' : '未授权'}</span></label><div class="field"><input class="group-name" value="${escapeHtml(group.name)}" aria-label="群聊名称"></div><code>${escapeHtml(group.id)}</code><span class="discovery-source" title="${escapeHtml(group.preview || '')}"><i></i><span>${group.last_seen ? '已发现' : '已配置'}${group.preview ? `<small>${escapeHtml(group.preview)}</small>` : ''}</span></span></div>`).join('');
  $$('.group-permission-row').forEach(row => {
    const group = state.groupCatalog.find(x => x.id === row.dataset.groupId);
    $('.group-enabled', row).onchange = e => { group.selected = e.target.checked; $('.permission-check span', row).textContent = group.selected ? '已授权' : '未授权'; updateGroupCount(); updateTestGroups(); markDirty(); };
    $('.group-name', row).oninput = e => { group.name = e.target.value.trim() || group.id; updateTestGroups(); markDirty(); };
  });
  $('#groupQuickSelect').innerHTML = state.groupCatalog.map(x => `<option value="${escapeHtml(x.id)}">${escapeHtml(x.name)}${x.preview ? ` · ${escapeHtml(x.preview)}` : ''} · ${escapeHtml(x.id)}</option>`).join('');
  updateGroupCount(); updateTestGroups();
}
async function loadDiscoveredGroups(silent = false) {
  $('#groupDiscoveryState').textContent = '正在读取第二微信群目录…';
  try {
    const data = await api('/api/groups/discover'); mergeGroupCatalog(data.groups); renderGroupPermissions();
    $('#groupDiscoveryState').textContent = `已发现 ${data.count} 个群聊`;
  } catch (e) { $('#groupDiscoveryState').textContent = '群目录获取失败'; if (!silent) toast(`群列表获取失败：${e.message}`, 'error'); }
}
function enableQuickSelectedGroup() {
  const group = state.groupCatalog.find(x => x.id === $('#groupQuickSelect').value); if (!group) return;
  group.selected = true; renderGroupPermissions(); markDirty();
}
function groupsData() { return state.groupCatalog.filter(x => x.selected && x.id.endsWith('@chatroom')).map(x => ({ name: x.name || x.id, id: x.id })); }
function updateGroupCount() { $('#groupCount').textContent = groupsData().length; }
function updateRouteSummary() {
  if (!state.config) return;
  $('#routeMode').textContent = $('#requireKeyword').checked ? '关键词' : '全消息';
  $('#routePrefix').textContent = $('#replyPrefix').value || '无';
  $('#routeCooldown').textContent = `${$('#cooldown').value || 0}s`;
}
function updateTestGroups() {
  const select = $('#testGroup'), old = select.value; select.innerHTML = groupsData().filter(x => x.id).map(x => `<option value="${escapeHtml(x.id)}">${escapeHtml(x.name)} · ${escapeHtml(x.id)}</option>`).join('');
  if ([...select.options].some(x => x.value === old)) select.value = old;
}
function escapeHtml(v) { const d = document.createElement('div'); d.textContent = v; return d.innerHTML; }

function collectConfig() {
  syncChannelForm();
  const active = currentChannel() || state.channels[0];
  return {
    revision: state.config.revision,
    provider: active.provider, base_url: active.base_url, api_key: active.api_key, model: active.model,
    channels: state.channels, active_channel_id: state.selectedChannelId,
    auto_failover: $('#autoFailover').checked, auto_health_check: $('#autoHealthCheck').checked, health_check_interval_seconds: Number($('#healthInterval').value),
    temperature: Number($('#temperature').value), max_tokens: Number($('#maxTokens').value), timeout_seconds: Number($('#timeout').value), system_prompt: $('#systemPrompt').value.trim(), personality: $('#personality').value.trim(),
    enabled: $('#enabled').checked, require_keyword: $('#requireKeyword').checked, ignore_self_messages: $('#ignoreSelf').checked, dry_run: $('#dryRun').checked,
    reply_prefix: $('#replyPrefix').value, trigger_keywords: $('#keywords').value.split(/[,，]/).map(x => x.trim()).filter(Boolean), cooldown: Number($('#cooldown').value),
    max_reply_chars: Number($('#maxReplyChars').value), max_context_messages: Number($('#maxContext').value), onebot_api: state.config.onebot_api, target_groups: groupsData()
  };
}

function markDirty() { state.dirty = true; $('#saveState').textContent = 'UNSAVED CHANGES'; }
async function saveConfig(button) {
  if (!$('#aiForm').reportValidity() || !$('#groupForm').reportValidity()) return false;
  setBusy(button, true, '保存中…');
  try { fillConfig(await api('/api/config', { method: 'POST', body: JSON.stringify(collectConfig()) })); toast('配置已保存并生效'); return true; }
  catch (e) { toast(`保存失败：${e.message}`, 'error', 7000); return false; }
  finally { setBusy(button, false); }
}

async function action(name, button) {
  if (name === 'start_all' && state.dirty && !await saveConfig(button)) return;
  setBusy(button, true);
  try { const result = await api(`/api/action/${name}`, { method: 'POST', body: '{}' }); renderStatus(result.status); toast('操作完成'); }
  catch (e) { toast(`操作失败：${e.message}`, 'error', 9000); }
  finally { setBusy(button, false); setTimeout(() => refreshStatus(true), 800); }
}

function connectionBody() { return { base_url: $('#baseUrl').value.trim(), api_key: $('#apiKey').value.trim(), model: $('#model').value.trim(), timeout_seconds: Number($('#timeout').value) }; }
async function getModels(button) {
  setBusy(button, true, '获取中');
  try { const r = await api('/api/models', { method: 'POST', body: JSON.stringify(connectionBody()) }); $('#modelList').innerHTML = r.models.map(x => `<option value="${escapeHtml(x)}"></option>`).join(''); toast(`已获取 ${r.count} 个模型`); }
  catch (e) { toast(`获取模型失败：${e.message}`, 'error', 7000); }
  finally { setBusy(button, false); }
}

async function quickTest(button) {
  const box = $('#quickResult'); setBusy(button, true); box.className = 'test-result idle'; box.innerHTML = '<strong>正在请求</strong><span>等待模型响应…</span>';
  try { const r = await api('/api/test/ai', { method: 'POST', body: JSON.stringify({ ...connectionBody(), prompt: $('#quickPrompt').value }) }); box.className = 'test-result success'; box.innerHTML = `<strong>连接成功 · ${r.latency_ms} ms</strong><span>${escapeHtml(r.reply)}</span>`; state.channelHealth[state.selectedChannelId] = { status: 'healthy', latency_ms: r.latency_ms, error: '' }; renderChannelHealth(); }
  catch (e) { box.className = 'test-result error'; box.innerHTML = `<strong>连接失败</strong><span>${escapeHtml(e.message)}</span>`; state.channelHealth[state.selectedChannelId] = { status: 'unhealthy', latency_ms: null, error: e.message }; renderChannelHealth(); }
  finally { setBusy(button, false); }
}

async function testAllChannels(button) {
  if (state.dirty && !await saveConfig(button)) return;
  setBusy(button, true, '测试中');
  try {
    const data = await api('/api/channels/test-all', { method: 'POST', body: '{}' });
    data.results.forEach(x => { state.channelHealth[x.id] = x; }); renderChannelHealth();
    toast(`渠道检测完成：${data.healthy}/${data.total} 畅通`, data.healthy === data.total ? 'success' : 'error');
  } catch (e) { toast(`渠道检测失败：${e.message}`, 'error'); }
  finally { setBusy(button, false); }
}

function openChannelDialog() {
  $('#channelDialogForm').reset(); $('#newTimeout').value = 30; $('#newChannelProvider').value = 'compatible'; $('#channelDialog').showModal();
}
function closeChannelDialog() { $('#channelDialog').close(); }
function addChannelFromDialog() {
  const id = `channel-${Date.now().toString(36)}`;
  const channel = { id, name: $('#newChannelName').value.trim(), provider: $('#newChannelProvider').value, base_url: $('#newBaseUrl').value.trim(), api_key: $('#newApiKey').value.trim(), model: $('#newModel').value.trim(), timeout_seconds: Number($('#newTimeout').value), enabled: true, priority: state.channels.length };
  state.channels.push(channel); state.channelHealth[id] = { status: 'testing', latency_ms: null, error: '' };
  state.selectedChannelId = id; renderChannelSelect(); fillChannelForm(); closeChannelDialog(); markDirty();
  probeNewChannel(channel);
}
async function probeNewChannel(channel) {
  try {
    const r = await api('/api/test/ai', { method: 'POST', body: JSON.stringify({ base_url: channel.base_url, api_key: channel.api_key, model: channel.model, timeout_seconds: channel.timeout_seconds, prompt: '只回复OK' }) });
    state.channelHealth[channel.id] = { status: 'healthy', latency_ms: r.latency_ms, error: '' }; toast(`渠道“${channel.name}”检测畅通`);
  } catch (e) {
    state.channelHealth[channel.id] = { status: 'unhealthy', latency_ms: null, error: e.message }; toast(`渠道“${channel.name}”检测失败：${e.message}`, 'error', 7000);
  }
  renderChannelHealth();
}
function deleteCurrentChannel() {
  if (state.channels.length <= 1) { toast('至少保留一个模型渠道', 'error'); return; }
  delete state.channelHealth[state.selectedChannelId]; state.channels = state.channels.filter(x => x.id !== state.selectedChannelId); state.selectedChannelId = state.channels[0].id;
  renderChannelSelect(); fillChannelForm(); markDirty();
}

async function runTest(type, button) {
  const result = $(`#result-${type}`); result.className = 'loading'; result.textContent = '测试执行中…'; setBusy(button, true);
  const group_id = $('#testGroup').value;
  const spec = type === 'ai' ? ['/api/test/ai', { ...connectionBody(), prompt: $('#testAiPrompt').value }] : type === 'onebot' ? ['/api/test/onebot', { group_id, text: $('#testOnebotText').value }] : ['/api/test/callback', { group_id, text: $('#testCallbackText').value }];
  const started = Date.now(); appendTestConsole(type, 'running', `POST ${spec[0]}`, spec[1]);
  try {
    const r = await api(spec[0], { method: 'POST', body: JSON.stringify(spec[1]) }); const elapsed = Date.now() - started;
    result.className = 'success'; result.textContent = type === 'ai' ? `成功 · ${r.latency_ms} ms · ${r.reply}` : type === 'callback' ? r.note : `发送成功 · ${r.group_id}`;
    appendTestConsole(type, 'success', `200 OK · ${elapsed} ms`, r);
  }
  catch (e) { result.className = 'error'; result.textContent = `失败 · ${e.message}`; appendTestConsole(type, 'error', `FAILED · ${Date.now() - started} ms`, { error: e.message }); }
  finally { setBusy(button, false); refreshStatus(true); }
}

function appendTestConsole(type, status, headline, payload) {
  const consoleEl = $('#testConsole'); const empty = $('.muted', consoleEl); if (empty) empty.remove();
  const entry = document.createElement('div'); entry.className = 'console-entry';
  const label = { ai: 'AI MODEL', onebot: 'ONEBOT SEND', callback: 'FULL CALLBACK' }[type];
  entry.innerHTML = `<div class="command">$ ${label}</div><div class="${status === 'error' ? 'error' : status === 'success' ? 'success' : 'latency'}">${escapeHtml(headline)}</div><pre>${escapeHtml(JSON.stringify(payload, null, 2))}</pre>`;
  consoleEl.append(entry); consoleEl.scrollTop = consoleEl.scrollHeight;
}

function formatLogTime(value) { if (!value) return new Date().toLocaleTimeString('zh-CN', { hour12: false }); return value.length > 10 ? value.slice(11) : value; }
function addLog(item) {
  state.logs.push(item); if (state.logs.length > 1200) state.logs.splice(0, 200); $('#logPulse').style.opacity = '1';
  if (!state.paused) renderLogs();
}

function renderLogs() {
  const enabled = new Set($$('.level-filters input:checked').map(x => x.value));
  const rows = state.logs.filter(x => (state.source === 'all' || x.source === state.source) && enabled.has(x.level));
  const out = $('#logOutput'), nearBottom = out.scrollHeight - out.scrollTop - out.clientHeight < 70;
  out.innerHTML = rows.length ? rows.slice(-700).map(x => `<div class="log-line ${x.level}"><span class="time">${escapeHtml(formatLogTime(x.time))}</span><span class="source">${x.source === 'ai' ? 'AI' : 'ONEBOT'}</span><span class="level">${x.level.toUpperCase()}</span><span class="message">${escapeHtml(x.message)}</span></div>`).join('') : '<div class="terminal-empty">当前筛选条件下没有日志</div>';
  $('#logCounter').textContent = `${rows.length} events`; if (nearBottom || rows.length < 80) out.scrollTop = out.scrollHeight;
  renderDashboardLogs();
}

function renderDashboardLogs() {
  const rows = state.logs.filter(x => state.miniSource === 'all' || x.source === state.miniSource).slice(-180);
  const out = $('#dashboardLogOutput'), nearBottom = out.scrollHeight - out.scrollTop - out.clientHeight < 60;
  out.innerHTML = rows.length ? rows.map(x => `<div class="mini-log-line ${x.level}"><span class="time">${escapeHtml(formatLogTime(x.time))}</span><span class="source">${x.source === 'ai' ? 'AI' : 'ONEBOT'}</span><span class="level">${x.level.toUpperCase()}</span><span class="message">${escapeHtml(x.message)}</span></div>`).join('') : '<div class="terminal-empty">当前来源没有日志</div>';
  $('#miniLogCount').textContent = `${rows.length} events`; if (nearBottom || rows.length < 60) out.scrollTop = out.scrollHeight;
}

function connectLogs() {
  state.eventSource?.close(); const es = new EventSource('/api/logs/stream'); state.eventSource = es;
  es.onmessage = e => { try { addLog(JSON.parse(e.data)); } catch (_) {} };
  es.onerror = () => { $('#logPulse').style.opacity = '.3'; };
}

function bind() {
  $$('.nav-item').forEach(x => x.onclick = () => showPage(x.dataset.page)); $$('[data-jump]').forEach(x => x.onclick = () => showPage(x.dataset.jump));
  $('#refreshBtn').onclick = () => refreshStatus(); $('#startAllBtn').onclick = e => action('start_all', e.currentTarget); $$('.action-btn').forEach(x => x.onclick = () => action(x.dataset.action, x));
  $('#channelSelect').onchange = e => selectChannel(e.target.value); $('#addChannelBtn').onclick = openChannelDialog; $('#deleteChannelBtn').onclick = deleteCurrentChannel;
  $('#testAllChannelsBtn').onclick = e => testAllChannels(e.currentTarget); $('#refreshHealthBtn').onclick = () => refreshChannelHealth();
  $('#channelProvider').onchange = e => applyProviderPreset(e.target.value); $('#newChannelProvider').onchange = e => applyProviderPreset(e.target.value, 'new');
  $('#closeChannelDialog').onclick = closeChannelDialog; $('#cancelChannelDialog').onclick = closeChannelDialog;
  $('#channelDialogForm').onsubmit = e => { e.preventDefault(); if (e.currentTarget.reportValidity()) addChannelFromDialog(); };
  $('#toggleKey').onclick = () => { $('#apiKey').type = $('#apiKey').type === 'password' ? 'text' : 'password'; };
  $('#modelsBtn').onclick = e => getModels(e.currentTarget); $('#quickTestBtn').onclick = e => quickTest(e.currentTarget);
  $('#temperature').oninput = e => { $('#temperatureValue').value = e.target.value; markDirty(); }; $('#systemPrompt').oninput = e => { $('#promptCount').textContent = e.target.value.length; markDirty(); }; $('#personality').oninput = e => { $('#personalityCount').textContent = e.target.value.length; markDirty(); };
  $('#aiForm').onsubmit = e => { e.preventDefault(); saveConfig(e.submitter); }; $('#groupForm').onsubmit = e => { e.preventDefault(); saveConfig(e.submitter); };
  $('#refreshGroupsBtn').onclick = () => loadDiscoveredGroups(); $('#enableSelectedGroupBtn').onclick = enableQuickSelectedGroup; $$('#aiForm input, #aiForm textarea, #aiForm select, #groupForm input').forEach(x => x.addEventListener('change', () => { updateRouteSummary(); markDirty(); }));
  $$('.test-run').forEach(x => x.onclick = () => runTest(x.dataset.test, x));
  $('#clearTestConsole').onclick = () => { $('#testConsole').innerHTML = '<p class="muted">// 测试控制台已清空</p>'; };
  $$('[data-mini-source]').forEach(x => x.onclick = () => { $$('[data-mini-source]').forEach(y => y.classList.toggle('active', y === x)); state.miniSource = x.dataset.miniSource; renderDashboardLogs(); });
  $$('.source-tabs button').forEach(x => x.onclick = () => { $$('.source-tabs button').forEach(y => y.classList.toggle('active', y === x)); state.source = x.dataset.source; renderLogs(); });
  $$('.level-filters input').forEach(x => x.onchange = renderLogs); $('#clearLogs').onclick = () => { state.logs = []; renderLogs(); };
  $('#pauseLogs').onclick = e => { state.paused = !state.paused; e.currentTarget.textContent = state.paused ? '继续' : '暂停'; if (!state.paused) renderLogs(); };
}

async function init() {
  bind(); connectLogs();
  try { fillConfig(await api('/api/config')); } catch (e) { toast(`配置加载失败：${e.message}`, 'error'); }
  await refreshStatus(); setInterval(() => refreshStatus(true), 5000); setInterval(() => refreshChannelHealth(true), 15000);
  setInterval(() => { if ($('#page-groups').classList.contains('active') && !state.dirty) loadDiscoveredGroups(true); }, 30000);
}
init();
