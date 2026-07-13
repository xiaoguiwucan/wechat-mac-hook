const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const state = { config: null, status: null, channels: [], selectedChannelId: '', channelHealth: {}, groupCatalog: [], dirty: false, logs: [], source: 'all', miniSource: 'all', paused: false, eventSource: null, traceDiagnostic: null };
const pageMeta = {
  overview: ['运行总览', '第二微信、OneBot 与 AI 服务'], ai: ['AI 模型', '对话、OCR 与 ASR 模型配置'],
  groups: ['群聊策略', '目标群与自动回复规则'], tests: ['测试中心', '验证模型、Hook 与完整回调链路'],
  media: ['图片图库', '图片 OCR、文件/视频索引与媒体记忆'],
  'voice-records': ['语音内容', '群语音泡、ASR 转写与语音记忆'],
  voices: ['语音包管理', 'silk / zip / zip1 导入与语音发送'],
  faces: ['表情包管理', 'face / GIF 动图收藏、解析与发送'],
  memory: ['记忆数据库', '聊天记录、人物画像与群长期记忆'], logs: ['实时日志', 'AI 回复与 OneBot 运行输出']
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
  if (name === 'media') requestAnimationFrame(() => loadMediaCenter(null, true));
  if (name === 'voice-records') requestAnimationFrame(() => loadVoiceRecords(null, true));
  if (name === 'voices') requestAnimationFrame(() => loadVoicepacks(null, true));
  if (name === 'faces') requestAnimationFrame(() => loadFaces(null, true));
  if (name === 'memory' && $('#memoryResults')?.textContent?.includes('选择左侧操作')) requestAnimationFrame(() => loadMedia($('#loadMediaBtn'), true));
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
  $('.hook', $('[data-service="onebot"]')).textContent = data.onebot.hook_ready ? (data.onebot.send_ready ? '文本/文件就绪' : '已附加 / 待唤醒') : '未就绪';
  const mediaHook = $('.media-hook', $('[data-service="onebot"]'));
  if (mediaHook) {
    const media = data.onebot.media_upload || {};
    mediaHook.textContent = data.onebot.media_upload_ready ? '图片/视频/语音就绪' : '待真实上传';
    mediaHook.title = media.message || '当前微信进程还没有捕获真实 UploadMedia';
  }
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

async function refreshTraces(silent = false) {
  try {
    const data = await api('/api/traces/recent');
    state.traceDiagnostic = data.diagnostic;
    const d = data.diagnostic || {};
    $('#diagReceived').textContent = d.received_at || '--';
    $('#diagGenerated').textContent = d.ai_generated_at || '--';
    $('#diagSendLatency').textContent = d.send_latency_ms == null ? '--' : `${d.send_latency_ms} ms`;
    $('#diagSendResult').textContent = d.send_result || '--';
    $('#diagRecvStatus').textContent = d.onebot_receive_status || '--';
    $('#diagSendStatus').textContent = d.onebot_send_status || '--';
    $('#diagTraceId').textContent = `trace_id: ${d.trace_id || '--'}`;
  } catch (e) { if (!silent) toast(`链路诊断刷新失败：${e.message}`, 'error'); }
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
  const o = c.vision_ocr || {};
  $('#ocrEnabled').checked = !!o.enabled; $('#ocrBaseUrl').value = o.base_url || c.base_url || ''; $('#ocrApiKey').value = o.api_key || '';
  $('#ocrModel').value = o.model || c.model || ''; $('#ocrTimeout').value = o.timeout_seconds || 60; $('#ocrPrompt').value = o.prompt || '';
  $('#ocrAutoAnalyze').checked = !!o.auto_analyze;
  const a = c.asr || {};
  $('#asrEnabled').checked = !!a.enabled; $('#asrBaseUrl').value = a.base_url || 'https://api.20250424.xyz/v1'; $('#asrApiKey').value = a.api_key || '';
  $('#asrModel').value = a.model || 'TeleAI/TeleSpeechASR'; $('#asrTimeout').value = a.timeout_seconds || 90; $('#asrLanguage').value = a.language || 'zh';
  $('#asrPrompt').value = a.prompt || ''; $('#asrAutoTranscribe').checked = a.auto_transcribe !== false;
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
  $('#groupList').innerHTML = state.groupCatalog.map(group => {
    const confidence = group.confidence == null ? '' : ` · ${group.confidence}%`;
    const source = group.source || (group.last_seen ? 'event' : 'config');
    return `<div class="group-permission-row" data-group-id="${escapeHtml(group.id)}"><label class="permission-check"><input class="group-enabled" type="checkbox" ${group.selected ? 'checked' : ''}><i></i><span>${group.selected ? '已授权' : '未授权'}</span></label><div class="field"><input class="group-name" value="${escapeHtml(group.name)}" aria-label="群聊名称"></div><code>${escapeHtml(group.id)}</code><span class="discovery-source" title="${escapeHtml(group.preview || '')}"><i></i><span>${escapeHtml(source)}${confidence}<small>${group.preview ? escapeHtml(group.preview) : escapeHtml(group.last_seen || '可编辑别名')}</small></span></span></div>`;
  }).join('');
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
function aliasesData() { return Object.fromEntries(state.groupCatalog.filter(x => x.id.endsWith('@chatroom')).map(x => [x.id, (x.name || '').trim()])); }
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
  const mem = $('#memoryGroup'); if (mem) { const oldMem = mem.value; mem.innerHTML = state.groupCatalog.filter(x => x.id).map(x => `<option value="${escapeHtml(x.id)}">${escapeHtml(x.name)} · ${escapeHtml(x.id)}</option>`).join(''); if ([...mem.options].some(x => x.value === oldMem)) mem.value = oldMem; }
  const media = $('#mediaGroup'); if (media) { const oldMedia = media.value; media.innerHTML = `<option value="">全部群媒体</option>` + state.groupCatalog.filter(x => x.id).map(x => `<option value="${escapeHtml(x.id)}">${escapeHtml(x.name)} · ${escapeHtml(x.id)}</option>`).join(''); if ([...media.options].some(x => x.value === oldMedia)) media.value = oldMedia; }
  const voiceRecords = $('#voiceRecordsGroup'); if (voiceRecords) { const oldVoiceRecords = voiceRecords.value; voiceRecords.innerHTML = `<option value="">全部来源群</option>` + state.groupCatalog.filter(x => x.id).map(x => `<option value="${escapeHtml(x.id)}">${escapeHtml(x.name)} · ${escapeHtml(x.id)}</option>`).join(''); if ([...voiceRecords.options].some(x => x.value === oldVoiceRecords)) voiceRecords.value = oldVoiceRecords; }
  const voice = $('#voiceTargetGroup'); if (voice) { const oldVoice = voice.value; voice.innerHTML = groupsData().filter(x => x.id).map(x => `<option value="${escapeHtml(x.id)}">${escapeHtml(x.name)} · ${escapeHtml(x.id)}</option>`).join(''); if ([...voice.options].some(x => x.value === oldVoice)) voice.value = oldVoice; }
  const faceTarget = $('#faceTargetGroup'); if (faceTarget) { const oldFaceTarget = faceTarget.value; faceTarget.innerHTML = groupsData().filter(x => x.id).map(x => `<option value="${escapeHtml(x.id)}">${escapeHtml(x.name)} · ${escapeHtml(x.id)}</option>`).join(''); if ([...faceTarget.options].some(x => x.value === oldFaceTarget)) faceTarget.value = oldFaceTarget; }
  const faceGroup = $('#faceGroup'); if (faceGroup) { const oldFaceGroup = faceGroup.value; faceGroup.innerHTML = `<option value="">全部来源群</option>` + state.groupCatalog.filter(x => x.id).map(x => `<option value="${escapeHtml(x.id)}">${escapeHtml(x.name)} · ${escapeHtml(x.id)}</option>`).join(''); if ([...faceGroup.options].some(x => x.value === oldFaceGroup)) faceGroup.value = oldFaceGroup; }
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
    max_reply_chars: Number($('#maxReplyChars').value), max_context_messages: Number($('#maxContext').value), onebot_api: state.config.onebot_api, target_groups: groupsData(),
    group_aliases: aliasesData(), memory: state.config.memory || { enabled: true, max_turns: 12, summary_enabled: true },
    tools: state.config.tools || { enabled: true, allowed: ['get_status', 'get_recent_logs', 'list_groups', 'test_model_channel', 'send_probe', 'search_messages', 'get_group_memory', 'vector_search', 'list_personas', 'list_media'] },
    onebot_monitor: state.config.onebot_monitor || { enabled: true, auto_recover: false },
    vision_ocr: {
      enabled: $('#ocrEnabled').checked, base_url: $('#ocrBaseUrl').value.trim(), api_key: $('#ocrApiKey').value.trim(),
      model: $('#ocrModel').value.trim(), timeout_seconds: Number($('#ocrTimeout').value || 60),
      prompt: $('#ocrPrompt').value.trim(), auto_analyze: $('#ocrAutoAnalyze').checked
    },
    asr: {
      enabled: $('#asrEnabled').checked, base_url: $('#asrBaseUrl').value.trim(), api_key: $('#asrApiKey').value.trim(),
      model: $('#asrModel').value.trim(), timeout_seconds: Number($('#asrTimeout').value || 90),
      language: $('#asrLanguage').value.trim() || 'zh', prompt: $('#asrPrompt').value.trim(), auto_transcribe: $('#asrAutoTranscribe').checked
    }
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

async function saveAliases(button) {
  setBusy(button, true, '保存中…');
  try {
    const data = await api('/api/groups/aliases', { method: 'POST', body: JSON.stringify({ aliases: aliasesData() }) });
    mergeGroupCatalog(data.groups); renderGroupPermissions();
    toast('群别名已保存，未授权群也会持久显示名称');
  } catch (e) { toast(`别名保存失败：${e.message}`, 'error', 7000); }
  finally { setBusy(button, false); }
}

async function syncUiGroups(button) {
  setBusy(button, true, '同步中…'); $('#groupDiscoveryState').textContent = '正在通过辅助功能只读扫描第二微信…';
  try {
    const r = await api('/api/groups/sync-ui', { method: 'POST', body: '{}' });
    $('#groupDiscoveryState').textContent = `UI 同步完成：读取 ${r.count} 条文本`;
    appendTestConsole('sync', 'success', 'UI GROUP SYNC', r);
    toast(r.note || 'UI 群名同步完成');
  } catch (e) { $('#groupDiscoveryState').textContent = 'UI 同步失败'; toast(e.message, 'error', 9000); }
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

function ocrBody() { return { base_url: $('#ocrBaseUrl').value.trim(), api_key: $('#ocrApiKey').value.trim(), model: $('#ocrModel').value.trim(), timeout_seconds: Number($('#ocrTimeout').value || 60), prompt: $('#ocrPrompt').value.trim(), image: $('#ocrTestImage').value.trim() }; }
async function ocrTest(button) {
  const box = $('#ocrResult'); setBusy(button, true, '测试中'); box.className = 'test-result idle'; box.innerHTML = '<strong>正在 OCR</strong><span>等待视觉模型响应…</span>';
  try {
    const r = await api('/api/test/vision-ocr', { method: 'POST', body: JSON.stringify(ocrBody()) });
    box.className = 'test-result success'; box.innerHTML = `<strong>OCR 成功 · ${r.latency_ms} ms</strong><span>${escapeHtml(r.reply)}</span>`;
    toast('OCR 模型测试成功');
  } catch (e) {
    box.className = 'test-result error'; box.innerHTML = `<strong>OCR 失败</strong><span>${escapeHtml(e.message)}</span>`;
    toast(`OCR 测试失败：${e.message}`, 'error', 9000);
  } finally { setBusy(button, false); }
}

function asrBody() { return { base_url: $('#asrBaseUrl').value.trim(), api_key: $('#asrApiKey').value.trim(), model: $('#asrModel').value.trim(), timeout_seconds: Number($('#asrTimeout').value || 90), language: $('#asrLanguage').value.trim() || 'zh', prompt: $('#asrPrompt').value.trim(), audio: $('#asrTestAudio').value.trim() }; }
async function asrTest(button) {
  const box = $('#asrResult'); setBusy(button, true, '测试中'); box.className = 'test-result idle'; box.innerHTML = '<strong>正在 ASR</strong><span>上传语音原始文件并等待识别…</span>';
  try {
    const r = await api('/api/test/asr', { method: 'POST', body: JSON.stringify(asrBody()) });
    box.className = 'test-result success'; box.innerHTML = `<strong>ASR 成功 · ${r.latency_ms} ms</strong><span>${escapeHtml(r.text)}</span>`;
    toast('ASR 模型测试成功');
  } catch (e) {
    box.className = 'test-result error'; box.innerHTML = `<strong>ASR 失败</strong><span>${escapeHtml(e.message)}</span>`;
    toast(`ASR 测试失败：${e.message}`, 'error', 9000);
  } finally { setBusy(button, false); }
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
  const baseText = $('#testOnebotText').value;
  const specs = {
    ai: ['/api/test/ai', { ...connectionBody(), prompt: $('#testAiPrompt').value }],
    onebot: ['/api/test/onebot', { group_id, text: baseText }],
    callback: ['/api/test/callback', { group_id, text: $('#testCallbackText').value }],
    probe: ['/api/onebot/send-probe', { group_id }],
    recover: ['/api/onebot/recover', { restart_wechat2: false }],
    wechat2: ['/api/onebot/recover', { restart_wechat2: true }],
    at: ['/api/messages/send', { group_id, type: 'at', user_id: $('#atUserId').value, text: baseText }],
    reply: ['/api/messages/send', { group_id, type: 'reply', message_id: $('#replyMessageId').value, text: baseText }],
    image: ['/api/messages/send', { group_id, type: 'image', file: $('#mediaPath').value }],
    file: ['/api/messages/send', { group_id, type: 'file', file: $('#mediaPath').value }],
    video: ['/api/messages/send', { group_id, type: 'video', file: $('#videoPath').value || $('#mediaPath').value }],
    record: ['/api/messages/send', { group_id, type: 'record', file: $('#recordPath').value || $('#mediaPath').value }],
  };
  const spec = specs[type] || specs.onebot;
  const started = Date.now(); appendTestConsole(type, 'running', `POST ${spec[0]}`, spec[1]);
  try {
    const r = await api(spec[0], { method: 'POST', body: JSON.stringify(spec[1]) }); const elapsed = Date.now() - started;
    result.className = 'success'; result.textContent = type === 'ai' ? `成功 · ${r.latency_ms} ms · ${r.reply}` : type === 'callback' ? r.note : `成功 · ${r.latency_ms || elapsed} ms`;
    appendTestConsole(type, 'success', `200 OK · ${elapsed} ms`, r);
  }
  catch (e) { result.className = 'error'; result.textContent = `失败 · ${e.message}`; appendTestConsole(type, 'error', `FAILED · ${Date.now() - started} ms`, { error: e.message }); }
  finally { setBusy(button, false); refreshStatus(true); }
}

function appendTestConsole(type, status, headline, payload) {
  const consoleEl = $('#testConsole'); const empty = $('.muted', consoleEl); if (empty) empty.remove();
  const entry = document.createElement('div'); entry.className = 'console-entry';
  const label = { ai: 'AI MODEL', onebot: 'ONEBOT SEND', callback: 'FULL CALLBACK', probe: 'SEND PROBE', recover: 'RECOVER ONEBOT', wechat2: 'RESTART WECHAT2', at: 'SEND AT', reply: 'SEND REPLY', image: 'SEND IMAGE', file: 'SEND FILE', video: 'SEND VIDEO', record: 'SEND RECORD', sync: 'UI SYNC' }[type] || String(type).toUpperCase();
  entry.innerHTML = `<div class="command">$ ${label}</div><div class="${status === 'error' ? 'error' : status === 'success' ? 'success' : 'latency'}">${escapeHtml(headline)}</div><pre>${escapeHtml(JSON.stringify(payload, null, 2))}</pre>`;
  consoleEl.append(entry); consoleEl.scrollTop = consoleEl.scrollHeight;
}

async function refreshMemoryStats(silent = false) {
  try {
    const r = await api('/api/memory/stats');
    $('#memoryStats').innerHTML = `
      <div><span>${r.messages}</span><small>消息总数</small></div>
      <div><span>${r.groups}</span><small>群数量</small></div>
      <div><span>${r.members}</span><small>成员基础</small></div>
      <div><span>${r.incoming}</span><small>收到消息</small></div>
      <div><span>${r.outgoing}</span><small>AI 发出</small></div>
      <div><span>${r.personas || 0}</span><small>人物画像</small></div>
      <div><span>${r.vectors || 0}</span><small>向量索引</small></div>
      <div><span>${r.media_items || 0}</span><small>媒体索引</small></div>
      <div><span>${escapeHtml(r.latest_message_at || '--')}</span><small>最新入库</small></div>
      <code>${escapeHtml(r.db_path)}</code>`;
  } catch (e) { if (!silent) toast(`记忆库统计失败：${e.message}`, 'error'); }
}

function setMemoryMode(title, sub, mode) {
  $('#memoryResultTitle').textContent = title;
  $('#memoryResultSub').textContent = sub;
  $('#memoryMode').textContent = mode;
}

async function searchMemory(button) {
  setBusy(button, true, '查询中…');
  try {
    const r = await api('/api/memory/search', { method: 'POST', body: JSON.stringify({ group_id: $('#memoryGroup').value, query: $('#memoryQuery').value, limit: 80 }) });
    setMemoryMode('聊天记录查询', `返回 ${r.count} 条消息`, 'MESSAGES');
    $('#memoryResults').innerHTML = r.items.length ? r.items.map(x => `<div class="memory-row"><div><strong>${escapeHtml(x.sender_name || x.user_id || x.direction)}</strong><small>${escapeHtml(x.created_at || '')} · ${escapeHtml(x.group_id || '')} · ${escapeHtml(x.trace_id || '')}</small></div><p>${escapeHtml(x.text || x.raw_message || '')}</p></div>`).join('') : '<div class="terminal-empty">没有查到记录</div>';
  } catch (e) { toast(`查询失败：${e.message}`, 'error'); }
  finally { setBusy(button, false); }
}

async function vectorSearch(button) {
  setBusy(button, true, '检索中…');
  try {
    const r = await api('/api/memory/vector-search', { method: 'POST', body: JSON.stringify({ group_id: $('#memoryGroup').value, query: $('#memoryQuery').value, limit: 30 }) });
    setMemoryMode('向量语义检索', `本地轻量向量命中 ${r.count} 条`, 'VECTOR');
    $('#memoryResults').innerHTML = r.items.length ? r.items.map(x => `<div class="memory-row vector"><div><strong>score ${x.score}</strong><small>${escapeHtml(x.created_at || '')} · ${escapeHtml(x.sender_name || x.user_id || x.direction || '')} · ${escapeHtml(x.event_id || '')}</small></div><p>${escapeHtml(x.text || '')}</p></div>`).join('') : '<div class="terminal-empty">没有语义命中；可以先导入/积累更多聊天记录</div>';
  } catch (e) { toast(`向量检索失败：${e.message}`, 'error'); }
  finally { setBusy(button, false); }
}

async function loadMembers(button) {
  setBusy(button, true, '读取中…');
  try {
    const r = await api('/api/memory/personas', { method: 'POST', body: JSON.stringify({ group_id: $('#memoryGroup').value, limit: 100 }) });
    setMemoryMode('人物画像', `返回 ${r.count} 个成员画像`, 'PERSONA');
    $('#memoryResults').innerHTML = r.items.length ? r.items.map(x => {
      let tags = []; try { tags = JSON.parse(x.tags_json || '[]'); } catch (_) {}
      return `<div class="memory-row persona"><div><strong>${escapeHtml(x.display_name || x.nickname || x.user_id)}</strong><small>${escapeHtml(x.group_id)} · ${x.message_count} messages · ${escapeHtml(x.last_seen || '')}</small></div><p>${escapeHtml(x.summary || '暂无画像摘要，点击“重建索引/画像”生成。')}</p><div class="tag-list">${tags.map(t => `<span>${escapeHtml(t)}</span>`).join('')}</div><button class="link-btn persona-edit" data-user-id="${escapeHtml(x.user_id)}" data-group-id="${escapeHtml(x.group_id)}" data-summary="${escapeHtml(x.summary || '')}">编辑画像</button></div>`;
    }).join('') : '<div class="terminal-empty">当前群暂无成员画像数据</div>';
    $$('.persona-edit').forEach(btn => btn.onclick = () => editPersona(btn));
  } catch (e) { toast(`成员画像读取失败：${e.message}`, 'error'); }
  finally { setBusy(button, false); }
}

function editPersona(btn) {
  const summary = prompt(`编辑 ${btn.dataset.userId} 的人物画像`, btn.dataset.summary || '');
  if (summary == null) return;
  api('/api/memory/persona/save', { method: 'POST', body: JSON.stringify({ user_id: btn.dataset.userId, group_id: btn.dataset.groupId, summary, tags: [], facts: [] }) })
    .then(() => { toast('人物画像已保存'); loadMembers($('#loadMembersBtn')); })
    .catch(e => toast(`画像保存失败：${e.message}`, 'error'));
}

function bindMediaCardActions(root = document) {
  $$('.media-analyze', root).forEach(btn => btn.onclick = () => analyzeMedia(btn));
  $$('.media-annotate', root).forEach(btn => btn.onclick = () => editMedia(btn));
  $$('.media-ocr', root).forEach(btn => btn.onclick = () => ocrMedia(btn));
  $$('.media-asr', root).forEach(btn => btn.onclick = () => asrMedia(btn));
  $$('.media-preview', root).forEach(btn => btn.onclick = () => previewMedia(btn));
  $$('.media-expand', root).forEach(btn => btn.onclick = () => previewContent(btn));
}

async function loadMedia(button, silent = false) {
  if (button) setBusy(button, true, '读取中…');
  try {
    const r = await api('/api/memory/media', { method: 'POST', body: JSON.stringify({ group_id: $('#memoryGroup').value, query: $('#memoryQuery').value.trim(), limit: 120 }) });
    setMemoryMode('群图库 / 媒体记录', `图片/文件/语音/视频索引 ${r.count} 条`, 'GALLERY');
    $('#memoryResults').innerHTML = r.items.length ? `<div class="media-gallery">${r.items.map(mediaCard).join('')}</div>` : '<div class="terminal-empty">暂无媒体消息索引。群里发图片后会自动进入 OCR 队列。</div>';
    bindMediaCardActions($('#memoryResults'));
  } catch (e) { if (!silent) toast(`媒体读取失败：${e.message}`, 'error'); }
  finally { if (button) setBusy(button, false); }
}

async function loadMediaCenter(button, silent = false) {
  if (button) setBusy(button, true, '查询中…');
  const chip = $('#mediaStatusChip');
  if (chip) { chip.textContent = '读取中'; chip.className = 'health-chip warn'; }
  try {
    const r = await api('/api/memory/media', { method: 'POST', body: JSON.stringify({
      group_id: $('#mediaGroup').value,
      media_type: $('#mediaType').value || 'image',
      status: $('#mediaStatus').value,
      query: $('#mediaQuery').value.trim(),
      limit: 180
    }) });
    $('#mediaCenterTitle').textContent = '图片图库 / 媒体索引';
    $('#mediaCenterSub').textContent = `当前筛选返回 ${r.count} 条；图片支持放大、OCR、编辑标签`;
    $('#mediaCenterMode').textContent = r.count ? 'GALLERY' : 'EMPTY';
    $('#mediaCenterResults').innerHTML = r.items.length ? `<div class="media-gallery">${r.items.map(mediaCard).join('')}</div>` : '<div class="terminal-empty">暂无媒体消息。你在值班群发图片/文件/视频后，这里会自动出现。</div>';
    bindMediaCardActions($('#mediaCenterResults'));
    if (chip) { chip.textContent = `已入库 ${r.count} 条`; chip.className = 'health-chip good'; }
  } catch (e) {
    if (chip) { chip.textContent = '读取失败'; chip.className = 'health-chip warn'; }
    if (!silent) toast(`图库读取失败：${e.message}`, 'error');
  } finally { if (button) setBusy(button, false); }
}

async function loadVoiceRecords(button, silent = false) {
  if (button) setBusy(button, true, '查询中…');
  const chip = $('#voiceRecordsStatusChip');
  if (chip) { chip.textContent = '读取中'; chip.className = 'health-chip warn'; }
  try {
    const r = await api('/api/memory/media', { method: 'POST', body: JSON.stringify({
      group_id: $('#voiceRecordsGroup').value,
      media_type: 'record',
      status: $('#voiceRecordsStatus').value,
      query: $('#voiceRecordsQuery').value.trim(),
      limit: 180
    }) });
    $('#voiceRecordsTitle').textContent = '群语音内容 / ASR';
    $('#voiceRecordsSub').textContent = `当前筛选返回 ${r.count} 条；支持试听、ASR 转写、编辑文本`;
    $('#voiceRecordsMode').textContent = r.count ? 'ASR' : 'EMPTY';
    $('#voiceRecordsResults').innerHTML = r.items.length ? `<div class="media-gallery">${r.items.map(voiceRecordCard).join('')}</div>` : '<div class="terminal-empty">暂无语音记录。群里发语音后，record 音频会自动入库到这里。</div>';
    bindMediaCardActions($('#voiceRecordsResults'));
    if (chip) { chip.textContent = `语音 ${r.count} 条`; chip.className = 'health-chip good'; }
  } catch (e) {
    if (chip) { chip.textContent = '读取失败'; chip.className = 'health-chip warn'; }
    if (!silent) toast(`语音内容读取失败：${e.message}`, 'error');
  } finally { if (button) setBusy(button, false); }
}

function mediaCard(x) {
  const tags = [...(x.tags || []), ...(x.keywords || [])].filter(Boolean).slice(0, 12);
  const typeLabel = (x.media_type || 'MEDIA').toUpperCase();
  const isVoiceWaiting = x.media_type === 'record' && x.status === 'waiting_transcript';
  const preview = x.data_url ? `<button class="media-thumb media-preview" data-src="${escapeHtml(x.data_url)}" data-title="媒体 #${x.id}"><img src="${escapeHtml(x.data_url)}" alt="media ${x.id}"></button>` : `<div class="media-thumb placeholder ${escapeHtml(x.media_type || '')}"><span>${escapeHtml(typeLabel)}</span><small>${escapeHtml(x.file || x.url || '已建立索引')}</small></div>`;
  const audio = x.audio_url ? `<audio class="media-audio" controls src="${escapeHtml(x.audio_url)}"></audio>` : '';
  const fileText = x.file || x.url || '';
  return `<article class="media-card" data-media-id="${x.id}">
    ${preview}
    <div class="media-card-body">
      <div class="media-title"><strong>${escapeHtml(x.media_type)} #${x.id}</strong><span class="status-pill ${escapeHtml(x.status || 'indexed')}">${escapeHtml(x.status || 'indexed')}</span></div>
      <small>${escapeHtml(x.created_at || '')} · ${escapeHtml(x.sender_name || x.user_id || '未知发送者')}</small>
      ${contentBlock('摘要', x.image_summary || '待解析', `媒体 #${x.id} 摘要`)}
      ${contentBlock('OCR', x.ocr_text || '无', `媒体 #${x.id} OCR`)}
      ${audio}
      ${fileText ? contentBlock('文件', fileText, `媒体 #${x.id} 文件`) : ''}
      ${tags.length ? `<div class="tag-list">${tags.map(t => `<span>${escapeHtml(t)}</span>`).join('')}</div>` : '<div class="tag-list muted-tags"><span>暂无标签</span></div>'}
      ${x.error ? `<p class="${isVoiceWaiting ? 'notice-text' : 'error-text'}">${escapeHtml(x.error)}</p>` : ''}
      <div class="media-actions">
        <button class="link-btn media-preview" data-src="${escapeHtml(x.data_url || '')}" data-title="媒体 #${x.id}" ${x.data_url ? '' : 'disabled'}>放大</button>
        <button class="link-btn media-ocr" data-media-id="${x.id}" ${x.media_type === 'image' ? '' : 'disabled'}>OCR解析</button>
        <button class="link-btn media-asr" data-media-id="${x.id}" ${x.media_type === 'record' ? '' : 'disabled'}>ASR转写</button>
        <button class="link-btn media-annotate" data-media-id="${x.id}" data-media-type="${escapeHtml(x.media_type || '')}" data-summary="${escapeHtml(x.image_summary || '')}" data-ocr="${escapeHtml(x.ocr_text || '')}" data-tags="${escapeHtml((x.tags || []).join('，'))}" data-keywords="${escapeHtml((x.keywords || []).join('，'))}">编辑</button>
        <button class="link-btn media-analyze" data-media-id="${x.id}">元数据</button>
      </div>
    </div>
  </article>`;
}

function voiceRecordCard(x) {
  const tags = [...(x.tags || []), ...(x.keywords || [])].filter(Boolean).slice(0, 12);
  const audio = x.audio_url ? `<audio class="media-audio" controls src="${escapeHtml(x.audio_url)}"></audio>` : '<div class="media-help-card"><strong>暂无可试听音频</strong><p>还没拿到本地 silk/wav 文件，等待 OneBot 下载缓存入库。</p></div>';
  const transcript = x.ocr_text || '';
  const fileText = x.file || x.url || '';
  return `<article class="media-card voice-record-card" data-media-id="${x.id}">
    <div class="media-card-body">
      <div class="media-title"><strong>语音 #${x.id}</strong><span class="status-pill ${escapeHtml(x.status || 'indexed')}">${escapeHtml(x.status || 'indexed')}</span></div>
      <small>${escapeHtml(x.created_at || '')} · ${escapeHtml(x.sender_name || x.user_id || '未知发送者')}</small>
      ${audio}
      ${contentBlock('ASR 转写', transcript || '未转写', `语音 #${x.id} ASR 转写`)}
      ${contentBlock('摘要', x.image_summary || '待解析', `语音 #${x.id} 摘要`)}
      ${fileText ? contentBlock('音频文件', fileText, `语音 #${x.id} 文件`) : ''}
      ${tags.length ? `<div class="tag-list">${tags.map(t => `<span>${escapeHtml(t)}</span>`).join('')}</div>` : '<div class="tag-list muted-tags"><span>暂无标签</span></div>'}
      ${x.error ? `<p class="${x.status === 'waiting_transcript' ? 'notice-text' : 'error-text'}">${escapeHtml(x.error)}</p>` : ''}
      <div class="media-actions">
        <button class="link-btn media-asr" data-media-id="${x.id}">ASR转写</button>
        <button class="link-btn media-annotate" data-media-id="${x.id}" data-media-type="record" data-summary="${escapeHtml(x.image_summary || '')}" data-ocr="${escapeHtml(transcript)}" data-tags="${escapeHtml((x.tags || []).join('，'))}" data-keywords="${escapeHtml((x.keywords || []).join('，'))}">编辑文本</button>
        <button class="link-btn media-analyze" data-media-id="${x.id}">元数据</button>
      </div>
    </div>
  </article>`;
}

function contentBlock(label, value, title) {
  const text = String(value == null ? '' : value);
  return `<button type="button" class="media-text media-expand" data-title="${escapeHtml(title || label)}" data-content="${escapeHtml(text)}"><b>${escapeHtml(label)}</b><span>${escapeHtml(text || '无')}</span><em>点击展开</em></button>`;
}

async function analyzeMedia(btn) {
  try {
    const r = await api('/api/memory/media/analyze', { method: 'POST', body: JSON.stringify({ id: btn.dataset.mediaId }) });
    setMemoryMode('媒体元数据', `媒体 #${btn.dataset.mediaId} 本地解析结果`, 'MEDIA META');
    $('#memoryResults').innerHTML = `<div class="memory-row media"><pre>${escapeHtml(JSON.stringify(r, null, 2))}</pre></div>`;
  } catch (e) { toast(`媒体解析失败：${e.message}`, 'error'); }
}

async function ocrMedia(btn) {
  setBusy(btn, true, '解析中');
  try {
    const r = await api('/api/memory/media/ocr', { method: 'POST', body: JSON.stringify({ id: btn.dataset.mediaId }) });
    toast(`图片 #${btn.dataset.mediaId} 解析完成 · ${r.latency_ms || 0} ms`);
    if ($('#page-media').classList.contains('active')) loadMediaCenter($('#mediaRefreshBtn'), true);
    if ($('#page-memory').classList.contains('active')) loadMedia($('#loadMediaBtn'), true);
  } catch (e) { toast(`OCR 解析失败：${e.message}`, 'error', 9000); }
  finally { setBusy(btn, false); }
}

async function asrMedia(btn) {
  setBusy(btn, true, '转写中');
  try {
    const r = await api('/api/memory/media/asr', { method: 'POST', body: JSON.stringify({ id: btn.dataset.mediaId }) });
    toast(`语音 #${btn.dataset.mediaId} ASR 完成 · ${r.latency_ms || 0} ms`);
    if ($('#page-media').classList.contains('active')) loadMediaCenter($('#mediaRefreshBtn'), true);
    if ($('#page-voice-records')?.classList.contains('active')) loadVoiceRecords($('#voiceRecordsRefreshBtn'), true);
    if ($('#page-memory').classList.contains('active')) loadMedia($('#loadMediaBtn'), true);
  } catch (e) { toast(`ASR 转写失败：${e.message}`, 'error', 9000); }
  finally { setBusy(btn, false); }
}

async function editMedia(btn) {
  const isRecord = btn.dataset.mediaType === 'record' || $('#page-voice-records')?.classList.contains('active');
  const image_summary = prompt(isRecord ? '编辑语音摘要' : '编辑图片/文件摘要', btn.dataset.summary || '');
  if (image_summary == null) return;
  const ocr_text = prompt(isRecord ? '编辑 ASR 转写文本（没有可留空）' : '编辑 OCR 文本（没有可留空）', btn.dataset.ocr || '');
  if (ocr_text == null) return;
  const tags = prompt(isRecord ? '编辑语音标签，用逗号分隔' : '编辑图片标签，用逗号分隔', btn.dataset.tags || '');
  if (tags == null) return;
  const keywords = prompt('编辑关键字标签，用逗号分隔', btn.dataset.keywords || '');
  if (keywords == null) return;
  try {
    await api('/api/memory/media/save', { method: 'POST', body: JSON.stringify({ id: btn.dataset.mediaId, image_summary, ocr_text, tags, keywords, status: isRecord && ocr_text.trim() ? 'transcribed' : 'annotated' }) });
    toast(isRecord ? '语音摘要/ASR/标签 已保存' : '媒体摘要/OCR/标签 已保存');
    if ($('#page-media').classList.contains('active')) loadMediaCenter($('#mediaRefreshBtn'), true);
    if ($('#page-voice-records')?.classList.contains('active')) loadVoiceRecords($('#voiceRecordsRefreshBtn'), true);
    if ($('#page-memory').classList.contains('active')) loadMedia($('#loadMediaBtn'), true);
  } catch (e) { toast(`媒体标注保存失败：${e.message}`, 'error'); }
}

function previewMedia(btn) {
  if (!btn.dataset.src) return;
  const dlg = $('#mediaPreviewDialog');
  $('#mediaPreviewTitle').textContent = btn.dataset.title || '图片预览';
  $('#mediaPreviewImage').src = btn.dataset.src;
  dlg.showModal();
}

function previewContent(btn) {
  const dlg = $('#contentPreviewDialog');
  $('#contentPreviewTitle').textContent = btn.dataset.title || '内容详情';
  $('#contentPreviewBody').textContent = btn.dataset.content || '';
  dlg.showModal();
}

async function syncRecordTranscripts(button) {
  setBusy(button, true, 'ASR中…');
  try {
    const groupId = $('#voiceRecordsGroup')?.value || '';
    const manualText = $('#voiceRecordsManualText')?.value?.trim() || '';
    if (manualText) {
      const r = await api('/api/memory/media/sync-transcripts', { method: 'POST', body: JSON.stringify({ group_id: groupId, text: manualText }) });
      toast(r.count ? `已用兜底文本写入 ${r.count} 条语音` : '兜底文本未写入：没有可更新的语音泡', r.count ? 'success' : 'error', 7000);
      if (r.count && $('#voiceRecordsManualText')) $('#voiceRecordsManualText').value = '';
      await loadVoiceRecords($('#voiceRecordsRefreshBtn'), true);
      return;
    }
    const r = await api('/api/memory/media', { method: 'POST', body: JSON.stringify({ group_id: groupId, media_type: 'record', limit: 50 }) });
    const rows = (r.items || []).filter(x => !x.ocr_text && x.file && !['asr_running', 'transcribed'].includes(x.status));
    if (!rows.length) { toast('没有需要 ASR 转写的语音原始文件', 'error', 6000); return; }
    let ok = 0, fail = 0;
    for (const row of rows.slice(0, 10)) {
      try { await api('/api/memory/media/asr', { method: 'POST', body: JSON.stringify({ id: row.id }) }); ok += 1; }
      catch (_) { fail += 1; }
    }
    toast(`批量 ASR 完成：成功 ${ok} 条，失败 ${fail} 条`);
    await loadVoiceRecords($('#voiceRecordsRefreshBtn'), true);
  } catch (e) {
    toast(`批量 ASR 失败：${e.message}`, 'error', 9000);
  }
  finally { setBusy(button, false); }
}

function voiceListRow(x) {
  const text = x.text || x.title || '';
  const tags = (x.tags || []).slice(0, 5);
  const tagHtml = tags.length ? tags.map(t => '<span>' + escapeHtml(t) + '</span>').join('') : '<small>无标签</small>';
  const match = x.match_reason ? `<small class="voice-match">${escapeHtml(x.match_reason)}</small>` : '';
  return `<div class="voice-list-row" data-voice-id="${x.id}">
    <div class="voice-list-index">${x.list_index || '--'}</div>
    <div class="voice-list-content">${contentBlock('语音内容', text, '语音素材 #' + x.id + ' 内容')}</div>
    <div class="voice-list-pack"><strong>${escapeHtml(x.pack_name || '未命名语音包')}</strong><small>${escapeHtml(x.category || '未分类')}</small><small>#${x.id} · ${(x.file_ext || 'voice').toUpperCase()}</small></div>
    <div class="voice-list-player"><audio class="voice-audio" controls preload="none" src="/api/voicepacks/audio?id=${encodeURIComponent(x.id)}"></audio><small>${Math.round((x.duration_ms || 0) / 1000)} 秒 · ${Math.round((x.size || 0) / 1024)} KB · 使用 ${x.usage_count || 0} 次</small></div>
    <div class="voice-list-tags">${match}${tagHtml}</div>
    <div class="voice-list-actions"><button class="link-btn voice-send" data-voice-id="${x.id}">发送</button><button class="link-btn voice-copy" data-text="${escapeHtml(text)}">复制</button></div>
  </div>`;
}

function bindVoiceListActions(root) {
  $$('.voice-send', root).forEach(btn => btn.onclick = () => sendVoicepack(btn));
  $$('.voice-copy', root).forEach(btn => btn.onclick = async () => { await navigator.clipboard.writeText(btn.dataset.text || ''); toast('语音文案已复制'); });
  $$('.media-expand', root).forEach(btn => btn.onclick = () => previewContent(btn));
}

function renderVoiceVirtualList(items) {
  const root = $('#voiceResults');
  const rowHeight = 82;
  const overscan = 8;
  root.innerHTML = `<div class="voice-list voice-list-virtual">
    <div class="voice-list-head"><span>序号</span><span>语音内容</span><span>所属语音包</span><span>试听</span><span>匹配/标签</span><span>操作</span></div>
    <div class="voice-list-scroll"><div class="voice-list-spacer"></div><div class="voice-list-window"></div></div>
  </div>`;
  const spacer = $('.voice-list-spacer', root);
  const windowEl = $('.voice-list-window', root);
  spacer.style.height = `${items.length * rowHeight}px`;

  let frame = 0;
  const paint = () => {
    frame = 0;
    const top = Math.max(0, root.scrollTop - 34);
    const visible = Math.ceil(root.clientHeight / rowHeight) + overscan * 2;
    const start = Math.max(0, Math.floor(top / rowHeight) - overscan);
    const end = Math.min(items.length, start + visible);
    windowEl.style.transform = `translateY(${start * rowHeight}px)`;
    windowEl.innerHTML = items.slice(start, end).map(voiceListRow).join('');
    bindVoiceListActions(windowEl);
  };
  root.onscroll = () => {
    if (!frame) frame = requestAnimationFrame(paint);
  };
  paint();
}

function renderVoiceCategories(packs) {
  const oldCategory = $('#voiceCategory').value;
  const oldPack = $('#voicePackFilter').value;
  const cats = [...new Set((packs || []).map(x => x.category).filter(Boolean))].sort();
  $('#voiceCategory').innerHTML = '<option value="">全部文件夹</option>' + cats.map(x => `<option value="${escapeHtml(x)}">${escapeHtml(x)}</option>`).join('');
  if ([...$('#voiceCategory').options].some(x => x.value === oldCategory)) $('#voiceCategory').value = oldCategory;
  const visiblePacks = (packs || []).filter(x => !$('#voiceCategory').value || x.category === $('#voiceCategory').value);
  $('#voicePackFilter').innerHTML = '<option value="">全部语音包</option>' + visiblePacks.map(x => `<option value="${x.id}">${x.sequence || ''}. ${escapeHtml(x.name || '未命名语音包')} · ${x.item_count || 0} 条</option>`).join('');
  if ([...$('#voicePackFilter').options].some(x => x.value === oldPack)) $('#voicePackFilter').value = oldPack;
}

async function loadVoicepacks(button, silent = false) {
  if (button) setBusy(button, true, '读取中…');
  const chip = $('#voiceStatusChip');
  if (chip) { chip.textContent = '读取中'; chip.className = 'health-chip warn'; }
  try {
    const r = await api('/api/voicepacks', { method: 'POST', body: JSON.stringify({ category: $('#voiceCategory').value, pack_id: $('#voicePackFilter').value || 0, query: $('#voiceQuery').value.trim(), limit: 50000 }) });
    renderVoiceCategories(r.packs || []);
    const stats = r.stats || {};
    const statNodes = $$('#voiceStats strong');
    if (statNodes.length >= 3) { statNodes[0].textContent = stats.total_packs || 0; statNodes[1].textContent = stats.total_items || 0; statNodes[2].textContent = (stats.categories || []).length; }
    $('#voiceTitle').textContent = '语音素材库';
    $('#voiceSub').textContent = `当前显示 ${r.count} 条 / 当前筛选 ${r.matched_count ?? r.count} 条 / 全部 ${stats.total_items || 0} 条；按文件夹和语音包筛选，内容可点击展开`;
    $('#voiceMode').textContent = r.count ? 'VOICE' : 'EMPTY';
    if (r.items.length) renderVoiceVirtualList(r.items);
    else $('#voiceResults').innerHTML = '<div class="voice-empty">暂无语音素材。请先扫描并导入语音包总目录。</div>';
    if (chip) { chip.textContent = `语音 ${r.count} 条`; chip.className = 'health-chip good'; }
  } catch (e) {
    if (chip) { chip.textContent = '读取失败'; chip.className = 'health-chip warn'; }
    if (!silent) toast(`语音包读取失败：${e.message}`, 'error');
  } finally { if (button) setBusy(button, false); }
}

async function recommendVoicepacks(button) {
  const query = $('#voiceQuery').value.trim();
  if (!query) { toast('请先输入想表达的内容，例如“别着急”或“谢谢”', 'error'); return; }
  setBusy(button, true, '匹配中…');
  try {
    const r = await api('/api/voicepacks/recommend', { method: 'POST', body: JSON.stringify({ query, category: $('#voiceCategory').value, pack_id: $('#voicePackFilter').value || 0, limit: 8 }) });
    const names = (r.candidates || []).slice(0, 3).map(x => x.title || x.text).filter(Boolean);
    toast(names.length ? `找到 ${r.count} 条候选：${names.join('、')}` : '没有找到高置信度候选，请换成更接近语音内容的描述', names.length ? 'success' : 'error', 8000);
    await loadVoicepacks(null, true);
  } catch (e) { toast(`推荐失败：${e.message}`, 'error', 8000); }
  finally { setBusy(button, false); }
}

async function planVoicepacks(button) {
  setBusy(button, true, '扫描中…');
  try {
    const paths = $('#voiceImportPaths').value.split(/\r?\n/).map(x => x.trim()).filter(Boolean);
    if (!paths.length) throw new Error('请先填写来源路径');
    const r = await api('/api/voicepacks/plan', { method: 'POST', body: JSON.stringify({ paths, category: $('#voiceImportCategory').value.trim() }) });
    const errors = (r.errors || []).map(x => '<div class="voice-plan-error">' + escapeHtml(x.path) + '：' + escapeHtml(x.error) + '</div>').join('');
    const groups = (r.groups || []).map(x => '<li><strong>' + escapeHtml(x.pack_name) + '</strong><span>' + escapeHtml(x.category) + ' · ' + x.count + ' 条</span><small>' + x.samples.map(escapeHtml).join('、') + '</small></li>').join('');
    $('#voiceImportPlan').innerHTML = '<strong>扫描到 ' + r.total + ' 条，' + (r.groups?.length || 0) + ' 个语音包</strong>' + (groups ? '<ul>' + groups + '</ul>' : '') + errors;
    toast(r.total ? '扫描完成：' + r.total + ' 条语音' : '没有扫描到可导入的语音文件', r.total ? 'success' : 'error');
  } catch (e) { toast('扫描失败：' + e.message, 'error', 9000); }
  finally { setBusy(button, false); }
}

async function importVoicepacks(button) {
  setBusy(button, true, '导入中…');
  try {
    const paths = $('#voiceImportPaths').value.split(/\r?\n/).map(x => x.trim()).filter(Boolean);
    if (!paths.length) throw new Error('请先填写来源路径');
    const r = await api('/api/voicepacks/import', { method: 'POST', body: JSON.stringify({ paths, category: $('#voiceImportCategory').value.trim() }) });
    toast(`导入完成：新增 ${r.imported} 条，跳过 ${r.skipped} 条，错误 ${r.errors?.length || 0} 个`, r.errors?.length ? 'error' : 'success', 9000);
    if (r.pack_summary?.length) $('#voiceImportPlan').innerHTML = '<strong>最近一次导入</strong><ul>' + r.pack_summary.map(x => '<li><strong>' + escapeHtml(x.pack_name) + '</strong><span>新增 ' + x.imported + ' · 跳过 ' + x.skipped + '</span></li>').join('') + '</ul>';
    await loadVoicepacks($('#voiceRefreshBtn'), true);
  } catch (e) { toast(`导入语音包失败：${e.message}`, 'error', 12000); }
  finally { setBusy(button, false); }
}

async function sendVoicepack(button) {
  setBusy(button, true, '发送中');
  try {
    const r = await api('/api/voicepacks/send', { method: 'POST', body: JSON.stringify({ id: button.dataset.voiceId, group_id: $('#voiceTargetGroup').value }) });
    toast(`已发送语音：${r.item?.title || button.dataset.voiceId} · ${r.send?.latency_ms || 0}ms`);
    await loadVoicepacks(null, true);
  } catch (e) { toast(`语音发送失败：${e.message}`, 'error', 12000); }
  finally { setBusy(button, false); }
}

function faceCard(x) {
  const tags = (x.tags || []).slice(0, 8);
  const keywords = (x.keywords || []).slice(0, 8);
  const src = x.data_url || '';
  const title = x.image_summary || x.ocr_text || '未解析表情包';
  return `<article class="face-card media-card" data-face-id="${x.id}">
    <div class="media-thumb ${src ? '' : 'empty'}">
      ${src ? `<img src="${src}" alt="face">` : '<span>GIF</span>'}
    </div>
    <div class="media-info">
      <div class="media-title"><strong>face #${x.id}</strong><span>${escapeHtml(x.status || 'indexed')}</span></div>
      ${contentBlock('摘要', title, `表情 #${x.id} 摘要`)}
      ${contentBlock('OCR', x.ocr_text || '无', `表情 #${x.id} OCR`)}
      ${contentBlock('文件', x.file || '', `表情 #${x.id} 文件`)}
      ${tags.length || keywords.length ? `<div class="tag-list">${[...tags, ...keywords].slice(0, 12).map(t => `<span>${escapeHtml(t)}</span>`).join('')}</div>` : ''}
      <div class="media-actions">
        ${src ? `<button class="link-btn face-preview" data-src="${escapeHtml(src)}" data-title="face #${x.id}">预览</button>` : ''}
        <button class="link-btn face-send" data-face-id="${x.id}">发送到目标群</button>
        <button class="link-btn media-ocr" data-media-id="${x.id}">重新解析</button>
        <button class="link-btn media-edit" data-media-id="${x.id}" data-summary="${escapeHtml(x.image_summary || '')}" data-ocr="${escapeHtml(x.ocr_text || '')}" data-tags="${escapeHtml((x.tags || []).join('，'))}" data-keywords="${escapeHtml((x.keywords || []).join('，'))}">编辑</button>
      </div>
    </div>
  </article>`;
}

async function loadFaces(button, silent = false) {
  if (button) setBusy(button, true, '读取中…');
  const chip = $('#faceStatusChip');
  if (chip) { chip.textContent = '读取中'; chip.className = 'health-chip warn'; }
  try {
    const r = await api('/api/faces', { method: 'POST', body: JSON.stringify({ group_id: $('#faceGroup').value, query: $('#faceQuery').value.trim(), limit: 500 }) });
    $('#faceTitle').textContent = '表情素材库';
    $('#faceSub').textContent = `当前去重显示 ${r.count} 个 face / GIF 表情`;
    $('#faceMode').textContent = r.count ? 'FACE' : 'EMPTY';
    $('#faceResults').innerHTML = r.items.length ? `<div class="face-grid media-gallery">${r.items.map(faceCard).join('')}</div>` : '<div class="voice-empty">暂无 face 表情包。群里收到表情包后会自动进入这里。</div>';
    $$('.face-preview', $('#faceResults')).forEach(btn => btn.onclick = () => previewMedia(btn));
    $$('.face-send', $('#faceResults')).forEach(btn => btn.onclick = () => sendFace(btn));
    $$('.media-ocr', $('#faceResults')).forEach(btn => btn.onclick = () => ocrMedia(btn));
    $$('.media-edit', $('#faceResults')).forEach(btn => btn.onclick = () => editMedia(btn));
    $$('.media-expand', $('#faceResults')).forEach(btn => btn.onclick = () => previewContent(btn));
    if (chip) { chip.textContent = `表情 ${r.count} 个`; chip.className = 'health-chip good'; }
  } catch (e) {
    if (chip) { chip.textContent = '读取失败'; chip.className = 'health-chip warn'; }
    if (!silent) toast(`表情包读取失败：${e.message}`, 'error');
  } finally { if (button) setBusy(button, false); }
}

async function sendFace(button) {
  setBusy(button, true, '发送中');
  try {
    const r = await api('/api/faces/send', { method: 'POST', body: JSON.stringify({ id: button.dataset.faceId, group_id: $('#faceTargetGroup').value }) });
    toast(`已发送表情包：#${r.item?.id || button.dataset.faceId} · ${r.send?.latency_ms || 0}ms`);
  } catch (e) { toast(`表情包发送失败：${e.message}`, 'error', 12000); }
  finally { setBusy(button, false); }
}

async function loadGroupMemory(button) {
  setBusy(button, true, '读取中…');
  try {
    const r = await api('/api/memory/group', { method: 'POST', body: JSON.stringify({ group_id: $('#memoryGroup').value }) });
    $('#memorySummary').value = r.summary || '';
    toast('群长期记忆已读取');
  } catch (e) { toast(`读取群记忆失败：${e.message}`, 'error'); }
  finally { setBusy(button, false); }
}

async function saveGroupMemory(button) {
  setBusy(button, true, '保存中…');
  try {
    await api('/api/memory/group/save', { method: 'POST', body: JSON.stringify({ group_id: $('#memoryGroup').value, summary: $('#memorySummary').value, facts: [] }) });
    toast('群长期记忆已保存');
  } catch (e) { toast(`保存群记忆失败：${e.message}`, 'error'); }
  finally { setBusy(button, false); }
}

async function rebuildMemory(button) {
  setBusy(button, true, '重建中…');
  try {
    const r = await api('/api/memory/rebuild', { method: 'POST', body: JSON.stringify({ scope: 'all', group_id: $('#memoryGroup').value }) });
    setMemoryMode('重建结果', '索引、媒体、人物画像已重新生成', 'REBUILT');
    $('#memoryResults').innerHTML = `<div class="memory-row"><pre>${escapeHtml(JSON.stringify(r, null, 2))}</pre></div>`;
    await refreshMemoryStats(true);
    toast('记忆索引和画像已重建');
  } catch (e) { toast(`重建失败：${e.message}`, 'error'); }
  finally { setBusy(button, false); }
}

async function importMemory(button) {
  setBusy(button, true, '导入中…');
  try {
    const raw = $('#memoryImportBox').value.trim();
    if (!raw) throw new Error('请先粘贴 JSON 数组');
    const items = JSON.parse(raw);
    const r = await api('/api/memory/import', { method: 'POST', body: JSON.stringify({ items }) });
    setMemoryMode('导入结果', `收到 ${r.received} 条，新增 ${r.inserted} 条`, 'IMPORT');
    $('#memoryResults').innerHTML = `<div class="memory-row"><pre>${escapeHtml(JSON.stringify(r, null, 2))}</pre></div>`;
    await refreshMemoryStats(true);
    toast(`导入完成：新增 ${r.inserted} 条`);
  } catch (e) { toast(`导入失败：${e.message}`, 'error', 8000); }
  finally { setBusy(button, false); }
}

async function exportMemory(button) {
  setBusy(button, true, '导出中…');
  try {
    const r = await api('/api/memory/export', { method: 'POST', body: JSON.stringify({ group_id: $('#memoryGroup').value, limit: 1000 }) });
    setMemoryMode('导出 JSON', '当前群记忆数据，可复制保存', 'EXPORT');
    $('#memoryResults').innerHTML = `<div class="memory-row"><pre>${escapeHtml(JSON.stringify(r, null, 2))}</pre></div>`;
    toast('已生成导出 JSON');
  } catch (e) { toast(`导出失败：${e.message}`, 'error'); }
  finally { setBusy(button, false); }
}

function formatLogTime(value) { if (!value) return new Date().toLocaleTimeString('zh-CN', { hour12: false }); return value.length > 10 ? value.slice(11) : value; }
function addLog(item) {
  state.logs.push(item); if (state.logs.length > 1200) state.logs.splice(0, 200); $('#logPulse').style.opacity = '1';
  if (!state.paused) renderLogs();
}

function renderLogs() {
  const enabled = new Set($$('.level-filters input:checked').map(x => x.value));
  const groupFilter = ($('#logGroupFilter')?.value || '').trim();
  const traceFilter = ($('#logTraceFilter')?.value || '').trim();
  const errorsOnly = $('#logErrorsOnly')?.checked;
  const sendOnly = $('#logSendOnly')?.checked;
  const rows = state.logs.filter(x => {
    if (state.source !== 'all' && x.source !== state.source) return false;
    if (!enabled.has(x.level)) return false;
    if (groupFilter && !String(x.group_id || x.message || '').includes(groupFilter)) return false;
    if (traceFilter && !String(x.trace_id || x.message || '').includes(traceFilter)) return false;
    if (errorsOnly && !['error', 'critical'].includes(x.level)) return false;
    if (sendOnly && !/send_group|ONEBOT SEND|send/i.test(`${x.event || ''} ${x.message || ''}`)) return false;
    return true;
  });
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
  $('#refreshBtn').onclick = () => { refreshStatus(); refreshTraces(); }; $('#refreshTraceBtn').onclick = () => refreshTraces(); $('#startAllBtn').onclick = e => action('start_all', e.currentTarget); $$('.action-btn').forEach(x => x.onclick = () => action(x.dataset.action, x));
  $('#channelSelect').onchange = e => selectChannel(e.target.value); $('#addChannelBtn').onclick = openChannelDialog; $('#deleteChannelBtn').onclick = deleteCurrentChannel;
  $('#testAllChannelsBtn').onclick = e => testAllChannels(e.currentTarget); $('#refreshHealthBtn').onclick = () => refreshChannelHealth();
  $('#channelProvider').onchange = e => applyProviderPreset(e.target.value); $('#newChannelProvider').onchange = e => applyProviderPreset(e.target.value, 'new');
  $('#closeChannelDialog').onclick = closeChannelDialog; $('#cancelChannelDialog').onclick = closeChannelDialog;
  $('#channelDialogForm').onsubmit = e => { e.preventDefault(); if (e.currentTarget.reportValidity()) addChannelFromDialog(); };
  $('#toggleKey').onclick = () => { $('#apiKey').type = $('#apiKey').type === 'password' ? 'text' : 'password'; };
  $('#modelsBtn').onclick = e => getModels(e.currentTarget); $('#quickTestBtn').onclick = e => quickTest(e.currentTarget);
  $('#ocrTestBtn').onclick = e => ocrTest(e.currentTarget); $('#toggleOcrKey').onclick = () => { $('#ocrApiKey').type = $('#ocrApiKey').type === 'password' ? 'text' : 'password'; };
  $('#asrTestBtn').onclick = e => asrTest(e.currentTarget); $('#toggleAsrKey').onclick = () => { $('#asrApiKey').type = $('#asrApiKey').type === 'password' ? 'text' : 'password'; };
  $('#temperature').oninput = e => { $('#temperatureValue').value = e.target.value; markDirty(); }; $('#systemPrompt').oninput = e => { $('#promptCount').textContent = e.target.value.length; markDirty(); }; $('#personality').oninput = e => { $('#personalityCount').textContent = e.target.value.length; markDirty(); };
  $('#aiForm').onsubmit = e => { e.preventDefault(); saveConfig(e.submitter); }; $('#groupForm').onsubmit = e => { e.preventDefault(); saveConfig(e.submitter); };
  $('#refreshGroupsBtn').onclick = () => loadDiscoveredGroups(); $('#saveAliasesBtn').onclick = e => saveAliases(e.currentTarget); $('#syncUiGroupsBtn').onclick = e => syncUiGroups(e.currentTarget); $('#enableSelectedGroupBtn').onclick = enableQuickSelectedGroup; $$('#aiForm input, #aiForm textarea, #aiForm select, #groupForm input').forEach(x => x.addEventListener('change', () => { updateRouteSummary(); markDirty(); }));
  $$('.test-run').forEach(x => x.onclick = () => runTest(x.dataset.test, x));
  $('#clearTestConsole').onclick = () => { $('#testConsole').innerHTML = '<p class="muted">// 测试控制台已清空</p>'; };
  $('#refreshMemoryBtn').onclick = () => refreshMemoryStats(); $('#searchMemoryBtn').onclick = e => searchMemory(e.currentTarget); $('#vectorSearchBtn').onclick = e => vectorSearch(e.currentTarget);
  $('#loadMembersBtn').onclick = e => loadMembers(e.currentTarget); $('#loadMediaBtn').onclick = e => loadMedia(e.currentTarget);
  $('#mediaRefreshBtn').onclick = e => loadMediaCenter(e.currentTarget); $('#mediaSearchBtn').onclick = e => loadMediaCenter(e.currentTarget); $('#mediaOpenMemoryBtn').onclick = () => showPage('memory');
  $$('#mediaGroup,#mediaType,#mediaStatus').forEach(x => x.addEventListener('change', () => loadMediaCenter(null, true)));
  $('#mediaQuery').addEventListener('keydown', e => { if (e.key === 'Enter') loadMediaCenter($('#mediaSearchBtn')); });
  $('#voiceRecordsRefreshBtn').onclick = e => loadVoiceRecords(e.currentTarget); $('#voiceRecordsSearchBtn').onclick = e => loadVoiceRecords(e.currentTarget); $('#voiceRecordsOpenMemoryBtn').onclick = () => showPage('memory');
  $('#voiceRecordsBatchAsrBtn').onclick = e => syncRecordTranscripts(e.currentTarget);
  $$('#voiceRecordsGroup,#voiceRecordsStatus').forEach(x => x.addEventListener('change', () => loadVoiceRecords(null, true)));
  $('#voiceRecordsQuery').addEventListener('keydown', e => { if (e.key === 'Enter') loadVoiceRecords($('#voiceRecordsSearchBtn')); });
  $('#voiceRefreshBtn').onclick = e => loadVoicepacks(e.currentTarget); $('#voiceSearchBtn').onclick = e => loadVoicepacks(e.currentTarget); $('#voiceRecommendBtn').onclick = e => recommendVoicepacks(e.currentTarget);
  $('#voicePlanBtn').onclick = e => planVoicepacks(e.currentTarget); $('#voiceImportBtn').onclick = e => importVoicepacks(e.currentTarget);
  $$('#voiceCategory,#voicePackFilter').forEach(x => x.addEventListener('change', () => {
    if (x.id === 'voiceCategory') $('#voicePackFilter').value = '';
    loadVoicepacks(null, true);
  }));
  $('#voiceQuery').addEventListener('keydown', e => { if (e.key === 'Enter') loadVoicepacks($('#voiceSearchBtn')); });
  $('#faceRefreshBtn').onclick = e => loadFaces(e.currentTarget); $('#faceSearchBtn').onclick = e => loadFaces(e.currentTarget);
  $$('#faceGroup').forEach(x => x.addEventListener('change', () => loadFaces(null, true)));
  $('#faceQuery').addEventListener('keydown', e => { if (e.key === 'Enter') loadFaces($('#faceSearchBtn')); });
  $('#loadGroupMemoryBtn').onclick = e => loadGroupMemory(e.currentTarget); $('#saveGroupMemoryBtn').onclick = e => saveGroupMemory(e.currentTarget);
  $('#rebuildMemoryBtn').onclick = e => rebuildMemory(e.currentTarget); $('#importMemoryBtn').onclick = e => importMemory(e.currentTarget); $('#exportMemoryBtn').onclick = e => exportMemory(e.currentTarget);
  $$('[data-mini-source]').forEach(x => x.onclick = () => { $$('[data-mini-source]').forEach(y => y.classList.toggle('active', y === x)); state.miniSource = x.dataset.miniSource; renderDashboardLogs(); });
  $$('.source-tabs button').forEach(x => x.onclick = () => { $$('.source-tabs button').forEach(y => y.classList.toggle('active', y === x)); state.source = x.dataset.source; renderLogs(); });
  $$('.level-filters input').forEach(x => x.onchange = renderLogs); $$('#logGroupFilter,#logTraceFilter,#logErrorsOnly,#logSendOnly').forEach(x => x.addEventListener('input', renderLogs)); $$('#logErrorsOnly,#logSendOnly').forEach(x => x.addEventListener('change', renderLogs)); $('#clearLogs').onclick = () => { state.logs = []; renderLogs(); };
  $('#pauseLogs').onclick = e => { state.paused = !state.paused; e.currentTarget.textContent = state.paused ? '继续' : '暂停'; if (!state.paused) renderLogs(); };
}

async function init() {
  bind(); connectLogs();
  try { fillConfig(await api('/api/config')); } catch (e) { toast(`配置加载失败：${e.message}`, 'error'); }
  await refreshStatus(); await refreshTraces(true); setInterval(() => refreshStatus(true), 5000); setInterval(() => refreshTraces(true), 5000); setInterval(() => refreshChannelHealth(true), 15000);
  refreshMemoryStats(true);
  setInterval(() => { if ($('#page-groups').classList.contains('active') && !state.dirty) loadDiscoveredGroups(true); }, 30000);
  setInterval(() => { if ($('#page-media').classList.contains('active')) loadMediaCenter(null, true); }, 5000);
  setInterval(() => { if ($('#page-voice-records')?.classList.contains('active')) loadVoiceRecords(null, true); }, 5000);
}
init();
