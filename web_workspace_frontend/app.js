
const DEFAULT_API_BASE = localStorage.getItem('astrabot:apiBaseUrl') || 'https://astrabot-tchj.onrender.com';
const DEFAULT_AUTH_TOKEN = localStorage.getItem('astrabot:authToken') || '';
const DEFAULT_ME = JSON.parse(localStorage.getItem('astrabot:me') || 'null');
const DEFAULT_VIDEO_STATE = JSON.parse(localStorage.getItem('astrabot:videoState') || '{}');
const DEFAULT_IMAGE_STATE = JSON.parse(localStorage.getItem('astrabot:imageState') || '{}');
const DEFAULT_VOICE_STATE = JSON.parse(localStorage.getItem('astrabot:voiceState') || '{}');
const DEFAULT_VOICE_HISTORY_STATE = JSON.parse(localStorage.getItem('astrabot:voiceHistoryState') || '{}');
const DEFAULT_MUSIC_STATE = JSON.parse(localStorage.getItem('astrabot:musicState') || '{}');
const DEFAULT_MUSIC_HISTORY_STATE = JSON.parse(localStorage.getItem('astrabot:musicHistoryState') || '{}');
const DEFAULT_VIDEO_EDITOR_STATE = JSON.parse(localStorage.getItem('astrabot:videoEditorState') || 'null');

const runtime = {
  files: {},
  lastChatBootstrapLoaded: false,
  videoPollTimer: null,
  videoEditPollTimer: null,
  musicPollTimer: null,
  showcaseMediaObserver: null,
};

const DEFAULT_APP_VIEW = localStorage.getItem('astrabot:view') || (window.location.hash === '#workspace' ? 'workspace' : 'showcase');

const FILE_INPUT_MAP = {
  chat_attachments: { key: 'chat.attachments', multiple: true },
  video_startFrame: { key: 'video.startFrame', multiple: false },
  video_endFrame: { key: 'video.endFrame', multiple: false },
  video_lastFrame: { key: 'video.lastFrame', multiple: false },
  video_referenceImages: { key: 'video.referenceImages', multiple: true },
  video_avatarImage: { key: 'video.avatarImage', multiple: false },
  video_motionVideo: { key: 'video.motionVideo', multiple: false },
  video_sourceVideo: { key: 'video.sourceVideo', multiple: false },
  editorAudioUpload: { key: 'editor.audioUpload', multiple: false },
  editorMergeUpload: { key: 'editor.mergeUpload', multiple: false },
  image_sourceImage: { key: 'image.sourceImage', multiple: false },
  image_baseImage: { key: 'image.baseImage', multiple: false },
};

function makeRuntimeFileEntry(file) {
  return { file, name: file.name, url: URL.createObjectURL(file), type: file.type || '', size: file.size || 0, lastModified: file.lastModified || 0 };
}

const state = {
  apiBaseUrl: DEFAULT_API_BASE,
  authToken: DEFAULT_AUTH_TOKEN,
  me: DEFAULT_ME,
  balance: null,
  apiOnline: false,
  view: DEFAULT_APP_VIEW,
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

  videoEditor: normalizeVideoEditorState(DEFAULT_VIDEO_EDITOR_STATE),

  image: {
    provider: DEFAULT_IMAGE_STATE.provider || 'nano_banana_pro',
    model: DEFAULT_IMAGE_STATE.model || 'nano-banana-pro',
    mode: DEFAULT_IMAGE_STATE.mode || 'image_to_image',
    prompt: DEFAULT_IMAGE_STATE.prompt || '',
    aspectRatio: DEFAULT_IMAGE_STATE.aspectRatio || 'match_input_image',
    resolution: DEFAULT_IMAGE_STATE.resolution || '2K',
    safetyLevel: DEFAULT_IMAGE_STATE.safetyLevel || 'high',
    posterStyle: DEFAULT_IMAGE_STATE.posterStyle || 'cinematic',
    stylePreset: DEFAULT_IMAGE_STATE.stylePreset || 'editorial',
    moodPreset: DEFAULT_IMAGE_STATE.moodPreset || 'premium',
    upscalePreset: DEFAULT_IMAGE_STATE.upscalePreset || 'standard',
    outputUrl: '',
    downloadUrl: '',
    beforeImageUrl: DEFAULT_IMAGE_STATE.beforeImageUrl || '',
    afterImageUrl: DEFAULT_IMAGE_STATE.afterImageUrl || '',
    compareMode: !!DEFAULT_IMAGE_STATE.compareMode,
    comparePosition: Number.isFinite(Number(DEFAULT_IMAGE_STATE.comparePosition)) ? Number(DEFAULT_IMAGE_STATE.comparePosition) : 50,
    generationId: '',
    panel: DEFAULT_IMAGE_STATE.panel === 'library' ? 'library' : 'params',
    isGenerating: false,
    errorText: '',
    statusText: 'Выбери режим, добавь изображения при необходимости и запусти генерацию.',
  },
  imageHistory: {
    items: [],
    loading: false,
    loaded: false,
    selectedId: '',
    selectedItem: null,
    lastError: '',
    limit: 24,
    offset: 0,
  },
  voice: {
    voiceId: DEFAULT_VOICE_STATE.voiceId || '',
    modelId: DEFAULT_VOICE_STATE.modelId || 'eleven_multilingual_v2',
    outputFormat: DEFAULT_VOICE_STATE.outputFormat || 'mp3_44100_128',
    languageCode: DEFAULT_VOICE_STATE.languageCode || 'ru',
    manualVoiceSettings: !!DEFAULT_VOICE_STATE.manualVoiceSettings,
    stability: Number.isFinite(Number(DEFAULT_VOICE_STATE.stability)) ? Number(DEFAULT_VOICE_STATE.stability) : 0.5,
    similarityBoost: Number.isFinite(Number(DEFAULT_VOICE_STATE.similarityBoost)) ? Number(DEFAULT_VOICE_STATE.similarityBoost) : 0.75,
    style: Number.isFinite(Number(DEFAULT_VOICE_STATE.style)) ? Number(DEFAULT_VOICE_STATE.style) : 0,
    speed: Number.isFinite(Number(DEFAULT_VOICE_STATE.speed)) ? Number(DEFAULT_VOICE_STATE.speed) : 1,
    useSpeakerBoost: typeof DEFAULT_VOICE_STATE.useSpeakerBoost === 'boolean' ? DEFAULT_VOICE_STATE.useSpeakerBoost : true,
    text: DEFAULT_VOICE_STATE.text || '',
    audioUrl: '',
    downloadUrl: '',
    generationId: DEFAULT_VOICE_STATE.generationId || '',
    voices: [],
    isGenerating: false,
    errorText: '',
    lastGeneratedAt: DEFAULT_VOICE_STATE.lastGeneratedAt || '',
  },
  voiceHistory: {
    items: [],
    loading: false,
    loaded: false,
    selectedId: DEFAULT_VOICE_HISTORY_STATE.selectedId || '',
    selectedItem: null,
    lastError: '',
    limit: 24,
    offset: 0,
  },
  music: {
    ai: DEFAULT_MUSIC_STATE.ai || 'suno',
    backend: DEFAULT_MUSIC_STATE.backend || 'sunoapi',
    mode: DEFAULT_MUSIC_STATE.mode || 'idea',
    activeTab: DEFAULT_MUSIC_STATE.activeTab || 'idea',
    title: DEFAULT_MUSIC_STATE.title || '',
    tags: DEFAULT_MUSIC_STATE.tags || '',
    language: DEFAULT_MUSIC_STATE.language || 'ru',
    mood: DEFAULT_MUSIC_STATE.mood || '',
    references: DEFAULT_MUSIC_STATE.references || '',
    instrumental: !!DEFAULT_MUSIC_STATE.instrumental,
    ideaText: DEFAULT_MUSIC_STATE.ideaText || '',
    lyricsText: DEFAULT_MUSIC_STATE.lyricsText || '',
    generationId: DEFAULT_MUSIC_STATE.generationId || '',
    isGenerating: false,
    status: DEFAULT_MUSIC_STATE.status || 'idle',
    statusText: DEFAULT_MUSIC_STATE.statusText || 'Собери идею, текст и параметры справа, затем запусти генерацию.',
    errorText: '',
    lastCompletedAt: DEFAULT_MUSIC_STATE.lastCompletedAt || '',
    results: [],
    songwriter: {
      input: DEFAULT_MUSIC_STATE.songwriterInput || '',
      loading: false,
      messages: Array.isArray(DEFAULT_MUSIC_STATE.songwriterMessages) ? DEFAULT_MUSIC_STATE.songwriterMessages : [],
      lastAnswer: DEFAULT_MUSIC_STATE.songwriterLastAnswer || '',
    },
  },
  musicHistory: {
    items: [],
    loading: false,
    loaded: false,
    selectedId: DEFAULT_MUSIC_HISTORY_STATE.selectedId || '',
    selectedItem: null,
    lastError: '',
    limit: 24,
    offset: 0,
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
  chat: { icon: 'spark', title: 'ChatGPT Studio', subtitle: 'Центральный AI-чат для идей, сценариев и быстрых переходов в другие студии.', eyebrow: 'Creative AI Studio' },
  video: { icon: 'video', title: 'Video Studio', subtitle: 'Kling, Veo, Seedance и Sora в одной рабочей зоне с живыми настройками.', eyebrow: 'Video generation' },
  image: { icon: 'image', title: 'Image Studio', subtitle: 'Nano Banana, нейрофотосессии, posters и image-to-image сценарии.', eyebrow: 'Image generation' },
  voice: { icon: 'voice', title: 'Voice Studio', subtitle: 'Озвучка, выбор голоса и быстрый экспорт результата.', eyebrow: 'Voice workflow' },
  music: { icon: 'music', title: 'Music Studio', subtitle: 'Songwriter, Suno, Udio и выдача треков прямо в рабочей зоне.', eyebrow: 'Music workflow' },
  library: { icon: 'library', title: 'Prompt Library', subtitle: 'Категории, группы и карточки промптов с быстрым переносом в студии.', eyebrow: 'Prompt system' },
  workspace: { icon: 'workspace', title: 'Workspace', subtitle: 'Планы, референсы, заметки и проектная логика поверх генераций.', eyebrow: 'Project hub' },
  history: { icon: 'history', title: 'History', subtitle: 'История запусков, статусов и готовых результатов.', eyebrow: 'Result archive' },
  billing: { icon: 'billing', title: 'Billing', subtitle: 'Баланс, токены и экономика генераций.', eyebrow: 'Token economy' },
  profile: { icon: 'profile', title: 'Profile', subtitle: 'Telegram-связка, базовые настройки и состояние системы.', eyebrow: 'Account and access' },
};

const STUDIO_ICON_SVG = {
  spark: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9L12 3Z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/></svg>',
  video: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3.5" y="6" width="12" height="12" rx="3" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M15.5 10l5-3v10l-5-3" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/></svg>',
  image: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3.5" y="5" width="17" height="14" rx="3" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M7 15l3.2-3.2a1 1 0 0 1 1.4 0L14 14l2.2-2.2a1 1 0 0 1 1.4 0L20 14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/><circle cx="9" cy="9" r="1.3" fill="currentColor"/></svg>',
  voice: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4a3 3 0 0 1 3 3v4a3 3 0 1 1-6 0V7a3 3 0 0 1 3-3Z" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M6 11a6 6 0 0 0 12 0M12 17v3M9 20h6" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>',
  music: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 5v9.5a2.5 2.5 0 1 1-1.7-2.38V7.4l7-1.6v7.7a2.5 2.5 0 1 1-1.7-2.38V4.4L14 5Z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/></svg>',
  library: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 6.5A2.5 2.5 0 0 1 7.5 4H20v14H7.5A2.5 2.5 0 0 0 5 20.5V6.5Zm0 0V20M8 7h8M8 11h8" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  workspace: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3.5" y="4" width="17" height="16" rx="3" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M3.5 9.5h17M8.5 4v16" fill="none" stroke="currentColor" stroke-width="1.7"/></svg>',
  history: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 12a8 8 0 1 0 2.34-5.66L4 8.7" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/><path d="M12 8v4l3 2" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  billing: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3.5" y="6" width="17" height="12" rx="3" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M3.5 10.5h17M8 14h3" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>',
  profile: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="8" r="3.2" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M5.5 19a6.5 6.5 0 0 1 13 0" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>',
};

function renderStudioIcon(iconKey) {
  return STUDIO_ICON_SVG[iconKey] || STUDIO_ICON_SVG.spark;
}


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
  topaz_photo: {
    name: 'Topaz Upscale Фото',
    models: {
      'topaz-photo': {
        name: 'Topaz Photo AI',
        backend: 'planned',
        modes: {
          upscale: { name: 'Upscale', fields: ['sourceImage', 'upscalePreset'] },
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
        name: 'ModelArk / Seedream',
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
        name: 'Seedream Text to Image',
        backend: 'planned',
        modes: {
          t2i: { name: 'Text → Image', fields: ['prompt'] },
        },
      },
    },
  },
};

function normalizeVideoEditorState(saved) {
  const base = {
    activeVideo: {
      sourceType: 'generation',
      generationId: '',
      uploadId: '',
      videoUrl: '',
      downloadUrl: '',
      durationSec: 0,
      filename: '',
    },
    trim: {
      enabled: false,
      startSec: 0,
      endSec: 0,
    },
    originalAudio: {
      mute: false,
      volume: 100,
    },
    audioClips: [],
    mergeQueue: [],
    isProcessing: false,
    lastJobId: '',
    status: 'idle',
    errorText: '',
    noticeText: '',
  };
  if (!saved || typeof saved !== 'object') return base;
  const next = JSON.parse(JSON.stringify(base));
  next.activeVideo = { ...next.activeVideo, ...(saved.activeVideo || {}) };
  next.trim = { ...next.trim, ...(saved.trim || {}) };
  next.originalAudio = { ...next.originalAudio, ...(saved.originalAudio || {}) };
  next.audioClips = Array.isArray(saved.audioClips) ? saved.audioClips.map((item) => ({
    uploadId: String(item?.uploadId || ''),
    filename: String(item?.filename || 'audio'),
    durationSec: Number(item?.durationSec || 0),
    audioStart: Number(item?.audioStart || 0),
    audioEnd: Number(item?.audioEnd || 0),
    videoStart: Number(item?.videoStart || 0),
    volume: Number(item?.volume ?? 100),
  })) : [];
  next.mergeQueue = Array.isArray(saved.mergeQueue) ? saved.mergeQueue.map((item) => ({
    type: String(item?.type || 'generation'),
    id: String(item?.id || ''),
    filename: String(item?.filename || 'video'),
    durationSec: Number(item?.durationSec || 0),
    sourceLabel: String(item?.sourceLabel || ''),
  })) : [];
  next.isProcessing = !!saved.isProcessing;
  next.lastJobId = String(saved.lastJobId || '');
  next.status = String(saved.status || 'idle');
  next.errorText = String(saved.errorText || '');
  next.noticeText = String(saved.noticeText || '');
  return next;
}

function resetVideoEditorState(active = null) {
  state.videoEditor = normalizeVideoEditorState(null);
  if (active) {
    state.videoEditor.activeVideo = {
      sourceType: active.sourceType || 'generation',
      generationId: active.generationId || '',
      uploadId: active.uploadId || '',
      videoUrl: active.videoUrl || '',
      downloadUrl: active.downloadUrl || active.videoUrl || '',
      durationSec: Number(active.durationSec || 0),
      filename: active.filename || '',
    };
    state.videoEditor.trim.endSec = Number(active.durationSec || 0);
  }
}

function videoEditorHasActiveVideo() {
  return !!String(state.videoEditor?.activeVideo?.generationId || '').trim() || !!String(state.videoEditor?.activeVideo?.uploadId || '').trim();
}

function formatSecondsCompact(value) {
  const seconds = Math.max(0, Number(value || 0));
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

function syncVideoEditorWithHistoryItem(item) {
  const selected = item || historySelectedItem();
  if (!selected) {
    resetVideoEditorState();
    return;
  }
  const duration = Number(selected.duration_sec || 0);
  resetVideoEditorState({
    sourceType: 'generation',
    generationId: selected.id || '',
    videoUrl: historyVideoUrl(selected),
    downloadUrl: historyVideoDownloadUrl(selected),
    durationSec: duration,
    filename: trimText(selected.prompt || `${selected.provider || 'video'} ${selected.model || ''}`, 88) || 'video',
  });
}

function ensureActiveVideoFirstInMergeQueue() {
  if (!videoEditorHasActiveVideo()) return false;
  const activeId = String(state.videoEditor.activeVideo.generationId || '').trim();
  if (!activeId) return false;
  const exists = state.videoEditor.mergeQueue.some((item) => item.type === 'generation' && item.id === activeId);
  if (!exists) {
    state.videoEditor.mergeQueue.unshift({
      type: 'generation',
      id: activeId,
      filename: state.videoEditor.activeVideo.filename || 'Активный ролик',
      durationSec: Number(state.videoEditor.activeVideo.durationSec || 0),
      sourceLabel: 'active',
    });
  }
  return true;
}

function getVideoEditorTimelinePayload() {
  const durationSec = Math.max(0, Number(state.videoEditor.activeVideo.durationSec || 0));
  const mergeItems = Array.isArray(state.videoEditor.mergeQueue) ? state.videoEditor.mergeQueue.filter((item) => item?.id) : [];
  const useMerge = mergeItems.length > 1 || (mergeItems.length === 1 && !(mergeItems[0].type === 'generation' && mergeItems[0].id === state.videoEditor.activeVideo.generationId));
  const trimStart = Math.max(0, Number(state.videoEditor.trim.startSec || 0));
  const trimEndRaw = Number(state.videoEditor.trim.endSec || durationSec || 0);
  const trimEnd = durationSec > 0 ? Math.min(durationSec, trimEndRaw) : trimEndRaw;

  return {
    source_generation_id: state.videoEditor.activeVideo.generationId,
    timeline: {
      trim: {
        enabled: !!state.videoEditor.trim.enabled,
        start_sec: trimStart,
        end_sec: trimEnd,
      },
      original_audio: {
        mute: !!state.videoEditor.originalAudio.mute,
        volume: Math.max(0, Math.min(100, Number(state.videoEditor.originalAudio.volume || 0))),
      },
      audio_clips: state.videoEditor.audioClips.map((item) => ({
        upload_id: item.uploadId,
        audio_start: Math.max(0, Number(item.audioStart || 0)),
        audio_end: Math.max(0, Number(item.audioEnd || 0)),
        video_start: Math.max(0, Number(item.videoStart || 0)),
        volume: Math.max(0, Math.min(100, Number(item.volume || 0))),
      })),
      merge_items: useMerge ? mergeItems.map((item) => ({ type: item.type, id: item.id })) : [],
    },
  };
}

function stopVideoEditPolling() {
  if (runtime.videoEditPollTimer) {
    clearInterval(runtime.videoEditPollTimer);
    runtime.videoEditPollTimer = null;
  }
}

function startVideoEditPolling({ immediate = false } = {}) {
  stopVideoEditPolling();
  if (!state.videoEditor.lastJobId) return;
  runtime.videoEditPollTimer = setInterval(() => {
    pollVideoEditJob({ silent: true }).catch(() => {});
  }, 4000);
  if (immediate) {
    pollVideoEditJob({ silent: true }).catch(() => {});
  }
}

async function uploadWorkspaceEditorFile(file, kindHint = '') {
  const form = new FormData();
  form.append('file', file, file.name || 'upload.bin');
  const res = await apiFetch('/api/workspace/video/upload', { method: 'POST', body: form });
  const data = await res.json();
  if (kindHint && data.file_type !== kindHint) {
    throw new Error(kindHint === 'audio' ? 'Нужен аудиофайл (mp3 / wav / m4a).' : 'Нужен видеофайл (mp4 / mov / webm).');
  }
  return data;
}

async function handleEditorAudioFileSelected(file) {
  if (!file) return;
  if (state.videoEditor.audioClips.length >= 3) {
    toast('info', 'Лимит достигнут', 'В первой версии доступно максимум 3 аудио-куска.');
    return;
  }
  try {
    const uploaded = await uploadWorkspaceEditorFile(file, 'audio');
    const duration = Number(uploaded.duration || 0);
    state.videoEditor.audioClips.push({
      uploadId: uploaded.upload_id,
      filename: uploaded.filename || file.name || 'audio',
      durationSec: duration,
      audioStart: 0,
      audioEnd: duration,
      videoStart: 0,
      volume: 100,
    });
    state.videoEditor.noticeText = 'Аудиофайл загружен и добавлен в монтаж.';
    state.videoEditor.errorText = '';
    saveState();
    render();
  } catch (e) {
    toast('error', 'Не удалось загрузить аудио', String(e.message || e));
  }
}

async function handleEditorMergeVideoSelected(file) {
  if (!file) return;
  if (state.videoEditor.mergeQueue.length >= 10) {
    toast('info', 'Лимит достигнут', 'В первой версии доступно максимум 10 видео для склейки.');
    return;
  }
  try {
    ensureActiveVideoFirstInMergeQueue();
    const uploaded = await uploadWorkspaceEditorFile(file, 'video');
    state.videoEditor.mergeQueue.push({
      type: 'upload',
      id: uploaded.upload_id,
      filename: uploaded.filename || file.name || 'video',
      durationSec: Number(uploaded.duration || 0),
      sourceLabel: 'upload',
    });
    state.videoEditor.noticeText = 'Внешний ролик загружен и добавлен в очередь склейки.';
    state.videoEditor.errorText = '';
    saveState();
    render();
  } catch (e) {
    toast('error', 'Не удалось загрузить видео', String(e.message || e));
  }
}

async function pollVideoEditJob(options = {}) {
  const { silent = false } = options;
  if (!state.videoEditor.lastJobId) return;
  try {
    const res = await apiFetch(`/api/workspace/video/job/${encodeURIComponent(state.videoEditor.lastJobId)}`);
    const data = await res.json();
    const job = data.job || {};
    const item = data.item || null;
    state.videoEditor.status = String(job.status || 'processing');
    state.videoEditor.errorText = String(job.error_message || '');
    state.videoEditor.noticeText = item && state.videoEditor.status === 'completed'
      ? 'Новый ролик сохранён в библиотеку.'
      : (state.videoEditor.status === 'failed' ? '' : 'Обработка видео на backend...');
    state.videoEditor.isProcessing = ['queued', 'processing'].includes(state.videoEditor.status);

    if (state.videoEditor.status === 'completed') {
      stopVideoEditPolling();
      state.videoEditor.isProcessing = false;
      await loadVideoHistory({ silent: true, keepSelection: true, selectId: item?.id || '' });
      if (item) {
        applyHistoryItemToVideoWorkspace(item);
      }
      saveState();
      if (!silent) toast('success', 'Монтаж готов', 'Новый ролик сохранён в библиотеку.');
      return;
    }

    if (state.videoEditor.status === 'failed') {
      stopVideoEditPolling();
      state.videoEditor.isProcessing = false;
      saveState();
      render();
      if (!silent) toast('error', 'Ошибка обработки', state.videoEditor.errorText || 'FFmpeg вернул ошибку.');
      return;
    }

    saveState();
    if (!silent) render();
  } catch (e) {
    if (!silent) toast('error', 'Не удалось проверить монтаж', String(e.message || e));
  }
}

async function saveVideoEdit() {
  if (!requireAuth()) return;
  if (!videoEditorHasActiveVideo()) {
    toast('info', 'Нет активного ролика', 'Сначала открой ролик в рабочей зоне.');
    return;
  }
  const payload = getVideoEditorTimelinePayload();
  if (payload.timeline.trim.enabled && payload.timeline.trim.end_sec <= payload.timeline.trim.start_sec) {
    toast('error', 'Неверный trim', 'Конец должен быть больше начала.');
    return;
  }
  if (payload.timeline.trim.enabled && (payload.timeline.trim.end_sec - payload.timeline.trim.start_sec) < 0.5) {
    toast('error', 'Неверный trim', 'Минимальная длина результата — 0.5 сек.');
    return;
  }

  state.videoEditor.isProcessing = true;
  state.videoEditor.status = 'queued';
  state.videoEditor.errorText = '';
  state.videoEditor.noticeText = 'Задача монтажа отправлена на backend.';
  saveState();
  render();

  try {
    const res = await apiFetch('/api/workspace/video/edit', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    state.videoEditor.lastJobId = data.job_id || '';
    state.videoEditor.status = String(data.status || 'queued');
    state.videoEditor.noticeText = 'Обработка видео началась.';
    saveState();
    render();
    startVideoEditPolling({ immediate: true });
  } catch (e) {
    state.videoEditor.isProcessing = false;
    state.videoEditor.status = 'failed';
    state.videoEditor.errorText = String(e.message || e);
    state.videoEditor.noticeText = '';
    saveState();
    render();
    toast('error', 'Не удалось запустить монтаж', state.videoEditor.errorText);
  }
}

function renderVideoEditor() {
  if (!videoEditorHasActiveVideo()) {
    return `
      <div class="video-editor-block">
        <div class="video-editor-empty">
          <strong>Редактор видео</strong>
          <div>Открой ролик в рабочей зоне, чтобы редактировать его.</div>
        </div>
      </div>
    `;
  }

  const durationSec = Math.max(0, Number(state.videoEditor.activeVideo.durationSec || 0));
  const trimEnd = Math.min(durationSec || Number(state.videoEditor.trim.endSec || 0), Number(state.videoEditor.trim.endSec || durationSec || 0));
  const trimStart = Math.min(trimEnd, Math.max(0, Number(state.videoEditor.trim.startSec || 0)));
  const mute = !!state.videoEditor.originalAudio.mute;

  const audioCards = state.videoEditor.audioClips.length
    ? state.videoEditor.audioClips.map((clip, index) => `
      <div class="video-editor-subcard">
        <div class="video-editor-row between">
          <strong>${escapeHtml(clip.filename || `Audio ${index + 1}`)}</strong>
          <button class="btn ghost small" data-action="editor-remove-audio-clip" data-index="${index}">Удалить</button>
        </div>
        <div class="video-editor-mini-grid">
          <label>audio start<input id="editor_audio_${index}_audio_start" type="number" min="0" step="0.1" value="${Number(clip.audioStart || 0)}"></label>
          <label>audio end<input id="editor_audio_${index}_audio_end" type="number" min="0" step="0.1" value="${Number(clip.audioEnd || 0)}"></label>
          <label>video start<input id="editor_audio_${index}_video_start" type="number" min="0" step="0.1" value="${Number(clip.videoStart || 0)}"></label>
          <label>громкость %<input id="editor_audio_${index}_volume" type="number" min="0" max="100" step="1" value="${Number(clip.volume || 100)}"></label>
        </div>
        <small>Длина аудио: ${escapeHtml(formatSecondsCompact(clip.durationSec || 0))}</small>
      </div>
    `).join('')
    : `<div class="video-editor-empty-list">Пока нет аудио-вставок. Можно добавить до 3 кусочков.</div>`;

  const mergeCards = state.videoEditor.mergeQueue.length
    ? state.videoEditor.mergeQueue.map((item, index) => `
      <div class="video-editor-subcard">
        <div class="video-editor-row between">
          <div>
            <strong>${escapeHtml(item.filename || 'video')}</strong>
            <small>${escapeHtml(item.type === 'generation' ? 'Библиотека' : 'Upload')} · ${escapeHtml(formatSecondsCompact(item.durationSec || 0))}</small>
          </div>
          <div class="actions compact-gap">
            <button class="btn ghost small" data-action="editor-merge-up" data-index="${index}">↑</button>
            <button class="btn ghost small" data-action="editor-merge-down" data-index="${index}">↓</button>
            <button class="btn ghost small" data-action="editor-merge-remove" data-index="${index}">Удалить</button>
          </div>
        </div>
      </div>
    `).join('')
    : `<div class="video-editor-empty-list">Очередь склейки пока пуста. Активный ролик можно добавить кнопкой ниже.</div>`;

  const statusTextMap = {
    idle: 'Готово к сохранению',
    queued: 'Обработка...',
    processing: 'Обработка...',
    completed: 'Сохранено в библиотеку',
    failed: 'Ошибка обработки',
  };

  return `
    <div class="video-editor-block">
      <div class="video-editor-head">
        <div>
          <div class="section-title" style="margin:0;">Редактор видео</div>
          <div class="help-text">Активный ролик: ${escapeHtml(state.videoEditor.activeVideo.filename || 'Видео')} · ${escapeHtml(formatSecondsCompact(durationSec))}</div>
        </div>
        <span class="badge muted">Mini editor v1</span>
      </div>

      <div class="video-editor-grid">
        <div class="video-editor-card">
          <div class="video-editor-row between">
            <h4>Обрезка</h4>
            <label class="switch"><input id="editor_trim_enabled" type="checkbox" ${state.videoEditor.trim.enabled ? 'checked' : ''}><span></span></label>
          </div>
          <div class="video-editor-mini-grid">
            <label>Старт, сек<input id="editor_trim_start_input" type="number" min="0" max="${durationSec}" step="0.1" value="${trimStart.toFixed(1)}"></label>
            <label>Конец, сек<input id="editor_trim_end_input" type="number" min="0" max="${durationSec}" step="0.1" value="${trimEnd.toFixed(1)}"></label>
          </div>
          <div class="video-editor-range-wrap">
            <input id="editor_trim_start_range" type="range" min="0" max="${durationSec || 0}" step="0.1" value="${trimStart.toFixed(1)}" ${!state.videoEditor.trim.enabled ? 'disabled' : ''}>
            <input id="editor_trim_end_range" type="range" min="0" max="${durationSec || 0}" step="0.1" value="${trimEnd.toFixed(1)}" ${!state.videoEditor.trim.enabled ? 'disabled' : ''}>
          </div>
          <div class="video-editor-row between">
            <small>${escapeHtml(formatSecondsCompact(trimStart))}</small>
            <small>${escapeHtml(formatSecondsCompact(trimEnd))}</small>
          </div>
          <div class="actions compact-gap" style="margin-top:10px;">
            <button class="btn ghost small" data-action="editor-reset-trim">Сбросить диапазон</button>
          </div>
        </div>

        <div class="video-editor-card">
          <div class="video-editor-row between">
            <h4>Звук видео</h4>
            <label class="switch"><input id="editor_original_audio_mute" type="checkbox" ${mute ? 'checked' : ''}><span></span></label>
          </div>
          <div class="video-editor-mini-grid">
            <label>Громкость, %<input id="editor_original_audio_volume_input" type="number" min="0" max="100" step="1" value="${Number(state.videoEditor.originalAudio.volume || 100)}" ${mute ? 'disabled' : ''}></label>
          </div>
          <input id="editor_original_audio_volume" type="range" min="0" max="100" step="1" value="${Number(state.videoEditor.originalAudio.volume || 100)}" ${mute ? 'disabled' : ''}>
          <small>${mute ? 'Исходный звук будет удалён.' : '0% = фактически mute, 100% = как в исходном ролике.'}</small>
        </div>

        <div class="video-editor-card">
          <div class="video-editor-row between">
            <h4>Аудио-вставки</h4>
            <button class="btn outline small" data-action="editor-pick-audio">Добавить аудио</button>
          </div>
          ${audioCards}
          <input id="editorAudioUpload" class="hidden" type="file" accept=".mp3,.wav,.m4a,audio/*">
        </div>

        <div class="video-editor-card">
          <div class="video-editor-row between">
            <h4>Склейка роликов</h4>
            <span class="badge muted">${state.videoEditor.mergeQueue.length}/10</span>
          </div>
          <div class="actions compact-gap" style="margin-bottom:12px; flex-wrap:wrap;">
            <button class="btn outline small" data-action="editor-add-active-video">Активный ролик</button>
            <button class="btn outline small" data-action="editor-add-history-video">Добавить из библиотеки</button>
            <button class="btn outline small" data-action="editor-pick-merge-video">Загрузить с компьютера</button>
          </div>
          <div class="help-text" style="margin-bottom:10px;">Для кнопки «Добавить из библиотеки» используется выбранный ролик из истории справа.</div>
          ${mergeCards}
          <input id="editorMergeUpload" class="hidden" type="file" accept=".mp4,.mov,.webm,video/*">
        </div>
      </div>

      <div class="video-editor-footer">
        <div class="video-editor-status ${escapeHtml(state.videoEditor.status)}">
          <strong>${escapeHtml(statusTextMap[state.videoEditor.status] || 'Готово к сохранению')}</strong>
          <div>${escapeHtml(state.videoEditor.errorText || state.videoEditor.noticeText || 'После обработки новый ролик появится в библиотеке как отдельный объект.')}</div>
        </div>
        <button class="btn primary full" data-action="save-video-edit" ${state.videoEditor.isProcessing ? 'disabled' : ''}>${state.videoEditor.isProcessing ? 'Обработка...' : 'Сохранить как новый ролик'}</button>
      </div>
    </div>
  `;
}

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
  localStorage.setItem('astrabot:videoEditorState', JSON.stringify(state.videoEditor));
  localStorage.setItem('astrabot:imageState', JSON.stringify({
    provider: state.image.provider,
    model: state.image.model,
    mode: state.image.mode,
    prompt: state.image.prompt,
    aspectRatio: state.image.aspectRatio,
    resolution: state.image.resolution,
    safetyLevel: state.image.safetyLevel,
    posterStyle: state.image.posterStyle,
    stylePreset: state.image.stylePreset,
    moodPreset: state.image.moodPreset,
    panel: state.image.panel,
  }));
  localStorage.setItem('astrabot:voiceState', JSON.stringify({
    voiceId: state.voice.voiceId,
    modelId: state.voice.modelId,
    outputFormat: state.voice.outputFormat,
    languageCode: state.voice.languageCode,
    manualVoiceSettings: !!state.voice.manualVoiceSettings,
    stability: Number(state.voice.stability),
    similarityBoost: Number(state.voice.similarityBoost),
    style: Number(state.voice.style),
    speed: Number(state.voice.speed),
    useSpeakerBoost: !!state.voice.useSpeakerBoost,
    text: state.voice.text,
    generationId: state.voice.generationId || '',
    lastGeneratedAt: state.voice.lastGeneratedAt || '',
  }));
  localStorage.setItem('astrabot:voiceHistoryState', JSON.stringify({
    selectedId: state.voiceHistory.selectedId || '',
  }));
  localStorage.setItem('astrabot:musicState', JSON.stringify({
    ai: state.music.ai,
    backend: state.music.backend,
    mode: state.music.mode,
    activeTab: state.music.activeTab,
    title: state.music.title,
    tags: state.music.tags,
    language: state.music.language,
    mood: state.music.mood,
    references: state.music.references,
    instrumental: !!state.music.instrumental,
    ideaText: state.music.ideaText,
    lyricsText: state.music.lyricsText,
    generationId: state.music.generationId || '',
    status: state.music.status || 'idle',
    statusText: state.music.statusText || '',
    lastCompletedAt: state.music.lastCompletedAt || '',
    songwriterInput: state.music.songwriter.input || '',
    songwriterLastAnswer: state.music.songwriter.lastAnswer || '',
    songwriterMessages: Array.isArray(state.music.songwriter.messages) ? state.music.songwriter.messages.slice(-20) : [],
  }));
  localStorage.setItem('astrabot:musicHistoryState', JSON.stringify({
    selectedId: state.musicHistory.selectedId || '',
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

function selectedVoiceData() {
  return (state.voice.voices || []).find((item) => item.voice_id === state.voice.voiceId) || null;
}

function voiceModelLabel(modelId = '') {
  if (modelId === 'eleven_flash_v2_5') return 'Eleven Flash v2.5';
  if (modelId === 'eleven_turbo_v2_5') return 'Eleven Turbo v2.5';
  if (modelId === 'eleven_multilingual_v2') return 'Eleven Multilingual v2';
  return modelId || '—';
}

function voiceSettingsBadge(value, digits = 2) {
  const num = Number(value);
  if (!Number.isFinite(num)) return '—';
  return num.toFixed(digits).replace(/\.00$/, '').replace(/(\.\d)0$/, '$1');
}

function voiceLanguageLabel(languageCode = '') {
  const map = {
    auto: 'Авто',
    ru: 'Русский',
    en: 'English',
    uk: 'Українська',
    de: 'Deutsch',
    fr: 'Français',
    es: 'Español',
    it: 'Italiano',
    pt: 'Português',
    pl: 'Polski',
    tr: 'Türkçe',
    ar: 'العربية',
    hi: 'हिन्दी',
    zh: '中文',
    ja: '日本語',
    ko: '한국어',
  };
  return map[String(languageCode || '').trim()] || (languageCode || 'Авто');
}

function voiceOutputLabel(outputFormat = '') {
  const map = {
    mp3_44100_128: 'MP3 · 44.1 kHz · 128 kbps',
    mp3_44100_192: 'MP3 · 44.1 kHz · 192 kbps',
    pcm_44100: 'PCM · 44.1 kHz',
  };
  return map[outputFormat] || outputFormat || '—';
}

function voiceEstimatedDurationText() {
  const chars = String(state.voice.text || '').trim().length;
  if (!chars) return '0 сек';
  const seconds = Math.max(1, Math.round(chars / 14));
  return `~${seconds} сек`;
}

function voiceDownloadFilename() {
  const voice = selectedVoiceData();
  const voiceSlug = String(voice?.name || 'voice')
    .toLowerCase()
    .replace(/[^a-zа-я0-9]+/gi, '-')
    .replace(/^-+|-+$/g, '') || 'voice';
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  const ext = String(state.voice.outputFormat || '').startsWith('mp3') ? 'mp3' : 'wav';
  return `${voiceSlug}-${stamp}.${ext}`;
}

function revokeVoiceAudioUrl() {
  if (state.voice.audioUrl && String(state.voice.audioUrl).startsWith('blob:')) {
    try { URL.revokeObjectURL(state.voice.audioUrl); } catch {}
  }
}

function clearVoiceRunState({ keepText = true, clearHistorySelection = true } = {}) {
  revokeVoiceAudioUrl();
  state.voice.audioUrl = '';
  state.voice.downloadUrl = '';
  state.voice.generationId = '';
  state.voice.errorText = '';
  state.voice.isGenerating = false;
  state.voice.lastGeneratedAt = '';
  if (!keepText) state.voice.text = '';
  if (clearHistorySelection) {
    state.voiceHistory.selectedId = '';
    state.voiceHistory.selectedItem = null;
  }
  saveState();
}

function voiceHistorySelectedItem() {
  const items = Array.isArray(state.voiceHistory?.items) ? state.voiceHistory.items : [];
  const selectedId = String(state.voiceHistory?.selectedId || '').trim();
  if (state.voiceHistory?.selectedItem && state.voiceHistory.selectedItem.id) {
    return state.voiceHistory.selectedItem;
  }
  if (selectedId) {
    const found = items.find((item) => String(item?.id || '') === selectedId);
    if (found) return found;
  }
  return null;
}

function voiceHistoryAudioUrl(item) {
  if (!item) return '';
  const candidates = [item.audio_url, item.download_url].filter(Boolean);
  return candidates[0] || '';
}

function voiceHistoryDownloadUrl(item) {
  if (!item) return '';
  const candidates = [item.download_url, item.audio_url].filter(Boolean);
  return candidates[0] || '';
}

function applyVoiceHistoryItemToWorkspace(item, options = {}) {
  if (!item) return;
  const { silent = false } = options;
  revokeVoiceAudioUrl();
  state.voiceHistory.selectedId = item.id || '';
  state.voiceHistory.selectedItem = item;
  state.voice.generationId = item.id || '';
  state.voice.audioUrl = voiceHistoryAudioUrl(item);
  state.voice.downloadUrl = voiceHistoryDownloadUrl(item);
  state.voice.errorText = item.error_message || '';
  state.voice.lastGeneratedAt = item.completed_at || item.created_at || '';
  if (item.voice_id) state.voice.voiceId = item.voice_id;
  if (item.model) state.voice.modelId = item.model;
  if (item.output_format) state.voice.outputFormat = item.output_format;
  if (item.text) state.voice.text = item.text;
  saveState();
  render();
  if (!silent) toast('success', 'Открыто из истории', 'Сохранённая озвучка загружена в рабочую область.');
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

function revokeRuntimeFileEntry(entry) {
  if (entry?.url) {
    try { URL.revokeObjectURL(entry.url); } catch (_e) {}
  }
}

function revokeRuntimeFileValue(value) {
  if (Array.isArray(value)) {
    value.forEach((entry) => revokeRuntimeFileEntry(entry));
    return;
  }
  revokeRuntimeFileEntry(value);
}

function setFile(key, file, multiple = false) {
  if (!file) return;
  if (multiple) {
    const incoming = Array.from(file || []);
    const current = Array.isArray(runtime.files[key]) ? runtime.files[key] : [];
    const merged = [...current];
    const seen = new Set(current.map((entry) => `${entry.name}::${entry.size}::${entry.lastModified || 0}`));
    incoming.forEach((item) => {
      const signature = `${item.name}::${item.size || 0}::${item.lastModified || 0}`;
      if (seen.has(signature)) return;
      seen.add(signature);
      merged.push(makeRuntimeFileEntry(item));
    });
    runtime.files[key] = merged;
    return;
  }
  revokeRuntimeFileValue(runtime.files[key]);
  runtime.files[key] = makeRuntimeFileEntry(file);
}

function getFile(key) {
  return runtime.files[key] || null;
}

function removeUploadFile(inputId, index = null) {
  const config = FILE_INPUT_MAP[inputId];
  if (!config) return;
  const current = runtime.files[config.key];
  if (!current) return;

  if (config.multiple && Array.isArray(current)) {
    const targetIndex = Number(index);
    if (!Number.isInteger(targetIndex) || targetIndex < 0 || targetIndex >= current.length) return;
    const removed = current[targetIndex];
    revokeRuntimeFileEntry(removed);
    const next = current.filter((_, itemIndex) => itemIndex !== targetIndex);
    if (next.length) runtime.files[config.key] = next;
    else delete runtime.files[config.key];
  } else {
    revokeRuntimeFileValue(current);
    delete runtime.files[config.key];
  }

  const input = document.getElementById(inputId);
  if (input) input.value = '';
  saveState();
  render();
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
    case 'music': return { studio: 'Music', provider: state.music.ai === 'udio' ? 'Udio' : 'Suno', model: state.music.backend || 'auto', mode: state.music.mode === 'lyrics' ? 'Lyrics' : 'Idea' };
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

function imageProviderConfig() {
  return IMAGE_REGISTRY[state.image.provider] || IMAGE_REGISTRY.nano_banana_pro;
}

function imageModelConfig() {
  return imageProviderConfig().models[state.image.model] || Object.values(imageProviderConfig().models)[0];
}

function imageModeConfig() {
  return imageModelConfig().modes[state.image.mode] || Object.values(imageModelConfig().modes)[0];
}

function syncImageSelection() {
  const fallbackProvider = IMAGE_REGISTRY.nano_banana_pro ? 'nano_banana_pro' : Object.keys(IMAGE_REGISTRY)[0];
  const provider = IMAGE_REGISTRY[state.image.provider] ? state.image.provider : fallbackProvider;
  state.image.provider = provider;
  const providerConfig = IMAGE_REGISTRY[provider];
  const modelIds = Object.keys(providerConfig.models || {});
  if (!modelIds.includes(state.image.model)) state.image.model = modelIds[0];
  const modelConfig = providerConfig.models[state.image.model];
  const modeIds = Object.keys(modelConfig.modes || {});
  if (!modeIds.includes(state.image.mode)) state.image.mode = modeIds[0];

  if (['text_to_image', 't2i'].includes(state.image.mode) && state.image.aspectRatio === 'match_input_image') {
    state.image.aspectRatio = '16:9';
  }
}

function imageNeedsSourceImage() {
  syncImageSelection();
  if (state.image.provider === 'nano_banana') return true;
  if (state.image.provider === 'nano_banana_pro' && state.image.mode === 'image_to_image') return true;
  if (state.image.provider === 'posters' && state.image.mode === 'photo_edit') return true;
  if (state.image.provider === 'photosession') return true;
  if (state.image.provider === 'two_images') return true;
  if (state.image.provider === 'topaz_photo') return true;
  return false;
}

function imageNeedsBaseImage() {
  syncImageSelection();
  return state.image.provider === 'two_images';
}

function imageRunCost() {
  syncImageSelection();
  switch (state.image.provider) {
    case 'nano_banana':
      return 1;
    case 'nano_banana_pro':
      return 2;
    case 'photosession':
      return 1;
    case 'two_images':
      return 1;
    case 'topaz_photo': {
      const preset = String(state.image.upscalePreset || 'standard');
      if (preset === 'detail') return 3;
      if (preset === 'max') return 4;
      return 2;
    }
    case 'posters':
      return 0;
    case 'text_to_image':
      return 0;
    default:
      return 0;
  }
}

function imageRunButtonLabel() {
  if (state.image.isGenerating) return 'Генерация...';
  const cost = imageRunCost();
  return cost > 0 ? `Сгенерировать · ${cost} ток.` : 'Сгенерировать';
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


function setImagePanel(panel) {
  state.image.panel = panel === 'library' ? 'library' : 'params';
  if (state.image.panel === 'library' && state.authToken) {
    loadImageHistory({ silent: true, keepSelection: true }).catch(() => {});
  }
  saveState();
  render();
}

function imageHistorySelectedItem() {
  if (!state.imageHistory.selectedId) return null;
  return state.imageHistory.items.find((item) => item.id === state.imageHistory.selectedId) || state.imageHistory.selectedItem || null;
}

function imageHistoryUrl(item) {
  if (!item) return '';
  return item.download_url || item.image_url || item.after_image_url || '';
}

function imageHistoryTitle(item) {
  if (!item) return 'Изображение';
  if (item.prompt) return trimText(item.prompt, 88);
  if (item.provider === 'topaz_photo') {
    const preset = String(item.preset_slug || 'standard').trim();
    return `Topaz Upscale · ${preset}`;
  }
  return trimText(`${item.provider || 'image'} · ${item.model || ''}`, 88) || 'Изображение';
}

function imageCompareState(sourceItem = null) {
  const item = sourceItem || (state.image.panel === 'library' ? imageHistorySelectedItem() : null);
  const beforeUrl = item ? (item.before_image_url || item.source_image_url || '') : (state.image.beforeImageUrl || '');
  const afterUrl = item ? (item.after_image_url || imageHistoryUrl(item) || '') : (state.image.afterImageUrl || state.image.outputUrl || '');
  const compareMode = item ? !!item.compare_mode : !!state.image.compareMode;
  return {
    beforeUrl,
    afterUrl,
    compareMode: !!(compareMode && beforeUrl && afterUrl),
  };
}

function renderImageCompareStage(beforeUrl, afterUrl) {
  const comparePosition = Math.max(0, Math.min(100, Number(state.image.comparePosition || 50)));
  return `
    <div class="image-compare-shell">
      <div class="image-compare" style="--compare-position:${comparePosition};">
        <img class="image-compare-image image-compare-before" src="${escapeHtml(beforeUrl)}" alt="До апскейла">
        <div class="image-compare-after-wrap">
          <img class="image-compare-image image-compare-after" src="${escapeHtml(afterUrl)}" alt="После апскейла">
        </div>
        <div class="image-compare-divider" aria-hidden="true">
          <span class="image-compare-handle">↔</span>
        </div>
        <input class="image-compare-range" id="image_compareRange" type="range" min="0" max="100" step="1" value="${comparePosition}" aria-label="Сравнение до и после">
        <div class="image-compare-label before">До</div>
        <div class="image-compare-label after">После</div>
      </div>
      <div class="help-text image-compare-help">Потяни ползунок по изображению, чтобы посмотреть до и после апскейла.</div>
    </div>
  `;
}

function setImageComparePosition(nextValue, { commit = false } = {}) {
  const normalized = Math.max(0, Math.min(100, Number(nextValue || 0)));
  state.image.comparePosition = normalized;
  syncImageCompareUi();
  if (commit) saveState();
}

function syncImageCompareUi() {
  const compare = document.querySelector('.image-compare');
  if (!compare) return;
  const normalized = Math.max(0, Math.min(100, Number(state.image.comparePosition || 50)));
  compare.style.setProperty('--compare-position', String(normalized));
  const range = compare.querySelector('#image_compareRange');
  if (range) range.value = String(normalized);
}

function attachImageCompareInteractions() {
  const compare = document.querySelector('.image-compare');
  if (!compare || compare.dataset.dragReady === '1') return;
  compare.dataset.dragReady = '1';

  const range = compare.querySelector('#image_compareRange');
  const handle = compare.querySelector('.image-compare-handle');
  let dragging = false;
  let activePointerId = null;

  const updateFromClientX = (clientX, commit = false) => {
    const rect = compare.getBoundingClientRect();
    if (!rect.width) return;
    const ratio = ((clientX - rect.left) / rect.width) * 100;
    setImageComparePosition(ratio, { commit });
  };

  const stopDragging = (commit = true) => {
    if (!dragging) return;
    dragging = false;
    activePointerId = null;
    compare.classList.remove('is-dragging');
    if (commit) saveState();
  };

  compare.addEventListener('pointerdown', (event) => {
    dragging = true;
    activePointerId = event.pointerId;
    compare.classList.add('is-dragging');
    if (compare.setPointerCapture) {
      try { compare.setPointerCapture(event.pointerId); } catch (_) {}
    }
    updateFromClientX(event.clientX, false);
    event.preventDefault();
  });

  compare.addEventListener('pointermove', (event) => {
    if (!dragging) return;
    if (activePointerId !== null && event.pointerId !== activePointerId) return;
    updateFromClientX(event.clientX, false);
    event.preventDefault();
  });

  compare.addEventListener('pointerup', (event) => {
    if (activePointerId !== null && event.pointerId !== activePointerId) return;
    updateFromClientX(event.clientX, false);
    stopDragging(true);
  });

  compare.addEventListener('pointercancel', () => stopDragging(true));
  compare.addEventListener('lostpointercapture', () => stopDragging(true));
  compare.addEventListener('dragstart', (event) => event.preventDefault());

  if (range) {
    range.addEventListener('input', (event) => {
      setImageComparePosition(event.target.value, { commit: false });
    });
    range.addEventListener('change', (event) => {
      setImageComparePosition(event.target.value, { commit: true });
    });
  }

  if (handle) {
    handle.addEventListener('dragstart', (event) => event.preventDefault());
  }

  syncImageCompareUi();
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
        <span class="nav-icon">${renderStudioIcon(meta.icon)}</span>
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

  const hideTopActions = ['chat', 'video', 'image', 'voice', 'music'].includes(state.studio);
  if (topbarActions) topbarActions.style.display = hideTopActions ? 'none' : '';
  if (seedDemoBtn) seedDemoBtn.style.display = ['video', 'image', 'voice', 'music'].includes(state.studio) ? 'none' : '';
  if (globalRunBtn) globalRunBtn.style.display = ['chat', 'video', 'image', 'voice', 'music'].includes(state.studio) ? 'none' : '';
  if (resetStudioBtn) resetStudioBtn.style.display = ['video', 'image', 'voice'].includes(state.studio) ? 'none' : '';

  if (inspectorTitle) {
    inspectorTitle.textContent = state.studio === 'video' && state.video.panel === 'library' ? 'Библиотека видео' : 'Параметры';
  }
  if (inspectorEyebrow) {
    inspectorEyebrow.textContent = state.studio === 'video' && state.video.panel === 'library' ? 'Library' : 'Inspector';
  }

  document.getElementById('headerTitle').textContent = meta.title;
  document.getElementById('headerSubtitle').textContent = meta.subtitle;
  document.getElementById('headerEyebrow').textContent = meta.eyebrow || meta.title;
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
  attachImageCompareInteractions();
  initShowcaseMedia();
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
  stopVideoEditPolling();
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
  resetVideoEditorState();
  if (!keepPrompt) state.video.prompt = '';
  saveState();
}




function clearImageRunState({ keepPrompt = true, keepFiles = true } = {}) {
  state.image.outputUrl = '';
  state.image.downloadUrl = '';
  state.image.beforeImageUrl = '';
  state.image.afterImageUrl = '';
  state.image.compareMode = false;
  state.image.comparePosition = 50;
  state.image.generationId = '';
  state.image.errorText = '';
  state.image.isGenerating = false;
  state.image.statusText = 'Выбери режим, добавь изображения при необходимости и запусти генерацию.';
  if (!keepPrompt) state.image.prompt = '';
  if (!keepFiles) {
    ['image.sourceImage', 'image.baseImage'].forEach((key) => {
      revokeRuntimeFileValue(runtime.files[key]);
      delete runtime.files[key];
    });
    ['image_sourceImage', 'image_baseImage'].forEach((id) => {
      const input = document.getElementById(id);
      if (input) input.value = '';
    });
  }
  saveState();
}

function buildVideoEditorLaunchUrl() {
  const baseUrl = String(state.apiBaseUrl || window.location.origin || 'https://astrabot-tchj.onrender.com').replace(/\/$/, '');
  const params = new URLSearchParams();
  if (state.authToken) params.set('token', state.authToken);
  params.set('api_base', baseUrl);
  params.set('return_url', window.location.href);
  return `${baseUrl}/workspace/video-editor-v2?${params.toString()}`;
}

function renderVideoWorkspace() {
  const activeItem = historySelectedItem();
  const showHistoryVideo = state.video.panel === 'library' && activeItem && historyVideoUrl(activeItem);
  const previewUrl = showHistoryVideo ? historyVideoUrl(activeItem) : state.video.outputUrl;
  const statusLabel = videoStatusLabel(state.video.lastStatus);
  const loadingHeadline = getVideoLoadingHeadline(state.video.percent, state.video.lastStatus);
  const loadingSubline = getVideoLoadingSubline(state.video.percent, state.video.lastStatus);
  const assets = [
    mediaCard('Start frame', getFile('video.startFrame'), false, false, 'contain'),
    mediaCard('End frame', getFile('video.endFrame'), false, false, 'contain'),
    mediaCard('Last frame', getFile('video.lastFrame'), false, false, 'contain'),
    mediaCard('Avatar image', getFile('video.avatarImage'), false, false, 'contain'),
    mediaCard('Motion video', getFile('video.motionVideo'), true, false, 'contain'),
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
        <div class="result-card video-stage-card video-stage-card-plain">
          <div class="placeholder-stage video video-stage-clean">
            ${stageInner}
          </div>
        </div>
        <div class="video-editor-launch-card">
          <div class="video-editor-launch-copy">
            <div class="section-title" style="margin:0;">Новый редактор видео</div>
            <div class="help-text">Старый mini editor v1 убран из интерфейса. Для монтажа открой новый редактор с таймлайном.</div>
          </div>
          <div class="actions compact-gap" style="justify-content:center; flex-wrap:wrap; margin-top:14px;">
            <a class="btn primary" href="${escapeHtml(buildVideoEditorLaunchUrl())}">Открыть редактор видео(РАЗРАБОТКА)</a>
          </div>
        </div>
        ${assets ? `<div class="upload-grid two" style="margin-top:16px;">${assets}</div>` : ''}
      </div>
    </div>
  `;
}


function renderImageWorkspace() {
  syncImageSelection();
  const source = getFile('image.sourceImage');
  const base = getFile('image.baseImage');
  const historyItem = state.image.panel === 'library' ? imageHistorySelectedItem() : null;
  const compareState = imageCompareState(historyItem);
  const activeUrl = historyItem ? imageHistoryUrl(historyItem) : (state.image.outputUrl || compareState.afterUrl);
  const activeDownloadUrl = historyItem ? imageHistoryUrl(historyItem) : (state.image.downloadUrl || state.image.outputUrl || compareState.afterUrl);
  const assets = [
    mediaCard('Source image', source, false, false, 'contain'),
    mediaCard('Base image', base, false, false, 'contain'),
  ].filter(Boolean).join('');

  const stageInner = compareState.compareMode ? `
    <div class="video-stage-result image-stage-result">
      ${renderImageCompareStage(compareState.beforeUrl, compareState.afterUrl)}
      <div class="actions compact-gap" style="justify-content:center; flex-wrap:wrap; margin-top:14px;">
        <a class="btn primary" href="${escapeHtml(activeDownloadUrl || compareState.afterUrl)}" download>Скачать изображение</a>
        ${historyItem ? `<button class="btn outline" data-action="use-image-history-item" data-generation-id="${escapeHtml(historyItem.id || '')}">В рабочую зону</button>` : `<button class="btn outline" data-action="clear-image-run">Очистить результат</button>`}
      </div>
      ${historyItem ? `<div class="help-text" style="margin-top:10px;">Открыт сохранённый результат из истории Image Studio.</div>` : ''}
    </div>
  ` : activeUrl ? `
    <div class="video-stage-result image-stage-result">
      <img class="preview-media image-preview-media" src="${escapeHtml(activeUrl)}" alt="Generated image">
      <div class="actions compact-gap" style="justify-content:center; flex-wrap:wrap; margin-top:14px;">
        <a class="btn primary" href="${escapeHtml(activeDownloadUrl || activeUrl)}" download>Скачать изображение</a>
        ${historyItem ? `<button class="btn outline" data-action="use-image-history-item" data-generation-id="${escapeHtml(historyItem.id || '')}">В рабочую зону</button>` : `<button class="btn outline" data-action="clear-image-run">Очистить результат</button>`}
      </div>
      ${historyItem ? `<div class="help-text" style="margin-top:10px;">Открыт сохранённый результат из истории Image Studio.</div>` : ''}
    </div>
  ` : (state.image.isGenerating ? `
    <div class="image-loading-shell">
      <div class="video-loader-shell video-loader-shell-scan">
        <div class="video-scan-stage">
          <div class="video-scan-grid"></div>
          <div class="video-scan-sweep"></div>
          <div class="video-scan-glow"></div>
          <div class="video-loader">
            <div class="video-loader-ring"></div>
            <div class="video-loader-ring ring-2"></div>
            <div class="video-loader-ring ring-3"></div>
            <div class="video-loader-core">✦</div>
          </div>
        </div>
        <div class="video-loader-copy">
          <strong>Собираю изображение</strong>
          <div>${escapeHtml(state.image.statusText || 'Жди финальный результат в центральной рабочей зоне.')}</div>
        </div>
      </div>
    </div>
  ` : `
    <div class="empty-copy">
      <strong>Изображение появится здесь</strong>
      <div>Справа выбери семейство, режим, добавь входные изображения при необходимости и запусти генерацию. Для сохранённых результатов используй историю изображений в правой панели.</div>
    </div>
  `);

  return `
    <div class="workspace-grid single image-workspace-grid">
      <div class="workspace-main scroll image-workspace-main">
        <div class="result-card image-stage-card image-stage-card-plain">
          <div class="placeholder-stage image image-stage-clean">
            ${stageInner}
          </div>
        </div>
        ${assets ? `<div class="upload-grid two" style="margin-top:16px;">${assets}</div>` : ''}
        ${state.image.errorText ? `<div class="planner-card" style="margin-top:16px;"><h4>Ошибка</h4><div class="help-text">${escapeHtml(state.image.errorText)}</div></div>` : ''}
      </div>
    </div>
  `;
}

function renderVoiceWorkspace() {
  const selectedVoice = selectedVoiceData();
  const historyItem = voiceHistorySelectedItem();
  const activeAudioUrl = state.voice.audioUrl || voiceHistoryAudioUrl(historyItem);
  const activeDownloadUrl = state.voice.downloadUrl || state.voice.audioUrl || voiceHistoryDownloadUrl(historyItem);
  const voiceName = selectedVoice?.name || historyItem?.voice_name || 'Голос не выбран';
  const textLength = String(state.voice.text || '').length;
  const hasAudio = !!activeAudioUrl;

  return `
    <div class="workspace-grid single voice-workspace-grid">
      <div class="workspace-main scroll voice-workspace-main">
        <div class="voice-editor-card">
          <div class="voice-editor-head">
            <div>
              <h3>Текст для озвучки</h3>
              <p>Главная рабочая зона Voice Studio: пишешь текст, запускаешь генерацию и сразу получаешь готовый звук ниже.</p>
            </div>
            <div class="voice-toolbar-meta">
              <span class="badge muted">${textLength} симв.</span>
              <span class="badge muted">${escapeHtml(voiceEstimatedDurationText())}</span>
              <span class="badge ${selectedVoice || historyItem ? 'ok' : 'warn'}">${escapeHtml(voiceName)}</span>
            </div>
          </div>

          <textarea id="voice_text" class="voice-textarea" placeholder="Вставь текст для озвучки. Например: приветствие, дикторский текст для рекламы, voice-over для видео, CTA, сценарий ролика...">${escapeHtml(state.voice.text || '')}</textarea>

          <div class="voice-toolbar">
            <div class="help-text">Выбранный голос: <strong>${escapeHtml(voiceName)}</strong> · ${escapeHtml(voiceModelLabel(state.voice.modelId))} · ${escapeHtml(voiceOutputLabel(state.voice.outputFormat))}</div>
            <div class="actions compact-gap" style="flex-wrap:wrap;">
              <button class="btn outline" data-action="clear-voice-stage">Очистить результат</button>
            </div>
          </div>
        </div>

        <div class="voice-result-card">
          <div class="field-head" style="margin-bottom:14px; align-items:flex-start; gap:12px;">
            <div>
              <h4 style="margin:0 0 6px;">Результат</h4>
              <div class="help-text">Здесь появляется готовый audio-файл с прослушиванием и скачиванием.</div>
            </div>
            ${state.voice.lastGeneratedAt ? `<span class="badge muted">${escapeHtml(formatDate(state.voice.lastGeneratedAt))}</span>` : ''}
          </div>

          ${hasAudio ? `
            <div class="voice-result-shell">
              <div class="voice-result-summary">
                <strong>${escapeHtml(voiceName)}</strong>
                <small>${escapeHtml(voiceModelLabel(state.voice.modelId))}<br>${escapeHtml(voiceOutputLabel(state.voice.outputFormat))}</small>
              </div>
              <audio class="voice-audio-player" controls src="${escapeHtml(activeAudioUrl)}"></audio>
              <div class="actions compact-gap" style="flex-wrap:wrap;">
                <a class="btn primary" href="${escapeHtml(activeDownloadUrl || activeAudioUrl)}" download="${escapeHtml(voiceDownloadFilename())}">Скачать звук</a>
              </div>
              ${historyItem ? `<div class="help-text">Открыта сохранённая озвучка из истории Voice Studio.</div>` : ''}
            </div>
          ` : `
            <div class="voice-result-empty">
              <strong>${state.voice.isGenerating ? 'Генерирую аудио...' : 'Пока результата нет'}</strong>
              <div>${state.voice.isGenerating ? 'Как только ElevenLabs вернёт файл, здесь появится плеер и кнопка скачивания.' : 'Напиши текст, выбери голос справа и запусти генерацию.'}</div>
            </div>
          `}

          ${state.voice.errorText ? `<div class="planner-card" style="margin-top:16px;"><h4>Ошибка</h4><div class="help-text">${escapeHtml(state.voice.errorText)}</div></div>` : ''}
        </div>
      </div>
    </div>
  `;
}

function renderVoiceInspector() {
  const selectedVoice = selectedVoiceData();
  const voices = Array.isArray(state.voice.voices) ? state.voice.voices : [];
  const historyItems = Array.isArray(state.voiceHistory.items) ? state.voiceHistory.items : [];
  const selectedHistoryId = String(state.voiceHistory.selectedId || '').trim();
  const modelOptions = [
    ['eleven_multilingual_v2', 'Eleven Multilingual v2'],
    ['eleven_flash_v2_5', 'Eleven Flash v2.5'],
    ['eleven_turbo_v2_5', 'Eleven Turbo v2.5'],
  ];
  const outputOptions = [
    ['mp3_44100_128', 'MP3 · 128 kbps'],
    ['mp3_44100_192', 'MP3 · 192 kbps'],
  ];
  const languageOptions = [
    ['auto', 'Авто'],
    ['ru', 'Русский'],
    ['en', 'English'],
    ['uk', 'Українська'],
    ['de', 'Deutsch'],
    ['fr', 'Français'],
    ['es', 'Español'],
    ['it', 'Italiano'],
    ['pt', 'Português'],
    ['pl', 'Polski'],
    ['tr', 'Türkçe'],
    ['ar', 'العربية'],
    ['hi', 'हिन्दी'],
    ['zh', '中文'],
    ['ja', '日本語'],
    ['ko', '한국어'],
  ];

  return `
    <div class="inspector-card voice-side-stack">
      <div class="field-head" style="margin-bottom:12px;">
        <h4 style="margin:0;">Выбор голоса</h4>
        <span class="badge muted">${voices.length}</span>
      </div>
      <div class="voice-voice-grid">
        ${voices.length ? voices.map((voice) => `
          <button class="voice-card-btn ${state.voice.voiceId === voice.voice_id ? 'active' : ''}" data-action="select-voice-card" data-voice-id="${escapeHtml(voice.voice_id)}">
            <strong>${escapeHtml(voice.name || 'Voice')}</strong>
            <span>${escapeHtml(state.voice.voiceId === voice.voice_id ? 'Выбран' : 'Нажми для выбора')}</span>
          </button>
        `).join('') : `<div class="empty-state">Голоса ещё не загружены. Обнови страницу или перезайди во вкладку Voice Studio.</div>`}
      </div>
    </div>

    <div class="inspector-card voice-config-card">
      <div class="voice-config-grid">
        ${fieldSelect('Модель', 'voice_modelId', state.voice.modelId, modelOptions)}
        ${fieldSelect('Формат', 'voice_outputFormat', state.voice.outputFormat, outputOptions)}
        ${fieldSelect('Язык', 'voice_languageCode', state.voice.languageCode || 'auto', languageOptions)}
      </div>
      <div class="help-text" style="margin-top:12px;">Текущий голос: <strong>${escapeHtml(selectedVoice?.name || 'не выбран')}</strong> · Язык: <strong>${escapeHtml(voiceLanguageLabel(state.voice.languageCode || 'auto'))}</strong></div>
    </div>

    <div class="inspector-card voice-advanced-card">
      <div class="field-head" style="margin-bottom:12px; align-items:flex-start; gap:12px;">
        <div>
          <h4 style="margin:0 0 6px;">Расширенные настройки</h4>
          <div class="help-text">Эти параметры применяются только к новому запуску на сайте и не переписывают историю.</div>
        </div>
        <label class="toggle-pill">
          <input id="voice_manualVoiceSettings" type="checkbox" ${state.voice.manualVoiceSettings ? 'checked' : ''}>
          <span>Ручные voice settings</span>
        </label>
      </div>

      <div class="voice-range-stack ${state.voice.manualVoiceSettings ? '' : 'is-disabled'}">
        <label class="voice-range-row">
          <div class="voice-range-top">
            <span>Stability</span>
            <strong>${escapeHtml(voiceSettingsBadge(state.voice.stability))}</strong>
          </div>
          <input id="voice_stability" type="range" min="0" max="1" step="0.01" value="${escapeHtml(String(state.voice.stability))}" ${state.voice.manualVoiceSettings ? '' : 'disabled'}>
          <small>Ниже — больше вариативности и эмоций. Выше — стабильнее и ровнее речь.</small>
        </label>

        <label class="voice-range-row">
          <div class="voice-range-top">
            <span>Similarity boost</span>
            <strong>${escapeHtml(voiceSettingsBadge(state.voice.similarityBoost))}</strong>
          </div>
          <input id="voice_similarityBoost" type="range" min="0" max="1" step="0.01" value="${escapeHtml(String(state.voice.similarityBoost))}" ${state.voice.manualVoiceSettings ? '' : 'disabled'}>
          <small>Насколько плотно модель держится исходного тембра выбранного голоса.</small>
        </label>

        <label class="voice-range-row">
          <div class="voice-range-top">
            <span>Style</span>
            <strong>${escapeHtml(voiceSettingsBadge(state.voice.style))}</strong>
          </div>
          <input id="voice_style" type="range" min="0" max="1" step="0.01" value="${escapeHtml(String(state.voice.style))}" ${state.voice.manualVoiceSettings ? '' : 'disabled'}>
          <small>Добавляет больше стилевой выразительности. Чем выше, тем заметнее подача.</small>
        </label>

        <label class="voice-range-row">
          <div class="voice-range-top">
            <span>Speed</span>
            <strong>${escapeHtml(voiceSettingsBadge(state.voice.speed))}</strong>
          </div>
          <input id="voice_speed" type="range" min="0.7" max="1.2" step="0.01" value="${escapeHtml(String(state.voice.speed))}" ${state.voice.manualVoiceSettings ? '' : 'disabled'}>
          <small>1.0 — стандартная скорость. Ниже — медленнее, выше — быстрее.</small>
        </label>

        <label class="toggle-pill">
          <input id="voice_useSpeakerBoost" type="checkbox" ${state.voice.useSpeakerBoost ? 'checked' : ''} ${state.voice.manualVoiceSettings ? '' : 'disabled'}>
          <span>Speaker boost</span>
        </label>
      </div>
    </div>

    <div class="inspector-card voice-history-panel">
      <div class="field-head" style="margin-bottom:12px; align-items:flex-start; gap:10px;">
        <h4 style="margin:0;">История озвучек</h4>
        <div class="actions compact-gap" style="margin-top:0; flex-wrap:wrap; justify-content:flex-end;">
          <span class="badge muted">${historyItems.length}</span>
          <button class="btn ghost small" data-action="refresh-voice-history">Обновить</button>
        </div>
      </div>
      ${state.voiceHistory.loading ? `<div class="help-text">Загружаю историю...</div>` : historyItems.length ? `
        <div class="voice-history-list">
          ${historyItems.map((item) => `
            <div class="history-item compact ${selectedHistoryId === String(item.id || '') ? 'active' : ''}">
              <div class="history-item-row">
                <strong>${escapeHtml(item.voice_name || 'Voice')} · ${escapeHtml(voiceOutputLabel(item.output_format || ''))}</strong>
                <span class="badge ${String(item.status || '') === 'completed' ? 'ok' : 'warn'}">${escapeHtml(item.status || '—')}</span>
              </div>
              <small>${escapeHtml(trimText(item.text || '', 120))}</small>
              <div class="help-text" style="margin-top:8px;">${escapeHtml(formatDate(item.completed_at || item.created_at || ''))}</div>
              <div class="actions compact-gap" style="margin-top:10px; flex-wrap:wrap;">
                <button class="btn ghost small" data-action="use-voice-history-item" data-generation-id="${escapeHtml(item.id || '')}">Открыть</button>
                <button class="btn ghost small danger" data-action="delete-voice-history-item" data-generation-id="${escapeHtml(item.id || '')}">Удалить</button>
              </div>
            </div>
          `).join('')}
        </div>
      ` : `<div class="voice-history-empty">Пока нет сохранённых озвучек. После первой генерации они появятся здесь.</div>`}
      ${state.voiceHistory.lastError ? `<div class="help-text" style="margin-top:10px; color:#ff9b9b;">${escapeHtml(state.voiceHistory.lastError)}</div>` : ''}
    </div>

    <div class="inspector-card">
      <button class="btn primary full ${state.voice.isGenerating ? 'loading' : ''}" data-action="run-voice" ${state.voice.isGenerating ? 'disabled' : ''}>${state.voice.isGenerating ? 'Генерация...' : 'Сгенерировать звук'}</button>
      <div class="help-text" style="margin-top:10px;">Введённый текст из центра отправится в TTS, сохранится в истории и вернётся готовым аудио-файлом прямо в рабочую область.</div>
    </div>
  `;
}


function musicSelectedItem() {
  return state.musicHistory.selectedItem || state.musicHistory.items.find((item) => item.id === state.musicHistory.selectedId) || null;
}

function musicCurrentTracks() {
  const selected = musicSelectedItem();
  if (selected && Array.isArray(selected.tracks) && selected.tracks.length) return selected.tracks;
  return Array.isArray(state.music.results) ? state.music.results : [];
}

function musicLastAnswerText() {
  const messages = Array.isArray(state.music.songwriter.messages) ? state.music.songwriter.messages : [];
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const item = messages[i];
    if (item && item.role === 'assistant' && String(item.content || '').trim()) return String(item.content || '').trim();
  }
  return String(state.music.songwriter.lastAnswer || '').trim();
}

function musicSourceTextForSongwriter() {
  const mode = state.music.mode === 'lyrics' ? 'lyrics' : 'idea';
  const source = mode === 'lyrics' ? state.music.lyricsText : state.music.ideaText;
  return String(source || '').trim();
}

function ensureMusicCompatibility(options = {}) {
  const { preserveLyricsTab = true } = options;
  if (state.music.ai !== 'udio' && state.music.ai !== 'suno') state.music.ai = 'suno';
  if (state.music.ai === 'udio') {
    state.music.backend = 'piapi';
    state.music.mode = 'idea';
    if (!preserveLyricsTab && state.music.activeTab === 'lyrics') state.music.activeTab = 'idea';
  } else if (!['sunoapi', 'piapi', 'auto'].includes(state.music.backend)) {
    state.music.backend = 'sunoapi';
  }
  if (!['idea', 'lyrics', 'songwriter', 'results'].includes(state.music.activeTab)) state.music.activeTab = 'idea';
}

function setMusicTab(tab) {
  if (!['idea', 'lyrics', 'songwriter', 'results'].includes(String(tab || ''))) return;
  if (tab === 'idea') state.music.mode = 'idea';
  if (tab === 'lyrics' && state.music.ai === 'suno') state.music.mode = 'lyrics';
  if (tab === 'lyrics' && state.music.ai === 'udio') {
    state.music.mode = 'idea';
    toast('info', 'Udio работает через Idea', 'Текст песни можно хранить в Lyrics, но генерация Udio всё равно запускается через идею.');
  }
  state.music.activeTab = tab;
  saveState();
  render();
}

function renderMusicTrackCards(tracks = [], options = {}) {
  const emptyText = options.emptyText || 'После первой успешной генерации здесь появятся карточки треков.';
  if (!Array.isArray(tracks) || !tracks.length) {
    return `<div class="music-empty-card">${escapeHtml(emptyText)}</div>`;
  }
  return tracks.map((track, index) => {
    const title = track.title || `Track ${index + 1}`;
    const audioUrl = track.audio_url || track.download_url || '';
    const videoUrl = track.video_url || '';
    const coverUrl = track.cover_url || '';
    return `
      <div class="music-track-card">
        <div class="music-track-top">
          <div>
            <strong>${escapeHtml(title)}</strong>
            <div class="help-text">Трек #${index + 1}${track.provider_track_id ? ` · ${escapeHtml(track.provider_track_id)}` : ''}</div>
          </div>
          ${coverUrl ? `<img class="music-track-cover" src="${escapeHtml(coverUrl)}" alt="${escapeHtml(title)}">` : `<div class="music-track-cover placeholder">♪</div>`}
        </div>
        ${audioUrl ? `<audio controls preload="none" src="${escapeHtml(audioUrl)}"></audio>` : `<div class="help-text">У этого результата пока нет audio_url.</div>`}
        <div class="actions compact-gap" style="margin-top:10px; flex-wrap:wrap;">
          ${audioUrl ? `<a class="btn ghost small" href="${escapeHtml(audioUrl)}" target="_blank" rel="noopener">Открыть MP3</a>` : ''}
          ${videoUrl ? `<a class="btn ghost small" href="${escapeHtml(videoUrl)}" target="_blank" rel="noopener">Открыть MP4</a>` : ''}
          <button class="btn outline small" data-action="send-music-to-chat" data-text="${encodeURIComponent(track.title || '')}">В ChatGPT</button>
        </div>
      </div>
    `;
  }).join('');
}


function musicExpectedTrackCount() {
  return state.music.ai === 'suno' ? 2 : 1;
}

function musicTrackLabel(count) {
  const value = Number(count || 0);
  if (value % 10 === 1 && value % 100 !== 11) return 'трек';
  if ([2, 3, 4].includes(value % 10) && ![12, 13, 14].includes(value % 100)) return 'трека';
  return 'треков';
}

function musicRunButtonLabel() {
  if (state.music.isGenerating) {
    return state.music.ai === 'suno' ? 'Генерирую 2 трека...' : 'Генерирую музыку...';
  }
  return state.music.ai === 'suno'
    ? 'Сгенерировать 2 трека · 2 токена'
    : 'Сгенерировать музыку · 2 токена';
}

function musicRunHelperText() {
  if (state.music.ai === 'suno') {
    return 'Suno за один запуск возвращает сразу 2 трека. Backend скрыт и по умолчанию используется SunoAPI.';
  }
  return 'Udio работает через PiAPI и запускается через режим Idea. Lyrics можно держать как черновик для GPT и ручной правки.';
}

function musicLiveStatusText() {
  const status = String(state.music.status || '').toLowerCase();
  const tracks = musicCurrentTracks();
  if (status === 'completed' && tracks.length) {
    return `Готово: получено ${tracks.length} ${musicTrackLabel(tracks.length)}.`;
  }
  if (status === 'failed') {
    return 'Генерация завершилась ошибкой.';
  }
  if (['queued', 'processing', 'running', 'in_progress'].includes(status) || state.music.isGenerating) {
    return state.music.ai === 'suno'
      ? 'Генерирую музыку. После завершения Suno должен вернуть 2 трека.'
      : 'Генерирую музыку. После завершения результат появится в этой рабочей зоне.';
  }
  return state.music.statusText || 'Собери идею или lyrics, настрой параметры справа и запускай генерацию.';
}

function renderMusicLiveBoard() {
  const currentTracks = musicCurrentTracks();
  const historyItems = Array.isArray(state.musicHistory.items) ? state.musicHistory.items.slice(0, 4) : [];
  const expectedCount = musicExpectedTrackCount();
  const statusTone = state.music.status === 'completed' ? 'ok' : state.music.status === 'failed' ? 'warn' : 'muted';

  return `
    <div class="music-live-column">
      <div class="music-live-card">
        <div class="field-head">
          <h4>Рабочая зона</h4>
          <span class="badge ${statusTone}">${escapeHtml(state.music.status || 'idle')}</span>
        </div>
        <div class="music-live-status">
          <strong>${escapeHtml(musicLiveStatusText())}</strong>
          <small>${escapeHtml(state.music.ai === 'udio' ? 'Udio' : 'Suno')} · ${escapeHtml(state.music.mode)}${state.music.generationId ? ` · id ${escapeHtml(state.music.generationId)}` : ''}</small>
        </div>
        ${state.music.errorText ? `<div class="music-warning">${escapeHtml(state.music.errorText)}</div>` : ''}
      </div>

      <div class="music-live-card">
        <div class="field-head">
          <h4>Треки этого запуска</h4>
          <span class="badge muted">${currentTracks.length}/${expectedCount}</span>
        </div>
        ${renderMusicTrackCards(currentTracks, {
          emptyText: state.music.ai === 'suno'
            ? 'После запуска здесь появятся 2 готовых трека Suno.'
            : 'После запуска здесь появится готовый трек.'
        })}
      </div>

      <div class="music-live-card">
        <div class="field-head">
          <h4>Последние запуски</h4>
          <button class="btn ghost small" data-action="refresh-music-history">Обновить</button>
        </div>
        <div class="music-live-history">
          ${historyItems.length ? historyItems.map((item) => `
            <button class="music-history-compact ${state.musicHistory.selectedId === item.id ? 'active' : ''}" data-action="use-music-history-item" data-generation-id="${escapeHtml(item.id || '')}">
              <strong>${escapeHtml(item.title || (item.ai === 'udio' ? 'Udio run' : 'Suno run'))}</strong>
              <small>${escapeHtml(formatDate(item.completed_at || item.created_at || ''))}</small>
            </button>
          `).join('') : `<div class="music-empty-card">История пока пуста.</div>`}
        </div>
      </div>
    </div>
  `;
}


function renderMusicWorkspace() {
  ensureMusicCompatibility({ preserveLyricsTab: true });
  const tab = state.music.activeTab || 'idea';
  const selected = musicSelectedItem();
  const historyItems = Array.isArray(state.musicHistory.items) ? state.musicHistory.items : [];
  const isLyricsLocked = state.music.ai === 'udio';
  const songwriterMessages = Array.isArray(state.music.songwriter.messages) ? state.music.songwriter.messages : [];
  const songThread = songwriterMessages.length ? songwriterMessages.map((msg) => `
    <div class="music-chat-bubble ${msg.role === 'user' ? 'user' : 'assistant'}">
      <div>${escapeHtml(msg.content || '')}</div>
    </div>
  `).join('') : `<div class="music-empty-card">Пока пусто. Напиши задачу вроде: «сделай цепкий припев для танцевальной школы, 2 куплета и припев».</div>`;

  return `
    <div class="workspace-grid single music-workspace-grid">
      <div class="workspace-main scroll">
        <div class="music-stage-grid">
          <div class="music-stage-main">
            <div class="music-tabs">
              <button class="music-tab ${tab === 'idea' ? 'active' : ''}" data-action="music-set-tab" data-tab="idea">Idea</button>
              <button class="music-tab ${tab === 'lyrics' ? 'active' : ''}" data-action="music-set-tab" data-tab="lyrics">Lyrics</button>
              <button class="music-tab ${tab === 'songwriter' ? 'active' : ''}" data-action="music-set-tab" data-tab="songwriter">Songwriter</button>
              <button class="music-tab ${tab === 'results' ? 'active' : ''}" data-action="music-set-tab" data-tab="results">Results</button>
            </div>

            ${tab === 'idea' ? `
              <div class="music-panel">
                <div class="field-head">
                  <h4>Идея трека</h4>
                  <div class="actions compact-gap">
                    <button class="btn ghost small" data-action="music-fill-template">Шаблон</button>
                    <button class="btn outline small" data-action="music-open-songwriter">Открыть Songwriter</button>
                  </div>
                </div>
                <div class="help-text">Здесь держим задачу для генератора: для чего нужен трек, какой вайб, кому адресован, какой нужен хук, темп, под что музыка будет использоваться.</div>
                <textarea id="music_ideaText" rows="16" placeholder="Например: современный вдохновляющий трек для рекламы школы танцев, female vocal, эмоциональный припев, уверенный рост к финалу, ощущение большого города, premium, catchy hook...">${escapeHtml(state.music.ideaText)}</textarea>
                <div class="music-helper-row">
                  <div class="music-mini-card">
                    <strong>Что писать здесь</strong>
                    <small>Задача, настроение, назначение трека, референсы, pacing, CTA, образ бренда.</small>
                  </div>
                  <div class="music-mini-card">
                    <strong>Что не писать здесь</strong>
                    <small>Подробный финальный текст песни. Для этого есть Lyrics и Songwriter.</small>
                  </div>
                </div>
              </div>
            ` : ''}

            ${tab === 'lyrics' ? `
              <div class="music-panel">
                <div class="field-head">
                  <h4>Текст песни</h4>
                  <div class="actions compact-gap">
                    <button class="btn ghost small" data-action="music-open-songwriter">Написать через GPT</button>
                    <button class="btn outline small" data-action="music-apply-last-answer" data-target="lyrics">Вставить ответ GPT</button>
                  </div>
                </div>
                ${isLyricsLocked ? `<div class="music-warning">Udio не запускается напрямую из текста. Но этот блок всё равно полезен как редактор черновика: можно собрать lyrics через GPT, потом превратить их в idea.</div>` : `<div class="help-text">Для Suno здесь можно держать уже готовые куплеты, припев, bridge и структуру.</div>`}
                <textarea id="music_lyricsText" rows="18" placeholder="[Verse]
...

[Chorus]
...">${escapeHtml(state.music.lyricsText)}</textarea>
              </div>
            ` : ''}

            ${tab === 'songwriter' ? `
              <div class="music-panel">
                <div class="field-head">
                  <h4>GPT Songwriter</h4>
                  <div class="actions compact-gap">
                    <button class="btn ghost small" data-action="songwriter-reset">Сбросить</button>
                    <button class="btn outline small" data-action="songwriter-seed">Стартовые вопросы</button>
                  </div>
                </div>
                <div class="help-text">Это рабочий инструмент для текста песни: варианты припева, правки, структуры и переписывание под нужный жанр.</div>
                <div class="music-chat-thread">${songThread}</div>
                <div class="music-chat-composer">
                  <textarea id="music_songwriterInput" rows="5" placeholder="Напиши задачу GPT: например «сделай русский коммерческий припев, чтобы легко запоминался, 2 куплета + припев, тема — школа танцев»">${escapeHtml(state.music.songwriter.input || '')}</textarea>
                  <div class="actions compact-gap" style="flex-wrap:wrap;">
                    <button class="btn primary ${state.music.songwriter.loading ? 'loading' : ''}" data-action="songwriter-send" ${state.music.songwriter.loading ? 'disabled' : ''}>${state.music.songwriter.loading ? 'Генерация...' : 'Сгенерировать текст'}</button>
                    <button class="btn ghost" data-action="music-apply-last-answer" data-target="lyrics">Вставить в Lyrics</button>
                    <button class="btn outline" data-action="music-apply-last-answer" data-target="idea">Вставить в Idea</button>
                  </div>
                </div>
              </div>
            ` : ''}

            ${tab === 'results' ? `
              <div class="music-panel">
                <div class="field-head">
                  <h4>История и сохранённые запуски</h4>
                  <div class="actions compact-gap">
                    <button class="btn ghost small" data-action="refresh-music-history">Обновить</button>
                    <button class="btn outline small" data-action="music-open-history">Открыть последний</button>
                  </div>
                </div>
                <div class="music-history-list">
                  ${historyItems.length ? historyItems.map((item) => `
                    <div class="music-history-item ${state.musicHistory.selectedId === item.id ? 'active' : ''}">
                      <div class="music-history-head">
                        <strong>${escapeHtml(item.title || item.ai || 'Music run')}</strong>
                        <span class="badge ${item.status === 'completed' ? 'ok' : item.status === 'failed' ? 'warn' : 'muted'}">${escapeHtml(item.status || 'queued')}</span>
                      </div>
                      <small>${escapeHtml(trimText(item.mode === 'lyrics' ? item.lyrics_text : item.idea_text, 92) || 'Без текста')}</small>
                      <div class="help-text">${escapeHtml(formatDate(item.completed_at || item.created_at || ''))}</div>
                      <div class="actions compact-gap" style="margin-top:10px; flex-wrap:wrap;">
                        <button class="btn ghost small" data-action="use-music-history-item" data-generation-id="${escapeHtml(item.id || '')}">Открыть</button>
                        <button class="btn ghost small danger" data-action="delete-music-history-item" data-generation-id="${escapeHtml(item.id || '')}">Удалить</button>
                      </div>
                    </div>
                  `).join('') : `<div class="music-empty-card">История пока пуста. После первой генерации здесь появятся сохранённые запуски.</div>`}
                </div>
                ${selected ? `
                  <div class="music-selected-meta">
                    <div class="music-mini-card">
                      <strong>Выбранный запуск</strong>
                      <small>AI: ${escapeHtml(selected.ai || '—')} · mode: ${escapeHtml(selected.mode || '—')} · tracks: ${escapeHtml(String((selected.tracks || []).length || 0))}</small>
                    </div>
                    <div class="music-mini-card">
                      <strong>Поля</strong>
                      <small>Title: ${escapeHtml(selected.title || '—')} · Tags: ${escapeHtml(selected.tags || '—')} · Language: ${escapeHtml(selected.language || '—')}</small>
                    </div>
                  </div>
                ` : ''}
              </div>
            ` : ''}
          </div>

          ${renderMusicLiveBoard()}
        </div>
      </div>
    </div>
  `;
}

function renderMusicInspector() {
  ensureMusicCompatibility({ preserveLyricsTab: true });
  return `
    <div class="music-inspector">
      <div class="inspector-card">
        <div class="field-head"><h4>AI модель</h4><span class="badge muted">${state.music.ai === 'suno' ? '2 трека' : 'PiAPI'}</span></div>
        <div class="music-ai-switch">
          <button class="music-ai-pill ${state.music.ai === 'suno' ? 'active' : ''}" data-action="music-set-ai" data-value="suno" type="button">
            <span class="music-ai-title">Suno</span>
            <small>2 трека за запуск · SunoAPI</small>
          </button>
          <button class="music-ai-pill ${state.music.ai === 'udio' ? 'active' : ''}" data-action="music-set-ai" data-value="udio" type="button">
            <span class="music-ai-title">Udio</span>
            <small>Idea mode · PiAPI</small>
          </button>
        </div>
      </div>

      <div class="inspector-card">
        <div class="field-head"><h4>Режим генерации</h4><span class="badge muted">${state.music.mode}</span></div>
        <div class="seg music-mode-switch">
          <button class="segbtn ${state.music.mode === 'idea' ? 'active' : ''}" data-action="music-set-tab" data-tab="idea" type="button">Idea</button>
          <button class="segbtn ${(state.music.mode === 'lyrics' && state.music.ai === 'suno') ? 'active' : ''}" data-action="music-set-tab" data-tab="lyrics" type="button" ${state.music.ai === 'udio' ? 'disabled' : ''}>Lyrics</button>
        </div>
        <div class="help-text" style="margin-top:10px;">${escapeHtml(musicRunHelperText())}</div>
      </div>

      <div class="inspector-card">
        <div class="field-head"><h4>Метаданные</h4><button class="btn ghost small" data-action="music-use-idea-as-title">Из идеи</button></div>
        <label>Title<input id="music_title" value="${escapeHtml(state.music.title)}" placeholder="Название трека"></label>
        <label>Tags / genre<input id="music_tags" value="${escapeHtml(state.music.tags)}" placeholder="dance-pop, cinematic, female vocal"></label>
        <label>Language
          <select id="music_language">
            <option value="ru" ${state.music.language === 'ru' ? 'selected' : ''}>Русский</option>
            <option value="en" ${state.music.language === 'en' ? 'selected' : ''}>English</option>
            <option value="auto" ${state.music.language === 'auto' ? 'selected' : ''}>Auto</option>
          </select>
        </label>
        <label>Mood<input id="music_mood" value="${escapeHtml(state.music.mood)}" placeholder="uplifting, premium, energetic"></label>
        <label>References<textarea id="music_references" rows="4" placeholder="Референсы, бренды, артисты, вайб">${escapeHtml(state.music.references)}</textarea></label>
        <label class="toggle-pill">
          <input id="music_instrumental" type="checkbox" ${state.music.instrumental ? 'checked' : ''}>
          <span>Инструментал без вокала</span>
        </label>
      </div>

      <div class="inspector-card">
        <div class="field-head"><h4>Запуск</h4><span class="badge ${state.music.status === 'completed' ? 'ok' : state.music.status === 'failed' ? 'warn' : 'muted'}">${escapeHtml(state.music.status || 'idle')}</span></div>
        <button class="btn primary full music-run-btn ${state.music.isGenerating ? 'loading' : ''}" data-action="run-music" ${state.music.isGenerating ? 'disabled' : ''}>${escapeHtml(musicRunButtonLabel())}</button>
        <div class="actions compact-gap" style="margin-top:10px; flex-wrap:wrap;">
          <button class="btn ghost small" data-action="music-open-songwriter">GPT songwriter</button>
          <button class="btn outline small" data-action="refresh-music-history">Обновить history</button>
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
              <button class="history-delete-btn" data-action="delete-history-item" data-generation-id="${escapeHtml(item.id || '')}" title="Удалить из истории" aria-label="Удалить из истории">×</button>
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

function mediaCard(title, asset, isVideo = false, multiple = false, fit = 'cover') {
  if (!asset) return '';
  const thumbClass = `asset-thumb ${fit === 'contain' ? 'fit-contain' : ''}`.trim();
  if (multiple && Array.isArray(asset)) {
    return `
      <div class="asset-card">
        <h4>${escapeHtml(title)}</h4>
        <div class="upload-grid two">
          ${asset.map((a) => `<img class="${thumbClass}" src="${escapeHtml(a.url)}" alt="${escapeHtml(a.name)}">`).join('')}
        </div>
        <small>${asset.length} файлов</small>
      </div>
    `;
  }
  return `
    <div class="asset-card">
      <h4>${escapeHtml(title)}</h4>
      ${isVideo ? `<video class="${thumbClass}" src="${escapeHtml(asset.url)}" controls></video>` : `<img class="${thumbClass}" src="${escapeHtml(asset.url)}" alt="${escapeHtml(asset.name)}">`}
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
  const providerOptions = Object.entries(VIDEO_REGISTRY).map(([id, provider]) => `<option value="${escapeHtml(id)}" ${state.video.provider === id ? 'selected' : ''}>${escapeHtml(provider.name)}</option>`).join('');
  const modelOptions = Object.entries(videoProviderConfig().models).map(([id, model]) => `<option value="${escapeHtml(id)}" ${state.video.model === id ? 'selected' : ''}>${escapeHtml(model.name)}</option>`).join('');
  const modeOptions = Object.entries(videoModelConfig().modes).map(([id, mode]) => `<option value="${escapeHtml(id)}" ${state.video.mode === id ? 'selected' : ''}>${escapeHtml(mode.name)}</option>`).join('');

  if (state.video.panel === 'library') {
    const items = state.history.items || [];
    return `
      <div class="inspector-card">
        <div class="field-head" style="margin-bottom:12px;"><div class="section-title" style="margin:0;">Библиотека видео</div><button class="btn ghost small" data-action="show-video-params">Параметры</button></div>
        <div class="help-text">При нажатии на историю параметры скрываются, а справа открывается библиотека видео.</div>
        <div class="mini-list" style="margin-top:14px;">
          ${items.length ? items.map((item) => `
            <div class="history-item compact ${state.history.selectedId === item.id ? 'active' : ''}">
              <button class="history-delete-btn" data-action="delete-history-item" data-generation-id="${escapeHtml(item.id || '')}" title="Удалить из истории" aria-label="Удалить из истории">×</button>
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
      <div class="help-text" style="margin-bottom:12px;"></div>
      <div class="selector-stack">
        <div class="input-group">
          <label class="label">Семейство</label>
          <select id="video_provider">${providerOptions}</select>
        </div>
        <div class="input-group">
          <label class="label">Модель</label>
          <select id="video_model">${modelOptions}</select>
        </div>
        ${Object.keys(videoModelConfig().modes).length > 1 ? `<div class="input-group"><label class="label">Режим</label><select id="video_mode">${modeOptions}</select></div>` : ''}
      </div>
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
    addFields(`${fieldSelect('Duration', 'video_duration', state.video.duration, durationOptions)}${state.video.model === 'kling-3.0' ? fieldSelect('Resolution', 'video_resolution', state.video.resolution, [['720','720p'],['1080','1080p']]) : ''}${fieldSelect('Aspect ratio', 'video_aspectRatio', state.video.aspectRatio, [['16:9','16:9'],['9:16','9:16'],['1:1','1:1']])}`);
    if (state.video.model === 'kling-3.0') {
      parts.push(`<div class="inspector-card">${fieldTogglePanel('Enable audio', 'video_enableAudio', state.video.enableAudio, 'Звук увеличивает стоимость на 1 токен за каждую секунду ролика.', state.video.enableAudio ? 'Звук включён' : 'Без звука')}</div>`);
    }
    return parts.join('');
  }
  if (state.video.provider === 'veo') {
    addFields(`${fieldSelect('Duration', 'video_durationVeo', state.video.duration || '8', [['4','4 sec'],['6','6 sec'],['8','8 sec']])}${fieldSelect('Aspect ratio', 'video_aspectRatioVeo', state.video.aspectRatio || '16:9', [['16:9','16:9'],['9:16','9:16']])}`);
    parts.push(`<div class="inspector-card">${fieldTogglePanel('Generate audio', 'video_generateAudio', state.video.enableAudio, 'Включает генерацию звука в ролике.', state.video.enableAudio ? 'Аудио активно' : 'Аудио отключено')}</div>`);
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


function renderImageInspector() {
  syncImageSelection();
  if (state.image.panel === 'library') {
    const items = state.imageHistory.items || [];
    return `
      <div class="inspector-card">
        <div class="field-head" style="margin-bottom:12px;"><div class="section-title" style="margin:0;">История изображений</div><button class="btn ghost small" data-action="show-image-params">Параметры</button></div>
        <div class="help-text">Здесь сохраняются результаты из Image Studio. Можно открыть старую генерацию, скачать её или удалить из истории.</div>
        <div class="actions compact-gap" style="margin-top:12px; flex-wrap:wrap;">
          <button class="btn ghost small" data-action="refresh-image-history">Обновить</button>
        </div>
        <div class="mini-list" style="margin-top:14px;">
          ${state.imageHistory.loading ? `<div class="empty-state">Загружаю историю изображений...</div>` : ''}
          ${!state.imageHistory.loading && !items.length ? `<div class="empty-state">Пока нет сохранённых изображений.</div>` : ''}
          ${!state.imageHistory.loading ? items.map((item) => {
            const previewUrl = imageHistoryUrl(item);
            return `
            <div class="history-item compact ${state.imageHistory.selectedId === item.id ? 'active' : ''}">
              <button class="history-delete-btn" data-action="delete-image-history-item" data-generation-id="${escapeHtml(item.id || '')}" title="Удалить из истории" aria-label="Удалить из истории">×</button>
              ${previewUrl ? `<div style="margin-bottom:10px;"><img src="${escapeHtml(previewUrl)}" alt="preview" style="width:100%; height:132px; object-fit:cover; border-radius:14px; border:1px solid rgba(255,255,255,0.08);"></div>` : ''}
              <div class="history-item-row"><strong>${escapeHtml(imageHistoryTitle(item))}</strong><span class="badge ${historyStatusTone(item.status)}">${escapeHtml(historyStatusLabel(item.status))}</span></div>
              <small>${escapeHtml(formatDate(item.completed_at || item.created_at))}</small>
              <div class="actions compact-gap" style="margin-top:10px; flex-wrap:wrap;">
                <button class="btn outline small" data-action="use-image-history-item" data-generation-id="${escapeHtml(item.id || '')}">В рабочую зону</button>
              </div>
            </div>`;
          }).join('') : ''}
        </div>
        ${state.imageHistory.lastError ? `<div class="help-text" style="margin-top:12px;">${escapeHtml(state.imageHistory.lastError)}</div>` : ''}
      </div>
    `;
  }

  const providerOptions = Object.entries(IMAGE_REGISTRY).map(([id, provider]) => `<option value="${escapeHtml(id)}" ${state.image.provider === id ? 'selected' : ''}>${escapeHtml(provider.name)}</option>`).join('');
  const providerModels = Object.entries(imageProviderConfig().models);
  const hasMultipleModels = providerModels.length > 1;
  const modelOptions = providerModels.map(([id, model]) => `<option value="${escapeHtml(id)}" ${state.image.model === id ? 'selected' : ''}>${escapeHtml(model.name)}</option>`).join('');
  const modeOptions = Object.entries(imageModelConfig().modes).map(([id, mode]) => `<option value="${escapeHtml(id)}" ${state.image.mode === id ? 'selected' : ''}>${escapeHtml(mode.name)}</option>`).join('');

  return `
    <div class="inspector-card">
      <div class="field-head" style="margin-bottom:12px;"><div class="section-title" style="margin:0;">Image Studio</div><button class="btn ghost small" data-action="show-image-library">История изображений</button></div>
      <div class="selector-stack">
        <div class="input-group">
          <label class="label">Семейство</label>
          <select id="image_provider">${providerOptions}</select>
        </div>
        ${hasMultipleModels ? `
          <div class="input-group">
            <label class="label">Модель</label>
            <select id="image_model">${modelOptions}</select>
          </div>
        ` : ''}
        ${Object.keys(imageModelConfig().modes).length > 1 ? `<div class="input-group"><label class="label">Режим</label><select id="image_mode">${modeOptions}</select></div>` : ''}
      </div>
    </div>
    ${renderImageModeFields()}
    <div class="inspector-card">
      <button class="btn primary full ${state.image.isGenerating ? 'loading' : ''}" data-action="run-image" ${state.image.isGenerating ? 'disabled' : ''}>${escapeHtml(imageRunButtonLabel())}</button>
      <div class="help-text" style="margin-top:10px;">${escapeHtml(state.image.statusText || 'Стоимость и режим генерации зависят от выбранного семейства.')}</div>
    </div>
  `;
}

function renderImageModeFields() {
  const parts = [];
  const addUpload = (label, id, hint, multiple = false, accept = 'image/*') => {
    parts.push(sectionUpload(label, id, hint, multiple, accept));
  };
  const addFields = (html) => parts.push(`<div class="inspector-card"><div class="field-grid two">${html}</div></div>`);
  const addPrompt = (placeholder = 'Опиши, что нужно создать или изменить: стиль, композицию, свет, детали, фон.') => parts.push(sectionTextarea('Prompt', 'image_prompt', state.image.prompt, placeholder));

  if (imageNeedsBaseImage()) addUpload('Base image', 'image_baseImage', 'Главное фото или база, от которой нужно отталкиваться.');
  if (imageNeedsSourceImage() || (state.image.provider === 'posters' && state.image.mode === 'poster')) {
    addUpload('Source image', 'image_sourceImage', state.image.provider === 'posters' && state.image.mode === 'poster' ? 'Опционально: фото, которое нужно встроить в афишу.' : 'Основное изображение для редактирования или фотосессии.');
  }

  if (state.image.provider === 'topaz_photo') {
    addFields(`${fieldSelect('Preset', 'image_upscalePreset', state.image.upscalePreset || 'standard', [['standard','Standard · 2 ток.'],['detail','Detail · 3 ток.'],['max','Max · 4 ток.']])}`);
    parts.push(`
      <div class="inspector-card">
        <div class="help-text">Topaz Photo Upscale работает без prompt: просто загрузи фото, выбери пресет и получишь compare slider в рабочей зоне.</div>
      </div>
    `);
    return parts.join('');
  }

  if (state.image.provider === 'posters' && state.image.mode === 'poster') {
    addPrompt('Опиши афишу: заголовок, стиль, композицию, фон, типографику, настроение.');
    addFields(`${fieldSelect('Poster style', 'image_posterStyle', state.image.posterStyle || 'cinematic', [['cinematic','Cinematic'],['minimal','Minimal'],['neon','Neon'],['luxury','Luxury'],['editorial','Editorial']])}`);
    return parts.join('');
  }

  if (state.image.provider === 'photosession') {
    addPrompt('Опиши образ, локацию, свет, одежду, позу и итоговую атмосферу фотосессии.');
    addFields(`${fieldSelect('Style', 'image_stylePreset', state.image.stylePreset || 'editorial', [['editorial','Editorial'],['fashion','Fashion'],['cinematic','Cinematic'],['street','Street'],['luxury','Luxury']])}${fieldSelect('Mood', 'image_moodPreset', state.image.moodPreset || 'premium', [['premium','Premium'],['soft','Soft'],['dramatic','Dramatic'],['bright','Bright'],['romantic','Romantic']])}`);
    return parts.join('');
  }

  if (state.image.provider === 'two_images') {
    addPrompt('Опиши, как объединить два изображения: что взять из base image, что взять из source image, какой нужен итоговый стиль.');
    addFields(`${fieldSelect('Resolution', 'image_resolution', state.image.resolution || '2K', [['1K','1K'],['2K','2K']])}${fieldSelect('Safety', 'image_safetyLevel', state.image.safetyLevel || 'high', [['high','High'],['medium','Medium'],['low','Low']])}`);
    return parts.join('');
  }

  addPrompt();
  const showImageResolution = true;
  const showAspect = state.image.mode !== 'image_edit';
  const aspectOptions = state.image.mode === 'image_to_image'
    ? [['match_input_image','Match input'],['16:9','16:9'],['9:16','9:16'],['1:1','1:1'],['3:4','3:4'],['4:3','4:3']]
    : [['16:9','16:9'],['9:16','9:16'],['1:1','1:1'],['3:4','3:4'],['4:3','4:3']];
  const fieldParts = [];
  if (showImageResolution) fieldParts.push(fieldSelect('Resolution', 'image_resolution', state.image.resolution || '2K', [['1K','1K'],['2K','2K']]));
  if (showAspect) fieldParts.push(fieldSelect('Aspect ratio', state.image.mode === 'text_to_image' || state.image.mode === 't2i' ? 'image_aspectRatioText' : 'image_aspectRatio', state.image.aspectRatio || (state.image.mode === 'image_to_image' ? 'match_input_image' : '16:9'), aspectOptions));
  fieldParts.push(fieldSelect('Safety', 'image_safetyLevel', state.image.safetyLevel || 'high', [['high','High'],['medium','Medium'],['low','Low']]));
  addFields(fieldParts.join(''));
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
  const config = FILE_INPUT_MAP[id];
  const asset = config ? getFile(config.key) : null;
  const triggerTitle = multiple ? 'Добавить файлы' : 'Добавить файл';
  const acceptLabel = accept === 'video/*' ? 'MP4 / MOV / WEBM' : 'PNG / JPG / WEBP';
  const selectedMarkup = asset ? (
    Array.isArray(asset)
      ? `
        <div class="upload-file-list">
          ${asset.map((item, index) => `
            <div class="upload-file-pill has-file">
              <span class="upload-file-name">${escapeHtml(item.name || `Файл ${index + 1}`)}</span>
              <button class="upload-file-remove" type="button" data-action="remove-upload-file" data-upload-id="${escapeHtml(id)}" data-index="${index}" aria-label="Удалить файл" title="Удалить файл">×</button>
            </div>
          `).join('')}
        </div>
      `
      : `
        <div class="upload-file-list">
          <div class="upload-file-pill has-file">
            <span class="upload-file-name">${escapeHtml(asset.name || 'Файл выбран')}</span>
            <button class="upload-file-remove" type="button" data-action="remove-upload-file" data-upload-id="${escapeHtml(id)}" aria-label="Удалить файл" title="Удалить файл">×</button>
          </div>
        </div>
      `
  ) : `<div class="upload-file-pill">Файл ещё не выбран</div>`;

  return `
    <div class="inspector-card">
      <div class="input-group">
        <label class="label">${escapeHtml(label)}</label>
        <input class="upload-input-native" id="${id}" type="file" ${multiple ? 'multiple' : ''} accept="${escapeHtml(accept)}">
        <label class="upload-trigger" for="${id}">
          <span class="upload-trigger-plus">+</span>
          <span class="upload-trigger-copy">
            <strong>${escapeHtml(triggerTitle)}</strong>
            <small>${escapeHtml(acceptLabel)}</small>
          </span>
        </label>
        <div class="help-text">${escapeHtml(help)}</div>
        ${selectedMarkup}
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

function fieldTogglePanel(label, id, checked, help, stateLabel = '') {
  return `
    <div class="toggle-panel ${checked ? 'is-active' : ''}">
      <div class="toggle-panel-copy">
        <div class="toggle-panel-title-row">
          <strong>${escapeHtml(label)}</strong>
          ${stateLabel ? `<span class="toggle-panel-badge">${escapeHtml(stateLabel)}</span>` : ''}
        </div>
        <div class="help-text">${escapeHtml(help)}</div>
      </div>
      <label class="switch switch-lg"><input id="${id}" type="checkbox" ${checked ? 'checked' : ''}><span></span></label>
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
    const hasCurrentVoice = state.voice.voices.some((item) => item.voice_id === state.voice.voiceId);
    if ((!state.voice.voiceId || !hasCurrentVoice) && state.voice.voices[0]) state.voice.voiceId = state.voice.voices[0].voice_id;
    saveState();
    render();
  } catch (e) {
    toast('error', 'Не удалось загрузить voices', String(e.message || e));
  }
}

async function loadVoiceHistory(options = {}) {
  const { silent = false, keepSelection = true, selectId = '' } = options;
  if (!requireAuth()) return;
  state.voiceHistory.loading = true;
  state.voiceHistory.lastError = '';
  if (!silent) render();
  try {
    const res = await apiFetch(`/api/workspace/tts/history?limit=${encodeURIComponent(state.voiceHistory.limit || 24)}&offset=0`);
    const data = await res.json();
    const items = Array.isArray(data.items) ? data.items : [];
    state.voiceHistory.items = items;
    state.voiceHistory.loaded = true;

    const preferredId = String(selectId || (keepSelection ? state.voiceHistory.selectedId : '') || '').trim();
    const selected = preferredId ? items.find((item) => String(item.id || '') === preferredId) : null;

    state.voiceHistory.selectedId = selected?.id || '';
    state.voiceHistory.selectedItem = selected || null;

    if (selected && (!state.voice.audioUrl || selectId || String(state.voice.generationId || '') !== String(selected.id || ''))) {
      applyVoiceHistoryItemToWorkspace(selected, { silent: true });
      return;
    }

    saveState();
    if (!silent) render();
  } catch (e) {
    state.voiceHistory.lastError = String(e.message || e);
    if (!silent) {
      render();
      toast('error', 'Не удалось загрузить историю voice', state.voiceHistory.lastError);
    }
  } finally {
    state.voiceHistory.loading = false;
    if (!silent) render();
  }
}

async function loadVoiceHistoryItem(generationId, options = {}) {
  const { silent = false } = options;
  const generationIdText = String(generationId || '').trim();
  if (!generationIdText) return null;
  try {
    const res = await apiFetch(`/api/workspace/tts/history/${encodeURIComponent(generationIdText)}`);
    const data = await res.json();
    const item = data.item || null;
    if (!item) return null;
    const idx = state.voiceHistory.items.findIndex((entry) => String(entry.id || '') === generationIdText);
    if (idx >= 0) state.voiceHistory.items[idx] = item;
    else state.voiceHistory.items.unshift(item);
    state.voiceHistory.selectedId = item.id || generationIdText;
    state.voiceHistory.selectedItem = item;
    saveState();
    if (!silent) render();
    return item;
  } catch (e) {
    if (!silent) toast('error', 'Не удалось открыть voice item', String(e.message || e));
    return null;
  }
}

async function deleteVoiceHistoryItem(generationId) {
  const generationIdText = String(generationId || '').trim();
  if (!generationIdText) return;
  try {
    await apiFetch(`/api/workspace/tts/history/${encodeURIComponent(generationIdText)}`, { method: 'DELETE' });
    state.voiceHistory.items = state.voiceHistory.items.filter((item) => String(item.id || '') !== generationIdText);
    if (String(state.voiceHistory.selectedId || '') === generationIdText) {
      state.voiceHistory.selectedId = '';
      state.voiceHistory.selectedItem = null;
    }
    if (String(state.voice.generationId || '') === generationIdText) {
      clearVoiceRunState({ keepText: true });
    }
    if (!state.voice.audioUrl && state.voiceHistory.items[0]) {
      applyVoiceHistoryItemToWorkspace(state.voiceHistory.items[0], { silent: true });
      return;
    }
    saveState();
    render();
    toast('success', 'Удалено', 'Озвучка удалена из истории.');
  } catch (e) {
    toast('error', 'Не удалось удалить', String(e.message || e));
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



async function loadImageHistory(options = {}) {
  const { silent = false, selectId = '', keepSelection = true } = options;
  if (!state.authToken) {
    state.imageHistory.items = [];
    state.imageHistory.selectedId = '';
    state.imageHistory.selectedItem = null;
    state.imageHistory.loaded = false;
    state.imageHistory.loading = false;
    state.imageHistory.lastError = '';
    if (!silent) render();
    return [];
  }

  state.imageHistory.loading = true;
  state.imageHistory.lastError = '';
  if (!silent) render();

  try {
    const qs = new URLSearchParams({ limit: String(state.imageHistory.limit || 24), offset: String(state.imageHistory.offset || 0) });
    const res = await apiFetch(`/api/workspace/image/history?${qs.toString()}`);
    const data = await res.json();
    const items = Array.isArray(data.items) ? data.items : [];
    state.imageHistory.items = items;
    state.imageHistory.loaded = true;

    const preferredId = String(selectId || '').trim() || (keepSelection ? String(state.imageHistory.selectedId || '').trim() : '');
    if (preferredId && items.some((item) => item.id === preferredId)) {
      state.imageHistory.selectedId = preferredId;
      state.imageHistory.selectedItem = items.find((item) => item.id === preferredId) || null;
    } else if (!keepSelection) {
      state.imageHistory.selectedId = '';
      state.imageHistory.selectedItem = null;
    }

    if (!silent) render();
    return items;
  } catch (e) {
    state.imageHistory.lastError = String(e.message || e);
    if (!silent) {
      render();
      toast('error', 'Не удалось загрузить историю изображений', state.imageHistory.lastError);
    }
    return [];
  } finally {
    state.imageHistory.loading = false;
    if (!silent) render();
  }
}

async function loadImageHistoryItem(generationId, options = {}) {
  const { silent = false } = options;
  const generationIdText = String(generationId || '').trim();
  if (!generationIdText) return null;
  if (!state.authToken) {
    if (!silent) toast('error', 'Нужна авторизация', 'Сначала войди через Telegram, чтобы открыть историю изображений.');
    return null;
  }

  try {
    const res = await apiFetch(`/api/workspace/image/history/${encodeURIComponent(generationIdText)}`);
    const data = await res.json();
    const item = data.item || null;
    if (!item) throw new Error('Пустой ответ истории изображений');

    state.imageHistory.selectedId = item.id || generationIdText;
    state.imageHistory.selectedItem = item;
    const idx = state.imageHistory.items.findIndex((entry) => entry.id === item.id);
    if (idx >= 0) state.imageHistory.items[idx] = { ...state.imageHistory.items[idx], ...item };
    else state.imageHistory.items.unshift(item);

    saveState();
    render();
    return item;
  } catch (e) {
    if (!silent) toast('error', 'Не удалось открыть изображение', String(e.message || e));
    return null;
  }
}

function applyImageHistoryItemToWorkspace(item) {
  const selected = item || imageHistorySelectedItem();
  if (!selected) {
    toast('info', 'История пуста', 'Сначала дождись хотя бы одного сохранённого изображения.');
    return;
  }
  const imageUrl = imageHistoryUrl(selected);
  if (!imageUrl) {
    toast('error', 'Нет ссылки на изображение', 'Для этого результата ещё не найден доступный файл.');
    return;
  }
  state.image.generationId = selected.id || '';
  state.image.prompt = selected.prompt || state.image.prompt;
  state.image.outputUrl = imageUrl;
  state.image.downloadUrl = imageUrl;
  state.image.beforeImageUrl = selected.before_image_url || selected.source_image_url || '';
  state.image.afterImageUrl = selected.after_image_url || imageUrl;
  state.image.compareMode = !!selected.compare_mode;
  state.image.comparePosition = 50;
  if (selected.preset_slug) state.image.upscalePreset = selected.preset_slug;
  state.image.errorText = selected.error_message || '';
  state.image.statusText = selected.has_storage_file ? 'Открыт сохранённый результат из библиотеки AstraBot.' : 'Открыт результат из истории.';
  state.image.panel = 'library';
  state.studio = 'image';
  saveState();
  render();
  toast('success', 'Изображение открыто', 'Результат возвращён в рабочую зону.');
}

async function deleteImageHistoryItem(generationId) {
  const generationIdText = String(generationId || '').trim();
  if (!generationIdText) return;
  if (!state.authToken) {
    toast('error', 'Нужна авторизация', 'Сначала войди через Telegram, чтобы управлять историей изображений.');
    return;
  }
  const target = state.imageHistory.items.find((item) => item.id === generationIdText) || state.imageHistory.selectedItem || null;
  const title = trimText(target?.prompt || 'это изображение', 56);
  if (!window.confirm(`Удалить из истории ${title}?`)) return;

  try {
    const res = await apiFetch(`/api/workspace/image/history/${encodeURIComponent(generationIdText)}`, { method: 'DELETE' });
    if (!res.ok) throw new Error('delete_failed');
    state.imageHistory.items = state.imageHistory.items.filter((item) => item.id !== generationIdText);
    if (state.imageHistory.selectedId === generationIdText) {
      state.imageHistory.selectedId = '';
      state.imageHistory.selectedItem = null;
    }
    if (String(state.image.generationId || '').trim() === generationIdText) {
      clearImageRunState({ keepPrompt: true, keepFiles: true });
      state.image.statusText = 'Результат удалён из истории. Рабочая область очищена.';
      state.image.panel = 'library';
    }
    saveState();
    render();
    toast('success', 'Удалено', 'Изображение убрано из истории.');
  } catch (e) {
    toast('error', 'Не удалось удалить', String(e.message || e));
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
  syncVideoEditorWithHistoryItem(selected);
  saveState();
  render();
  toast('success', 'Видео открыто', 'Ролик возвращён в рабочую зону.');
}

async function deleteHistoryItem(generationId) {
  const generationIdText = String(generationId || '').trim();
  if (!generationIdText) return;
  if (!state.authToken) {
    toast('error', 'Нужна авторизация', 'Сначала войди через Telegram, чтобы управлять историей.');
    return;
  }
  const target = state.history.items.find((item) => item.id === generationIdText) || state.history.selectedItem || null;
  const title = trimText(target?.prompt || 'это видео', 56);
  if (!window.confirm(`Удалить из истории ${title}?`)) return;

  try {
    const res = await apiFetch(`/api/workspace/history/${encodeURIComponent(generationIdText)}`, { method: 'DELETE' });
    if (!res.ok) throw new Error('delete_failed');
    state.history.items = state.history.items.filter((item) => item.id !== generationIdText);
    if (state.history.selectedId === generationIdText) {
      state.history.selectedId = '';
      state.history.selectedItem = null;
    }
    if (String(state.video.generationId || '').trim() === generationIdText) {
      clearVideoRunState({ keepPrompt: true });
      state.video.statusText = 'Ролик удалён из истории. Рабочая область очищена.';
      state.video.panel = 'library';
    }
    saveState();
    render();
    toast('success', 'Удалено', 'Генерация убрана из истории.');
  } catch (e) {
    toast('error', 'Не удалось удалить', String(e.message || e));
  }
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


async function runImage() {
  if (!requireAuth()) return;
  syncImageSelection();
  if (state.image.isGenerating) return;
  const prompt = String(state.image.prompt || '').trim();
  if (state.image.provider !== 'topaz_photo' && !prompt) {
    toast('error', 'Нужен prompt', 'Опиши, что нужно создать или изменить в изображении.');
    return;
  }
  if (imageNeedsSourceImage() && !getFile('image.sourceImage')) {
    toast('error', 'Нужно изображение', 'Для выбранного режима сначала загрузи source image.');
    return;
  }
  if (imageNeedsBaseImage() && !getFile('image.baseImage')) {
    toast('error', 'Нужно base image', 'Для режима Картинка + Картинка сначала загрузи base image.');
    return;
  }

  const form = new FormData();
  form.append('provider', state.image.provider);
  form.append('model', state.image.model);
  form.append('mode', state.image.mode);
  form.append('prompt', prompt);
  form.append('resolution', String(state.image.resolution || '2K'));
  form.append('aspect_ratio', String(state.image.aspectRatio || ''));
  form.append('safety_level', String(state.image.safetyLevel || 'high'));
  form.append('poster_style', String(state.image.posterStyle || ''));
  form.append('style_preset', String(state.image.stylePreset || ''));
  form.append('mood_preset', String(state.image.moodPreset || ''));
  form.append('preset_slug', String(state.image.upscalePreset || 'standard'));

  const source = getFile('image.sourceImage');
  const base = getFile('image.baseImage');
  if (source?.file) form.append('source_image', source.file, source.name || source.file.name || 'source.png');
  if (base?.file) form.append('base_image', base.file, base.name || base.file.name || 'base.png');

  state.image.isGenerating = true;
  state.image.outputUrl = '';
  state.image.downloadUrl = '';
  state.image.beforeImageUrl = '';
  state.image.afterImageUrl = '';
  state.image.compareMode = false;
  state.image.comparePosition = 50;
  state.image.generationId = '';
  state.image.errorText = '';
  state.image.statusText = 'Задача отправлена. Жди итоговую картинку в рабочей зоне.';
  saveState();
  render();

  try {
    const res = await apiFetch('/api/workspace/image/run', { method: 'POST', body: form });
    const data = await res.json();
    state.image.generationId = data.generation_id || '';
    state.image.outputUrl = data.image_url || data.output_url || '';
    state.image.downloadUrl = data.download_url || state.image.outputUrl;
    state.image.beforeImageUrl = data.before_image_url || '';
    state.image.afterImageUrl = data.after_image_url || state.image.outputUrl;
    state.image.compareMode = !!data.compare_mode;
    state.image.comparePosition = 50;
    if (data.preset_slug) state.image.upscalePreset = data.preset_slug;
    state.image.statusText = data.status_text || 'Изображение готово.';
    state.image.panel = 'params';
    pushRun({ studio: 'Image', title: `${currentMeta().provider} · ${currentMeta().model}`, summary: prompt.slice(0, 120) });
    if (state.image.generationId) {
      loadImageHistory({ silent: true, keepSelection: true, selectId: state.image.generationId }).catch(() => {});
    }
    toast('success', 'Изображение готово', state.image.statusText || 'Результат появился в рабочей зоне.');
  } catch (e) {
    state.image.errorText = String(e.message || e);
    state.image.statusText = 'Не удалось выполнить генерацию.';
    toast('error', 'Ошибка генерации', state.image.errorText);
  } finally {
    state.image.isGenerating = false;
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
      syncVideoEditorWithHistoryItem(item);
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
  if (!requireAuth()) return;
  if (!state.voice.text.trim()) {
    toast('error', 'Нужен текст', 'Введи текст для озвучки.');
    return;
  }
  if (!state.voice.voiceId) {
    toast('error', 'Нужен голос', 'Сначала загрузи и выбери voice.');
    return;
  }
  state.voice.isGenerating = true;
  state.voice.errorText = '';
  render();
  try {
    const res = await apiFetch('/api/workspace/tts/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text: state.voice.text,
        voice_id: state.voice.voiceId,
        model_id: state.voice.modelId,
        output_format: state.voice.outputFormat,
        language_code: state.voice.languageCode || 'auto',
        manual_voice_settings: !!state.voice.manualVoiceSettings,
        stability: Number(state.voice.stability),
        similarity_boost: Number(state.voice.similarityBoost),
        style: Number(state.voice.style),
        speed: Number(state.voice.speed),
        use_speaker_boost: !!state.voice.useSpeakerBoost,
      }),
    });
    const data = await res.json();
    revokeVoiceAudioUrl();
    state.voice.audioUrl = data.audio_url || '';
    state.voice.downloadUrl = data.download_url || data.audio_url || '';
    state.voice.generationId = data.generation_id || '';
    state.voice.lastGeneratedAt = data.completed_at || data.created_at || new Date().toISOString();
    state.voice.isGenerating = false;
    state.voice.errorText = '';
    pushRun({ studio: 'Voice', title: 'TTS generate', summary: state.voice.text.slice(0, 100) });
    saveState();
    loadVoiceHistory({ silent: true, keepSelection: true, selectId: state.voice.generationId }).catch(() => {});
    toast('success', 'Аудио готово', 'Файл сгенерирован и сохранён в истории.');
    render();
  } catch (e) {
    state.voice.isGenerating = false;
    state.voice.errorText = String(e.message || e);
    saveState();
    render();
    toast('error', 'TTS error', String(e.message || e));
  }
}


async function loadMusicHistory(options = {}) {
  const { silent = false, selectId = '', keepSelection = true } = options;
  if (!state.authToken) {
    state.musicHistory.items = [];
    state.musicHistory.selectedId = '';
    state.musicHistory.selectedItem = null;
    state.musicHistory.loaded = false;
    state.musicHistory.loading = false;
    state.musicHistory.lastError = '';
    if (!silent) render();
    return [];
  }

  state.musicHistory.loading = true;
  state.musicHistory.lastError = '';
  if (!silent) render();

  try {
    const qs = new URLSearchParams({ limit: String(state.musicHistory.limit || 24), offset: String(state.musicHistory.offset || 0) });
    const res = await apiFetch(`/api/workspace/music/history?${qs.toString()}`);
    const data = await res.json();
    const items = Array.isArray(data.items) ? data.items : [];
    state.musicHistory.items = items;
    state.musicHistory.loaded = true;

    const preferredId = String(selectId || '').trim() || (keepSelection ? String(state.musicHistory.selectedId || '').trim() : '');
    if (preferredId && items.some((item) => item.id === preferredId)) {
      state.musicHistory.selectedId = preferredId;
      state.musicHistory.selectedItem = items.find((item) => item.id === preferredId) || null;
    } else if (!keepSelection) {
      state.musicHistory.selectedId = '';
      state.musicHistory.selectedItem = null;
    }

    if (!silent) render();
    return items;
  } catch (e) {
    state.musicHistory.lastError = String(e.message || e);
    if (!silent) {
      render();
      toast('error', 'Не удалось загрузить музыку', state.musicHistory.lastError);
    }
    return [];
  } finally {
    state.musicHistory.loading = false;
    if (!silent) render();
  }
}

async function loadMusicHistoryItem(generationId, options = {}) {
  const { silent = false, startPolling = false } = options;
  const generationIdText = String(generationId || '').trim();
  if (!generationIdText) return null;
  if (!state.authToken) {
    if (!silent) toast('error', 'Нужна авторизация', 'Сначала войди через Telegram, чтобы открыть историю музыки.');
    return null;
  }

  try {
    const res = await apiFetch(`/api/workspace/music/history/${encodeURIComponent(generationIdText)}`);
    const data = await res.json();
    const item = data.item || null;
    if (!item) throw new Error('Пустой ответ истории музыки');

    state.musicHistory.selectedId = item.id || generationIdText;
    state.musicHistory.selectedItem = item;
    const idx = state.musicHistory.items.findIndex((entry) => entry.id === item.id);
    if (idx >= 0) state.musicHistory.items[idx] = { ...state.musicHistory.items[idx], ...item };
    else state.musicHistory.items.unshift(item);

    state.music.generationId = item.id || '';
    state.music.results = Array.isArray(item.tracks) ? item.tracks : [];
    state.music.status = item.status || state.music.status;
    const trackCount = Array.isArray(item.tracks) ? item.tracks.length : 0;
    state.music.statusText = item.status === 'completed'
      ? `Музыка готова. Получено ${trackCount} ${musicTrackLabel(trackCount)}.`
      : item.status === 'failed'
        ? 'Генерация завершилась ошибкой.'
        : 'Музыка в процессе генерации...';
    state.music.errorText = item.error_message || '';
    state.music.lastCompletedAt = item.completed_at || state.music.lastCompletedAt || '';

    saveState();
    render();

    if (startPolling && ['queued', 'processing', 'running'].includes(String(item.status || '').toLowerCase())) startMusicPolling(item.id);
    return item;
  } catch (e) {
    if (!silent) toast('error', 'Не удалось открыть генерацию', String(e.message || e));
    return null;
  }
}

function stopMusicPolling() {
  if (runtime.musicPollTimer) {
    clearTimeout(runtime.musicPollTimer);
    runtime.musicPollTimer = null;
  }
}

function startMusicPolling(generationId) {
  stopMusicPolling();
  const generationIdText = String(generationId || '').trim();
  if (!generationIdText) return;

  const tick = async () => {
    const item = await loadMusicHistoryItem(generationIdText, { silent: true });
    const status = String(item?.status || '').toLowerCase();
    if (!item || ['completed', 'failed', 'error', 'cancelled', 'canceled'].includes(status)) {
      state.music.isGenerating = false;
      saveState();
      render();
      if (item?.status === 'completed') {
        loadMusicHistory({ silent: true, keepSelection: true, selectId: generationIdText }).catch(() => {});
        toast('success', 'Музыка готова', 'Треки и история обновлены.');
      } else if (item?.status === 'failed') {
        toast('error', 'Music error', item.error_message || 'Генерация завершилась ошибкой.');
      }
      stopMusicPolling();
      return;
    }
    runtime.musicPollTimer = setTimeout(tick, 5000);
  };

  runtime.musicPollTimer = setTimeout(tick, 3500);
}

function applyMusicHistoryItemToWorkspace(item) {
  const selected = item || musicSelectedItem();
  if (!selected) {
    toast('info', 'История пуста', 'Сначала запусти хотя бы одну генерацию музыки.');
    return;
  }
  state.music.generationId = selected.id || '';
  state.music.ai = selected.ai || state.music.ai;
  state.music.backend = selected.backend || state.music.backend;
  state.music.mode = selected.mode || state.music.mode;
  state.music.title = selected.title || state.music.title;
  state.music.tags = selected.tags || state.music.tags;
  state.music.language = selected.language || state.music.language;
  state.music.mood = selected.mood || state.music.mood;
  state.music.references = selected.references || state.music.references;
  state.music.ideaText = selected.idea_text || state.music.ideaText;
  state.music.lyricsText = selected.lyrics_text || state.music.lyricsText;
  state.music.instrumental = !!selected.instrumental;
  state.music.results = Array.isArray(selected.tracks) ? selected.tracks : [];
  state.music.status = selected.status || state.music.status;
  const selectedTrackCount = Array.isArray(selected.tracks) ? selected.tracks.length : 0;
  state.music.statusText = selected.status === 'completed'
    ? `Открыт сохранённый результат: ${selectedTrackCount} ${musicTrackLabel(selectedTrackCount)}.`
    : 'Открыт запуск из истории.';
  state.music.errorText = selected.error_message || '';
  state.studio = 'music';
  saveState();
  render();
  toast('success', 'Музыка открыта', 'Запуск возвращён в Music Studio.');
}

async function deleteMusicHistoryItem(generationId) {
  const generationIdText = String(generationId || '').trim();
  if (!generationIdText) return;
  if (!state.authToken) {
    toast('error', 'Нужна авторизация', 'Сначала войди через Telegram, чтобы управлять историей музыки.');
    return;
  }
  const target = state.musicHistory.items.find((item) => item.id === generationIdText) || state.musicHistory.selectedItem || null;
  const title = trimText(target?.title || target?.idea_text || 'эту генерацию', 56);
  if (!window.confirm(`Удалить из истории ${title}?`)) return;

  try {
    const res = await apiFetch(`/api/workspace/music/history/${encodeURIComponent(generationIdText)}`, { method: 'DELETE' });
    if (!res.ok) throw new Error('delete_failed');
    state.musicHistory.items = state.musicHistory.items.filter((item) => item.id !== generationIdText);
    if (state.musicHistory.selectedId === generationIdText) {
      state.musicHistory.selectedId = '';
      state.musicHistory.selectedItem = null;
    }
    saveState();
    render();
    toast('success', 'Удалено', 'Запуск удалён из истории музыки.');
  } catch (e) {
    toast('error', 'Не удалось удалить', String(e.message || e));
  }
}

async function runMusic() {
  ensureMusicCompatibility({ preserveLyricsTab: true });
  const ideaText = String(state.music.ideaText || '').trim();
  const lyricsText = String(state.music.lyricsText || '').trim();
  const mode = state.music.ai === 'udio' ? 'idea' : state.music.mode;

  if (mode === 'idea' && !ideaText) {
    toast('error', 'Нужна идея', 'Заполни центральный блок Idea перед генерацией.');
    setMusicTab('idea');
    return;
  }
  if (mode === 'lyrics' && !lyricsText) {
    toast('error', 'Нужен текст песни', 'Заполни Lyrics или сгенерируй его через Songwriter.');
    setMusicTab('lyrics');
    return;
  }
  if (!state.authToken) {
    toast('error', 'Нужна авторизация', 'Сначала войди через Telegram, чтобы сайт мог работать с историей и токенами.');
    return;
  }

  state.music.isGenerating = true;
  state.music.status = 'processing';
  state.music.statusText = state.music.ai === 'suno'
    ? 'Запуск генерации музыки... Ждём 2 трека от Suno.'
    : 'Запуск генерации музыки...';
  state.music.errorText = '';
  saveState();
  render();

  try {
    const res = await apiFetch('/api/workspace/music/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ai: state.music.ai,
        backend: state.music.backend,
        mode,
        title: state.music.title,
        tags: state.music.tags,
        language: state.music.language,
        mood: state.music.mood,
        references: state.music.references,
        instrumental: !!state.music.instrumental,
        idea_text: ideaText,
        lyrics_text: lyricsText,
      }),
    });
    const data = await res.json();
    state.music.generationId = data.generation_id || '';
    state.music.status = data.status || 'queued';
    state.music.statusText = state.music.ai === 'suno'
      ? 'Музыка поставлена в обработку. После завершения здесь появятся 2 трека.'
      : 'Музыка поставлена в обработку. После завершения результат появится в рабочей зоне.';
    pushRun({ studio: 'Music', title: 'Music generate', summary: (mode === 'lyrics' ? lyricsText : ideaText).slice(0, 100) });
    saveState();
    render();
    if (state.music.generationId) {
      startMusicPolling(state.music.generationId);
      loadMusicHistory({ silent: true, keepSelection: true, selectId: state.music.generationId }).catch(() => {});
    }
  } catch (e) {
    state.music.isGenerating = false;
    state.music.status = 'failed';
    state.music.statusText = 'Не удалось запустить генерацию.';
    state.music.errorText = String(e.message || e);
    saveState();
    render();
    toast('error', 'Music run error', state.music.errorText);
  }
}

async function runSongwriter() {
  ensureMusicCompatibility({ preserveLyricsTab: true });
  const userText = String(state.music.songwriter.input || '').trim() || musicSourceTextForSongwriter();
  if (!userText) {
    toast('error', 'Нужен текст', 'Опиши идею трека или задачу для GPT Songwriter.');
    return;
  }

  const history = Array.isArray(state.music.songwriter.messages)
    ? state.music.songwriter.messages.filter((item) => ['user', 'assistant'].includes(item.role)).slice(-16)
    : [];

  state.music.songwriter.loading = true;
  if (String(state.music.songwriter.input || '').trim()) {
    history.push({ role: 'user', content: state.music.songwriter.input.trim() });
    state.music.songwriter.messages = history;
  }
  saveState();
  render();

  try {
    const res = await apiFetch('/api/workspace/songwriter', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text: userText,
        history,
        language: state.music.language,
        genre: state.music.tags,
        mood: state.music.mood,
        references: state.music.references,
      }),
    });
    const data = await res.json();
    const answer = data.answer || '';
    state.music.songwriter.lastAnswer = answer;
    state.music.songwriter.messages = [...history, { role: 'assistant', content: answer }].slice(-20);
    state.music.songwriter.input = '';
    state.music.songwriter.loading = false;
    state.music.activeTab = 'songwriter';
    pushRun({ studio: 'Music', title: 'Songwriter', summary: userText.slice(0, 100) });
    saveState();
    render();
    toast('success', 'Songwriter ответил', 'Ответ добавлен в центральный блок Songwriter.');
  } catch (e) {
    state.music.songwriter.loading = false;
    saveState();
    render();
    toast('error', 'Songwriter error', String(e.message || e));
  }
}

function seedMusicSongwriter() {
  const seed = [
    { role: 'assistant', content: 'Давай быстро соберём вводные: 1) жанр/стиль 2) настроение 3) язык 4) о чём песня 5) нужен ли припев с хук-фразой.' }
  ];
  state.music.songwriter.messages = seed;
  state.music.songwriter.lastAnswer = seed[0].content;
  state.music.songwriter.input = 'Нужен русский коммерческий текст песни для рекламы танцевальной школы.';
  state.music.activeTab = 'songwriter';
  saveState();
  render();
}

function applyLastSongwriterAnswer(target) {
  const answer = musicLastAnswerText();
  if (!answer) {
    toast('info', 'Пока пусто', 'Сначала получи ответ от GPT Songwriter.');
    return;
  }
  if (target === 'lyrics') {
    state.music.lyricsText = state.music.lyricsText ? `${state.music.lyricsText.trim()}

${answer}` : answer;
    state.music.activeTab = 'lyrics';
  } else {
    state.music.ideaText = state.music.ideaText ? `${state.music.ideaText.trim()}

${answer}` : answer;
    state.music.activeTab = 'idea';
  }
  saveState();
  render();
  toast('success', 'Ответ вставлен', target === 'lyrics' ? 'Ответ GPT добавлен в Lyrics.' : 'Ответ GPT добавлен в Idea.');
}

function seedDemo() {
  state.chat.messages = [
    { role: 'system', content: 'Добро пожаловать в AstraBot Workspace.' },
    { role: 'user', content: 'Сделай концепцию короткого рекламного ролика для танцевальной школы.' },
    { role: 'assistant', content: 'Можно построить ролик на контрасте: пустой зал → свет включается → динамика группы → call to action. Дальше отдельно выдам prompt для Kling и текст для озвучки.' },
  ];
  state.video.prompt = 'Dynamic cinematic commercial for a dance school, golden rim light, confident young dancers, sweeping camera movement, premium branded ending.';
  state.music.ideaText = 'Нужна идея вдохновляющей песни для рекламы танцевальной школы.';
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
      resetVideoEditorState();
      break;
    case 'image':
      clearImageRunState({ keepPrompt: false, keepFiles: false });
      break;
    case 'voice':
      clearVoiceRunState({ keepText: false });
      break;
    case 'music':
      stopMusicPolling();
      state.music.ideaText = '';
      state.music.lyricsText = '';
      state.music.songwriter.input = '';
      state.music.songwriter.messages = [];
      state.music.songwriter.lastAnswer = '';
      state.music.results = [];
      state.music.generationId = '';
      state.music.status = 'idle';
      state.music.statusText = 'Собери идею, текст и параметры справа, затем запусти генерацию.';
      state.music.errorText = '';
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

  const fileConfig = FILE_INPUT_MAP[id];
  if (fileConfig) {
    if (id === 'editorAudioUpload') {
      handleEditorAudioFileSelected(files && files[0] ? files[0] : null);
      target.value = '';
      return;
    }
    if (id === 'editorMergeUpload') {
      handleEditorMergeVideoSelected(files && files[0] ? files[0] : null);
      target.value = '';
      return;
    }
    const { key, multiple } = fileConfig;
    setFile(key, multiple ? files : files[0], multiple);
    if (id === 'video_motionVideo') probeMotionDuration(getFile('video.motionVideo'));
    render();
    return;
  }

  const update = (obj, key, val) => { obj[key] = val; };

  if (id.startsWith('editor_audio_')) {
    const match = id.match(/^editor_audio_(\d+)_(audio_start|audio_end|video_start|volume)$/);
    if (match) {
      const index = Number(match[1]);
      const field = match[2];
      const clip = state.videoEditor.audioClips[index];
      if (clip) {
        if (field === 'audio_start') clip.audioStart = Number(value || 0);
        if (field === 'audio_end') clip.audioEnd = Number(value || 0);
        if (field === 'video_start') clip.videoStart = Number(value || 0);
        if (field === 'volume') clip.volume = Number(value || 0);
        saveState();
        render();
      }
    }
    return;
  }

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
    case 'editor_trim_enabled':
      state.videoEditor.trim.enabled = checked;
      break;
    case 'editor_trim_start_input':
    case 'editor_trim_start_range':
      state.videoEditor.trim.startSec = Number(value || 0);
      break;
    case 'editor_trim_end_input':
    case 'editor_trim_end_range':
      state.videoEditor.trim.endSec = Number(value || 0);
      break;
    case 'editor_original_audio_mute':
      state.videoEditor.originalAudio.mute = checked;
      break;
    case 'editor_original_audio_volume':
    case 'editor_original_audio_volume_input':
      state.videoEditor.originalAudio.volume = Number(value || 0);
      break;

    case 'image_provider':
      state.image.provider = value;
      state.image.model = Object.keys(IMAGE_REGISTRY[value].models)[0];
      state.image.mode = Object.keys(IMAGE_REGISTRY[value].models[state.image.model].modes)[0];
      state.image.panel = 'params';
      clearImageRunState({ keepPrompt: true, keepFiles: true });
      break;
    case 'image_model':
      state.image.model = value;
      state.image.mode = Object.keys(IMAGE_REGISTRY[state.image.provider].models[value].modes)[0];
      state.image.panel = 'params';
      clearImageRunState({ keepPrompt: true, keepFiles: true });
      break;
    case 'image_mode':
      state.image.mode = value;
      state.image.panel = 'params';
      clearImageRunState({ keepPrompt: true, keepFiles: true });
      break;
    case 'image_prompt': state.image.prompt = value; break;
    case 'image_resolution': state.image.resolution = value; break;
    case 'image_aspectRatio':
    case 'image_aspectRatioText': state.image.aspectRatio = value; break;
    case 'image_safetyLevel': state.image.safetyLevel = value; break;
    case 'image_posterStyle': state.image.posterStyle = value; break;
    case 'image_stylePreset': state.image.stylePreset = value; break;
    case 'image_moodPreset': state.image.moodPreset = value; break;
    case 'image_upscalePreset': state.image.upscalePreset = value; break;
    case 'image_compareRange':
      setImageComparePosition(value, { commit: false });
      return;

    case 'voice_voiceId': state.voice.voiceId = value; break;
    case 'voice_modelId': state.voice.modelId = value; break;
    case 'voice_outputFormat': state.voice.outputFormat = value; break;
    case 'voice_languageCode': state.voice.languageCode = value || 'auto'; break;
    case 'voice_manualVoiceSettings': state.voice.manualVoiceSettings = !!target.checked; break;
    case 'voice_stability': state.voice.stability = Number(value); break;
    case 'voice_similarityBoost': state.voice.similarityBoost = Number(value); break;
    case 'voice_style': state.voice.style = Number(value); break;
    case 'voice_speed': state.voice.speed = Number(value); break;
    case 'voice_useSpeakerBoost': state.voice.useSpeakerBoost = !!target.checked; break;
    case 'voice_text': state.voice.text = value; break;

    case 'music_ai':
      state.music.ai = value;
      ensureMusicCompatibility({ preserveLyricsTab: true });
      break;
    case 'music_backend':
      state.music.backend = value;
      ensureMusicCompatibility({ preserveLyricsTab: true });
      break;
    case 'music_mode':
      state.music.mode = value;
      ensureMusicCompatibility({ preserveLyricsTab: true });
      break;
    case 'music_language': state.music.language = value; break;
    case 'music_title': state.music.title = value; break;
    case 'music_tags': state.music.tags = value; break;
    case 'music_mood': state.music.mood = value; break;
    case 'music_references': state.music.references = value; break;
    case 'music_instrumental': state.music.instrumental = checked; break;
    case 'music_ideaText': state.music.ideaText = value; break;
    case 'music_lyricsText': state.music.lyricsText = value; break;
    case 'music_songwriterInput': state.music.songwriter.input = value; break;
    case 'workspaceNotes': state.workspaceNotes = value; break;
    default: return;
  }
  saveState();
  const structuralRerenderIds = new Set([
    'chat_mode',
    'video_provider', 'video_model', 'video_mode',
    'image_provider', 'image_model', 'image_mode',
    'music_ai', 'music_backend', 'music_mode',
    'voice_voiceId', 'voice_modelId', 'voice_outputFormat', 'voice_languageCode'
  ]);
  const workspaceRerenderIds = new Set([]);
  const inspectorRerenderIds = new Set([
    'voice_manualVoiceSettings',
    'voice_stability',
    'voice_similarityBoost',
    'voice_style',
    'voice_speed',
    'voice_useSpeakerBoost',
  ]);
  if (structuralRerenderIds.has(id) || target.tagName === 'SELECT') {
    render();
  } else if (workspaceRerenderIds.has(id)) {
    renderWorkspace();
  } else if (inspectorRerenderIds.has(id) || (target.type === 'checkbox' && String(id || '').startsWith('voice_'))) {
    renderInspector();
    renderHeader();
  } else if (target.type === 'checkbox') {
    render();
  } else {
    renderHeader();
  }
}


function activateStudio(studio, options = {}) {
  if (!studio || !STUDIO_META[studio]) return;
  const previousStudio = state.studio;
  state.studio = studio;
  if (state.studio === 'video' && previousStudio !== 'video') state.video.panel = 'params';
  if (state.studio === 'library' && !state.prompts.categories.length) loadPromptCategories();
  if (state.studio === 'voice' && !state.voice.voices.length) loadVoices();
  if (state.studio === 'voice' && state.authToken) loadVoiceHistory({ silent: true, keepSelection: true }).catch(() => {});
  if (state.studio === 'music' && state.authToken) loadMusicHistory({ silent: true, keepSelection: true }).catch(() => {});
  if (state.studio === 'history' && state.authToken) loadVideoHistory({ silent: true, keepSelection: true });
  if (state.studio === 'video' && state.video.panel === 'library' && state.authToken) loadVideoHistory({ silent: true, keepSelection: true });
  if (state.studio === 'image' && state.image.panel === 'library' && state.authToken) loadImageHistory({ silent: true, keepSelection: true });
  if (!options.skipRender) render();
  if (!options.skipSave) saveState();
}

function setAppView(view, options = {}) {
  state.view = view === 'workspace' ? 'workspace' : 'showcase';
  localStorage.setItem('astrabot:view', state.view);
  renderLandingView();
  if (options.updateHash !== false) {
    if (state.view === 'workspace') history.replaceState(null, '', '#workspace');
    else history.replaceState(null, '', window.location.pathname + window.location.search);
  }
  if (options.scroll !== false) {
    const target = state.view === 'workspace'
      ? document.getElementById('workspaceSection')
      : document.getElementById('showcaseView');
    target?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
}

function renderLandingView() {
  document.body.classList.toggle('workspace-view', state.view === 'workspace');
  document.body.classList.toggle('showcase-view', state.view !== 'workspace');
  const status = document.getElementById('landingStatusPill');
  if (status) {
    if (state.authToken && state.me) status.textContent = 'Telegram connected';
    else if (state.apiOnline) status.textContent = 'API online';
    else status.textContent = 'AI workspace live';
  }
}

function handleAction(action, dataset = {}) {
  switch (action) {
    case 'switch-studio': {
      activateStudio(dataset.studio);
      break;
    }
    case 'open-workspace': {
      if (dataset.studio) activateStudio(dataset.studio, { skipRender: true, skipSave: true });
      render();
      saveState();
      setAppView('workspace');
      break;
    }
    case 'show-showcase': {
      setAppView('showcase');
      break;
    }
    case 'show-pricing': {
      setAppView('showcase');
      requestAnimationFrame(() => {
        const pricing = document.getElementById('pricingSection');
        if (pricing) pricing.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
      break;
    }
    case 'login-placeholder': {
      toast('info', 'Вход скоро появится', 'Пока это заглушка. Позже сюда подключим полноценную авторизацию.');
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
    case 'editor-pick-audio': {
      const input = document.getElementById('editorAudioUpload');
      if (input) input.click();
      break;
    }
    case 'editor-pick-merge-video': {
      const input = document.getElementById('editorMergeUpload');
      if (input) input.click();
      break;
    }
    case 'editor-reset-trim':
      state.videoEditor.trim.startSec = 0;
      state.videoEditor.trim.endSec = Number(state.videoEditor.activeVideo.durationSec || 0);
      saveState();
      render();
      break;
    case 'editor-remove-audio-clip':
      state.videoEditor.audioClips = state.videoEditor.audioClips.filter((_, i) => i !== Number(dataset.index || -1));
      saveState();
      render();
      break;
    case 'editor-add-active-video':
      if (ensureActiveVideoFirstInMergeQueue()) {
        saveState();
        render();
      }
      break;
    case 'editor-add-history-video': {
      const item = historySelectedItem();
      if (!item || !item.id) {
        toast('info', 'Нужно выбрать ролик', 'Справа открой историю видео и выбери ролик из библиотеки.');
        break;
      }
      ensureActiveVideoFirstInMergeQueue();
      const exists = state.videoEditor.mergeQueue.some((entry) => entry.type === 'generation' && entry.id === item.id);
      if (!exists) {
        state.videoEditor.mergeQueue.push({
          type: 'generation',
          id: item.id,
          filename: trimText(item.prompt || `${item.provider || 'video'} ${item.model || ''}`, 88) || 'video',
          durationSec: Number(item.duration_sec || 0),
          sourceLabel: 'library',
        });
      }
      saveState();
      render();
      break;
    }
    case 'editor-merge-up': {
      const index = Number(dataset.index || -1);
      if (index > 0) {
        const items = [...state.videoEditor.mergeQueue];
        [items[index - 1], items[index]] = [items[index], items[index - 1]];
        state.videoEditor.mergeQueue = items;
        saveState();
        render();
      }
      break;
    }
    case 'editor-merge-down': {
      const index = Number(dataset.index || -1);
      if (index >= 0 && index < state.videoEditor.mergeQueue.length - 1) {
        const items = [...state.videoEditor.mergeQueue];
        [items[index + 1], items[index]] = [items[index], items[index + 1]];
        state.videoEditor.mergeQueue = items;
        saveState();
        render();
      }
      break;
    }
    case 'editor-merge-remove':
      state.videoEditor.mergeQueue = state.videoEditor.mergeQueue.filter((_, i) => i !== Number(dataset.index || -1));
      saveState();
      render();
      break;
    case 'save-video-edit':
      saveVideoEdit();
      break;
    case 'show-video-library':
      setVideoPanel('library');
      break;
    case 'show-video-params':
      setVideoPanel('params');
      break;
    case 'show-image-library':
      setImagePanel('library');
      break;
    case 'show-image-params':
      setImagePanel('params');
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
    case 'delete-history-item':
      deleteHistoryItem(dataset.generationId);
      break;
    case 'refresh-image-history':
      loadImageHistory();
      break;
    case 'use-image-history-item':
      loadImageHistoryItem(dataset.generationId, { silent: false }).then((item) => {
        if (item) applyImageHistoryItemToWorkspace(item);
      });
      break;
    case 'delete-image-history-item':
      deleteImageHistoryItem(dataset.generationId);
      break;
    case 'remove-upload-file':
      removeUploadFile(dataset.uploadId, dataset.index);
      break;
    case 'clear-image-run':
      clearImageRunState({ keepPrompt: true, keepFiles: true });
      render();
      break;
    case 'run-image':
      runImage();
      break;
    case 'load-voices': loadVoices(); break;
    case 'refresh-voice-history': loadVoiceHistory(); break;
    case 'use-voice-history-item':
      loadVoiceHistoryItem(dataset.generationId, { silent: false }).then((item) => {
        if (item) applyVoiceHistoryItemToWorkspace(item);
      });
      break;
    case 'delete-voice-history-item':
      deleteVoiceHistoryItem(dataset.generationId);
      break;
    case 'select-voice-card':
      state.voice.voiceId = dataset.voiceId || '';
      saveState();
      render();
      break;
    case 'clear-voice-stage':
      clearVoiceRunState({ keepText: true });
      render();
      break;
    case 'run-voice': runVoice(); break;
    case 'run-music': runMusic(); break;
    case 'run-songwriter': runSongwriter(); break;
    case 'songwriter-send': runSongwriter(); break;
    case 'songwriter-reset':
      state.music.songwriter.messages = [];
      state.music.songwriter.lastAnswer = '';
      state.music.songwriter.input = '';
      saveState();
      render();
      break;
    case 'songwriter-seed':
      seedMusicSongwriter();
      break;
    case 'music-set-tab':
      setMusicTab(dataset.tab);
      break;
    case 'music-open-songwriter':
      state.music.activeTab = 'songwriter';
      if (!state.music.songwriter.messages.length) seedMusicSongwriter();
      else {
        saveState();
        render();
      }
      break;
    case 'music-set-ai':
      state.music.ai = dataset.value || 'suno';
      ensureMusicCompatibility({ preserveLyricsTab: true });
      saveState();
      render();
      break;
    case 'music-apply-last-answer':
      applyLastSongwriterAnswer(dataset.target || 'lyrics');
      break;
    case 'music-fill-template':
      state.music.activeTab = 'idea';
      state.music.ideaText = 'Современный эмоциональный трек для рекламы школы танцев. Начало — лёгкое и интригующее, дальше рост энергии, в припеве мощный хук и ощущение большой сцены. Нужен premium вайб, urban lighting, мотивация, уверенность, движение вперёд, финал с сильным call-to-action.';
      saveState();
      render();
      break;
    case 'music-use-idea-as-title': {
      const source = trimText((state.music.ideaText || '').split(/[\n\.]/).find(Boolean) || '', 60).trim();
      if (source) state.music.title = source;
      saveState();
      render();
      break;
    }
    case 'refresh-music-history':
      loadMusicHistory({ silent: false, keepSelection: true });
      break;
    case 'music-open-history':
      if (state.musicHistory.selectedId) {
        loadMusicHistoryItem(state.musicHistory.selectedId, { silent: false });
      } else if (state.musicHistory.items.length) {
        loadMusicHistoryItem(state.musicHistory.items[0].id, { silent: false });
      } else {
        loadMusicHistory({ silent: false, keepSelection: true });
      }
      break;
    case 'use-music-history-item':
      loadMusicHistoryItem(dataset.generationId, { silent: false }).then((item) => {
        if (item) applyMusicHistoryItemToWorkspace(item);
      });
      break;
    case 'delete-music-history-item':
      deleteMusicHistoryItem(dataset.generationId);
      break;
    case 'send-music-to-chat': {
      const sourceText = decodeURIComponent(dataset.text || '') || state.music.ideaText || state.music.lyricsText || '';
      state.studio = 'chat';
      state.chat.input = `Помоги доработать музыкальную идею. Черновик: ${sourceText}`;
      render();
      break;
    }
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
    case 'image': runImage(); break;
    case 'voice': runVoice(); break;
    case 'music': runMusic(); break;
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
  renderLandingView();
  enhanceCustomSelects();
  attachImageCompareInteractions();
  initShowcaseMedia();
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
    loadVoiceHistory({ silent: true, keepSelection: true }).catch(() => {});
    if (state.image.panel === 'library') loadImageHistory({ silent: true }).catch(() => {});
  }
  if (state.voice.voices.length === 0) loadVoices();
  if (state.studio === 'library' || state.prompts.categories.length === 0) loadPromptCategories();
  if (state.video.providerTaskId && !state.video.outputUrl && !isVideoTaskFailed(state.video.lastStatus)) {
    startVideoPolling({ immediate: true });
  }
  if (state.videoEditor.lastJobId && ['queued', 'processing'].includes(String(state.videoEditor.status || ''))) {
    startVideoEditPolling({ immediate: true });
  }
  render();
}

init();


function initShowcaseMedia() {
  const videos = Array.from(document.querySelectorAll('[data-autoplay-observe]'));
  if (!videos.length) return;

  if (runtime.showcaseMediaObserver) {
    runtime.showcaseMediaObserver.disconnect();
    runtime.showcaseMediaObserver = null;
  }

  const prefersReducedMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const playVideo = (video) => {
    video.muted = true;
    video.playsInline = true;
    if (prefersReducedMotion) return;
    const playPromise = video.play();
    if (playPromise && typeof playPromise.catch === 'function') playPromise.catch(() => {});
  };

  if (!('IntersectionObserver' in window) || prefersReducedMotion) {
    videos.forEach(playVideo);
    return;
  }

  const observer = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      const video = entry.target;
      if (entry.isIntersecting && entry.intersectionRatio >= 0.35) {
        playVideo(video);
      } else {
        try { video.pause(); } catch (_) {}
      }
    });
  }, { threshold: [0, 0.35, 0.65] });

  videos.forEach((video) => observer.observe(video));
  runtime.showcaseMediaObserver = observer;
}
