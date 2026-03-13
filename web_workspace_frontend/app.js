const DEFAULT_API_BASE = localStorage.getItem('astrabot:workspace:apiBaseUrl') || 'https://astrabot-tchj.onrender.com';
const DEFAULT_BOT_URL = localStorage.getItem('astrabot:workspace:botUrl') || '';

const STUDIO_CONFIG = {
  chat: {
    title: 'ChatGPT Studio',
    eyebrow: 'Workspace Assistant',
    subtitle: 'Диалоговая студия для идей, упаковки, сценариев и prompt engineering.',
    provider: 'OpenAI',
    defaultModel: 'gpt-4o-mini',
    modeLabel: () => state.chat.mode === 'prompt_builder' ? 'Prompt Builder' : 'Chat',
  },
  video: {
    title: 'Kling 3 Video',
    eyebrow: 'Video Generation',
    subtitle: 'Текст → видео через защищённый workspace API с токеновым списанием на backend.',
    provider: 'PiAPI / Kling 3',
    defaultModel: 'kling-3',
    modeLabel: () => 'Text → Video',
  },
  voice: {
    title: 'Voice Studio',
    eyebrow: 'ElevenLabs TTS',
    subtitle: 'Озвучка текста голосами из твоего каталога ElevenLabs.',
    provider: 'ElevenLabs',
    defaultModel: 'eleven_multilingual_v2',
    modeLabel: () => 'Text → Speech',
  },
  songwriter: {
    title: 'Songwriter Studio',
    eyebrow: 'Lyrics Helper',
    subtitle: 'Рабочая зона для текста песен, концептов, куплетов, припевов и вайба.',
    provider: 'OpenAI',
    defaultModel: 'songwriter',
    modeLabel: () => 'Lyrics / Concepts',
  },
  prompts: {
    title: 'Prompt Library',
    eyebrow: 'Supabase Library',
    subtitle: 'Категории, группы и готовые prompts из текущей prompt library.',
    provider: 'Supabase',
    defaultModel: 'prompt-library',
    modeLabel: () => 'Library',
  },
  account: {
    title: 'Account & Balance',
    eyebrow: 'Profile / Tokens',
    subtitle: 'Сессия, Telegram-профиль, баланс и live integrations.',
    provider: 'Workspace',
    defaultModel: 'workspace',
    modeLabel: () => 'Profile',
  },
};

const state = {
  apiBaseUrl: DEFAULT_API_BASE,
  botUrl: DEFAULT_BOT_URL,
  apiOnline: false,
  studio: localStorage.getItem('astrabot:workspace:studio') || 'chat',
  recentRuns: JSON.parse(localStorage.getItem('astrabot:workspace:recentRuns') || '[]'),
  bootstrap: {
    chat_models: ['gpt-4o-mini'],
    live_integrations: ['workspace_chat', 'balance', 'kling3', 'tts', 'songwriter', 'prompts'],
    bot_url: DEFAULT_BOT_URL,
  },
  auth: {
    insideTelegram: false,
    initDataAvailable: false,
    initData: '',
    accessToken: sessionStorage.getItem('astrabot:workspace:token') || '',
    user: JSON.parse(sessionStorage.getItem('astrabot:workspace:user') || 'null'),
    expiresAt: Number(sessionStorage.getItem('astrabot:workspace:expiresAt') || 0),
  },
  balance: null,
  chat: {
    model: localStorage.getItem('astrabot:workspace:chat:model') || 'gpt-4o-mini',
    mode: localStorage.getItem('astrabot:workspace:chat:mode') || 'chat',
    temperature: Number(localStorage.getItem('astrabot:workspace:chat:temperature') || 0.6),
    maxTokens: Number(localStorage.getItem('astrabot:workspace:chat:maxTokens') || 900),
    composer: '',
    messages: JSON.parse(localStorage.getItem('astrabot:workspace:chat:messages') || '[]'),
    busy: false,
  },
  video: {
    prompt: '',
    duration: 5,
    resolution: '720',
    aspectRatio: '16:9',
    enableAudio: false,
    providerTaskId: '',
    statusPayload: null,
    polling: false,
  },
  voice: {
    text: '',
    voiceId: '',
    voices: [],
    audioUrl: '',
    audioFilename: 'astrabot-tts.mp3',
    busy: false,
  },
  songwriter: {
    text: '',
    language: 'ru',
    genre: '',
    mood: '',
    references: '',
    history: [],
    busy: false,
  },
  prompts: {
    categories: [],
    selectedCategory: '',
    groups: [],
    selectedGroupId: '',
    items: [],
    loading: false,
  },
};

const el = {};

function saveState() {
  localStorage.setItem('astrabot:workspace:apiBaseUrl', state.apiBaseUrl);
  localStorage.setItem('astrabot:workspace:botUrl', state.botUrl || '');
  localStorage.setItem('astrabot:workspace:studio', state.studio);
  localStorage.setItem('astrabot:workspace:recentRuns', JSON.stringify(state.recentRuns.slice(0, 50)));
  localStorage.setItem('astrabot:workspace:chat:model', state.chat.model || '');
  localStorage.setItem('astrabot:workspace:chat:mode', state.chat.mode || 'chat');
  localStorage.setItem('astrabot:workspace:chat:temperature', String(state.chat.temperature));
  localStorage.setItem('astrabot:workspace:chat:maxTokens', String(state.chat.maxTokens));
  localStorage.setItem('astrabot:workspace:chat:messages', JSON.stringify(state.chat.messages.slice(-40)));
}

function saveSession() {
  sessionStorage.setItem('astrabot:workspace:token', state.auth.accessToken || '');
  sessionStorage.setItem('astrabot:workspace:user', JSON.stringify(state.auth.user || null));
  sessionStorage.setItem('astrabot:workspace:expiresAt', String(state.auth.expiresAt || 0));
}

function clearSession() {
  state.auth.accessToken = '';
  state.auth.user = null;
  state.auth.expiresAt = 0;
  state.balance = null;
  saveSession();
}

function escapeHtml(value = '') {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function formatDate(ts) {
  if (!ts) return '—';
  try {
    return new Date(ts).toLocaleString('ru-RU');
  } catch {
    return String(ts);
  }
}

function toast(type, title, text) {
  const stack = document.getElementById('toastStack');
  const node = document.createElement('div');
  node.className = `toast ${type}`;
  node.innerHTML = `<strong>${escapeHtml(title)}</strong><div>${escapeHtml(text)}</div>`;
  stack.appendChild(node);
  setTimeout(() => { node.style.opacity = '0'; node.style.transform = 'translateY(6px)'; }, 3400);
  setTimeout(() => node.remove(), 3900);
}

function pushRun(entry) {
  state.recentRuns.unshift({ id: crypto.randomUUID(), ts: new Date().toISOString(), ...entry });
  state.recentRuns = state.recentRuns.slice(0, 40);
  saveState();
  renderRecentRuns();
}

function isAuthenticated() {
  return Boolean(state.auth.accessToken && state.auth.user && state.auth.expiresAt > Date.now());
}

function requireAuth() {
  if (isAuthenticated()) return true;
  if (!state.auth.insideTelegram) {
    toast('error', 'Нужен Telegram', 'Этот сайт должен быть открыт внутри Telegram Mini App, иначе безопасная авторизация недоступна.');
  } else {
    toast('error', 'Нет сессии', 'Нажми «Подключить» и заново пройди авторизацию через Telegram Mini App.');
  }
  return false;
}

function findMediaUrl(data) {
  if (!data) return '';
  if (typeof data === 'string' && /^https?:\/\//i.test(data)) return data;
  if (Array.isArray(data)) {
    for (const item of data) {
      const hit = findMediaUrl(item);
      if (hit) return hit;
    }
    return '';
  }
  if (typeof data === 'object') {
    const priorityKeys = ['video_url', 'videoUrl', 'output_url', 'url', 'resource_url'];
    for (const key of priorityKeys) {
      if (typeof data[key] === 'string' && /^https?:\/\//i.test(data[key])) return data[key];
    }
    for (const value of Object.values(data)) {
      const hit = findMediaUrl(value);
      if (hit) return hit;
    }
  }
  return '';
}

function telegramWebApp() {
  return window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
}

async function apiFetch(path, options = {}, retryAuth = true) {
  const base = String(state.apiBaseUrl || '').replace(/\/$/, '');
  if (!base) throw new Error('API Base URL is empty');

  const headers = new Headers(options.headers || {});
  if (isAuthenticated()) headers.set('Authorization', `Bearer ${state.auth.accessToken}`);
  if (!headers.has('Content-Type') && options.body && !(options.body instanceof FormData) && !(options.body instanceof Blob)) {
    headers.set('Content-Type', 'application/json');
  }

  const response = await fetch(`${base}${path}`, { ...options, headers });
  if (response.status === 401 && retryAuth && state.auth.insideTelegram && state.auth.initDataAvailable) {
    const ok = await authenticateWithTelegram(true);
    if (ok) return apiFetch(path, options, false);
  }

  if (!response.ok) {
    let detail = response.statusText || `HTTP ${response.status}`;
    try {
      const data = await response.clone().json();
      detail = data.detail || data.error || JSON.stringify(data);
    } catch {
      try {
        detail = await response.text();
      } catch {}
    }
    throw new Error(detail || `HTTP ${response.status}`);
  }
  return response;
}

async function jsonFetch(path, options = {}, retryAuth = true) {
  const res = await apiFetch(path, options, retryAuth);
  return res.json();
}

async function checkApiHealth(showToast = true) {
  try {
    const data = await jsonFetch('/api/workspace/health', {}, false);
    state.apiOnline = !!data.ok;
    if (showToast) toast('success', 'API online', 'Backend отвечает и workspace router подключён.');
  } catch (error) {
    state.apiOnline = false;
    if (showToast) toast('error', 'API недоступен', error.message);
  }
  renderSessionCard();
}

async function loadBootstrap() {
  try {
    const data = await jsonFetch('/api/workspace/bootstrap', {}, false);
    state.bootstrap = {
      ...state.bootstrap,
      ...data,
    };
    state.botUrl = data.bot_url || state.botUrl || '';
    if (!state.chat.model || !state.bootstrap.chat_models.includes(state.chat.model)) {
      state.chat.model = state.bootstrap.chat_models[0] || 'gpt-4o-mini';
    }
    saveState();
  } catch (error) {
    console.warn('bootstrap failed', error);
  }
}

async function authenticateWithTelegram(silent = false) {
  const tg = telegramWebApp();
  if (!tg || !tg.initData) {
    if (!silent) toast('error', 'Telegram не найден', 'Открой этот сайт из Telegram Mini App, чтобы пройти защищённую авторизацию.');
    return false;
  }

  state.auth.insideTelegram = true;
  state.auth.initDataAvailable = Boolean(tg.initData);
  state.auth.initData = tg.initData;

  try {
    const data = await jsonFetch('/api/workspace/auth/telegram', {
      method: 'POST',
      body: JSON.stringify({ init_data: tg.initData }),
    }, false);
    state.auth.accessToken = data.access_token;
    state.auth.user = data.user || null;
    state.auth.expiresAt = Date.now() + (Number(data.expires_in || 0) * 1000);
    state.balance = Number(data.balance_tokens ?? state.balance ?? 0);
    saveSession();
    renderSessionCard();
    renderInspector();
    renderWorkspace();
    if (!silent) toast('success', 'Сессия активна', 'Telegram авторизация успешно подтверждена на backend.');
    return true;
  } catch (error) {
    clearSession();
    renderSessionCard();
    if (!silent) toast('error', 'Авторизация не прошла', error.message);
    return false;
  }
}

async function refreshProfile() {
  if (!requireAuth()) return;
  try {
    const data = await jsonFetch('/api/workspace/me');
    state.auth.user = data.user || state.auth.user;
    state.balance = Number(data.balance_tokens ?? state.balance ?? 0);
    saveSession();
    renderSessionCard();
    renderInspector();
  } catch (error) {
    toast('error', 'Не удалось обновить профиль', error.message);
  }
}

async function refreshBalance(showToast = false) {
  if (!requireAuth()) return;
  try {
    const data = await jsonFetch('/api/workspace/balance');
    state.balance = Number(data.balance_tokens ?? 0);
    renderSessionCard();
    renderInspector();
    if (showToast) toast('success', 'Баланс обновлён', `Доступно ${state.balance} токенов.`);
  } catch (error) {
    toast('error', 'Ошибка баланса', error.message);
  }
}

async function loadVoices() {
  if (state.voice.voices.length) return;
  try {
    const data = await jsonFetch('/api/workspace/tts/voices', {}, false);
    state.voice.voices = Array.isArray(data.items) ? data.items : [];
    if (!state.voice.voiceId && state.voice.voices[0]) state.voice.voiceId = state.voice.voices[0].voice_id;
  } catch (error) {
    toast('error', 'Не удалось загрузить голоса', error.message);
  }
}

async function loadPromptCategories() {
  if (state.prompts.categories.length) return;
  try {
    state.prompts.loading = true;
    const data = await jsonFetch('/api/workspace/prompts/categories', {}, false);
    state.prompts.categories = Array.isArray(data.items) ? data.items : [];
    if (!state.prompts.selectedCategory && state.prompts.categories[0]) {
      state.prompts.selectedCategory = state.prompts.categories[0].slug;
      await loadPromptGroups();
    }
  } catch (error) {
    toast('error', 'Не удалось загрузить категории', error.message);
  } finally {
    state.prompts.loading = false;
  }
}

async function loadPromptGroups() {
  if (!state.prompts.selectedCategory) return;
  try {
    state.prompts.loading = true;
    const data = await jsonFetch(`/api/workspace/prompts/groups?category=${encodeURIComponent(state.prompts.selectedCategory)}`, {}, false);
    state.prompts.groups = Array.isArray(data.items) ? data.items : [];
    state.prompts.selectedGroupId = state.prompts.groups[0]?.id || '';
    await loadPromptItems();
  } catch (error) {
    state.prompts.groups = [];
    state.prompts.items = [];
    toast('error', 'Не удалось загрузить группы', error.message);
  } finally {
    state.prompts.loading = false;
  }
}

async function loadPromptItems() {
  if (!state.prompts.selectedGroupId) {
    state.prompts.items = [];
    return;
  }
  try {
    const data = await jsonFetch(`/api/workspace/prompts/items?group_id=${encodeURIComponent(state.prompts.selectedGroupId)}`, {}, false);
    state.prompts.items = Array.isArray(data.items) ? data.items : [];
  } catch (error) {
    state.prompts.items = [];
    toast('error', 'Не удалось загрузить prompts', error.message);
  }
}

function setStudio(studio) {
  if (!STUDIO_CONFIG[studio]) return;
  state.studio = studio;
  saveState();
  renderChrome();
  renderWorkspace();
  renderInspector();
  if (studio === 'voice') loadVoices().then(() => { renderWorkspace(); renderInspector(); });
  if (studio === 'prompts') loadPromptCategories().then(() => { renderWorkspace(); renderInspector(); });
}

function renderChrome() {
  const cfg = STUDIO_CONFIG[state.studio];
  el.headerEyebrow.textContent = cfg.eyebrow;
  el.headerTitle.textContent = cfg.title;
  el.headerSubtitle.textContent = cfg.subtitle;
  el.metaStudio.textContent = cfg.title;
  el.metaProvider.textContent = cfg.provider;
  el.metaModel.textContent = getCurrentModelLabel();
  el.metaMode.textContent = cfg.modeLabel();

  el.studioNav.innerHTML = Object.entries(STUDIO_CONFIG).map(([key, conf]) => `
    <button class="nav-item ${key === state.studio ? 'active' : ''}" data-studio="${key}">
      <span class="nav-emoji">${studioEmoji(key)}</span>
      <span>
        <span class="nav-title">${escapeHtml(conf.title)}</span>
        <span class="nav-subtitle">${escapeHtml(conf.subtitle)}</span>
      </span>
    </button>
  `).join('');
}

function studioEmoji(studio) {
  return ({ chat: '💬', video: '🎬', voice: '🎙️', songwriter: '🎵', prompts: '🧠', account: '👤' })[studio] || '✨';
}

function getCurrentModelLabel() {
  switch (state.studio) {
    case 'chat': return state.chat.model || STUDIO_CONFIG.chat.defaultModel;
    case 'video': return 'Kling 3';
    case 'voice': return state.voice.voiceId ? (state.voice.voices.find(v => v.voice_id === state.voice.voiceId)?.name || STUDIO_CONFIG.voice.defaultModel) : STUDIO_CONFIG.voice.defaultModel;
    case 'songwriter': return 'Songwriter';
    case 'prompts': return 'Supabase library';
    case 'account': return 'Workspace session';
    default: return '—';
  }
}

function renderRecentRuns() {
  if (!state.recentRuns.length) {
    el.recentRuns.innerHTML = 'Пока пусто';
    el.recentRuns.className = 'recent-list empty-state';
    return;
  }
  el.recentRuns.className = 'recent-list';
  el.recentRuns.innerHTML = state.recentRuns.map(item => `
    <div class="run-item">
      <strong>${escapeHtml(item.title || 'Действие')}</strong>
      <small>${escapeHtml(item.detail || '')}</small>
      <small>${formatDate(item.ts)}</small>
    </div>
  `).join('');
}

function renderSessionCard() {
  el.apiBaseUrl.value = state.apiBaseUrl;
  el.apiStatus.textContent = state.apiOnline ? 'online' : 'offline';
  el.apiStatus.className = state.apiOnline ? 'badge ok' : 'badge muted';

  const sessionSummary = document.getElementById('sessionSummary');
  const tgBadge = `<span class="badge ${state.auth.insideTelegram ? 'ok' : 'muted'}">${state.auth.insideTelegram ? 'внутри Telegram' : 'браузер'}</span>`;
  const authBadge = `<span class="badge ${isAuthenticated() ? 'ok' : 'muted'}">${isAuthenticated() ? 'активна' : 'нет'}</span>`;
  sessionSummary.innerHTML = `
    <div class="row-between"><span class="muted">Telegram</span>${tgBadge}</div>
    <div class="row-between"><span class="muted">Авторизация</span>${authBadge}</div>
    <div class="row-between"><span class="muted">User</span><span>${escapeHtml(state.auth.user?.first_name || state.auth.user?.username || '—')}</span></div>
  `;

  el.balanceValue.textContent = state.balance == null ? '—' : String(state.balance);
  if (isAuthenticated()) {
    el.balanceHint.textContent = `user #${state.auth.user?.telegram_user_id || '—'} • сессия до ${formatDate(state.auth.expiresAt)}`;
    el.connectTelegramBtn.textContent = 'Обновить сессию';
  } else {
    el.balanceHint.textContent = state.auth.insideTelegram ? 'нажми «Подключить», чтобы подтвердить Telegram-пользователя' : 'открой сайт внутри Telegram Mini App';
    el.connectTelegramBtn.textContent = state.auth.insideTelegram ? 'Подключить' : 'Открыть в Telegram';
  }
}

function renderWorkspace() {
  renderChrome();
  switch (state.studio) {
    case 'chat':
      renderChatStudio();
      break;
    case 'video':
      renderVideoStudio();
      break;
    case 'voice':
      renderVoiceStudio();
      break;
    case 'songwriter':
      renderSongwriterStudio();
      break;
    case 'prompts':
      renderPromptStudio();
      break;
    case 'account':
      renderAccountStudio();
      break;
    default:
      el.workspaceBody.innerHTML = '<div class="empty-copy">Unknown studio</div>';
  }
}

function renderInspector() {
  switch (state.studio) {
    case 'chat':
      el.inspectorBody.innerHTML = `
        <div class="inspector-card">
          <h4>Режим чата</h4>
          <div class="input-group">
            <label class="label">Mode</label>
            <select id="chatModeSelect">
              <option value="chat" ${state.chat.mode === 'chat' ? 'selected' : ''}>Chat</option>
              <option value="prompt_builder" ${state.chat.mode === 'prompt_builder' ? 'selected' : ''}>Prompt Builder</option>
            </select>
          </div>
          <div class="input-group">
            <label class="label">Model</label>
            <select id="chatModelSelect">${(state.bootstrap.chat_models || ['gpt-4o-mini']).map(model => `<option value="${escapeHtml(model)}" ${state.chat.model === model ? 'selected' : ''}>${escapeHtml(model)}</option>`).join('')}</select>
          </div>
          <div class="field-grid two">
            <div class="input-group">
              <label class="label">Temperature</label>
              <input id="chatTemperatureInput" type="number" min="0" max="1.5" step="0.1" value="${escapeHtml(String(state.chat.temperature))}">
            </div>
            <div class="input-group">
              <label class="label">Max tokens</label>
              <input id="chatMaxTokensInput" type="number" min="150" max="4000" step="50" value="${escapeHtml(String(state.chat.maxTokens))}">
            </div>
          </div>
          <div class="actions two-up compact-gap">
            <button id="saveChatSettingsBtn" class="btn primary">Сохранить</button>
            <button id="clearChatBtn" class="btn ghost">Очистить</button>
          </div>
        </div>
        <div class="inspector-card">
          <h4>Подсказка</h4>
          <div class="help-text">Prompt Builder имеет смысл держать на отдельной модели через env <code>PROMPT_BUILDER_MODEL</code>. Если на backend она не задана, студия использует обычную chat-модель.</div>
        </div>
      `;
      bindInspectorChat();
      break;
    case 'video':
      el.inspectorBody.innerHTML = `
        <div class="inspector-card">
          <h4>Kling 3 settings</h4>
          <div class="tableish">
            <div class="table-row"><span class="muted">Duration</span><span>${state.video.duration} sec</span><span class="badge muted">server-billed</span></div>
            <div class="table-row"><span class="muted">Resolution</span><span>${escapeHtml(state.video.resolution)}p</span><span class="badge muted">PiAPI</span></div>
            <div class="table-row"><span class="muted">Aspect</span><span>${escapeHtml(state.video.aspectRatio)}</span><span class="badge muted">omni</span></div>
          </div>
        </div>
        <div class="inspector-card">
          <h4>Статус</h4>
          <div class="help-text">После старта backend списывает токены, создаёт provider task и при ошибке пытается вернуть токены через refund ledger.</div>
        </div>
      `;
      break;
    case 'voice':
      el.inspectorBody.innerHTML = `
        <div class="inspector-card">
          <h4>Voice catalog</h4>
          <div class="help-text">Список голосов идёт из существующего curated-каталога ElevenLabs на backend. Генерация доступна только после Telegram auth.</div>
        </div>
      `;
      break;
    case 'songwriter':
      el.inspectorBody.innerHTML = `
        <div class="inspector-card">
          <h4>Songwriter context</h4>
          <div class="help-text">Запрос уходит в существующий songwriter helper. Контекст языка, жанра, настроения и референсов добавляется на backend в system prompt.</div>
        </div>
      `;
      break;
    case 'prompts':
      el.inspectorBody.innerHTML = `
        <div class="inspector-card">
          <h4>Prompt Library</h4>
          <div class="help-text">Эта секция читает твои Supabase-таблицы prompt_categories, prompt_groups и prompt_items через отдельные workspace wrappers.</div>
        </div>
      `;
      break;
    case 'account':
      el.inspectorBody.innerHTML = `
        <div class="inspector-card">
          <h4>Deploy hints</h4>
          <div class="help-text">
            <div>1. Статика живёт в отдельном Render Static Site.</div>
            <div>2. Backend отвечает из AstraBot Web Service.</div>
            <div>3. CORS нужно ограничить доменом фронта через <code>WORKSPACE_ALLOWED_ORIGINS</code>.</div>
          </div>
        </div>
      `;
      break;
  }
}

function bindInspectorChat() {
  document.getElementById('saveChatSettingsBtn')?.addEventListener('click', () => {
    state.chat.mode = document.getElementById('chatModeSelect').value;
    state.chat.model = document.getElementById('chatModelSelect').value;
    state.chat.temperature = Number(document.getElementById('chatTemperatureInput').value || 0.6);
    state.chat.maxTokens = Number(document.getElementById('chatMaxTokensInput').value || 900);
    saveState();
    renderChrome();
    renderWorkspace();
    toast('success', 'Настройки сохранены', 'Параметры чата обновлены.');
  });
  document.getElementById('clearChatBtn')?.addEventListener('click', () => {
    state.chat.messages = [];
    saveState();
    renderWorkspace();
  });
}

function renderChatStudio() {
  const feed = state.chat.messages.length ? state.chat.messages.map(msg => `
    <div class="chat-bubble ${msg.role === 'user' ? 'user' : msg.role === 'assistant' ? 'assistant' : 'system'}">${escapeHtml(msg.content)}</div>
  `).join('') : `<div class="empty-copy"><strong>Чат готов</strong>Открой сайт внутри Telegram, подключи сессию и используй Studio как prompt helper или рабочий copilot.</div>`;

  el.workspaceBody.innerHTML = `
    <div class="workspace-grid">
      <div class="workspace-main placeholder-stage chat">
        <div class="chat-shell">
          <div class="chat-feed">${feed}</div>
          <div class="chat-composer">
            <div class="quick-chips">
              <button class="chip" data-chip="Собери сильный prompt для Veo 3.1 под рекламный вертикальный ролик 9:16.">Veo prompt</button>
              <button class="chip" data-chip="Упакуй идею Telegram AI-сервиса в лендинг, тарифы и оффер.">Лендинг и оффер</button>
              <button class="chip" data-chip="Сделай 5 вариантов hooks для Reels про prompt engineering.">Hooks для Reels</button>
            </div>
            <div class="composer-row">
              <textarea id="chatComposer" placeholder="Напиши запрос...">${escapeHtml(state.chat.composer || '')}</textarea>
              <button id="chatSendBtn" class="btn primary">${state.chat.busy ? 'Отправка…' : 'Отправить'}</button>
            </div>
          </div>
        </div>
      </div>
      <div class="workspace-side scroll">
        <div class="planner-card">
          <h4>Текущий режим</h4>
          <small>${escapeHtml(state.chat.mode === 'prompt_builder' ? 'Prompt Builder' : 'Chat')} • ${escapeHtml(state.chat.model)}</small>
        </div>
        <div class="history-card">
          <h4>Session guard</h4>
          <small>${isAuthenticated() ? `Авторизован как #${state.auth.user?.telegram_user_id}` : 'Для боевой работы нужен Telegram auth.'}</small>
        </div>
      </div>
    </div>
  `;

  document.getElementById('chatComposer').addEventListener('input', (event) => {
    state.chat.composer = event.target.value;
  });
  document.getElementById('chatSendBtn').addEventListener('click', sendChatMessage);
  document.querySelectorAll('[data-chip]').forEach(btn => btn.addEventListener('click', () => {
    state.chat.composer = btn.dataset.chip || '';
    renderWorkspace();
  }));
}

function renderVideoStudio() {
  const previewUrl = findMediaUrl(state.video.statusPayload);
  const prettyJson = state.video.statusPayload ? escapeHtml(JSON.stringify(state.video.statusPayload, null, 2)) : 'Пока нет provider payload';

  el.workspaceBody.innerHTML = `
    <div class="workspace-grid">
      <div class="workspace-main scroll">
        <div class="field-grid two">
          <div class="input-group">
            <label class="label">Prompt</label>
            <textarea id="videoPromptInput" placeholder="Опиши сцену и движение камеры">${escapeHtml(state.video.prompt)}</textarea>
          </div>
          <div class="upload-grid">
            <div class="field-grid two">
              <div class="input-group">
                <label class="label">Duration</label>
                <select id="videoDurationSelect">
                  ${[3,5,10,15].map(v => `<option value="${v}" ${state.video.duration === v ? 'selected' : ''}>${v} sec</option>`).join('')}
                </select>
              </div>
              <div class="input-group">
                <label class="label">Resolution</label>
                <select id="videoResolutionSelect">
                  ${['720','1080'].map(v => `<option value="${v}" ${state.video.resolution === v ? 'selected' : ''}>${v}p</option>`).join('')}
                </select>
              </div>
            </div>
            <div class="field-grid two">
              <div class="input-group">
                <label class="label">Aspect ratio</label>
                <select id="videoAspectSelect">
                  ${['16:9','9:16','1:1'].map(v => `<option value="${v}" ${state.video.aspectRatio === v ? 'selected' : ''}>${v}</option>`).join('')}
                </select>
              </div>
              <div class="toggle-row">
                <div>
                  <strong>Audio</strong>
                  <div class="help-text">Включить звук, если поддерживается провайдером.</div>
                </div>
                <label class="switch">
                  <input id="videoAudioToggle" type="checkbox" ${state.video.enableAudio ? 'checked' : ''}>
                  <span></span>
                </label>
              </div>
            </div>
            <div class="actions two-up compact-gap">
              <button id="videoCreateBtn" class="btn primary">Создать видео</button>
              <button id="videoPollBtn" class="btn ghost">Проверить статус</button>
            </div>
          </div>
        </div>
      </div>
      <div class="workspace-side scroll">
        <div class="result-card">
          <h4>Provider task</h4>
          <small>${escapeHtml(state.video.providerTaskId || 'ещё не создан')}</small>
        </div>
        <div class="result-card">
          <h4>Preview</h4>
          ${previewUrl ? `<video class="preview-media" controls src="${escapeHtml(previewUrl)}"></video>` : '<div class="asset-empty">Видео появится после ready status</div>'}
        </div>
        <div class="history-card">
          <h4>Payload</h4>
          <pre class="json-box">${prettyJson}</pre>
        </div>
      </div>
    </div>
  `;

  document.getElementById('videoPromptInput').addEventListener('input', (event) => { state.video.prompt = event.target.value; });
  document.getElementById('videoDurationSelect').addEventListener('change', (event) => { state.video.duration = Number(event.target.value); renderInspector(); });
  document.getElementById('videoResolutionSelect').addEventListener('change', (event) => { state.video.resolution = event.target.value; renderInspector(); });
  document.getElementById('videoAspectSelect').addEventListener('change', (event) => { state.video.aspectRatio = event.target.value; renderInspector(); });
  document.getElementById('videoAudioToggle').addEventListener('change', (event) => { state.video.enableAudio = !!event.target.checked; renderInspector(); });
  document.getElementById('videoCreateBtn').addEventListener('click', createVideoTask);
  document.getElementById('videoPollBtn').addEventListener('click', pollVideoTask);
}

function renderVoiceStudio() {
  const voices = state.voice.voices;
  const audioBlock = state.voice.audioUrl
    ? `<audio controls src="${escapeHtml(state.voice.audioUrl)}"></audio><div class="actions compact-gap"><a class="btn ghost" href="${escapeHtml(state.voice.audioUrl)}" download="${escapeHtml(state.voice.audioFilename)}">Скачать MP3</a></div>`
    : '<div class="asset-empty">Сначала сгенерируй озвучку</div>';

  el.workspaceBody.innerHTML = `
    <div class="workspace-grid">
      <div class="workspace-main scroll">
        <div class="field-grid two">
          <div class="input-group">
            <label class="label">Текст</label>
            <textarea id="voiceTextInput" placeholder="Вставь текст для озвучки">${escapeHtml(state.voice.text)}</textarea>
          </div>
          <div class="input-group">
            <label class="label">Голос</label>
            <select id="voiceSelect">
              ${(voices || []).map(v => `<option value="${escapeHtml(v.voice_id)}" ${state.voice.voiceId === v.voice_id ? 'selected' : ''}>${escapeHtml(v.name)}</option>`).join('') || '<option value="">Нет голосов</option>'}
            </select>
            <div class="actions compact-gap">
              <button id="voiceGenerateBtn" class="btn primary">${state.voice.busy ? 'Генерация…' : 'Сгенерировать'}</button>
              <button id="voiceRefreshBtn" class="btn ghost">Обновить список</button>
            </div>
          </div>
        </div>
      </div>
      <div class="workspace-side scroll">
        <div class="result-card">
          <h4>Результат</h4>
          ${audioBlock}
        </div>
      </div>
    </div>
  `;

  document.getElementById('voiceTextInput').addEventListener('input', (event) => { state.voice.text = event.target.value; });
  document.getElementById('voiceSelect').addEventListener('change', (event) => { state.voice.voiceId = event.target.value; renderChrome(); });
  document.getElementById('voiceGenerateBtn').addEventListener('click', generateVoice);
  document.getElementById('voiceRefreshBtn').addEventListener('click', async () => {
    state.voice.voices = [];
    await loadVoices();
    renderWorkspace();
    renderInspector();
  });
}

function renderSongwriterStudio() {
  const history = state.songwriter.history.length ? state.songwriter.history.map(item => `
    <div class="chat-bubble ${item.role === 'user' ? 'user' : 'assistant'}">${escapeHtml(item.content)}</div>
  `).join('') : '<div class="empty-copy"><strong>Songwriter ready</strong>Опиши идею песни, жанр и эмоцию. История будет храниться только в браузере.</div>';

  el.workspaceBody.innerHTML = `
    <div class="workspace-grid">
      <div class="workspace-main placeholder-stage chat">
        <div class="chat-shell">
          <div class="chat-feed">${history}</div>
          <div class="chat-composer">
            <div class="field-grid two">
              <div class="input-group"><label class="label">Язык</label><input id="songLanguageInput" value="${escapeHtml(state.songwriter.language)}"></div>
              <div class="input-group"><label class="label">Жанр</label><input id="songGenreInput" value="${escapeHtml(state.songwriter.genre)}"></div>
            </div>
            <div class="field-grid two">
              <div class="input-group"><label class="label">Настроение</label><input id="songMoodInput" value="${escapeHtml(state.songwriter.mood)}"></div>
              <div class="input-group"><label class="label">Референсы</label><input id="songRefsInput" value="${escapeHtml(state.songwriter.references)}"></div>
            </div>
            <div class="composer-row">
              <textarea id="songTextInput" placeholder="Напиши задачу для songwriter...">${escapeHtml(state.songwriter.text)}</textarea>
              <button id="songSendBtn" class="btn primary">${state.songwriter.busy ? 'Отправка…' : 'Получить текст'}</button>
            </div>
          </div>
        </div>
      </div>
      <div class="workspace-side scroll">
        <div class="history-card">
          <h4>Подсказка</h4>
          <small>Например: «Сделай припев про AI-студию в Telegram, бодрый pop/edm, русский язык, цепкий hook».</small>
        </div>
      </div>
    </div>
  `;

  document.getElementById('songLanguageInput').addEventListener('input', (e) => { state.songwriter.language = e.target.value; });
  document.getElementById('songGenreInput').addEventListener('input', (e) => { state.songwriter.genre = e.target.value; });
  document.getElementById('songMoodInput').addEventListener('input', (e) => { state.songwriter.mood = e.target.value; });
  document.getElementById('songRefsInput').addEventListener('input', (e) => { state.songwriter.references = e.target.value; });
  document.getElementById('songTextInput').addEventListener('input', (e) => { state.songwriter.text = e.target.value; });
  document.getElementById('songSendBtn').addEventListener('click', requestSongwriter);
}

function renderPromptStudio() {
  el.workspaceBody.innerHTML = `
    <div class="workspace-grid">
      <div class="workspace-main scroll">
        <div class="field-grid two">
          <div class="input-group">
            <label class="label">Категория</label>
            <select id="promptCategorySelect">
              ${(state.prompts.categories || []).map(cat => `<option value="${escapeHtml(cat.slug)}" ${state.prompts.selectedCategory === cat.slug ? 'selected' : ''}>${escapeHtml(cat.title || cat.slug)}</option>`).join('') || '<option value="">Нет категорий</option>'}
            </select>
          </div>
          <div class="input-group">
            <label class="label">Группа</label>
            <select id="promptGroupSelect">
              ${(state.prompts.groups || []).map(group => `<option value="${escapeHtml(group.id)}" ${state.prompts.selectedGroupId === group.id ? 'selected' : ''}>${escapeHtml(group.title)}</option>`).join('') || '<option value="">Нет групп</option>'}
            </select>
          </div>
        </div>
        <div class="mini-list">
          ${(state.prompts.items || []).map(item => `
            <div class="prompt-item">
              <strong>${escapeHtml(item.title || 'Prompt')}</strong>
              <small>${escapeHtml(item.model_hint || '')}</small>
              <div class="actions compact-gap" style="margin-top:10px;">
                <button class="btn ghost small" data-copy-prompt="${escapeHtml(item.prompt_text || '')}">Копировать</button>
                <button class="btn outline small" data-send-prompt="${escapeHtml(item.prompt_text || '')}">В Chat Studio</button>
              </div>
            </div>
          `).join('') || '<div class="empty-copy"><strong>Нет элементов</strong>Выбери категорию и группу.</div>'}
        </div>
      </div>
      <div class="workspace-side scroll">
        <div class="library-card">
          <h4>Статистика</h4>
          <small>Категорий: ${state.prompts.categories.length}</small>
          <small>Групп: ${state.prompts.groups.length}</small>
          <small>Items: ${state.prompts.items.length}</small>
        </div>
      </div>
    </div>
  `;

  document.getElementById('promptCategorySelect')?.addEventListener('change', async (event) => {
    state.prompts.selectedCategory = event.target.value;
    await loadPromptGroups();
    renderWorkspace();
    renderInspector();
  });
  document.getElementById('promptGroupSelect')?.addEventListener('change', async (event) => {
    state.prompts.selectedGroupId = event.target.value;
    await loadPromptItems();
    renderWorkspace();
    renderInspector();
  });
  document.querySelectorAll('[data-copy-prompt]').forEach(btn => btn.addEventListener('click', async () => {
    const text = btn.dataset.copyPrompt || '';
    await navigator.clipboard.writeText(text);
    toast('success', 'Скопировано', 'Prompt отправлен в буфер обмена.');
  }));
  document.querySelectorAll('[data-send-prompt]').forEach(btn => btn.addEventListener('click', () => {
    state.chat.composer = btn.dataset.sendPrompt || '';
    setStudio('chat');
  }));
}

function renderAccountStudio() {
  const user = state.auth.user;
  const integrations = (state.bootstrap.live_integrations || []).map(item => `<span class="chip">${escapeHtml(item)}</span>`).join('');
  el.workspaceBody.innerHTML = `
    <div class="workspace-grid">
      <div class="workspace-main scroll">
        <div class="metrics">
          <div class="metric"><strong>${state.balance == null ? '—' : escapeHtml(String(state.balance))}</strong><span>Токены</span></div>
          <div class="metric"><strong>${isAuthenticated() ? 'ON' : 'OFF'}</strong><span>Workspace session</span></div>
          <div class="metric"><strong>${escapeHtml(String((state.bootstrap.live_integrations || []).length))}</strong><span>Live integrations</span></div>
        </div>
        <div class="field-grid two" style="margin-top:16px;">
          <div class="profile-card">
            <h4>Telegram user</h4>
            <small>ID: ${escapeHtml(String(user?.telegram_user_id || '—'))}</small>
            <small>Имя: ${escapeHtml(user?.first_name || '—')}</small>
            <small>Username: ${escapeHtml(user?.username || '—')}</small>
            <small>Premium: ${user?.is_premium ? 'yes' : 'no'}</small>
          </div>
          <div class="billing-card">
            <h4>Live integrations</h4>
            <div class="quick-chips">${integrations || '<span class="empty-state">нет данных</span>'}</div>
          </div>
        </div>
        <div class="actions compact-gap" style="margin-top:16px;">
          <button id="refreshProfileBtn" class="btn primary">Обновить профиль</button>
          <button id="refreshBalanceBtn" class="btn ghost">Обновить баланс</button>
          <button id="logoutBtn" class="btn outline">Сбросить сессию</button>
        </div>
      </div>
      <div class="workspace-side scroll">
        <div class="history-card">
          <h4>Deploy route</h4>
          <small>Static Site → Telegram auth → /api/workspace/* → AstraBot backend.</small>
        </div>
        <div class="history-card">
          <h4>Bot URL</h4>
          <small>${escapeHtml(state.botUrl || 'не задан. Укажи WORKSPACE_BOT_URL на backend или сохрани его в localStorage.')}</small>
        </div>
      </div>
    </div>
  `;

  document.getElementById('refreshProfileBtn')?.addEventListener('click', refreshProfile);
  document.getElementById('refreshBalanceBtn')?.addEventListener('click', () => refreshBalance(true));
  document.getElementById('logoutBtn')?.addEventListener('click', () => {
    clearSession();
    renderSessionCard();
    renderWorkspace();
    renderInspector();
    toast('info', 'Сессия очищена', 'Bearer token удалён из sessionStorage.');
  });
}

async function sendChatMessage() {
  if (state.chat.busy || !requireAuth()) return;
  const text = (state.chat.composer || '').trim();
  if (!text) {
    toast('error', 'Пустой запрос', 'Напиши сообщение для Chat Studio.');
    return;
  }

  state.chat.busy = true;
  state.chat.messages.push({ role: 'user', content: text });
  state.chat.composer = '';
  saveState();
  renderWorkspace();

  try {
    const data = await jsonFetch('/api/workspace/chat', {
      method: 'POST',
      body: JSON.stringify({
        text,
        history: state.chat.messages.slice(-12).map(item => ({ role: item.role, content: item.content })),
        model: state.chat.model,
        mode: state.chat.mode,
        temperature: state.chat.temperature,
        max_tokens: state.chat.maxTokens,
      }),
    });
    state.chat.messages.push({ role: 'assistant', content: data.answer || 'Пустой ответ' });
    pushRun({ title: 'Chat reply', detail: `${state.chat.mode} • ${state.chat.model}` });
  } catch (error) {
    state.chat.messages.push({ role: 'system', content: `Ошибка: ${error.message}` });
    toast('error', 'Chat error', error.message);
  } finally {
    state.chat.busy = false;
    saveState();
    renderWorkspace();
  }
}

async function createVideoTask() {
  if (!requireAuth()) return;
  const prompt = (state.video.prompt || '').trim();
  if (!prompt) {
    toast('error', 'Нужен prompt', 'Опиши видео перед запуском Kling 3.');
    return;
  }

  try {
    const data = await jsonFetch('/api/workspace/kling3/create', {
      method: 'POST',
      body: JSON.stringify({
        prompt,
        duration: state.video.duration,
        resolution: state.video.resolution,
        enable_audio: state.video.enableAudio,
        aspect_ratio: state.video.aspectRatio,
      }),
    });
    state.video.providerTaskId = data.provider_task_id || '';
    state.video.statusPayload = data.task || data;
    if (typeof data.balance_tokens === 'number') state.balance = data.balance_tokens;
    renderSessionCard();
    renderWorkspace();
    renderInspector();
    pushRun({ title: 'Kling 3 create', detail: `task ${state.video.providerTaskId || 'created'} • -${data.tokens_required || '?'} tokens` });
    toast('success', 'Видео запущено', state.video.providerTaskId ? `Task ID: ${state.video.providerTaskId}` : 'Провайдер принял задачу.');
  } catch (error) {
    toast('error', 'Kling error', error.message);
  }
}

async function pollVideoTask() {
  if (!requireAuth()) return;
  if (!state.video.providerTaskId) {
    toast('error', 'Нет task id', 'Сначала запусти Kling 3 task.');
    return;
  }
  try {
    const data = await jsonFetch(`/api/workspace/kling3/task/${encodeURIComponent(state.video.providerTaskId)}`);
    state.video.statusPayload = data.task || data;
    renderWorkspace();
    const media = findMediaUrl(state.video.statusPayload);
    if (media) pushRun({ title: 'Kling 3 ready', detail: state.video.providerTaskId });
  } catch (error) {
    toast('error', 'Status error', error.message);
  }
}

async function generateVoice() {
  if (state.voice.busy || !requireAuth()) return;
  const text = (state.voice.text || '').trim();
  if (!text) {
    toast('error', 'Нужен текст', 'Добавь текст для озвучки.');
    return;
  }
  if (!state.voice.voiceId) {
    toast('error', 'Нужен голос', 'Выбери голос из списка.');
    return;
  }
  state.voice.busy = true;
  renderWorkspace();
  try {
    const response = await apiFetch('/api/workspace/tts/generate', {
      method: 'POST',
      body: JSON.stringify({
        text,
        voice_id: state.voice.voiceId,
        model_id: 'eleven_multilingual_v2',
        output_format: 'mp3_44100_128',
      }),
    });
    const blob = await response.blob();
    if (state.voice.audioUrl) URL.revokeObjectURL(state.voice.audioUrl);
    state.voice.audioUrl = URL.createObjectURL(blob);
    const voiceName = state.voice.voices.find(v => v.voice_id === state.voice.voiceId)?.name || 'voice';
    state.voice.audioFilename = `${voiceName}.mp3`;
    renderWorkspace();
    pushRun({ title: 'TTS generated', detail: voiceName });
    toast('success', 'Озвучка готова', 'MP3 возвращён из backend.');
  } catch (error) {
    toast('error', 'TTS error', error.message);
  } finally {
    state.voice.busy = false;
    renderWorkspace();
  }
}

async function requestSongwriter() {
  if (state.songwriter.busy || !requireAuth()) return;
  const text = (state.songwriter.text || '').trim();
  if (!text) {
    toast('error', 'Нужен запрос', 'Опиши, что именно должен сделать songwriter.');
    return;
  }
  state.songwriter.busy = true;
  renderWorkspace();
  try {
    const data = await jsonFetch('/api/workspace/songwriter', {
      method: 'POST',
      body: JSON.stringify({
        text,
        history: state.songwriter.history.slice(-10).map(item => ({ role: item.role, content: item.content })),
        language: state.songwriter.language,
        genre: state.songwriter.genre,
        mood: state.songwriter.mood,
        references: state.songwriter.references,
      }),
    });
    state.songwriter.history.push({ role: 'user', content: text });
    state.songwriter.history.push({ role: 'assistant', content: data.answer || 'Пустой ответ' });
    state.songwriter.text = '';
    renderWorkspace();
    pushRun({ title: 'Songwriter', detail: `${state.songwriter.genre || 'lyrics'} • ${state.songwriter.language || 'ru'}` });
  } catch (error) {
    toast('error', 'Songwriter error', error.message);
  } finally {
    state.songwriter.busy = false;
    renderWorkspace();
  }
}

function seedDemo() {
  switch (state.studio) {
    case 'chat':
      state.chat.composer = 'Собери мне оффер и Telegram onboarding для AI-сервиса с генерацией фото, видео и музыки.';
      break;
    case 'video':
      state.video.prompt = 'Cinematic vertical ad shot of a glowing AI studio dashboard, camera pushes in, dramatic neon light, polished product trailer feel.';
      break;
    case 'voice':
      state.voice.text = 'Привет! Это тестовая озвучка из AstraBot Workspace. Мы проверяем отдельный фронт и защищённый API.';
      break;
    case 'songwriter':
      state.songwriter.text = 'Сделай припев для песни про Telegram AI-студию, яркий hook, современный поп-электроник vibe.';
      state.songwriter.genre = 'pop / electronic';
      state.songwriter.mood = 'bright / catchy';
      break;
    case 'prompts':
      toast('info', 'Demo', 'Для prompt library demo не нужен — просто загрузи категории и группы.');
      break;
    case 'account':
      toast('info', 'Demo', 'Здесь лучше нажать refresh profile / balance.');
      break;
  }
  renderWorkspace();
}

function resetCurrentStudio() {
  switch (state.studio) {
    case 'chat':
      state.chat.composer = '';
      state.chat.messages = [];
      break;
    case 'video':
      state.video = { prompt: '', duration: 5, resolution: '720', aspectRatio: '16:9', enableAudio: false, providerTaskId: '', statusPayload: null, polling: false };
      break;
    case 'voice':
      if (state.voice.audioUrl) URL.revokeObjectURL(state.voice.audioUrl);
      state.voice.text = '';
      state.voice.audioUrl = '';
      break;
    case 'songwriter':
      state.songwriter.text = '';
      state.songwriter.genre = '';
      state.songwriter.mood = '';
      state.songwriter.references = '';
      state.songwriter.history = [];
      break;
    case 'prompts':
      state.prompts.selectedGroupId = '';
      state.prompts.items = [];
      break;
    case 'account':
      break;
  }
  saveState();
  renderWorkspace();
  renderInspector();
}

function bindGlobalUi() {
  el.studioNav.addEventListener('click', (event) => {
    const button = event.target.closest('[data-studio]');
    if (!button) return;
    setStudio(button.dataset.studio);
  });
  el.clearRunsBtn.addEventListener('click', () => {
    state.recentRuns = [];
    saveState();
    renderRecentRuns();
  });
  el.saveSettingsBtn.addEventListener('click', () => {
    state.apiBaseUrl = el.apiBaseUrl.value.trim();
    saveState();
    toast('success', 'Сохранено', 'API Base URL обновлён.');
  });
  el.checkApiBtn.addEventListener('click', () => checkApiHealth(true));
  el.connectTelegramBtn.addEventListener('click', async () => {
    if (state.auth.insideTelegram) {
      await authenticateWithTelegram(false);
      return;
    }
    if (state.botUrl) {
      window.open(state.botUrl, '_blank');
    } else {
      toast('info', 'Открыть в Telegram', 'Задай WORKSPACE_BOT_URL на backend или сохрани ссылку на бота, чтобы открыть Mini App из Telegram.');
    }
  });
  el.seedDemoBtn.addEventListener('click', seedDemo);
  el.globalRunBtn.addEventListener('click', () => {
    if (state.studio === 'chat') return sendChatMessage();
    if (state.studio === 'video') return createVideoTask();
    if (state.studio === 'voice') return generateVoice();
    if (state.studio === 'songwriter') return requestSongwriter();
    if (state.studio === 'prompts') return loadPromptCategories().then(() => renderWorkspace());
    if (state.studio === 'account') return refreshProfile();
  });
  el.resetStudioBtn.addEventListener('click', resetCurrentStudio);
}

async function bootstrapTelegram() {
  const tg = telegramWebApp();
  if (!tg) {
    state.auth.insideTelegram = false;
    state.auth.initDataAvailable = false;
    renderSessionCard();
    return;
  }
  try {
    tg.ready();
    tg.expand();
  } catch {}
  state.auth.insideTelegram = true;
  state.auth.initDataAvailable = Boolean(tg.initData);
  state.auth.initData = tg.initData || '';
  renderSessionCard();
  if (!isAuthenticated() && state.auth.initDataAvailable) {
    await authenticateWithTelegram(true);
  }
}

async function init() {
  el.studioNav = document.getElementById('studioNav');
  el.recentRuns = document.getElementById('recentRuns');
  el.apiBaseUrl = document.getElementById('apiBaseUrl');
  el.apiStatus = document.getElementById('apiStatus');
  el.balanceValue = document.getElementById('balanceValue');
  el.balanceHint = document.getElementById('balanceHint');
  el.connectTelegramBtn = document.getElementById('connectTelegramBtn');
  el.saveSettingsBtn = document.getElementById('saveSettingsBtn');
  el.checkApiBtn = document.getElementById('checkApiBtn');
  el.clearRunsBtn = document.getElementById('clearRunsBtn');
  el.headerEyebrow = document.getElementById('headerEyebrow');
  el.headerTitle = document.getElementById('headerTitle');
  el.headerSubtitle = document.getElementById('headerSubtitle');
  el.metaStudio = document.getElementById('metaStudio');
  el.metaProvider = document.getElementById('metaProvider');
  el.metaModel = document.getElementById('metaModel');
  el.metaMode = document.getElementById('metaMode');
  el.workspaceBody = document.getElementById('workspaceBody');
  el.inspectorBody = document.getElementById('inspectorBody');
  el.seedDemoBtn = document.getElementById('seedDemoBtn');
  el.globalRunBtn = document.getElementById('globalRunBtn');
  el.resetStudioBtn = document.getElementById('resetStudioBtn');

  bindGlobalUi();
  renderRecentRuns();
  renderChrome();
  renderSessionCard();
  renderWorkspace();
  renderInspector();

  await checkApiHealth(false);
  await loadBootstrap();
  renderChrome();
  renderInspector();
  renderWorkspace();
  await bootstrapTelegram();

  if (isAuthenticated()) {
    await refreshProfile();
  }
  if (state.studio === 'voice') await loadVoices();
  if (state.studio === 'prompts') await loadPromptCategories();

  renderChrome();
  renderSessionCard();
  renderWorkspace();
  renderInspector();
}

document.addEventListener('DOMContentLoaded', init);
