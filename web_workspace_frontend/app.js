
const DEFAULT_API_BASE = localStorage.getItem('astrabot:apiBaseUrl') || 'https://astrabot-tchj.onrender.com';
const DEFAULT_AUTH_TOKEN = localStorage.getItem('astrabot:authToken') || '';
const DEFAULT_ME = JSON.parse(localStorage.getItem('astrabot:me') || 'null');
const DEFAULT_VIDEO_STATE = JSON.parse(localStorage.getItem('astrabot:videoState') || '{}');

const runtime = {
  files: {},
  lastChatBootstrapLoaded: false,
  videoPollTimer: null,
};

const state = {
  apiBaseUrl: DEFAULT_API_BASE,
  authToken: DEFAULT_AUTH_TOKEN,
  me: DEFAULT_ME,
  balance: null,
  apiOnline: false,
  studio: localStorage.getItem('astrabot:studio') || 'chat',
  recentRuns: JSON.parse(localStorage.getItem('astrabot:recentRuns') || '[]'),
  workspaceNotes: localStorage.getItem('astrabot:workspaceNotes') || '',
  bootstrap: {
    chatModels: ['gpt-4o-mini', 'gpt-5.4'],
    liveIntegrations: ['workspace_chat', 'balance', 'kling3', 'tts', 'songwriter', 'prompts'],
  },
  chat: {
    model: localStorage.getItem('astrabot:chatModel') || 'gpt-4o-mini',
    mode: localStorage.getItem('astrabot:chatMode') || 'chat',
    temperature: Number(localStorage.getItem('astrabot:chatTemperature') || '0.6'),
    maxTokens: Number(localStorage.getItem('astrabot:chatMaxTokens') || '900'),
    input: '',
    messages: JSON.parse(localStorage.getItem('astrabot:chatMessages') || JSON.stringify([
      { role: 'system', content: 'Добро пожаловать в AstraBot Workspace. Здесь чат, генерации и проекты живут в одной рабочей зоне.' }
    ])),
  },

video: {
  provider: DEFAULT_VIDEO_STATE.provider || 'kling',
  model: DEFAULT_VIDEO_STATE.model || 'kling-3.0',
  mode: DEFAULT_VIDEO_STATE.mode || 'text_to_video',
  prompt: DEFAULT_VIDEO_STATE.prompt || '',
  duration: DEFAULT_VIDEO_STATE.duration || '5',
  resolution: DEFAULT_VIDEO_STATE.resolution || '720',
  aspectRatio: DEFAULT_VIDEO_STATE.aspectRatio || '16:9',
  enableAudio: !!DEFAULT_VIDEO_STATE.enableAudio,
  quality: DEFAULT_VIDEO_STATE.quality || 'pro',
  outputUrl: '',
  downloadUrl: '',
  coverUrl: '',
  percent: null,
  generationId: '',
  providerTaskId: '',
  statusText: 'Выбери модель, настрой параметры и нажми запуск.',
  errorText: '',
  lastStatus: 'idle',
  panel: 'params',
  motionDurationSec: Number.isFinite(Number(DEFAULT_VIDEO_STATE.motionDurationSec)) ? Number(DEFAULT_VIDEO_STATE.motionDurationSec) : null,
  isGenerating: false,
},

  image: {
    provider: 'nano_banana_pro',
    model: 'nano-banana-pro',
    mode: 'image_to_image',
    prompt: '',
    aspectRatio: 'match_input_image',
    resolution: '2K',
    safetyLevel: 'high',
    stylePreset: 'cinematic',
    outputUrl: '',
    statusText: 'Image Studio архитектурно готова. Подключим backend-эндпоинты по мере вынесения в web API.',
  },
  voice: {
    voiceId: '',
    modelId: 'eleven_multilingual_v2',
    outputFormat: 'mp3_44100_128',
    text: '',
    audioUrl: '',
    voices: [],
  },
  music: {
    provider: 'suno',
    model: 'sunoapi',
    mode: 'idea',
    title: '',
    tags: '',
    language: 'ru',
    mood: '',
    references: '',
    text: '',
    songwriterAnswer: '',
  },
  prompts: {
    categories: [],
    selectedCategory: '',
    groups: [],
    selectedGroupId: '',
    items: [],
    loading: false,
  },
  history: {
    items: [],
    loading: false,
    loaded: false,
    selectedId: '',
    selectedItem: null,
    lastError: '',
    limit: 24,
    offset: 0,
  },
};

function scrollChatToBottom() {
  requestAnimationFrame(() => {
    const feed = document.getElementById('chatFeed');
    if (feed) feed.scrollTop = feed.scrollHeight;
  });
}

const STUDIO_META = {
  chat: { emoji: '💬', title: 'ChatGPT Studio', subtitle: 'Центральный чат-диалог для идей, сценариев, промптов и быстрых переходов в другие студии.' },
  video: { emoji: '🎬', title: 'Video Studio', subtitle: 'Kling / Veo / Seedance / Sora в одной рабочей зоне с динамическими полями справа.' },
  image: { emoji: '🖼️', title: 'Image Studio', subtitle: 'Nano Banana, афиши, фотосессии и image-to-image сценарии на общей архитектуре.' },
  voice: { emoji: '🎙️', title: 'Voice Studio', subtitle: 'TTS, выбор голоса и быстрый экспорт результата в проект.' },
  music: { emoji: '🎼', title: 'Music Studio', subtitle: 'Songwriter, Suno, Udio и работа с идеей трека в одном пространстве.' },
  library: { emoji: '📚', title: 'Prompt Library', subtitle: 'Категории, группы, карточки промптов и быстрый перенос в студии.' },
  workspace: { emoji: '🧠', title: 'Workspace', subtitle: 'Планы, референсы, заметки и проектная логика поверх генераций.' },
  history: { emoji: '🕘', title: 'History', subtitle: 'Локальная и будущая серверная история запусков, статусов и результатов.' },
  billing: { emoji: '💳', title: 'Billing', subtitle: 'Баланс, пакеты токенов, экономика генераций и будущая касса.' },
  profile: { emoji: '👤', title: 'Profile', subtitle: 'Связка сайта и Telegram-аккаунта, базовые настройки и состояние системы.' },
};


const VIDEO_REGISTRY = {
  kling: {
    name: 'Kling',
    models: {
      'motion-control': {
        name: 'Motion Control',
        backend: 'live',
        modes: {
          motion_control: { name: 'Motion Control', fields: ['avatarImage', 'motionVideo', 'prompt', 'quality'] },
        },
      },
      'kling-1.6': {
        name: 'Kling 1.6',
        backend: 'live',
        modes: {
          image_to_video: { name: 'Image → Video', fields: ['startFrame', 'prompt', 'durationLegacy', 'quality'] },
        },
      },
      'kling-2.5': {
        name: 'Kling 2.5',
        backend: 'live',
        modes: {
          text_to_video: { name: 'Text → Video', fields: ['prompt', 'duration', 'aspectRatio'] },
          image_to_video: { name: 'Image → Video', fields: ['startFrame', 'endFrame', 'prompt', 'duration', 'aspectRatio'] },
        },
      },
      'kling-3.0': {
        name: 'Kling 3.0',
        backend: 'live',
        modes: {
          text_to_video: { name: 'Text → Video', fields: ['prompt', 'duration', 'resolution', 'aspectRatio', 'enableAudio'] },
          image_to_video: { name: 'Image → Video', fields: ['startFrame', 'endFrame', 'prompt', 'duration', 'resolution', 'aspectRatio', 'enableAudio'] },
          multi_shot: { name: 'Multi-shot', fields: ['prompt', 'startFrame', 'endFrame', 'duration', 'resolution', 'aspectRatio', 'enableAudio'] },
        },
      },
    },
  },
  veo: {
    name: 'Veo',
    models: {
      'veo-fast': {
        name: 'Veo Fast',
        backend: 'live',
        modes: {
          text_to_video: { name: 'Text → Video', fields: ['prompt', 'durationVeo', 'aspectRatioVeo', 'generateAudio'] },
          image_to_video: { name: 'Image → Video', fields: ['startFrame', 'prompt', 'durationVeo', 'aspectRatioVeo', 'generateAudio'] },
        },
      },
      'veo-3.1-pro': {
        name: 'Veo 3.1',
        backend: 'live',
        modes: {
          text_to_video: { name: 'Text → Video', fields: ['prompt', 'durationVeo', 'aspectRatioVeo', 'generateAudio'] },
          image_to_video: { name: 'Image → Video', fields: ['startFrame', 'lastFrame', 'referenceImages', 'prompt', 'durationVeo', 'aspectRatioVeo', 'generateAudio'] },
        },
      },
    },
  },
  seedance: {
    name: 'Seedance',
    models: {
      'seedance-preview': {
        name: 'Seedance Preview',
        backend: 'live',
        modes: {
          text_to_video: { name: 'Text → Video', fields: ['prompt', 'durationSeedance', 'aspectRatioSeedance'] },
          image_to_video: { name: 'Image → Video', fields: ['referenceImages', 'prompt', 'durationSeedance', 'aspectRatioSeedance'] },
        },
      },
      'seedance-fast': {
        name: 'Seedance Fast',
        backend: 'live',
        modes: {
          text_to_video: { name: 'Text → Video', fields: ['prompt', 'durationSeedance', 'aspectRatioSeedance'] },
          image_to_video: { name: 'Image → Video', fields: ['referenceImages', 'prompt', 'durationSeedance', 'aspectRatioSeedance'] },
        },
      },
    },
  },
  sora: {
    name: 'Sora',
    models: {
      'sora-2': {
        name: 'Sora 2',
        backend: 'live',
        modes: {
          text_to_video: { name: 'Text → Video', fields: ['prompt', 'durationSora', 'aspectRatioSora'] },
        },
      },
    },
  },
};

const IMAGE_REGISTRY = {
  nano_banana: {
    name: 'Nano Banana',
    models: {
      'nano-banana': {
        name: 'Nano Banana',
        backend: 'planned',
        modes: {
          image_edit: { name: 'Image Edit', fields: ['sourceImage', 'prompt'] },
        },
      },
    },
  },
  nano_banana_pro: {
    name: 'Nano Banana Pro',
    models: {
      'nano-banana-pro': {
        name: 'Nano Banana Pro',
        backend: 'planned',
        modes: {
          image_to_image: { name: 'Image → Image', fields: ['sourceImage', 'prompt', 'resolutionImage', 'aspectRatioImage', 'safetyLevel'] },
          text_to_image: { name: 'Text → Image', fields: ['prompt', 'resolutionImage', 'aspectRatioImageText', 'safetyLevel'] },
        },
      },
    },
  },
  posters: {
    name: 'Фото / Афиши',
    models: {
      'poster-engine': {
        name: 'Poster / Edit Flow',
        backend: 'planned',
        modes: {
          poster: { name: 'Poster', fields: ['sourceImage', 'prompt', 'posterStyle'] },
          photo_edit: { name: 'Photo Edit', fields: ['sourceImage', 'prompt'] },
        },
      },
    },
  },
  photosession: {
    name: 'Нейро фотосессии',
    models: {
      'photosession': {
        name: 'Neuro Photosession',
        backend: 'planned',
        modes: {
          photosession: { name: 'Photosession', fields: ['sourceImage', 'prompt', 'stylePreset', 'moodPreset'] },
        },
      },
    },
  },
  two_images: {
    name: 'Картинка + Картинка',
    models: {
      'two-images': {
        name: 'Two Images',
        backend: 'planned',
        modes: {
          merge: { name: 'Merge / Transfer', fields: ['baseImage', 'sourceImage', 'prompt'] },
        },
      },
    },
  },
  text_to_image: {
    name: 'Текст → Картинка',
    models: {
      't2i': {
        name: 'Text to Image',
        backend: 'planned',
        modes: {
          t2i: { name: 'Text → Image', fields: ['prompt'] },
        },
      },
    },
  },
};

function saveState() {
  localStorage.setItem('astrabot:studio', state.studio);
  localStorage.setItem('astrabot:apiBaseUrl', state.apiBaseUrl);
  localStorage.setItem('astrabot:authToken', state.authToken || '');
  localStorage.setItem('astrabot:me', JSON.stringify(state.me || null));
  localStorage.setItem('astrabot:recentRuns', JSON.stringify(state.recentRuns.slice(0, 50)));
  localStorage.setItem('astrabot:workspaceNotes', state.workspaceNotes);
  localStorage.setItem('astrabot:chatModel', state.chat.model);
  localStorage.setItem('astrabot:chatMode', state.chat.mode);
  localStorage.setItem('astrabot:chatTemperature', String(state.chat.temperature));
  localStorage.setItem('astrabot:chatMaxTokens', String(state.chat.maxTokens));
  localStorage.setItem('astrabot:chatMessages', JSON.stringify(state.chat.messages.slice(-50)));
  localStorage.setItem('astrabot:videoState', JSON.stringify({
    provider: state.video.provider,
    model: state.video.model,
    mode: state.video.mode,
    prompt: state.video.prompt,
    duration: state.video.duration,
    resolution: state.video.resolution,
    aspectRatio: state.video.aspectRatio,
    enableAudio: state.video.enableAudio,
    quality: state.video.quality,
    motionDurationSec: state.video.motionDurationSec,
  }));
}

function escapeHtml(str = '') {
  return String(str)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function formatDate(ts) {
  try {
    return new Date(ts).toLocaleString('ru-RU');
  } catch {
    return ts;
  }
}


function trimText(value, max = 120) {
  const text = String(value || '').trim();
  if (!text) return '';
  if (!Number.isFinite(Number(max)) || max <= 0) return text;
  return text.length > max ? `${text.slice(0, max).trim()}…` : text;
}

function formatFileSize(bytes) {
  const value = Number(bytes || 0);
  if (!Number.isFinite(value) || value <= 0) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let size = value;
  let unitIndex = 0;
  while (size >= 1024 && unitIndex < units.length - 1) {
    size /= 1024;
    unitIndex += 1;
  }
  const digits = size >= 100 || unitIndex === 0 ? 0 : size >= 10 ? 1 : 2;
  return `${size.toFixed(digits)} ${units[unitIndex]}`;
}

function historySelectedItem() {
  const items = Array.isArray(state.history?.items) ? state.history.items : [];
  const selectedId = String(state.history?.selectedId || '').trim();
  if (state.history?.selectedItem && state.history.selectedItem.id) {
    return state.history.selectedItem;
  }
  if (selectedId) {
    const found = items.find((item) => String(item?.id || '') === selectedId);
    if (found) return found;
  }
  return items[0] || null;
}

function historyVideoUrl(item) {
  if (!item) return '';
  const candidates = [
    item.video_url,
    item.download_url,
    item.signed_url,
    item.public_url,
    item.provider_video_url,
  ].filter(Boolean);
  return candidates[0] || '';
}

function historyVideoDownloadUrl(item) {
  if (!item) return '';
  const candidates = [
    item.download_url,
    item.video_url,
    item.signed_url,
    item.public_url,
    item.provider_video_url,
  ].filter(Boolean);
  return candidates[0] || '';
}

function historyStatusTone(status) {
  const value = String(status || '').toLowerCase();
  if (!value || value === 'idle') return 'muted';
  if (['completed', 'success', 'succeeded', 'finished', 'done'].includes(value)) return 'ok';
  if (['failed', 'error', 'cancelled', 'canceled'].includes(value)) return 'error';
  return 'muted';
}

function historyStatusLabel(status) {
  const value = String(status || '').toLowerCase();
  const map = {
    idle: 'Ожидание',
    queued: 'В очереди',
    pending: 'В очереди',
    processing: 'Обрабатывается',
    running: 'Обрабатывается',
    in_progress: 'Обрабатывается',
    completed: 'Готово',
    success: 'Готово',
    succeeded: 'Готово',
    finished: 'Готово',
    done: 'Готово',
    failed: 'Ошибка',
    error: 'Ошибка',
    cancelled: 'Остановлено',
    canceled: 'Остановлено',
  };
  return map[value] || (status || 'Ожидание');
}

function toast(type, title, text) {
  const stack = document.getElementById('toastStack');
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.innerHTML = `<strong>${escapeHtml(title)}</strong><div>${escapeHtml(text)}</div>`;
  stack.appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transform = 'translateY(6px)'; }, 3400);
  setTimeout(() => el.remove(), 3900);
}

function requireAuth() {
  if (!state.authToken || !state.me) {
    toast('error', 'Нужен вход', 'Сначала войди через Telegram, чтобы использовать боевые действия.');
    return false;
  }
  return true;
}

async function apiFetch(path, options = {}) {
  const base = String(state.apiBaseUrl || '').replace(/\/$/, '');
  if (!base) throw new Error('API Base URL is empty');
  const headers = new Headers(options.headers || {});
  if (state.authToken && !headers.has('Authorization')) headers.set('Authorization', `Bearer ${state.authToken}`);
  const res = await fetch(`${base}${path}`, { ...options, headers });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = data.detail || data.error || JSON.stringify(data);
    } catch {
      detail = await res.text();
    }
    throw new Error(detail || `HTTP ${res.status}`);
  }
  return res;
}

function pushRun(run) {
  state.recentRuns.unshift({ id: crypto.randomUUID(), ts: new Date().toISOString(), ...run });
  state.recentRuns = state.recentRuns.slice(0, 40);
  saveState();
  renderRecentRuns();
}

function setFile(key, file, multiple = false) {
  if (!file) return;
  if (multiple) {
    const files = Array.from(file);
    runtime.files[key] = files.map((f) => ({ file: f, name: f.name, url: URL.createObjectURL(f), type: f.type || '', size: f.size || 0 }));
    return;
  }
  runtime.files[key] = { file, name: file.name, url: URL.createObjectURL(file), type: file.type || '', size: file.size || 0 };
}

function getFile(key) {
  return runtime.files[key] || null;
}

function getChatAttachments() {
  const files = getFile('chat.attachments');
  return Array.isArray(files) ? files : [];
}

function clearChatAttachments() {
  const files = getChatAttachments();
  files.forEach((item) => {
    if (item?.url) {
      try { URL.revokeObjectURL(item.url); } catch (_e) {}
    }
  });
  delete runtime.files['chat.attachments'];
  const input = document.getElementById('chat_attachments');
  if (input) input.value = '';
}

function removeChatAttachment(index) {
  const files = getChatAttachments();
  if (!files.length) return;
  const next = files.filter((_, i) => i !== Number(index));
  const removed = files.find((_, i) => i === Number(index));
  if (removed?.url) {
    try { URL.revokeObjectURL(removed.url); } catch (_e) {}
  }
  if (next.length) runtime.files['chat.attachments'] = next;
  else delete runtime.files['chat.attachments'];
  const input = document.getElementById('chat_attachments');
  if (input && !next.length) input.value = '';
}

function getCurrentVideoModel() {
  const provider = VIDEO_REGISTRY[state.video.provider];
  return provider?.models?.[state.video.model] || null;
}

function getCurrentImageModel() {
  const provider = IMAGE_REGISTRY[state.image.provider];
  return provider?.models?.[state.image.model] || null;
}

function isPromptBuilderAvailable() {
  return state.chat.model === 'gpt-5.4';
}

function ensureChatModeCompatibility(showToast = false) {
  if (isPromptBuilderAvailable()) {
    if (state.chat.mode !== 'prompt_builder') {
      state.chat.mode = 'prompt_builder';
      if (showToast) toast('info', 'Режим изменён', 'Для GPT 5.4 включён только Prompt Builder.');
    }
    return;
  }
  if (state.chat.mode !== 'chat') {
    state.chat.mode = 'chat';
    if (showToast) toast('info', 'Режим изменён', 'Для GPT 4 mini доступен только обычный чат.');
  }
}

function currentMeta() {
  switch (state.studio) {
    case 'chat':
      return { studio: 'ChatGPT', provider: 'Chat GPT', model: state.chat.model, mode: state.chat.mode === 'prompt_builder' ? 'Prompt Builder' : 'Chat' };
    case 'video': {
      const provider = VIDEO_REGISTRY[state.video.provider];
      const model = provider?.models?.[state.video.model];
      const mode = model?.modes?.[state.video.mode];
      return { studio: 'Video', provider: provider?.name || '—', model: model?.name || '—', mode: mode?.name || '—' };
    }
    case 'image': {
      const provider = IMAGE_REGISTRY[state.image.provider];
      const model = provider?.models?.[state.image.model];
      const mode = model?.modes?.[state.image.mode];
      return { studio: 'Image', provider: provider?.name || '—', model: model?.name || '—', mode: mode?.name || '—' };
    }
    case 'voice': return { studio: 'Voice', provider: 'ElevenLabs', model: state.voice.modelId, mode: 'Text to Speech' };
    case 'music': return { studio: 'Music', provider: state.music.provider === 'udio' ? 'Udio' : 'Suno', model: state.music.model, mode: state.music.mode === 'lyrics' ? 'Lyrics' : 'Idea' };
    case 'library': return { studio: 'Library', provider: 'Prompt Library', model: state.prompts.selectedCategory || 'categories', mode: state.prompts.selectedGroupId || 'browse' };
    case 'workspace': return { studio: 'Workspace', provider: 'Project Board', model: 'Internal', mode: 'Planning' };
    case 'history': return { studio: 'History', provider: 'Local timeline', model: 'Runs', mode: 'Audit' };
    case 'billing': return { studio: 'Billing', provider: 'Wallet', model: 'Tokens', mode: 'Economics' };
    case 'profile': return { studio: 'Profile', provider: 'User State', model: 'Telegram', mode: 'System' };
    default: return { studio: 'AstraBot', provider: 'Workspace', model: '—', mode: '—' };
  }
}



function videoProviderConfig() {
  return VIDEO_REGISTRY[state.video.provider] || VIDEO_REGISTRY.kling;
}

function videoModelConfig() {
  return videoProviderConfig().models[state.video.model] || Object.values(videoProviderConfig().models)[0];
}

function videoModeConfig() {
  return videoModelConfig().modes[state.video.mode] || Object.values(videoModelConfig().modes)[0];
}

function syncVideoSelection() {
  const provider = VIDEO_REGISTRY[state.video.provider] ? state.video.provider : 'kling';
  state.video.provider = provider;
  const providerConfig = VIDEO_REGISTRY[provider];
  if (!providerConfig.models[state.video.model]) {
    state.video.model = Object.keys(providerConfig.models)[0];
  }
  const modelConfig = providerConfig.models[state.video.model];
  if (!modelConfig.modes[state.video.mode]) {
    state.video.mode = Object.keys(modelConfig.modes)[0];
  }
}

function pluralizeTokens(value) {
  const n = Math.abs(Number(value) || 0);
  const n10 = n % 10;
  const n100 = n % 100;
  if (n10 === 1 && n100 !== 11) return 'токен';
  if (n10 >= 2 && n10 <= 4 && (n100 < 12 || n100 > 14)) return 'токена';
  return 'токенов';
}

function getVideoRunCost() {
  syncVideoSelection();
  const duration = Number(state.video.duration || 0);
  const quality = String(state.video.quality || 'pro').toLowerCase();
  const model = state.video.model;
  if (model === 'motion-control') {
    const seconds = Number(state.video.motionDurationSec || 0);
    if (!seconds) return { known: false, label: '▶ Запуск', helper: 'Стоимость зависит от длины референс-видео.' };
    const rate = quality === 'standard' ? 1 : 2;
    const tokens = Math.max(1, Math.ceil(seconds)) * rate;
    return { known: true, tokens, label: `▶ Запуск • ${tokens} ${pluralizeTokens(tokens)}` };
  }
  if (model === 'kling-1.6') {
    const rate = quality === 'standard' ? 1 : 2;
    const tokens = Math.max(1, duration) * rate;
    return { known: true, tokens, label: `▶ Запуск • ${tokens} ${pluralizeTokens(tokens)}` };
  }
  if (model === 'kling-2.5') {
    const tokens = Math.max(1, duration);
    return { known: true, tokens, label: `▶ Запуск • ${tokens} ${pluralizeTokens(tokens)}` };
  }
  if (model === 'kling-3.0') {
    const rate = state.video.enableAudio ? 3 : 2;
    const tokens = Math.max(1, duration) * rate;
    return { known: true, tokens, label: `▶ Запуск • ${tokens} ${pluralizeTokens(tokens)}` };
  }
  if (model === 'veo-fast') {
    const rate = state.video.enableAudio ? 2 : 1;
    const tokens = Math.max(1, duration) * rate;
    return { known: true, tokens, label: `▶ Запуск • ${tokens} ${pluralizeTokens(tokens)}` };
  }
  if (model === 'veo-3.1-pro') {
    const rate = state.video.enableAudio ? 3 : 2;
    const tokens = Math.max(1, duration) * rate;
    return { known: true, tokens, label: `▶ Запуск • ${tokens} ${pluralizeTokens(tokens)}` };
  }
  if (model === 'seedance-preview') {
    const tokens = Math.max(1, duration) * 2;
    return { known: true, tokens, label: `▶ Запуск • ${tokens} ${pluralizeTokens(tokens)}` };
  }
  if (model === 'seedance-fast') {
    const tokens = Math.max(1, duration);
    return { known: true, tokens, label: `▶ Запуск • ${tokens} ${pluralizeTokens(tokens)}` };
  }
  if (model === 'sora-2') {
    const costMap = { 4: 5, 8: 10, 12: 15 };
    const tokens = costMap[duration] || 5;
    return { known: true, tokens, label: `▶ Запуск • ${tokens} ${pluralizeTokens(tokens)}` };
  }
  return { known: false, label: '▶ Запуск' };
}

function videoRunButtonLabel() {
  if (state.video.isGenerating) return '⏳ Генерация...';
  const cost = getVideoRunCost();
  if (cost.known) return cost.label;
  return cost.label || '▶ Запуск';
}

function setVideoPanel(panel) {
  state.video.panel = panel === 'library' ? 'library' : 'params';
  if (state.video.panel === 'library' && state.authToken) {
    loadVideoHistory({ silent: true, keepSelection: true }).catch(() => {});
  }
  saveState();
  render();
}

function historySelectedItem() {
  if (!state.history.selectedId) return null;
  return state.history.items.find((item) => item.id === state.history.selectedId) || state.history.selectedItem || null;
}

function historyVideoUrl(item) {
  if (!item) return '';
  return item.video_url || item.download_url || item.signed_url || item.provider_video_url || '';
}

function historyVideoDownloadUrl(item) {
  if (!item) return '';
  return item.download_url || item.video_url || item.signed_url || item.provider_video_url || '';
}

function historyStatusLabel(status) {
  const value = String(status || '').toLowerCase();
  if (['completed', 'success', 'succeeded', 'done', 'finished'].includes(value)) return 'Готово';
  if (['failed', 'error'].includes(value)) return 'Ошибка';
  if (['queued', 'pending', 'submitted'].includes(value)) return 'В очереди';
  if (['processing', 'running', 'in_progress'].includes(value)) return 'Генерация';
  return value ? value : 'Ожидание';
}

function historyStatusTone(status) {
  const value = String(status || '').toLowerCase();
  if (['completed', 'success', 'succeeded', 'done', 'finished'].includes(value)) return 'ok';
  if (['failed', 'error'].includes(value)) return 'danger';
  if (['queued', 'pending', 'submitted', 'processing', 'running', 'in_progress'].includes(value)) return 'warn';
  return 'muted';
}

async function probeMotionDuration(fileObj) {
  if (!fileObj?.url) {
    state.video.motionDurationSec = null;
    saveState();
    render();
    return;
  }
  const video = document.createElement('video');
  video.preload = 'metadata';
  video.src = fileObj.url;
  video.onloadedmetadata = () => {
    const duration = Number(video.duration || 0);
    state.video.motionDurationSec = Number.isFinite(duration) && duration > 0 ? Math.ceil(duration) : null;
    saveState();
    render();
  };
  video.onerror = () => {
    state.video.motionDurationSec = null;
    saveState();
    render();
  };
}

function renderNav() {
  const nav = document.getElementById('studioNav');
  const order = ['chat', 'video', 'image', 'voice', 'music', 'library', 'workspace', 'history', 'billing', 'profile'];
  nav.innerHTML = order.map((key) => {
    const meta = STUDIO_META[key];
    return `
      <button class="nav-item ${state.studio === key ? 'active' : ''}" data-action="switch-studio" data-studio="${key}">
        <span class="nav-emoji">${meta.emoji}</span>
        <span>
          <span class="nav-title">${escapeHtml(meta.title)}</span>
          <span class="nav-subtitle">${escapeHtml(meta.subtitle)}</span>
        </span>
      </button>
    `;
  }).join('');
}

function renderHeader() {
  const meta = STUDIO_META[state.studio];
  const shell = document.querySelector('.shell');
  const inspector = document.querySelector('.inspector');
  const workspaceTopline = document.querySelector('.workspace-topline');
  const globalRunBtn = document.getElementById('globalRunBtn');
  const seedDemoBtn = document.getElementById('seedDemoBtn');
  const topbarActions = document.querySelector('.topbar-actions');
  const resetStudioBtn = document.getElementById('resetStudioBtn');
  const inspectorTitle = document.querySelector('.inspector-head h2');
  const inspectorEyebrow = document.querySelector('.inspector-head .eyebrow');
  shell?.classList.toggle('chat-no-inspector', false);
  if (inspector) inspector.setAttribute('aria-hidden', state.studio === 'chat' ? 'true' : 'false');
  if (workspaceTopline) workspaceTopline.style.display = state.studio === 'chat' ? 'none' : '';

  const hideTopActions = state.studio === 'chat' || state.studio === 'video';
  if (topbarActions) topbarActions.style.display = hideTopActions ? 'none' : '';
  if (seedDemoBtn) seedDemoBtn.style.display = state.studio === 'video' ? 'none' : '';
  if (globalRunBtn) globalRunBtn.style.display = state.studio === 'chat' || state.studio === 'video' ? 'none' : '';
  if (resetStudioBtn) resetStudioBtn.style.display = state.studio === 'video' ? 'none' : '';

  if (inspectorTitle) {
    inspectorTitle.textContent = state.studio === 'video' && state.video.panel === 'library' ? 'Библиотека видео' : 'Параметры';
  }
  if (inspectorEyebrow) {
    inspectorEyebrow.textContent = state.studio === 'video' && state.video.panel === 'library' ? 'Library' : 'Inspector';
  }

  document.getElementById('headerTitle').textContent = meta.title;
  document.getElementById('headerSubtitle').textContent = meta.subtitle;
  document.getElementById('headerEyebrow').textContent = `${meta.emoji} ${meta.title}`;
  const metaInfo = currentMeta();
  document.getElementById('metaStudio').textContent = metaInfo.studio;
  document.getElementById('metaProvider').textContent = metaInfo.provider;
  document.getElementById('metaModel').textContent = metaInfo.model;
  document.getElementById('metaMode').textContent = metaInfo.mode;
  document.getElementById('apiBaseUrl').value = state.apiBaseUrl;
  document.getElementById('balanceValue').textContent = state.balance == null ? '—' : `${state.balance} ток.`;
  document.getElementById('apiStatus').className = `badge ${state.apiOnline ? 'ok' : 'muted'}`;
  document.getElementById('apiStatus').textContent = state.apiOnline ? 'online' : 'offline';
  renderAuthCard();
}



function botUsernameFromBase(baseUrl) {
  const fromConfig = window.ASTRABOT_BOT_USERNAME || 'NeiroAstraBot';
  return fromConfig.replace(/^@/, '');
}

function formatUserName(user) {
  if (!user) return '—';
  const full = `${user.first_name || ''} ${user.last_name || ''}`.trim();
  return full || (user.username ? `@${user.username}` : 'Telegram user');
}

function renderAuthCard() {
  const guest = document.getElementById('authGuestView');
  const userView = document.getElementById('authUserView');
  if (!guest || !userView) return;
  const loggedIn = !!(state.authToken && state.me);
  guest.classList.toggle('hidden', loggedIn);
  userView.classList.toggle('hidden', !loggedIn);
  const hint = document.getElementById('balanceHint');
  if (hint) hint.textContent = loggedIn ? 'данные из backend' : 'выполни вход через Telegram';
  if (loggedIn) {
    const user = state.me || {};
    const nameEl = document.getElementById('authUserName');
    const metaEl = document.getElementById('authUserMeta');
    const avatarEl = document.getElementById('authAvatar');
    if (nameEl) nameEl.textContent = formatUserName(user);
    if (metaEl) metaEl.textContent = user.username ? `@${user.username}` : `id ${user.telegram_user_id || user.id || '—'}`;
    if (avatarEl) {
      if (user.photo_url) avatarEl.innerHTML = `<img src="${escapeHtml(user.photo_url)}" alt="avatar">`;
      else avatarEl.textContent = (user.first_name || user.username || 'TG').slice(0, 2).toUpperCase();
    }
    return;
  }
  mountTelegramLogin();
}

function setSession(payload) {
  state.authToken = payload?.access_token || '';
  state.me = payload?.user || null;
  if (typeof payload?.balance_tokens !== 'undefined') state.balance = Number(payload.balance_tokens || 0);
  localStorage.setItem('astrabot:authToken', state.authToken || '');
  localStorage.setItem('astrabot:me', JSON.stringify(state.me || null));
  render();
}

async function logoutWorkspace() {
  state.authToken = '';
  state.me = null;
  state.balance = null;
  localStorage.removeItem('astrabot:authToken');
  localStorage.removeItem('astrabot:me');
  try { await apiFetch('/api/workspace/logout', { method: 'POST' }); } catch (_) {}
  render();
  toast('success', 'Выход выполнен', 'Сессия сайта очищена.');
}

async function handleTelegramAuth(user) {
  try {
    const res = await apiFetch('/api/workspace/auth/telegram', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ auth_data: user }),
    });
    const data = await res.json();
    setSession(data);
    toast('success', 'Вход выполнен', `Добро пожаловать, ${formatUserName(data.user)}.`);
  } catch (e) {
    toast('error', 'Вход через Telegram не выполнен', String(e.message || e));
  }
}

window.onTelegramAuth = handleTelegramAuth;

function mountTelegramLogin() {
  const box = document.getElementById('telegramLoginMount');
  if (!box || box.dataset.mounted === '1' || (state.authToken && state.me)) return;
  box.dataset.mounted = '1';
  box.innerHTML = '';
  const script = document.createElement('script');
  script.async = true;
  script.src = 'https://telegram.org/js/telegram-widget.js?22';
  script.setAttribute('data-telegram-login', botUsernameFromBase(state.apiBaseUrl));
  script.setAttribute('data-size', 'large');
  script.setAttribute('data-radius', '12');
  script.setAttribute('data-userpic', 'false');
  script.setAttribute('data-request-access', 'write');
  script.setAttribute('data-onauth', 'onTelegramAuth(user)');
  box.appendChild(script);
}

async function loadMe() {
  if (!state.authToken) return false;
  try {
    const res = await apiFetch('/api/workspace/me');
    const data = await res.json();
    state.me = data.user || null;
    state.balance = typeof data.balance_tokens !== 'undefined' ? Number(data.balance_tokens || 0) : state.balance;
    localStorage.setItem('astrabot:me', JSON.stringify(state.me || null));
    return true;
  } catch (e) {
    state.authToken = '';
    state.me = null;
    localStorage.removeItem('astrabot:authToken');
    localStorage.removeItem('astrabot:me');
    return false;
  }
}

function renderRecentRuns() {
  const box = document.getElementById('recentRuns');
  if (!state.recentRuns.length) {
    box.innerHTML = '<div class="empty-state">Пока пусто</div>';
    return;
  }
  box.innerHTML = state.recentRuns.slice(0, 8).map((run) => `
    <div class="run-item">
      <strong>${escapeHtml(run.title || run.studio || 'Run')}</strong>
      <small>${escapeHtml(run.summary || '')}</small>
      <small>${formatDate(run.ts)}</small>
    </div>
  `).join('');
}

function renderWorkspace() {
  const el = document.getElementById('workspaceBody');
  switch (state.studio) {
    case 'chat': el.innerHTML = renderChatWorkspace(); break;
    case 'video': el.innerHTML = renderVideoWorkspace(); break;
    case 'image': el.innerHTML = renderImageWorkspace(); break;
    case 'voice': el.innerHTML = renderVoiceWorkspace(); break;
    case 'music': el.innerHTML = renderMusicWorkspace(); break;
    case 'library': el.innerHTML = renderLibraryWorkspace(); break;
    case 'workspace': el.innerHTML = renderPlanningWorkspace(); break;
    case 'history': el.innerHTML = renderHistoryWorkspace(); break;
    case 'billing': el.innerHTML = renderBillingWorkspace(); break;
    case 'profile': el.innerHTML = renderProfileWorkspace(); break;
    default: el.innerHTML = `<div class="placeholder-stage"><div class="empty-copy"><strong>Студия в разработке</strong><div>Для этой студии пока нет workspace-renderer.</div></div></div>`;
  }
}

function renderInspector() {
  const el = document.getElementById('inspectorBody');
  switch (state.studio) {
    case 'chat': el.innerHTML = renderChatInspector(); break;
    case 'video': el.innerHTML = renderVideoInspector(); break;
    case 'image': el.innerHTML = renderImageInspector(); break;
    case 'voice': el.innerHTML = renderVoiceInspector(); break;
    case 'music': el.innerHTML = renderMusicInspector(); break;
    case 'library': el.innerHTML = renderLibraryInspector(); break;
    case 'workspace': el.innerHTML = renderPlanningInspector(); break;
    case 'history': el.innerHTML = renderHistoryInspector(); break;
    case 'billing': el.innerHTML = renderBillingInspector(); break;
    case 'profile': el.innerHTML = renderProfileInspector(); break;
    default: el.innerHTML = '';
  }
}


function renderChatWorkspace() {
  ensureChatModeCompatibility();
  const isPromptBuilder = state.chat.mode === 'prompt_builder';
  const messages = state.chat.messages.map((m) => {
    const canCopyPrompt = m.role === 'assistant' && isPromptBuilder && m.isPrompt !== false;
    return `
      <div class="chat-bubble-wrap ${m.role}">
        <div class="chat-bubble ${m.role}">${escapeHtml(m.content)}</div>
        ${canCopyPrompt ? `
          <div class="chat-bubble-actions">
            <button class="btn ghost small" data-action="copy-chat-prompt" data-text="${encodeURIComponent(m.content || '')}">Скопировать промпт</button>
          </div>
        ` : ''}
      </div>
    `;
  }).join('');
  const attachments = getChatAttachments();
  const attachmentsHtml = attachments.length ? `
    <div class="chat-attachment-strip">
      ${attachments.map((item, index) => `
        <div class="chat-file-pill">
          <span>📎 ${escapeHtml(trimText(item.name || 'file', 34))}</span>
          <button type="button" class="chat-file-pill-remove" data-action="remove-chat-file" data-index="${index}" aria-label="Удалить файл">×</button>
        </div>
      `).join('')}
    </div>
  ` : '';
  const placeholder = isPromptBuilder
    ? 'Опиши, какой prompt нужен: для фото, видео, улучшения черновика или по референсу…'
    : 'Напиши задачу для ChatGPT, попроси идею, анализ, текст или помощь по проекту...';

  return `
    <div class="workspace-grid single">
      <div class="workspace-main placeholder-stage chat">
        <div class="chat-shell">
          <div class="chat-feed" id="chatFeed">${messages}</div>
          <div class="chat-composer">
            ${attachmentsHtml}
            <div class="composer-row">
              <textarea id="chatInput" placeholder="${escapeHtml(placeholder)}">${escapeHtml(state.chat.input || '')}</textarea>
              <div class="composer-actions">
                <input id="chat_attachments" class="hidden" type="file" multiple>
                <button class="btn ghost icon-btn" data-action="pick-chat-files" title="Прикрепить файлы" aria-label="Прикрепить файлы">📎</button>
                <button class="btn primary" data-action="send-chat">Отправить</button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  `;
}



function videoStatusTone(status) {
  const value = String(status || '').toLowerCase();
  if (!value || value === 'idle') return 'muted';
  if (['succeeded', 'completed', 'success', 'finished', 'done'].includes(value)) return 'ok';
  if (['failed', 'error', 'cancelled', 'canceled'].includes(value)) return 'warn';
  return 'muted';
}

function videoStatusLabel(status) {
  const value = String(status || '').toLowerCase();
  const map = {
    idle: 'Ожидание',
    submitted: 'Запущено',
    queued: 'В очереди',
    pending: 'В очереди',
    processing: 'Обрабатывается',
    running: 'Обрабатывается',
    in_progress: 'Обрабатывается',
    succeeded: 'Готово',
    completed: 'Готово',
    success: 'Готово',
    finished: 'Готово',
    done: 'Готово',
    failed: 'Ошибка',
    error: 'Ошибка',
    cancelled: 'Остановлено',
    canceled: 'Остановлено',
  };
  return map[value] || (status || 'Ожидание');
}

function isVideoTaskFailed(status) {
  const value = String(status || '').toLowerCase();
  return ['failed', 'error', 'cancelled', 'canceled'].includes(value);
}

function isVideoTaskFinished(status) {
  const value = String(status || '').toLowerCase();
  return ['succeeded', 'completed', 'success', 'finished', 'done', 'failed', 'error', 'cancelled', 'canceled'].includes(value);
}

function extractVideoTaskStatus(task) {
  return (
    task?.status ||
    task?.state ||
    task?.task_status ||
    task?.task_state ||
    task?.provider_status ||
    task?.output?.status ||
    task?.data?.status ||
    task?.data?.task_status ||
    task?.meta?.status ||
    'unknown'
  );
}

function extractVideoTaskUrl(task) {
  const candidates = [
    task?.output_url,
    task?.video,
    task?.video_url,
    task?.url,
    task?.download_url,
    task?.output?.video,
    task?.output?.video_url,
    task?.output?.url,
    task?.output?.download_url,
    task?.data?.video,
    task?.data?.video_url,
    task?.data?.url,
    task?.data?.download_url,
    task?.data?.output?.video,
    task?.data?.output?.video_url,
    task?.data?.output?.url,
    task?.data?.output?.download_url,
  ].filter(Boolean);
  return candidates[0] || '';
}

function extractVideoTaskDownloadUrl(task) {
  const candidates = [
    task?.download_url,
    task?.video,
    task?.output?.download_url,
    task?.output?.video,
    task?.data?.download_url,
    task?.data?.video,
    task?.data?.output?.download_url,
    task?.data?.output?.video,
  ].filter(Boolean);
  return candidates[0] || '';
}

function extractVideoTaskCoverUrl(task) {
  const candidates = [
    task?.cover_url,
    task?.output?.cover_url,
    task?.data?.cover_url,
    task?.data?.output?.cover_url,
  ].filter(Boolean);
  return candidates[0] || '';
}

function extractVideoTaskPercent(task) {
  const raw = (
    task?.percent ??
    task?.output?.percent ??
    task?.data?.percent ??
    task?.data?.output?.percent ??
    null
  );
  if (raw === null || raw === undefined || raw === '') return null;
  const value = Number(raw);
  if (!Number.isFinite(value)) return null;
  return Math.max(0, Math.min(100, Math.round(value)));
}

function extractVideoTaskError(task) {
  const candidates = [
    task?.error_message,
    task?.error?.message,
    task?.error?.raw_message,
    task?.message,
    task?.detail,
    task?.data?.error?.message,
    task?.data?.error_message,
    task?.output?.error?.message,
    task?.output?.error_message,
    task?.output?.message,
  ].filter(Boolean);

  if (candidates.length) return String(candidates[0]);

  const status = String(extractVideoTaskStatus(task) || '').toLowerCase();
  if (['failed', 'error', 'cancelled', 'canceled'].includes(status)) {
    return String(task?.message || task?.detail || '');
  }
  return '';
}

function getVideoLoadingHeadline(_percent, status) {
  if (isVideoTaskFailed(status)) return 'Генерация завершилась с ошибкой';
  if (['completed', 'success', 'succeeded', 'finished', 'done'].includes(String(status || '').toLowerCase())) {
    return 'Финализируем результат';
  }
  return 'Генерация началась';
}

function getVideoLoadingSubline(_percent, status) {
  if (isVideoTaskFailed(status)) return state.video.errorText || state.video.statusText || 'Не удалось завершить генерацию.';
  if (['completed', 'success', 'succeeded', 'finished', 'done'].includes(String(status || '').toLowerCase()) && !state.video.outputUrl) {
    return 'Провайдер уже завершил рендер. Подтягиваем итоговый файл в рабочую зону.';
  }
  return 'Ожидайте, видео появится в рабочей зоне автоматически.';
}

function getVideoLoadingPhase(status) {
  if (isVideoTaskFailed(status)) return 'Ошибка рендера';
  const normalized = String(status || '').toLowerCase();
  if (['completed', 'success', 'succeeded', 'finished', 'done'].includes(normalized)) return 'Финализация файла';
  if (['submitted', 'queued', 'pending', 'created'].includes(normalized)) return 'Постановка в очередь';
  if (['processing', 'running', 'in_progress'].includes(normalized)) return 'Сборка кадров';
  return 'AI scan / render engine';
}

function stopVideoPolling() {
  if (runtime.videoPollTimer) {
    clearInterval(runtime.videoPollTimer);
    runtime.videoPollTimer = null;
  }
}

function startVideoPolling({ immediate = false } = {}) {
  if (!state.video.generationId || state.video.outputUrl || isVideoTaskFailed(state.video.lastStatus)) return;
  stopVideoPolling();
  runtime.videoPollTimer = setInterval(() => {
    pollVideoTask({ silent: true, fromAuto: true }).catch(() => {});
  }, 5000);
  if (immediate) {
    pollVideoTask({ silent: true, fromAuto: true }).catch(() => {});
  }
}

function clearVideoRunState({ keepPrompt = true } = {}) {
  stopVideoPolling();
  state.video.outputUrl = '';
  state.video.downloadUrl = '';
  state.video.coverUrl = '';
  state.video.percent = null;
  state.video.generationId = '';
  state.video.providerTaskId = '';
  state.video.errorText = '';
  state.video.lastStatus = 'idle';
  state.video.isGenerating = false;
  state.video.statusText = 'Выбери модель, настрой параметры и нажми запуск.';
  if (!keepPrompt) state.video.prompt = '';
  saveState();
}


function renderVideoWorkspace() {
  const activeItem = historySelectedItem();
  const showHistoryVideo = state.video.panel === 'library' && activeItem && historyVideoUrl(activeItem);
  const previewUrl = showHistoryVideo ? historyVideoUrl(activeItem) : state.video.outputUrl;
  const statusLabel = videoStatusLabel(state.video.lastStatus);
  const loadingHeadline = getVideoLoadingHeadline(state.video.percent, state.video.lastStatus);
  const loadingSubline = getVideoLoadingSubline(state.video.percent, state.video.lastStatus);
  const assets = [
    mediaCard('Start frame', getFile('video.startFrame')),
    mediaCard('End frame', getFile('video.endFrame')),
    mediaCard('Last frame', getFile('video.lastFrame')),
    mediaCard('Avatar image', getFile('video.avatarImage')),
    mediaCard('Motion video', getFile('video.motionVideo'), true),
    mediaCard('Reference images', getFile('video.referenceImages'), false, true),
  ].filter(Boolean).join('');

  const stageInner = previewUrl ? `
    <div class="video-stage-result">
      <video class="preview-media" src="${escapeHtml(previewUrl)}" controls playsinline poster="${escapeHtml(state.video.coverUrl || '')}"></video>
      <div class="actions compact-gap" style="justify-content:center; flex-wrap:wrap; margin-top:14px;">
        <a class="btn primary" href="${escapeHtml(state.video.downloadUrl || previewUrl)}" download>Скачать видео</a>
        <button class="btn outline" data-action="clear-video-run">Очистить</button>
      </div>
    </div>
  ` : (state.video.generationId ? `
    <div class="video-loader-shell video-loader-shell-scan">
      <div class="video-scan-stage">
        <div class="video-scan-grid"></div>
        <div class="video-scan-sweep"></div>
        <div class="video-scan-glow"></div>
        <div class="video-loader">
          <div class="video-loader-ring"></div>
          <div class="video-loader-ring ring-2"></div>
          <div class="video-loader-ring ring-3"></div>
          <div class="video-loader-core">▶</div>
        </div>
      </div>
      <div class="video-loader-copy">
        <strong>${escapeHtml(loadingHeadline)}</strong>
        <div>${escapeHtml(loadingSubline)}</div>
      </div>
    </div>
  ` : `
    <div class="empty-copy">
      <strong>Видео появится здесь</strong>
      <div>При открытии студии всегда показывается чистая рабочая область. Выбери модель справа, укажи параметры и нажми запуск.</div>
    </div>
  `);

  return `
    <div class="workspace-grid single video-workspace-grid">
      <div class="workspace-main scroll video-workspace-main">
        <div class="result-card video-stage-card">
          <div class="video-stage-head video-stage-head-clean">
            <div>
              <h4 style="margin:0 0 6px;">Рабочая область</h4>
              <small>${escapeHtml(state.video.errorText || state.video.statusText || `Статус: ${statusLabel}. Здесь появится текущее видео или выбранный ролик из библиотеки.`)}</small>
            </div>
          </div>
          <div class="placeholder-stage video video-stage-clean">
            ${stageInner}
          </div>
        </div>
        ${assets ? `<div class="upload-grid two" style="margin-top:16px;">${assets}</div>` : ''}
      </div>
    </div>
  `;
}


function renderImageWorkspace() {
  const source = getFile('image.sourceImage');
  const base = getFile('image.baseImage');
  return `
    <div class="workspace-grid">
      <div class="workspace-main scroll">
        <div class="placeholder-stage image">
          ${state.image.outputUrl ? `<img class="preview-media" src="${escapeHtml(state.image.outputUrl)}" alt="Generated image">` : `
          <div class="empty-copy">
            <strong>Image workspace</strong>
            <div>Тут будут карточки результатов, before/after, галерея вариантов, превью референсов и export panel. Архитектура уже знает про Nano Banana, Nano Banana Pro, posters, two-images и text-to-image.</div>
          </div>`}
        </div>
        <div class="upload-grid two" style="margin-top:16px;">
          ${mediaCard('Source image', source) || ''}
          ${mediaCard('Base image', base) || ''}
          ${!source && !base ? `<div class="asset-card"><h4>Ожидаются входные изображения</h4><small>Правые поля будут меняться в зависимости от режима: source image, base image, prompt, resolution, safety level и т.д.</small></div>` : ''}
        </div>
      </div>
      <div class="workspace-side scroll">
        <div class="result-card">
          <h4>Выбранный сценарий</h4>
          <small>${escapeHtml(currentMeta().provider)} → ${escapeHtml(currentMeta().model)} → ${escapeHtml(currentMeta().mode)}</small>
        </div>
        <div class="result-card">
          <h4>Статус подключения</h4>
          <small>${escapeHtml(state.image.statusText)}</small>
        </div>
      </div>
    </div>
  `;
}

function renderVoiceWorkspace() {
  const voiceName = (state.voice.voices.find(v => v.voice_id === state.voice.voiceId) || {}).name || '—';
  return `
    <div class="workspace-grid">
      <div class="workspace-main scroll">
        <div class="placeholder-stage">
          ${state.voice.audioUrl ? `
            <div style="width:min(560px, 100%); display:grid; gap:16px;">
              <strong style="font-size:22px;">${escapeHtml(voiceName)}</strong>
              <audio controls src="${escapeHtml(state.voice.audioUrl)}"></audio>
              <div class="muted">Аудио уже готово. Его можно использовать как voice-over для видео или сохранить в проект.</div>
            </div>
          ` : `
            <div class="empty-copy"><strong>Voice workspace</strong><div>Выбери голос справа, вставь текст и получай MP3 прямо в центр рабочей зоны.</div></div>
          `}
        </div>
      </div>
      <div class="workspace-side scroll">
        <div class="result-card">
          <h4>Текст</h4>
          <small>${escapeHtml(state.voice.text || 'Пока текста нет')}</small>
        </div>
        <div class="result-card">
          <h4>Параметры</h4>
          <small>Voice: ${escapeHtml(voiceName)}<br>Model: ${escapeHtml(state.voice.modelId)}<br>Format: ${escapeHtml(state.voice.outputFormat)}</small>
        </div>
      </div>
    </div>
  `;
}

function renderMusicWorkspace() {
  return `
    <div class="workspace-grid">
      <div class="workspace-main scroll">
        <div class="placeholder-stage">
          <div class="empty-copy">
            <strong>Music workspace</strong>
            <div>Справа выбираются Suno / Udio, режим idea / lyrics, теги, вайб и язык. В центре держим идею трека, текст, ответы Songwriter и будущие аудио-результаты.</div>
          </div>
        </div>
        <div class="planner-card" style="margin-top:16px;">
          <h4>Songwriter output</h4>
          <div class="help-text">${escapeHtml(state.music.songwriterAnswer || 'Пока пусто. Нажми Generate songwriter response.')}</div>
        </div>
      </div>
      <div class="workspace-side scroll">
        <div class="result-card">
          <h4>Идея / текст</h4>
          <small>${escapeHtml(state.music.text || 'Нет текста')}</small>
        </div>
        <div class="result-card">
          <h4>Метаданные</h4>
          <small>Provider: ${escapeHtml(currentMeta().provider)}<br>Mode: ${escapeHtml(currentMeta().mode)}<br>Tags: ${escapeHtml(state.music.tags || '—')}</small>
        </div>
      </div>
    </div>
  `;
}

function renderLibraryWorkspace() {
  const categories = state.prompts.categories.map((c) => `
    <button class="chip ${state.prompts.selectedCategory === c.slug ? 'active' : ''}" data-action="select-category" data-category="${escapeHtml(c.slug)}">${escapeHtml(c.title || c.slug)}</button>
  `).join('');
  const groups = state.prompts.groups.map((g) => `
    <div class="prompt-item">
      <strong>${escapeHtml(g.title)}</strong>
      <small>${escapeHtml(g.cover_url || 'Без cover')}</small>
      <div style="margin-top:10px;"><button class="btn ghost small" data-action="select-group" data-group-id="${escapeHtml(g.id)}">Открыть</button></div>
    </div>
  `).join('');
  const items = state.prompts.items.map((item) => `
    <div class="prompt-item">
      <strong>${escapeHtml(item.title || 'Prompt')}</strong>
      <small>${escapeHtml(item.model_hint || 'Без model_hint')}</small>
      <div class="help-text" style="margin-top:8px;">${escapeHtml((item.prompt_text || '').slice(0, 260))}${(item.prompt_text || '').length > 260 ? '…' : ''}</div>
      <div style="margin-top:10px; display:flex; gap:8px; flex-wrap:wrap;">
        <button class="btn ghost small" data-action="copy-prompt" data-prompt="${encodeURIComponent(item.prompt_text || '')}">Копировать</button>
        <button class="btn outline small" data-action="send-prompt-to-chat" data-prompt="${encodeURIComponent(item.prompt_text || '')}">В ChatGPT</button>
      </div>
    </div>
  `).join('');
  return `
    <div class="workspace-grid">
      <div class="workspace-main scroll">
        <div class="library-card">
          <div class="field-head"><h4>Категории</h4><button class="btn ghost small" data-action="refresh-prompts">Обновить</button></div>
          <div class="quick-chips">${categories || '<span class="muted">Категории ещё не загружены.</span>'}</div>
        </div>
        <div class="upload-grid two" style="margin-top:16px;">
          <div class="library-card">
            <div class="field-head"><h4>Группы</h4><span class="badge muted">${state.prompts.selectedCategory || '—'}</span></div>
            <div class="mini-list">${groups || '<div class="empty-state">Нет групп. Выбери категорию или проверь Supabase.</div>'}</div>
          </div>
          <div class="library-card">
            <div class="field-head"><h4>Элементы</h4><span class="badge muted">${state.prompts.selectedGroupId || '—'}</span></div>
            <div class="mini-list">${items || '<div class="empty-state">Выбери группу, чтобы увидеть prompt items.</div>'}</div>
          </div>
        </div>
      </div>
      <div class="workspace-side scroll">
        <div class="result-card">
          <h4>Как использовать</h4>
          <small>Библиотека уже подключена к существующим роутам &lt;code&gt;/api/prompts/*&lt;/code&gt;. Копируй prompt в буфер или отправляй его прямо в ChatGPT Studio для доработки.</small>
        </div>
      </div>
    </div>
  `;
}

function renderPlanningWorkspace() {
  return `
    <div class="workspace-grid">
      <div class="workspace-main scroll">
        <div class="planner-card">
          <div class="field-head"><h4>Project board</h4><button class="btn ghost small" data-action="seed-plan">Сгенерировать пример</button></div>
          <textarea id="workspaceNotes" rows="18" placeholder="План ролика, структура запуска, референсы, текст озвучки, идеи для music/video/image пайплайна...">${escapeHtml(state.workspaceNotes)}</textarea>
        </div>
      </div>
      <div class="workspace-side scroll">
        <div class="plan-grid two">
          <div class="plan-item"><strong>1. ChatGPT</strong><small>Собери идею, сценарий и промпты.</small></div>
          <div class="plan-item"><strong>2. Video</strong><small>Запусти Kling / Veo с нужным режимом.</small></div>
          <div class="plan-item"><strong>3. Voice</strong><small>Сделай озвучку из готового текста.</small></div>
          <div class="plan-item"><strong>4. Music</strong><small>Подготовь песню или инструментал.</small></div>
        </div>
      </div>
    </div>
  `;
}


function renderHistoryWorkspace() {
  const items = state.history.items || [];
  const selected = historySelectedItem();
  const previewUrl = historyVideoUrl(selected);
  return `
    <div class="history-browser">
      <div class="history-preview-panel">
        <div class="field-head" style="align-items:flex-start; flex-wrap:wrap; gap:12px;">
          <div>
            <h4 style="margin:0 0 6px;">Предпросмотр</h4>
            <small>Лишние сводки и управление убраны. Здесь только ролик и действия с ним.</small>
          </div>
          <div class="actions compact-gap" style="margin-top:0; flex-wrap:wrap;">
            <button class="btn ghost small" data-action="refresh-history">Обновить</button>
            <button class="btn outline small" data-action="switch-studio" data-studio="video">Video Studio</button>
          </div>
        </div>
        ${previewUrl ? `<div class="history-preview-media" style="margin-top:14px;"><video class="preview-media" src="${escapeHtml(previewUrl)}" controls playsinline></video></div>` : `<div class="history-preview-empty" style="margin-top:14px;"><div><strong>${state.authToken ? 'Выбери ролик справа' : 'Нужна авторизация'}</strong><div>${state.authToken ? 'Библиотека открывается справа. Нажми «Просмотр» у нужного ролика.' : 'Сначала войди через Telegram, чтобы увидеть свою библиотеку видео.'}</div></div></div>`}
        ${selected ? `<div class="actions compact-gap" style="margin-top:14px; flex-wrap:wrap;">${previewUrl ? `<a class="btn primary" href="${escapeHtml(historyVideoDownloadUrl(selected) || previewUrl)}" download>Скачать видео</a>` : ''}<button class="btn ghost" data-action="use-history-item" data-generation-id="${escapeHtml(selected.id || '')}">В рабочую зону</button></div>` : ''}
      </div>
      <div class="history-library-panel">
        <div class="field-head"><h4 style="margin:0;">Библиотека видео</h4><span class="badge muted">${items.length}</span></div>
        <div class="history-library-list" style="margin-top:14px;">
          ${state.history.lastError ? `<div class="empty-state">${escapeHtml(state.history.lastError)}</div>` : ''}
          ${items.length ? items.map((item) => `
            <div class="history-library-item ${selected?.id === item.id ? 'active' : ''}">
              <div class="history-item-row"><strong>${escapeHtml(trimText(item.prompt || `${item.provider || 'video'} · ${item.model || ''}`, 96) || 'Видео')}</strong><span class="badge ${historyStatusTone(item.status)}">${escapeHtml(historyStatusLabel(item.status))}</span></div>
              <small>${escapeHtml(formatDate(item.completed_at || item.created_at))}</small>
              <small>${escapeHtml(trimText([item.provider, item.model, item.mode].filter(Boolean).join(' · '), 120) || '—')}</small>
              <div class="actions compact-gap" style="margin-top:10px; flex-wrap:wrap;"><button class="btn ghost small" data-action="preview-history-item" data-generation-id="${escapeHtml(item.id || '')}">Просмотр</button><button class="btn outline small" data-action="use-history-item" data-generation-id="${escapeHtml(item.id || '')}">В рабочую зону</button></div>
            </div>
          `).join('') : `<div class="empty-state">Пока нет сохранённых видео.</div>`}
        </div>
      </div>
    </div>
  `;
}


function renderBillingWorkspace() {
  const runs = state.recentRuns.length;
  const liveCount = state.bootstrap.liveIntegrations.length;
  return `
    <div class="workspace-grid">
      <div class="workspace-main scroll">
        <div class="metrics">
          <div class="metric"><strong>${state.balance == null ? '—' : state.balance}</strong><span>текущий баланс</span></div>
          <div class="metric"><strong>${runs}</strong><span>локальных запусков</span></div>
          <div class="metric"><strong>${liveCount}</strong><span>live интеграций</span></div>
        </div>
        <div class="upload-grid two" style="margin-top:16px;">
          <div class="billing-card">
            <h4>Как сейчас устроено</h4>
            <small>Wallet уже читается через серверную сессию пользователя. Дальше сюда можно перенести пакеты, YooKassa / Stars, историю hold/refund и детализацию затрат по моделям.</small>
          </div>
          <div class="billing-card">
            <h4>Подготовленная архитектура</h4>
            <small>UI уже имеет отдельный billing-раздел. Когда будут web-роуты для платежей и истории транзакций, сюда просто подставится серверный data layer.</small>
          </div>
        </div>
      </div>
      <div class="workspace-side scroll">
        <div class="result-card">
          <h4>Рекомендация</h4>
          <small>Держи пополнение и биллинг как отдельный поток, но не разрывай его с генерациями: стоимость всегда должна считаться и показываться в правом inspector перед запуском.</small>
        </div>
      </div>
    </div>
  `;
}

function renderProfileWorkspace() {
  return `
    <div class="workspace-grid">
      <div class="workspace-main scroll">
        <div class="profile-card">
          <h4>Профиль</h4>
          <div class="tableish">
            <div class="table-row"><span class="muted">Пользователь</span><span>${escapeHtml(state.me ? ((state.me.first_name || '') + ' ' + (state.me.last_name || '')).trim() || state.me.username || 'Telegram user' : '—')}</span><span class="badge muted">telegram</span></div>
            <div class="table-row"><span class="muted">Username</span><span>${escapeHtml(state.me?.username ? '@' + state.me.username : '—')}</span><span class="badge muted">session</span></div>
            <div class="table-row"><span class="muted">API Base URL</span><span>${escapeHtml(state.apiBaseUrl || '—')}</span><span class="badge ${state.apiOnline ? 'ok' : 'warn'}">${state.apiOnline ? 'online' : 'offline'}</span></div>
          </div>
        </div>
      </div>
      <div class="workspace-side scroll">
        <div class="result-card">
          <h4>Следующий шаг</h4>
          <small>Для продакшена сюда нужно добавить Telegram Login с проверкой подписи и общую user-модель, чтобы сайт и бот работали от одного аккаунта.</small>
        </div>
      </div>
    </div>
  `;
}

function mediaCard(title, asset, isVideo = false, multiple = false) {
  if (!asset) return '';
  if (multiple && Array.isArray(asset)) {
    return `
      <div class="asset-card">
        <h4>${escapeHtml(title)}</h4>
        <div class="upload-grid two">
          ${asset.map((a) => `<img class="asset-thumb" src="${escapeHtml(a.url)}" alt="${escapeHtml(a.name)}">`).join('')}
        </div>
        <small>${asset.length} файлов</small>
      </div>
    `;
  }
  return `
    <div class="asset-card">
      <h4>${escapeHtml(title)}</h4>
      ${isVideo ? `<video class="asset-thumb" src="${escapeHtml(asset.url)}" controls></video>` : `<img class="asset-thumb" src="${escapeHtml(asset.url)}" alt="${escapeHtml(asset.name)}">`}
      <small>${escapeHtml(asset.name)}</small>
    </div>
  `;
}


function renderChatInspector() {
  const isPromptBuilder = isPromptBuilderAvailable();
  return `
    <div class="inspector-card">
      <div class="section-title">ChatGPT Studio</div>
      <div class="input-group"><label class="label">Assistant</label><input type="text" value="ChatGPT" disabled></div>
      <div class="input-group"><label class="label">Model</label>
        <select id="chat_model">
          <option value="gpt-4o-mini" ${state.chat.model === 'gpt-4o-mini' ? 'selected' : ''}>GPT 4 mini</option>
          <option value="gpt-5.4" ${state.chat.model === 'gpt-5.4' ? 'selected' : ''}>GPT 5.4</option>
        </select>
      </div>
      <div class="input-group"><label class="label">Режим</label>
        <input type="text" value="${isPromptBuilder ? 'Prompt Builder' : 'Chat'}" disabled>
      </div>
    </div>
  `;
}


function renderVideoInspector() {
  syncVideoSelection();
  const providerOptions = Object.entries(VIDEO_REGISTRY).map(([id, provider]) => `
    <button class="seg-chip ${state.video.provider === id ? 'active' : ''}" data-action="set-video-provider" data-provider="${id}">${escapeHtml(provider.name)}</button>
  `).join('');
  const modelOptions = Object.entries(videoProviderConfig().models).map(([id, model]) => `
    <button class="seg-chip ${state.video.model === id ? 'active' : ''}" data-action="set-video-model" data-model="${id}">${escapeHtml(model.name)}</button>
  `).join('');

  if (state.video.panel === 'library') {
    const items = state.history.items || [];
    return `
      <div class="inspector-card">
        <div class="field-head" style="margin-bottom:12px;"><div class="section-title" style="margin:0;">Библиотека видео</div><button class="btn ghost small" data-action="show-video-params">Параметры</button></div>
        <div class="help-text">При нажатии на историю параметры скрываются, а справа открывается библиотека видео.</div>
        <div class="mini-list" style="margin-top:14px;">
          ${items.length ? items.map((item) => `
            <div class="history-item compact ${state.history.selectedId === item.id ? 'active' : ''}">
              <div class="history-item-row"><strong>${escapeHtml(trimText(item.prompt || `${item.provider || 'video'} · ${item.model || ''}`, 88) || 'Видео')}</strong><span class="badge ${historyStatusTone(item.status)}">${escapeHtml(historyStatusLabel(item.status))}</span></div>
              <small>${escapeHtml(formatDate(item.completed_at || item.created_at))}</small>
              <div class="actions compact-gap" style="margin-top:10px; flex-wrap:wrap;"><button class="btn outline small" data-action="use-history-item" data-generation-id="${escapeHtml(item.id || '')}">В рабочую зону</button></div>
            </div>
          `).join('') : `<div class="empty-state">Пока нет сохранённых видео.</div>`}
        </div>
      </div>
    `;
  }

  return `
    <div class="inspector-card">
      <div class="field-head" style="margin-bottom:12px;"><div class="section-title" style="margin:0;">Video Studio</div><button class="btn ghost small" data-action="show-video-library">История видео</button></div>
      <div class="input-group"><label class="label">Семейство</label><div class="seg-chips">${providerOptions}</div></div>
      <div class="input-group" style="margin-top:12px;"><label class="label">Модель</label><div class="seg-chips">${modelOptions}</div></div>
      ${Object.keys(videoModelConfig().modes).length > 1 ? `<div class="input-group" style="margin-top:12px;"><label class="label">Режим</label><select id="video_mode">${Object.entries(videoModelConfig().modes).map(([id, mode]) => `<option value="${id}" ${state.video.mode === id ? 'selected' : ''}>${escapeHtml(mode.name)}</option>`).join('')}</select></div>` : ''}
    </div>
    ${renderVideoModeFields(videoModelConfig())}
    <div class="inspector-card">
      <button class="btn primary full video-run-btn ${state.video.isGenerating ? 'loading' : ''}" id="videoRunPrimaryBtn" data-action="run-video" ${state.video.isGenerating ? 'disabled' : ''}>${escapeHtml(videoRunButtonLabel())}</button>
      <div class="help-text" style="margin-top:10px;">${escapeHtml(getVideoRunCost().helper || 'Стоимость генерации пересчитывается прямо в кнопке.')}</div>
    </div>
  `;
}

function renderVideoModeFields(model) {
  const parts = [];
  const mode = state.video.mode;

  const addUpload = (label, id, hint, multiple = false, accept = 'image/*') => {
    parts.push(sectionUpload(label, id, hint, multiple, accept));
  };
  const addFields = (html) => parts.push(`<div class="inspector-card"><div class="field-grid two">${html}</div></div>`);
  const addPrompt = () => parts.push(sectionTextarea('Prompt', 'video_prompt', state.video.prompt, 'Опиши сцену, действие, камеру, свет и ожидаемый результат.'));

  if (state.video.model === 'motion-control') {
    addUpload('Avatar photo', 'video_avatarImage', 'Фото персонажа, который должен повторять движение.');
    addUpload('Motion video', 'video_motionVideo', 'Референс-видео с движением.', false, 'video/*');
    addPrompt();
    addFields(`${fieldSelect('Quality', 'video_quality', state.video.quality, [['standard','Standard'],['pro','Pro']])}`);
    return parts.join('');
  }

  if (['image_to_video', 'multi_shot'].includes(mode)) addUpload('Start frame', 'video_startFrame', 'Стартовый кадр для генерации.');
  if (state.video.provider === 'kling' && ['image_to_video', 'multi_shot'].includes(mode) && ['kling-2.5', 'kling-3.0'].includes(state.video.model)) addUpload('End frame', 'video_endFrame', 'Финальный кадр, если нужен переход или финальная поза.');
  if (state.video.provider === 'veo' && state.video.model === 'veo-3.1-pro' && mode === 'image_to_video') addUpload('Last frame', 'video_lastFrame', 'Финальный кадр для Veo 3.1.');
  if (state.video.provider === 'veo' && state.video.model === 'veo-3.1-pro' && mode === 'image_to_video') addUpload('Reference images', 'video_referenceImages', 'До 3 референсов.', true, 'image/*');
  if (state.video.provider === 'seedance' && mode === 'image_to_video') addUpload('Reference images', 'video_referenceImages', 'До 9 референсов.', true, 'image/*');

  addPrompt();

  if (state.video.model === 'kling-1.6') {
    addFields(`${fieldSelect('Duration', 'video_durationLegacy', state.video.duration || '5', [['5','5 sec'],['10','10 sec']])}${fieldSelect('Quality', 'video_quality', state.video.quality, [['standard','Standard'],['pro','Pro']])}`);
    return parts.join('');
  }
  if (['kling-2.5', 'kling-3.0'].includes(state.video.model)) {
    const durationOptions = state.video.model === 'kling-2.5' ? [['5','5 sec'],['10','10 sec']] : [['3','3 sec'],['5','5 sec'],['10','10 sec'],['15','15 sec']];
    addFields(`${fieldSelect('Duration', 'video_duration', state.video.duration, durationOptions)}${state.video.model === 'kling-3.0' ? fieldSelect('Resolution', 'video_resolution', state.video.resolution, [['720','720p'],['1080','1080p']]) : ''}${fieldSelect('Aspect ratio', 'video_aspectRatio', state.video.aspectRatio, [['16:9','16:9'],['9:16','9:16'],['1:1','1:1']])}${state.video.model === 'kling-3.0' ? fieldToggle('Enable audio', 'video_enableAudio', state.video.enableAudio, 'Звук увеличивает стоимость на 1 токен/сек.') : ''}`);
    return parts.join('');
  }
  if (state.video.provider === 'veo') {
    addFields(`${fieldSelect('Duration', 'video_durationVeo', state.video.duration || '8', [['4','4 sec'],['6','6 sec'],['8','8 sec']])}${fieldSelect('Aspect ratio', 'video_aspectRatioVeo', state.video.aspectRatio || '16:9', [['16:9','16:9'],['9:16','9:16']])}${fieldToggle('Generate audio', 'video_generateAudio', state.video.enableAudio, 'Включает генерацию звука.')}`);
    return parts.join('');
  }
  if (state.video.provider === 'seedance') {
    addFields(`${fieldSelect('Duration', 'video_durationSeedance', state.video.duration || '5', [['5','5 sec'],['10','10 sec'],['15','15 sec']])}${fieldSelect('Aspect ratio', 'video_aspectRatioSeedance', state.video.aspectRatio || '16:9', [['16:9','16:9'],['9:16','9:16'],['4:3','4:3'],['3:4','3:4']])}`);
    return parts.join('');
  }
  if (state.video.provider === 'sora') {
    addFields(`${fieldSelect('Duration', 'video_durationSora', state.video.duration || '4', [['4','4 sec'],['8','8 sec'],['12','12 sec']])}${fieldSelect('Aspect ratio', 'video_aspectRatioSora', state.video.aspectRatio || '16:9', [['16:9','16:9'],['9:16','9:16']])}`);
  }
  return parts.join('');
}

function renderHistoryInspector() {
  return `<div class="inspector-card"><div class="section-title">Библиотека</div><div class="help-text">Открой Video Studio и используй правую панель для библиотеки видео.</div></div>`;
}


function renderBillingInspector() {
  return `
    <div class="inspector-card">
      <div class="section-title">Billing controls</div>
      <div class="help-text">Баланс уже можно читать. Для продакшена сюда добавятся пакеты токенов, история hold/refund, пополнение и тарифные планы.</div>
      <button class="btn secondary full" data-action="load-balance" style="margin-top:12px;">Обновить баланс</button>
    </div>
  `;
}

function renderProfileInspector() {
  return `
    <div class="inspector-card">
      <div class="section-title">Profile controls</div>
      <div class="help-text">Здесь позже появятся Telegram Login, настройки аккаунта, профиль пользователя и связка сайта с ботом.</div>
    </div>
  `;
}

function sectionTextarea(label, id, value, placeholder = '') {
  return `
    <div class="inspector-card">
      <div class="input-group">
        <label class="label">${escapeHtml(label)}</label>
        <textarea id="${id}" placeholder="${escapeHtml(placeholder)}">${escapeHtml(value || '')}</textarea>
      </div>
    </div>
  `;
}

function sectionUpload(label, id, help, multiple = false, accept = 'image/*') {
  const key = id.replace(/_/g, '.');
  const asset = getFile(key);
  const hint = asset ? (Array.isArray(asset) ? `${asset.length} файлов выбрано` : asset.name) : 'Файл ещё не выбран';
  return `
    <div class="inspector-card">
      <div class="input-group">
        <label class="label">${escapeHtml(label)}</label>
        <input id="${id}" type="file" ${multiple ? 'multiple' : ''} accept="${escapeHtml(accept)}">
        <div class="help-text">${escapeHtml(help)}<br><span class="kbd">${escapeHtml(hint)}</span></div>
      </div>
    </div>
  `;
}

function fieldSelect(label, id, value, options) {
  return `
    <div class="input-group">
      <label class="label">${escapeHtml(label)}</label>
      <select id="${id}">
        ${options.map(([v, l]) => `<option value="${escapeHtml(v)}" ${String(value) === String(v) ? 'selected' : ''}>${escapeHtml(l)}</option>`).join('')}
      </select>
    </div>
  `;
}

function fieldInput(label, id, value) {
  return `
    <div class="input-group">
      <label class="label">${escapeHtml(label)}</label>
      <input id="${id}" value="${escapeHtml(value || '')}">
    </div>
  `;
}

function fieldToggle(label, id, checked, help) {
  return `
    <div class="toggle-row">
      <div>
        <strong>${escapeHtml(label)}</strong>
        <div class="help-text">${escapeHtml(help)}</div>
      </div>
      <label class="switch"><input id="${id}" type="checkbox" ${checked ? 'checked' : ''}><span></span></label>
    </div>
  `;
}


const CUSTOM_SELECT_STYLE_ID = 'astrabot-custom-select-styles';

function ensureCustomSelectStyles() {
  if (document.getElementById(CUSTOM_SELECT_STYLE_ID)) return;
  const style = document.createElement('style');
  style.id = CUSTOM_SELECT_STYLE_ID;
  style.textContent = `
    .native-select-hidden {
      position: absolute !important;
      opacity: 0 !important;
      pointer-events: none !important;
      width: 0 !important;
      height: 0 !important;
      margin: 0 !important;
      padding: 0 !important;
      border: 0 !important;
    }
    .ab-select {
      position: relative;
      width: 100%;
    }
    .ab-select-trigger {
      width: 100%;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 14px 16px;
      border-radius: 16px;
      border: 1px solid rgba(255,255,255,0.08);
      background: rgba(255,255,255,0.04);
      color: var(--text);
      text-align: left;
      font: inherit;
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.02);
      transition: border-color .18s ease, background .18s ease, transform .18s ease, box-shadow .18s ease;
    }
    .ab-select:hover .ab-select-trigger,
    .ab-select.is-open .ab-select-trigger,
    .ab-select-trigger:focus-visible {
      border-color: rgba(124,92,255,0.55);
      background: rgba(255,255,255,0.055);
      box-shadow: 0 0 0 3px rgba(124,92,255,0.14);
      outline: none;
    }
    .ab-select-trigger.is-disabled {
      opacity: .55;
      cursor: not-allowed;
    }
    .ab-select-label {
      flex: 1;
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .ab-select-caret {
      width: 10px;
      height: 10px;
      flex: 0 0 auto;
      border-right: 2px solid rgba(255,255,255,0.72);
      border-bottom: 2px solid rgba(255,255,255,0.72);
      transform: rotate(45deg) translateY(-1px);
      transition: transform .18s ease;
      margin-right: 4px;
    }
    .ab-select.is-open .ab-select-caret {
      transform: rotate(-135deg) translateY(-1px);
    }
    .ab-select-menu {
      position: absolute;
      top: calc(100% + 8px);
      left: 0;
      right: 0;
      z-index: 70;
      padding: 8px;
      border-radius: 18px;
      border: 1px solid rgba(255,255,255,0.08);
      background: rgba(10,14,25,0.98);
      box-shadow: 0 26px 60px rgba(2,6,23,0.58);
      display: none;
      max-height: 280px;
      overflow: auto;
      backdrop-filter: blur(18px);
    }
    .ab-select.is-open .ab-select-menu {
      display: grid;
      gap: 6px;
    }
    .ab-select-option {
      width: 100%;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      border: 1px solid transparent;
      border-radius: 14px;
      background: transparent;
      color: var(--text);
      font: inherit;
      text-align: left;
      padding: 11px 12px;
      transition: background .18s ease, border-color .18s ease, transform .18s ease, opacity .18s ease;
    }
    .ab-select-option:hover,
    .ab-select-option:focus-visible {
      background: rgba(255,255,255,0.06);
      border-color: rgba(124,92,255,0.32);
      outline: none;
    }
    .ab-select-option.is-selected {
      background: linear-gradient(135deg, rgba(124,92,255,0.24), rgba(91,124,255,0.16));
      border-color: rgba(124,92,255,0.42);
    }
    .ab-select-option.is-disabled {
      opacity: .42;
      cursor: not-allowed;
    }
    .ab-select-check {
      font-size: 12px;
      color: #c4b5fd;
      opacity: .95;
    }
  `;
  document.head.appendChild(style);
}

function closeCustomSelects(except = null) {
  document.querySelectorAll('.ab-select.is-open').forEach((node) => {
    if (except && node === except) return;
    node.classList.remove('is-open');
  });
}

function buildCustomSelect(select) {
  if (!select || select.dataset.customized === 'true') return;
  select.dataset.customized = 'true';
  select.classList.add('native-select-hidden');

  const wrapper = document.createElement('div');
  wrapper.className = 'ab-select';

  const trigger = document.createElement('button');
  trigger.type = 'button';
  trigger.className = 'ab-select-trigger';

  const label = document.createElement('span');
  label.className = 'ab-select-label';

  const caret = document.createElement('span');
  caret.className = 'ab-select-caret';

  trigger.append(label, caret);

  const menu = document.createElement('div');
  menu.className = 'ab-select-menu';

  const syncFromSelect = () => {
    const selectedOption = select.options[select.selectedIndex] || select.options[0];
    label.textContent = selectedOption ? selectedOption.textContent : 'Выбери значение';
    trigger.classList.toggle('is-disabled', !!select.disabled);
    wrapper.querySelectorAll('.ab-select-option').forEach((optionEl) => {
      const isSelected = optionEl.dataset.value === String(select.value);
      optionEl.classList.toggle('is-selected', isSelected);
      const check = optionEl.querySelector('.ab-select-check');
      if (check) check.textContent = isSelected ? '✓' : '';
    });
  };

  [...select.options].forEach((option) => {
    const optionBtn = document.createElement('button');
    optionBtn.type = 'button';
    optionBtn.className = 'ab-select-option';
    optionBtn.dataset.value = option.value;
    optionBtn.disabled = option.disabled;
    optionBtn.classList.toggle('is-disabled', option.disabled);

    const textNode = document.createElement('span');
    textNode.textContent = option.textContent;

    const checkNode = document.createElement('span');
    checkNode.className = 'ab-select-check';

    optionBtn.append(textNode, checkNode);

    optionBtn.addEventListener('click', () => {
      if (option.disabled || select.disabled) return;
      if (select.value !== option.value) {
        select.value = option.value;
        select.dispatchEvent(new Event('change', { bubbles: true }));
        select.dispatchEvent(new Event('input', { bubbles: true }));
      }
      syncFromSelect();
      wrapper.classList.remove('is-open');
    });

    menu.appendChild(optionBtn);
  });

  trigger.addEventListener('click', () => {
    if (select.disabled) return;
    const willOpen = !wrapper.classList.contains('is-open');
    closeCustomSelects(wrapper);
    wrapper.classList.toggle('is-open', willOpen);
  });

  trigger.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      wrapper.classList.remove('is-open');
      return;
    }
    if (e.key === 'Enter' || e.key === ' ' || e.key === 'ArrowDown') {
      e.preventDefault();
      if (!select.disabled) {
        closeCustomSelects(wrapper);
        wrapper.classList.add('is-open');
      }
    }
  });

  select.addEventListener('change', syncFromSelect);

  select.insertAdjacentElement('afterend', wrapper);
  wrapper.append(trigger, menu);
  syncFromSelect();
}

function enhanceCustomSelects() {
  ensureCustomSelectStyles();
  document.querySelectorAll('select').forEach(buildCustomSelect);
}

async function checkApi() {
  try {
    const workspaceRes = await apiFetch('/api/workspace/health');
    const data = await workspaceRes.json();
    state.apiOnline = !!data.ok;
    toast('success', 'API подключен', 'Workspace router отвечает.');
  } catch (e) {
    try {
      const fallback = await apiFetch('/health');
      await fallback.json();
      state.apiOnline = true;
      toast('info', 'Backend жив', 'Основной backend отвечает, но workspace router ещё не подключен.');
    } catch (err) {
      state.apiOnline = false;
      toast('error', 'API недоступен', String(err.message || err));
    }
  }
  render();
}

async function loadBootstrap() {
  try {
    const res = await apiFetch('/api/workspace/bootstrap');
    const data = await res.json();
    if (Array.isArray(data.chat_models) && data.chat_models.length) state.bootstrap.chatModels = data.chat_models;
    if (Array.isArray(data.live_integrations)) state.bootstrap.liveIntegrations = data.live_integrations;
    if (data.user) state.me = data.user;
    if (typeof data.balance_tokens !== 'undefined') state.balance = Number(data.balance_tokens || 0);
  } catch (e) {
    // silent fallback to defaults
  }
}

async function loadBalance() {
  if (!requireAuth()) return;
  try {
    const res = await apiFetch('/api/workspace/balance');
    const data = await res.json();
    state.balance = Number(data.balance_tokens || 0);
    document.getElementById('balanceHint').textContent = 'данные из backend';
    toast('success', 'Баланс обновлён', `Текущий баланс: ${state.balance} ток.`);
    render();
  } catch (e) {
    toast('error', 'Не удалось получить баланс', String(e.message || e));
  }
}

async function loadVoices() {
  try {
    const res = await apiFetch('/api/workspace/tts/voices');
    const data = await res.json();
    state.voice.voices = Array.isArray(data) ? data : [];
    if (!state.voice.voiceId && state.voice.voices[0]) state.voice.voiceId = state.voice.voices[0].voice_id;
    render();
  } catch (e) {
    toast('error', 'Не удалось загрузить voices', String(e.message || e));
  }
}

async function loadPromptCategories() {
  state.prompts.loading = true;
  render();
  try {
    const res = await apiFetch('/api/workspace/prompts/categories');
    const data = await res.json();
    state.prompts.categories = data.items || [];
    if (!state.prompts.selectedCategory && state.prompts.categories[0]) {
      state.prompts.selectedCategory = state.prompts.categories[0].slug;
      await loadPromptGroups(state.prompts.selectedCategory);
    }
    render();
  } catch (e) {
    toast('error', 'Не удалось загрузить категории', String(e.message || e));
  } finally {
    state.prompts.loading = false;
  }
}

async function loadPromptGroups(category) {
  state.prompts.selectedCategory = category;
  state.prompts.selectedGroupId = '';
  state.prompts.items = [];
  try {
    const res = await apiFetch(`/api/workspace/prompts/groups?category=${encodeURIComponent(category)}`);
    const data = await res.json();
    state.prompts.groups = data.items || [];
    if (state.prompts.groups[0]) {
      state.prompts.selectedGroupId = state.prompts.groups[0].id;
      await loadPromptItems(state.prompts.selectedGroupId);
    }
    render();
  } catch (e) {
    toast('error', 'Не удалось загрузить группы', String(e.message || e));
  }
}

async function loadPromptItems(groupId) {
  state.prompts.selectedGroupId = groupId;
  try {
    const res = await apiFetch(`/api/workspace/prompts/items?group_id=${encodeURIComponent(groupId)}`);
    const data = await res.json();
    state.prompts.items = data.items || [];
    render();
  } catch (e) {
    toast('error', 'Не удалось загрузить элементы', String(e.message || e));
  }
}


async function sendChat() {
  ensureChatModeCompatibility();
  const outgoing = state.chat.input.trim();
  const attachments = getChatAttachments();
  if (!outgoing && !attachments.length) {
    toast('error', 'Пустое сообщение', 'Введите текст в чат или прикрепите файл.');
    return;
  }

  const filePreview = attachments.length
    ? `📎 Файлы: ${attachments.map((item) => item.name).join(', ')}`
    : '';
  const userMessage = [outgoing, filePreview].filter(Boolean).join('\n\n') || filePreview;

  state.chat.messages.push({ role: 'user', content: userMessage });
  state.chat.input = '';
  render();
  scrollChatToBottom();
  saveState();

  try {
    const history = state.chat.messages.filter((m) => m.role === 'user' || m.role === 'assistant').slice(-12);
    let res;

    if (attachments.length) {
      const form = new FormData();
      form.append('text', outgoing);
      form.append('history', JSON.stringify(history));
      form.append('model', state.chat.model);
      form.append('mode', state.chat.mode);
      form.append('temperature', String(state.chat.temperature));
      form.append('max_tokens', String(state.chat.maxTokens));
      attachments.forEach((item) => {
        if (item?.file) form.append('files', item.file, item.name || item.file.name || 'file');
      });
      res = await apiFetch('/api/workspace/chat', {
        method: 'POST',
        body: form,
      });
    } else {
      res = await apiFetch('/api/workspace/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          text: outgoing,
          history,
          model: state.chat.model,
          mode: state.chat.mode,
          temperature: state.chat.temperature,
          max_tokens: state.chat.maxTokens,
        }),
      });
    }

    const data = await res.json();
    state.chat.messages.push({ role: 'assistant', content: data.answer || 'Пустой ответ.', isPrompt: data.is_prompt !== false });
    pushRun({ studio: 'ChatGPT', title: `Chat · ${state.chat.mode === 'prompt_builder' ? 'Prompt Builder' : 'Chat'}`, summary: (outgoing || filePreview).slice(0, 100) });
    clearChatAttachments();
    render();
    scrollChatToBottom();
    saveState();
  } catch (e) {
    state.chat.input = outgoing;
    state.chat.messages.push({ role: 'system', content: `Ошибка: ${String(e.message || e)}`, isPrompt: false });
    render();
    scrollChatToBottom();
    saveState();
  }
}




async function loadVideoHistory(options = {}) {
  const { silent = false, selectId = '', keepSelection = true } = options;
  if (!state.authToken) {
    state.history.items = [];
    state.history.selectedId = '';
    state.history.selectedItem = null;
    state.history.loaded = false;
    state.history.loading = false;
    state.history.lastError = '';
    if (!silent) render();
    return [];
  }

  state.history.loading = true;
  state.history.lastError = '';
  if (!silent) render();

  try {
    const qs = new URLSearchParams({ limit: String(state.history.limit || 24), offset: String(state.history.offset || 0) });
    const res = await apiFetch(`/api/workspace/history?${qs.toString()}`);
    const data = await res.json();
    const items = Array.isArray(data.items) ? data.items : [];
    state.history.items = items;
    state.history.loaded = true;

    const preferredId = String(selectId || '').trim() || (keepSelection ? String(state.history.selectedId || '').trim() : '');
    if (preferredId && items.some((item) => item.id === preferredId)) {
      state.history.selectedId = preferredId;
      state.history.selectedItem = items.find((item) => item.id === preferredId) || null;
    } else if (!keepSelection) {
      state.history.selectedId = '';
      state.history.selectedItem = null;
    }

    if (!silent) render();
    return items;
  } catch (e) {
    state.history.lastError = String(e.message || e);
    if (!silent) {
      render();
      toast('error', 'Не удалось загрузить историю', state.history.lastError);
    }
    return [];
  } finally {
    state.history.loading = false;
    if (!silent) render();
  }
}

async function loadHistoryItem(generationId, options = {}) {
  const { silent = false, switchStudio = false } = options;
  const generationIdText = String(generationId || '').trim();
  if (!generationIdText) return null;
  if (!state.authToken) {
    if (!silent) toast('error', 'Нужна авторизация', 'Сначала войди через Telegram, чтобы открыть историю.');
    return null;
  }

  try {
    const res = await apiFetch(`/api/workspace/history/${encodeURIComponent(generationIdText)}`);
    const data = await res.json();
    const item = data.item || null;
    if (!item) throw new Error('Пустой ответ истории');

    state.history.selectedId = item.id || generationIdText;
    state.history.selectedItem = item;
    const idx = state.history.items.findIndex((entry) => entry.id === item.id);
    if (idx >= 0) state.history.items[idx] = { ...state.history.items[idx], ...item };
    else state.history.items.unshift(item);

    if (switchStudio) state.studio = 'history';
    saveState();
    render();
    return item;
  } catch (e) {
    if (!silent) toast('error', 'Не удалось открыть видео', String(e.message || e));
    return null;
  }
}

function applyHistoryItemToVideoWorkspace(item) {
  const selected = item || historySelectedItem();
  if (!selected) {
    toast('info', 'История пуста', 'Сначала дождись хотя бы одного сохранённого видео.');
    return;
  }
  const videoUrl = historyVideoUrl(selected);
  if (!videoUrl) {
    toast('error', 'Нет ссылки на видео', 'Для этого ролика ещё не найден доступный файл.');
    return;
  }
  stopVideoPolling();
  state.video.generationId = selected.id || '';
  state.video.providerTaskId = selected.task_id || '';
  state.video.prompt = selected.prompt || state.video.prompt;
  state.video.outputUrl = videoUrl;
  state.video.downloadUrl = historyVideoDownloadUrl(selected) || videoUrl;
  state.video.coverUrl = selected.thumbnail_url || '';
  state.video.percent = selected.status === 'completed' ? 100 : null;
  state.video.lastStatus = String(selected.status || 'completed').toLowerCase();
  state.video.errorText = selected.error_message || '';
  state.video.statusText = selected.has_storage_file ? 'Открыт сохранённый ролик из библиотеки AstraBot.' : 'Открыт ролик из истории провайдера.';
  state.video.panel = 'library';
  state.studio = 'video';
  saveState();
  render();
  toast('success', 'Видео открыто', 'Ролик возвращён в рабочую зону.');
}

function renderVideoHistoryShelf() {
  return '';
}

async function runVideo() {
  if (!requireAuth()) return;
  syncVideoSelection();
  if (state.video.isGenerating) return;
  if (!state.video.prompt.trim()) {
    toast('error', 'Нужен prompt', 'Введи prompt для генерации видео.');
    return;
  }
  const form = new FormData();
  form.append('provider', state.video.provider);
  form.append('model', state.video.model);
  form.append('mode', state.video.mode);
  form.append('prompt', state.video.prompt.trim());
  form.append('duration', String(state.video.duration || ''));
  form.append('resolution', String(state.video.resolution || ''));
  form.append('aspect_ratio', String(state.video.aspectRatio || ''));
  form.append('enable_audio', state.video.enableAudio ? '1' : '0');
  form.append('quality', String(state.video.quality || ''));

  const fileFields = {
    'video.startFrame': 'start_frame',
    'video.endFrame': 'end_frame',
    'video.lastFrame': 'last_frame',
    'video.avatarImage': 'avatar_image',
    'video.motionVideo': 'motion_video',
  };
  Object.entries(fileFields).forEach(([key, field]) => {
    const file = getFile(key);
    if (file?.file) form.append(field, file.file, file.name || file.file.name || field);
  });
  const refs = getFile('video.referenceImages');
  if (Array.isArray(refs)) refs.forEach((item) => item?.file && form.append('reference_images', item.file, item.name || item.file.name || 'ref.jpg'));

  state.video.isGenerating = true;
  state.video.outputUrl = '';
  state.video.downloadUrl = '';
  state.video.coverUrl = '';
  state.video.errorText = '';
  state.video.lastStatus = 'submitted';
  state.video.statusText = 'Задача отправлена. Видео появится в рабочей зоне автоматически.';
  render();

  try {
    const res = await apiFetch('/api/workspace/video/run', { method: 'POST', body: form });
    const data = await res.json();
    state.video.generationId = data.generation_id || '';
    state.video.providerTaskId = data.task_id || '';
    state.video.statusText = data.status_text || 'Генерация началась.';
    pushRun({ studio: 'Video', title: `${currentMeta().provider} · ${currentMeta().model}`, summary: state.video.prompt.slice(0, 120) });
    saveState();
    startVideoPolling({ immediate: true });
    toast('success', 'Запуск выполнен', data.status_text || 'Генерация началась.');
  } catch (e) {
    state.video.errorText = String(e.message || e);
    state.video.lastStatus = 'error';
    state.video.statusText = 'Не удалось запустить генерацию.';
    toast('error', 'Ошибка запуска', state.video.errorText);
  } finally {
    state.video.isGenerating = false;
    saveState();
    render();
  }
}

async function pollVideoTask(options = {}) {
  const { silent = false } = options;
  if (!state.video.generationId) return;
  try {
    const res = await apiFetch(`/api/workspace/history/${encodeURIComponent(state.video.generationId)}`);
    const data = await res.json();
    const item = data.item || null;
    if (!item) return;
    state.history.selectedId = item.id || state.history.selectedId;
    state.history.selectedItem = item;
    const idx = state.history.items.findIndex((entry) => entry.id === item.id);
    if (idx >= 0) state.history.items[idx] = { ...state.history.items[idx], ...item };
    else state.history.items.unshift(item);

    state.video.lastStatus = String(item.status || 'processing').toLowerCase();
    state.video.errorText = item.error_message || '';
    state.video.statusText = item.error_message || (['completed'].includes(state.video.lastStatus) ? 'Видео готово и загружено в рабочую зону.' : 'Нейросеть собирает видео. Ожидай финальный файл.');

    const readyUrl = historyVideoUrl(item);
    if (readyUrl && state.video.lastStatus === 'completed') {
      state.video.outputUrl = readyUrl;
      state.video.downloadUrl = historyVideoDownloadUrl(item) || readyUrl;
      state.video.percent = 100;
      stopVideoPolling();
      saveState();
      render();
      if (!silent) toast('success', 'Видео готово', 'Результат появился в рабочей зоне.');
      return;
    }

    if (isVideoTaskFailed(state.video.lastStatus)) {
      stopVideoPolling();
      saveState();
      render();
      if (!silent) toast('error', 'Ошибка генерации', state.video.errorText || 'Провайдер вернул ошибку.');
      return;
    }

    saveState();
    if (!silent) render();
  } catch (e) {
    if (!silent) toast('error', 'Не удалось проверить статус', String(e.message || e));
  }
}

async function runVoice() {

  if (!state.voice.text.trim()) {
    toast('error', 'Нужен текст', 'Введи текст для озвучки.');
    return;
  }
  if (!state.voice.voiceId) {
    toast('error', 'Нужен голос', 'Сначала загрузи и выбери voice.');
    return;
  }
  try {
    const res = await apiFetch('/api/workspace/tts/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text: state.voice.text,
        voice_id: state.voice.voiceId,
        model_id: state.voice.modelId,
        output_format: state.voice.outputFormat,
      }),
    });
    const blob = await res.blob();
    state.voice.audioUrl = URL.createObjectURL(blob);
    pushRun({ studio: 'Voice', title: 'TTS generate', summary: state.voice.text.slice(0, 100) });
    toast('success', 'Аудио готово', 'MP3 успешно сгенерирован.');
    render();
  } catch (e) {
    toast('error', 'TTS error', String(e.message || e));
  }
}

async function runSongwriter() {
  if (!state.music.text.trim()) {
    toast('error', 'Нужен текст', 'Опиши идею трека или вставь текст песни.');
    return;
  }
  try {
    const res = await apiFetch('/api/workspace/songwriter', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text: state.music.text,
        history: [],
        language: state.music.language,
        genre: state.music.tags,
        mood: state.music.mood,
        references: state.music.references,
      }),
    });
    const data = await res.json();
    state.music.songwriterAnswer = data.answer || '';
    pushRun({ studio: 'Music', title: 'Songwriter', summary: state.music.text.slice(0, 100) });
    toast('success', 'Songwriter ответил', 'Ответ добавлен в центр рабочей зоны.');
    render();
  } catch (e) {
    toast('error', 'Songwriter error', String(e.message || e));
  }
}

function seedDemo() {
  state.chat.messages = [
    { role: 'system', content: 'Добро пожаловать в AstraBot Workspace.' },
    { role: 'user', content: 'Сделай концепцию короткого рекламного ролика для танцевальной школы.' },
    { role: 'assistant', content: 'Можно построить ролик на контрасте: пустой зал → свет включается → динамика группы → call to action. Дальше отдельно выдам prompt для Kling и текст для озвучки.' },
  ];
  state.video.prompt = 'Dynamic cinematic commercial for a dance school, golden rim light, confident young dancers, sweeping camera movement, premium branded ending.';
  state.music.text = 'Нужна идея вдохновляющей песни для рекламы танцевальной школы.';
  state.workspaceNotes = '1) В ChatGPT собрать концепцию\n2) В Video Studio подготовить prompt для Kling 3\n3) В Voice Studio сделать voice-over\n4) В Music Studio подобрать идею трека';
  render();
  saveState();
  toast('success', 'Demo заполнен', 'Заполнил чат, видео-промпт и planning board примерами.');
}

function resetCurrentStudio() {
  switch (state.studio) {
    case 'chat':
      state.chat.input = '';
      state.chat.messages = [{ role: 'system', content: 'Новый чат. Задай задачу или попроси создать промпт.' }];
      clearChatAttachments();
      break;
    case 'video':
      clearVideoRunState({ keepPrompt: false });
      break;
    case 'image':
      state.image.prompt = '';
      state.image.outputUrl = '';
      break;
    case 'voice':
      state.voice.text = '';
      state.voice.audioUrl = '';
      break;
    case 'music':
      state.music.text = '';
      state.music.songwriterAnswer = '';
      break;
    case 'workspace':
      state.workspaceNotes = '';
      break;
  }
  render();
  saveState();
}

function handleInputChange(target) {
  const { id, value, type, checked, files } = target;
  if (!id) return;

  const fileMap = {
    chat_attachments: ['chat.attachments', true],
    video_startFrame: ['video.startFrame', false],
    video_endFrame: ['video.endFrame', false],
    video_lastFrame: ['video.lastFrame', false],
    video_referenceImages: ['video.referenceImages', true],
    video_avatarImage: ['video.avatarImage', false],
    video_motionVideo: ['video.motionVideo', false],
    video_sourceVideo: ['video.sourceVideo', false],
    image_sourceImage: ['image.sourceImage', false],
    image_baseImage: ['image.baseImage', false],
  };
  if (fileMap[id]) {
    const [key, multiple] = fileMap[id];
    setFile(key, multiple ? files : files[0], multiple);
    if (id === 'video_motionVideo') probeMotionDuration(getFile('video.motionVideo'));
    render();
    return;
  }

  const update = (obj, key, val) => { obj[key] = val; };

  switch (id) {
    case 'apiBaseUrl': state.apiBaseUrl = value; break;
    case 'chatInput': state.chat.input = value; break;
    case 'chat_model':
      state.chat.model = value;
      ensureChatModeCompatibility(true);
      break;
    case 'chat_temperature': state.chat.temperature = Number(value); break;
    case 'chat_maxTokens': state.chat.maxTokens = Number(value); break;

    case 'video_provider':
      state.video.provider = value;
      state.video.model = Object.keys(VIDEO_REGISTRY[value].models)[0];
      state.video.mode = Object.keys(VIDEO_REGISTRY[value].models[state.video.model].modes)[0];
      state.video.panel = 'params';
      break;
    case 'video_model':
      state.video.model = value;
      state.video.mode = Object.keys(VIDEO_REGISTRY[state.video.provider].models[value].modes)[0];
      state.video.panel = 'params';
      break;
    case 'video_mode': state.video.mode = value; break;
    case 'video_prompt': state.video.prompt = value; break;
    case 'video_duration':
    case 'video_durationLegacy':
    case 'video_durationVeo':
    case 'video_durationSeedance':
    case 'video_durationSora': state.video.duration = value; break;
    case 'video_resolution': state.video.resolution = value; break;
    case 'video_aspectRatio':
    case 'video_aspectRatioVeo':
    case 'video_aspectRatioSeedance':
    case 'video_aspectRatioSora': state.video.aspectRatio = value; break;
    case 'video_enableAudio':
    case 'video_generateAudio': state.video.enableAudio = checked; break;
    case 'video_quality': state.video.quality = value; break;

    case 'image_provider':
      state.image.provider = value;
      state.image.model = Object.keys(IMAGE_REGISTRY[value].models)[0];
      state.image.mode = Object.keys(IMAGE_REGISTRY[value].models[state.image.model].modes)[0];
      break;
    case 'image_model':
      state.image.model = value;
      state.image.mode = Object.keys(IMAGE_REGISTRY[state.image.provider].models[value].modes)[0];
      break;
    case 'image_mode': state.image.mode = value; break;
    case 'image_prompt': state.image.prompt = value; break;
    case 'image_resolution': state.image.resolution = value; break;
    case 'image_aspectRatio':
    case 'image_aspectRatioText': state.image.aspectRatio = value; break;
    case 'image_safetyLevel': state.image.safetyLevel = value; break;
    case 'image_posterStyle': state.image.posterStyle = value; break;
    case 'image_stylePreset': state.image.stylePreset = value; break;
    case 'image_moodPreset': state.image.moodPreset = value; break;

    case 'voice_voiceId': state.voice.voiceId = value; break;
    case 'voice_modelId': state.voice.modelId = value; break;
    case 'voice_outputFormat': state.voice.outputFormat = value; break;
    case 'voice_text': state.voice.text = value; break;

    case 'music_provider': state.music.provider = value; break;
    case 'music_mode': state.music.mode = value; break;
    case 'music_model': state.music.model = value; break;
    case 'music_language': state.music.language = value; break;
    case 'music_title': state.music.title = value; break;
    case 'music_tags': state.music.tags = value; break;
    case 'music_mood': state.music.mood = value; break;
    case 'music_references': state.music.references = value; break;
    case 'music_text': state.music.text = value; break;
    case 'workspaceNotes': state.workspaceNotes = value; break;
    default: return;
  }
  saveState();
  const structuralRerenderIds = new Set([
    'chat_mode',
    'video_provider', 'video_model', 'video_mode',
    'image_provider', 'image_model', 'image_mode',
    'music_provider', 'music_mode', 'music_model',
    'voice_voiceId', 'voice_modelId', 'voice_outputFormat'
  ]);
  if (structuralRerenderIds.has(id) || target.tagName === 'SELECT' || target.type === 'checkbox') {
    render();
  } else {
    renderHeader();
  }
}

function handleAction(action, dataset = {}) {
  switch (action) {
    case 'switch-studio': {
      const previousStudio = state.studio;
      state.studio = dataset.studio;
      if (state.studio === 'video' && previousStudio !== 'video') state.video.panel = 'params';
      if (state.studio === 'library' && !state.prompts.categories.length) loadPromptCategories();
      if (state.studio === 'voice' && !state.voice.voices.length) loadVoices();
      if (state.studio === 'history' && state.authToken) loadVideoHistory({ silent: true, keepSelection: true });
      if (state.studio === 'video' && state.video.panel === 'library' && state.authToken) loadVideoHistory({ silent: true, keepSelection: true });
      render();
      saveState();
      break;
    }
    case 'send-chat': sendChat(); break;
    case 'pick-chat-files': {
      const input = document.getElementById('chat_attachments');
      if (input) input.click();
      break;
    }
    case 'remove-chat-file':
      removeChatAttachment(Number(dataset.index || -1));
      render();
      break;
    case 'copy-chat-prompt': {
      const text = decodeURIComponent(dataset.text || '');
      navigator.clipboard.writeText(text).then(() => toast('success', 'Скопировано', 'Промпт скопирован в буфер обмена.')).catch(() => toast('error', 'Не удалось скопировать', 'Скопируй текст вручную.'));
      break;
    }
    case 'chat-quick':
      state.chat.input = dataset.prompt || '';
      render();
      break;
    case 'run-video': runVideo(); break;
    case 'poll-video-task': pollVideoTask(); break;
    case 'show-video-library':
      setVideoPanel('library');
      break;
    case 'show-video-params':
      setVideoPanel('params');
      break;
    case 'set-video-provider':
      state.video.provider = dataset.provider || 'kling';
      state.video.model = Object.keys(VIDEO_REGISTRY[state.video.provider].models)[0];
      state.video.mode = Object.keys(VIDEO_REGISTRY[state.video.provider].models[state.video.model].modes)[0];
      state.video.panel = 'params';
      saveState();
      render();
      break;
    case 'set-video-model':
      state.video.model = dataset.model || state.video.model;
      state.video.mode = Object.keys(VIDEO_REGISTRY[state.video.provider].models[state.video.model].modes)[0];
      state.video.panel = 'params';
      saveState();
      render();
      break;
    case 'clear-video-run': clearVideoRunState({ keepPrompt: true }); render(); break;
    case 'refresh-history': loadVideoHistory(); break;
    case 'preview-history-item':
      loadHistoryItem(dataset.generationId, { switchStudio: state.studio === 'history' }).then((item) => {
        if (item && state.studio === 'video') {
          state.history.selectedId = item.id || state.history.selectedId;
          state.history.selectedItem = item;
          state.video.outputUrl = historyVideoUrl(item) || state.video.outputUrl;
          state.video.downloadUrl = historyVideoDownloadUrl(item) || state.video.downloadUrl;
          render();
        }
      });
      break;
    case 'use-history-item':
      loadHistoryItem(dataset.generationId, { silent: false }).then((item) => {
        if (item) applyHistoryItemToVideoWorkspace(item);
      });
      break;
    case 'run-image': toast('info', 'Image Studio готова архитектурно', 'Для запуска осталось вынести web-friendly image endpoints из backend.'); break;
    case 'load-voices': loadVoices(); break;
    case 'run-voice': runVoice(); break;
    case 'run-songwriter': runSongwriter(); break;
    case 'send-music-to-chat':
      state.studio = 'chat';
      state.chat.input = `Помоги доработать идею песни. Черновик: ${state.music.text}`;
      render();
      break;
    case 'refresh-prompts': loadPromptCategories(); break;
    case 'select-category': loadPromptGroups(dataset.category); break;
    case 'select-group': loadPromptItems(dataset.groupId); break;
    case 'copy-prompt': {
      const prompt = decodeURIComponent(dataset.prompt || '');
      navigator.clipboard.writeText(prompt).then(() => toast('success', 'Скопировано', 'Prompt скопирован в буфер обмена.')).catch(() => toast('error', 'Не удалось скопировать', 'Скопируй текст вручную.'));
      break;
    }
    case 'send-prompt-to-chat': {
      state.studio = 'chat';
      state.chat.input = decodeURIComponent(dataset.prompt || '');
      render();
      saveState();
      break;
    }
    case 'goto-chat': state.studio = 'chat'; render(); break;
    case 'save-planning': saveState(); toast('success', 'Сохранено', 'Workspace notes сохранены в браузере.'); break;
    case 'seed-plan':
      state.workspaceNotes = 'Проект: рекламный ролик для школы танцев\n\n1. ChatGPT — собрать концепцию и CTA\n2. Video — Kling 3 / prompt / start frame\n3. Voice — озвучка текста\n4. Music — idea + songwriter\n5. Финальный экспорт';
      render();
      saveState();
      break;
    case 'clear-runs':
    case 'clear-runs-sidebar':
      state.recentRuns = [];
      saveState();
      render();
      break;
    case 'load-balance': loadBalance(); break;
    default:
      break;
  }
}

function runCurrentStudio() {
  switch (state.studio) {
    case 'chat': sendChat(); break;
    case 'video': runVideo(); break;
    case 'image': toast('info', 'Image Studio', 'Осталось подключить backend-эндпоинты для image flows.'); break;
    case 'voice': runVoice(); break;
    case 'music': runSongwriter(); break;
    case 'library': loadPromptCategories(); break;
    case 'workspace': saveState(); toast('success', 'Workspace сохранён', 'Заметки сохранены локально.'); break;
    case 'billing': loadBalance(); break;
    default: toast('info', 'Нет действия', 'Для этой студии глобальная кнопка пока не назначена.');
  }
}

function render() {
  ensureChatModeCompatibility();
  renderNav();
  renderHeader();
  renderRecentRuns();
  renderWorkspace();
  renderInspector();
  enhanceCustomSelects();
}

document.addEventListener('click', (e) => {
  const clickedInsideCustomSelect = e.target.closest('.ab-select');
  if (!clickedInsideCustomSelect) closeCustomSelects();
  const btn = e.target.closest('[data-action]');
  if (btn) {
    handleAction(btn.dataset.action, btn.dataset);
    return;
  }
  if (e.target.id === 'saveSettingsBtn') {
    state.apiBaseUrl = document.getElementById('apiBaseUrl').value.trim();
    saveState();
    renderHeader();
    toast('success', 'Настройки сохранены', 'API Base URL сохранён.');
    return;
  }
  if (e.target.id === 'checkApiBtn') { checkApi(); return; }
  if (e.target.id === 'loadBalanceBtn') { loadBalance(); return; }
  if (e.target.id === 'logoutBtn') { logoutWorkspace(); return; }
  if (e.target.id === 'clearRunsBtn') { state.recentRuns = []; saveState(); renderRecentRuns(); renderWorkspace(); return; }
  if (e.target.id === 'seedDemoBtn') { seedDemo(); return; }
  if (e.target.id === 'globalRunBtn') { runCurrentStudio(); return; }
  if (e.target.id === 'resetStudioBtn') { resetCurrentStudio(); return; }
});

document.addEventListener('input', (e) => handleInputChange(e.target));
document.addEventListener('change', (e) => handleInputChange(e.target));
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeCustomSelects();
  if (state.studio === 'chat' && e.target.id === 'chatInput' && e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    sendChat();
  }
});

async function init() {
  render();
  await loadBootstrap();
  await checkApi();
  if (state.authToken) {
    await loadMe();
    loadVideoHistory({ silent: true }).catch(() => {});
  }
  if (state.voice.voices.length === 0) loadVoices();
  if (state.studio === 'library' || state.prompts.categories.length === 0) loadPromptCategories();
  if (state.video.providerTaskId && !state.video.outputUrl && !isVideoTaskFailed(state.video.lastStatus)) {
    startVideoPolling({ immediate: true });
  }
  render();
}

init();
