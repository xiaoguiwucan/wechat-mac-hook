const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const state = { config: null, status: null, brainConfig: null, replyTasks: [], faceItems: [], pokeFaceIds: new Set(), channels: [], selectedChannelId: '', channelHealth: {}, groupCatalog: [], groupMemberCatalog: {}, groupMemberCatalogMeta: {}, ignoredGroupMembers: {}, groupPersonalities: {}, groupAdmins: {}, groupAdminRoles: {}, groupAdminPermissions: [], groupAdminPreviewUserId: '', persona: { members: [], selectedUserId: '', detail: null, tab: 'overview', refreshTimer: null }, dirty: false, logs: [], source: 'all', miniSource: 'all', paused: false, eventSource: null, traceDiagnostic: null };
let voiceImportFiles = [];
let voiceImportUploadedPaths = [];
const pageMeta = {
  overview: ['运行总览', '当前微信、OneBot 与 AI 服务'], ai: ['模型配置', '对话、生图、OCR 与 ASR 模型配置'],
  groups: ['群聊策略', '目标群与自动回复规则'], brain: ['群聊大脑', '接话门槛、七维评分与并发策略'],
  personas: ['USR 用户画像', '永久档案、行为统计、关系与原话证据'],
  'reply-tasks': ['实时对话', '回复线程、任务阶段与耗时'], vector: ['本地向量', 'oMLX 模型、检索与永久记忆回填'],
  tests: ['评估看板', '验证模型、Hook 与完整回调链路'], media: ['图片图库', '图片 OCR、文件/视频索引与媒体记忆'],
  'voice-records': ['语音内容', '群语音泡、ASR 转写与语音记忆'],
  voices: ['语音包管理', 'silk / zip / zip1 导入与语音发送'],
  faces: ['表情包管理', 'face / GIF 动图收藏、解析与发送'],
  memory: ['记忆数据库', '聊天记录、人物画像与群长期记忆'], logs: ['完整日志', 'AI 回复与 OneBot 运行输出']
};
const pageWorkspace = {
  overview: 'runtime',
  ai: 'intelligence', groups: 'intelligence', brain: 'intelligence', 'reply-tasks': 'intelligence', tests: 'intelligence',
  personas: 'memory', vector: 'memory', memory: 'memory',
  media: 'assets', 'voice-records': 'assets', voices: 'assets', faces: 'assets',
  logs: 'diagnostics'
};
const workspaceDefaultPage = { runtime: 'overview', intelligence: 'ai', memory: 'personas', assets: 'media', diagnostics: 'logs' };
const factorLabels = { involvement: '参与关联', continuity: '对话连续性', memory: '永久记忆关联', value: '回复增量', humor: '玩笑与造梗', emotion: '情绪适配', timing: '时机完整度' };
const modifierLabels = { same_member_followup: '同成员 120 秒追问', exact_meme: '精确命中群梗', high_vector: '高相似历史向量', media_match: '匹配语音或表情', useful_after_silence: '沉默 20 分钟后有内容', unfinished_fast_exchange: '快速对话尚未完整', growing_burst: '30 秒消息持续增长', already_answered: '问题已完整回答', low_information: '短词/通知/链接/重复' };
const orbitNodeDetails = {
  person: { title: '人物与外号', icon: 'ph-user-focus', confidence: .92, source: '成员资料 / 外号别名库', recalled: 28, injected: 8, similarity: .72, evidence: [['老', '老油条（张伟）', '命中外号、当前成员和历史关系', .91], ['山', '山民小陈', '与当前话题存在稳定互动记录', .76], ['攻', '攻略王阿杰', '相关人物画像中包含出行偏好', .69]], timeline: [['23:12:44', '老油条', '上次团建去的千岛湖，风景不错但人有点多。'], ['22:47:01', '山民小陈', '推荐个徒步的地方，就在古道附近。']] },
  meme: { title: '群梗 / 暗语', icon: 'ph-chats-circle', confidence: .88, source: '永久群梗 / 经典原话', recalled: 36, injected: 7, similarity: .78, evidence: [['画', '画饼梗', '经典原话与当前“团建计划”语境重合', .90], ['鸽', '别又鸽了', '群内高频调侃表达被精确命中', .82], ['班', '值班仙人', '成员外号与值班事件同时出现', .71]], timeline: [['23:08:12', '风', '这次不是画饼，地点真定了。'], ['21:42:36', '老油条', '上回说团建最后是谁鸽了来着。']] },
  fts: { title: '对话全文 FTS', icon: 'ph-text-columns', confidence: .91, source: 'messages_fts / BM25', recalled: 42, injected: 9, similarity: .74, evidence: [['团', '团建地点讨论', '“团建、地点、推荐”文本精确匹配', .93], ['湖', '千岛湖原话', '命中历史消息中的地点原文', .86], ['徒', '古道徒步建议', '关键词与同义表达共同命中', .75]], timeline: [['23:12:44', '老油条', '上次团建去的千岛湖，风景不错但人有点多。'], ['22:47:01', '山民小陈', '推荐个徒步的地方，就在古道附近。']] },
  embedding: { title: '向量召回', icon: 'ph-vector-three', confidence: .89, source: 'Qwen3 Embedding / sqlite-vec', recalled: 50, injected: 12, similarity: .81, evidence: [['语', '语义相似历史', '不依赖相同字面，按完整语境召回', .89], ['旧', '远期团建记录', '永久向量记忆命中较早历史', .84], ['图', '相关图片摘要', 'OCR 与图片语义进入同一候选池', .73]], timeline: [['2026-05-18', '攻略王阿杰', '如果人多，最好选交通方便又能分组活动的地方。'], ['2026-02-03', '山民小陈', '古道附近有露营点，团建也能安排。']] },
  reranker: { title: 'Reranker 重排', icon: 'ph-arrows-down-up', confidence: .86, source: 'Qwen3 Reranker / 自适应两批', recalled: 24, injected: 10, similarity: .84, evidence: [['01', '人物 + 时间组合', '同时满足人物、时间和话题约束', .92], ['02', '经典原话', '比普通语义相似结果更适合注入', .87], ['03', '相关图片摘要', '补充视觉证据且不重复文本结论', .78]], timeline: [['23:33:31', '系统', '首批 12 条分差较小，触发第二批重排。'], ['23:33:32', '系统', '两批校准后保留 10 条证据。']] },
  voice: { title: '语音匹配', icon: 'ph-waveform', confidence: .79, source: '语音标题 / 标签 / 语义向量', recalled: 16, injected: 3, similarity: .79, evidence: [['笑', '“笑死我了”语音', '适配轻松调侃语境', .84], ['走', '“走一个”语音', '适合作为团建提议回应', .76], ['行', '“这可以”语音', '简短确认且不会重复文字', .71]], timeline: [['23:33:32', '媒介决策', '语音适配分 79，进入概率抽样。'], ['23:33:32', '素材索引', '候选来自语音包标题、分类和标签。']] },
  face: { title: '表情匹配', icon: 'ph-smiley', confidence: .66, source: 'OCR / 别名 / 情绪与意图', recalled: 18, injected: 4, similarity: .66, evidence: [['哈', '熊猫头憋笑', '情绪标签与搞笑反应吻合', .78], ['走', '走开啊别拍我', 'OCR 与别名精确匹配', .74], ['看', '白色小人惊讶', '适合对新地点建议作反应', .66]], timeline: [['23:33:32', '媒介决策', '表情适配分 66，低于语音候选。'], ['23:33:32', '素材索引', '已检查文件可用性和历史发送成功率。']] }
};
const orbitalPageDesigns = {
  overview: { tone: 'cyan', icon: 'ph-gauge', code: 'RUNTIME ORBIT', title: '运行轨道总控', description: '把微信、OneBot、AI 与消息链路放在同一条实时运行轨道中。', state: '核心服务同步中', nodes: [['ph-wechat-logo', '当前微信', 'INSTANCE 1', '官方安装'], ['ph-plugs-connected', 'OneBot', '58080', '消息与媒体'], ['ph-brain', 'AI 网关', '36060', '生成与调度']] },
  ai: { tone: 'violet', icon: 'ph-cpu', code: 'MODEL ORBIT', title: '多模型神经中枢', description: '统一编排对话、生图、OCR 与 ASR 渠道，保存后实时切换运行链路。', state: '配置热加载', nodes: [['ph-arrows-clockwise', '故障切换', 'AUTO', '渠道健康'], ['ph-image-square', 'AI 生图', 'IMAGE', '图片生成'], ['ph-eye', '视觉理解', 'OCR', '图片解析'], ['ph-waveform', '语音理解', 'ASR', '实时转写']] },
  vector: { tone: 'cyan', icon: 'ph-vector-three', code: 'VECTOR ORBIT', title: '本地向量引擎', description: 'Embedding 召回与 Reranker 精排共同驱动永久记忆检索。', state: 'oMLX 本地推理', nodes: [['ph-cube', '向量维度', '4096D', '完整精度'], ['ph-magnifying-glass', '初始召回', 'TOP 60', '多路融合'], ['ph-arrows-down-up', '精排注入', '12–24', '自适应扩批']] },
  'reply-tasks': { tone: 'mint', icon: 'ph-chats-circle', code: 'THREAD ORBIT', title: '多线程回复调度', description: '跨群并行、同线程串行，每个问题都绑定原消息和完整阶段。', state: '实时任务流', nodes: [['ph-stack', '全局工作池', '8', '并行任务'], ['ph-users-three', '单群并发', '3', '线程隔离'], ['ph-broadcast', '状态刷新', '<1s', '统一事件流']] },
  groups: { tone: 'violet', icon: 'ph-users-three', code: 'SOCIAL ORBIT', title: '群聊策略矩阵', description: '按群控制权限、回复边界与成员屏蔽，保存即刻生效。', state: '群级热更新', nodes: [['ph-shield-check', '群聊授权', 'ACL', '目标群隔离'], ['ph-user-minus', '成员屏蔽', 'LIVE', '历史目录'], ['ph-sliders-horizontal', '回复规则', 'HOT', '实时应用']] },
  tests: { tone: 'amber', icon: 'ph-chart-line-up', code: 'EVALUATION ORBIT', title: '链路评估实验场', description: '使用真实请求、真实耗时与真实回调验证完整机器人链路。', state: '诊断沙盒', nodes: [['ph-lightning', '模型探针', 'REAL', '真实调用'], ['ph-path', '链路追踪', 'TRACE', '逐段定位'], ['ph-check-circle', '回调验证', 'E2E', '发送闭环']] },
  media: { tone: 'amber', icon: 'ph-images', code: 'VISION ORBIT', title: '视觉素材星库', description: '图片、文件与视频经过解析、去重和索引后进入永久记忆。', state: '视觉索引在线', nodes: [['ph-eye', '图片解析', 'OCR', '文字与摘要'], ['ph-fingerprint', '文件去重', 'HASH', '稳定素材键'], ['ph-database', '永久索引', 'FTS+VEC', '语义召回']] },
  'voice-records': { tone: 'violet', icon: 'ph-waveform', code: 'VOICE MEMORY ORBIT', title: '群语音记忆轨道', description: '原始语音、ASR 转写与上下文共同构成可检索的长期语音记忆。', state: 'ASR 队列在线', nodes: [['ph-microphone', '原始语音', 'RAW', 'OneBot 捕获'], ['ph-waveform', '智能转写', 'ASR', '中文语义'], ['ph-vector-three', '记忆入库', 'VEC', '永久检索']] },
  voices: { tone: 'violet', icon: 'ph-speaker-high', code: 'VOICE PACK ORBIT', title: '语音包素材引擎', description: '按分组管理 SILK 素材，通过标题、分类与语义选择最合适的接话。', state: '语音检索在线', nodes: [['ph-package', '语音分组', 'PACK', '独立管理'], ['ph-file-audio', '原始格式', 'SILK', '快速发送'], ['ph-magnifying-glass', '语义推荐', 'VECTOR', '上下文匹配']] },
  faces: { tone: 'violet', icon: 'ph-smiley', code: 'REACTION ORBIT', title: '表情反应素材库', description: '用 OCR、别名、情绪和意图快速找到符合当前语境的表情。', state: '表情检索在线', nodes: [['ph-fingerprint', '去重收藏', 'HASH', '稳定 face_key'], ['ph-smiley', '情绪意图', 'TAG', '上下文适配'], ['ph-vector-three', '语义检索', 'VEC', '快速命中']] },
  personas: { tone: 'mint', icon: 'ph-user-focus', code: 'PERSONA ORBIT', title: '永久人物认知图谱', description: '按群隔离保存成员档案、行为、关系、群梗与可追溯原话。', state: '永久画像系统', nodes: [['ph-clock-counter-clockwise', '行为统计', '7×24', '全历史'], ['ph-share-network', '关系网络', 'TOP 20', '真实互动'], ['ph-quotes', '原话证据', 'TRACE', '可追溯']] },
  memory: { tone: 'cyan', icon: 'ph-database', code: 'MEMORY ORBIT', title: '永久记忆数据库', description: '消息、人物、群梗、媒体与向量共同组成严格群隔离的记忆层。', state: 'WAL 永久存储', nodes: [['ph-text-columns', '全文检索', 'FTS5', '精确命中'], ['ph-vector-three', '语义召回', 'VECTOR', '久远记忆'], ['ph-lock-key', '群聊隔离', 'STRICT', '禁止串群']] },
  logs: { tone: 'mint', icon: 'ph-terminal-window', code: 'TELEMETRY ORBIT', title: '实时遥测终端', description: '聚合 AI 与 OneBot 事件，以 trace_id 还原每一次回复链路。', state: '日志流连接中', nodes: [['ph-broadcast', '事件通道', 'SSE', '实时推送'], ['ph-funnel', '多维筛选', 'LIVE', '群与级别'], ['ph-path', '链路标识', 'TRACE_ID', '完整定位']] }
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

function applyWorkspace(name, openDefault = false) {
  const workspace = name || 'runtime';
  document.body.dataset.workspace = workspace;
  $$('.workspace-item').forEach(x => x.classList.toggle('active', x.dataset.workspace === workspace));
  $$('[data-workspace-group]').forEach(x => x.classList.toggle('active', x.dataset.workspaceGroup === workspace));
  if (openDefault) showPage(workspaceDefaultPage[workspace] || 'overview');
}

function applyTheme(theme, persist = false) {
  const next = theme === 'light' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  document.body.dataset.theme = next;
  $$('[data-theme-value]').forEach(x => {
    const active = x.dataset.themeValue === next;
    x.classList.toggle('active', active);
    x.setAttribute('aria-pressed', String(active));
  });
  if (persist) localStorage.setItem('wxconsole-theme', next);
}

function setupUiTooltips() {
  $$('button[title], [role="button"][title]').forEach(element => {
    const label = element.getAttribute('title')?.trim();
    if (!label) return;
    element.dataset.uiTooltip = label;
    if (!element.getAttribute('aria-label') && !element.textContent.trim()) element.setAttribute('aria-label', label);
  });
}

function decorateOrbitalPages() {
  Object.entries(orbitalPageDesigns).forEach(([pageName, design]) => {
    const page = $('#page-' + pageName); if (!page || $('[data-orbital-banner]', page)) return;
    const banner = document.createElement('section');
    banner.className = 'orbital-page-banner'; banner.dataset.orbitalBanner = pageName; banner.dataset.orbitTone = design.tone;
    banner.innerHTML = `<div class="orbital-banner-copy"><span>${escapeHtml(design.code)}</span><div class="orbital-banner-heading"><i class="ph ${escapeHtml(design.icon)}"></i><div><h2>${escapeHtml(design.title)}</h2><p>${escapeHtml(design.description)}</p></div></div><b><i class="live-dot"></i>${escapeHtml(design.state)}</b></div><div class="orbital-signal-track">${design.nodes.map((node, index) => `<article data-orbital-signal="${index}"><i class="ph ${escapeHtml(node[0])}"></i><div><span>${escapeHtml(node[1])}</span><strong>${escapeHtml(node[2])}</strong><small>${escapeHtml(node[3])}</small></div></article>`).join('')}</div><footer><span><i class="ph ph-orbit"></i> ORBITAL NEURAL MODULE</span><b>LIVE · LOCAL · ISOLATED</b></footer>`;
    const anchor = $('.page-bar', page);
    if (anchor) anchor.after(banner); else page.prepend(banner);
    $$('.panel', page).forEach((panel, index) => { const number = String(index + 1).padStart(2, '0'); panel.dataset.orbitPanel = number; const head = $('.panel-head', panel); if (head) head.dataset.orbitPanel = number; });
  });
}

function decorateOrbitalWorkbenches() {
  const sectionNames = [
    ['.memory-hero', '记忆概览'], ['.persona-profile', '个人档案'], ['.persona-behavior', '行为统计'],
    ['.persona-relations', '关系网络'], ['.persona-evidence', '证据时间线'], ['.trace-panel', '链路诊断'],
    ['.operations', '后台操作'], ['.dashboard-terminal', '实时日志'], ['.test-controls', '注入参数'],
    ['.test-console', '运行终端'], ['.provider-panel', '模型渠道'], ['.reply-task-panel', '任务时间线'],
    ['.media-filter-panel', '筛选控制台'], ['.media-gallery-panel', '素材记录'], ['.voice-control-panel', '素材控制台'],
    ['.voice-gallery-panel', '素材列表'], ['.memory-control-panel', '记忆控制台'], ['.memory-results-panel', '记忆视图'],
    ['.route-panel', '群聊授权'], ['.member-blacklist-panel', '成员屏蔽'], ['.rules-panel', '回复规则']
  ];
  Object.keys(orbitalPageDesigns).forEach(pageName => {
    const page = $('#page-' + pageName); if (!page || $('[data-orbit-section-map]', page)) return;
    const panels = $$('.panel', page).filter(panel => !panel.closest('dialog'));
    panels.forEach((panel, index) => {
      const knownName = sectionNames.find(([selector]) => panel.matches(selector))?.[1];
      const visibleTitle = $('.panel-head h2, .terminal-head h2, .console-head strong', panel)?.textContent?.trim();
      const title = knownName || visibleTitle || `工作模块 ${index + 1}`;
      panel.id ||= `orbit-${pageName}-section-${index + 1}`;
      panel.dataset.orbitSection = title;
      if (panel.matches('.provider-panel,.media-filter-panel,.voice-control-panel,.memory-control-panel,.test-controls')) panel.dataset.orbitKind = 'control';
      else if (panel.matches('.dashboard-terminal,.test-console,.full-terminal')) panel.dataset.orbitKind = 'terminal';
      else if (panel.matches('.voice-gallery-panel,.media-gallery-panel,.memory-results-panel,.reply-task-panel')) panel.dataset.orbitKind = 'stream';
      else panel.dataset.orbitKind = 'module';
    });
    $$('.field', page).forEach(field => field.dataset.orbitField = '');
    $$('.toggle-row,.compact-toggle', page).forEach(toggle => toggle.dataset.orbitToggle = '');
    $$('.bar-actions,.channel-actions,.voice-search-actions,.memory-button-grid,.task-filters,.log-toolbar', page).forEach(actions => actions.dataset.orbitControls = '');
    if (panels.length < 2) return;
    const map = document.createElement('nav');
    map.className = 'orbit-section-map'; map.dataset.orbitSectionMap = pageName; map.setAttribute('aria-label', `${pageMeta[pageName]?.[0] || ''}页内模块`);
    map.innerHTML = `<span><i class="ph ph-circles-three-plus"></i> MODULE MAP</span><div>${panels.map((panel, index) => `<button type="button" class="${index === 0 ? 'active' : ''}" data-orbit-section-target="${escapeHtml(panel.id)}"><b>${String(index + 1).padStart(2, '0')}</b>${escapeHtml(panel.dataset.orbitSection)}</button>`).join('')}</div>`;
    const banner = $('[data-orbital-banner]', page);
    if (banner) banner.after(map); else page.prepend(map);
    $$('[data-orbit-section-target]', map).forEach(button => button.addEventListener('click', () => {
      const target = document.getElementById(button.dataset.orbitSectionTarget); if (!target) return;
      $$('button', map).forEach(item => item.classList.toggle('active', item === button));
      target.scrollIntoView({ behavior: 'smooth', block: 'start' });
      target.classList.remove('orbit-section-focus'); void target.offsetWidth; target.classList.add('orbit-section-focus');
      setTimeout(() => target.classList.remove('orbit-section-focus'), 900);
    }));
    if ('IntersectionObserver' in window) {
      const observer = new IntersectionObserver(entries => {
        if (!entries.some(entry => entry.isIntersecting)) return;
        const guide = 104;
        const visible = panels.map((panel, index) => ({ panel, index, rect: panel.getBoundingClientRect() }))
          .filter(item => item.rect.bottom > guide && item.rect.top < innerHeight)
          .sort((a, b) => Math.abs(a.rect.top - guide) - Math.abs(b.rect.top - guide) || a.index - b.index)[0];
        if (!visible) return;
        $$('button', map).forEach(button => button.classList.toggle('active', button.dataset.orbitSectionTarget === visible.panel.id));
      }, { rootMargin: '-96px 0px -52% 0px', threshold: [0, .18, .45] });
      panels.forEach(panel => observer.observe(panel));
      map._orbitObserver = observer;
    }
  });
}

function setupResponsiveControlPanels() {
  $$('.media-filter-panel,.voice-control-panel,.memory-control-panel').forEach(panel => {
    const head = $('.panel-head', panel);
    if (!head || $('.mobile-control-toggle', head)) return;
    panel.classList.add('mobile-control-collapsed');
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'mobile-control-toggle'; button.setAttribute('aria-expanded', 'false');
    button.innerHTML = '<i class="ph ph-sliders-horizontal"></i><span>展开</span>';
    button.onclick = () => {
      const collapsed = panel.classList.toggle('mobile-control-collapsed');
      button.setAttribute('aria-expanded', String(!collapsed));
      $('span', button).textContent = collapsed ? '展开' : '收起';
    };
    head.append(button);
  });
}

function animatePageSignal(pageName) {
  const banner = $(`[data-orbital-banner="${pageName}"]`); if (!banner) return;
  banner.classList.remove('is-routing'); void banner.offsetWidth; banner.classList.add('is-routing');
  $$('[data-orbital-signal]', banner).forEach((node, index) => setTimeout(() => {
    node.classList.remove('signal-pulse'); void node.offsetWidth; node.classList.add('signal-pulse');
    setTimeout(() => node.classList.remove('signal-pulse'), 760);
  }, 100 + index * 150));
  setTimeout(() => banner.classList.remove('is-routing'), 1100);
}

function pulseActivePageSignal(index = 0) {
  const page = $('.page.active'); if (!page) return;
  const nodes = $$('[data-orbital-signal]', page); if (!nodes.length) return;
  const node = nodes[Math.abs(Number(index) || 0) % nodes.length];
  node.classList.remove('signal-pulse'); void node.offsetWidth; node.classList.add('signal-pulse');
  setTimeout(() => node.classList.remove('signal-pulse'), 760);
}

function setBrainView(view, persist = true) {
  const next = view === 'config' ? 'config' : 'orbit';
  const page = $('#page-brain'); if (!page) return;
  page.dataset.brainView = next;
  $$('[data-brain-view-value]').forEach(button => {
    const active = button.dataset.brainViewValue === next;
    button.classList.toggle('active', active); button.setAttribute('aria-pressed', String(active));
  });
  if (persist) localStorage.setItem('wxconsole-brain-view', next);
  if (next === 'orbit') requestAnimationFrame(runOrbitSequence);
}

function formatMuteDuration(value) {
  const seconds = Math.max(10, Math.min(86400, Number(value) || 180));
  if (seconds % 3600 === 0) return `${seconds / 3600} 小时`;
  if (seconds % 60 === 0) return `${seconds / 60} 分钟`;
  return `${seconds} 秒`;
}

function updateMuteSummary(value) {
  const seconds = Math.max(10, Math.min(86400, Number(value) || 180));
  if ($('#brainMuteSummary')) $('#brainMuteSummary').textContent = formatMuteDuration(seconds);
  if ($('#brainMuteSummarySeconds')) $('#brainMuteSummarySeconds').textContent = `${seconds} 秒 · 按群独立 · 重启保留`;
}

function selectOrbitNode(key, pulse = true) {
  const detail = orbitNodeDetails[key]; if (!detail) return;
  $$('[data-orbit-node]').forEach(node => node.classList.toggle('is-selected', node.dataset.orbitNode === key));
  const icon = $('#orbitInspectorIcon'); icon.className = `ph ${detail.icon}`;
  $('#orbitInspectorTitle').textContent = detail.title;
  $('#orbitInspectorConfidence').textContent = `置信度 ${detail.confidence.toFixed(2)}`;
  $('#orbitInspectorStats').innerHTML = `<div><dt>来源类型</dt><dd>${escapeHtml(detail.source)}</dd></div><div><dt>召回候选</dt><dd>${detail.recalled}</dd></div><div><dt>注入结果</dt><dd>${detail.injected}</dd></div><div><dt>平均相似度</dt><dd>${detail.similarity.toFixed(2)}</dd></div>`;
  $('#orbitInspectorEvidence').innerHTML = detail.evidence.map((item, index) => `<article class="orbit-evidence"><span class="orbit-avatar ${index === 1 ? 'violet' : index === 2 ? 'mint' : ''}">${escapeHtml(item[0])}</span><div><strong>${escapeHtml(item[1])}</strong><p>${escapeHtml(item[2])}</p></div><b>${Number(item[3]).toFixed(2)}</b></article>`).join('');
  $('#orbitInspectorTimeline').innerHTML = detail.timeline.map(item => `<blockquote><time>${escapeHtml(item[0])}</time><strong>${escapeHtml(item[1])}</strong><p>${escapeHtml(item[2])}</p></blockquote>`).join('');
  const inspector = $('.orbit-inspector'); inspector.classList.remove('is-updating'); void inspector.offsetWidth; inspector.classList.add('is-updating');
  if (pulse) pulseOrbitNode(key);
}

function pulseOrbitNode(key) {
  const node = $(`[data-orbit-node="${key}"]`); if (!node) return;
  node.classList.remove('signal-pulse'); void node.offsetWidth; node.classList.add('signal-pulse');
  setTimeout(() => node.classList.remove('signal-pulse'), 900);
}

function runOrbitSequence() {
  const canvas = $('.orbit-canvas'); if (!canvas || !$('#page-brain').classList.contains('active')) return;
  canvas.classList.remove('route-sequence'); void canvas.offsetWidth; canvas.classList.add('route-sequence');
  ['person', 'fts', 'embedding', 'reranker'].forEach((key, index) => setTimeout(() => pulseOrbitNode(key), 130 + index * 150));
  setTimeout(() => canvas.classList.remove('route-sequence'), 1300);
}

function setupModelSectionNav() {
  const nav = $('#modelSectionNav'); if (!nav) return;
  const sections = $$('[data-model-section]', $('#aiForm'));
  nav.innerHTML = sections.map((section, index) => `<button type="button" class="${index === 0 ? 'active' : ''}" data-model-target="${section.id}">${escapeHtml(section.dataset.modelSection)}</button>`).join('');
  $$('[data-model-target]', nav).forEach(button => button.onclick = () => {
    const target = $('#' + button.dataset.modelTarget); if (!target) return;
    $$('[data-model-target]', nav).forEach(item => item.classList.toggle('active', item === button));
    target.scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' });
  });
}

function showPage(name) {
  if (!pageMeta[name]) return;
  document.body.dataset.page = name;
  applyWorkspace(pageWorkspace[name]);
  $$('.nav-item').forEach(x => x.classList.toggle('active', x.dataset.page === name));
  $$('.page').forEach(x => x.classList.toggle('active', x.id === `page-${name}`));
  $('#pageTitle').textContent = pageMeta[name][0]; $('#pageSubtitle').textContent = pageMeta[name][1];
  $('#startAllBtn').hidden = name !== 'overview';
  document.body.classList.remove('nav-open');
  history.replaceState(null, '', `#${name}`);
  window.scrollTo(0, 0);
  if (name === 'logs') requestAnimationFrame(() => { const out = $('#logOutput'); out.scrollTop = out.scrollHeight; });
  if (name === 'media') requestAnimationFrame(() => loadMediaCenter(null, true));
  if (name === 'voice-records') requestAnimationFrame(() => loadVoiceRecords(null, true));
  if (name === 'voices') requestAnimationFrame(() => { loadVoicepacks(null, true); loadMediaReplyStats(true); });
  if (name === 'faces') requestAnimationFrame(() => { loadFaces(null, true); loadMediaReplyStats(true); });
  if (name === 'groups') requestAnimationFrame(() => loadGroupMembers(null, true));
  if (name === 'personas') { $('#page-personas').dataset.mobilePane = 'directory'; requestAnimationFrame(() => loadPersonaMembers(true)); }
  if (name === 'brain') requestAnimationFrame(runOrbitSequence);
  else requestAnimationFrame(() => animatePageSignal(name));
  if (name === 'memory' && $('#memoryResults')?.textContent?.includes('选择左侧操作')) {
    requestAnimationFrame(() => searchMemory($('#searchMemoryBtn')));
  }
  if (name === 'reply-tasks' || name === 'vector') requestAnimationFrame(() => refreshReplyTasks(true));
}

function renderMediaTriggerDiagnostics(kind, data) {
  const root = $(`#${kind}MediaDiagnostics`); if (!root) return;
  const stats = data?.[kind] || {}; const funnel = $$('.media-trigger-funnel b', root);
  const values = [data?.reply_candidates || 0, stats.fit_passed || 0, stats.candidate_passed || 0, stats.sent || 0];
  funnel.forEach((node, index) => { node.textContent = values[index] ?? 0; });
  const head = $('.media-trigger-head span', root); if (head) head.textContent = `${data?.hours || 24}h · ${stats.selected || 0} 次选中`;
  const mainGate = Object.entries(stats.gates || {}).sort((a, b) => b[1] - a[1])[0];
  const labels = { fit_below_threshold: '媒介适配分不足', candidate_below_threshold: '没有高置信素材', probability_miss: '概率未抽中', selected: '已选中' };
  const note = $('p', root);
  if (note) note.textContent = mainGate ? `当前主要损耗：${labels[mainGate[0]] || mainGate[0]} ${mainGate[1]} 次。概率只在前置门槛全部通过后抽样。` : '概率只在回复门槛、媒介适配和素材置信度都通过后抽样。';
}

async function loadMediaReplyStats(silent = false) {
  try {
    const data = await api('/api/media-reply/stats?hours=24');
    renderMediaTriggerDiagnostics('voice', data); renderMediaTriggerDiagnostics('face', data);
  } catch (error) {
    if (!silent) toast(`触发统计读取失败：${error.message}`, 'error');
  }
}

function fillBrainConfig(data) {
  state.brainConfig = data;
  state.ignoredGroupMembers = Object.fromEntries(Object.entries(data.ignored_group_members || {}).map(([groupId, ids]) => [groupId, [...new Set((ids || []).map(String))]]));
  const c = data.reply_strategy || {};
  $('#brainMode').value = c.mode || 'veteran';
  $('#brainThreshold').value = c.threshold ?? 52; $('#brainThresholdValue').value = c.threshold ?? 52;
  if ($('#orbitThreshold')) $('#orbitThreshold').textContent = c.threshold ?? 52;
  $('#brainGlobalWorkers').value = c.global_workers ?? 8;
  if ($('#orbitWorkers')) $('#orbitWorkers').textContent = c.global_workers ?? 8;
  $('#brainGroupWorkers').value = c.per_group_workers ?? 3;
  $('#brainModelWorkers').value = c.model_concurrency ?? 6;
  $('#brainMuteDuration').value = c.mute_duration_seconds ?? 180;
  updateMuteSummary(c.mute_duration_seconds ?? 180);
  $('#brainScoringMode').value = c.scoring_mode || 'local_fast';
  $('#brainRerankCandidates').value = c.rerank_candidates ?? 12;
  const retrieval = data.retrieval || {};
  $('#brainVectorLimit').value = retrieval.vector_limit ?? 60;
  $('#brainFtsLimit').value = retrieval.fts_limit ?? 30;
  $('#brainAdaptiveRerank').checked = retrieval.adaptive_rerank !== false;
  $('#brainMentionUser').checked = c.mention_user_on_reply !== false;
  const weights = c.factor_weights || { involvement: 18, continuity: 14, memory: 16, value: 14, humor: 14, emotion: 10, timing: 14 };
  $('#factorEditor').innerHTML = Object.entries(factorLabels).map(([key, label]) => `<label><span>${label}</span><input data-factor="${key}" type="number" min="0" max="100" step="1" value="${Number(weights[key] ?? 0)}"></label>`).join('');
  const modifiers = c.modifiers || {};
  $('#modifierEditor').innerHTML = `<strong>本地修正值</strong>${Object.entries(modifierLabels).map(([key, label]) => `<label><span>${label}</span><input data-modifier="${key}" type="number" min="-100" max="100" step="1" value="${Number(modifiers[key] ?? 0)}"></label>`).join('')}`;
  const e = data.embedding || {};
  $('#embeddingBaseUrl').value = e.base_url || 'http://127.0.0.1:8017/v1';
  $('#embeddingModel').value = e.model || 'Qwen3-Embedding-8B-mxfp8';
  $('#rerankerModel').value = e.reranker_model || 'Qwen3-Reranker-4B-mxfp8';
  const media = data.media_reply || {};
  $('#voiceAutoEnabled').checked = media.automatic_enabled !== false;
  $('#faceAutoEnabled').checked = media.automatic_enabled !== false;
  $('#voiceReplyProbability').value = Math.round(Number(media.voice_probability ?? .15) * 100);
  $('#voiceReplyProbabilityValue').value = `${$('#voiceReplyProbability').value}%`;
  $('#faceReplyProbability').value = Math.round(Number(media.face_probability ?? .20) * 100);
  $('#faceReplyProbabilityValue').value = `${$('#faceReplyProbability').value}%`;
  $('#voiceMinFit').value = media.voice_min_fit ?? media.min_fit ?? 55;
  $('#faceMinFit').value = media.face_min_fit ?? media.min_fit ?? 45;
  $('#faceMinConfidence').value = media.min_candidate_confidence ?? .65;
  $('#faceGlobalShared').checked = media.global_face_assets !== false;
  updateBlacklistGroupOptions();
}

async function loadBrainConfig(silent = false) {
  try { fillBrainConfig(await api('/api/brain/config')); }
  catch (e) { if (!silent) toast(`群聊大脑配置读取失败：${e.message}`, 'error'); }
}

async function saveBrainConfig(button) {
  setBusy(button, true, '应用中…');
  try {
    const weights = {}; $$('[data-factor]').forEach(x => { weights[x.dataset.factor] = Number(x.value); });
    const modifiers = {}; $$('[data-modifier]').forEach(x => { modifiers[x.dataset.modifier] = Number(x.value); });
    const old = state.brainConfig || {};
    const result = await api('/api/brain/config', { method: 'POST', body: JSON.stringify({
      reply_strategy: { ...(old.reply_strategy || {}), mode: $('#brainMode').value, threshold: Number($('#brainThreshold').value), scoring_mode: $('#brainScoringMode').value, rerank_candidates: Number($('#brainRerankCandidates').value), global_workers: Number($('#brainGlobalWorkers').value), per_group_workers: Number($('#brainGroupWorkers').value), model_concurrency: Number($('#brainModelWorkers').value), mute_duration_seconds: Number($('#brainMuteDuration').value), mention_user_on_reply: $('#brainMentionUser').checked, factor_weights: weights, modifiers },
      embedding: { ...(old.embedding || {}), enabled: true, base_url: $('#embeddingBaseUrl').value.trim(), model: $('#embeddingModel').value.trim(), reranker_model: $('#rerankerModel').value.trim(), dimensions: 4096 },
      retrieval: { ...(old.retrieval || {}), vector_limit: Number($('#brainVectorLimit').value), fts_limit: Number($('#brainFtsLimit').value), adaptive_rerank: $('#brainAdaptiveRerank').checked }
    }) });
    fillBrainConfig(result);
    toast(result.hot_reload?.applied ? `已实时生效，AI PID ${result.hot_reload.response?.data?.pid || '未变化'}` : `配置已保存，热加载失败：${result.hot_reload?.error || '未知错误'}`, result.hot_reload?.applied ? 'success' : 'error', 7000);
  } catch (e) { toast(`实时应用失败：${e.message}`, 'error'); }
  finally { setBusy(button, false); }
}

async function saveMediaReplyConfig(button, source) {
  setBusy(button, true, '应用中…');
  try {
    const old = state.brainConfig || {};
    const current = old.media_reply || {};
    const automaticEnabled = source === 'voice' ? $('#voiceAutoEnabled').checked : $('#faceAutoEnabled').checked;
    const payload = {
      ...current,
      automatic_enabled: automaticEnabled,
      voice_probability: Number($('#voiceReplyProbability').value) / 100,
      face_probability: Number($('#faceReplyProbability').value) / 100,
      voice_min_fit: Number($('#voiceMinFit').value),
      face_min_fit: Number($('#faceMinFit').value),
      min_candidate_confidence: Number($('#faceMinConfidence').value),
      global_face_assets: $('#faceGlobalShared').checked,
      auto_media_replaces_text: true,
    };
    const result = await api('/api/brain/config', { method: 'POST', body: JSON.stringify({ media_reply: payload }) });
    fillBrainConfig(result);
    toast(result.hot_reload?.applied ? `媒介策略已实时生效，AI PID ${result.hot_reload.response?.data?.pid || '未变'}` : '配置已保存，但热加载失败', result.hot_reload?.applied ? 'success' : 'error', 7000);
  } catch (e) { toast(`媒介策略应用失败：${e.message}`, 'error'); }
  finally { setBusy(button, false); }
}

async function testMediaContext(button, source) {
  const query = source === 'voice' ? $('#voiceQuery').value.trim() : $('#faceQuery').value.trim();
  const groupId = source === 'voice' ? $('#voiceTargetGroup').value : $('#faceTargetGroup').value;
  if (!query) { toast('请先输入需要测试的对话语境', 'error'); return; }
  setBusy(button, true, '分析中…');
  try {
    const r = await api('/api/media-reply/test', { method: 'POST', body: JSON.stringify({ query, group_id: groupId }) });
    const rows = source === 'voice' ? r.voices : r.faces;
    const names = (rows || []).slice(0, 3).map(x => source === 'voice' ? x.title : (x.ocr_text || x.summary || `#${x.id}`));
    toast(names.length ? `语境匹配候选：${names.join('、')}` : '当前语境没有达到可发送的素材置信度', names.length ? 'success' : 'error', 9000);
  } catch (e) { toast(`语境测试失败：${e.message}`, 'error'); }
  finally { setBusy(button, false); }
}

function renderReplyTasks() {
  const status = $('#taskStateFilter')?.value || '', query = ($('#taskQuery')?.value || '').trim().toLowerCase();
  const finals = new Set(['completed', 'skipped', 'failed', 'cancelled']);
  const rows = state.replyTasks.filter(x => {
    if (status === 'active' && finals.has(x.state)) return false;
    if (status && status !== 'active' && x.state !== status) return false;
    return !query || `${x.group_name} ${x.sender_name} ${x.thread_id} ${x.question}`.toLowerCase().includes(query);
  });
  $('#replyTaskList').innerHTML = rows.length ? rows.map(x => { const t = x.details?.timings_ms || {}, routes = x.details?.route_counts || {}; const routeText = Object.entries(routes).map(([k,v]) => `${k} ${v}`).join(' · '); return `<article class="reply-task state-${escapeHtml(x.state)}">
    <div class="task-main"><span class="task-state">${escapeHtml(x.state_label || x.state)}</span><strong>${escapeHtml(x.group_name || x.group_id)} · ${escapeHtml(x.sender_name || x.user_id)}</strong><p title="${escapeHtml(x.question || '')}">${escapeHtml(x.question || '')}</p><code title="${escapeHtml(x.thread_id || '')}">${escapeHtml(x.thread_id || '')}</code></div>
    <div class="task-meta"><span>${x.queue_position ? `排队 ${x.queue_position}` : `${Number(x.elapsed_seconds || 0).toFixed(1)}s`}</span><span>${x.score == null ? '评分 --' : `评分 ${x.score} / ${x.threshold}`}</span><span>${escapeHtml(x.model || '等待模型')}</span><b>${escapeHtml(x.medium || '待选择媒介')}</b><span class="task-timing">向量 ${Number(t.embedding_and_recall || 0).toFixed(0)}ms · 结构 ${Number(t.structured_routes || 0).toFixed(0)}ms · 重排 ${Number(t.rerank || 0).toFixed(0)}ms · 生成 ${Number(t.generation || 0).toFixed(0)}ms${x.details?.expanded_second_batch ? ' · 已扩批' : ''}${routeText ? `<br>${escapeHtml(routeText)}` : ''}</span></div>
  </article>`; }).join('') : '<div class="terminal-empty">当前筛选条件下没有回复任务</div>';
}

function setLatencyMetric(id, value, warningAt, dangerAt) {
  const output = $('#' + id); if (!output) return;
  output.textContent = value == null ? '--' : `${Number(value).toFixed(1)} ms`;
  const card = output.closest('.embedding-metric');
  card?.classList.remove('metric-good', 'metric-warning', 'metric-danger', 'metric-missing');
  card?.classList.add(value == null ? 'metric-missing' : Number(value) >= dangerAt ? 'metric-danger' : Number(value) >= warningAt ? 'metric-warning' : 'metric-good');
}

async function refreshReplyTasks(silent = false) {
  try {
    const data = await api('/api/brain/tasks?limit=200'); state.replyTasks = data.items || []; renderReplyTasks();
    $('#replyActiveCount').textContent = data.active || 0; $('#replyQueuedCount').textContent = data.queued || 0; $('#replyDoneCount').textContent = data.completed_recent || 0;
    const s = data.runtime?.scheduler || {}; $('#workerUsage').textContent = `${s.active_workers || 0} / ${s.global_workers || 8}`;
    const e = data.runtime?.embedding || {}; $('#backfillProgress').textContent = e.pending ?? 0; $('#embeddingState').textContent = e.paused ? '已暂停' : e.running ? '回填中' : '就绪';
    $('#embeddingVectors').textContent = e.vectors ?? e.processed ?? 0; $('#embeddingPending').textContent = e.pending ?? 0;
    const modelState = e.local_models?.[e.model] || {}, rerankerState = e.local_models?.[e.reranker_model] || {};
    $('#embeddingLoaded').textContent = modelState.loaded && rerankerState.loaded ? '均已加载' : modelState.is_loading || rerankerState.is_loading ? '加载中' : '部分未加载';
    const bytes = Number(modelState.actual_size || modelState.estimated_size || 0) + Number(rerankerState.actual_size || rerankerState.estimated_size || 0);
    $('#embeddingMemory').textContent = bytes ? `${(bytes / 1073741824).toFixed(1)} GB` : '--';
    setLatencyMetric('embeddingP50', e.embedding_p50_ms, 300, 500); setLatencyMetric('embeddingP95', e.embedding_p95_ms, 500, 1500);
    setLatencyMetric('rerankerP50', e.reranker_p50_ms, 500, 1000); setLatencyMetric('rerankerP95', e.reranker_p95_ms, 1000, 2000);
    const orbitalLatency = e.embedding_p50_ms ?? e.reranker_p50_ms;
    if ($('#orbitLatency')) $('#orbitLatency').textContent = orbitalLatency == null ? '--' : `${Number(orbitalLatency).toFixed(0)}ms`;
    if ($('#orbitTotalLatency')) $('#orbitTotalLatency').textContent = orbitalLatency == null ? '147ms' : `${Number(orbitalLatency).toFixed(0)}ms`;
  } catch (e) { if (!silent) toast(`回复任务读取失败：${e.message}`, 'error'); }
}

async function controlBackfill(action, button) {
  setBusy(button, true, '处理中…');
  try { await api(`/api/embedding/backfill/${action}`, { method: 'POST', body: '{}' }); await refreshReplyTasks(true); toast(action === 'pause' ? '向量回填已暂停' : action === 'resume' ? '向量回填已继续' : '已将所有历史加入回填队列'); }
  catch (e) { toast(`回填操作失败：${e.message}`, 'error'); }
  finally { setBusy(button, false); }
}

async function previewBrain(button) {
  setBusy(button, true, '计算中…');
  try {
    const result = await api('/api/brain/preview', { method: 'POST', body: '{}' });
    $('#brainPreviewSummary').textContent = `共评估 ${result.evaluated} 条，门槛 ${result.threshold}，预计回复 ${result.predicted} 条`;
    $('#brainPreviewList').innerHTML = result.items.length ? result.items.slice().reverse().map(x => `<article><strong>${escapeHtml(x.sender_name || x.group_id)} · ${x.estimated_score}</strong><p>${escapeHtml(x.text)}</p><small>${escapeHtml((x.signals || []).map(s => `${s.signal} ${s.value > 0 ? '+' : ''}${s.value}`).join(' · ') || '模型七维评分将在真实流程中补充')}</small></article>`).join('') : '<div class="terminal-empty">当前门槛下没有预计回复项</div>';
    $('#brainPreviewDialog').showModal();
  } catch (e) { toast(`预览失败：${e.message}`, 'error'); }
  finally { setBusy(button, false); }
}

async function testEmbedding(button) {
  setBusy(button, true, '测试中…');
  try {
    const groupId = state.config?.target_groups?.[0]?.id || state.config?.groups?.[0]?.id || '';
    const result = await api('/api/embedding/test', {method:'POST', body:JSON.stringify({group_id:groupId,query:$('#embeddingTestQuery').value.trim()})});
    $('#embeddingTestResult').textContent = `${result.latency_ms} ms · 召回 ${(result.items || []).length} 条${result.error ? ` · ${result.error}` : ''}`;
    await refreshReplyTasks(true);
  } catch(e) { $('#embeddingTestResult').textContent = e.message; toast(`向量测试失败：${e.message}`,'error'); }
  finally { setBusy(button,false); }
}

function renderStatus(data) {
  state.status = data;
  for (const name of ['wechat', 'onebot', 'ai']) {
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
    mediaHook.textContent = !data.onebot.attach_permission ? '缺少调试权限' : (data.onebot.media_upload_ready ? '图片/视频/语音就绪' : '待真实上传');
    mediaHook.title = !data.onebot.attach_permission ? '需要开启 macOS Developer Tools 调试权限' : (media.message || '当前微信进程还没有捕获真实 UploadMedia');
  }
  $('.configured', $('[data-service="ai"]')).textContent = data.ai.configured ? '已配置' : '缺少 Key';
  const all = data.wechat.running && data.onebot.running && data.onebot.port_open && data.ai.running && data.ai.port_open;
  const chip = $('#healthChip'); chip.textContent = all ? '全部在线' : '需要处理'; chip.className = `health-chip ${all ? 'good' : 'warn'}`;
  $('#testHealth').textContent = all ? '服务链路在线' : '部分服务离线'; $('#testHealth').className = `health-chip ${all ? 'good' : 'warn'}`;
  $('#syncTime').textContent = `${data.time} 已同步`;
  $('#targetPath').textContent = `${data.target.app} · ${data.target.bundle_id} · ${data.target.version}`;
  renderAutoLogin(data.wechat_auto_login || {});
}

function renderAutoLogin(item) {
  const labels = {
    starting: '启动中', disabled: '已关闭', wechat_not_running: '当前微信未运行',
    logged_in: '已登录 · 不会点击', login_confirmed: '已进入微信', waiting_window: '等待登录窗口', watching: '监控中', login_detected: '已识别登录页',
    clicked_login: '已点击登录', attempt_limit: '已达尝试上限', error: '检测异常'
  };
  const status = item.status || 'starting';
  $('#autoLoginState').textContent = labels[status] || status;
  $('#autoLoginState').className = `auto-login-state ${['logged_in','login_confirmed','watching','clicked_login'].includes(status) ? 'good' : ['error','attempt_limit'].includes(status) ? 'bad' : ''}`;
  $('#autoLoginPulse').className = `auto-login-pulse ${status}`;
  if ($('#autoLoginEnabled') && document.activeElement !== $('#autoLoginEnabled')) $('#autoLoginEnabled').checked = item.enabled !== false;
  const count = Number(item.consecutive_detections || 0);
  $('#autoLoginDetail').textContent = item.last_error || (status === 'login_detected' ? `安全确认 ${count}/2；连续识别后才点击` : `仅检查 WeChat.app · ${item.checked_at || '等待首次检测'}`);
}

async function saveAutoLogin(enabled) {
  try {
    const result = await api('/api/auto-login/config', { method: 'POST', body: JSON.stringify({ enabled }) });
    renderAutoLogin(result.state || {}); toast(enabled ? '自动登录守护已开启' : '自动登录守护已关闭');
  } catch (e) { $('#autoLoginEnabled').checked = !enabled; toast(`保存失败：${e.message}`, 'error'); }
}

async function checkAutoLogin(button) {
  setBusy(button, true, '检测中…');
  try { const result = await api('/api/auto-login/check', { method: 'POST', body: '{}' }); renderAutoLogin(result); toast('已完成安全检测，本次不执行点击'); }
  catch (e) { toast(`检测失败：${e.message}`, 'error'); }
  finally { setBusy(button, false); }
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
  state.groupPersonalities = c.group_personalities && typeof c.group_personalities === 'object' ? { ...c.group_personalities } : {};
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
  const g = c.image_generation || {};
  $('#imageGenEnabled').checked = !!g.enabled; $('#imageGenBaseUrl').value = g.base_url || c.base_url || '';
  $('#imageGenApiKey').value = g.api_key || ''; $('#imageGenModel').value = g.model || '';
  $('#imageGenSize').value = g.size || '1024x1024'; $('#imageGenQuality').value = g.quality || 'standard';
  $('#imageGenTimeout').value = g.timeout_seconds || 180;
  renderChannelSelect(); fillChannelForm(); renderGroups(c.target_groups); updateRouteSummary(); state.dirty = false; $('#aiForm').classList.remove('is-dirty'); $('#saveState').textContent = `SYNCED · ${c.revision}`;
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
    existing.set(item.id, old ? { ...item, name: item.source === 'alias' ? item.name : (old.name && old.name !== old.id ? old.name : item.name), selected: old.selected } : { ...item });
  });
  state.groupCatalog = [...existing.values()].sort((a, b) => Number(b.selected) - Number(a.selected) || a.name.localeCompare(b.name, 'zh-CN'));
}
function groupDisplayName(group) {
  const id = String(group?.id || '');
  const name = String(group?.name || '').trim();
  if (name && name !== id && !name.endsWith('@chatroom')) return name;
  const number = id.replace('@chatroom', '');
  return `未识别群聊 · 尾号${number.slice(-4) || '未知'}`;
}
function groupSelectLabel(group) {
  const count = Number(group?.observed_members || 0);
  return `${groupDisplayName(group)}${count ? ` · 已识别 ${count} 人` : ''}`;
}
function renderGroupPermissions() {
  $('#groupList').innerHTML = state.groupCatalog.map(group => {
    const confidence = group.confidence == null ? '' : ` · ${group.confidence}%`;
    const source = group.source || (group.last_seen ? 'event' : 'config');
    const editableName = groupDisplayName(group) === group.name ? group.name : '';
    const count = Number(group.observed_members || 0);
    return `<div class="group-permission-row" data-group-id="${escapeHtml(group.id)}"><label class="permission-check"><input class="group-enabled" type="checkbox" ${group.selected ? 'checked' : ''}><i></i><span>${group.selected ? '已授权' : '未授权'}</span></label><div class="field group-name-field"><input class="group-name" value="${escapeHtml(editableName)}" placeholder="${escapeHtml(groupDisplayName(group))}" aria-label="群聊名称"><small>${editableName ? '真实群名 · 可直接修改' : '尚未绑定真实群名'}</small></div><code title="微信内部群 ID">${escapeHtml(group.id)}</code><span class="discovery-source" title="${escapeHtml(group.preview || '')}"><i></i><span>${escapeHtml(source)}${confidence}<small>${count ? `已识别 ${count} 名成员` : (group.preview ? escapeHtml(group.preview) : escapeHtml(group.last_seen || '暂无成员记录'))}</small></span></span></div>`;
  }).join('');
  $$('.group-permission-row').forEach(row => {
    const group = state.groupCatalog.find(x => x.id === row.dataset.groupId);
    $('.group-enabled', row).onchange = e => { group.selected = e.target.checked; $('.permission-check span', row).textContent = group.selected ? '已授权' : '未授权'; updateGroupCount(); updateTestGroups(); markDirty(); };
    $('.group-name', row).oninput = e => { group.name = e.target.value.trim() || group.id; updateTestGroups(); markDirty(); };
  });
  $('#groupQuickSelect').innerHTML = state.groupCatalog.map(x => `<option value="${escapeHtml(x.id)}">${escapeHtml(groupSelectLabel(x))}</option>`).join('');
  updateGroupCount(); updateTestGroups();
}
let groupDiscoveryRequest = 0;
async function loadDiscoveredGroups(silent = false) {
  const requestId = ++groupDiscoveryRequest;
  $('#groupDiscoveryState').textContent = '正在读取当前微信群目录…';
  let lastError;
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      const data = await api('/api/groups/discover');
      if (requestId !== groupDiscoveryRequest) return;
      mergeGroupCatalog(data.groups);
      renderGroupPermissions();
      $('#groupDiscoveryState').textContent = `已发现 ${data.count} 个群聊`;
      return;
    } catch (e) {
      lastError = e;
      if (attempt === 0) await new Promise(resolve => setTimeout(resolve, 450));
    }
  }
  if (requestId !== groupDiscoveryRequest) return;
  $('#groupDiscoveryState').textContent = state.groupCatalog.length
    ? `刷新失败，已保留现有 ${state.groupCatalog.length} 个群聊`
    : '群目录获取失败';
  if (!silent) toast(`群列表获取失败：${lastError?.message || '未知错误'}`, 'error');
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
  const select = $('#testGroup'), old = select.value; select.innerHTML = state.groupCatalog.filter(x => x.selected && x.id).map(x => `<option value="${escapeHtml(x.id)}">${escapeHtml(groupSelectLabel(x))}</option>`).join('');
  if ([...select.options].some(x => x.value === old)) select.value = old;
  const groupOptions = () => state.groupCatalog.filter(x => x.id).map(x => `<option value="${escapeHtml(x.id)}">${escapeHtml(groupSelectLabel(x))}</option>`).join('');
  const selectedOptions = () => state.groupCatalog.filter(x => x.selected && x.id).map(x => `<option value="${escapeHtml(x.id)}">${escapeHtml(groupSelectLabel(x))}</option>`).join('');
  const mem = $('#memoryGroup'); if (mem) { const oldMem = mem.value; mem.innerHTML = groupOptions(); if ([...mem.options].some(x => x.value === oldMem)) mem.value = oldMem; }
  const personaGroup = $('#personaGroup'); if (personaGroup) { const oldPersona = personaGroup.value; personaGroup.innerHTML = groupOptions(); if ([...personaGroup.options].some(x => x.value === oldPersona)) personaGroup.value = oldPersona; }
  const media = $('#mediaGroup'); if (media) { const oldMedia = media.value; media.innerHTML = `<option value="">全部群媒体</option>` + groupOptions(); if ([...media.options].some(x => x.value === oldMedia)) media.value = oldMedia; }
  const voiceRecords = $('#voiceRecordsGroup'); if (voiceRecords) { const oldVoiceRecords = voiceRecords.value; voiceRecords.innerHTML = `<option value="">全部来源群</option>` + groupOptions(); if ([...voiceRecords.options].some(x => x.value === oldVoiceRecords)) voiceRecords.value = oldVoiceRecords; }
  const voice = $('#voiceTargetGroup'); if (voice) { const oldVoice = voice.value; voice.innerHTML = selectedOptions(); if ([...voice.options].some(x => x.value === oldVoice)) voice.value = oldVoice; }
  const faceTarget = $('#faceTargetGroup'); if (faceTarget) { const oldFaceTarget = faceTarget.value; faceTarget.innerHTML = selectedOptions(); if ([...faceTarget.options].some(x => x.value === oldFaceTarget)) faceTarget.value = oldFaceTarget; }
  const faceGroup = $('#faceGroup'); if (faceGroup) { const oldFaceGroup = faceGroup.value; faceGroup.innerHTML = `<option value="">全部来源群</option>` + groupOptions(); if ([...faceGroup.options].some(x => x.value === oldFaceGroup)) faceGroup.value = oldFaceGroup; }
  updateBlacklistGroupOptions();
}

function updateBlacklistGroupOptions() {
  const select = $('#memberBlacklistGroup'); if (!select) return;
  const old = select.value;
  const groups = state.groupCatalog.filter(x => x.id?.endsWith('@chatroom'));
  select.innerHTML = groups.map(x => `<option value="${escapeHtml(x.id)}">${escapeHtml(groupSelectLabel(x))}</option>`).join('');
  if ([...select.options].some(x => x.value === old)) select.value = old;
  updateBlacklistCount();
  updateGroupAdminOptions();
  updateReplyMentionOptions();
  updateGroupPersonalityOptions();
}

function updateGroupAdminOptions() {
  const select = $('#groupAdminGroup'); if (!select) return;
  const old = select.value;
  const groups = state.groupCatalog.filter(x => x.id?.endsWith('@chatroom'));
  select.innerHTML = groups.map(x => `<option value="${escapeHtml(x.id)}">${escapeHtml(groupSelectLabel(x))}</option>`).join('');
  if ([...select.options].some(x => x.value === old)) select.value = old;
}

function updateReplyMentionOptions() {
  const select = $('#replyMentionGroup'); if (!select) return;
  const old = select.value;
  const groups = state.groupCatalog.filter(x => x.id?.endsWith('@chatroom'));
  select.innerHTML = groups.map(x => `<option value="${escapeHtml(x.id)}">${escapeHtml(groupSelectLabel(x))}</option>`).join('');
  if ([...select.options].some(x => x.value === old)) select.value = old;
  renderReplyMentionSetting();
}

function updateGroupPersonalityOptions() {
  const select = $('#groupPersonalityGroup'); if (!select) return;
  const old = select.value;
  const groups = state.groupCatalog.filter(x => x.id?.endsWith('@chatroom'));
  select.innerHTML = groups.map(x => `<option value="${escapeHtml(x.id)}">${escapeHtml(groupSelectLabel(x))}</option>`).join('');
  if ([...select.options].some(x => x.value === old)) select.value = old;
  renderGroupPersonalitySetting();
}

function normalizeGroupPersonality(value) {
  if (typeof value === 'string') return { enabled: true, name: '', prompt: value };
  const item = value && typeof value === 'object' ? value : {};
  return { enabled: !!item.enabled, name: String(item.name || ''), prompt: String(item.prompt || '') };
}

function renderGroupPersonalitySetting() {
  const groupId = $('#groupPersonalityGroup')?.value || '';
  const item = normalizeGroupPersonality(state.groupPersonalities[groupId]);
  if ($('#groupPersonalityEnabled')) $('#groupPersonalityEnabled').checked = item.enabled;
  if ($('#groupPersonalityName')) $('#groupPersonalityName').value = item.name;
  if ($('#groupPersonalityPrompt')) $('#groupPersonalityPrompt').value = item.prompt;
  if ($('#groupPersonalityCount')) $('#groupPersonalityCount').textContent = String(item.prompt.length);
  if ($('#groupPersonalityState')) {
    $('#groupPersonalityState').textContent = !groupId
      ? '请选择群聊'
      : item.prompt
        ? `当前群独立配置：${item.enabled ? '已启用' : '已关闭'}${item.name ? ` · ${item.name}` : ''}`
        : '尚未设置，当前继承全局机器人性格';
  }
}

function markGroupPersonalityPending() {
  const name = $('#groupPersonalityName')?.value.trim() || '未命名性格';
  if ($('#groupPersonalityCount')) $('#groupPersonalityCount').textContent = String($('#groupPersonalityPrompt')?.value.length || 0);
  if ($('#groupPersonalityState')) $('#groupPersonalityState').textContent = `待保存：${$('#groupPersonalityEnabled')?.checked ? '启用' : '关闭'} · ${name}`;
}

async function saveGroupPersonalitySetting(button) {
  const groupId = $('#groupPersonalityGroup')?.value || '';
  const enabled = !!$('#groupPersonalityEnabled')?.checked;
  const name = $('#groupPersonalityName')?.value.trim() || '';
  const prompt = $('#groupPersonalityPrompt')?.value.trim() || '';
  if (!groupId) { toast('请先选择群聊', 'error'); return; }
  if (enabled && !prompt) { toast('启用单群性格前请填写性格与表达规则', 'error'); $('#groupPersonalityPrompt')?.focus(); return; }
  setBusy(button, true, '保存中…');
  try {
    const result = await api('/api/groups/personality', {
      method: 'POST', body: JSON.stringify({ group_id: groupId, enabled, name, prompt })
    });
    state.groupPersonalities[groupId] = result.item;
    renderGroupPersonalitySetting();
    toast(result.hot_reload?.applied
      ? `单群性格已实时生效，AI PID ${result.hot_reload.response?.data?.pid || '未变化'}`
      : `配置已保存，热加载失败：${result.hot_reload?.error || '未知错误'}`,
      result.hot_reload?.applied ? 'success' : 'error', 7000);
  } catch (e) { toast(`单群性格保存失败：${e.message}`, 'error', 7000); }
  finally { setBusy(button, false); }
}

function renderReplyMentionSetting() {
  const groupId = $('#replyMentionGroup')?.value || '';
  const strategy = state.brainConfig?.reply_strategy || {};
  const override = strategy.group_overrides?.[groupId] || {};
  const hasOverride = Object.prototype.hasOwnProperty.call(override, 'mention_user_on_reply');
  const enabled = hasOverride
    ? override.mention_user_on_reply !== false
    : strategy.mention_user_on_reply !== false;
  if ($('#replyMentionEnabled')) $('#replyMentionEnabled').checked = enabled;
  if ($('#replyMentionState')) {
    $('#replyMentionState').textContent = !groupId
      ? '请选择群聊'
      : hasOverride
        ? `当前群独立设置：${enabled ? '回复时艾特' : '不艾特'}`
        : `继承全局默认：${enabled ? '回复时艾特' : '不艾特'}`;
  }
}

async function saveReplyMentionSetting(button) {
  const groupId = $('#replyMentionGroup')?.value || '';
  if (!groupId) { toast('请先选择群聊', 'error'); return; }
  setBusy(button, true, '保存中…');
  try {
    const old = state.brainConfig || {};
    const strategy = { ...(old.reply_strategy || {}) };
    const overrides = { ...(strategy.group_overrides || {}) };
    overrides[groupId] = {
      ...(overrides[groupId] || {}),
      mention_user_on_reply: $('#replyMentionEnabled').checked,
    };
    strategy.group_overrides = overrides;
    const result = await api('/api/brain/config', {
      method: 'POST', body: JSON.stringify({ reply_strategy: strategy })
    });
    fillBrainConfig(result);
    $('#replyMentionGroup').value = groupId;
    renderReplyMentionSetting();
    const enabled = $('#replyMentionEnabled').checked;
    toast(result.hot_reload?.applied
      ? `本群已${enabled ? '开启' : '关闭'}回复艾特，AI PID ${result.hot_reload.response?.data?.pid || '未变化'}`
      : `配置已保存，热加载失败：${result.hot_reload?.error || '未知错误'}`,
      result.hot_reload?.applied ? 'success' : 'error', 7000);
  } catch (e) { toast(`回复艾特设置失败：${e.message}`, 'error', 7000); }
  finally { setBusy(button, false); }
}

function currentBlacklistIds() {
  const groupId = $('#memberBlacklistGroup')?.value || '';
  return new Set((state.ignoredGroupMembers[groupId] || []).map(String));
}

function updateBlacklistCount() {
  const count = currentBlacklistIds().size;
  if ($('#memberBlacklistCount')) $('#memberBlacklistCount').textContent = String(count);
}

function renderGroupMembers() {
  const groupId = $('#memberBlacklistGroup')?.value || '';
  const query = ($('#memberBlacklistQuery')?.value || '').trim().toLowerCase();
  const rows = state.groupMemberCatalog[groupId] || [];
  const meta = state.groupMemberCatalogMeta[groupId] || {};
  const ignored = currentBlacklistIds();
  const visible = rows.filter(x => !query || `${x.name || ''} ${x.nickname || ''} ${x.card || ''} ${x.user_id || ''}`.toLowerCase().includes(query));
  const declared = Number(meta.declared_member_count || 0), named = Number(meta.named_member_count || 0);
  $('#memberCatalogCount').textContent = rows.length ? (declared ? `已识别 ${rows.length} / 群总人数约 ${declared} · 有名称 ${named} · 当前显示 ${visible.length}` : `历史已识别 ${rows.length} 人 · 有名称 ${named} · 当前显示 ${visible.length}`) : '尚未刷新成员目录';
  $('#memberBlacklistList').innerHTML = visible.length ? visible.map(item => {
    const sourceLabels = { onebot_live: '微信实时', message_history: '真实消息', quoted_history: '引用原话', permanent_memory: '永久记忆', saved_blacklist: '已保存' };
    const sources = (item.sources || []).map(x => sourceLabels[x] || x).join(' + ');
    const detail = [item.card && `群昵称 ${item.card}`, item.nickname && `昵称 ${item.nickname}`, item.message_count ? `已记录 ${item.message_count} 条消息` : ''].filter(Boolean).join(' · ');
    const readable = item.name && item.name !== '群友';
    const fallback = `未识别成员 · ${String(item.user_id || '').slice(-6)}`;
    const name = readable ? item.name : fallback;
    return `<label class="member-blacklist-row"><input type="checkbox" data-member-id="${escapeHtml(item.user_id)}" ${ignored.has(String(item.user_id)) ? 'checked' : ''}><span class="member-avatar">${escapeHtml((name || '群').slice(0, 1))}</span><span class="member-identity"><strong>${escapeHtml(name)}</strong><small>${escapeHtml(detail || '暂无可读群昵称')}</small><code title="微信内部成员 ID">${escapeHtml(item.user_id)}</code></span><span class="member-source">${escapeHtml(sources || '成员目录')}</span></label>`;
  }).join('') : `<div class="terminal-empty">${rows.length ? '没有匹配的成员' : '暂无成员数据，请点击“刷新成员目录”'}</div>`;
  $$('[data-member-id]', $('#memberBlacklistList')).forEach(box => box.onchange = () => {
    const ids = currentBlacklistIds();
    if (box.checked) ids.add(box.dataset.memberId); else ids.delete(box.dataset.memberId);
    state.ignoredGroupMembers[groupId] = [...ids];
    updateBlacklistCount();
    $('#memberBlacklistState').textContent = '有未保存的黑名单修改';
  });
  updateBlacklistCount();
}

async function loadGroupMembers(button = null, silent = false) {
  const groupId = $('#memberBlacklistGroup')?.value || '';
  if (!groupId) { renderGroupMembers(); return; }
  if (silent && state.groupMemberCatalog[groupId]) { renderGroupMembers(); return; }
  if (button) setBusy(button, true, '拉取中…');
  $('#memberBlacklistState').textContent = '正在合并微信实时目录与永久记忆…';
  try {
    const data = await api(`/api/groups/members?group_id=${encodeURIComponent(groupId)}`);
    state.groupMemberCatalog[groupId] = data.items || [];
    state.groupMemberCatalogMeta[groupId] = data;
    state.ignoredGroupMembers[groupId] = [...new Set((data.ignored_ids || []).map(String))];
    renderGroupMembers();
    const live = Number(data.source_counts?.onebot_live || 0), history = Number(data.source_counts?.message_history || 0), quoted = Number(data.source_counts?.quoted_history || 0);
    $('#memberBlacklistState').textContent = data.onebot_complete ? `完整名册 ${data.count} 人 · 实时生效` : `历史已识别 ${data.count} 人 · 实时 ${live} · 发言 ${history} · 引用补全 ${quoted}`;
    if (data.onebot_error && !silent) toast(`微信实时目录暂不可用，已显示全部永久记忆成员：${data.onebot_error}`, 'error', 7000);
  } catch (e) {
    $('#memberBlacklistState').textContent = '成员拉取失败';
    if (!silent) toast(`群成员拉取失败：${e.message}`, 'error', 7000);
  } finally { if (button) setBusy(button, false); }
}

async function saveGroupBlacklist(button) {
  const groupId = $('#memberBlacklistGroup')?.value || '';
  if (!groupId) { toast('请先选择群聊', 'error'); return; }
  setBusy(button, true, '保存中…');
  try {
    const result = await api('/api/groups/ignored-members', { method: 'POST', body: JSON.stringify({ group_id: groupId, user_ids: [...currentBlacklistIds()] }) });
    state.ignoredGroupMembers[groupId] = result.ignored_ids || [];
    updateBlacklistCount();
    $('#memberBlacklistState').textContent = `已屏蔽 ${result.count} 人，保存后已立即生效`;
    toast(result.hot_reload?.applied ? `对话黑名单已实时生效，AI PID ${result.hot_reload.response?.data?.pid || '未变'}` : `黑名单已保存，热加载失败：${result.hot_reload?.error || '未知错误'}`, result.hot_reload?.applied ? 'success' : 'error', 7000);
  } catch (e) { toast(`黑名单保存失败：${e.message}`, 'error', 7000); }
  finally { setBusy(button, false); }
}

const ADMIN_PERMISSION_LABELS = {
  'status.view': '状态查看', 'reply.control': '回复控制', 'strategy.manage': '策略管理',
  'media.manage': '媒介管理', 'personality.manage': '性格管理', 'members.manage': '成员管理',
  'memory.manage': '上下文管理', 'audit.view': '审计查看'
};
const ADMIN_ROLE_LABELS = { observer: '观察员', moderator: '协管员', admin: '群管理员', custom: '自定义' };

function currentAdminGroup() { return $('#groupAdminGroup')?.value || ''; }
function currentGroupAdmins() { return state.groupAdmins[currentAdminGroup()] || []; }
function memberNameForAdmin(userId) {
  const item = (state.groupMemberCatalog[currentAdminGroup()] || []).find(x => String(x.user_id) === String(userId));
  return item?.name && item.name !== '群友' ? item.name : `未识别成员 · ${String(userId || '').slice(-6)}`;
}
function addGroupAdmin(userId, displayName = '', source = 'directory') {
  userId = String(userId || '').trim(); if (!userId || userId.endsWith('@chatroom')) return;
  const groupId = currentAdminGroup(), rows = currentGroupAdmins();
  if (rows.some(x => String(x.user_id) === userId)) { toast('该成员已经是本群管理员', 'error'); return; }
  rows.push({ user_id: userId, display_name: displayName || memberNameForAdmin(userId), role: 'observer', permissions: [], source, enabled: true });
  state.groupAdmins[groupId] = rows; state.groupAdminPreviewUserId = userId;
  renderGroupAdminPanel(); loadAdminMenuPreview();
}
function effectiveAdminPermissions(item) {
  return [...new Set([...(state.groupAdminRoles[item.role] || []), ...(item.permissions || [])])];
}
function renderGroupAdminPanel() {
  const groupId = currentAdminGroup(), members = state.groupMemberCatalog[groupId] || [];
  const query = ($('#groupAdminSearch')?.value || '').trim().toLowerCase();
  const admins = currentGroupAdmins(), adminIds = new Set(admins.map(x => String(x.user_id)));
  const visible = members.filter(x => !adminIds.has(String(x.user_id)) && (!query || `${x.name || ''} ${x.nickname || ''} ${x.card || ''} ${x.user_id || ''}`.toLowerCase().includes(query)));
  $('#groupAdminCatalogMeta').textContent = `${members.length} 位已识别成员 · ${visible.length} 位可添加`;
  $('#groupAdminMemberList').innerHTML = visible.length ? visible.map(item => {
    const name = item.name && item.name !== '群友' ? item.name : `未识别成员 · ${String(item.user_id || '').slice(-6)}`;
    return `<button type="button" class="admin-member-option" data-admin-add="${escapeHtml(item.user_id)}"><span class="member-avatar">${escapeHtml(name.slice(0, 1))}</span><span><strong>${escapeHtml(name)}</strong><small>${escapeHtml((item.sources || []).join(' · ') || '历史目录')}</small><code>${escapeHtml(item.user_id)}</code></span><i class="ph ph-plus"></i></button>`;
  }).join('') : '<div class="terminal-empty">没有可添加的匹配成员</div>';
  $$('[data-admin-add]', $('#groupAdminMemberList')).forEach(button => button.onclick = () => {
    const item = members.find(x => String(x.user_id) === button.dataset.adminAdd);
    addGroupAdmin(button.dataset.adminAdd, item?.name || '', 'directory');
  });
  $('#groupAdminCount').textContent = `${admins.length} 人`;
  $('#groupAdminList').innerHTML = admins.length ? admins.map((item, index) => {
    const name = item.display_name || memberNameForAdmin(item.user_id);
    const roleOptions = Object.entries(ADMIN_ROLE_LABELS).map(([value, label]) => `<option value="${value}" ${item.role === value ? 'selected' : ''}>${label}</option>`).join('');
    const effective = new Set(effectiveAdminPermissions(item));
    const permissions = state.groupAdminPermissions.map(permission => `<label class="${effective.has(permission) ? 'active' : ''}"><input type="checkbox" data-admin-permission="${escapeHtml(permission)}" data-admin-index="${index}" ${effective.has(permission) ? 'checked' : ''} ${item.role !== 'custom' ? 'disabled' : ''}><span>${escapeHtml(ADMIN_PERMISSION_LABELS[permission] || permission)}</span></label>`).join('');
    const warning = item.source === 'manual' ? '手动授权 · 请核对真实 ID' : ((state.groupMemberCatalog[groupId] || []).some(x => String(x.user_id) === String(item.user_id)) ? '当前群成员' : '历史授权 · 当前目录未发现');
    return `<article class="group-admin-card ${state.groupAdminPreviewUserId === item.user_id ? 'selected' : ''}" data-admin-select="${escapeHtml(item.user_id)}"><header><span class="member-avatar">${escapeHtml(name.slice(0, 1))}</span><div><strong>${escapeHtml(name)}</strong><small>${escapeHtml(warning)}</small><code>${escapeHtml(item.user_id)}</code></div><button type="button" class="icon-btn" data-admin-remove="${index}" title="撤销授权"><i class="ph ph-trash"></i></button></header><div class="admin-role-row"><label>角色预设</label><select data-admin-role="${index}">${roleOptions}</select></div><div class="admin-permission-grid">${permissions}</div></article>`;
  }).join('') : '<div class="terminal-empty">尚未添加管理员</div>';
  $$('[data-admin-select]', $('#groupAdminList')).forEach(card => card.onclick = e => {
    if (e.target.closest('select,input,button,label')) return;
    state.groupAdminPreviewUserId = card.dataset.adminSelect; renderGroupAdminPanel(); loadAdminMenuPreview();
  });
  $$('[data-admin-remove]', $('#groupAdminList')).forEach(button => button.onclick = e => {
    e.stopPropagation(); admins.splice(Number(button.dataset.adminRemove), 1);
    if (!admins.some(x => x.user_id === state.groupAdminPreviewUserId)) state.groupAdminPreviewUserId = admins[0]?.user_id || '';
    renderGroupAdminPanel(); loadAdminMenuPreview();
  });
  $$('[data-admin-role]', $('#groupAdminList')).forEach(select => select.onchange = () => {
    const item = admins[Number(select.dataset.adminRole)]; item.role = select.value;
    if (item.role !== 'custom') item.permissions = [];
    renderGroupAdminPanel(); loadAdminMenuPreview();
  });
  $$('[data-admin-permission]', $('#groupAdminList')).forEach(input => input.onchange = () => {
    const item = admins[Number(input.dataset.adminIndex)], permission = input.dataset.adminPermission;
    const set = new Set(item.permissions || []); input.checked ? set.add(permission) : set.delete(permission);
    item.permissions = [...set]; renderGroupAdminPanel(); loadAdminMenuPreview();
  });
}
async function loadGroupAdmins(silent = false) {
  const groupId = currentAdminGroup(); if (!groupId) return;
  $('#groupAdminState').textContent = '正在加载权限…';
  try {
    if (!state.groupMemberCatalog[groupId]) {
      const members = await api(`/api/groups/members?group_id=${encodeURIComponent(groupId)}`);
      state.groupMemberCatalog[groupId] = members.items || [];
      state.groupMemberCatalogMeta[groupId] = members;
    }
    const data = await api(`/api/group-admins?group_id=${encodeURIComponent(groupId)}`);
    state.groupAdmins[groupId] = data.items || []; state.groupAdminRoles = data.roles || {}; state.groupAdminPermissions = data.permissions || [];
    state.groupAdminPreviewUserId = state.groupAdmins[groupId][0]?.user_id || '';
    $('#groupAdminState').textContent = `${data.items?.length || 0} 位管理员 · 按群隔离`;
    renderGroupAdminPanel(); await Promise.all([loadAdminMenuPreview(), loadAdminAudit()]);
  } catch (e) { $('#groupAdminState').textContent = '权限加载失败'; if (!silent) toast(`管理员加载失败：${e.message}`, 'error'); }
}
async function saveGroupAdmins(button) {
  const groupId = currentAdminGroup(); if (!groupId) return;
  setBusy(button, true, '保存中…');
  try {
    const data = await api('/api/group-admins/save', { method: 'POST', body: JSON.stringify({ group_id: groupId, admins: currentGroupAdmins() }) });
    state.groupAdmins[groupId] = data.items || []; state.groupAdminRoles = data.roles || {}; state.groupAdminPermissions = data.permissions || [];
    $('#groupAdminState').textContent = `已保存 ${data.items?.length || 0} 位管理员，立即生效`;
    renderGroupAdminPanel(); await loadAdminMenuPreview(); toast('群管理员权限已实时生效', 'success');
  } catch (e) { toast(`管理员保存失败：${e.message}`, 'error', 7000); }
  finally { setBusy(button, false); }
}
async function loadAdminMenuPreview() {
  const groupId = currentAdminGroup(); if (!groupId) return;
  const local = currentGroupAdmins().find(item => String(item.user_id) === String(state.groupAdminPreviewUserId));
  if (local) {
    const group = state.groupCatalog.find(item => item.id === groupId);
    const permissions = new Set(effectiveAdminPermissions(local));
    const mark = permission => permissions.has(permission) ? '●' : '○';
    $('#groupAdminMenuPreview').textContent = `╭─ ✦ 小风 · 群管理台
│  ${groupDisplayName(group)} · 本群管理员
│  ${ADMIN_ROLE_LABELS[local.role] || '自定义'} · 权限 ${permissions.size}/8
│
│  ◈ 快捷
│  ${mark('status.view')} #状态
│  ${mark('reply.control')} #机器人 开 / 关
│  ${mark('reply.control')} #闭嘴 3m  ·  #开口
│
│  ◈ 功能
│  ${mark('strategy.manage')} #菜单 策略
│  ${mark('media.manage')} #菜单 媒介
│  ${mark('personality.manage')} #菜单 性格
│  ${mark('members.manage')} #菜单 成员
│  ${mark('memory.manage')} #菜单 记忆
╰─ ● 可执行  ○ 仅查看 · #帮助 命令`;
    return;
  }
  try {
    const data = await api(`/api/group-admins/menu-preview?group_id=${encodeURIComponent(groupId)}&user_id=${encodeURIComponent(state.groupAdminPreviewUserId || '')}`);
    $('#groupAdminMenuPreview').textContent = data.text || '暂无预览';
  } catch (e) { $('#groupAdminMenuPreview').textContent = `预览失败：${e.message}`; }
}
async function loadAdminAudit() {
  const groupId = currentAdminGroup(); if (!groupId) return;
  try {
    const data = await api(`/api/group-admins/audit?group_id=${encodeURIComponent(groupId)}&limit=20`);
    $('#groupAdminAuditList').innerHTML = (data.items || []).length ? data.items.map(item => `<article><time>${escapeHtml(item.created_at || '')}</time><div><strong>${escapeHtml(item.display_name || memberNameForAdmin(item.user_id))}</strong><p>${escapeHtml(item.command || '')}</p><small>${item.result === 'success' ? '✓ 执行成功' : item.result === 'failed' ? `× ${escapeHtml(item.error || '执行失败')}` : escapeHtml(item.result || '')}</small></div></article>`).join('') : '<div class="terminal-empty">暂无管理记录</div>';
  } catch (e) { $('#groupAdminAuditList').innerHTML = `<div class="terminal-empty">审计加载失败：${escapeHtml(e.message)}</div>`; }
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
    },
    image_generation: {
      enabled: $('#imageGenEnabled').checked, base_url: $('#imageGenBaseUrl').value.trim(), api_key: $('#imageGenApiKey').value.trim(),
      model: $('#imageGenModel').value.trim(), size: $('#imageGenSize').value, quality: $('#imageGenQuality').value,
      timeout_seconds: Number($('#imageGenTimeout').value || 180), response_format: 'b64_json'
    }
  };
}

function markDirty() { state.dirty = true; $('#aiForm').classList.add('is-dirty'); $('#saveState').textContent = 'UNSAVED CHANGES'; }
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
  setBusy(button, true, '同步中…'); $('#groupDiscoveryState').textContent = '正在通过辅助功能只读扫描当前微信…';
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

async function imageGenerationTest(button) {
  const box = $('#imageGenResult');
  if (!$('#imageGenEnabled').checked) { toast('请先启用对话生图', 'error'); return; }
  if (!$('#imageGenModel').value.trim()) { toast('请填写生图 Model ID', 'error'); return; }
  if (state.dirty && !await saveConfig(button)) return;
  setBusy(button, true, '生成中'); box.className = 'test-result idle';
  box.innerHTML = '<strong>正在真实生图</strong><span>生成完成后会保存到本地，本测试不发送到群。</span>';
  try {
    const r = await api('/api/test/image-generation', { method: 'POST', body: JSON.stringify({ prompt: $('#imageGenTestPrompt').value.trim() }) });
    const preview = r.data_url ? `<img src="${r.data_url}" alt="生图测试结果" style="display:block;max-width:220px;max-height:220px;margin-top:10px;border-radius:10px">` : '';
    box.className = 'test-result success'; box.innerHTML = `<strong>生图成功 · ${Number(r.latency_ms || 0)} ms</strong><span>${escapeHtml(r.model || '')} · ${escapeHtml(r.file || '')}</span>${preview}`;
    toast('生图渠道测试成功');
  } catch (e) {
    box.className = 'test-result error'; box.innerHTML = `<strong>生图失败</strong><span>${escapeHtml(e.message)}</span>`;
    toast(`生图测试失败：${e.message}`, 'error', 10000);
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
    recover: ['/api/onebot/recover', { restart_wechat: false }],
    wechat: ['/api/onebot/recover', { restart_wechat: true }],
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
  const label = { ai: 'AI MODEL', onebot: 'ONEBOT SEND', callback: 'FULL CALLBACK', probe: 'SEND PROBE', recover: 'RECOVER ONEBOT', wechat: 'RESTART WECHAT', at: 'SEND AT', reply: 'SEND REPLY', image: 'SEND IMAGE', file: 'SEND FILE', video: 'SEND VIDEO', record: 'SEND RECORD', sync: 'UI SYNC' }[type] || String(type).toUpperCase();
  entry.innerHTML = `<div class="command">$ ${label}</div><div class="${status === 'error' ? 'error' : status === 'success' ? 'success' : 'latency'}">${escapeHtml(headline)}</div><pre>${escapeHtml(JSON.stringify(payload, null, 2))}</pre>`;
  consoleEl.append(entry); consoleEl.scrollTop = consoleEl.scrollHeight;
}

async function refreshMemoryStats(silent = false) {
  try {
    const r = await api('/api/memory/stats');
    const latest = String(r.latest_message_at || '--');
    $('#memoryStats').innerHTML = `
      <section class="memory-stat-group capacity"><header>消息容量</header><div><span>${r.messages}</span><small>消息总数</small></div><div><span>${r.groups}</span><small>群数量</small></div><div><span>${r.members}</span><small>成员</small></div><div><span>${r.incoming}</span><small>收到</small></div><div><span>${r.outgoing}</span><small>AI 发出</small></div></section>
      <section class="memory-stat-group intelligence"><header>智能索引</header><div><span>${r.personas || 0}</span><small>人物画像</small></div><div><span>${r.vectors || 0}</span><small>向量索引</small></div></section>
      <section class="memory-stat-group media"><header>素材</header><div><span>${r.media_items || 0}</span><small>媒体索引</small></div></section>
      <section class="memory-stat-group recency"><header>最近活动</header><div><span title="${escapeHtml(latest)}">${escapeHtml(latest)}</span><small>最新入库</small></div></section>
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
    setMemoryMode('向量语义检索', `本地 Qwen3 向量命中 ${r.count} 条`, 'VECTOR');
    $('#memoryResults').innerHTML = r.items.length ? r.items.map(x => `<div class="memory-row vector"><div><strong>score ${x.score}</strong><small>${escapeHtml(x.created_at || '')} · ${escapeHtml(x.sender_name || x.user_id || x.direction || '')} · ${escapeHtml(x.event_id || '')}</small></div><p>${escapeHtml(x.text || '')}</p></div>`).join('') : '<div class="terminal-empty">没有语义命中；可以先导入/积累更多聊天记录</div>';
  } catch (e) { toast(`向量检索失败：${e.message}`, 'error'); }
  finally { setBusy(button, false); }
}

const personaCategoryLabels = { fact: '永久事实', interest: '兴趣偏好', habit: '互动习惯', style: '表达风格', role: '群内角色', topic: '高频话题', quote: '经典原话' };
const personaStatusLabels = { completed: '已完成', running: '分析中', queued: '排队中', paused: '已暂停', failed: '失败', cancelled: '已取消', not_analyzed: '未分析', legacy_auto: '旧画像', manual: '人工画像' };

function personaInitial(name) { return [...String(name || '群').trim()][0] || '群'; }
function personaQuery(path, params) { const q = new URLSearchParams(Object.entries(params).filter(([, value]) => value !== '' && value != null)); return `${path}?${q}`; }

async function loadPersonaMembers(silent = false, preserveSelection = true) {
  const groupId = $('#personaGroup')?.value || '';
  if (!groupId) return;
  try {
    const result = await api(personaQuery('/api/personas/list', { group_id: groupId, query: $('#personaSearch').value.trim(), status: $('#personaStatus').value, page_size: 500 }));
    state.persona.members = result.items || [];
    $('#personaDirectoryMeta').textContent = `${result.total || 0} 位群成员 · 当前显示 ${result.count || 0} 位`;
    renderPersonaMembers();
    const selectedExists = state.persona.members.some(x => x.user_id === state.persona.selectedUserId);
    if ((!preserveSelection || !selectedExists) && state.persona.members.length) state.persona.selectedUserId = state.persona.members[0].user_id;
    if (state.persona.selectedUserId) await loadPersonaDetail(state.persona.selectedUserId, true);
    else renderPersonaEmpty();
  } catch (e) { if (!silent) toast(`画像目录读取失败：${e.message}`, 'error'); }
}

function renderPersonaMembers() {
  const current = state.persona.selectedUserId;
  $('#personaMemberList').innerHTML = state.persona.members.length ? state.persona.members.map(item => {
    const name = item.display_name || item.card || item.nickname || item.user_id;
    const aliases = (item.aliases || []).join('、');
    const status = item.analysis_status || 'not_analyzed';
    const memberTitle = `${name} · ${item.user_id}${aliases ? ` · 外号：${aliases}` : ''}`;
    return `<button class="persona-member ${item.user_id === current ? 'active' : ''}" data-persona-user="${escapeHtml(item.user_id)}" title="${escapeHtml(memberTitle)}"><span class="persona-list-avatar">${escapeHtml(personaInitial(name))}</span><span class="persona-list-main"><strong title="${escapeHtml(name)}">${escapeHtml(name)}</strong><small title="${escapeHtml(aliases ? `外号：${aliases}` : item.user_id)}">${escapeHtml(aliases ? `外号：${aliases}` : item.user_id)}</small><em>${Number(item.message_count || 0).toLocaleString()} 条 · ${escapeHtml(item.last_seen || '尚无发言')}</em></span><span class="persona-list-state ${escapeHtml(status)}">${escapeHtml(personaStatusLabels[status] || status)}</span></button>`;
  }).join('') : '<div class="terminal-empty">当前筛选条件下没有成员</div>';
  $$('[data-persona-user]', $('#personaMemberList')).forEach(button => button.onclick = () => {
    state.persona.selectedUserId = button.dataset.personaUser;
    $('#page-personas').dataset.mobilePane = 'detail';
    selectPersonaTab('overview'); renderPersonaMembers();
    if (matchMedia('(max-width: 640px)').matches) window.scrollTo({ top: 0, behavior: 'auto' });
    loadPersonaDetail(button.dataset.personaUser);
  });
}

function renderPersonaEmpty() {
  state.persona.detail = null; $('#personaEmpty').hidden = false; $('#personaProfileContent').hidden = true;
  $('#personaKpis').innerHTML = ''; $('#personaHeatmap').innerHTML = ''; $('#personaTrend').innerHTML = '';
  $('#personaGraph').innerHTML = '<div class="terminal-empty">选择成员后生成关系图</div>';
  $('#personaEvidence').innerHTML = '<div class="terminal-empty">暂无证据</div>';
}

async function loadPersonaDetail(userId, silent = false) {
  if (!userId || !$('#personaGroup').value) return;
  try {
    const detail = await api(personaQuery('/api/personas/detail', { group_id: $('#personaGroup').value, user_id: userId }));
    if (state.persona.selectedUserId !== userId) return;
    state.persona.detail = detail; renderPersonaDetail(detail); renderPersonaMembers();
  } catch (e) { if (!silent) toast(`画像详情读取失败：${e.message}`, 'error'); }
}

function renderPersonaDetail(detail) {
  const p = detail.profile || {}, metrics = detail.metrics || {}, claims = detail.claims || [];
  $('#personaEmpty').hidden = true; $('#personaProfileContent').hidden = false;
  $('#personaAvatar').textContent = personaInitial(p.display_name); $('#personaName').textContent = p.display_name || p.user_id;
  const aliases = (detail.aliases || []).map(x => x.alias).filter(Boolean);
  $('#personaIdentity').textContent = [aliases.length ? `外号 ${aliases.join('、')}` : '', p.user_id, `${Number(p.message_count || 0).toLocaleString()} 条消息`].filter(Boolean).join(' · ');
  const status = p.analysis_status || 'not_analyzed'; $('#personaState').className = `persona-state ${status}`; $('#personaState').textContent = personaStatusLabels[status] || status;
  $('#personaAnalysisCallout').hidden = !['not_analyzed', 'failed'].includes(status);
  $('#personaSummary').textContent = p.summary || '尚未生成摘要。点击“分析选中成员”读取该成员全部历史消息。';
  const facts = [...(p.manual_facts || []).map(x => ({ value: typeof x === 'string' ? x : x.value, manual: true })), ...(p.tags || []).map(value => ({ value, tag: true }))];
  $('#personaFacts').innerHTML = facts.length ? facts.map(x => `<span class="${x.manual ? 'manual' : ''}">${escapeHtml(x.value || '')}${x.manual ? '<i>人工</i>' : ''}</span>`).join('') : '<small class="persona-muted">暂无永久事实或标签</small>';
  const memes = detail.memes || []; $('#personaMemeSection').hidden = !memes.length;
  $('#personaMemes').innerHTML = memes.map(x => `<span title="${escapeHtml(x.meaning || '')}">${escapeHtml(x.name)}<i>${Math.round(Number(x.confidence || 0) * 100)}%</i></span>`).join('');
  const grouped = Object.entries(personaCategoryLabels).map(([key, label]) => [key, label, claims.filter(x => x.category === key).slice(0, key === 'quote' ? 5 : 8)]).filter(([, , rows]) => rows.length);
  $('#personaClaims').innerHTML = grouped.length ? grouped.map(([key, label, rows]) => `<section><div class="persona-section-title"><h3>${label}</h3><span>${rows.length}</span></div>${rows.map(x => `<p class="persona-claim"><span>${escapeHtml(x.value)}</span><small>${Math.round(Number(x.confidence || 0) * 100)}% · ${x.source === 'manual' ? '人工' : '有证据'}</small></p>`).join('')}</section>`).join('') : '<div class="terminal-empty">尚无结构化画像结论</div>';
  renderPersonaBehavior(metrics); renderPersonaGraph(p, detail.relationships || []); renderPersonaEvidence(claims);
  const activeJob = (detail.jobs || []).find(x => ['queued', 'running', 'paused'].includes(x.status));
  $('#personaProgress').hidden = !activeJob;
  if (activeJob) { const percent = activeJob.total_messages ? Math.round(activeJob.processed_messages / activeJob.total_messages * 100) : 100; $('#personaProgress i').style.width = `${percent}%`; $('#personaProgress small').textContent = `${personaStatusLabels[activeJob.status]} · ${activeJob.processed_messages}/${activeJob.total_messages} · ${percent}%`; $('#personaJobState').textContent = `${percent}%`; $('#personaProgressAction').textContent = activeJob.status === 'paused' ? '继续' : '暂停'; $('#personaProgressAction').dataset.jobId = activeJob.id; $('#personaProgressAction').dataset.action = activeJob.status === 'paused' ? 'resume' : 'pause'; }
  else $('#personaJobState').textContent = status === 'completed' ? 'READY' : (personaStatusLabels[status] || 'READY');
}

function renderPersonaBehavior(metrics) {
  const media = metrics.media || {};
  $('#personaKpis').innerHTML = `<div><strong>${Number(metrics.message_count || 0).toLocaleString()}</strong><small>历史消息</small></div><div><strong>${Number(metrics.average_length || 0).toFixed(1)}</strong><small>平均字数</small></div><div><strong>${Math.round(Number(metrics.media_ratio || 0) * 100)}%</strong><small>媒体比例</small></div><div><strong>${(metrics.interactions || []).length}</strong><small>常聊对象</small></div>`;
  const heat = metrics.heatmap || Array.from({ length: 7 }, () => Array(24).fill(0)); const maxHeat = Math.max(1, ...heat.flat());
  $('#personaHeatmap').innerHTML = heat.flatMap((day, weekday) => day.map((count, hour) => `<i style="--level:${Math.max(.06, count / maxHeat)}" title="周${'一二三四五六日'[weekday]} ${String(hour).padStart(2, '0')}:00 · ${count} 条"></i>`)).join('');
  const trend = metrics.trend || [], maxTrend = Math.max(1, ...trend.map(x => x.count));
  $('#personaTrend').innerHTML = trend.length ? `<div class="persona-trend-bars">${trend.map(x => `<i style="height:${Math.max(3, x.count / maxTrend * 100)}%" title="${escapeHtml(x.date)} · ${x.count} 条"></i>`).join('')}</div><div class="persona-trend-meta"><span>${escapeHtml(trend[0].date)}</span><strong>消息趋势</strong><span>${escapeHtml(trend.at(-1).date)}</span></div>` : '<div class="terminal-empty">暂无趋势数据</div>';
  $('#personaTopics').innerHTML = `<div><h3>高频话题</h3>${(metrics.topics || []).slice(0, 12).map(x => `<span>${escapeHtml(x.name)}<i>${x.count}</i></span>`).join('') || '<small>暂无</small>'}</div><div><h3>媒体构成</h3>${Object.entries(media).map(([key, count]) => `<span>${escapeHtml(key)}<i>${count}</i></span>`).join('') || '<small>纯文字为主</small>'}</div>`;
}

function renderPersonaGraph(profile, rows) {
  if (!rows.length) { $('#personaGraph').innerHTML = '<div class="terminal-empty">暂未观察到稳定互动关系</div>'; return; }
  const width = 520, height = 310, cx = 260, cy = 155, maxCount = Math.max(1, ...rows.map(x => Number(x.count || 0)));
  const nodes = rows.slice(0, 20).map((row, index) => { const angle = (Math.PI * 2 * index / Math.max(rows.length, 1)) - Math.PI / 2; const radius = rows.length > 12 ? (index % 2 ? 132 : 105) : 118; return { ...row, x: cx + Math.cos(angle) * radius, y: cy + Math.sin(angle) * radius, size: 25 + 15 * Number(row.count || 0) / maxCount }; });
  const lines = nodes.map(x => `<line x1="${cx}" y1="${cy}" x2="${x.x}" y2="${x.y}" style="stroke-width:${1 + 4 * x.count / maxCount}"/><text x="${(cx + x.x) / 2}" y="${(cy + x.y) / 2 - 4}">${escapeHtml((x.relations || []).join('、'))}</text>`).join('');
  const nodeHtml = nodes.map(x => `<button class="persona-graph-node" style="left:${x.x / width * 100}%;top:${x.y / height * 100}%;width:${x.size}px;height:${x.size}px" title="${escapeHtml(x.name)} · ${x.count} 次互动${x.relations?.length ? ` · ${x.relations.join('、')}` : ''}">${escapeHtml(personaInitial(x.name))}<span>${escapeHtml(x.name)}</span></button>`).join('');
  $('#personaGraph').innerHTML = `<svg viewBox="0 0 ${width} ${height}" aria-hidden="true">${lines}</svg><div class="persona-graph-center"><b>${escapeHtml(personaInitial(profile.display_name))}</b><span>${escapeHtml(profile.display_name)}</span></div>${nodeHtml}`;
}

function renderPersonaEvidence(claims) {
  const filter = $('#personaClaimFilter').value; const rows = claims.filter(x => !filter || x.category === filter).flatMap(claim => (claim.evidence || []).map(evidence => ({ claim, evidence })));
  rows.sort((a, b) => String(b.evidence.time || '').localeCompare(String(a.evidence.time || '')));
  $('#personaEvidence').innerHTML = rows.length ? rows.map(({ claim, evidence }) => `<article><span class="persona-evidence-dot"></span><div><header><strong>${escapeHtml(personaCategoryLabels[claim.category] || claim.category)}</strong><time>${escapeHtml(evidence.time || '')}</time></header><h3>${escapeHtml(claim.value)}</h3><blockquote>${escapeHtml(evidence.text || '')}</blockquote><footer>message_id: ${escapeHtml(evidence.message_id || evidence.event_id || '--')} · 置信度 ${Math.round(Number(claim.confidence || 0) * 100)}%</footer></div></article>`).join('') : '<div class="terminal-empty">当前类别暂无可定位证据</div>';
}

async function analyzePersona(scope, button) {
  const userId = scope === 'member' ? state.persona.selectedUserId : '';
  if (scope === 'member' && !userId) return toast('请先选择成员', 'error');
  setBusy(button, true, '排队中…');
  try { const result = await api('/api/personas/analyze', { method: 'POST', body: JSON.stringify({ group_id: $('#personaGroup').value, user_id: userId, mode: 'full' }) }); toast(`已创建 ${result.queued} 个画像任务`); await loadPersonaMembers(true); }
  catch (e) { toast(`画像分析启动失败：${e.message}`, 'error'); } finally { setBusy(button, false); }
}

async function controlPersonaJob(button) {
  const jobId = Number(button.dataset.jobId || 0), action = button.dataset.action;
  if (!jobId || !action) return;
  button.disabled = true;
  try { await api('/api/personas/analyze', { method: 'POST', body: JSON.stringify({ action, job_id: jobId }) }); toast(action === 'pause' ? '画像分析已暂停' : '画像分析已继续'); await loadPersonaDetail(state.persona.selectedUserId, true); }
  catch (e) { toast(`任务操作失败：${e.message}`, 'error'); } finally { button.disabled = false; }
}

function openPersonaEditor() {
  const detail = state.persona.detail; if (!detail) return;
  const p = detail.profile || {}; $('#personaEditorIdentity').textContent = `${p.display_name || p.user_id} · ${p.group_id}`;
  $('#personaManualSummary').value = p.manual_summary || ''; $('#personaManualTags').value = (p.manual_tags || []).join(', ');
  $('#personaManualFacts').value = (p.manual_facts || []).map(x => typeof x === 'string' ? x : x.value).filter(Boolean).join('\n'); $('#personaEditor').showModal();
}

async function savePersonaEditor() {
  const p = state.persona.detail?.profile; if (!p) return;
  const tags = $('#personaManualTags').value.split(/[,，]/).map(x => x.trim()).filter(Boolean); const facts = $('#personaManualFacts').value.split('\n').map(x => x.trim()).filter(Boolean);
  await api('/api/personas/save', { method: 'POST', body: JSON.stringify({ group_id: p.group_id, user_id: p.user_id, summary: $('#personaManualSummary').value.trim(), tags, facts }) });
  $('#personaEditor').close(); toast('人工画像已保存并实时生效'); await loadPersonaDetail(p.user_id);
}

function selectPersonaTab(tab) {
  state.persona.tab = tab; $$('[data-persona-tab]').forEach(x => x.classList.toggle('active', x.dataset.personaTab === tab));
  $('#page-personas').dataset.mobileTab = tab;
  if (tab === 'edit') openPersonaEditor();
}

function showPersonaDirectory() {
  $('#page-personas').dataset.mobilePane = 'directory';
  requestAnimationFrame(() => $('#personaMemberList')?.focus?.());
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

function orbitAudioMarkup(src, label = '试听') {
  return `<div class="orbit-audio-player"><button type="button" class="orbit-audio-toggle" aria-label="${escapeHtml(label)}"><i class="ph ph-play"></i><span>${escapeHtml(label)}</span></button><span class="orbit-audio-track"><i></i></span><time>--:--</time><audio preload="metadata" src="${escapeHtml(src)}"></audio></div>`;
}

function bindOrbitAudioPlayers(root = document) {
  $$('.orbit-audio-player', root).forEach(player => {
    if (player.dataset.bound) return; player.dataset.bound = '1';
    const audio = $('audio', player); const button = $('.orbit-audio-toggle', player); const icon = $('i', button); const label = $('span', button); const progress = $('.orbit-audio-track i', player); const time = $('time', player);
    const format = seconds => Number.isFinite(seconds) ? `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, '0')}` : '--:--';
    const sync = () => { progress.style.width = `${audio.duration ? Math.min(100, audio.currentTime / audio.duration * 100) : 0}%`; time.textContent = format(audio.currentTime || audio.duration); };
    audio.addEventListener('loadedmetadata', () => { time.textContent = format(audio.duration); });
    audio.addEventListener('timeupdate', sync);
    audio.addEventListener('ended', () => { icon.className = 'ph ph-play'; label.textContent = '试听'; audio.currentTime = 0; sync(); });
    button.addEventListener('click', async () => {
      if (audio.paused) {
        $$('audio').forEach(other => { if (other !== audio && !other.paused) other.pause(); });
        try { await audio.play(); icon.className = 'ph ph-pause'; label.textContent = '暂停'; } catch (error) { toast(`音频试听失败：${error.message}`, 'error'); }
      } else { audio.pause(); icon.className = 'ph ph-play'; label.textContent = '试听'; }
    });
  });
}

function bindMediaCardActions(root = document) {
  $$('.media-analyze', root).forEach(btn => btn.onclick = () => analyzeMedia(btn));
  $$('.media-annotate', root).forEach(btn => btn.onclick = () => editMedia(btn));
  $$('.media-ocr', root).forEach(btn => btn.onclick = () => ocrMedia(btn));
  $$('.media-asr', root).forEach(btn => btn.onclick = () => asrMedia(btn));
  $$('.media-preview', root).forEach(btn => btn.onclick = () => previewMedia(btn));
  $$('.media-expand', root).forEach(btn => btn.onclick = () => previewContent(btn));
  bindOrbitAudioPlayers(root);
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

function compactFileLabel(value) {
  const raw = String(value || '');
  if (!raw) return '';
  const clean = raw.split(/[?#]/)[0];
  return decodeURIComponent(clean.split(/[\\/]/).filter(Boolean).at(-1) || clean);
}

function mediaCard(x) {
  const tags = [...(x.tags || []), ...(x.keywords || [])].filter(Boolean).slice(0, 12);
  const typeLabel = (x.media_type || 'MEDIA').toUpperCase();
  const isVoiceWaiting = x.media_type === 'record' && x.status === 'waiting_transcript';
  const rawFile = x.file || x.url || '', shortFile = compactFileLabel(rawFile);
  const preview = x.data_url ? `<button class="media-thumb media-preview" data-src="${escapeHtml(x.data_url)}" data-title="媒体 #${x.id}"><img src="${escapeHtml(x.data_url)}" alt="media ${x.id}"></button>` : `<div class="media-thumb placeholder ${escapeHtml(x.media_type || '')}" title="${escapeHtml(rawFile)}"><span>${escapeHtml(typeLabel)}</span><small>${escapeHtml(shortFile || '已建立索引')}</small></div>`;
  const audio = x.audio_url ? orbitAudioMarkup(x.audio_url, '试听') : '';
  const fileText = rawFile;
  return `<article class="media-card" data-media-id="${x.id}">
    ${preview}
    <div class="media-card-body">
      <div class="media-title"><strong>${escapeHtml(x.media_type)} #${x.id}</strong><span class="status-pill ${escapeHtml(x.status || 'indexed')}">${escapeHtml(x.status || 'indexed')}</span></div>
      <small>${escapeHtml(x.created_at || '')} · ${escapeHtml(x.sender_name || x.user_id || '未知发送者')}</small>
      ${contentBlock('摘要', x.image_summary || '待解析', `媒体 #${x.id} 摘要`)}
      ${contentBlock('OCR', x.ocr_text || '无', `媒体 #${x.id} OCR`)}
      ${audio}
      ${fileText ? contentBlock('文件', shortFile, `媒体 #${x.id} 文件`) : ''}
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
  const audio = x.audio_url ? orbitAudioMarkup(x.audio_url, '试听') : '<div class="media-help-card"><strong>暂无可试听音频</strong><p>还没拿到本地 silk/wav 文件，等待 OneBot 下载缓存入库。</p></div>';
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
    if ($('#page-faces').classList.contains('active')) loadFaces(null, true);
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
    if ($('#page-faces').classList.contains('active')) loadFaces(null, true);
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
    <div class="voice-list-player">${orbitAudioMarkup(`/api/voicepacks/audio?id=${encodeURIComponent(x.id)}`, '试听')}<small>${Math.round((x.duration_ms || 0) / 1000)} 秒 · ${Math.round((x.size || 0) / 1024)} KB · 使用 ${x.usage_count || 0} 次</small></div>
    <div class="voice-list-tags">${match}${tagHtml}</div>
    <div class="voice-list-actions"><button class="link-btn voice-send" data-voice-id="${x.id}">发送</button><button class="link-btn voice-copy" data-text="${escapeHtml(text)}">复制</button><button class="link-btn danger-text voice-delete" data-voice-id="${x.id}" data-voice-title="${escapeHtml(text)}">删除</button></div>
  </div>`;
}

function bindVoiceListActions(root) {
  $$('.voice-send', root).forEach(btn => btn.onclick = () => sendVoicepack(btn));
  $$('.voice-copy', root).forEach(btn => btn.onclick = async () => { await navigator.clipboard.writeText(btn.dataset.text || ''); toast('语音文案已复制'); });
  $$('.voice-delete', root).forEach(btn => btn.onclick = () => deleteVoiceItem(btn));
  $$('.media-expand', root).forEach(btn => btn.onclick = () => previewContent(btn));
  bindOrbitAudioPlayers(root);
}

function renderVoiceVirtualList(items) {
  const root = $('#voiceResults');
  const rowHeight = matchMedia('(max-width: 640px)').matches ? 272 : 82;
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
  const oldImportPack = $('#voiceImportTargetPack').value;
  const cats = [...new Set((packs || []).map(x => x.category).filter(Boolean))].sort();
  $('#voiceCategory').innerHTML = '<option value="">全部文件夹</option>' + cats.map(x => `<option value="${escapeHtml(x)}">${escapeHtml(x)}</option>`).join('');
  if ([...$('#voiceCategory').options].some(x => x.value === oldCategory)) $('#voiceCategory').value = oldCategory;
  const visiblePacks = (packs || []).filter(x => !$('#voiceCategory').value || x.category === $('#voiceCategory').value);
  $('#voicePackFilter').innerHTML = '<option value="">全部语音包</option>' + visiblePacks.map(x => `<option value="${x.id}">${x.sequence || ''}. ${escapeHtml(x.name || '未命名语音包')} · ${x.item_count || 0} 条</option>`).join('');
  if ([...$('#voicePackFilter').options].some(x => x.value === oldPack)) $('#voicePackFilter').value = oldPack;
  $('#voiceImportTargetPack').innerHTML = '<option value="">自动创建新语音包</option>' + (packs || []).map(x => `<option value="${x.id}">${escapeHtml(x.category || '未分类')} / ${escapeHtml(x.name || '未命名语音包')} · ${x.item_count || 0} 条</option>`).join('');
  if ([...$('#voiceImportTargetPack').options].some(x => x.value === oldImportPack)) $('#voiceImportTargetPack').value = oldImportPack;
  $('#voiceDeletePackBtn').disabled = !$('#voicePackFilter').value;
  syncVoiceImportTarget();
}

function syncVoiceImportTarget() {
  const appending = !!$('#voiceImportTargetPack').value;
  $('#voiceImportCategory').disabled = appending;
}

function renderVoiceImportSelection() {
  const root = $('#voiceImportSelection');
  const clear = $('#voiceImportClearBtn');
  if (!voiceImportFiles.length) {
    root.innerHTML = '<strong>尚未选择文件</strong><span>可一次选择多个 zip / zip1 压缩包或音频文件</span>';
    clear.hidden = true;
    return;
  }
  const total = voiceImportFiles.reduce((sum, file) => sum + file.size, 0);
  const names = voiceImportFiles.slice(0, 4).map(file => escapeHtml(file.name)).join('、');
  root.innerHTML = `<strong>已选择 ${voiceImportFiles.length} 个文件 · ${(total / 1024 / 1024).toFixed(1)} MB</strong><span>${names}${voiceImportFiles.length > 4 ? ` 等 ${voiceImportFiles.length} 个文件` : ''}</span>`;
  clear.hidden = false;
}

function chooseVoiceImportFiles(files) {
  voiceImportFiles = [...files];
  voiceImportUploadedPaths = [];
  renderVoiceImportSelection();
}

function clearVoiceImportFiles() {
  voiceImportFiles = [];
  voiceImportUploadedPaths = [];
  $('#voiceImportFiles').value = '';
  renderVoiceImportSelection();
}

async function uploadSelectedVoicepacks() {
  if (!voiceImportFiles.length) return [];
  if (voiceImportUploadedPaths.length === voiceImportFiles.length) return voiceImportUploadedPaths;
  voiceImportUploadedPaths = [];
  for (let index = 0; index < voiceImportFiles.length; index += 1) {
    const file = voiceImportFiles[index];
    $('#voiceImportSelection').innerHTML = `<strong>正在上传 ${index + 1} / ${voiceImportFiles.length}</strong><span>${escapeHtml(file.name)} · ${(file.size / 1024 / 1024).toFixed(1)} MB</span>`;
    const result = await api('/api/voicepacks/upload', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/octet-stream',
        'X-Voicepack-Filename': encodeURIComponent(file.name),
      },
      body: file,
    });
    voiceImportUploadedPaths.push(result.path);
  }
  $('#voiceImportPaths').value = voiceImportUploadedPaths.join('\n');
  renderVoiceImportSelection();
  return voiceImportUploadedPaths;
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
    await uploadSelectedVoicepacks();
    const paths = $('#voiceImportPaths').value.split(/\r?\n/).map(x => x.trim()).filter(Boolean);
    if (!paths.length) throw new Error('请先选择压缩包/音频文件，或填写来源路径');
    const r = await api('/api/voicepacks/plan', { method: 'POST', body: JSON.stringify({ paths, category: $('#voiceImportCategory').value.trim(), target_pack_id: $('#voiceImportTargetPack').value || 0 }) });
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
    await uploadSelectedVoicepacks();
    const paths = $('#voiceImportPaths').value.split(/\r?\n/).map(x => x.trim()).filter(Boolean);
    if (!paths.length) throw new Error('请先选择压缩包/音频文件，或填写来源路径');
    const r = await api('/api/voicepacks/import', { method: 'POST', body: JSON.stringify({ paths, category: $('#voiceImportCategory').value.trim(), target_pack_id: $('#voiceImportTargetPack').value || 0 }) });
    toast(`导入完成：新增 ${r.imported} 条，跳过 ${r.skipped} 条，错误 ${r.errors?.length || 0} 个`, r.errors?.length ? 'error' : 'success', 9000);
    if (r.pack_summary?.length) $('#voiceImportPlan').innerHTML = '<strong>最近一次导入</strong><ul>' + r.pack_summary.map(x => '<li><strong>' + escapeHtml(x.pack_name) + '</strong><span>新增 ' + x.imported + ' · 跳过 ' + x.skipped + '</span></li>').join('') + '</ul>';
    clearVoiceImportFiles();
    $('#voiceImportPaths').value = '';
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

async function deleteVoiceItem(button) {
  const title = button.dataset.voiceTitle || `#${button.dataset.voiceId}`;
  if (!window.confirm(`确定删除语音“${title}”吗？删除后会立即退出检索。`)) return;
  setBusy(button, true, '删除中');
  try {
    const r = await api('/api/voicepacks/delete-item', { method: 'POST', body: JSON.stringify({ id: button.dataset.voiceId, delete_file: true }) });
    toast(`已删除语音和索引：${r.deleted || 0} 条`);
    await loadVoicepacks(null, true);
  } catch (e) { toast(`删除语音失败：${e.message}`, 'error', 10000); }
  finally { setBusy(button, false); }
}

async function deleteCurrentVoicePack(button) {
  const select = $('#voicePackFilter');
  const packId = select.value;
  if (!packId) return;
  const label = select.options[select.selectedIndex]?.textContent || `#${packId}`;
  if (!window.confirm(`确定删除整个语音包“${label}”吗？包内语音、文件和检索索引都会删除。`)) return;
  setBusy(button, true, '删除中…');
  try {
    const r = await api('/api/voicepacks/delete-pack', { method: 'POST', body: JSON.stringify({ pack_id: packId, delete_files: true }) });
    select.value = '';
    toast(`已删除语音包：${r.deleted || 0} 条语音，清理 ${r.embedding_count || 0} 条向量`);
    await loadVoicepacks(null, true);
  } catch (e) { toast(`删除语音包失败：${e.message}`, 'error', 10000); }
  finally { setBusy(button, false); }
}

function faceCard(x) {
  const tags = [...(x.tags || []), ...(x.keywords || []), ...(x.emotions || [])].slice(0, 5);
  const src = x.data_url || '';
  const title = x.image_summary || x.ocr_text || '未解析表情包';
  return `<article class="face-card media-card" data-face-id="${x.id}">
    <div class="media-thumb ${src ? '' : 'empty'}">
      ${src ? `<img src="${escapeHtml(src)}" alt="${escapeHtml(title)}">` : '<span>GIF</span>'}
    </div>
    <div class="media-info">
      <div class="media-title"><strong>face #${x.id}</strong><span>${escapeHtml(x.status || 'indexed')}</span></div>
      <p class="face-card-summary">${escapeHtml(title)}</p>
      ${tags.length ? `<div class="tag-list">${tags.map(t => `<span>${escapeHtml(t)}</span>`).join('')}</div>` : '<div class="tag-list"><span>待补充语义</span></div>'}
      <div class="media-actions">
        <button class="link-btn poke-face-select" data-face-id="${x.id}">${state.pokeFaceIds.has(Number(x.id)) ? '已选拍一拍' : '选为拍一拍'}</button>
        <button class="link-btn face-send" data-face-id="${x.id}">发送到目标群</button>
        <button class="link-btn face-details" data-face-id="${x.id}">查看详情</button>
      </div>
    </div>
  </article>`;
}

function openFaceDetail(faceId) {
  const x = state.faceItems.find(item => String(item.id) === String(faceId)); if (!x) return;
  const src = x.data_url || '', tags = [...(x.tags || []), ...(x.keywords || []), ...(x.aliases || []), ...(x.emotions || []), ...(x.intents || [])];
  $('#faceDetailTitle').textContent = `face #${x.id}`;
  $('#faceDetailBody').innerHTML = `${src ? `<div class="face-detail-preview"><img src="${escapeHtml(src)}" alt="表情 #${x.id}"></div>` : '<div class="face-detail-preview"><span>暂无可预览文件</span></div>'}
    <section class="face-detail-section"><h3>摘要</h3><p>${escapeHtml(x.image_summary || '尚未生成摘要')}</p></section>
    <section class="face-detail-section"><h3>OCR 与语义</h3><p>${escapeHtml(x.ocr_text || '没有识别到文字')}</p>${tags.length ? `<div class="tag-list">${[...new Set(tags)].map(tag => `<span>${escapeHtml(tag)}</span>`).join('')}</div>` : ''}</section>
    <section class="face-detail-section"><h3>文件与索引</h3><code>${escapeHtml(x.file || '文件路径不可用')}</code></section>
    <div class="face-detail-actions">
      <button class="btn primary face-send" data-face-id="${x.id}">发送到目标群</button>
      <button class="btn subtle media-ocr" data-media-id="${x.canonical_media_id || ''}">重新解析</button>
      <button class="btn subtle media-edit" data-media-id="${x.canonical_media_id || ''}" data-summary="${escapeHtml(x.image_summary || '')}" data-ocr="${escapeHtml(x.ocr_text || '')}" data-tags="${escapeHtml((x.tags || []).join('，'))}" data-keywords="${escapeHtml((x.keywords || []).join('，'))}">编辑内容</button>
      <button class="btn subtle face-semantic-edit" data-face-id="${x.id}" data-aliases="${escapeHtml((x.aliases || []).join('，'))}" data-emotions="${escapeHtml((x.emotions || []).join('，'))}" data-intents="${escapeHtml((x.intents || []).join('，'))}">语义标签</button>
      <button class="btn ghost face-toggle" data-face-id="${x.id}" data-enabled="${Number(x.enabled) ? '1' : '0'}">${Number(x.enabled) ? '屏蔽素材' : '启用素材'}</button>
      <button class="btn ghost face-group-toggle" data-face-id="${x.id}" data-enabled="${Number(x.group_enabled) ? '1' : '0'}">${Number(x.group_enabled) ? '当前群禁用' : '当前群启用'}</button>
    </div>`;
  const root = $('#faceDetailBody');
  $$('.face-send', root).forEach(button => button.onclick = () => sendFace(button));
  $$('.media-ocr', root).forEach(button => button.onclick = () => ocrMedia(button));
  $$('.media-edit', root).forEach(button => button.onclick = () => editMedia(button));
  $$('.face-toggle', root).forEach(button => button.onclick = () => toggleFace(button));
  $$('.face-group-toggle', root).forEach(button => button.onclick = () => toggleFaceForGroup(button));
  $$('.face-semantic-edit', root).forEach(button => button.onclick = () => editFaceSemantics(button));
  $('#faceDetailDialog').showModal();
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
    state.faceItems = r.items || [];
    $('#faceResults').innerHTML = r.items.length ? `<div class="face-grid media-gallery">${r.items.map(faceCard).join('')}</div>` : '<div class="voice-empty">暂无 face 表情包。群里收到表情包后会自动进入这里。</div>';
    $$('.face-preview', $('#faceResults')).forEach(btn => btn.onclick = () => previewMedia(btn));
    $$('.face-send', $('#faceResults')).forEach(btn => btn.onclick = () => sendFace(btn));
    $$('.media-ocr', $('#faceResults')).forEach(btn => btn.onclick = () => ocrMedia(btn));
    $$('.media-edit', $('#faceResults')).forEach(btn => btn.onclick = () => editMedia(btn));
    $$('.face-toggle', $('#faceResults')).forEach(btn => btn.onclick = () => toggleFace(btn));
    $$('.face-group-toggle', $('#faceResults')).forEach(btn => btn.onclick = () => toggleFaceForGroup(btn));
    $$('.face-semantic-edit', $('#faceResults')).forEach(btn => btn.onclick = () => editFaceSemantics(btn));
    $$('.face-details', $('#faceResults')).forEach(btn => btn.onclick = () => openFaceDetail(btn.dataset.faceId));
    $$('.poke-face-select', $('#faceResults')).forEach(btn => btn.onclick = () => togglePokeFace(btn.dataset.faceId));
    $$('.media-expand', $('#faceResults')).forEach(btn => btn.onclick = () => previewContent(btn));
    renderPokeSelectedFaces();
    if (chip) { chip.textContent = `表情 ${r.count} 个`; chip.className = 'health-chip good'; }
  } catch (e) {
    if (chip) { chip.textContent = '读取失败'; chip.className = 'health-chip warn'; }
    if (!silent) toast(`表情包读取失败：${e.message}`, 'error');
  } finally { if (button) setBusy(button, false); }
}

function renderPokeSelectedFaces() {
  const byId = new Map(state.faceItems.map(x => [Number(x.id), x]));
  const items = [...state.pokeFaceIds].map(id => byId.get(Number(id)) || { id: Number(id), missing: true });
  const count = $('#pokeSelectedCount'); if (count) count.textContent = `${items.length} 张`;
  $('#pokeSelectedFaces').innerHTML = items.length ? items.map(x => {
    const source = String(x.file || x.url || '').toLowerCase();
    const animated = source.includes('.gif');
    const nativeReady = String(x.meta_json || '').includes('<emoji');
    const status = x.missing ? '待加载' : nativeReady && animated ? 'GIF 原生' : animated ? 'GIF 上传' : '图片上传';
    return `<button type="button" class="poke-selected-face ${animated ? 'is-gif' : ''}" data-face-id="${x.id}" title="点击移除 #${x.id}">${x.data_url ? `<img src="${escapeHtml(x.data_url)}" alt="face #${x.id}">` : `<span>${animated ? 'GIF' : '#' + x.id}</span>`}<i>${escapeHtml(status)}</i><b>#${x.id} ×</b></button>`;
  }).join('') : '<small>请在右侧素材中点击“选为拍一拍”，或直接上传图片</small>';
  $$('.poke-selected-face', $('#pokeSelectedFaces')).forEach(button => button.onclick = () => togglePokeFace(button.dataset.faceId));
}

function togglePokeFace(faceId) {
  const id = Number(faceId);
  state.pokeFaceIds.has(id) ? state.pokeFaceIds.delete(id) : state.pokeFaceIds.add(id);
  if (state.pokeFaceIds.size) $('#pokeImageEnabled').checked = true;
  else $('#pokeImageEnabled').checked = false;
  renderPokeSelectedFaces();
  $$('.poke-face-select').forEach(button => { if (Number(button.dataset.faceId) === id) button.textContent = state.pokeFaceIds.has(id) ? '已选拍一拍' : '选为拍一拍'; });
  schedulePokeConfigSave('图片选择已保存');
}

async function loadPokeConfig() {
  const value = await api('/api/poke-reply/config');
  $('#pokeEnabled').checked = value.enabled; $('#pokeTextEnabled').checked = value.text_enabled;
  $('#pokeImageEnabled').checked = value.image_enabled; $('#pokeTexts').value = (value.texts || []).join('\n');
  $('#pokeCooldown').value = value.cooldown_seconds ?? 8; state.pokeFaceIds = new Set((value.face_ids || []).map(Number));
  renderPokeSelectedFaces();
  updatePokeSaveState('已同步', 'good');
}

let pokeSaveTimer = null;
let pokeSaveSerial = 0;
let pokeSaveQueue = Promise.resolve();

function updatePokeSaveState(text, kind = '') {
  const chip = $('#pokeSaveState'); if (!chip) return;
  chip.textContent = text; chip.className = `route-status ${kind}`.trim();
}

function schedulePokeConfigSave(successText = '已自动保存') {
  clearTimeout(pokeSaveTimer);
  updatePokeSaveState('待保存', 'warn');
  pokeSaveTimer = setTimeout(() => savePokeConfig(null, true, successText), 350);
}

async function savePokeConfig(button, silent = false, successText = '已保存并生效') {
  const serial = ++pokeSaveSerial;
  const faceIds = [...state.pokeFaceIds];
  const textEnabled = $('#pokeTextEnabled').checked;
  let imageEnabled = $('#pokeImageEnabled').checked;
  // Closing text while images are selected means "image-only", not "no
  // content".  Normalize this before saving so the UI never shows a false
  // validation error during rapid toggle/select operations.
  if (!textEnabled && faceIds.length) {
    imageEnabled = true;
    $('#pokeImageEnabled').checked = true;
  }
  const payload = {enabled:$('#pokeEnabled').checked,
    text_enabled:textEnabled,image_enabled:imageEnabled,
    texts:$('#pokeTexts').value.split(/\n/).map(x=>x.trim()).filter(Boolean),face_ids:faceIds,
    cooldown_seconds:Number($('#pokeCooldown').value ?? 8)};
  if (button) setBusy(button, true, '保存中…');
  updatePokeSaveState('保存中…', 'warn');
  try {
    // Serialize saves. Previously an older request could finish last and
    // overwrite a newly selected face, producing the misleading "select an
    // image" error even though the card still looked selected.
    const request = () => api('/api/poke-reply/config', {method:'POST', body:JSON.stringify(payload)});
    pokeSaveQueue = pokeSaveQueue.then(request, request);
    await pokeSaveQueue;
    if (serial === pokeSaveSerial) updatePokeSaveState(successText, 'good');
    if (!silent) toast('拍一拍回复已保存并实时生效');
  } catch(e) {
    if (serial === pokeSaveSerial) updatePokeSaveState('保存失败', 'bad');
    toast(`拍一拍保存失败：${e.message}`,'error');
  } finally { if (button) setBusy(button,false); }
}

async function uploadPokeFaces(files) {
  for (const file of [...files]) {
    if (!/^image\/(png|jpeg|gif|webp)$/.test(file.type) || file.size > 20*1024*1024) { toast(`${file.name} 格式不支持或超过 20MB`,'error'); continue; }
    const dataUrl = await new Promise((resolve,reject)=>{ const reader=new FileReader(); reader.onload=()=>resolve(reader.result); reader.onerror=reject; reader.readAsDataURL(file); });
    try { const result=await api('/api/poke-reply/upload',{method:'POST',body:JSON.stringify({name:file.name,data_url:dataUrl})}); state.faceItems.unshift({...result.item,data_url:result.data_url,tags:[],keywords:[]}); state.pokeFaceIds.add(Number(result.item.id)); $('#pokeImageEnabled').checked = true; toast(`已上传并选中 ${file.name}`); }
    catch(e){ toast(`${file.name} 上传失败：${e.message}`,'error'); }
  }
  renderPokeSelectedFaces(); await loadFaces(null,true); schedulePokeConfigSave('上传图片已保存');
}

async function sendFace(button) {
  setBusy(button, true, '发送中');
  try {
    const r = await api('/api/faces/send', { method: 'POST', body: JSON.stringify({ face_id: button.dataset.faceId, group_id: $('#faceTargetGroup').value }) });
    toast(`已发送表情包：#${r.item?.id || button.dataset.faceId} · ${r.state} · ${r.elapsed_ms || 0}ms`);
  } catch (e) { toast(`表情包发送失败：${e.message}`, 'error', 12000); }
  finally { setBusy(button, false); }
}

async function toggleFace(button) {
  const enabled = button.dataset.enabled !== '1';
  setBusy(button, true, '保存中');
  try {
    await api('/api/faces/update', { method: 'POST', body: JSON.stringify({ face_id: button.dataset.faceId, enabled }) });
    toast(enabled ? '表情已启用' : '表情已屏蔽');
    await loadFaces(null, true);
  } catch (e) { toast(`表情状态保存失败：${e.message}`, 'error'); }
  finally { setBusy(button, false); }
}

async function toggleFaceForGroup(button) {
  const groupId = $('#faceGroup').value;
  if (!groupId) { toast('请先选择需要单独配置的群', 'error'); return; }
  const groupEnabled = button.dataset.enabled !== '1';
  setBusy(button, true, '保存中');
  try {
    await api('/api/faces/update', { method: 'POST', body: JSON.stringify({ face_id: button.dataset.faceId, group_id: groupId, group_enabled: groupEnabled }) });
    toast(groupEnabled ? '该表情已在当前群启用' : '该表情已在当前群禁用');
    await loadFaces(null, true);
  } catch (e) { toast(`群级表情配置失败：${e.message}`, 'error'); }
  finally { setBusy(button, false); }
}

async function editFaceSemantics(button) {
  const aliases = prompt('编辑表情别名，用逗号分隔', button.dataset.aliases || '');
  if (aliases == null) return;
  const emotions = prompt('编辑情绪标签，如：搞笑、无语、生气', button.dataset.emotions || '');
  if (emotions == null) return;
  const intents = prompt('编辑接话意图，如：回怼、拒绝、夸赞', button.dataset.intents || '');
  if (intents == null) return;
  const split = value => value.split(/[,，;；]/).map(x => x.trim()).filter(Boolean);
  try {
    await api('/api/faces/update', { method: 'POST', body: JSON.stringify({
      face_id: button.dataset.faceId, aliases_json: split(aliases), emotions_json: split(emotions), intents_json: split(intents),
    }) });
    toast('表情别名、情绪和意图已更新并进入向量队列');
    await loadFaces(null, true);
  } catch (e) { toast(`表情语义标签保存失败：${e.message}`, 'error'); }
}

async function reindexFaces(button) {
  setBusy(button, true, '重建中…');
  try {
    const r = await api('/api/faces/reindex', { method: 'POST', body: '{}' });
    toast(`表情索引已重建：${r.assets || 0} 个去重素材`);
    await loadFaces(null, true);
  } catch (e) { toast(`表情索引重建失败：${e.message}`, 'error'); }
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

function isHeartbeatLog(item) {
  const text = `${item.event || ''} ${item.message || ''}`;
  return /(?:GET|POST)\s+\/?(?:api\/)?status\b|http_access[\s\S]*\/?(?:api\/)?status\b|message=["']?(?:GET|POST)[\s\S]*\/?(?:api\/)?status\b/i.test(text);
}

function collapseRepeatedLogs(rows) {
  return rows.reduce((result, item) => {
    const key = `${item.source || ''}|${item.level || ''}|${item.event || ''}|${item.message || ''}`;
    const last = result[result.length - 1];
    if (last && last._key === key) {
      last.repeat += 1;
      last.time = item.time;
      return result;
    }
    result.push({ ...item, _key: key, repeat: 1 });
    return result;
  }, []);
}

function dashboardLogSummary(item) {
  const message = String(item.message || '');
  if (/protobuf_msg: cannot extract message data/i.test(message)) return '跳过无法解析的微信系统消息';
  if (/\[JS日志\][\s\S]*捕获到 StartTask/i.test(message)) return '微信 Hook 捕获到新的消息任务';
  if (/发送数据\s+msg=/i.test(message)) return 'OneBot 正在把新消息送入 AI 链路';
  if (/返回内容\s+body=/i.test(message)) return /"status"\s*:\s*"ok"/i.test(message) ? 'AI 链路已接收并处理消息' : 'AI 链路返回处理结果';
  if (/send_group|ONEBOT SEND|send/i.test(`${item.event || ''} ${message}`)) return 'OneBot 执行群消息发送';
  return message.length > 160 ? `${message.slice(0, 157)}…` : message;
}

function renderLogs() {
  const enabled = new Set($$('.level-filters input:checked').map(x => x.value));
  const groupFilter = ($('#logGroupFilter')?.value || '').trim();
  const traceFilter = ($('#logTraceFilter')?.value || '').trim();
  const errorsOnly = $('#logErrorsOnly')?.checked;
  const sendOnly = $('#logSendOnly')?.checked;
  const hideHeartbeat = $('#logHideHeartbeat')?.checked;
  const rows = state.logs.filter(x => {
    if (state.source !== 'all' && x.source !== state.source) return false;
    if (!enabled.has(x.level)) return false;
    if (groupFilter && !String(x.group_id || x.message || '').includes(groupFilter)) return false;
    if (traceFilter && !String(x.trace_id || x.message || '').includes(traceFilter)) return false;
    if (errorsOnly && !['error', 'critical'].includes(x.level)) return false;
    if (sendOnly && !/send_group|ONEBOT SEND|send/i.test(`${x.event || ''} ${x.message || ''}`)) return false;
    if (hideHeartbeat && isHeartbeatLog(x)) return false;
    return true;
  });
  const compactRows = collapseRepeatedLogs(rows);
  const out = $('#logOutput'), nearBottom = out.scrollHeight - out.scrollTop - out.clientHeight < 70;
  out.innerHTML = compactRows.length ? compactRows.slice(-700).map(x => `<div class="log-line ${x.level}"><span class="time">${escapeHtml(formatLogTime(x.time))}</span><span class="source">${x.source === 'ai' ? 'AI' : 'ONEBOT'}</span><span class="level">${x.level.toUpperCase()}</span><span class="message">${escapeHtml(x.message)}${x.repeat > 1 ? `<b class="log-repeat">×${x.repeat}</b>` : ''}</span></div>`).join('') : '<div class="terminal-empty">当前筛选条件下没有日志</div>';
  const merged = rows.length - compactRows.length;
  $('#logCounter').textContent = merged > 0 ? `${compactRows.length} 条 · 合并 ${merged}` : `${compactRows.length} 条`;
  if (nearBottom || compactRows.length < 80) out.scrollTop = out.scrollHeight;
  renderDashboardLogs();
}

function renderDashboardLogs() {
  const rawRows = state.logs.filter(x => (state.miniSource === 'all' || x.source === state.miniSource) && !isHeartbeatLog(x));
  const rows = collapseRepeatedLogs(rawRows).slice(-100);
  const out = $('#dashboardLogOutput'), nearBottom = out.scrollHeight - out.scrollTop - out.clientHeight < 60;
  out.innerHTML = rows.length ? rows.map(x => `<div class="mini-log-line ${x.level}" title="${escapeHtml(x.message)}"><span class="time">${escapeHtml(formatLogTime(x.time))}</span><span class="source">${x.source === 'ai' ? 'AI' : 'ONEBOT'}</span><span class="level">${x.level.toUpperCase()}</span><span class="message">${escapeHtml(dashboardLogSummary(x))}${x.repeat > 1 ? `<b class="log-repeat">×${x.repeat}</b>` : ''}</span></div>`).join('') : '<div class="terminal-empty">当前来源没有有效日志</div>';
  $('#miniLogCount').textContent = `${rows.length} 条`; if (nearBottom || rows.length < 60) out.scrollTop = out.scrollHeight;
}

function connectLogs() {
  state.eventSource?.close(); const es = new EventSource('/api/events/stream'); state.eventSource = es;
  es.onmessage = e => { try { const item = JSON.parse(e.data); pulseActivePageSignal(item.type === 'reply_task' ? 1 : item.type === 'persona_analysis' ? 2 : 0); if (item.type === 'log') addLog(item); else if (item.type === 'reply_task') { refreshReplyTasks(true); const phase = item.state || item.data?.state || ''; const node = phase.includes('rerank') ? 'reranker' : phase.includes('retriev') ? 'embedding' : phase.includes('voice') ? 'voice' : phase.includes('face') ? 'face' : phase.includes('generat') ? 'person' : 'fts'; pulseOrbitNode(node); } else if (item.type === 'memory_backfill') { refreshReplyTasks(true); pulseOrbitNode('embedding'); } else if (item.type === 'persona_analysis') { pulseOrbitNode('person'); clearTimeout(state.persona.refreshTimer); state.persona.refreshTimer = setTimeout(() => { if ($('#page-personas').classList.contains('active')) loadPersonaMembers(true); }, 180); } else if (item.type === 'admin_command' && $('#page-groups').classList.contains('active')) { loadAdminAudit(); } } catch (_) {} };
  es.onerror = () => { $('#logPulse').style.opacity = '.3'; };
}

function bind() {
  $$('.nav-item').forEach(x => x.onclick = () => showPage(x.dataset.page)); $$('[data-jump]').forEach(x => x.onclick = () => showPage(x.dataset.jump));
  $$('.workspace-item').forEach(x => x.onclick = () => applyWorkspace(x.dataset.workspace, true));
  $$('[data-theme-value]').forEach(x => x.onclick = () => applyTheme(x.dataset.themeValue, true));
  $$('[data-brain-view-value]').forEach(x => x.onclick = () => setBrainView(x.dataset.brainViewValue));
  $('#brainMuteOpenBtn').onclick = () => {
    setBrainView('config');
    requestAnimationFrame(() => {
      const field = $('#brainMuteField');
      field?.scrollIntoView({ behavior: 'smooth', block: 'center' });
      field?.classList.remove('is-located');
      void field?.offsetWidth;
      field?.classList.add('is-located');
      setTimeout(() => field?.classList.remove('is-located'), 1800);
      setTimeout(() => $('#brainMuteDuration')?.focus({ preventScroll: true }), 260);
    });
  };
  $('#brainMuteDuration').oninput = e => updateMuteSummary(e.currentTarget.value);
  $$('[data-orbit-node]').forEach(x => x.onclick = () => selectOrbitNode(x.dataset.orbitNode));
  $('#mobileNavBtn').onclick = () => document.body.classList.toggle('nav-open');
  $('#contextCloseBtn').onclick = () => document.body.classList.remove('nav-open');
  $('#globalCommand').onkeydown = e => {
    if (e.key === 'Escape') { e.currentTarget.value = ''; e.currentTarget.blur(); return; }
    if (e.key !== 'Enter') return;
    const query = e.currentTarget.value.trim().toLowerCase();
    if (!query) return;
    const target = Object.entries(pageMeta).find(([key, value]) => `${key} ${value.join(' ')}`.toLowerCase().includes(query));
    if (target) { showPage(target[0]); e.currentTarget.value = ''; }
    else toast(`没有找到“${e.currentTarget.value.trim()}”`, 'error');
  };
  document.addEventListener('keydown', e => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); $('#globalCommand').focus(); }
  });
  window.addEventListener('hashchange', () => {
    const requested = location.hash.replace(/^#/, '');
    if (pageMeta[requested] && !$('#page-' + requested).classList.contains('active')) showPage(requested);
  });
  $('#refreshBtn').onclick = () => { refreshStatus(); refreshTraces(); }; $('#refreshTraceBtn').onclick = () => refreshTraces(); $('#startAllBtn').onclick = e => { if (confirm('确认启动当前微信、OneBot 与 AI 服务？')) action('start_all', e.currentTarget); }; $$('.action-btn').forEach(x => x.onclick = () => action(x.dataset.action, x));
  $('#autoLoginEnabled').onchange = e => saveAutoLogin(e.target.checked);
  $('#autoLoginCheckBtn').onclick = e => checkAutoLogin(e.currentTarget);
  $('#channelSelect').onchange = e => selectChannel(e.target.value); $('#addChannelBtn').onclick = openChannelDialog; $('#deleteChannelBtn').onclick = deleteCurrentChannel;
  $('#testAllChannelsBtn').onclick = e => testAllChannels(e.currentTarget); $('#refreshHealthBtn').onclick = () => refreshChannelHealth();
  $('#channelProvider').onchange = e => applyProviderPreset(e.target.value); $('#newChannelProvider').onchange = e => applyProviderPreset(e.target.value, 'new');
  $('#closeChannelDialog').onclick = closeChannelDialog; $('#cancelChannelDialog').onclick = closeChannelDialog;
  $('#channelDialogForm').onsubmit = e => { e.preventDefault(); if (e.currentTarget.reportValidity()) addChannelFromDialog(); };
  $('#toggleKey').onclick = () => { $('#apiKey').type = $('#apiKey').type === 'password' ? 'text' : 'password'; };
  $('#modelsBtn').onclick = e => getModels(e.currentTarget); $('#quickTestBtn').onclick = e => quickTest(e.currentTarget);
  $('#ocrTestBtn').onclick = e => ocrTest(e.currentTarget); $('#toggleOcrKey').onclick = () => { $('#ocrApiKey').type = $('#ocrApiKey').type === 'password' ? 'text' : 'password'; };
  $('#imageGenTestBtn').onclick = e => imageGenerationTest(e.currentTarget); $('#toggleImageGenKey').onclick = () => { $('#imageGenApiKey').type = $('#imageGenApiKey').type === 'password' ? 'text' : 'password'; };
  $('#asrTestBtn').onclick = e => asrTest(e.currentTarget); $('#toggleAsrKey').onclick = () => { $('#asrApiKey').type = $('#asrApiKey').type === 'password' ? 'text' : 'password'; };
  $('#temperature').oninput = e => { $('#temperatureValue').value = e.target.value; markDirty(); }; $('#systemPrompt').oninput = e => { $('#promptCount').textContent = e.target.value.length; markDirty(); }; $('#personality').oninput = e => { $('#personalityCount').textContent = e.target.value.length; markDirty(); };
  $('#aiForm').onsubmit = e => { e.preventDefault(); saveConfig(e.submitter); }; $('#groupForm').onsubmit = e => { e.preventDefault(); saveConfig(e.submitter); };
  $('#brainForm').onsubmit = e => { e.preventDefault(); saveBrainConfig(e.submitter); };
  $('#brainThreshold').oninput = e => { $('#brainThresholdValue').value = e.target.value; $('#brainMode').value = 'custom'; if ($('#orbitThreshold')) $('#orbitThreshold').textContent = e.target.value; };
  $('#brainMode').onchange = e => { const values = { reserved: 78, natural: 65, veteran: 52 }; if (values[e.target.value] != null) { $('#brainThreshold').value = values[e.target.value]; $('#brainThresholdValue').value = values[e.target.value]; if ($('#orbitThreshold')) $('#orbitThreshold').textContent = values[e.target.value]; } };
  $('#brainGlobalWorkers').oninput = e => { if ($('#orbitWorkers')) $('#orbitWorkers').textContent = e.target.value; };
  $('#refreshReplyTasksBtn').onclick = () => refreshReplyTasks(); $('#taskStateFilter').onchange = renderReplyTasks; $('#taskQuery').oninput = renderReplyTasks;
  $('#personaGroup').onchange = () => { state.persona.selectedUserId = ''; $('#page-personas').dataset.mobilePane = 'directory'; loadPersonaMembers(); };
  $('#personaStatus').onchange = () => loadPersonaMembers(); $('#personaSearchBtn').onclick = () => loadPersonaMembers();
  $('#personaSearch').oninput = () => { clearTimeout(state.persona.searchTimer); state.persona.searchTimer = setTimeout(() => loadPersonaMembers(true), 220); };
  $('#personaSearch').onkeydown = e => { if (e.key === 'Enter') loadPersonaMembers(); };
  $('#personaAnalyzeSelected').onclick = e => analyzePersona('member', e.currentTarget); $('#personaAnalyzeGroup').onclick = e => analyzePersona('group', e.currentTarget);
  $('#personaAnalyzeInline').onclick = e => analyzePersona('member', e.currentTarget);
  $('#personaFiltersToggle').onclick = e => {
    const open = $('#page-personas').classList.toggle('persona-filters-open');
    e.currentTarget.setAttribute('aria-expanded', String(open));
    e.currentTarget.innerHTML = `<i class="ph ph-funnel"></i>${open ? '收起筛选' : '筛选成员'}`;
  };
  $('#personaClaimFilter').onchange = () => renderPersonaEvidence(state.persona.detail?.claims || []); $('#personaEditBtn').onclick = openPersonaEditor;
  $('#personaProgressAction').onclick = e => controlPersonaJob(e.currentTarget);
  $$('[data-persona-tab]').forEach(x => x.onclick = () => selectPersonaTab(x.dataset.personaTab));
  $('#personaEditorForm').onsubmit = e => { e.preventDefault(); savePersonaEditor().catch(error => toast(`画像保存失败：${error.message}`, 'error')); };
  $('#personaEditorClose').onclick = $('#personaEditorCancel').onclick = () => $('#personaEditor').close();
  $('#personaBackBtn').onclick = showPersonaDirectory;
  $('#brainPreviewBtn').onclick = e => previewBrain(e.currentTarget); $('#closeBrainPreview').onclick = () => $('#brainPreviewDialog').close();
  $$('[data-brain-preview-clone]').forEach(x => x.onclick = e => previewBrain(e.currentTarget));
  $('#backfillStartBtn').onclick = e => controlBackfill('start', e.currentTarget); $('#backfillPauseBtn').onclick = e => { const resume = $('#embeddingState').textContent === '已暂停'; controlBackfill(resume ? 'resume' : 'pause', e.currentTarget); };
  $('#saveEmbeddingBtn').onclick = e => saveBrainConfig(e.currentTarget);
  $('#embeddingTestBtn').onclick = e => testEmbedding(e.currentTarget);
  $('#refreshGroupsBtn').onclick = () => loadDiscoveredGroups(); $('#saveAliasesBtn').onclick = e => saveAliases(e.currentTarget); $('#syncUiGroupsBtn').onclick = e => syncUiGroups(e.currentTarget); $('#enableSelectedGroupBtn').onclick = enableQuickSelectedGroup; $$('#aiForm input, #aiForm textarea, #aiForm select, #groupForm input:not(#memberBlacklistQuery)').forEach(x => x.addEventListener('change', () => { updateRouteSummary(); markDirty(); }));
  $('#refreshGroupMembersBtn').onclick = e => loadGroupMembers(e.currentTarget);
  $('#saveGroupBlacklistBtn').onclick = e => saveGroupBlacklist(e.currentTarget);
  $('#memberBlacklistGroup').onchange = () => loadGroupMembers();
  $('#memberBlacklistQuery').oninput = renderGroupMembers;
  $('#replyMentionGroup').onchange = renderReplyMentionSetting;
  $('#replyMentionEnabled').onchange = () => {
    $('#replyMentionState').textContent = `待保存：${$('#replyMentionEnabled').checked ? '回复时艾特' : '不艾特'}`;
  };
  $('#saveReplyMentionBtn').onclick = e => saveReplyMentionSetting(e.currentTarget);
  $('#groupPersonalityGroup').onchange = renderGroupPersonalitySetting;
  $('#groupPersonalityEnabled').onchange = markGroupPersonalityPending;
  $('#groupPersonalityName').oninput = markGroupPersonalityPending;
  $('#groupPersonalityPrompt').oninput = markGroupPersonalityPending;
  $('#saveGroupPersonalityBtn').onclick = e => saveGroupPersonalitySetting(e.currentTarget);
  $('#groupAdminGroup').onchange = () => loadGroupAdmins();
  $('#groupAdminSearch').oninput = renderGroupAdminPanel;
  $('#saveGroupAdminsBtn').onclick = e => saveGroupAdmins(e.currentTarget);
  $('#previewAdminMenuBtn').onclick = () => loadAdminMenuPreview();
  $('#refreshAdminAuditBtn').onclick = () => loadAdminAudit();
  $('#addManualAdminBtn').onclick = () => {
    const input = $('#manualAdminId'), value = input.value.trim();
    if (!value || value.endsWith('@chatroom')) { toast('请输入精确的成员 wxid / 微信内部 ID', 'error'); return; }
    addGroupAdmin(value, '', 'manual'); input.value = '';
  };
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
  $('#voiceImportFiles').onchange = e => chooseVoiceImportFiles(e.target.files);
  $('#voiceImportClearBtn').onclick = clearVoiceImportFiles;
  $('#voiceDeletePackBtn').onclick = e => deleteCurrentVoicePack(e.currentTarget);
  $('#voiceImportTargetPack').onchange = syncVoiceImportTarget;
  $('#voiceReplyProbability').oninput = e => { $('#voiceReplyProbabilityValue').value = `${e.target.value}%`; };
  $('#voiceMediaSaveBtn').onclick = e => saveMediaReplyConfig(e.currentTarget, 'voice');
  $('#voiceContextTestBtn').onclick = e => testMediaContext(e.currentTarget, 'voice');
  $$('#voiceCategory,#voicePackFilter').forEach(x => x.addEventListener('change', () => {
    if (x.id === 'voiceCategory') $('#voicePackFilter').value = '';
    loadVoicepacks(null, true);
  }));
  $('#voiceQuery').addEventListener('keydown', e => { if (e.key === 'Enter') loadVoicepacks($('#voiceSearchBtn')); });
  $('#faceRefreshBtn').onclick = e => loadFaces(e.currentTarget); $('#faceSearchBtn').onclick = e => loadFaces(e.currentTarget);
  $('#faceReplyProbability').oninput = e => { $('#faceReplyProbabilityValue').value = `${e.target.value}%`; };
  $('#faceMediaSaveBtn').onclick = e => saveMediaReplyConfig(e.currentTarget, 'face');
  $('#faceContextTestBtn').onclick = e => testMediaContext(e.currentTarget, 'face');
  $('#faceReindexBtn').onclick = e => reindexFaces(e.currentTarget);
  $('#pokeSaveBtn').onclick = e => savePokeConfig(e.currentTarget);
  $$('#pokeEnabled,#pokeTextEnabled,#pokeImageEnabled').forEach(input => input.addEventListener('change', () => schedulePokeConfigSave()));
  $('#pokeTexts').addEventListener('input', () => { updatePokeSaveState('正在编辑', 'warn'); });
  $('#pokeTexts').addEventListener('blur', () => schedulePokeConfigSave('文字已自动保存'));
  $('#pokeCooldown').addEventListener('change', () => schedulePokeConfigSave());
  $('#pokeFaceUpload').onchange = e => { uploadPokeFaces(e.target.files).finally(() => { e.target.value = ''; }); };
  $('#faceDetailClose').onclick = () => $('#faceDetailDialog').close();
  $$('#faceGroup').forEach(x => x.addEventListener('change', () => loadFaces(null, true)));
  $('#faceQuery').addEventListener('keydown', e => { if (e.key === 'Enter') loadFaces($('#faceSearchBtn')); });
  $('#loadGroupMemoryBtn').onclick = e => loadGroupMemory(e.currentTarget); $('#saveGroupMemoryBtn').onclick = e => saveGroupMemory(e.currentTarget);
  $('#rebuildMemoryBtn').onclick = e => rebuildMemory(e.currentTarget); $('#importMemoryBtn').onclick = e => importMemory(e.currentTarget); $('#exportMemoryBtn').onclick = e => exportMemory(e.currentTarget);
  $$('[data-mini-source]').forEach(x => x.onclick = () => { $$('[data-mini-source]').forEach(y => y.classList.toggle('active', y === x)); state.miniSource = x.dataset.miniSource; renderDashboardLogs(); });
  $$('.source-tabs button').forEach(x => x.onclick = () => { $$('.source-tabs button').forEach(y => y.classList.toggle('active', y === x)); state.source = x.dataset.source; renderLogs(); });
  $$('.level-filters input').forEach(x => x.onchange = renderLogs); $$('#logGroupFilter,#logTraceFilter,#logErrorsOnly,#logSendOnly,#logHideHeartbeat').forEach(x => x.addEventListener('input', renderLogs)); $$('#logErrorsOnly,#logSendOnly,#logHideHeartbeat').forEach(x => x.addEventListener('change', renderLogs)); $('#clearLogs').onclick = () => { state.logs = []; renderLogs(); };
  $('#logFiltersToggle').onclick = e => {
    const open = $('#page-logs').classList.toggle('filters-open');
    e.currentTarget.setAttribute('aria-expanded', String(open));
    e.currentTarget.innerHTML = `<i class="ph ph-funnel"></i>${open ? '收起过滤' : '过滤条件'}`;
  };
  $('#pauseLogs').onclick = e => { state.paused = !state.paused; e.currentTarget.textContent = state.paused ? '继续' : '暂停'; if (!state.paused) renderLogs(); };
}

async function init() {
  applyTheme(localStorage.getItem('wxconsole-theme') || document.documentElement.dataset.theme || 'dark');
  decorateOrbitalPages(); decorateOrbitalWorkbenches(); setupResponsiveControlPanels(); setupUiTooltips(); setupModelSectionNav(); setBrainView(localStorage.getItem('wxconsole-brain-view') || 'orbit', false); selectOrbitNode('person', false);
  $('#page-personas').dataset.mobilePane = 'directory';
  const initialPage = location.hash.slice(1);
  showPage(pageMeta[initialPage] ? initialPage : 'overview');
  bind(); connectLogs();
  try { fillConfig(await api('/api/config')); } catch (e) { toast(`配置加载失败：${e.message}`, 'error'); }
  await loadBrainConfig(true); await loadGroupAdmins(true); await refreshReplyTasks(true);
  try { await loadPokeConfig(); } catch (e) { toast(`拍一拍配置加载失败：${e.message}`, 'error'); }
  await refreshStatus(); await refreshTraces(true); setInterval(() => refreshStatus(true), 5000); setInterval(() => refreshTraces(true), 5000); setInterval(() => refreshChannelHealth(true), 15000);
  refreshMemoryStats(true);
  setInterval(() => { if ($('#page-groups').classList.contains('active') && !state.dirty) loadDiscoveredGroups(true); }, 30000);
  setInterval(() => { if ($('#page-media').classList.contains('active')) loadMediaCenter(null, true); }, 5000);
  setInterval(() => { if ($('#page-voice-records')?.classList.contains('active')) loadVoiceRecords(null, true); }, 5000);
  setInterval(() => refreshReplyTasks(true), 3000);
}
init();
