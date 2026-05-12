
const FIXED_API_BASE = 'https://nabex.ru';
const DEFAULT_API_BASE = FIXED_API_BASE;
try { localStorage.removeItem('astrabot:apiBaseUrl'); } catch {}
const DEFAULT_AUTH_TOKEN = localStorage.getItem('astrabot:authToken') || '';
const DEFAULT_ME = JSON.parse(localStorage.getItem('astrabot:me') || 'null');
const DEFAULT_VIDEO_STATE = JSON.parse(localStorage.getItem('astrabot:videoState') || '{}');
const DEFAULT_IMAGE_STATE = JSON.parse(localStorage.getItem('astrabot:imageState') || '{}');
const DEFAULT_VOICE_STATE = JSON.parse(localStorage.getItem('astrabot:voiceState') || '{}');
const VOICE_FIXED_OUTPUT_FORMAT = 'mp3_44100_192';
const DEFAULT_VOICE_HISTORY_STATE = JSON.parse(localStorage.getItem('astrabot:voiceHistoryState') || '{}');
const DEFAULT_MUSIC_STATE = JSON.parse(localStorage.getItem('astrabot:musicState') || '{}');
const DEFAULT_MUSIC_HISTORY_STATE = JSON.parse(localStorage.getItem('astrabot:musicHistoryState') || '{}');
const DEFAULT_SITE_BUILDER_STATE = JSON.parse(localStorage.getItem('astrabot:siteBuilderState') || '{}');
const DEFAULT_VIDEO_EDITOR_STATE = JSON.parse(localStorage.getItem('astrabot:videoEditorState') || 'null');
const DEFAULT_AUTH_UI_STATE = JSON.parse(localStorage.getItem('astrabot:authUiState') || '{}');
const DEFAULT_PARTNER_STATE = JSON.parse(localStorage.getItem('astrabot:partnerState') || '{}');
const BOOT_QUERY = new URLSearchParams(window.location.search || '');
const PARTNER_REF_KEY = 'astrabot:partnerRefCode';
const PARTNER_BOT_USERNAME = 'NeiroAstraBot';
const PENDING_TOPUP_KEY = 'astrabot:pendingTopupTokens';
const PENDING_TOPUP_RETURN_KEY = 'astrabot:pendingTopupReturnUrl';
const SITE_BUILDER_BRIEF_SAMPLE_URL = 'https://storage.yandexcloud.net/astrabot-media-andre/brief.txt?response-content-disposition=attachment%3B%20filename%3Dbrief.txt&response-content-type=application%2Foctet-stream';

const CHAT_WELCOME_TEXT = 'Новый диалог открыт. Выбери модель, задай вопрос или прикрепи файл.';
const DEFAULT_CHAT_MODEL = localStorage.getItem('astrabot:chatModel') || 'gpt-4o-mini';
const DEFAULT_CHAT_MODE = localStorage.getItem('astrabot:chatMode') || 'chat';

function createChatWelcomeMessage() {
  return { role: 'system', content: CHAT_WELCOME_TEXT };
}

function defaultChatMessages() {
  return [createChatWelcomeMessage()];
}

function safeJsonParse(value, fallback) {
  try {
    if (value === null || value === undefined || value === '') return fallback;
    return JSON.parse(value);
  } catch (_e) {
    return fallback;
  }
}

function cleanChatStoragePart(value) {
  return String(value || 'chat').trim().replace(/[^a-zA-Z0-9_.-]+/g, '_').slice(0, 90) || 'chat';
}

function chatSessionStorageKey(kind, model = DEFAULT_CHAT_MODEL, mode = DEFAULT_CHAT_MODE) {
  return `astrabot:chat:${cleanChatStoragePart(mode)}:${cleanChatStoragePart(model)}:${kind}`;
}

function isValidChatMessages(value) {
  return Array.isArray(value) && value.some((item) => item && typeof item === 'object' && typeof item.content === 'string');
}

function readChatMessagesForSession(model, mode, options = {}) {
  const { allowLegacy = false } = options;
  const stored = safeJsonParse(localStorage.getItem(chatSessionStorageKey('messages', model, mode)), null);
  if (isValidChatMessages(stored)) return stored;

  if (allowLegacy) {
    const legacy = safeJsonParse(localStorage.getItem('astrabot:chatMessages'), null);
    if (isValidChatMessages(legacy)) return legacy;
  }
  return defaultChatMessages();
}

function readChatSummaryForSession(model, mode, options = {}) {
  const { allowLegacy = false } = options;
  const stored = localStorage.getItem(chatSessionStorageKey('summary', model, mode));
  if (stored !== null) return String(stored || '');
  return allowLegacy ? String(localStorage.getItem('astrabot:chatSummary') || '') : '';
}

function chatModelTitle(model) {
  const titles = {
    'gpt-4o-mini': 'GPT 4 mini',
    'gpt-5.4': 'GPT 5.4',
    'claude-sonnet-4-6': 'Claude Sonnet 4.6',
  };
  return titles[String(model || '')] || String(model || 'AI Chat');
}

function normalizePartnerRefCode(value) {
  const code = String(value || '').trim().toUpperCase().replace(/^REF[_-]/, '').replace(/[^A-Z0-9_-]/g, '');
  return code.length >= 3 && code.length <= 32 ? code : '';
}

function partnerSiteLink(profile = {}) {
  const code = normalizePartnerRefCode(profile.ref_code || '');
  return String(profile.site_link || profile.universal_link || (code ? `https://nabex.ru/?ref=${code}` : '') || '');
}

function partnerBotLink(profile = {}) {
  const code = normalizePartnerRefCode(profile.ref_code || '');
  return String(profile.bot_link || (code ? `https://t.me/${PARTNER_BOT_USERNAME}?start=ref_${code}` : '') || '');
}

function capturePartnerRefFromUrl() {
  const code = normalizePartnerRefCode(BOOT_QUERY.get('ref') || BOOT_QUERY.get('partner') || BOOT_QUERY.get('ref_code'));
  if (!code) return '';
  try { localStorage.setItem(PARTNER_REF_KEY, code); } catch {}
  return code;
}

capturePartnerRefFromUrl();

const runtime = {
  files: {},
  lastChatBootstrapLoaded: false,
  videoPollTimer: null,
  imagePollTimer: null,
  switchxRefPollTimer: null,
  voicePollTimer: null,
  videoEditPollTimer: null,
  musicPollTimer: null,
  musicToolPollTimer: null,
  musicSourceFile: null,
  showcaseMediaObserver: null,
  saveStateTimer: null,
  saveStatePending: false,
  pendingTopupInFlight: false,
  mobileUi: {
    navOpen: false,
    sheetOpen: false,
    sheetKind: 'settings',
  },
  switchxMaskEditor: {
    sourceSignature: '',
    frameDataUrl: '',
    frameWidth: 0,
    frameHeight: 0,
    maskDataUrl: '',
    brushSize: 28,
    tool: 'brush',
    loading: false,
    ready: false,
    errorText: '',
  },
};

const DEFAULT_APP_VIEW = localStorage.getItem('astrabot:view') || (window.location.hash === '#workspace' ? 'workspace' : 'showcase');

const FILE_INPUT_MAP = {
  chat_attachments: { key: 'chat.attachments', multiple: true },
  video_startFrame: { key: 'video.startFrame', multiple: false },
  video_endFrame: { key: 'video.endFrame', multiple: false },
  video_lastFrame: { key: 'video.lastFrame', multiple: false },
  video_referenceImages: { key: 'video.referenceImages', multiple: true },
  video_referenceAudios: { key: 'video.referenceAudios', multiple: true },
  video_referenceVideos: { key: 'video.referenceVideos', multiple: true },
  video_avatarImage: { key: 'video.avatarImage', multiple: false },
  video_motionVideo: { key: 'video.motionVideo', multiple: false },
  video_sourceVideo: { key: 'video.sourceVideo', multiple: false },
  video_switchxSelectMask: { key: 'video.switchxSelectMask', multiple: false },
  editorAudioUpload: { key: 'editor.audioUpload', multiple: false },
  editorMergeUpload: { key: 'editor.mergeUpload', multiple: false },
  music_sourceAudio: { key: 'music.sourceAudio', multiple: false },
  image_sourceImage: { key: 'image.sourceImage', multiple: false },
  image_baseImage: { key: 'image.baseImage', multiple: false },
  image_styleRefImage: { key: 'image.styleRefImage', multiple: false },
  image_omniRefImage: { key: 'image.omniRefImage', multiple: false },
  site_revision_files: { key: 'siteBuilder.revisionFiles', multiple: true },
};

function makeRuntimeFileEntry(file) {
  return { file, name: file.name, url: URL.createObjectURL(file), type: file.type || '', size: file.size || 0, lastModified: file.lastModified || 0 };
}

const state = {
  apiBaseUrl: DEFAULT_API_BASE,
  authToken: DEFAULT_AUTH_TOKEN,
  me: DEFAULT_ME,
  balance: null,
  balanceHistory: {
    items: [],
    loading: false,
    loaded: false,
    lastError: '',
    filter: 'all',
    limit: 30,
  },
  partner: {
    dashboard: DEFAULT_PARTNER_STATE.dashboard || null,
    loading: false,
    loaded: false,
    lastError: '',
    payoutSending: false,
  },
  authUi: {
    profileTab: DEFAULT_AUTH_UI_STATE.profileTab || 'login',
    modalTab: DEFAULT_AUTH_UI_STATE.modalTab || 'login',
    modalOpen: !!DEFAULT_AUTH_UI_STATE.modalOpen,
    registerPendingEmail: DEFAULT_AUTH_UI_STATE.registerPendingEmail || '',
    linkPendingEmail: DEFAULT_AUTH_UI_STATE.linkPendingEmail || '',
    resetPendingEmail: DEFAULT_AUTH_UI_STATE.resetPendingEmail || '',
  },
  apiOnline: false,
  view: DEFAULT_APP_VIEW,
  studio: (['workspace', 'billing'].includes(localStorage.getItem('astrabot:studio')) ? 'chat' : (localStorage.getItem('astrabot:studio') || 'chat')),
  recentRuns: JSON.parse(localStorage.getItem('astrabot:recentRuns') || '[]'),
  workspaceNotes: localStorage.getItem('astrabot:workspaceNotes') || '',
  bootstrap: {
    chatModels: ['gpt-4o-mini', 'gpt-5.4', 'claude-sonnet-4-6'],
    liveIntegrations: ['workspace_chat', 'balance', 'kling3', 'tts', 'songwriter', 'prompts'],
  },
  chat: {
    model: DEFAULT_CHAT_MODEL,
    mode: DEFAULT_CHAT_MODE,
    temperature: Number(localStorage.getItem('astrabot:chatTemperature') || '0.6'),
    maxTokens: Number(localStorage.getItem('astrabot:chatMaxTokens') || '900'),
    input: '',
    messages: readChatMessagesForSession(DEFAULT_CHAT_MODEL, DEFAULT_CHAT_MODE, { allowLegacy: true }),
    summary: readChatSummaryForSession(DEFAULT_CHAT_MODEL, DEFAULT_CHAT_MODE, { allowLegacy: true }),
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
  providerMode: DEFAULT_VIDEO_STATE.providerMode || 'normal',
  outputUrl: DEFAULT_VIDEO_STATE.outputUrl || '',
  downloadUrl: DEFAULT_VIDEO_STATE.downloadUrl || '',
  coverUrl: DEFAULT_VIDEO_STATE.coverUrl || '',
  percent: Number.isFinite(Number(DEFAULT_VIDEO_STATE.percent)) ? Number(DEFAULT_VIDEO_STATE.percent) : null,
  generationId: DEFAULT_VIDEO_STATE.generationId || '',
  providerTaskId: DEFAULT_VIDEO_STATE.providerTaskId || '',
  statusText: DEFAULT_VIDEO_STATE.statusText || 'Выбери модель, настрой параметры и нажми запуск.',
  errorText: DEFAULT_VIDEO_STATE.errorText || '',
  lastStatus: DEFAULT_VIDEO_STATE.lastStatus || 'idle',
  panel: DEFAULT_VIDEO_STATE.panel === 'library' ? 'library' : 'params',
  motionDurationSec: Number.isFinite(Number(DEFAULT_VIDEO_STATE.motionDurationSec)) ? Number(DEFAULT_VIDEO_STATE.motionDurationSec) : null,
  sourceVideoDurationSec: Number.isFinite(Number(DEFAULT_VIDEO_STATE.sourceVideoDurationSec)) ? Number(DEFAULT_VIDEO_STATE.sourceVideoDurationSec) : null,
  switchxSourceUploadId: DEFAULT_VIDEO_STATE.switchxSourceUploadId || '',
  switchxRefGenerationId: DEFAULT_VIDEO_STATE.switchxRefGenerationId || '',
  switchxReferenceImageUrl: DEFAULT_VIDEO_STATE.switchxReferenceImageUrl || '',
  switchxReferenceStatus: DEFAULT_VIDEO_STATE.switchxReferenceStatus || 'idle',
  switchxRefPrompt: DEFAULT_VIDEO_STATE.switchxRefPrompt || '',
  switchxAlphaMode: DEFAULT_VIDEO_STATE.switchxAlphaMode || 'auto',
  isGenerating: !!DEFAULT_VIDEO_STATE.isGenerating,
  requestStartedAt: DEFAULT_VIDEO_STATE.requestStartedAt || '',
  seedanceUseStartFrame: !!DEFAULT_VIDEO_STATE.seedanceUseStartFrame,
  seedanceUseLastFrame: !!DEFAULT_VIDEO_STATE.seedanceUseLastFrame,
  kling3NewShots: Array.isArray(DEFAULT_VIDEO_STATE.kling3NewShots) && DEFAULT_VIDEO_STATE.kling3NewShots.length ? DEFAULT_VIDEO_STATE.kling3NewShots : [
    { prompt: '', duration: '3' },
    { prompt: '', duration: '3' },
  ],
  kling3NewElements: Array.isArray(DEFAULT_VIDEO_STATE.kling3NewElements) ? DEFAULT_VIDEO_STATE.kling3NewElements : [],
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
    negativePrompt: DEFAULT_IMAGE_STATE.negativePrompt || '',
    mjStylize: Number.isFinite(Number(DEFAULT_IMAGE_STATE.mjStylize)) ? Number(DEFAULT_IMAGE_STATE.mjStylize) : 100,
    mjChaos: Number.isFinite(Number(DEFAULT_IMAGE_STATE.mjChaos)) ? Number(DEFAULT_IMAGE_STATE.mjChaos) : 0,
    mjRaw: !!DEFAULT_IMAGE_STATE.mjRaw,
    mjSpeedMode: DEFAULT_IMAGE_STATE.mjSpeedMode || 'fast',
    mjSeed: DEFAULT_IMAGE_STATE.mjSeed || '',
    outputUrl: DEFAULT_IMAGE_STATE.outputUrl || '',
    downloadUrl: DEFAULT_IMAGE_STATE.downloadUrl || '',
    beforeImageUrl: DEFAULT_IMAGE_STATE.beforeImageUrl || '',
    afterImageUrl: DEFAULT_IMAGE_STATE.afterImageUrl || '',
    imageUrls: Array.isArray(DEFAULT_IMAGE_STATE.imageUrls) ? DEFAULT_IMAGE_STATE.imageUrls : [],
    availableActions: DEFAULT_IMAGE_STATE.availableActions && typeof DEFAULT_IMAGE_STATE.availableActions === 'object' ? DEFAULT_IMAGE_STATE.availableActions : {},
    activeImageIndex: Number.isFinite(Number(DEFAULT_IMAGE_STATE.activeImageIndex)) ? Number(DEFAULT_IMAGE_STATE.activeImageIndex) : 0,
    compareMode: !!DEFAULT_IMAGE_STATE.compareMode,
    comparePosition: Number.isFinite(Number(DEFAULT_IMAGE_STATE.comparePosition)) ? Number(DEFAULT_IMAGE_STATE.comparePosition) : 50,
    generationId: DEFAULT_IMAGE_STATE.generationId || '',
    panel: DEFAULT_IMAGE_STATE.panel === 'library' ? 'library' : 'params',
    isGenerating: !!DEFAULT_IMAGE_STATE.isGenerating,
    errorText: DEFAULT_IMAGE_STATE.errorText || '',
    statusText: DEFAULT_IMAGE_STATE.statusText || 'Выбери режим, добавь изображения при необходимости и запусти генерацию.',
    requestStartedAt: DEFAULT_IMAGE_STATE.requestStartedAt || '',
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
    outputFormat: VOICE_FIXED_OUTPUT_FORMAT,
    languageCode: DEFAULT_VOICE_STATE.languageCode || 'ru',
    manualVoiceSettings: !!DEFAULT_VOICE_STATE.manualVoiceSettings,
    showAdvancedPanel: !!DEFAULT_VOICE_STATE.showAdvancedPanel,
    stability: Number.isFinite(Number(DEFAULT_VOICE_STATE.stability)) ? Number(DEFAULT_VOICE_STATE.stability) : 0.5,
    similarityBoost: Number.isFinite(Number(DEFAULT_VOICE_STATE.similarityBoost)) ? Number(DEFAULT_VOICE_STATE.similarityBoost) : 0.75,
    style: Number.isFinite(Number(DEFAULT_VOICE_STATE.style)) ? Number(DEFAULT_VOICE_STATE.style) : 0,
    speed: Number.isFinite(Number(DEFAULT_VOICE_STATE.speed)) ? Number(DEFAULT_VOICE_STATE.speed) : 1,
    useSpeakerBoost: true,
    text: DEFAULT_VOICE_STATE.text || '',
    audioUrl: DEFAULT_VOICE_STATE.audioUrl || '',
    downloadUrl: DEFAULT_VOICE_STATE.downloadUrl || '',
    generationId: DEFAULT_VOICE_STATE.generationId || '',
    voices: [],
    isGenerating: !!DEFAULT_VOICE_STATE.isGenerating,
    errorText: DEFAULT_VOICE_STATE.errorText || '',
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
    model: DEFAULT_MUSIC_STATE.model || 'V4_5',
    title: DEFAULT_MUSIC_STATE.title || '',
    tags: DEFAULT_MUSIC_STATE.tags || '',
    language: DEFAULT_MUSIC_STATE.language || 'ru',
    mood: DEFAULT_MUSIC_STATE.mood || '',
    references: DEFAULT_MUSIC_STATE.references || '',
    negativeTags: DEFAULT_MUSIC_STATE.negativeTags || '',
    vocalGender: DEFAULT_MUSIC_STATE.vocalGender || '',
    styleWeight: Number(DEFAULT_MUSIC_STATE.styleWeight ?? 0.65),
    weirdnessConstraint: Number(DEFAULT_MUSIC_STATE.weirdnessConstraint ?? 0.65),
    audioWeight: Number(DEFAULT_MUSIC_STATE.audioWeight ?? 0.65),
    personaId: DEFAULT_MUSIC_STATE.personaId || '',
    personaModel: DEFAULT_MUSIC_STATE.personaModel || 'style_persona',
    instrumental: !!DEFAULT_MUSIC_STATE.instrumental,
    ideaText: DEFAULT_MUSIC_STATE.ideaText || '',
    lyricsText: DEFAULT_MUSIC_STATE.lyricsText || '',
    lyricsPrompt: DEFAULT_MUSIC_STATE.lyricsPrompt || '',
    generatedLyrics: Array.isArray(DEFAULT_MUSIC_STATE.generatedLyrics) ? DEFAULT_MUSIC_STATE.generatedLyrics : [],
    timestampedLyrics: DEFAULT_MUSIC_STATE.timestampedLyrics && typeof DEFAULT_MUSIC_STATE.timestampedLyrics === 'object' ? DEFAULT_MUSIC_STATE.timestampedLyrics : {},
    generationId: DEFAULT_MUSIC_STATE.generationId || '',
    isGenerating: !!DEFAULT_MUSIC_STATE.isGenerating,
    status: DEFAULT_MUSIC_STATE.status || 'idle',
    statusText: DEFAULT_MUSIC_STATE.statusText || 'Заполни идею или текст и запусти генерацию.',
    errorText: DEFAULT_MUSIC_STATE.errorText || '',
    lastCompletedAt: DEFAULT_MUSIC_STATE.lastCompletedAt || '',
    results: Array.isArray(DEFAULT_MUSIC_STATE.results) ? DEFAULT_MUSIC_STATE.results : [],
    toolAction: DEFAULT_MUSIC_STATE.toolAction || 'upload-cover',
    toolTaskId: DEFAULT_MUSIC_STATE.toolTaskId || '',
    toolTaskStatus: DEFAULT_MUSIC_STATE.toolTaskStatus || 'idle',
    toolTaskMessage: DEFAULT_MUSIC_STATE.toolTaskMessage || '',
    toolTracks: Array.isArray(DEFAULT_MUSIC_STATE.toolTracks) ? DEFAULT_MUSIC_STATE.toolTracks : [],
    uploadFileName: DEFAULT_MUSIC_STATE.uploadFileName || '',
    extendAudioId: DEFAULT_MUSIC_STATE.extendAudioId || '',
    toolPrompt: DEFAULT_MUSIC_STATE.toolPrompt || '',
    toolPromptMode: DEFAULT_MUSIC_STATE.toolPromptMode || (DEFAULT_MUSIC_STATE.mode === 'lyrics' ? 'lyrics' : 'idea'),
    continueAt: Number(DEFAULT_MUSIC_STATE.continueAt ?? 60),
    useCustomParams: DEFAULT_MUSIC_STATE.useCustomParams !== false,
    personaName: DEFAULT_MUSIC_STATE.personaName || '',
    personaDescription: DEFAULT_MUSIC_STATE.personaDescription || '',
    personaResult: DEFAULT_MUSIC_STATE.personaResult || null,
    showAdvancedPanel: !!DEFAULT_MUSIC_STATE.showAdvancedPanel,
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
    openItemId: '',
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
  siteBuilder: {
    projects: [],
    loading: false,
    loaded: false,
    selectedProjectId: DEFAULT_SITE_BUILDER_STATE.selectedProjectId || '',
    selectedProject: null,
    versions: [],
    jobs: [],
    lastError: '',
    prices: {
      create: Number(DEFAULT_SITE_BUILDER_STATE.createPrice || 30),
      revision: Number(DEFAULT_SITE_BUILDER_STATE.revisionPrice || 10),
    },
    create: {
      title: DEFAULT_SITE_BUILDER_STATE.title || '',
      briefRaw: DEFAULT_SITE_BUILDER_STATE.briefRaw || '',
      extraTextsRaw: DEFAULT_SITE_BUILDER_STATE.extraTextsRaw || '',
    },
    revisionText: DEFAULT_SITE_BUILDER_STATE.revisionText || '',
    hiddenProjects: Array.isArray(DEFAULT_SITE_BUILDER_STATE.hiddenProjects) ? DEFAULT_SITE_BUILDER_STATE.hiddenProjects : [],
    hiddenVersions: Array.isArray(DEFAULT_SITE_BUILDER_STATE.hiddenVersions) ? DEFAULT_SITE_BUILDER_STATE.hiddenVersions : [],
    hiddenJobs: Array.isArray(DEFAULT_SITE_BUILDER_STATE.hiddenJobs) ? DEFAULT_SITE_BUILDER_STATE.hiddenJobs : [],
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
  video: { icon: 'video', title: 'Video Studio', subtitle: 'Kling, Veo, Grok, Seedance и Sora в одной рабочей зоне с живыми настройками.', eyebrow: 'Video generation' },
  image: { icon: 'image', title: 'Image Studio', subtitle: 'Nano Banana, Seedream, апскейл и image-to-image сценарии.', eyebrow: 'Image generation' },
  voice: { icon: 'voice', title: 'Voice Studio', subtitle: 'Озвучка, выбор голоса и быстрый экспорт результата.', eyebrow: 'Voice workflow' },
  music: { icon: 'music', title: 'Music Studio', subtitle: 'Suno, Udio, генератор текста и понятная рабочая зона для генерации треков.', eyebrow: 'Музыкальная студия' },
  library: { icon: 'library', title: 'Prompt Library', subtitle: 'Категории, группы и карточки промптов с быстрым переносом в студии.', eyebrow: 'Prompt system' },
  workspace: { icon: 'workspace', title: 'Workspace', subtitle: 'Планы, референсы, заметки и проектная логика поверх генераций.', eyebrow: 'Project hub' },
  history: { icon: 'site', title: 'Site Creator', subtitle: 'Создание сайта, версии, правки и скачивание ZIP в одном разделе.', eyebrow: 'Website builder' },
  billing: { icon: 'billing', title: 'Billing', subtitle: 'Баланс, токены и экономика генераций.', eyebrow: 'Token economy' },
  profile: { icon: 'profile', title: 'Profile', subtitle: 'Telegram-связка, базовые настройки и состояние системы.', eyebrow: 'Account and access' },
  partner: { icon: 'partner', title: 'Партнёрка', subtitle: 'Реферальная ссылка, статистика, начисления и вывод средств.', eyebrow: 'Referral program' },
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
  site: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6.5A2.5 2.5 0 0 1 6.5 4h11A2.5 2.5 0 0 1 20 6.5v11a2.5 2.5 0 0 1-2.5 2.5h-11A2.5 2.5 0 0 1 4 17.5v-11Z" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M4 9h16M9 20V9" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>',
  billing: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3.5" y="6" width="17" height="12" rx="3" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M3.5 10.5h17M8 14h3" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>',
  profile: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="8" r="3.2" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M5.5 19a6.5 6.5 0 0 1 13 0" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>',
  partner: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 12a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm8 6a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M10.7 10.7l2.6 2.6M5 19a5 5 0 0 1 6 0M13 6a5 5 0 0 1 6 0" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>',
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
      'kling-3.0-new': {
        name: 'Kling 3.0 - New',
        backend: 'live',
        modes: {
          text_to_video: { name: 'Text → Video', fields: ['prompt', 'duration', 'resolution', 'aspectRatio', 'enableAudio'] },
          image_to_video: { name: 'Image → Video', fields: ['startFrame', 'endFrame', 'prompt', 'duration', 'resolution', 'aspectRatio', 'enableAudio'] },
          multi_shot: { name: 'Multi-shot', fields: ['startFrame', 'kling3NewShots', 'kling3NewElements', 'resolution', 'aspectRatio', 'enableAudio'] },
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
  grok: {
    name: 'Grok',
    models: {
      'grok-imagine-video': {
        name: 'Grok Imagine Video',
        backend: 'live',
        modes: {
          text_to_video: { name: 'Text → Video', fields: ['prompt', 'providerModeGrok', 'durationGrok', 'resolutionGrok', 'aspectRatioGrok'] },
          image_to_video: { name: 'Image → Video', fields: ['startFrame', 'prompt', 'providerModeGrok', 'durationGrok', 'resolutionGrok', 'aspectRatioGrok'] },
        },
      },
    },
  },
  seedance: {
    name: 'Seedance 2.0 Preview',
    models: {
      'seedance-preview': {
        name: 'Seedance 2.0 Preview',
        backend: 'live',
        modes: {
          text_to_video: { name: 'Text → Video', fields: ['prompt'] },
          image_to_video: { name: 'Image → Video', fields: ['prompt', 'referenceImages'] },
        },
      },
      'seedance-fast': {
        name: 'Seedance 2.0 Preview Fast',
        backend: 'live',
        modes: {
          text_to_video: { name: 'Text → Video', fields: ['prompt'] },
          image_to_video: { name: 'Image → Video', fields: ['prompt', 'referenceImages'] },
        },
      },
    },
  },
  seedance_kie: {
    name: 'Seedance 2.0',
    models: {
      'seedance-kie': {
        name: 'Seedance 2.0',
        backend: 'live',
        modes: {
          text_to_video: { name: 'Text → Video', fields: ['prompt'] },
          image_to_video: { name: 'Image → Video', fields: ['prompt', 'referenceImages', 'referenceAudios', 'startFrame', 'lastFrame'] },
          omni_reference: { name: 'Omni Reference', fields: ['prompt', 'referenceImages', 'referenceAudios', 'referenceVideos'] },
        },
      },
      'seedance-kie-fast': {
        name: 'Seedance 2.0 Fast',
        backend: 'live',
        modes: {
          text_to_video: { name: 'Text → Video', fields: ['prompt'] },
          image_to_video: { name: 'Image → Video', fields: ['prompt', 'referenceImages', 'referenceAudios', 'startFrame', 'lastFrame'] },
          omni_reference: { name: 'Omni Reference', fields: ['prompt', 'referenceImages', 'referenceAudios', 'referenceVideos'] },
        },
      },
    },
  },
  pixverse_c1: {
    name: 'PixVerse C1',
    models: {
      c1: {
        name: 'C1',
        backend: 'live',
        modes: {
          text_to_video: { name: 'Text → Video', fields: ['prompt'] },
          image_to_video: { name: 'Image → Video', fields: ['startFrame', 'prompt'] },
          transition: { name: 'First + Last Frame', fields: ['startFrame', 'lastFrame', 'prompt'] },
          fusion: { name: 'Reference / Fusion', fields: ['referenceImages', 'prompt'] },
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
  switchx: {
    name: 'SwitchX',
    models: {
      'switchx': {
        name: 'SwitchX',
        backend: 'live',
        modes: {
          video_swap: { name: 'Video Swap', fields: ['sourceVideo', 'referenceImages', 'prompt', 'resolutionSwitchx'] },
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
  nano_banana_2: {
    name: 'Nano Banana 2',
    models: {
      'nano-banana-2': {
        name: 'Nano Banana 2',
        backend: 'live',
        modes: {
          image_to_image: { name: 'Image → Image', fields: ['sourceImage', 'prompt', 'resolutionImage', 'aspectRatioImage'] },
          text_to_image: { name: 'Text → Image', fields: ['prompt', 'resolutionImage', 'aspectRatioImageText'] },
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
  nano_banana_pro_new: {
    name: 'Nano Banana Pro - NEW',
    models: {
      'nano-banana-pro-kie': {
        name: 'Nano Banana Pro - NEW',
        backend: 'live',
        modes: {
          image_to_image: { name: 'Image → Image', fields: ['sourceImage', 'prompt', 'resolutionImage', 'aspectRatioImage'] },
          text_to_image: { name: 'Text → Image', fields: ['prompt', 'resolutionImage', 'aspectRatioImageText'] },
        },
      },
    },
  },
  midjourney: {
    name: 'Midjourney',
    models: {
      'midjourney-v7': {
        name: 'Midjourney V7',
        backend: 'live',
        modes: {
          text_to_image: { name: 'Text → Image', fields: ['prompt', 'aspectRatioImageText'] },
        },
      },
    },
  },
  seedream: {
    name: 'Seedream',
    models: {
      'seedream-45': {
        name: 'Seedream 4.5',
        backend: 'live',
        modes: {
          single: { name: 'Seedream 4.5', fields: ['sourceImage', 'prompt', 'resolutionImage', 'aspectRatioImage'] },
          t2i: { name: 'Текст → Картинка', fields: ['prompt', 'resolutionImage', 'aspectRatioImageText'] },
          i2i: { name: 'Картинка + Картинка', fields: ['baseImage', 'sourceImage', 'prompt', 'resolutionImage', 'aspectRatioImage'] },
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
  gpt_image_2: {
    name: 'GPT Image 2.0',
    models: {
      'gpt-image-2': {
        name: 'GPT Image 2.0',
        backend: 'live',
        modes: {
          text_to_image: { name: 'Text → Image', fields: ['prompt', 'aspectRatioImageText'] },
          image_to_image: { name: 'Image → Image', fields: ['sourceImage', 'prompt', 'aspectRatioImage'] },
        },
      },
    },
  },
  posters: {
    hidden: true,
    name: 'Фото / Афиши (legacy)',
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


function getDownloadFilenameFromUrl(url, fallback = 'image.png') {
  try {
    const clean = String(url || '').split('?')[0].split('#')[0];
    const name = clean.substring(clean.lastIndexOf('/') + 1);
    return name ? decodeURIComponent(name) : fallback;
  } catch (_) {
    return fallback;
  }
}

async function forceDownloadFile(url, fallbackName = 'image.png') {
  if (!url) {
    toast('error', 'Нет файла', 'Ссылка на скачивание не найдена.');
    return;
  }

  try {
    const res = await fetch(url, { mode: 'cors', credentials: 'omit' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);

    const blob = await res.blob();
    const blobUrl = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = blobUrl;
    a.download = getDownloadFilenameFromUrl(url, fallbackName);
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(blobUrl), 1500);
  } catch (_) {
    toast('error', 'Скачать не удалось', 'Источник не дал скачать файл напрямую. Нужен CORS или backend-прокси.');
  }
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

function writeStateSnapshot() {
  state.apiBaseUrl = FIXED_API_BASE;
  localStorage.setItem('astrabot:studio', state.studio);
  localStorage.removeItem('astrabot:apiBaseUrl');
  localStorage.setItem('astrabot:authToken', state.authToken || '');
  localStorage.setItem('astrabot:me', JSON.stringify(state.me || null));
  localStorage.setItem('astrabot:authUiState', JSON.stringify(state.authUi || {}));
  localStorage.setItem('astrabot:recentRuns', JSON.stringify(state.recentRuns.slice(0, 50)));
  localStorage.setItem('astrabot:workspaceNotes', state.workspaceNotes);
  localStorage.setItem('astrabot:chatModel', state.chat.model);
  localStorage.setItem('astrabot:chatMode', state.chat.mode);
  localStorage.setItem('astrabot:chatTemperature', String(state.chat.temperature));
  localStorage.setItem('astrabot:chatMaxTokens', String(state.chat.maxTokens));
  persistCurrentChatSession();
  localStorage.removeItem('astrabot:chatMessages');
  localStorage.removeItem('astrabot:chatSummary');
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
    providerMode: state.video.providerMode || 'normal',
    motionDurationSec: state.video.motionDurationSec,
    sourceVideoDurationSec: state.video.sourceVideoDurationSec,
    outputUrl: state.video.outputUrl || '',
    downloadUrl: state.video.downloadUrl || '',
    coverUrl: state.video.coverUrl || '',
    percent: Number.isFinite(Number(state.video.percent)) ? Number(state.video.percent) : null,
    generationId: state.video.generationId || '',
    providerTaskId: state.video.providerTaskId || '',
    statusText: state.video.statusText || '',
    errorText: state.video.errorText || '',
    lastStatus: state.video.lastStatus || 'idle',
    panel: state.video.panel || 'params',
    isGenerating: !!state.video.isGenerating,
    requestStartedAt: state.video.requestStartedAt || '',
    switchxSourceUploadId: state.video.switchxSourceUploadId || '',
    switchxRefGenerationId: state.video.switchxRefGenerationId || '',
    switchxReferenceImageUrl: state.video.switchxReferenceImageUrl || '',
    switchxReferenceStatus: state.video.switchxReferenceStatus || 'idle',
    switchxRefPrompt: state.video.switchxRefPrompt || '',
    switchxAlphaMode: state.video.switchxAlphaMode || 'auto',
    seedanceUseStartFrame: !!state.video.seedanceUseStartFrame,
    seedanceUseLastFrame: !!state.video.seedanceUseLastFrame,
    kling3NewShots: getKling3NewShots(),
    kling3NewElements: getKling3NewElements(),
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
    outputUrl: state.image.outputUrl || '',
    downloadUrl: state.image.downloadUrl || '',
    beforeImageUrl: state.image.beforeImageUrl || '',
    afterImageUrl: state.image.afterImageUrl || '',
    compareMode: !!state.image.compareMode,
    comparePosition: Number.isFinite(Number(state.image.comparePosition)) ? Number(state.image.comparePosition) : 50,
    generationId: state.image.generationId || '',
    isGenerating: !!state.image.isGenerating,
    errorText: state.image.errorText || '',
    statusText: state.image.statusText || '',
    requestStartedAt: state.image.requestStartedAt || '',
  }));
  localStorage.setItem('astrabot:voiceState', JSON.stringify({
    voiceId: state.voice.voiceId,
    modelId: state.voice.modelId,
    outputFormat: state.voice.outputFormat,
    languageCode: state.voice.languageCode,
    manualVoiceSettings: !!state.voice.manualVoiceSettings,
    showAdvancedPanel: !!state.voice.showAdvancedPanel,
    stability: Number(state.voice.stability),
    similarityBoost: Number(state.voice.similarityBoost),
    style: Number(state.voice.style),
    speed: Number(state.voice.speed),
    useSpeakerBoost: true,
    text: state.voice.text,
    audioUrl: state.voice.audioUrl || '',
    downloadUrl: state.voice.downloadUrl || '',
    generationId: state.voice.generationId || '',
    isGenerating: !!state.voice.isGenerating,
    errorText: state.voice.errorText || '',
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
    model: state.music.model,
    title: state.music.title,
    tags: state.music.tags,
    language: state.music.language,
    mood: state.music.mood,
    references: state.music.references,
    negativeTags: state.music.negativeTags,
    vocalGender: state.music.vocalGender,
    styleWeight: Number(state.music.styleWeight ?? 0.65),
    weirdnessConstraint: Number(state.music.weirdnessConstraint ?? 0.65),
    audioWeight: Number(state.music.audioWeight ?? 0.65),
    personaId: state.music.personaId,
    personaModel: state.music.personaModel,
    instrumental: !!state.music.instrumental,
    ideaText: state.music.ideaText,
    lyricsText: state.music.lyricsText,
    lyricsPrompt: state.music.lyricsPrompt,
    generatedLyrics: Array.isArray(state.music.generatedLyrics) ? state.music.generatedLyrics.slice(0, 6) : [],
    timestampedLyrics: state.music.timestampedLyrics || {},
    generationId: state.music.generationId || '',
    isGenerating: !!state.music.isGenerating,
    status: state.music.status || 'idle',
    statusText: state.music.statusText || '',
    errorText: state.music.errorText || '',
    lastCompletedAt: state.music.lastCompletedAt || '',
    results: Array.isArray(state.music.results) ? state.music.results.slice(0, 8) : [],
    toolAction: state.music.toolAction || 'upload-cover',
    toolTaskId: state.music.toolTaskId || '',
    toolTaskStatus: state.music.toolTaskStatus || 'idle',
    toolTaskMessage: state.music.toolTaskMessage || '',
    toolTracks: Array.isArray(state.music.toolTracks) ? state.music.toolTracks.slice(0, 8) : [],
    uploadFileName: state.music.uploadFileName || '',
    extendAudioId: state.music.extendAudioId || '',
    toolPrompt: state.music.toolPrompt || '',
    toolPromptMode: state.music.toolPromptMode || 'lyrics',
    continueAt: Number(state.music.continueAt ?? 60),
    useCustomParams: state.music.useCustomParams !== false,
    personaName: state.music.personaName || '',
    personaDescription: state.music.personaDescription || '',
    personaResult: state.music.personaResult || null,
    showAdvancedPanel: !!state.music.showAdvancedPanel,
    songwriterInput: state.music.songwriter.input || '',
    songwriterLastAnswer: state.music.songwriter.lastAnswer || '',
    songwriterMessages: Array.isArray(state.music.songwriter.messages) ? state.music.songwriter.messages.slice(-20) : [],
  }));
  localStorage.setItem('astrabot:musicHistoryState', JSON.stringify({
    selectedId: state.musicHistory.selectedId || '',
  }));
  localStorage.setItem('astrabot:siteBuilderState', JSON.stringify({
    selectedProjectId: state.siteBuilder.selectedProjectId || '',
    title: state.siteBuilder.create.title || '',
    briefRaw: state.siteBuilder.create.briefRaw || '',
    extraTextsRaw: state.siteBuilder.create.extraTextsRaw || '',
    revisionText: state.siteBuilder.revisionText || '',
    createPrice: Number(state.siteBuilder.prices?.create || 30),
    revisionPrice: Number(state.siteBuilder.prices?.revision || 10),
    hiddenProjects: Array.isArray(state.siteBuilder.hiddenProjects) ? state.siteBuilder.hiddenProjects.slice(-120) : [],
    hiddenVersions: Array.isArray(state.siteBuilder.hiddenVersions) ? state.siteBuilder.hiddenVersions.slice(-240) : [],
    hiddenJobs: Array.isArray(state.siteBuilder.hiddenJobs) ? state.siteBuilder.hiddenJobs.slice(-240) : [],
  }));
}

function flushSaveState() {
  if (runtime.saveStateTimer) {
    clearTimeout(runtime.saveStateTimer);
    runtime.saveStateTimer = null;
  }
  if (!runtime.saveStatePending) return;
  runtime.saveStatePending = false;
  writeStateSnapshot();
}

function saveState(options = {}) {
  const immediate = options === true || options?.immediate === true;
  if (immediate) {
    runtime.saveStatePending = true;
    flushSaveState();
    return;
  }
  runtime.saveStatePending = true;
  if (runtime.saveStateTimer) clearTimeout(runtime.saveStateTimer);
  runtime.saveStateTimer = window.setTimeout(() => {
    flushSaveState();
  }, 180);
}

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') flushSaveState();
});
window.addEventListener('pagehide', flushSaveState);

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

function formatRub(value) {
  const n = Number(value || 0);
  if (!Number.isFinite(n)) return '0 ₽';
  return `${n.toLocaleString('ru-RU', { maximumFractionDigits: 2 })} ₽`;
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
  stopVoicePolling();
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

function voiceHistoryStatus(item) {
  return String(item?.status || '').trim().toLowerCase();
}

function voiceHistoryCanHydrateWorkspace(item) {
  const status = voiceHistoryStatus(item);
  if (voiceHistoryAudioUrl(item)) return true;
  return ['failed', 'error', 'cancelled', 'canceled'].includes(status);
}

function applyVoiceHistoryItemToWorkspace(item, options = {}) {
  if (!item) return;
  stopVoicePolling();
  const { silent = false } = options;
  revokeVoiceAudioUrl();
  state.voiceHistory.selectedId = item.id || '';
  state.voiceHistory.selectedItem = item;
  state.voice.generationId = item.id || '';
  state.voice.audioUrl = voiceHistoryAudioUrl(item);
  state.voice.downloadUrl = voiceHistoryDownloadUrl(item);
  state.voice.errorText = item.error_message || '';
  state.voice.lastGeneratedAt = item.completed_at || item.created_at || '';
  state.voice.isGenerating = false;
  if (item.voice_id) state.voice.voiceId = item.voice_id;
  if (item.model) state.voice.modelId = item.model;
  state.voice.outputFormat = VOICE_FIXED_OUTPUT_FORMAT;
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

function getApiBaseUrl() {
  return String(FIXED_API_BASE || '').replace(/\/$/, '');
}

async function apiFetch(path, options = {}) {
  const base = getApiBaseUrl();
  if (!base) throw new Error('API Base URL is empty');

  const headers = new Headers(options.headers || {});
  if (state.authToken && !headers.has('Authorization')) {
    headers.set('Authorization', `Bearer ${state.authToken}`);
  }

  const res = await fetch(`${base}${path}`, { ...options, headers });

  if (!res.ok) {
    let detail = res.statusText || `HTTP ${res.status}`;

    try {
      const raw = await res.text();
      if (raw) {
        try {
          const data = JSON.parse(raw);
          detail = data.detail || data.error || data.message || raw;
        } catch {
          detail = raw;
        }
      }
    } catch {
      // keep default detail
    }

    throw new Error(detail || `HTTP ${res.status}`);
  }

  return res;
}

function readPendingTopupTokens() {
  const fromQuery = Number(BOOT_QUERY.get('topup') || '0');
  if (Number.isFinite(fromQuery) && fromQuery > 0) return fromQuery;
  const fromStorage = Number(localStorage.getItem(PENDING_TOPUP_KEY) || '0');
  return Number.isFinite(fromStorage) && fromStorage > 0 ? fromStorage : 0;
}

function readPendingTopupReturnUrl() {
  try {
    return localStorage.getItem(PENDING_TOPUP_RETURN_KEY) || '';
  } catch (_) {
    return '';
  }
}

function clearPendingTopup() {
  try {
    localStorage.removeItem(PENDING_TOPUP_KEY);
    localStorage.removeItem(PENDING_TOPUP_RETURN_KEY);
  } catch (_) {}
}

function clearBootAuthParams() {
  try {
    const url = new URL(window.location.href);
    url.searchParams.delete('auth');
    url.searchParams.delete('topup');
    const next = `${url.pathname}${url.search}${url.hash}`;
    history.replaceState(null, '', next);
  } catch (_) {}
}

async function startWorkspaceTopup(tokens, options = {}) {
  const numericTokens = Number(tokens || 0);
  if (!Number.isFinite(numericTokens) || numericTokens <= 0) throw new Error('Неизвестный пакет пополнения.');
  if (!requireAuth()) return null;
  const returnUrl = String(options.returnUrl || readPendingTopupReturnUrl() || window.location.href);
  const res = await apiFetch('/api/workspace/topup/create', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tokens: numericTokens, return_url: returnUrl }),
  });
  const data = await res.json();
  clearPendingTopup();
  clearBootAuthParams();
  if (options.redirect !== false && data?.confirmation_url) {
    window.location.href = data.confirmation_url;
  }
  return data;
}

async function resumePendingTopup() {
  if (runtime.pendingTopupInFlight) return false;
  const tokens = readPendingTopupTokens();
  if (!tokens || !state.authToken || !state.me) return false;
  runtime.pendingTopupInFlight = true;
  try {
    await startWorkspaceTopup(tokens, { redirect: true });
    return true;
  } catch (e) {
    clearPendingTopup();
    clearBootAuthParams();
    toast('error', 'Не удалось создать оплату', String(e.message || e));
    return false;
  } finally {
    runtime.pendingTopupInFlight = false;
  }
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

function isImageSourceMultipleMode() {
  return (state.image.provider === 'nano_banana_pro_new' && state.image.mode === 'image_to_image')
    || (state.image.provider === 'gpt_image_2' && state.image.mode === 'image_to_image');
}

function removeUploadFile(inputId, index = null) {
  const config = FILE_INPUT_MAP[inputId];
  if (!config) return;
  const current = runtime.files[config.key];
  if (!current) return;

  const isMultiple = config.multiple || (inputId === 'image_sourceImage' && isImageSourceMultipleMode());

  if (isMultiple && Array.isArray(current)) {
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

  if (inputId === 'video_sourceVideo') {
    state.video.switchxSourceUploadId = '';
    state.video.switchxReferenceImageUrl = '';
    state.video.switchxRefGenerationId = '';
    state.video.switchxReferenceStatus = 'idle';
    state.video.sourceVideoDurationSec = null;
    resetSwitchxMaskEditor({ clearMaskFile: true });
  }
  if (inputId === 'video_switchxSelectMask') {
    const editor = runtime.switchxMaskEditor || {};
    editor.maskDataUrl = '';
  }

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

function persistCurrentChatSession() {
  const messages = Array.isArray(state?.chat?.messages) ? state.chat.messages.slice(-50) : defaultChatMessages();
  localStorage.setItem(chatSessionStorageKey('messages', state.chat.model, state.chat.mode), JSON.stringify(messages));
  localStorage.setItem(chatSessionStorageKey('summary', state.chat.model, state.chat.mode), state.chat.summary || '');
}

function loadCurrentChatSession(options = {}) {
  const { allowLegacy = false, keepInput = false } = options;
  state.chat.messages = readChatMessagesForSession(state.chat.model, state.chat.mode, { allowLegacy });
  state.chat.summary = readChatSummaryForSession(state.chat.model, state.chat.mode, { allowLegacy });
  if (!keepInput) state.chat.input = '';
  clearChatAttachments();
}

function switchChatSession(nextModel, nextMode, options = {}) {
  persistCurrentChatSession();
  state.chat.model = nextModel || state.chat.model;
  state.chat.mode = nextMode || state.chat.mode;
  ensureChatModeCompatibility(!!options.showToast);
  loadCurrentChatSession({ allowLegacy: false, keepInput: false });
}

function startNewChatSession(options = {}) {
  state.chat.messages = defaultChatMessages();
  state.chat.summary = '';
  state.chat.input = '';
  clearChatAttachments();
  localStorage.removeItem(chatSessionStorageKey('messages', state.chat.model, state.chat.mode));
  localStorage.removeItem(chatSessionStorageKey('summary', state.chat.model, state.chat.mode));
  if (options.renderNow !== false) {
    saveState();
    render();
    scrollChatToBottom();
  }
  if (options.toast !== false) toast('success', 'Новый диалог', 'Контекст текущего чата очищен.');
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

function chatAttachmentKindLabel(item) {
  const type = String(item?.type || '').toLowerCase();
  if (type.startsWith('image/')) return 'Изображение';
  if (type.startsWith('video/')) return 'Видео';
  if (type.startsWith('audio/')) return 'Аудио';
  if (type === 'application/pdf') return 'PDF';
  const ext = String(item?.name || '').split('.').pop();
  return ext && ext !== String(item?.name || '') ? ext.toUpperCase() : 'Файл';
}

function renderChatAttachmentCard(item, index) {
  const type = String(item?.type || '').toLowerCase();
  const isImage = type.startsWith('image/');
  const isVideo = type.startsWith('video/');
  const media = isImage
    ? `<img class="chat-file-thumb-media" src="${escapeHtml(item?.url || '')}" alt="${escapeHtml(item?.name || 'attachment')}">`
    : isVideo
      ? `<video class="chat-file-thumb-media" src="${escapeHtml(item?.url || '')}" muted playsinline preload="metadata"></video>`
      : `<div class="chat-file-thumb-fallback">📎</div>`;
  const meta = [chatAttachmentKindLabel(item), formatFileSize(item?.size)].filter(Boolean).join(' · ');
  return `
    <div class="chat-file-card${isImage || isVideo ? ' has-preview' : ''}">
      <div class="chat-file-thumb">${media}</div>
      <div class="chat-file-card-body">
        <div class="chat-file-card-name" title="${escapeHtml(item?.name || 'file')}">${escapeHtml(trimText(item?.name || 'file', 34))}</div>
        <div class="chat-file-card-meta">${escapeHtml(meta)}</div>
      </div>
      <button type="button" class="chat-file-pill-remove chat-file-card-remove" data-action="remove-chat-file" data-index="${index}" aria-label="Удалить файл">×</button>
    </div>
  `;
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
  return true;
}

function ensureChatModeCompatibility(showToast = false) {
  const allowedModes = new Set(['chat', 'prompt_builder']);
  const requestedMode = String(state.chat.mode || '').trim();
  if (!allowedModes.has(requestedMode)) {
    state.chat.mode = 'chat';
    if (showToast) toast('info', 'Режим изменён', 'Выбран обычный режим Chat.');
  }
  if (state.chat.mode === 'prompt_builder' && state.chat.model !== 'gpt-5.4') {
    state.chat.model = 'gpt-5.4';
    if (showToast) toast('info', 'Модель изменена', 'Для Prompt Builder автоматически включён GPT 5.4.');
  }
}

function currentMeta() {
  switch (state.studio) {
    case 'chat':
      return { studio: 'AI Chat', provider: state.chat.model === 'claude-sonnet-4-6' ? 'KIE Claude' : 'Chat GPT', model: state.chat.model, mode: state.chat.mode === 'prompt_builder' ? 'Prompt Builder' : 'Chat' };
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
    case 'music': return { studio: 'Music', provider: state.music.ai === 'udio' ? 'Udio' : 'Suno', model: state.music.ai === 'udio' ? 'PiAPI' : 'SunoAPI', mode: state.music.mode === 'lyrics' ? 'Текст' : 'Идея' };
    case 'library': return { studio: 'Library', provider: 'Prompt Library', model: state.prompts.selectedCategory || 'categories', mode: state.prompts.selectedGroupId || 'browse' };
    case 'workspace': return { studio: 'Workspace', provider: 'Project Board', model: 'Internal', mode: 'Planning' };
    case 'history': {
      const project = siteBuilderSelectedProject();
      return { studio: 'Site Creator', provider: 'Website Builder', model: project?.title || 'Landing', mode: project ? `v${Number(project.current_version || 0)}` : 'Draft' };
    }
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
  const legacyProvider = String(state.image.provider || '').trim();
  if (legacyProvider === 'two_images') {
    state.image.provider = 'seedream';
    state.image.model = 'seedream-45';
    state.image.mode = 'i2i';
  } else if (legacyProvider === 'text_to_image') {
    state.image.provider = 'seedream';
    state.image.model = 'seedream-45';
    state.image.mode = 't2i';
  } else if (legacyProvider === 'posters') {
    state.image.provider = 'gpt_image_2';
    state.image.model = 'gpt-image-2';
    state.image.mode = ['poster', 'photo_edit', 'image_to_image'].includes(String(state.image.mode || '').trim()) ? 'image_to_image' : 'text_to_image';
  }

  const fallbackProvider = IMAGE_REGISTRY.nano_banana_pro ? 'nano_banana_pro' : Object.keys(IMAGE_REGISTRY)[0];
  const provider = IMAGE_REGISTRY[state.image.provider] ? state.image.provider : fallbackProvider;
  state.image.provider = provider;
  const providerConfig = IMAGE_REGISTRY[provider];
  const modelIds = Object.keys(providerConfig.models || {});
  if (!modelIds.includes(state.image.model)) state.image.model = modelIds[0];
  const modelConfig = providerConfig.models[state.image.model];
  const modeIds = Object.keys(modelConfig.modes || {});
  if (!modeIds.includes(state.image.mode)) state.image.mode = modeIds[0];

  if (state.image.provider === 'nano_banana_2' || state.image.provider === 'nano_banana_pro_new') {
    if (!['2K', '4K'].includes(String(state.image.resolution || '2K'))) state.image.resolution = '2K';
    const allowedAspect = state.image.mode === 'image_to_image'
      ? ['match_input_image', '16:9', '9:16', '1:1', '4:5']
      : ['16:9', '9:16', '1:1', '4:5'];
    if (!allowedAspect.includes(String(state.image.aspectRatio || ''))) {
      state.image.aspectRatio = state.image.mode === 'image_to_image' ? 'match_input_image' : '16:9';
    }
  }

  if (state.image.provider === 'gpt_image_2') {
    const allowedAspect = state.image.mode === 'image_to_image'
      ? ['match_input_image', '16:9', '9:16', '1:1', '4:5']
      : ['16:9', '9:16', '1:1', '4:5'];
    const defaultAspect = state.image.mode === 'image_to_image' ? 'match_input_image' : '1:1';
    if (!allowedAspect.includes(String(state.image.aspectRatio || ''))) {
      state.image.aspectRatio = defaultAspect;
    }
  }

  if (state.image.provider === 'seedream') {
    if (!['2K', '4K'].includes(String(state.image.resolution || '2K'))) state.image.resolution = '2K';
    const allowedAspect = state.image.mode === 'single' || state.image.mode === 'i2i'
      ? ['match_input_image', '16:9', '9:16', '1:1', '4:5']
      : ['16:9', '9:16', '1:1', '4:5'];
    const defaultAspect = state.image.mode === 'single' || state.image.mode === 'i2i' ? 'match_input_image' : '9:16';
    if (!allowedAspect.includes(String(state.image.aspectRatio || ''))) {
      state.image.aspectRatio = defaultAspect;
    }
  }

  if (state.image.provider === 'midjourney') {
    if (!['16:9', '9:16', '1:1', '4:5'].includes(String(state.image.aspectRatio || ''))) state.image.aspectRatio = '1:1';
    if (!['fast', 'turbo'].includes(String(state.image.mjSpeedMode || ''))) state.image.mjSpeedMode = 'fast';
  }

  if (['text_to_image', 't2i'].includes(state.image.mode) && state.image.aspectRatio === 'match_input_image') {
    state.image.aspectRatio = state.image.provider === 'seedream' ? '9:16' : '16:9';
  }
}

function imageNeedsSourceImage() {
  syncImageSelection();
  if (state.image.provider === 'nano_banana') return true;
  if (state.image.provider === 'nano_banana_2' && state.image.mode === 'image_to_image') return true;
  if (state.image.provider === 'nano_banana_pro' && state.image.mode === 'image_to_image') return true;
  if (state.image.provider === 'nano_banana_pro_new' && state.image.mode === 'image_to_image') return true;
  if (state.image.provider === 'photosession') return true;
  if (state.image.provider === 'gpt_image_2' && state.image.mode === 'image_to_image') return true;
  if (state.image.provider === 'posters' && state.image.mode === 'photo_edit') return true;
  if (state.image.provider === 'seedream' && ['single', 'i2i'].includes(state.image.mode)) return true;
  if (state.image.provider === 'topaz_photo') return true;
  if (state.image.provider === 'midjourney') return false;
  return false;
}

function imageNeedsBaseImage() {
  syncImageSelection();
  return state.image.provider === 'seedream' && state.image.mode === 'i2i';
}

function imageRunCost() {
  syncImageSelection();
  switch (state.image.provider) {
    case 'nano_banana':
      return 1;
    case 'nano_banana_2':
      return String(state.image.resolution || '2K') === '4K' ? 2 : 1;
    case 'nano_banana_pro':
      return 2;
    case 'nano_banana_pro_new':
      return String(state.image.resolution || '2K') === '4K' ? 2 : 1;
    case 'seedream':
      return state.image.mode === 't2i' ? 0 : 1;
    case 'photosession':
      return 1;
    case 'gpt_image_2':
      return 0;
    case 'midjourney':
      return String(state.image.mjSpeedMode || 'fast') === 'turbo' ? 2 : 1;
    case 'topaz_photo': {
      const preset = String(state.image.upscalePreset || 'standard');
      if (preset === 'detail') return 3;
      if (preset === 'max') return 4;
      return 2;
    }
    case 'posters':
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
  if (provider === 'grok') {
    if (!['fun', 'normal', 'spicy'].includes(String(state.video.providerMode || '').trim())) state.video.providerMode = 'normal';
    if (!['480p', '720p'].includes(String(state.video.resolution || '').trim())) state.video.resolution = '480p';
    if (!['2:3', '3:2', '1:1', '16:9', '9:16'].includes(String(state.video.aspectRatio || '').trim())) state.video.aspectRatio = '16:9';
    state.video.duration = normalizeGrokDurationValue(state.video.duration || '6');
  }
  if (provider === 'seedance' || provider === 'seedance_kie') {
    state.video.duration = normalizeSeedanceDurationValue(state.video.duration || '5');
    const allowedAspectRatios = ['16:9', '9:16', '1:1'];
    if (!allowedAspectRatios.includes(String(state.video.aspectRatio || ''))) {
      state.video.aspectRatio = '16:9';
    }
    if (provider === 'seedance_kie') {
      state.video.enableAudio = true;
      state.video.resolution = state.video.model === 'seedance-kie-fast' ? '480p' : '720p';
    }
  }
  if (provider === 'kling' && state.video.model === 'kling-3.0-new') {
    state.video.resolution = normalizeKling3NewModeValue(state.video.resolution || 'std');
    const allowedAspectRatios = ['16:9', '9:16', '1:1'];
    if (!allowedAspectRatios.includes(String(state.video.aspectRatio || ''))) state.video.aspectRatio = '16:9';
    const allowedDurations = ['3', '5', '10', '15'];
    if (!allowedDurations.includes(String(state.video.duration || ''))) state.video.duration = '5';
    if (state.video.mode === 'multi_shot') {
      state.video.kling3NewShots = getKling3NewShots();
    }
  }
  if (provider === 'pixverse_c1') {
    state.video.duration = normalizePixVerseDurationValue(state.video.duration || '5');
    state.video.resolution = normalizePixVerseResolutionValue(state.video.resolution || '720p');
    state.video.aspectRatio = normalizePixVerseAspectRatioValue(state.video.aspectRatio || '16:9');
    state.video.enableAudio = true;
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

function normalizeGrokDurationValue(value) {
  const allowed = [6, 12, 18, 24, 30];
  const raw = Number(value || 0);
  if (!Number.isFinite(raw)) return '6';
  let best = allowed[0];
  let bestDistance = Math.abs(raw - best);
  for (const item of allowed) {
    const distance = Math.abs(raw - item);
    if (distance < bestDistance || (distance === bestDistance && item < best)) {
      best = item;
      bestDistance = distance;
    }
  }
  return String(best);
}

function grokDurationOptions() {
  return [['6', '6 sec'], ['12', '12 sec'], ['18', '18 sec'], ['24', '24 sec'], ['30', '30 sec']];
}

function normalizeSeedanceDurationValue(value) {
  const allowed = [5, 10, 15];
  const raw = Number(value || 0);
  if (!Number.isFinite(raw)) return '5';
  let best = allowed[0];
  let bestDistance = Math.abs(raw - best);
  for (const item of allowed) {
    const distance = Math.abs(raw - item);
    if (distance < bestDistance || (distance === bestDistance && item < best)) {
      best = item;
      bestDistance = distance;
    }
  }
  return String(best);
}

function normalizePixVerseDurationValue(value) {
  const allowed = [5, 10, 15];
  const raw = Number(value || 0);
  if (!Number.isFinite(raw)) return '5';
  let best = allowed[0];
  let bestDistance = Math.abs(raw - best);
  for (const item of allowed) {
    const distance = Math.abs(raw - item);
    if (distance < bestDistance || (distance === bestDistance && item < best)) {
      best = item;
      bestDistance = distance;
    }
  }
  return String(best);
}

function normalizePixVerseResolutionValue(value) {
  const normalized = String(value || '720p').trim().toLowerCase();
  if (normalized === '360p' || normalized === '360') return '360p';
  if (normalized === '540p' || normalized === '540') return '540p';
  if (normalized === '1080p' || normalized === '1080') return '1080p';
  return normalized === '720p' || normalized === '720' ? '720p' : '720p';
}

function normalizePixVerseAspectRatioValue(value) {
  const allowed = ['16:9', '4:3', '1:1', '3:4', '9:16', '2:3', '3:2', '21:9'];
  const normalized = String(value || '16:9').trim();
  return allowed.includes(normalized) ? normalized : '16:9';
}

function pixVerseDurationOptions() {
  return [['5', '5 sec'], ['10', '10 sec'], ['15', '15 sec']];
}

function getPixVerseRunCost(duration, resolution) {
  const seconds = Number(normalizePixVerseDurationValue(duration));
  const normalizedResolution = normalizePixVerseResolutionValue(resolution);
  const priceMap = {
    '360p': { 5: 2, 10: 4, 15: 6 },
    '540p': { 5: 2, 10: 5, 15: 7 },
    '720p': { 5: 3, 10: 6, 15: 9 },
    '1080p': { 5: 5, 10: 11, 15: 16 },
  };
  const tokens = priceMap[normalizedResolution]?.[seconds] || 3;
  return { tokens, normalizedResolution };
}


function normalizeSwitchxSourceDurationSec(value) {
  const raw = Number(value || 0);
  if (!Number.isFinite(raw) || raw <= 0) return null;
  return Math.max(1, Math.floor(raw + 0.5));
}

function getGrokRunCost(duration, resolution) {
  const seconds = Number(normalizeGrokDurationValue(duration));
  const normalizedResolution = String(resolution || '480p').trim().toLowerCase() === '720p' ? '720p' : '480p';
  const secondsPerToken = normalizedResolution === '720p' ? 6 : 12;
  const tokens = Math.max(1, Math.ceil(seconds / secondsPerToken));
  return { tokens, normalizedResolution, secondsPerToken };
}

function normalizeKling3NewModeValue(value) {
  const raw = String(value || 'std').trim();
  const lower = raw.toLowerCase();
  if (['standard', 'std', '720', '720p'].includes(lower)) return 'std';
  if (['pro', '1080', '1080p'].includes(lower)) return 'pro';
  if (['4k', '4K'].includes(raw) || lower === '4k') return '4K';
  return 'std';
}

function getKling3NewShots() {
  const source = Array.isArray(state.video.kling3NewShots) ? state.video.kling3NewShots : [];
  const normalized = source.map((shot) => ({
    prompt: String(shot?.prompt || '').slice(0, 500),
    duration: String(Math.max(1, Math.min(12, Number(shot?.duration || 3) || 3))),
    elements: Array.isArray(shot?.elements)
      ? shot.elements.map((el) => ({
          name: String(el?.name || '').trim(),
          kind: String(el?.kind || 'image').trim() || 'image',
          files_count: Number(el?.files_count || 0) || 0,
        })).filter((el) => el.name)
      : [],
  }));
  while (normalized.length < 2) normalized.push({ prompt: '', duration: '3', elements: [] });
  return normalized.slice(0, 5);
}

function getKling3NewShotElements(index) {
  const shots = getKling3NewShots();
  return Array.isArray(shots[index]?.elements) ? shots[index].elements : [];
}

function appendTokenToTextareaValue(currentValue, token) {
  const source = String(currentValue || '').trim();
  if (!source) return token;
  if (source.includes(token)) return source;
  return `${source} ${token}`.trim();
}


function escapeRegExp(value) {
  return String(value || '').replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function removeTokenFromTextareaValue(currentValue, token) {
  const source = String(currentValue || '');
  const safeToken = escapeRegExp(String(token || '').trim());
  if (!safeToken) return source.trim();
  return source
    .replace(new RegExp(`(^|\\s)${safeToken}(?=\\s|$)`, 'g'), ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function kling3NewElementImageUrls(element) {
  return String(element?.image_urls_text || '').split(/\r?\n/).map((x) => x.trim()).filter(Boolean);
}

function kling3NewElementImageMetas(element) {
  const raw = String(element?.image_meta_json || '').trim();
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.map((item) => ({
      name: String(item?.name || '').trim(),
      width: Number(item?.width || 0) || 0,
      height: Number(item?.height || 0) || 0,
      size: Number(item?.size || 0) || 0,
      url: String(item?.url || '').trim(),
    }));
  } catch (_) {
    return [];
  }
}

function kling3NewImageFileLabel(url) {
  try {
    const clean = String(url || '').split('?')[0].split('#')[0];
    const raw = clean.split('/').filter(Boolean).pop() || '';
    const decoded = decodeURIComponent(raw);
    return decoded.length > 34 ? `${decoded.slice(0, 16)}…${decoded.slice(-14)}` : decoded;
  } catch (_) {
    const raw = String(url || '').split('?')[0].split('/').pop() || '';
    return raw.length > 34 ? `${raw.slice(0, 16)}…${raw.slice(-14)}` : raw;
  }
}

function kling3NewFormatFileSize(bytes) {
  const value = Number(bytes || 0) || 0;
  if (!value) return '';
  if (value >= 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`;
  if (value >= 1024) return `${Math.round(value / 1024)} KB`;
  return `${value} B`;
}

function getLocalImageDimensions(file) {
  return new Promise((resolve, reject) => {
    if (!file) {
      resolve({ width: 0, height: 0 });
      return;
    }
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      const width = Number(img.naturalWidth || img.width || 0) || 0;
      const height = Number(img.naturalHeight || img.height || 0) || 0;
      URL.revokeObjectURL(url);
      resolve({ width, height });
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error(`Не удалось прочитать размер изображения: ${file.name || 'image'}`));
    };
    img.src = url;
  });
}

async function getKling3NewImageFileInfos(files) {
  const list = Array.from(files || []);
  return Promise.all(list.map(async (file) => {
    const dims = await getLocalImageDimensions(file);
    return {
      name: String(file?.name || 'image').trim(),
      width: Number(dims.width || 0) || 0,
      height: Number(dims.height || 0) || 0,
      size: Number(file?.size || 0) || 0,
    };
  }));
}

function syncKling3NewShotElementCounts() {
  state.video.kling3NewShots = getKling3NewShots();
  state.video.kling3NewElements = getKling3NewElements();
  const byName = new Map(state.video.kling3NewElements.map((el) => [el.name, el]));
  state.video.kling3NewShots.forEach((shot) => {
    if (!Array.isArray(shot.elements)) shot.elements = [];
    shot.elements = shot.elements.map((linked) => {
      const element = byName.get(linked.name);
      if (!element) return linked;
      const imageCount = kling3NewElementImageUrls(element).length;
      return {
        ...linked,
        kind: element.video_url ? 'video' : 'image',
        files_count: element.video_url ? 1 : imageCount,
      };
    }).filter((linked) => byName.has(linked.name));
  });
}

function removeKling3NewShotElement(index, elementName) {
  const name = String(elementName || '').trim();
  if (!name) return;
  state.video.kling3NewShots = getKling3NewShots();
  state.video.kling3NewElements = getKling3NewElements().filter((el) => el.name !== name);
  const shot = state.video.kling3NewShots[index];
  if (shot) {
    shot.elements = Array.isArray(shot.elements) ? shot.elements.filter((el) => el.name !== name) : [];
    shot.prompt = removeTokenFromTextareaValue(shot.prompt || '', `@${name}`);
  }
  saveState();
  render();
  toast('success', 'Element удалён', `@${name} удалён из shot и prompt.`);
}

function removeKling3NewShotElementImage(index, elementName, imageIndex) {
  const name = String(elementName || '').trim();
  const targetIndex = Number(imageIndex || 0);
  if (!name || targetIndex < 0) return;
  state.video.kling3NewShots = getKling3NewShots();
  state.video.kling3NewElements = getKling3NewElements();
  const element = state.video.kling3NewElements.find((el) => el.name === name);
  if (!element) return;
  const urls = kling3NewElementImageUrls(element);
  if (!urls.length || targetIndex >= urls.length) return;
  urls.splice(targetIndex, 1);
  const metas = kling3NewElementImageMetas(element);
  if (metas.length) {
    metas.splice(targetIndex, 1);
    element.image_meta_json = JSON.stringify(metas);
  }
  if (!urls.length) {
    removeKling3NewShotElement(index, name);
    return;
  }
  element.image_urls_text = urls.join('\n');
  const shot = state.video.kling3NewShots[index];
  if (shot && Array.isArray(shot.elements)) {
    const linked = shot.elements.find((el) => el.name === name);
    if (linked) linked.files_count = urls.length;
  }
  syncKling3NewShotElementCounts();
  saveState();
  render();
  toast('success', 'Фото удалено', `В @${name} осталось ${urls.length}/4 фото.`);
}

async function uploadKling3NewWorkspaceFiles(files, slot = 'element') {
  const urls = [];
  for (const file of Array.from(files || [])) {
    const fd = new FormData();
    fd.append('file', file);
    fd.append('telegram_user_id', String((state.user && (state.user.telegram_id || state.user.id)) || 0));
    fd.append('slot', slot);
    const res = await fetch('/api/kling3-kie/upload-reference', { method: 'POST', body: fd, credentials: 'include' });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) throw new Error(data.detail || data.message || `Upload failed: ${res.status}`);
    urls.push(data.public_url || data.url);
  }
  return urls;
}

async function handleKling3NewShotElementUpload(index, fileList, kind = 'image') {
  const files = Array.from(fileList || []);
  if (!files.length) return;
  if (kind === 'image') {
    const bad = files.filter((file) => {
      const type = String(file?.type || '').toLowerCase();
      const name = String(file?.name || '').toLowerCase();
      return !(['image/jpeg', 'image/jpg', 'image/png'].includes(type) || /\.(jpe?g|png)$/.test(name));
    });
    if (bad.length) throw new Error('Для image element доступны только JPG/PNG. WEBP не отправляем в KIE.');
    if (files.length > 4) {
      throw new Error('За один раз можно выбрать максимум 4 фото. Для запуска у image element должно быть 2–4 фото.');
    }
  }

  const imageFileInfos = kind === 'image' ? await getKling3NewImageFileInfos(files) : [];
  if (kind === 'image') {
    const tooSmall = imageFileInfos.find((info) => info.width < 300 || info.height < 300);
    if (tooSmall) {
      throw new Error(`Фото "${tooSmall.name}" слишком маленькое: ${tooSmall.width}×${tooSmall.height}px. Для Kling 3.0 - New нужно минимум 300×300px.`);
    }
  }

  state.video.kling3NewShots = getKling3NewShots();
  state.video.kling3NewElements = getKling3NewElements();
  const shot = state.video.kling3NewShots[index];
  if (!shot) return;
  if (!Array.isArray(shot.elements)) shot.elements = [];

  let targetElement = null;
  let targetShotElement = null;

  if (kind === 'image') {
    for (let i = shot.elements.length - 1; i >= 0; i -= 1) {
      const linked = shot.elements[i];
      if (!linked || linked.kind === 'video') continue;
      const existing = state.video.kling3NewElements.find((el) => el.name === linked.name);
      if (!existing) continue;
      const currentUrls = kling3NewElementImageUrls(existing);
      if (currentUrls.length < 4) {
        targetElement = existing;
        targetShotElement = linked;
        break;
      }
    }
  }

  const willCreateNew = !targetElement;
  if (willCreateNew && state.video.kling3NewElements.length >= 3) {
    toast('info', 'Лимит элементов', 'Kling 3.0 - New поддерживает до 3 elements на один multi-shot ролик.');
    return;
  }

  if (targetElement && kind === 'image') {
    const currentUrls = kling3NewElementImageUrls(targetElement);
    if (currentUrls.length + files.length > 4) {
      throw new Error(`В @${targetElement.name} уже ${currentUrls.length} фото. Максимум для одного image element — 4 фото.`);
    }
  }

  const nextIndex = shot.elements.length + 1;
  const elementName = targetElement?.name || `shot${index + 1}_el${nextIndex}`;
  const slot = `shot_${index + 1}_${kind}`;
  toast('info', 'Загрузка элемента', `Загружаю ${kind === 'video' ? 'видео' : 'изображения'} для Shot ${index + 1}...`);
  const urls = await uploadKling3NewWorkspaceFiles(files, slot);

  if (targetElement && kind === 'image') {
    const currentUrls = kling3NewElementImageUrls(targetElement);
    const currentMetas = kling3NewElementImageMetas(targetElement);
    const paddedCurrentMetas = currentUrls.map((url, metaIndex) => ({ ...(currentMetas[metaIndex] || {}), url }));
    const newMetas = urls.map((url, metaIndex) => ({ ...(imageFileInfos[metaIndex] || {}), url }));
    const nextUrls = currentUrls.concat(urls).slice(0, 4);
    targetElement.image_urls_text = nextUrls.join('\n');
    targetElement.image_meta_json = JSON.stringify(paddedCurrentMetas.concat(newMetas).slice(0, 4));
    if (targetShotElement) targetShotElement.files_count = nextUrls.length;
    saveState();
    render();
    toast('success', 'Фото добавлено', `В @${elementName} теперь ${nextUrls.length}/4 фото. Для запуска нужно минимум 2.`);
    return;
  }

  const element = {
    name: elementName,
    description: `Shot ${index + 1} element ${nextIndex}`,
    image_urls_text: kind === 'image' ? urls.join('\n') : '',
    image_meta_json: kind === 'image' ? JSON.stringify(urls.map((url, metaIndex) => ({ ...(imageFileInfos[metaIndex] || {}), url }))) : '',
    video_url: kind === 'video' ? (urls[0] || '') : '',
  };
  state.video.kling3NewElements.push(element);
  shot.elements.push({ name: elementName, kind, files_count: urls.length });
  shot.prompt = appendTokenToTextareaValue(shot.prompt || '', `@${elementName}`);
  saveState();
  render();
  const needMore = kind === 'image' && urls.length < 2 ? ' Добавь ещё 1 фото в этот же element перед запуском.' : '';
  toast('success', 'Элемент добавлен', `@${elementName} автоматически вставлен в Shot ${index + 1}.${needMore}`);
}

function getKling3NewShotDuration() {
  const shots = getKling3NewShots().filter((shot) => String(shot.prompt || '').trim());
  const total = shots.reduce((sum, shot) => sum + Math.max(1, Math.min(12, Number(shot.duration || 3) || 3)), 0);
  return Math.max(3, Math.min(15, total || Number(state.video.duration || 5) || 5));
}

function getKling3NewElements() {
  const source = Array.isArray(state.video.kling3NewElements) ? state.video.kling3NewElements : [];
  return source.map((el) => ({
    name: String(el?.name || '').trim().replace(/^@+/, '').replace(/[^a-zA-Z0-9_]/g, '_').slice(0, 48),
    description: String(el?.description || '').trim().slice(0, 240),
    image_urls_text: String(el?.image_urls_text || '').trim(),
    image_meta_json: String(el?.image_meta_json || '').trim(),
    video_url: String(el?.video_url || '').trim(),
  })).slice(0, 3);
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
  if (model === 'kling-3.0-new') {
    const mode = normalizeKling3NewModeValue(state.video.resolution || 'std');
    const seconds = state.video.mode === 'multi_shot' ? getKling3NewShotDuration() : Math.max(3, Math.min(15, duration || 5));
    const table = {
      std: { off: 1, on: 1.5 },
      pro: { off: 1.5, on: 2 },
      '4K': { off: 5, on: 6 },
    };
    const rate = table[mode]?.[state.video.enableAudio ? 'on' : 'off'] || 1;
    const tokens = Math.max(1, Math.ceil(seconds * rate));
    return { known: true, tokens, label: `▶ Запуск • ${tokens} ${pluralizeTokens(tokens)}`, helper: `Kling 3.0 - New: ${seconds} сек, ${mode}, ${state.video.enableAudio ? 'audio' : 'no audio'}.` };
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
  if (model === 'grok-imagine-video') {
    const grokCost = getGrokRunCost(duration, state.video.resolution || '480p');
    const tokens = grokCost.tokens;
    return { known: true, tokens, label: `▶ Запуск • ${tokens} ${pluralizeTokens(tokens)}`, helper: '' };
  }
  if (model === 'seedance-preview') {
    const rate = 2;
    const tokens = Math.max(1, duration) * rate;
    return { known: true, tokens, label: `▶ Запуск • ${tokens} ${pluralizeTokens(tokens)}`, helper: 'В режиме Preview с референсом формат может быть взят из изображения.' };
  }
  if (model === 'seedance-fast') {
    const rate = 1;
    const tokens = Math.max(1, duration) * rate;
    return { known: true, tokens, label: `▶ Запуск • ${tokens} ${pluralizeTokens(tokens)}`, helper: 'Seedance 2.0 Preview Fast.' };
  }
  if (model === 'seedance-kie') {
    const priceMap = { 5: 10, 10: 20, 15: 30 };
    const omniVideoRefs = getFile('video.referenceVideos');
    const hasOmniVideoRef = state.video.provider === 'seedance_kie' && state.video.mode === 'omni_reference' && Array.isArray(omniVideoRefs) && omniVideoRefs.length > 0;
    const surcharge = hasOmniVideoRef ? 20 : 0;
    const tokens = (priceMap[duration] || 10) + surcharge;
    return { known: true, tokens, label: `▶ Запуск • ${tokens} ${pluralizeTokens(tokens)}`, helper: hasOmniVideoRef ? '720p · audio on · video ref +20 ток.' : '720p · аудио включено.' };
  }
  if (model === 'seedance-kie-fast') {
    const priceMap = { 5: 5, 10: 10, 15: 15 };
    const omniVideoRefs = getFile('video.referenceVideos');
    const hasOmniVideoRef = state.video.provider === 'seedance_kie' && state.video.mode === 'omni_reference' && Array.isArray(omniVideoRefs) && omniVideoRefs.length > 0;
    const surcharge = hasOmniVideoRef ? 13 : 0;
    const tokens = (priceMap[duration] || 5) + surcharge;
    return { known: true, tokens, label: `▶ Запуск • ${tokens} ${pluralizeTokens(tokens)}`, helper: hasOmniVideoRef ? '480p Fast · audio on · video ref +13 ток.' : '480p Fast · аудио включено.' };
  }
  if (model === 'c1') {
    const pixVerseCost = getPixVerseRunCost(duration, state.video.resolution || '720p');
    const tokens = pixVerseCost.tokens;
    return { known: true, tokens, label: `▶ Запуск • ${tokens} ${pluralizeTokens(tokens)}`, helper: `${pixVerseCost.normalizedResolution} · звук включён.` };
  }
  if (model === 'sora-2') {
    const costMap = { 4: 5, 8: 10, 12: 15 };
    const tokens = costMap[duration] || 5;
    return { known: true, tokens, label: `▶ Запуск • ${tokens} ${pluralizeTokens(tokens)}` };
  }
  if (model === 'switchx') {
    const seconds = normalizeSwitchxSourceDurationSec(state.video.sourceVideoDurationSec);
    if (!seconds) return { known: false, label: '▶ Запуск', helper: 'Стоимость зависит от длины исходного видео.' };
    const rate = String(state.video.resolution || '1080') === '720' ? 1 : 2;
    const tokens = Math.max(1, seconds) * rate;
    return { known: true, tokens, label: `▶ Запуск • ${tokens} ${pluralizeTokens(tokens)}`, helper: `Исходник: ${seconds} сек · ${rate} ток/сек.` };
  }
  return { known: false, label: '▶ Запуск' };
}

function isVideoRunLocked() {
  const status = String(state.video.lastStatus || '').toLowerCase();
  return !!state.video.isGenerating || (!!state.video.generationId && !state.video.outputUrl && !isVideoTaskFinished(status));
}

function videoRunButtonLabel() {
  if (isVideoRunLocked()) return '⏳ Генерация...';
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

function imageHistoryUrls(item) {
  if (!item) return [];
  if (Array.isArray(item.image_urls) && item.image_urls.length) return item.image_urls.filter(Boolean);
  const single = imageHistoryUrl(item);
  return single ? [single] : [];
}

function imageHistoryAvailableActions(item) {
  if (!item || !item.available_actions || typeof item.available_actions !== 'object') return {};
  return item.available_actions;
}

function imageActiveUrls(item = null) {
  const source = item || (state.image.panel === 'library' ? imageHistorySelectedItem() : null);
  if (source && Array.isArray(source.image_urls) && source.image_urls.length) return source.image_urls.filter(Boolean);
  if (Array.isArray(state.image.imageUrls) && state.image.imageUrls.length) return state.image.imageUrls.filter(Boolean);
  const single = source ? imageHistoryUrl(source) : (state.image.downloadUrl || state.image.outputUrl || state.image.afterImageUrl || '');
  return single ? [single] : [];
}

function imageStageProvider(item = null) {
  const source = item || (state.image.panel === 'library' ? imageHistorySelectedItem() : null);
  return String(source?.provider || state.image.provider || '').trim().toLowerCase();
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


async function probeSourceVideoDuration(fileObj) {
  if (!fileObj?.url) {
    state.video.sourceVideoDurationSec = null;
    saveState();
    render();
    return;
  }
  const video = document.createElement('video');
  video.preload = 'metadata';
  video.src = fileObj.url;
  video.onloadedmetadata = () => {
    const duration = Number(video.duration || 0);
    state.video.sourceVideoDurationSec = normalizeSwitchxSourceDurationSec(duration);
    saveState();
    render();
  };
  video.onerror = () => {
    state.video.sourceVideoDurationSec = null;
    saveState();
    render();
  };
}


function isSwitchxSelectModeActive() {
  return false;
}

function switchxMaskEditorSourceSignature(fileObj) {
  if (!fileObj?.file && !fileObj?.url) return '';
  return [String(fileObj.name || fileObj.file?.name || 'source'), Number(fileObj.size || fileObj.file?.size || 0), Number(fileObj.lastModified || fileObj.file?.lastModified || 0)].join('::');
}

function resetSwitchxMaskEditor({ clearMaskFile = true } = {}) {
  const prev = runtime.switchxMaskEditor || {};
  runtime.switchxMaskEditor = {
    sourceSignature: '',
    frameDataUrl: '',
    frameWidth: 0,
    frameHeight: 0,
    maskDataUrl: '',
    brushSize: Number.isFinite(Number(prev.brushSize)) ? Number(prev.brushSize) : 28,
    tool: prev.tool === 'eraser' ? 'eraser' : 'brush',
    loading: false,
    ready: false,
    errorText: '',
  };
  if (clearMaskFile) {
    revokeRuntimeFileValue(runtime.files['video.switchxSelectMask']);
    delete runtime.files['video.switchxSelectMask'];
    const input = document.getElementById('video_switchxSelectMask');
    if (input) input.value = '';
  }
}

function switchxMaskEditorHasPaint(maskCanvas) {
  if (!maskCanvas) return false;
  const ctx = maskCanvas.getContext('2d');
  if (!ctx) return false;
  const { width, height } = maskCanvas;
  if (!width || !height) return false;
  const { data } = ctx.getImageData(0, 0, width, height);
  for (let i = 3; i < data.length; i += 4) {
    if (data[i] > 0) return true;
  }
  return false;
}

function switchxMaskEditorSetGeneratedFile(file) {
  if (!file) {
    revokeRuntimeFileValue(runtime.files['video.switchxSelectMask']);
    delete runtime.files['video.switchxSelectMask'];
    const input = document.getElementById('video_switchxSelectMask');
    if (input) input.value = '';
    return;
  }
  revokeRuntimeFileValue(runtime.files['video.switchxSelectMask']);
  runtime.files['video.switchxSelectMask'] = makeRuntimeFileEntry(file);
  const input = document.getElementById('video_switchxSelectMask');
  if (input) input.value = '';
}

function canvasToFile(canvas, filename = 'switchx_select_mask.png', type = 'image/png') {
  return new Promise((resolve, reject) => {
    if (!canvas) {
      reject(new Error('Canvas not ready'));
      return;
    }
    canvas.toBlob((blob) => {
      if (!blob) {
        reject(new Error('Не удалось сохранить маску в PNG'));
        return;
      }
      resolve(new File([blob], filename, { type }));
    }, type);
  });
}

function getSwitchxMaskPixelValue(r, g, b, a) {
  const rgbLuminance = Math.max(Number(r || 0), Number(g || 0), Number(b || 0));
  const alpha = Math.max(0, Math.min(255, Number(a || 0)));
  const scaled = alpha >= 255 ? rgbLuminance : Math.round((rgbLuminance / 255) * alpha);
  if (scaled <= 6) return 0;
  return Math.max(0, Math.min(255, scaled));
}

async function drawSwitchxMaskSourceToCanvas(source, width, height) {
  const image = typeof source === 'string' ? await loadImageElement(source) : source;
  const canvas = document.createElement('canvas');
  canvas.width = Math.max(1, Number(width || image?.naturalWidth || image?.videoWidth || image?.width || 1));
  canvas.height = Math.max(1, Number(height || image?.naturalHeight || image?.videoHeight || image?.height || 1));
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(image, 0, 0, canvas.width, canvas.height);
  return canvas;
}

async function switchxMaskSourceToGrayscaleFile(source, width, height, filename = 'switchx_select_mask.png') {
  const canvas = await drawSwitchxMaskSourceToCanvas(source, width, height);
  const ctx = canvas.getContext('2d');
  const frame = ctx.getImageData(0, 0, canvas.width, canvas.height);
  const data = frame.data;
  for (let i = 0; i < data.length; i += 4) {
    const value = getSwitchxMaskPixelValue(data[i], data[i + 1], data[i + 2], data[i + 3]);
    data[i] = value;
    data[i + 1] = value;
    data[i + 2] = value;
    data[i + 3] = 255;
  }
  ctx.putImageData(frame, 0, 0);
  return canvasToFile(canvas, filename, 'image/png');
}

async function switchxMaskSourceToTransparentDataUrl(source, width, height) {
  const canvas = await drawSwitchxMaskSourceToCanvas(source, width, height);
  const ctx = canvas.getContext('2d');
  const frame = ctx.getImageData(0, 0, canvas.width, canvas.height);
  const data = frame.data;
  for (let i = 0; i < data.length; i += 4) {
    const value = getSwitchxMaskPixelValue(data[i], data[i + 1], data[i + 2], data[i + 3]);
    data[i] = 255;
    data[i + 1] = 255;
    data[i + 2] = 255;
    data[i + 3] = value;
  }
  ctx.putImageData(frame, 0, 0);
  return canvas.toDataURL('image/png');
}

async function switchxMaskEditorSyncExport(maskCanvas) {
  const editor = runtime.switchxMaskEditor || {};
  if (!maskCanvas || !editor.frameWidth || !editor.frameHeight) {
    switchxMaskEditorSetGeneratedFile(null);
    return false;
  }
  if (!switchxMaskEditorHasPaint(maskCanvas)) {
    editor.maskDataUrl = '';
    switchxMaskEditorSetGeneratedFile(null);
    return false;
  }
  editor.maskDataUrl = await switchxMaskSourceToTransparentDataUrl(maskCanvas, editor.frameWidth, editor.frameHeight);
  const file = await switchxMaskSourceToGrayscaleFile(maskCanvas, editor.frameWidth, editor.frameHeight, 'switchx_select_mask.png');
  switchxMaskEditorSetGeneratedFile(file);
  return true;
}

function loadImageElement(url) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error('Не удалось загрузить изображение'));
    image.src = url;
  });
}

async function normalizeSwitchxMaskToTransparent(url, width, height) {
  return switchxMaskSourceToTransparentDataUrl(url, width, height);
}

function extractFirstFrameFromVideo(url) {
  return new Promise((resolve, reject) => {
    const video = document.createElement('video');
    let finished = false;
    const cleanup = () => {
      video.onloadeddata = null;
      video.onseeked = null;
      video.onerror = null;
    };
    const fail = (error) => {
      if (finished) return;
      finished = true;
      cleanup();
      reject(error instanceof Error ? error : new Error(String(error || 'Не удалось извлечь 1-й кадр')));
    };
    const capture = () => {
      if (finished) return;
      try {
        const width = Number(video.videoWidth || 0);
        const height = Number(video.videoHeight || 0);
        if (!width || !height) {
          fail(new Error('Не определились размеры source video'));
          return;
        }
        const canvas = document.createElement('canvas');
        canvas.width = width;
        canvas.height = height;
        const ctx = canvas.getContext('2d');
        ctx.drawImage(video, 0, 0, width, height);
        finished = true;
        cleanup();
        resolve({ dataUrl: canvas.toDataURL('image/png'), width, height });
      } catch (error) {
        fail(error);
      }
    };
    video.preload = 'auto';
    video.muted = true;
    video.playsInline = true;
    video.onloadeddata = () => capture();
    video.onseeked = () => capture();
    video.onerror = () => fail(new Error('Браузер не смог открыть source video'));
    video.src = url;
    video.load();
  });
}

async function ensureSwitchxMaskEditorFrame() {
  const editor = runtime.switchxMaskEditor || {};
  const sourceVideo = getFile('video.sourceVideo');
  if (!sourceVideo?.url) {
    editor.ready = false;
    editor.loading = false;
    editor.errorText = '';
    return false;
  }
  const signature = switchxMaskEditorSourceSignature(sourceVideo);
  if (editor.frameDataUrl && editor.sourceSignature === signature) {
    editor.ready = true;
    editor.loading = false;
    editor.errorText = '';
    return true;
  }
  editor.loading = true;
  editor.ready = false;
  editor.errorText = '';
  try {
    const frame = await extractFirstFrameFromVideo(sourceVideo.url);
    editor.sourceSignature = signature;
    editor.frameDataUrl = frame.dataUrl;
    editor.frameWidth = frame.width;
    editor.frameHeight = frame.height;
    editor.maskDataUrl = '';
    editor.loading = false;
    editor.ready = true;
    switchxMaskEditorSetGeneratedFile(null);
    return true;
  } catch (error) {
    editor.loading = false;
    editor.ready = false;
    editor.errorText = String(error?.message || error || 'Не удалось подготовить 1-й кадр');
    return false;
  }
}

async function initSwitchxMaskEditor() {
  const root = document.getElementById('switchxMaskEditorCard');
  if (!root || !isSwitchxSelectModeActive()) return;
  const statusEl = document.getElementById('switchxMaskEditorStatus');
  const canvas = document.getElementById('switchxMaskEditorCanvas');
  const brushInput = document.getElementById('switchxMaskBrushSize');
  const sizeValue = document.getElementById('switchxMaskBrushSizeValue');
  const sourceVideo = getFile('video.sourceVideo');
  const editor = runtime.switchxMaskEditor || {};

  if (sizeValue) sizeValue.textContent = `${Number(editor.brushSize || 28)} px`;

  if (!sourceVideo?.url && !editor.frameDataUrl) {
    if (statusEl) {
      statusEl.textContent = state.video.switchxSourceUploadId
        ? 'Для рисования маски заново прикрепи source video в браузере. Upload id уже есть, но без локального файла 1-й кадр не показать.'
        : 'Сначала загрузи source video, потом здесь появится 1-й кадр для рисования.';
    }
    return;
  }

  if ((!editor.frameDataUrl || editor.sourceSignature !== switchxMaskEditorSourceSignature(sourceVideo)) && sourceVideo?.url) {
    if (statusEl) statusEl.textContent = 'Достаю 1-й кадр source video...';
    const ok = await ensureSwitchxMaskEditorFrame();
    if (!ok) {
      if (statusEl) statusEl.textContent = editor.errorText || 'Не удалось подготовить 1-й кадр.';
      return;
    }
  }

  if (!canvas || !editor.frameDataUrl || !editor.frameWidth || !editor.frameHeight) {
    if (statusEl) statusEl.textContent = editor.errorText || 'Рабочая область маски пока недоступна.';
    return;
  }

  const frameImage = await loadImageElement(editor.frameDataUrl);
  const maskCanvas = document.createElement('canvas');
  maskCanvas.width = editor.frameWidth;
  maskCanvas.height = editor.frameHeight;
  const maskCtx = maskCanvas.getContext('2d');
  maskCtx.clearRect(0, 0, maskCanvas.width, maskCanvas.height);

  if (editor.maskDataUrl) {
    try {
      const maskImage = await loadImageElement(editor.maskDataUrl);
      maskCtx.drawImage(maskImage, 0, 0, maskCanvas.width, maskCanvas.height);
    } catch (_e) {}
  } else {
    const existingMask = getFile('video.switchxSelectMask');
    if (existingMask?.url) {
      try {
        editor.maskDataUrl = await normalizeSwitchxMaskToTransparent(existingMask.url, editor.frameWidth, editor.frameHeight);
        const maskImage = await loadImageElement(editor.maskDataUrl);
        maskCtx.drawImage(maskImage, 0, 0, maskCanvas.width, maskCanvas.height);
      } catch (_e) {}
    }
  }

  canvas.width = editor.frameWidth;
  canvas.height = editor.frameHeight;
  canvas.style.width = '100%';
  canvas.style.height = 'auto';
  const ctx = canvas.getContext('2d');

  const redraw = () => {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(frameImage, 0, 0, canvas.width, canvas.height);
    ctx.save();
    ctx.globalAlpha = 0.52;
    ctx.drawImage(maskCanvas, 0, 0, canvas.width, canvas.height);
    ctx.restore();
  };

  const syncStatus = (text) => {
    if (statusEl) statusEl.textContent = text;
  };

  const syncBadge = (hasMask) => {
    const badge = document.getElementById('switchxMaskReadyBadge');
    if (!badge) return;
    badge.textContent = hasMask ? 'Mask ready' : 'Mask draft';
    badge.style.background = hasMask ? 'rgba(64, 196, 144, 0.18)' : 'rgba(255,255,255,0.08)';
  };

  const saveMask = async (message) => {
    const hasMask = await switchxMaskEditorSyncExport(maskCanvas);
    if (!hasMask) {
      syncBadge(false);
      syncStatus('Маска очищена. Отметь сам объект, который нужно заменить.');
      return;
    }
    syncBadge(true);
    syncStatus(message || 'Маска сохранена. Выделен сам объект: он и будет меняться.');
  };

  redraw();
  syncBadge(!!getFile('video.switchxSelectMask'));
  syncStatus(getFile('video.switchxSelectMask') ? 'Маска готова. Можно запускать SwitchX Select.' : 'Отмечай сам объект. Всё вне объекта останется из исходного видео.');

  if (brushInput) {
    brushInput.value = String(Number(editor.brushSize || 28));
    brushInput.oninput = (event) => {
      editor.brushSize = Number(event.target.value || 28);
      if (sizeValue) sizeValue.textContent = `${Number(editor.brushSize || 28)} px`;
    };
  }

  root.querySelectorAll('[data-switchx-mask-tool]').forEach((button) => {
    const nextTool = String(button.getAttribute('data-switchx-mask-tool') || 'brush');
    button.onclick = () => {
      editor.tool = nextTool === 'eraser' ? 'eraser' : 'brush';
      root.querySelectorAll('[data-switchx-mask-tool]').forEach((item) => {
        const active = String(item.getAttribute('data-switchx-mask-tool') || '') === editor.tool;
        item.classList.toggle('primary', active);
        item.classList.toggle('ghost', !active);
      });
      syncStatus(editor.tool === 'eraser' ? 'Ластик: стирай лишнее с маски.' : 'Кисть: отмечай сам объект, который нужно заменить.');
    };
  });

  const clearBtn = document.getElementById('switchxMaskClearBtn');
  if (clearBtn) {
    clearBtn.onclick = async () => {
      maskCtx.clearRect(0, 0, maskCanvas.width, maskCanvas.height);
      editor.maskDataUrl = '';
      switchxMaskEditorSetGeneratedFile(null);
      redraw();
      syncBadge(false);
      syncStatus('Маска очищена.');
    };
  }

  const fillBtn = document.getElementById('switchxMaskFillBtn');
  if (fillBtn) {
    fillBtn.onclick = async () => {
      maskCtx.save();
      maskCtx.globalCompositeOperation = 'source-over';
      maskCtx.fillStyle = '#fff';
      maskCtx.fillRect(0, 0, maskCanvas.width, maskCanvas.height);
      maskCtx.restore();
      redraw();
      await saveMask('Маска залита целиком и сохранена.');
    };
  }

  let drawing = false;
  let lastX = 0;
  let lastY = 0;

  const pointFromEvent = (event) => {
    const rect = canvas.getBoundingClientRect();
    const scaleX = canvas.width / Math.max(rect.width, 1);
    const scaleY = canvas.height / Math.max(rect.height, 1);
    return {
      x: (event.clientX - rect.left) * scaleX,
      y: (event.clientY - rect.top) * scaleY,
    };
  };

  const paint = (fromX, fromY, toX, toY) => {
    maskCtx.save();
    maskCtx.lineCap = 'round';
    maskCtx.lineJoin = 'round';
    maskCtx.lineWidth = Number(editor.brushSize || 28);
    if (editor.tool === 'eraser') {
      maskCtx.globalCompositeOperation = 'destination-out';
      maskCtx.strokeStyle = 'rgba(0,0,0,1)';
    } else {
      maskCtx.globalCompositeOperation = 'source-over';
      maskCtx.strokeStyle = 'rgba(255,255,255,1)';
    }
    maskCtx.beginPath();
    maskCtx.moveTo(fromX, fromY);
    maskCtx.lineTo(toX, toY);
    maskCtx.stroke();
    maskCtx.restore();
    redraw();
  };

  canvas.onpointerdown = (event) => {
    event.preventDefault();
    const point = pointFromEvent(event);
    drawing = true;
    lastX = point.x;
    lastY = point.y;
    paint(point.x, point.y, point.x, point.y);
    canvas.setPointerCapture?.(event.pointerId);
    syncStatus(editor.tool === 'eraser' ? 'Стираю маску...' : 'Рисую маску...');
  };

  canvas.onpointermove = (event) => {
    if (!drawing) return;
    event.preventDefault();
    const point = pointFromEvent(event);
    paint(lastX, lastY, point.x, point.y);
    lastX = point.x;
    lastY = point.y;
  };

  const finishStroke = async (event) => {
    if (!drawing) return;
    drawing = false;
    try { canvas.releasePointerCapture?.(event?.pointerId); } catch (_e) {}
    await saveMask('Маска сохранена. Можно запускать SwitchX Select.');
  };

  canvas.onpointerup = finishStroke;
  canvas.onpointercancel = finishStroke;
  canvas.onpointerleave = (event) => {
    if (drawing && (event.buttons === 0 || typeof event.buttons === 'undefined')) finishStroke(event);
  };
}

function renderSwitchxMaskWorkspace() {
  return '';
  const sourceVideo = getFile('video.sourceVideo');
  const editor = runtime.switchxMaskEditor || {};
  const badgeText = getFile('video.switchxSelectMask') ? 'Mask ready' : 'Mask draft';
  const statusTone = getFile('video.switchxSelectMask') ? 'rgba(64, 196, 144, 0.18)' : 'rgba(255,255,255,0.08)';
  const statusText = !sourceVideo?.url && !editor.frameDataUrl
    ? (state.video.switchxSourceUploadId
        ? 'Source video уже загружен на сервер, но для рисования нужно снова прикрепить файл в браузере.'
        : 'Сначала загрузи source video, потом здесь откроется 1-й кадр.')
    : (editor.loading ? 'Достаю 1-й кадр source video...' : (editor.errorText || 'Отмечай сам объект, который нужно заменить. Маска сохраняется автоматически после каждого штриха.'));
  return `
    <div id="switchxMaskEditorCard" class="inspector-card" style="margin-top:16px; padding:18px; border:1px solid rgba(255,255,255,0.08); background:linear-gradient(180deg, rgba(15,18,31,0.96) 0%, rgba(8,11,22,0.96) 100%); box-shadow:0 18px 40px rgba(0,0,0,0.24);">
      <div style="display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap; margin-bottom:14px;">
        <div>
          <div class="section-title" style="margin:0;">Select mask workspace</div>
          <div class="help-text" style="margin-top:6px;">1-й кадр видео. Отмечай сам объект: только он будет заменён в SwitchX Select.</div>
        </div>
        <span id="switchxMaskReadyBadge" style="display:inline-flex; align-items:center; min-height:34px; padding:0 12px; border-radius:999px; background:${statusTone}; border:1px solid rgba(255,255,255,0.08); color:rgba(255,255,255,0.92); font-size:12px; font-weight:700; letter-spacing:0.01em;">${escapeHtml(badgeText)}</span>
      </div>
      <div style="display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin-bottom:12px;">
        <button type="button" class="btn ${editor.tool === 'eraser' ? 'ghost' : 'primary'} small" data-switchx-mask-tool="brush">Кисть</button>
        <button type="button" class="btn ${editor.tool === 'eraser' ? 'primary' : 'ghost'} small" data-switchx-mask-tool="eraser">Ластик</button>
        <button type="button" class="btn ghost small" id="switchxMaskClearBtn">Очистить</button>
        <button type="button" class="btn ghost small" id="switchxMaskFillBtn">Залить всё</button>
        <label style="display:inline-flex; align-items:center; gap:10px; min-width:220px; flex:1 1 220px;">
          <span class="help-text" style="margin:0; white-space:nowrap;">Размер кисти</span>
          <input id="switchxMaskBrushSize" type="range" min="8" max="120" step="1" value="${Number(editor.brushSize || 28)}" style="flex:1 1 auto;">
          <strong id="switchxMaskBrushSizeValue" style="font-size:12px; color:rgba(255,255,255,0.92); white-space:nowrap;">${Number(editor.brushSize || 28)} px</strong>
        </label>
      </div>
      <div style="position:relative; border-radius:18px; overflow:hidden; border:1px solid rgba(255,255,255,0.08); background:linear-gradient(180deg, rgba(4,7,18,0.96) 0%, rgba(2,4,12,0.98) 100%); min-height:260px; display:flex; align-items:center; justify-content:center;">
        ${sourceVideo?.url || editor.frameDataUrl ? `<canvas id="switchxMaskEditorCanvas" style="display:block; width:100%; max-height:540px; touch-action:none; cursor:crosshair;"></canvas>` : `<div class="empty-copy" style="padding:32px 22px;"><strong>Нет source video</strong><div>Прикрепи исходное видео, чтобы открыть рабочую область для Select.</div></div>`}
      </div>
      <div id="switchxMaskEditorStatus" class="help-text" style="margin-top:12px;">${escapeHtml(statusText)}</div>
    </div>
  `;
}

function renderNav() {
  const nav = document.getElementById('studioNav');
  const order = ['chat', 'video', 'image', 'voice', 'music', 'library', 'history', 'profile', 'partner'];
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

function mobileStudioShortTitle() {
  const meta = STUDIO_META[state.studio] || STUDIO_META.chat;
  const current = currentMeta();
  if (state.studio === 'video') return current.provider && current.model ? `${current.provider} · ${current.model}` : meta.title;
  if (state.studio === 'image') return current.provider && current.model ? `${current.provider} · ${current.model}` : meta.title;
  if (state.studio === 'music') return current.provider || meta.title;
  if (state.studio === 'voice') return 'Voice Studio';
  if (state.studio === 'history') return 'Site Creator';
  if (state.studio === 'library') return 'Prompt Library';
  if (state.studio === 'profile') return 'Профиль';
  if (state.studio === 'partner') return 'Партнёрка';
  return meta.title;
}

function mobileStudioSubtitle() {
  const current = currentMeta();
  if (state.studio === 'chat') return state.chat.mode === 'prompt_builder' ? 'Prompt Builder' : 'Новый чат';
  if (state.studio === 'video' || state.studio === 'image') return [current.mode, state.balance != null ? `${state.balance} ток.` : ''].filter(Boolean).join(' · ');
  if (state.studio === 'music') return state.music.activeTab === 'results' ? 'История и результаты' : (current.mode || 'Генерация музыки');
  if (state.studio === 'voice') return state.voice.isGenerating ? 'Генерация озвучки' : 'Текст в голос';
  if (state.studio === 'profile') return state.me ? formatUserName(state.me) : 'Вход и баланс';
  return current.mode || 'Workspace';
}

function closeMobileOverlays({ keepSheet = false } = {}) {
  runtime.mobileUi.navOpen = false;
  if (!keepSheet) runtime.mobileUi.sheetOpen = false;
}

function hasMobileSettingsPanel() {
  // These sections are full-width workspaces; opening the inspector on mobile would show an empty/irrelevant sheet.
  return !['library', 'history'].includes(state.studio);
}

function hasMobileHistoryPanel() {
  return ['video', 'image', 'voice', 'music', 'profile', 'history'].includes(state.studio);
}

function mobileMusicEditorFallbackTab() {
  const last = String(state.music.lastEditorTab || '');
  if (['idea', 'lyrics', 'songwriter', 'tools'].includes(last)) return last;
  return state.music.ai === 'suno' && state.music.mode === 'lyrics' ? 'lyrics' : 'idea';
}

function openMobileSheet(kind = 'settings') {
  const normalizedKind = kind === 'history' ? 'history' : 'settings';
  if (normalizedKind === 'settings' && !hasMobileSettingsPanel()) {
    closeMobileOverlays();
    renderMobileChrome();
    return;
  }
  if (normalizedKind === 'history' && !hasMobileHistoryPanel()) {
    closeMobileOverlays();
    renderMobileChrome();
    return;
  }
  runtime.mobileUi.navOpen = false;
  runtime.mobileUi.sheetKind = normalizedKind;
  runtime.mobileUi.sheetOpen = true;
  renderMobileChrome();
}

function openMobileHistoryPanel() {
  runtime.mobileUi.navOpen = false;
  runtime.mobileUi.sheetKind = 'history';

  if (!hasMobileHistoryPanel()) {
    closeMobileOverlays();
    renderMobileChrome();
    return;
  }

  if (state.studio === 'video') {
    state.video.panel = 'library';
    if (state.authToken) loadVideoHistory({ silent: true, keepSelection: true }).catch(() => {});
    saveState();
    render();
    runtime.mobileUi.sheetKind = 'history';
    runtime.mobileUi.sheetOpen = true;
    renderMobileChrome();
    return;
  }
  if (state.studio === 'image') {
    state.image.panel = 'library';
    if (state.authToken) loadImageHistory({ silent: true, keepSelection: true }).catch(() => {});
    saveState();
    render();
    runtime.mobileUi.sheetKind = 'history';
    runtime.mobileUi.sheetOpen = true;
    renderMobileChrome();
    return;
  }
  if (state.studio === 'voice') {
    if (state.authToken) loadVoiceHistory({ silent: true, keepSelection: true }).catch(() => {});
    renderInspector();
    runtime.mobileUi.sheetKind = 'history';
    runtime.mobileUi.sheetOpen = true;
    renderMobileChrome();
    return;
  }
  if (state.studio === 'music') {
    state.music.activeTab = 'results';
    if (state.authToken) loadMusicHistory({ silent: true, keepSelection: true }).catch(() => {});
    saveState();
    render();
    runtime.mobileUi.sheetKind = 'history';
    runtime.mobileUi.sheetOpen = true;
    renderMobileChrome();
    return;
  }
  if (state.studio === 'history') {
    if (state.authToken) loadSiteBuilderProjects({ silent: true, keepSelection: true }).catch(() => {});
    scrollWorkspaceToResult();
    return;
  }
  openMobileSheet('history');
}

function openMobileSettingsPanel() {
  runtime.mobileUi.navOpen = false;
  runtime.mobileUi.sheetKind = 'settings';

  if (!hasMobileSettingsPanel()) {
    closeMobileOverlays();
    renderMobileChrome();
    return;
  }

  if (state.studio === 'video') state.video.panel = 'params';
  if (state.studio === 'image') state.image.panel = 'params';
  if (state.studio === 'music' && state.music.activeTab === 'results') {
    state.music.activeTab = mobileMusicEditorFallbackTab();
  }
  saveState();
  render();
  runtime.mobileUi.sheetKind = 'settings';
  runtime.mobileUi.sheetOpen = true;
  renderMobileChrome();
}

function scrollWorkspaceToResult() {
  runtime.mobileUi.navOpen = false;
  runtime.mobileUi.sheetOpen = false;
  renderMobileChrome();
  requestAnimationFrame(() => {
    const target = document.querySelector('.video-stage-card, .image-stage-card, .voice-output-card, .music-stage, .workspace-main, #workspaceBody');
    target?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
}

function renderMobileChrome() {
  const chrome = document.getElementById('mobileWorkspaceChrome');
  if (!chrome) return;
  const isWorkspace = state.view === 'workspace';
  chrome.setAttribute('aria-hidden', isWorkspace ? 'false' : 'true');
  document.body.classList.toggle('mobile-nav-open', isWorkspace && !!runtime.mobileUi.navOpen);
  document.body.classList.toggle('mobile-sheet-open', isWorkspace && !!runtime.mobileUi.sheetOpen);
  document.body.classList.toggle('mobile-sheet-history', isWorkspace && runtime.mobileUi.sheetOpen && runtime.mobileUi.sheetKind === 'history');

  const title = document.getElementById('mobileStudioTitle');
  const subtitle = document.getElementById('mobileStudioSubtitle');
  const balance = document.getElementById('mobileDrawerBalance');
  if (title) title.textContent = mobileStudioShortTitle();
  if (subtitle) subtitle.textContent = mobileStudioSubtitle();
  if (balance) balance.textContent = state.balance == null ? 'Баланс: —' : `Баланс: ${state.balance} ток.`;

  const menu = document.getElementById('mobileStudioMenu');
  if (menu) {
    const groups = [
      ['chat', 'GPT / чат'],
      ['image', 'Дизайн / фото'],
      ['video', 'Видео'],
      ['voice', 'Озвучка'],
      ['music', 'Аудио / музыка'],
      ['library', 'Промты'],
      ['history', 'Сайты'],
      ['partner', 'Партнёрка'],
    ];
    menu.innerHTML = groups.map(([key, label]) => {
      const meta = STUDIO_META[key] || STUDIO_META.chat;
      return `
        <button class="mobile-studio-item ${state.studio === key ? 'active' : ''}" type="button" data-action="switch-studio" data-studio="${key}">
          <span class="mobile-studio-icon">${renderStudioIcon(meta.icon)}</span>
          <span><strong>${escapeHtml(label)}</strong><small>${escapeHtml(meta.title)}</small></span>
        </button>
      `;
    }).join('');
  }

  const sheetTitle = document.querySelector('.inspector-head h2');
  const sheetEyebrow = document.querySelector('.inspector-head .eyebrow');
  if (runtime.mobileUi.sheetKind === 'history') {
    if (sheetTitle && window.matchMedia('(max-width: 900px)').matches) sheetTitle.textContent = 'История';
    if (sheetEyebrow && window.matchMedia('(max-width: 900px)').matches) sheetEyebrow.textContent = 'Results';
  } else if (window.matchMedia('(max-width: 900px)').matches && state.studio !== 'video' && state.studio !== 'image') {
    if (sheetTitle) sheetTitle.textContent = 'Настройки';
    if (sheetEyebrow) sheetEyebrow.textContent = 'Inspector';
  }

  const activeBottomAction = runtime.mobileUi.sheetOpen
    ? (runtime.mobileUi.sheetKind === 'history' ? 'mobile-open-history' : 'mobile-open-settings')
    : 'mobile-show-create';
  document.querySelectorAll('.mobile-bottom-item').forEach((btn) => {
    const action = btn.dataset.action || '';
    const disabled = (action === 'mobile-open-settings' && !hasMobileSettingsPanel())
      || (action === 'mobile-open-history' && !hasMobileHistoryPanel());
    btn.classList.toggle('active', action === activeBottomAction && !disabled);
    btn.toggleAttribute('disabled', disabled);
    btn.setAttribute('aria-disabled', disabled ? 'true' : 'false');
  });
  document.querySelectorAll('.mobile-appbar [data-action="mobile-open-settings"]').forEach((btn) => {
    const disabled = !hasMobileSettingsPanel();
    btn.toggleAttribute('disabled', disabled);
    btn.setAttribute('aria-disabled', disabled ? 'true' : 'false');
  });
  document.querySelectorAll('.mobile-appbar [data-action="mobile-open-history"]').forEach((btn) => {
    const disabled = !hasMobileHistoryPanel();
    btn.toggleAttribute('disabled', disabled);
    btn.setAttribute('aria-disabled', disabled ? 'true' : 'false');
  });

  const inspectorHead = document.querySelector('.inspector-head');
  if (inspectorHead && !inspectorHead.querySelector('[data-action="mobile-close-sheet"]')) {
    const closeBtn = document.createElement('button');
    closeBtn.className = 'mobile-sheet-close';
    closeBtn.type = 'button';
    closeBtn.dataset.action = 'mobile-close-sheet';
    closeBtn.setAttribute('aria-label', 'Закрыть панель');
    closeBtn.textContent = '×';
    inspectorHead.appendChild(closeBtn);
  }
}

function renderHeader() {
  const meta = STUDIO_META[state.studio];
  const shell = document.querySelector('.shell');
  const inspector = document.querySelector('.inspector');
  const workspaceTopline = document.querySelector('.workspace-topline');
  const workspaceShell = document.querySelector('.workspace-shell');
  const globalRunBtn = document.getElementById('globalRunBtn');
  const seedDemoBtn = document.getElementById('seedDemoBtn');
  const topbarActions = document.querySelector('.topbar-actions');
  const resetStudioBtn = document.getElementById('resetStudioBtn');
  const inspectorTitle = document.querySelector('.inspector-head h2');
  const inspectorEyebrow = document.querySelector('.inspector-head .eyebrow');
  const isSiteCreator = state.studio === 'history';
  const isPromptLibrary = state.studio === 'library';
  const hideInspector = isSiteCreator || isPromptLibrary;

  shell?.classList.toggle('chat-no-inspector', false);
  shell?.classList.toggle('music-no-inspector', false);
  shell?.classList.toggle('history-no-inspector', hideInspector);
  workspaceShell?.classList.toggle('workspace-shell--site-creator', hideInspector);

  if (inspector) {
    inspector.hidden = hideInspector;
    inspector.setAttribute('aria-hidden', hideInspector ? 'true' : 'false');
  }
  if (workspaceTopline) workspaceTopline.style.display = state.studio === 'chat' ? 'none' : '';

  const hideTopActions = ['chat', 'video', 'image', 'voice', 'music', 'library', 'history', 'profile', 'partner'].includes(state.studio);
  if (topbarActions) topbarActions.style.display = hideTopActions ? 'none' : '';
  if (seedDemoBtn) seedDemoBtn.style.display = ['video', 'image', 'voice', 'music', 'library', 'history', 'profile', 'partner'].includes(state.studio) ? 'none' : '';
  if (globalRunBtn) globalRunBtn.style.display = ['chat', 'video', 'image', 'voice', 'music', 'library', 'history', 'profile', 'partner'].includes(state.studio) ? 'none' : '';
  if (resetStudioBtn) resetStudioBtn.style.display = ['video', 'image', 'voice', 'library', 'history', 'profile', 'partner'].includes(state.studio) ? 'none' : '';

  if (inspectorTitle) {
    inspectorTitle.textContent = state.studio === 'video' && state.video.panel === 'library' ? 'Библиотека видео' : 'Параметры';
  }
  if (inspectorEyebrow) {
    inspectorEyebrow.textContent = state.studio === 'video' && state.video.panel === 'library' ? 'Library' : 'Inspector';
  }

  const headerTitleEl = document.getElementById('headerTitle');
  const headerSubtitleEl = document.getElementById('headerSubtitle');
  const headerEyebrowEl = document.getElementById('headerEyebrow');
  if (headerTitleEl) headerTitleEl.textContent = meta.title;
  if (headerSubtitleEl) headerSubtitleEl.textContent = meta.subtitle;
  if (headerEyebrowEl) headerEyebrowEl.textContent = meta.eyebrow || meta.title;
  const metaInfo = currentMeta();
  document.getElementById('metaStudio').textContent = metaInfo.studio;
  document.getElementById('metaProvider').textContent = metaInfo.provider;
  document.getElementById('metaModel').textContent = metaInfo.model;
  document.getElementById('metaMode').textContent = metaInfo.mode;
  state.apiBaseUrl = FIXED_API_BASE;
  const apiBaseUrlInput = document.getElementById('apiBaseUrl');
  if (apiBaseUrlInput) apiBaseUrlInput.value = FIXED_API_BASE;
  const balanceValueEl = document.getElementById('balanceValue');
  if (balanceValueEl) balanceValueEl.textContent = state.balance == null ? '—' : `${state.balance} ток.`;
  const apiStatusEl = document.getElementById('apiStatus');
  if (apiStatusEl) {
    apiStatusEl.className = `badge ${state.apiOnline ? 'ok' : 'muted'}`;
    apiStatusEl.textContent = state.apiOnline ? 'online' : 'offline';
  }
  const portalLoginBtn = document.querySelector('.workspace-portal-actions [data-action="login-placeholder"], .workspace-portal-actions [data-action="open-auth-modal"], .workspace-portal-actions [data-action="switch-studio"][data-studio="profile"]');
  if (portalLoginBtn) {
    if (state.authToken && state.me) {
      portalLoginBtn.dataset.action = 'switch-studio';
      portalLoginBtn.dataset.studio = 'profile';
      portalLoginBtn.textContent = 'Профиль';
    } else {
      portalLoginBtn.dataset.action = 'open-auth-modal';
      delete portalLoginBtn.dataset.studio;
      portalLoginBtn.textContent = 'Войти';
    }
  }
  renderAuthCard();
}



function botUsernameFromBase(baseUrl) {
  const fromConfig = window.ASTRABOT_BOT_USERNAME || 'NeiroAstraBot';
  return fromConfig.replace(/^@/, '');
}

function authMethodsLabel(user) {
  const items = Array.isArray(user?.auth_methods) ? user.auth_methods : [];
  if (!items.length) return '—';
  return items.map((item) => item === 'telegram' ? 'Telegram' : item === 'email' ? 'Email' : item).join(' + ');
}

function formatUserName(user) {
  if (!user) return '—';
  const full = `${user.first_name || ''} ${user.last_name || ''}`.trim();
  return full || user.email || (user.username ? `@${user.username}` : (user.linked_telegram_user_id ? 'Telegram user' : 'Email user'));
}

function formatUserMeta(user) {
  if (!user) return '—';
  if (user.email && user.linked_telegram_user_id) return `${user.email} · Telegram + Email`;
  if (user.email) return user.email;
  if (user.username) return `@${user.username}`;
  if (user.linked_telegram_user_id) return `Telegram ID ${user.linked_telegram_user_id}`;
  return `Workspace ID ${user.workspace_user_id || user.telegram_user_id || user.id || '—'}`;
}

function validateEmailValue(value) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(String(value || '').trim());
}


function renderTelegramAuthSlot(targetId, variant = 'default') {
  if (variant === 'native-inline') {
    return `
      <div class="auth-social-btn auth-social-btn--telegram-native" style="min-height:56px; width:100%; padding:8px; display:flex; align-items:center; justify-content:center;">
        <div id="${targetId}" class="telegram-login-mount" style="display:flex; align-items:center; justify-content:center; width:100%; min-height:38px;"></div>
      </div>
    `;
  }

  const icon = variant === 'compact' ? '✈' : '✈';
  return `
    <div class="auth-social-btn auth-social-btn--telegram" style="position:relative; overflow:hidden; min-height:64px; display:flex; align-items:center; justify-content:center;">
      <div class="auth-telegram-fallback" style="pointer-events:none; display:flex; align-items:center; justify-content:center; gap:12px; width:100%; padding:0 18px; font-weight:700; font-size:22px; color:#f4f4f7; text-align:center;">
        <span class="auth-social-icon" style="display:inline-flex; align-items:center; justify-content:center; width:34px; height:34px; border-radius:999px; background:rgba(34,158,217,.18); color:#7fd8ff; font-size:18px; flex:0 0 34px;">${icon}</span>
        <span style="display:inline-block; line-height:1.1;">Продолжить с Telegram</span>
      </div>
      <div id="${targetId}" class="telegram-login-mount" style="position:absolute; inset:0; display:flex; align-items:center; justify-content:center; opacity:.02;"></div>
    </div>
  `;
}

function renderAuthCard() {
  const guest = document.getElementById('authGuestView');
  const userView = document.getElementById('authUserView');
  if (!guest || !userView) return;
  const loggedIn = !!(state.authToken && state.me);
  guest.classList.toggle('hidden', loggedIn);
  userView.classList.toggle('hidden', !loggedIn);
  const hint = document.getElementById('balanceHint');
  if (hint) hint.textContent = loggedIn ? 'Данные из Личного Кабинета' : 'вход через Telegram или по почте';
  if (loggedIn) {
    const user = state.me || {};
    const nameEl = document.getElementById('authUserName');
    const metaEl = document.getElementById('authUserMeta');
    const avatarEl = document.getElementById('authAvatar');
    if (nameEl) nameEl.textContent = formatUserName(user);
    if (metaEl) metaEl.textContent = formatUserMeta(user);
    if (avatarEl) {
      if (user.photo_url) avatarEl.innerHTML = `<img src="${escapeHtml(user.photo_url)}" alt="avatar">`;
      else avatarEl.textContent = (user.first_name || user.email || user.username || 'AB').slice(0, 2).toUpperCase();
    }
    return;
  }
  guest.innerHTML = `
    <div class="auth-copy">Войти в workspace можно через Telegram или по email. Если аккаунт уже создан в Telegram, почту можно привязать в профиле.</div>
    ${renderTelegramAuthSlot('telegramLoginMount')}
    <div class="actions compact-gap" style="margin-top:10px;">
      <button class="btn ghost full" data-action="open-auth-modal" data-tab="register">Войти / зарегистрироваться по почте</button>
    </div>
    <div class="auth-help muted">Telegram нужен для существующих пользователей бота. Для новых пользователей доступна регистрация по email.</div>
  `;
  mountTelegramLogin('telegramLoginMount', 'login');
}

function setSession(payload) {
  state.authToken = payload?.access_token || '';
  state.me = payload?.user || null;
  if (typeof payload?.balance_tokens !== 'undefined') state.balance = Number(payload.balance_tokens || 0);
  state.balanceHistory.loaded = false;
  state.partner.dashboard = null;
  state.partner.loaded = false;
  state.partner.lastError = '';
  state.balanceHistory.lastError = '';
  state.authUi.modalOpen = false;
  state.authUi.modalTab = 'login';
  state.authUi.registerPendingEmail = '';
  state.authUi.linkPendingEmail = '';
  state.authUi.resetPendingEmail = '';
  localStorage.setItem('astrabot:authToken', state.authToken || '');
  localStorage.setItem('astrabot:me', JSON.stringify(state.me || null));
  saveState();
  render();
  if (state.studio === 'history' && state.authToken) {
    loadSiteBuilderMeta({ silent: true }).catch(() => {});
    loadSiteBuilderProjects({ silent: true, keepSelection: true }).catch(() => {});
  }
  if (state.authToken && state.me) {
    bindPendingPartnerRef().then(() => {
      if (state.studio === 'partner') loadPartnerDashboard({ silent: true, force: true, renderNow: true }).catch(() => {});
    }).catch(() => {});
    loadBalanceHistory({ silent: true, force: true, renderNow: state.studio === 'profile' }).catch(() => {});
  }
  if (readPendingTopupTokens()) {
    setTimeout(() => { resumePendingTopup().catch(() => {}); }, 0);
  }
}

async function logoutWorkspace() {
  const previousToken = state.authToken || '';
  state.authToken = '';
  state.me = null;
  state.balance = null;
  state.balanceHistory.items = [];
  state.balanceHistory.loading = false;
  state.balanceHistory.loaded = false;
  state.balanceHistory.lastError = '';
  state.partner.dashboard = null;
  state.partner.loaded = false;
  state.partner.lastError = '';
  state.authUi.modalOpen = false;
  state.authUi.modalTab = 'login';
  state.authUi.registerPendingEmail = '';
  state.authUi.linkPendingEmail = '';
  state.authUi.resetPendingEmail = '';
  state.siteBuilder.projects = [];
  state.siteBuilder.selectedProjectId = '';
  state.siteBuilder.selectedProject = null;
  state.siteBuilder.versions = [];
  state.siteBuilder.jobs = [];
  localStorage.removeItem('astrabot:authToken');
  localStorage.removeItem('astrabot:me');
  localStorage.removeItem('astrabot:partnerState');
  saveState();
  try {
    if (previousToken) {
      await fetch(`${String(state.apiBaseUrl || '').replace(/\/$/, '')}/api/workspace/logout`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${previousToken}` },
      });
    }
  } catch (_) {}
  render();
  setTimeout(() => {
    window.location.reload();
  }, 60);
}



function openAuthModal(tab = 'login') {
  state.authUi.modalOpen = true;
  state.authUi.modalTab = tab || 'login';
  saveState();
  render();
}

function closeAuthModal() {
  state.authUi.modalOpen = false;
  saveState();
  renderAuthModal();
}

function renderAuthModal() {
  let root = document.getElementById('authModalRoot');
  if (!root) {
    root = document.createElement('div');
    root.id = 'authModalRoot';
    document.body.appendChild(root);
  }

  const open = !!state.authUi.modalOpen;
  document.body.classList.toggle('auth-modal-open', open);
  if (!open) {
    root.innerHTML = '';
    return;
  }

  const tab = state.authUi.modalTab || 'login';
  const registerPending = state.authUi.registerPendingEmail || '';
  const resetPending = state.authUi.resetPendingEmail || '';
  root.innerHTML = `
    <div class="auth-modal-backdrop" id="authModalBackdrop">
      <div class="auth-modal-card" role="dialog" aria-modal="true" aria-label="Вход в AstraBot Workspace">
        <button class="auth-modal-close" type="button" data-action="close-auth-modal" aria-label="Закрыть">×</button>
        <div class="auth-modal-title">${tab === 'register' ? 'Создать аккаунт' : tab === 'reset' ? 'Сбросить пароль' : 'С возвращением'}</div>
        <div class="auth-modal-subtitle">${tab === 'register' ? 'Зарегистрируйся, чтобы создавать контент' : tab === 'reset' ? 'Получите код на почту и задайте новый пароль' : 'Войдите, чтобы создавать'}</div>

        ${tab !== 'reset' ? `
          <div style="display:flex; gap:12px; align-items:stretch; flex-wrap:nowrap; margin-bottom:6px;">
            <div style="flex:1 1 0; min-width:0;">
              ${renderTelegramAuthSlot('authModalTelegramMount', 'native-inline')}
            </div>
            <div style="flex:1 1 0; min-width:0;">
              <button class="auth-social-btn" type="button" data-action="google-auth-placeholder" style="min-height:56px; width:100%; display:flex; align-items:center; justify-content:center; gap:10px; padding:0 16px;">
                <span class="auth-social-icon">G</span>
                <span>Google</span>
                <span class="auth-soon-tag">скоро</span>
              </button>
            </div>
          </div>
          <div class="auth-divider"><span>или по EMAIL</span></div>
        ` : ''}

        ${tab === 'login' ? `
          <div class="input-group"><label class="label">Email</label><input id="auth_modal_login_email" type="email" placeholder="name@example.com"></div>
          <div class="input-group auth-password-row"><label class="label">Пароль</label><input id="auth_modal_login_password" type="password" placeholder="Пароль"></div>
          <div class="auth-inline-link-row">
            <button class="link-btn" type="button" data-action="auth-modal-tab-reset">Забыли пароль?</button>
          </div>
          <button id="authModalLoginBtn" class="btn primary full" type="button">Войти</button>
          <div class="auth-switch-row">Нет аккаунта? <button class="link-btn" type="button" data-action="auth-modal-tab-register">Создать</button></div>
        ` : ''}

        ${tab === 'register' ? `
          <div class="input-group"><label class="label">Email</label><input id="auth_modal_register_email" type="email" placeholder="name@example.com" value="${escapeHtml(registerPending)}"></div>
          <div class="input-group"><label class="label">Пароль</label><input id="auth_modal_register_password" type="password" placeholder="Минимум 6 символов"></div>
          <div class="input-group"><label class="label">Повтори пароль</label><input id="auth_modal_register_password2" type="password" placeholder="Повтори пароль"></div>
          <button id="authModalRegisterStartBtn" class="btn primary full" type="button">${registerPending ? 'Отправить код заново' : 'Отправить код'}</button>
          ${registerPending ? `
            <div class="input-group" style="margin-top:12px;"><label class="label">Код из письма</label><input id="auth_modal_register_code" type="text" inputmode="numeric" placeholder="6 цифр"></div>
            <button id="authModalRegisterConfirmBtn" class="btn secondary full" type="button">Подтвердить и войти</button>
          ` : ''}
          <div class="auth-switch-row">Есть аккаунт? <button class="link-btn" type="button" data-action="auth-modal-tab-login">Войти</button></div>
        ` : ''}

        ${tab === 'reset' ? `
          <div class="input-group"><label class="label">Email</label><input id="auth_modal_reset_email" type="email" placeholder="name@example.com" value="${escapeHtml(resetPending)}"></div>
          <button id="authModalResetStartBtn" class="btn primary full" type="button">${resetPending ? 'Отправить код заново' : 'Отправить код'}</button>
          ${resetPending ? `
            <div class="input-group" style="margin-top:12px;"><label class="label">Код из письма</label><input id="auth_modal_reset_code" type="text" inputmode="numeric" placeholder="6 цифр"></div>
            <div class="input-group"><label class="label">Новый пароль</label><input id="auth_modal_reset_password" type="password" placeholder="Минимум 6 символов"></div>
            <div class="input-group"><label class="label">Повтори пароль</label><input id="auth_modal_reset_password2" type="password" placeholder="Повтори пароль"></div>
            <button id="authModalResetConfirmBtn" class="btn secondary full" type="button">Сохранить новый пароль</button>
          ` : ''}
          <div class="auth-switch-row"><button class="link-btn" type="button" data-action="auth-modal-tab-login">Вернуться ко входу</button></div>
        ` : ''}
      </div>
    </div>
  `;

  if (tab !== 'reset') mountTelegramLogin('authModalTelegramMount', 'login');
}

async function handleTelegramAuth(user, intent = 'login') {
  try {
    const path = intent === 'link' && state.authToken ? '/api/workspace/account/link-telegram' : '/api/workspace/auth/telegram';
    const successTitle = intent === 'link' ? 'Telegram привязан' : 'Вход выполнен';
    const res = await apiFetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ auth_data: user }),
    });
    const data = await res.json();
    setSession(data);
    toast('success', successTitle, intent === 'link' ? 'Telegram-аккаунт привязан к текущему профилю.' : `Добро пожаловать, ${formatUserName(data.user)}.`);
  } catch (e) {
    toast('error', intent === 'link' ? 'Не удалось привязать Telegram' : 'Вход через Telegram не выполнен', String(e.message || e));
  }
}

window.onTelegramAuthLogin = (user) => handleTelegramAuth(user, 'login');
window.onTelegramAuthLink = (user) => handleTelegramAuth(user, 'link');

function mountTelegramLogin(targetId = 'telegramLoginMount', intent = 'login') {
  const box = document.getElementById(targetId);
  if (!box) return;
  if (intent === 'login' && state.authToken && state.me) return;
  box.innerHTML = '';
  const script = document.createElement('script');
  script.async = true;
  script.src = 'https://telegram.org/js/telegram-widget.js?22';
  script.setAttribute('data-telegram-login', botUsernameFromBase(state.apiBaseUrl));
  script.setAttribute('data-size', 'large');
  script.setAttribute('data-radius', '12');
  script.setAttribute('data-userpic', 'false');
  script.setAttribute('data-request-access', 'write');
  script.setAttribute('data-onauth', intent === 'link' ? 'onTelegramAuthLink(user)' : 'onTelegramAuthLogin(user)');
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
    saveState();
    return true;
  } catch (e) {
    state.authToken = '';
    state.me = null;
    state.balanceHistory.items = [];
    state.balanceHistory.loading = false;
    state.balanceHistory.loaded = false;
    state.balanceHistory.lastError = '';
    localStorage.removeItem('astrabot:authToken');
    localStorage.removeItem('astrabot:me');
    saveState();
    return false;
  }
}

async function submitEmailLogin(email, password) {
  const res = await apiFetch('/api/workspace/auth/email-login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  });
  const data = await res.json();
  setSession(data);
  return data;
}

async function submitEmailRegisterStart(email, password) {
  const res = await apiFetch('/api/workspace/auth/email-register/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  });
  return res.json();
}

async function submitEmailRegisterConfirm(email, code) {
  const res = await apiFetch('/api/workspace/auth/email-register/confirm', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, code }),
  });
  const data = await res.json();
  setSession(data);
  return data;
}

async function submitLinkEmailStart(email, password) {
  const res = await apiFetch('/api/workspace/account/link-email/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  });
  return res.json();
}

async function submitLinkEmailConfirm(email, code) {
  const res = await apiFetch('/api/workspace/account/link-email/confirm', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, code }),
  });
  const data = await res.json();
  setSession(data);
  return data;
}

async function submitPasswordResetStart(email) {
  const res = await apiFetch('/api/workspace/auth/password-reset/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email }),
  });
  return res.json();
}

async function submitPasswordResetConfirm(email, code, password) {
  const res = await apiFetch('/api/workspace/auth/password-reset/confirm', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, code, password }),
  });
  const data = await res.json();
  setSession(data);
  return data;
}

async function submitChangePassword(currentPassword, newPassword) {
  const res = await apiFetch('/api/workspace/account/change-password', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
  });
  const data = await res.json();
  setSession(data);
  return data;
}

function renderRecentRuns() {
  const box = document.getElementById('recentRuns');
  if (!box) return;
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

function sanitizeLegacySidebarBalanceCard() {
  const sidebar = document.querySelector('.sidebar');
  if (!sidebar) return;
  const allowed = new Set(['brand', 'studio-nav']);
  Array.from(sidebar.children).forEach((node) => {
    if (!(node instanceof HTMLElement)) return;
    if (node.id === 'studioNav') return;
    if (node.classList.contains('brand')) return;
    if (node.classList.contains('sidebar-balance-only-card')) return;
    const text = String(node.textContent || '').toLowerCase();
    if (
      text.includes('системные настройки') ||
      text.includes('подключение и баланс') ||
      text.includes('api base url') ||
      text.includes('проверить api')
    ) {
      node.remove();
    }
  });
}

function sanitizeLegacySiteCreatorLayout() {
  if (state.studio !== 'history') return;
  const body = document.getElementById('workspaceBody');
  if (!body) return;
  const grid = body.querySelector('.site-creator-grid');
  if (!grid) return;

  const removePatterns = [
    'последние действия',
    'аккаунт',
    'системные настройки',
    'подключение и баланс',
    'api base url',
    'проверить api'
  ];

  Array.from(grid.children).forEach((column) => {
    if (!(column instanceof HTMLElement)) return;
    const text = String(column.textContent || '').toLowerCase();
    if (removePatterns.some((pattern) => text.includes(pattern))) {
      column.remove();
    }
  });

  body.querySelectorAll('.history-card, .profile-card, .result-card, .soft-panel, .panel').forEach((card) => {
    if (!(card instanceof HTMLElement)) return;
    const text = String(card.textContent || '').toLowerCase();
    if (removePatterns.some((pattern) => text.includes(pattern))) {
      card.remove();
    }
  });
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
    case 'partner': el.innerHTML = renderPartnerWorkspace(); break;
    default: el.innerHTML = `<div class="placeholder-stage"><div class="empty-copy"><strong>Студия в разработке</strong><div>Для этой студии пока нет workspace-renderer.</div></div></div>`;
  }
  sanitizeLegacySidebarBalanceCard();
  sanitizeLegacySiteCreatorLayout();
  attachImageCompareInteractions();
  initShowcaseMedia();
  initSwitchxMaskEditor().catch(() => {});
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
    case 'partner': el.innerHTML = renderPartnerInspector(); break;
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
      ${attachments.map((item, index) => renderChatAttachmentCard(item, index)).join('')}
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

function isTerminalTaskStatus(status) {
  const value = String(status || '').toLowerCase();
  return ['succeeded', 'completed', 'success', 'finished', 'done', 'failed', 'error', 'cancelled', 'canceled'].includes(value);
}

function parseTimestampMs(value) {
  const ts = Date.parse(String(value || '').trim());
  return Number.isFinite(ts) ? ts : 0;
}

function promptsLookSimilar(left, right) {
  const a = String(left || '').trim().toLowerCase();
  const b = String(right || '').trim().toLowerCase();
  if (!a || !b) return true;
  if (a === b) return true;
  const shortA = a.slice(0, 72);
  const shortB = b.slice(0, 72);
  return shortA.includes(shortB) || shortB.includes(shortA);
}

function findRecentHistoryCandidate(items, options = {}) {
  const startedAtMs = parseTimestampMs(options.startedAt);
  const maxAgeMs = Number(options.maxAgeMs || (45 * 60 * 1000));
  if (!Array.isArray(items) || !items.length || !startedAtMs) return null;
  const provider = String(options.provider || '').trim().toLowerCase();
  const model = String(options.model || '').trim().toLowerCase();
  const prompt = String(options.prompt || '').trim();
  const now = Date.now();
  return items.find((item) => {
    const itemMs = parseTimestampMs(item?.created_at || item?.updated_at || item?.completed_at);
    if (!itemMs) return false;
    if (itemMs < (startedAtMs - 2 * 60 * 1000)) return false;
    if (itemMs > (now + 5 * 60 * 1000)) return false;
    if (now - itemMs > maxAgeMs) return false;
    if (provider && String(item?.provider || '').trim().toLowerCase() !== provider) return false;
    if (model && String(item?.model || '').trim().toLowerCase() !== model) return false;
    if (!promptsLookSimilar(prompt, item?.prompt)) return false;
    return true;
  }) || null;
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

function stopImagePolling() {
  if (runtime.imagePollTimer) {
    clearInterval(runtime.imagePollTimer);
    runtime.imagePollTimer = null;
  }
}

function startImagePolling({ immediate = false } = {}) {
  const status = String(state.image.status || state.image.lastStatus || '').toLowerCase();
  if (!state.authToken || !state.image.generationId || state.image.outputUrl || ['failed', 'error', 'cancelled', 'canceled'].includes(status)) return;
  stopImagePolling();
  runtime.imagePollTimer = setInterval(() => {
    pollImageTask({ silent: true }).catch(() => {});
  }, 3000);
  if (immediate) {
    pollImageTask({ silent: true }).catch(() => {});
  }
}

function stopSwitchxRefPolling() {
  if (runtime.switchxRefPollTimer) {
    clearInterval(runtime.switchxRefPollTimer);
    runtime.switchxRefPollTimer = null;
  }
}

function startSwitchxRefPolling({ immediate = false } = {}) {
  const status = String(state.video.switchxReferenceStatus || '').toLowerCase();
  if (!state.authToken || !state.video.switchxRefGenerationId || state.video.switchxReferenceImageUrl || ['failed', 'error', 'completed'].includes(status)) return;
  stopSwitchxRefPolling();
  runtime.switchxRefPollTimer = setInterval(() => {
    pollSwitchxReference({ silent: true }).catch(() => {});
  }, 3000);
  if (immediate) {
    pollSwitchxReference({ silent: true }).catch(() => {});
  }
}

async function pollSwitchxReference({ silent = false } = {}) {
  if (!state.video.switchxRefGenerationId) return;
  try {
    const res = await apiFetch(`/api/workspace/video/switchx/reference/${encodeURIComponent(state.video.switchxRefGenerationId)}`);
    const data = await res.json();
    const item = data.item || {};
    state.video.switchxReferenceStatus = String(item.status || '').toLowerCase() || 'processing';
    if (item.image_url || item.download_url) {
      state.video.switchxReferenceImageUrl = item.download_url || item.image_url || '';
      state.video.switchxReferenceStatus = 'completed';
      stopSwitchxRefPolling();
      saveState();
      render();
      if (!silent) toast('success', 'AI-референс готов', 'Можно запускать SwitchX.');
      return;
    }
    if (['failed', 'error'].includes(state.video.switchxReferenceStatus)) {
      stopSwitchxRefPolling();
      state.video.errorText = item.error_message || 'Не удалось создать AI-референс.';
      saveState();
      render();
      if (!silent) toast('error', 'AI-референс не создан', state.video.errorText);
      return;
    }
    saveState();
    render();
  } catch (e) {
    if (!silent) toast('error', 'Не удалось обновить статус', String(e.message || e));
  }
}

async function requestSwitchxReference() {
  const sourceVideo = getFile('video.sourceVideo');
  if (!sourceVideo?.file && !state.video.switchxSourceUploadId) {
    toast('error', 'Нужно видео', 'Сначала загрузи исходное видео для SwitchX.');
    return;
  }
  if (!state.video.switchxRefPrompt.trim()) {
    toast('error', 'Нужен prompt для AI-референса', 'Заполни отдельный prompt для Nano Banana Pro.');
    return;
  }
  state.video.switchxReferenceStatus = 'queued';
  state.video.switchxReferenceImageUrl = '';
  state.video.errorText = '';
  saveState();
  render();
  const form = new FormData();
  form.append('ref_prompt', state.video.switchxRefPrompt.trim());
  if (state.video.switchxSourceUploadId) form.append('source_video_upload_id', state.video.switchxSourceUploadId);
  if (sourceVideo?.file && !state.video.switchxSourceUploadId) form.append('source_video', sourceVideo.file, sourceVideo.name || sourceVideo.file.name || 'source_video');
  try {
    const res = await apiFetch('/api/workspace/video/switchx/reference/run', { method: 'POST', body: form });
    const data = await res.json();
    state.video.switchxRefGenerationId = data.generation_id || '';
    state.video.switchxSourceUploadId = data.source_video_upload_id || state.video.switchxSourceUploadId;
    state.video.switchxReferenceStatus = 'processing';
    if (typeof data.balance_tokens !== 'undefined') state.balance = Number(data.balance_tokens || 0);
    saveState();
    render();
    startSwitchxRefPolling({ immediate: true });
    toast('success', 'AI-референс запущен', data.status_text || 'Создание референса началось.');
  } catch (e) {
    state.video.switchxReferenceStatus = 'failed';
    state.video.errorText = String(e.message || e);
    saveState();
    render();
    toast('error', 'Не удалось запустить AI-референс', state.video.errorText);
  }
}


function stopVoicePolling() {
  if (runtime.voicePollTimer) {
    clearInterval(runtime.voicePollTimer);
    runtime.voicePollTimer = null;
  }
}

function startVoicePolling({ immediate = false } = {}) {
  if (!state.authToken || !state.voice.generationId || state.voice.audioUrl || !state.voice.isGenerating) return;
  stopVoicePolling();
  runtime.voicePollTimer = setInterval(() => {
    pollVoiceTask({ silent: true }).catch(() => {});
  }, 3000);
  if (immediate) {
    pollVoiceTask({ silent: true }).catch(() => {});
  }
}

function clearVideoRunState({ keepPrompt = true } = {}) {
  stopVideoPolling();
  stopVideoEditPolling();
  stopSwitchxRefPolling();
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
  state.video.switchxRefGenerationId = '';
  state.video.switchxReferenceImageUrl = '';
  state.video.switchxReferenceStatus = 'idle';
  state.video.switchxSourceUploadId = '';
  state.video.seedanceUseStartFrame = false;
  state.video.seedanceUseLastFrame = false;
  resetVideoEditorState();
  if (!keepPrompt) {
    state.video.prompt = '';
    state.video.switchxRefPrompt = '';
  }
  saveState();
}

function clearRuntimeInputEntries(items = []) {
  items.forEach(({ key, inputId }) => {
    revokeRuntimeFileValue(runtime.files[key]);
    delete runtime.files[key];
    const input = document.getElementById(inputId);
    if (input) input.value = '';
  });
}

function clearVideoInputFiles() {
  clearRuntimeInputEntries([
    { key: 'video.startFrame', inputId: 'video_startFrame' },
    { key: 'video.endFrame', inputId: 'video_endFrame' },
    { key: 'video.lastFrame', inputId: 'video_lastFrame' },
    { key: 'video.referenceImages', inputId: 'video_referenceImages' },
    { key: 'video.referenceAudios', inputId: 'video_referenceAudios' },
    { key: 'video.referenceVideos', inputId: 'video_referenceVideos' },
    { key: 'video.avatarImage', inputId: 'video_avatarImage' },
    { key: 'video.motionVideo', inputId: 'video_motionVideo' },
    { key: 'video.sourceVideo', inputId: 'video_sourceVideo' },
    { key: 'video.switchxSelectMask', inputId: 'video_switchxSelectMask' },
  ]);
  state.video.motionDurationSec = null;
  state.video.sourceVideoDurationSec = null;
  resetSwitchxMaskEditor({ clearMaskFile: false });
}

function resetVideoTransientState({ keepPrompt = false, keepFiles = false } = {}) {
  clearVideoRunState({ keepPrompt });
  if (!keepFiles) clearVideoInputFiles();
}

function currentVideoFieldSet() {
  syncVideoSelection();
  const fields = videoModeConfig()?.fields;
  return new Set(Array.isArray(fields) ? fields : []);
}

function videoModeUsesField(field) {
  return currentVideoFieldSet().has(field);
}

function clearImageInputFiles() {
  clearRuntimeInputEntries([
    { key: 'image.sourceImage', inputId: 'image_sourceImage' },
    { key: 'image.baseImage', inputId: 'image_baseImage' },
    { key: 'image.styleRefImage', inputId: 'image_styleRefImage' },
    { key: 'image.omniRefImage', inputId: 'image_omniRefImage' },
  ]);
}

function clearImageRunState({ keepPrompt = true, keepFiles = true } = {}) {
  state.image.outputUrl = '';
  state.image.downloadUrl = '';
  state.image.beforeImageUrl = '';
  state.image.afterImageUrl = '';
  state.image.imageUrls = [];
  state.image.availableActions = {};
  state.image.activeImageIndex = 0;
  state.image.compareMode = false;
  state.image.comparePosition = 50;
  state.image.generationId = '';
  state.image.errorText = '';
  state.image.isGenerating = false;
  state.image.statusText = 'Выбери режим, добавь изображения при необходимости и запусти генерацию.';
  if (!keepPrompt) state.image.prompt = '';
  if (!keepFiles) clearImageInputFiles();
  saveState();
}

function buildVideoEditorLaunchUrl() {
  const baseUrl = getApiBaseUrl();
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
  const switchxRefCard = (state.video.provider === 'switchx' && state.video.switchxReferenceImageUrl) ? `
    <article class="media-card" style="position:relative; overflow:hidden; border:1px solid rgba(255,255,255,0.08); background:linear-gradient(180deg, rgba(16,20,34,0.96) 0%, rgba(7,10,20,0.96) 100%); box-shadow:0 18px 40px rgba(0,0,0,0.28);">
      <div class="media-card-head" style="display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:12px;">
        <span style="display:inline-flex; align-items:center; gap:8px; min-height:36px; padding:0 14px; border-radius:999px; border:1px solid rgba(255,177,66,0.22); background:linear-gradient(180deg, rgba(255,186,92,0.18) 0%, rgba(255,132,48,0.10) 100%); color:rgba(255,244,225,0.96); font-size:13px; font-weight:700; letter-spacing:0.01em; box-shadow:0 10px 24px rgba(255,140,40,0.10);">
          <span style="width:8px; height:8px; border-radius:50%; background:#ffb142; box-shadow:0 0 12px rgba(255,177,66,0.7);"></span>
          AI reference
        </span>
        <button type="button" data-action="remove-switchx-ai-reference" title="Убрать AI-референс" aria-label="Убрать AI-референс" style="width:36px; height:36px; border:none; border-radius:12px; display:inline-flex; align-items:center; justify-content:center; background:linear-gradient(180deg, rgba(255,255,255,0.10) 0%, rgba(255,255,255,0.05) 100%); box-shadow:0 10px 24px rgba(0,0,0,0.22), inset 0 0 0 1px rgba(255,255,255,0.08); color:rgba(255,255,255,0.92); font-size:17px; font-weight:700; line-height:1; cursor:pointer; flex:0 0 auto;">✕</button>
      </div>
      <div class="media-card-preview" style="display:flex; align-items:center; justify-content:center; min-height:188px; padding:14px; border-radius:18px; background:radial-gradient(circle at top, rgba(255,177,66,0.10), transparent 38%), linear-gradient(180deg, rgba(8,12,26,0.96) 0%, rgba(4,7,18,0.92) 100%); box-shadow:inset 0 0 0 1px rgba(255,255,255,0.04);">
        <img class="preview-media" style="display:block; width:100%; height:100%; max-width:220px; max-height:160px; object-fit:contain; object-position:center; border-radius:16px; box-shadow:0 14px 32px rgba(0,0,0,0.34); margin:0 auto;" src="${escapeHtml(state.video.switchxReferenceImageUrl)}" alt="AI reference" />
      </div>
      <div class="help-text">Сгенерирован из 1-го кадра через Nano Banana Pro.</div>
    </article>
  ` : '';
  const assets = [
    videoModeUsesField('sourceVideo') ? mediaCard('Source video', getFile('video.sourceVideo'), true, false, 'contain') : '',
    videoModeUsesField('startFrame') ? mediaCard('Start frame', getFile('video.startFrame'), false, false, 'contain') : '',
    videoModeUsesField('endFrame') ? mediaCard('End frame', getFile('video.endFrame'), false, false, 'contain') : '',
    videoModeUsesField('lastFrame') ? mediaCard('Last frame', getFile('video.lastFrame'), false, false, 'contain') : '',
    videoModeUsesField('avatarImage') ? mediaCard('Avatar image', getFile('video.avatarImage'), false, false, 'contain') : '',
    videoModeUsesField('motionVideo') ? mediaCard('Motion video', getFile('video.motionVideo'), true, false, 'contain') : '',
    videoModeUsesField('referenceImages') ? mediaCard('Reference images', getFile('video.referenceImages'), false, true) : '',
    videoModeUsesField('referenceVideos') ? mediaCard('Reference videos', getFile('video.referenceVideos'), true, true, 'contain') : '',
    switchxRefCard,
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

  const hideMainPromptForKling3NewMultiShot = state.video.model === 'kling-3.0-new' && state.video.mode === 'multi_shot';
  const promptCard = hideMainPromptForKling3NewMultiShot ? '' : renderWorkspacePromptCard(
    state.video.provider === 'switchx' ? 'SwitchX prompt' : 'Prompt',
    'video_prompt',
    state.video.prompt,
    state.video.provider === 'switchx'
      ? 'Опиши итоговую замену/стилизацию: окружение, свет, атмосферу и желаемый финальный вид.'
      : (state.video.provider === 'pixverse_c1' && state.video.mode === 'fusion'
        ? 'Собери сцену через теги @image1, @image2 и далее: кто что делает, где происходит, как движется камера.'
        : 'Опиши сцену, действие, камеру, свет и ожидаемый результат.'),
    state.video.provider === 'switchx'
      ? 'Этот prompt уходит в финальный запуск SwitchX. Для AI-референса ниже используется отдельный prompt.'
      : (state.video.provider === 'pixverse_c1' && state.video.mode === 'fusion'
        ? 'Референсы автоматически помечаются как @image1 , @image2 , @image3 и далее по порядку загрузки.'
        : '')
  );
  const pixverseFusionAliasBar = renderPixverseFusionAliasBar();

  const seedanceFrameControls = renderSeedanceFrameControls();
  const seedanceOmniVideoReference = renderSeedanceOmniVideoReference();
  const switchxMaskWorkspace = renderSwitchxMaskWorkspace();

  return `
    <div class="workspace-grid single video-workspace-grid">
      <div class="workspace-main scroll video-workspace-main">
        <div class="result-card video-stage-card video-stage-card-plain">
          <div class="placeholder-stage video video-stage-clean">
            ${stageInner}
          </div>
        </div>
        ${promptCard}
        ${renderKling3NewWorkspaceMultiShotBlock()}
        ${pixverseFusionAliasBar}
        ${switchxMaskWorkspace}
        ${seedanceFrameControls}
        ${seedanceOmniVideoReference}
        ${assets ? `<div class="upload-grid two" style="margin-top:16px;">${assets}</div>` : ''}
      </div>
    </div>
  `;
}

function renderSeedanceFrameControls() {
  if (!(state.video.provider === 'seedance_kie' && state.video.mode === 'image_to_video')) return '';

  const startBlock = `
    <div>
      <div class="inspector-card">${fieldTogglePanel('Use start frame', 'video_seedanceUseStartFrame', !!state.video.seedanceUseStartFrame, 'Добавляет стартовый кадр как приоритетный image reference.', state.video.seedanceUseStartFrame ? 'Активно' : 'Выключено')}</div>
      ${state.video.seedanceUseStartFrame ? sectionUpload('Start frame', 'video_startFrame', 'Опциональный стартовый кадр. Учитывается в общем лимите до 7 изображений.', false, 'image/*') : ''}
    </div>
  `;

  const lastBlock = `
    <div>
      <div class="inspector-card">${fieldTogglePanel('Use last frame', 'video_seedanceUseLastFrame', !!state.video.seedanceUseLastFrame, 'Добавляет последний кадр как финальный image reference.', state.video.seedanceUseLastFrame ? 'Активно' : 'Выключено')}</div>
      ${state.video.seedanceUseLastFrame ? sectionUpload('Last frame', 'video_lastFrame', 'Опциональный последний кадр. Учитывается в общем лимите до 7 изображений.', false, 'image/*') : ''}
    </div>
  `;

  return `
    <div class="upload-grid two" style="margin-top:16px;">
      ${startBlock}
      ${lastBlock}
    </div>
  `;
}


function renderSeedanceOmniVideoReference() {
  if (!(state.video.provider === 'seedance_kie' && state.video.mode === 'omni_reference')) return '';
  const surcharge = state.video.model === 'seedance-kie-fast' ? 13 : 20;
  return `
    <div style="margin-top:16px;">
      ${sectionUpload('Video reference', 'video_referenceVideos', `MP4 / MOV. Можно без видео, но если добавлен хотя бы один video reference, к запуску прибавляется +${surcharge} ток.`, true, 'video/mp4,video/quicktime,.mp4,.mov')}
      <div class="help-text" style="margin-top:10px;">Суммарная длина всех video references — до 15.4 сек. Photo reference и audio reference остаются справа.</div>
    </div>
  `;
}


function renderImageWorkspace() {
  syncImageSelection();
  const source = getFile('image.sourceImage');
  const sourceIsMultiple = Array.isArray(source);
  const base = getFile('image.baseImage');
  const styleRef = getFile('image.styleRefImage');
  const omniRef = getFile('image.omniRefImage');
  const historyItem = state.image.panel === 'library' ? imageHistorySelectedItem() : null;
  const compareState = imageCompareState(historyItem);
  const stageProvider = imageStageProvider(historyItem);
  const stageImageUrls = imageActiveUrls(historyItem);
  const safeIndex = Math.max(0, Math.min(stageImageUrls.length - 1, Number(state.image.activeImageIndex || 0)));
  const selectedStageUrl = stageImageUrls[safeIndex] || '';
  const activeUrl = selectedStageUrl || (historyItem ? imageHistoryUrl(historyItem) : (state.image.outputUrl || compareState.afterUrl));
  const activeDownloadUrl = selectedStageUrl || (historyItem ? imageHistoryUrl(historyItem) : (state.image.downloadUrl || state.image.outputUrl || compareState.afterUrl));
  const assets = [
    mediaCard(state.image.provider === 'nano_banana_pro_new' ? 'Reference images' : 'Source image', source, false, sourceIsMultiple, 'contain'),
    mediaCard('Base image', base, false, false, 'contain'),
    stageProvider === 'midjourney' ? mediaCard('Style ref', styleRef, false, false, 'contain') : '',
    stageProvider === 'midjourney' ? mediaCard('Omni ref', omniRef, false, false, 'contain') : '',
  ].filter(Boolean).join('');

  const showImageLoading = state.image.isGenerating || (!!state.image.generationId && !activeUrl && !state.image.errorText);
  const canReroll = stageProvider === 'midjourney' && !!state.image.generationId;
  const stageInner = compareState.compareMode ? `
    <div class="video-stage-result image-stage-result">
      ${renderImageCompareStage(compareState.beforeUrl, compareState.afterUrl)}
      <div class="actions compact-gap" style="justify-content:center; flex-wrap:wrap; margin-top:14px;">
        <button class="btn primary" data-action="download-image-result">Скачать изображение</button>
        ${historyItem ? `<button class="btn outline" data-action="use-image-history-item" data-generation-id="${escapeHtml(historyItem.id || '')}">В рабочую зону</button>` : `<button class="btn outline" data-action="clear-image-run">Очистить результат</button>`}
      </div>
      ${historyItem ? `<div class="help-text" style="margin-top:10px;">Открыт сохранённый результат из истории Image Studio.</div>` : ''}
    </div>
  ` : (stageProvider === 'midjourney' && stageImageUrls.length ? `
    <div class="video-stage-result image-stage-result">
      <div style="display:grid; gap:14px;">
        <img class="preview-media image-preview-media" src="${escapeHtml(selectedStageUrl)}" alt="Midjourney result">
        <div style="display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:12px;">
          ${stageImageUrls.map((url, index) => `
            <article style="border:1px solid ${safeIndex === index ? 'rgba(255,178,66,0.65)' : 'rgba(255,255,255,0.08)'}; border-radius:18px; padding:10px; background:rgba(9,14,28,0.88); box-shadow:${safeIndex === index ? '0 0 0 1px rgba(255,178,66,0.15)' : 'none'};">
              <button type="button" data-action="select-mj-image" data-image-index="${index}" style="display:block; width:100%; padding:0; border:none; background:none; cursor:pointer;">
                <img src="${escapeHtml(url)}" alt="Midjourney ${index + 1}" style="display:block; width:100%; aspect-ratio:1/1; object-fit:cover; border-radius:14px;">
              </button>
              <div class="actions compact-gap" style="margin-top:10px; flex-wrap:wrap;">
                <button class="btn outline small" data-action="midjourney-variation" data-image-index="${index}" data-variation-type="subtle">Vary subtle</button>
                <button class="btn outline small" data-action="midjourney-variation" data-image-index="${index}" data-variation-type="strong">Vary strong</button>
              </div>
            </article>
          `).join('')}
        </div>
        <div class="actions compact-gap" style="justify-content:center; flex-wrap:wrap; margin-top:4px;">
          <button class="btn primary" data-action="download-image-result">Скачать выбранное</button>
          <button class="btn outline" data-action="midjourney-reroll" ${canReroll ? '' : 'disabled'}>Reroll</button>
          ${historyItem ? `<button class="btn outline" data-action="use-image-history-item" data-generation-id="${escapeHtml(historyItem.id || '')}">В рабочую зону</button>` : `<button class="btn outline" data-action="clear-image-run">Очистить результат</button>`}
        </div>
      </div>
      ${historyItem ? `<div class="help-text" style="margin-top:10px;">Открыт сохранённый Midjourney результат из истории Image Studio.</div>` : '<div class="help-text" style="margin-top:10px;">Один запуск Midjourney возвращает 4 изображения. Выбери карточку ниже и запускай reroll / variation.</div>'}
    </div>
  ` : activeUrl ? `
    <div class="video-stage-result image-stage-result">
      <img class="preview-media image-preview-media" src="${escapeHtml(activeUrl)}" alt="Generated image">
      <div class="actions compact-gap" style="justify-content:center; flex-wrap:wrap; margin-top:14px;">
        <button class="btn primary" data-action="download-image-result">Скачать изображение</button>
        ${historyItem ? `<button class="btn outline" data-action="use-image-history-item" data-generation-id="${escapeHtml(historyItem.id || '')}">В рабочую зону</button>` : `<button class="btn outline" data-action="clear-image-run">Очистить результат</button>`}
      </div>
      ${historyItem ? `<div class="help-text" style="margin-top:10px;">Открыт сохранённый результат из истории Image Studio.</div>` : ''}
    </div>
  ` : (showImageLoading ? `
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
  `));

  const promptCard = state.image.provider === 'topaz_photo'
    ? ''
    : renderWorkspacePromptCard(
        stageProvider === 'midjourney' ? 'Midjourney prompt' : 'Prompt',
        'image_prompt',
        state.image.prompt,
        stageProvider === 'midjourney' ? 'Опиши сцену, стиль, свет, композицию, материалы, одежду, фон и детали. Midjourney v7 параметры уйдут отдельно из правой панели.' : 'Опиши, что нужно создать или изменить: стиль, композицию, свет, детали, фон.',
        '',
        stageProvider === 'midjourney'
          ? renderWorkspacePromptExtraTextarea(
              'Negative prompt',
              'image_negativePrompt',
              state.image.negativePrompt || '',
              'Что исключить из кадра: лишние люди, текст, watermark, extra fingers, low quality и т.д.',
              4,
            )
          : '',
      );

  return `
    <div class="workspace-grid single image-workspace-grid">
      <div class="workspace-main scroll image-workspace-main">
        <div class="result-card image-stage-card image-stage-card-plain">
          <div class="placeholder-stage image image-stage-clean">
            ${stageInner}
          </div>
        </div>
        ${promptCard}
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
            <div class="help-text">Выбранный голос: <strong>${escapeHtml(voiceName)}</strong> · ${escapeHtml(voiceModelLabel(state.voice.modelId))}</div>
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
        ${fieldSelect('Язык', 'voice_languageCode', state.voice.languageCode || 'auto', languageOptions)}
      </div>
      <div class="help-text" style="margin-top:12px;">Текущий голос: <strong>${escapeHtml(selectedVoice?.name || 'не выбран')}</strong> · Язык: <strong>${escapeHtml(voiceLanguageLabel(state.voice.languageCode || 'auto'))}</strong></div>
    </div>

    <div class="inspector-card voice-advanced-card ${state.voice.showAdvancedPanel ? 'open' : ''}">
      <div class="field-head voice-advanced-head" style="align-items:flex-start; gap:12px;">
        <div>
          <h4 style="margin:0 0 6px;">Расширенные настройки</h4>
          <div class="help-text">Эти параметры применяются только к новому запуску на сайте и не переписывают историю.</div>
        </div>
        <button class="btn ghost small" data-action="voice-toggle-advanced">${state.voice.showAdvancedPanel ? 'Скрыть' : 'Показать'}</button>
      </div>

      ${state.voice.showAdvancedPanel ? `
        <div class="voice-advanced-body">
          <label class="toggle-pill">
            <input id="voice_manualVoiceSettings" type="checkbox" ${state.voice.manualVoiceSettings ? 'checked' : ''}>
            <span>Ручные voice settings</span>
          </label>

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
          </div>
        </div>
      ` : ''}
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

function resetMusicWorkspaceStage(options = {}) {
  const { keepToolState = false } = options;
  stopMusicPolling();
  stopMusicToolPolling();
  state.music.isGenerating = false;
  state.music.results = [];
  state.music.generationId = '';
  state.music.status = 'idle';
  state.music.statusText = 'Заполни идею или текст и запусти генерацию.';
  state.music.errorText = '';
  state.music.lastCompletedAt = '';
  state.musicHistory.selectedId = '';
  state.musicHistory.selectedItem = null;
  if (!keepToolState) {
    state.music.toolTaskId = '';
    state.music.toolTaskStatus = 'idle';
    state.music.toolTaskMessage = '';
    state.music.toolTracks = [];
  }
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
    if (['tools', 'songwriter'].includes(state.music.activeTab) || (!preserveLyricsTab && state.music.activeTab === 'lyrics')) {
      state.music.activeTab = 'idea';
    }
    if (state.music.lastEditorTab === 'songwriter') state.music.lastEditorTab = 'idea';
  } else {
    state.music.backend = 'sunoapi';
    if (!['V4_5', 'V5'].includes(state.music.model)) state.music.model = 'V4_5';
  }

  if (!['idea', 'lyrics', 'songwriter', 'tools', 'results'].includes(state.music.activeTab)) state.music.activeTab = 'idea';
  if (!['upload-cover', 'upload-extend', 'add-vocals'].includes(state.music.toolAction)) state.music.toolAction = 'upload-cover';
  if (!['style_persona', 'voice_persona'].includes(state.music.personaModel)) state.music.personaModel = 'style_persona';
}

function setMusicTab(tab) {
  if (!['idea', 'lyrics', 'songwriter', 'tools', 'results'].includes(String(tab || ''))) return;

  if (tab === 'tools' && state.music.ai !== 'suno') {
    state.music.activeTab = 'idea';
    state.music.lastEditorTab = 'idea';
    saveState();
    render();
    return;
  }

  const activeTab = String(state.music.activeTab || '');
  const fallbackTab = state.music.ai === 'suno' && state.music.mode === 'lyrics' ? 'lyrics' : 'idea';

  if (tab === 'results' && activeTab === 'results') {
    state.music.activeTab = ['idea', 'lyrics', 'songwriter', 'tools'].includes(String(state.music.lastEditorTab || ''))
      ? state.music.lastEditorTab
      : fallbackTab;
    saveState();
    render();
    return;
  }

  if (tab !== 'results') state.music.lastEditorTab = tab;

  if (tab === 'idea') state.music.mode = 'idea';
  if (tab === 'lyrics' && state.music.ai === 'suno') state.music.mode = 'lyrics';
  if (tab === 'lyrics' && state.music.ai === 'udio') {
    state.music.mode = 'idea';
    toast('info', 'Udio работает через описание', 'Для Udio генерация всегда идёт из блока «Идея». Текст песни можно хранить как черновик.');
  }

  state.music.activeTab = tab;
  saveState();
  render();
}

function musicTrackPayload(track) {
  if (!track) return {};
  const raw = track.payload_json;
  if (!raw) return {};
  if (typeof raw === 'string') {
    try { return JSON.parse(raw); } catch (_) { return {}; }
  }
  return typeof raw === 'object' ? raw : {};
}

function musicTrackAudioId(track) {
  const payload = musicTrackPayload(track);
  return String(payload.audioId || payload.audio_id || payload.id || track.provider_track_id || '').trim();
}

function musicProviderTaskId() {
  const selected = musicSelectedItem();
  return String((selected && selected.provider_task_id) || state.music.toolTaskId || '').trim();
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
    : 'Сгенерировать музыку · бесплатно';
}

function musicToolIsRunning() {
  const status = String(state.music.toolTaskStatus || '').toLowerCase();
  return ['queued', 'processing', 'running', 'in_progress'].includes(status);
}

function musicInspectorRunConfig() {
  const isToolMode = state.music.ai === 'suno' && state.music.activeTab === 'tools';
  if (isToolMode) {
    const label = musicToolIsRunning()
      ? `${musicToolActionMeta().short}...`
      : musicToolActionMeta().short;
    return {
      action: 'music-run-tool',
      label,
      disabled: musicToolIsRunning(),
      loading: musicToolIsRunning(),
    };
  }
  return {
    action: 'run-music',
    label: musicRunButtonLabel(),
    disabled: !!state.music.isGenerating,
    loading: !!state.music.isGenerating,
  };
}

function musicRunHelperText() {
  if (state.music.ai === 'suno') {
    return 'Suno возвращает 2 трека за один запуск. На экране показаны только нужные параметры, а расширенные настройки спрятаны отдельно.';
  }
  return 'Udio запускается только из блока «Описание трека» и использует PiAPI. Настройки и инструменты Suno скрыты.';
}

function musicLiveStatusText() {
  const status = String(state.music.status || '').toLowerCase();
  const tracks = musicCurrentTracks();
  if (status === 'completed' && tracks.length) {
    return `Готово: получено ${tracks.length} ${musicTrackLabel(tracks.length)}.`;
  }
  if (status === 'failed') return 'Генерация завершилась ошибкой.';
  if (['queued', 'processing', 'running', 'in_progress'].includes(status) || state.music.isGenerating) {
    return state.music.ai === 'suno'
      ? 'Генерирую музыку. После завершения появятся 2 готовых трека.'
      : 'Генерирую музыку. После завершения результат появится ниже.';
  }
  return state.music.statusText || 'Выбери модель, заполни идею или текст песни и запусти генерацию.';
}

function musicToolStatusText() {
  const status = String(state.music.toolTaskStatus || 'idle').toLowerCase();
  if (status === 'completed') return `Инструмент завершён: получено ${state.music.toolTracks.length || 0} ${musicTrackLabel(state.music.toolTracks.length || 0)}.`;
  if (status === 'failed') return state.music.toolTaskMessage || 'Инструмент завершился ошибкой.';
  if (['queued', 'processing', 'running'].includes(status)) return state.music.toolTaskMessage || 'Задача отправлена в SunoAPI и сейчас обрабатывается.';
  return state.music.toolTaskMessage || 'Здесь будут появляться результаты продления, кавера и добавления вокала.';
}

function musicToolActionMeta(action = state.music.toolAction || 'upload-cover') {
  switch (String(action || 'upload-cover')) {
    case 'upload-cover':
      return {
        title: 'Кавер по файлу',
        short: 'Сделать кавер',
        description: 'Загрузи готовый аудиофайл и создай новую версию в логике Suno cover.',
      };
    case 'upload-extend':
      return {
        title: 'Продлить из файла',
        short: 'Продлить из файла',
        description: 'Загрузи аудио и продолжи его с выбранного таймкода.',
      };
    case 'add-vocals':
      return {
        title: 'Добавить вокал',
        short: 'Добавить вокал',
        description: 'Загрузи инструментал и попроси Suno добавить вокальную партию.',
      };
    default:
      return {
        title: 'Кавер по файлу',
        short: 'Сделать кавер',
        description: 'Загрузи готовый аудиофайл и создай новую версию в логике Suno cover.',
      };
  }
}

function bestMusicToolPrompt(source = 'lyrics') {
  if (source === 'idea') return String(state.music.ideaText || '').trim();
  return String(state.music.lyricsText || '').trim() || String(state.music.ideaText || '').trim();
}

function ensureMusicToolPromptForAction(action = state.music.toolAction || 'upload-cover') {
  if (!['upload-cover', 'add-vocals'].includes(String(action || ''))) return;
  if (String(state.music.toolPrompt || '').trim()) return;
  const preferred = String(action) === 'add-vocals' ? 'lyrics' : (state.music.mode === 'lyrics' ? 'lyrics' : 'idea');
  state.music.toolPromptMode = preferred;
  state.music.toolPrompt = bestMusicToolPrompt(preferred);
}

function musicToolPromptMeta(action = state.music.toolAction || 'upload-cover') {
  switch (String(action || 'upload-cover')) {
    case 'upload-cover':
      return {
        label: 'Текст или описание для нового кавера',
        placeholder: 'Напиши, какой должен быть новый кавер: новый текст песни, настроение, стиль, хук, структура, женский или мужской вокал.',
        hint: 'Сюда вводится именно то, что должно попасть в новый кавер. Можно написать новый текст вручную или подставить его из вкладки «Текст песни».',
      };
    case 'add-vocals':
      return {
        label: 'Текст для вокала',
        placeholder: 'Напиши слова песни, припев или короткое описание того, какой вокал нужно добавить поверх загруженного инструментала.',
        hint: 'Если хочешь получить вокал с конкретными словами, вставь их сюда. Если нужен просто стиль вокала, можно описать его словами.',
      };
    default:
      return { label: 'Текст', placeholder: '', hint: '' };
  }
}

function musicIsGeneratingNow() {
  const status = String(state.music.status || '').toLowerCase();
  return !!state.music.isGenerating || ['queued', 'processing', 'running', 'in_progress'].includes(status);
}

function musicWorkspacePhase(options = {}) {
  const toolMode = !!options.toolMode;
  if (toolMode) {
    const toolStatus = String(state.music.toolTaskStatus || '').toLowerCase();
    const toolTracks = Array.isArray(state.music.toolTracks) ? state.music.toolTracks : [];
    if (musicToolIsRunning()) return 'loading';
    if (toolTracks.length) return 'result';
    if (state.music.toolTaskMessage || ['failed', 'error', 'cancelled', 'canceled'].includes(toolStatus)) {
      if (['failed', 'error', 'cancelled', 'canceled'].includes(toolStatus)) return 'error';
    }
    return 'empty';
  }

  const status = String(state.music.status || '').toLowerCase();
  const tracks = musicCurrentTracks();
  if (musicIsGeneratingNow()) return 'loading';
  if (Array.isArray(tracks) && tracks.length) return 'result';
  if (state.music.errorText || ['failed', 'error', 'cancelled', 'canceled'].includes(status)) return 'error';
  return 'empty';
}

function renderMusicStageChips(items = []) {
  const chips = (Array.isArray(items) ? items : []).filter(Boolean);
  if (!chips.length) return '';
  return `<div class="music-stage-chip-row">${chips.map((item) => `<span class="music-stage-chip">${escapeHtml(item)}</span>`).join('')}</div>`;
}

function renderMusicWorkspaceStage(options = {}) {
  const {
    isSuno = true,
    currentModeLabel = 'Описание трека',
    currentTracks = [],
    expectedCount = 2,
    selected = null,
    toolMode = false,
  } = options;

  const phase = musicWorkspacePhase({ toolMode });
  const generationStamp = selected?.completed_at || selected?.created_at || state.music.lastCompletedAt || '';
  const toolTracks = Array.isArray(state.music.toolTracks) ? state.music.toolTracks : [];
  const stageTracks = toolMode ? toolTracks : currentTracks;
  const stageExpectedCount = toolMode ? 1 : expectedCount;
  const toolMeta = musicToolActionMeta();
  const commonChips = toolMode
    ? [
        'Suno',
        'Инструменты',
        toolMeta.title,
        state.music.toolTaskId ? `ID ${trimText(state.music.toolTaskId, 18)}` : '',
      ]
    : [
        isSuno ? 'Suno' : 'Udio',
        currentModeLabel,
        isSuno ? '2 трека' : '1 трек',
      ];

  if (phase === 'loading') {
    return `
      <div class="music-stage music-stage--loading">
        <div class="music-stage-visual music-stage-visual--loading" aria-hidden="true">
          <div class="music-stage-orbit"></div>
          <div class="music-stage-core">♪</div>
          <div class="music-stage-bars">
            <span></span><span></span><span></span><span></span><span></span><span></span><span></span>
          </div>
        </div>
        <div class="music-stage-copy">
          <span class="music-stage-kicker">${toolMode ? 'Инструмент' : 'Генерация'}</span>
          <h3>${escapeHtml(toolMode ? toolMeta.short : 'Генерация музыки')}</h3>
          <p>${escapeHtml(toolMode ? musicToolStatusText() : musicLiveStatusText())}</p>
          ${renderMusicStageChips(commonChips)}
          <div class="music-stage-steps">
            ${toolMode
              ? '<span class="done">Действие</span><span class="done">Файл</span><span class="active">Обработка</span><span>Результат</span>'
              : '<span class="done">Идея</span><span class="done">Параметры</span><span class="active">Генерация</span><span>Результат</span>'}
          </div>
          <div class="music-stage-note">${escapeHtml(toolMode ? 'После завершения результат инструмента появится прямо в этой верхней рабочей зоне.' : (isSuno ? 'Suno обычно возвращает сразу 2 варианта, они появятся здесь автоматически.' : 'Как только Udio закончит обработку, готовый трек появится здесь автоматически.'))}</div>
        </div>
      </div>
    `;
  }

  if (phase === 'error') {
    return `
      <div class="music-stage music-stage--error">
        <div class="music-stage-visual music-stage-visual--error" aria-hidden="true">
          <div class="music-stage-core music-stage-core--error">!</div>
        </div>
        <div class="music-stage-copy">
          <span class="music-stage-kicker">Ошибка</span>
          <h3>${escapeHtml(toolMode ? 'Ошибка инструмента' : 'Ошибка генерации')}</h3>
          <p>${escapeHtml(toolMode ? (state.music.toolTaskMessage || 'Во время обработки аудио произошла ошибка. Проверь файл и параметры, затем запусти ещё раз.') : (state.music.errorText || 'Во время генерации произошла ошибка. Проверь идею, текст и параметры, затем запусти ещё раз.'))}</p>
          ${renderMusicStageChips(commonChips)}
        </div>
      </div>
    `;
  }

  if (phase === 'result') {
    const headline = toolMode
      ? `${toolMeta.short} готово`
      : (stageTracks.length >= stageExpectedCount ? 'Музыка готова' : 'Результат обновляется');
    return `
      <div class="music-stage music-stage--result">
        <div class="music-stage-result-head">
          <div class="music-stage-copy">
            <span class="music-stage-kicker">Результат</span>
            <h3>${escapeHtml(headline)}</h3>
          </div>
        </div>
        <div class="music-stage-track-grid">
          ${renderMusicTrackCards(stageTracks, {
            emptyText: toolMode
              ? 'После запуска результат инструмента появится здесь.'
              : (isSuno ? 'После запуска здесь появятся 2 готовых трека Suno.' : 'После запуска здесь появится 1 готовый трек Udio.'),
            taskId: toolMode ? (state.music.toolTaskId || '') : musicProviderTaskId(),
            cardClass: 'music-track-card--stage',
            hideLyrics: !isSuno
          })}
        </div>
      </div>
    `;
  }

  return `
    <div class="music-stage music-stage--empty">
      <div class="music-stage-visual music-stage-visual--empty" aria-hidden="true">
        <div class="music-stage-disc"></div>
        <div class="music-stage-core">♪</div>
        <div class="music-stage-bars">
          <span></span><span></span><span></span><span></span><span></span>
        </div>
      </div>
      <div class="music-stage-copy">
        <span class="music-stage-kicker">Рабочая зона</span>
        <h3></h3>
        <p>${escapeHtml(toolMode ? 'Выбери действие справа, загрузи аудио и запусти инструмент. Результат кавера, продления или добавления вокала появится здесь.' : 'Нажми «Создать музыку» справа. Сначала здесь появится анимация генерации, а затем готовые треки с плеерами.')}</p>
        ${renderMusicStageChips(commonChips)}
      </div>
    </div>
  `;
}

function renderMusicTrackCards(tracks = [], options = {}) {
  const emptyText = options.emptyText || 'После первой успешной генерации здесь появятся карточки треков.';
  const taskId = options.taskId || musicProviderTaskId();
  const cardClass = ['music-track-card', options.cardClass].filter(Boolean).join(' ');
  const hideLyrics = !!options.hideLyrics;
  if (!Array.isArray(tracks) || !tracks.length) {
    return `<div class="music-empty-card">${escapeHtml(emptyText)}</div>`;
  }
  return tracks.map((track, index) => {
    const title = track.title || `Трек ${index + 1}`;
    const audioUrl = track.audio_url || track.download_url || '';
    const videoUrl = track.video_url || '';
    const lyrics = String(track.lyrics || '').trim();
    const audioId = musicTrackAudioId(track);
    const timed = audioId ? state.music.timestampedLyrics[audioId] : null;
    return `
      <div class="${cardClass}">
        <div class="music-track-top">
          <div>
            <strong>${escapeHtml(title)}</strong>
          </div>
        </div>
        ${audioUrl ? `<audio controls preload="none" src="${escapeHtml(audioUrl)}"></audio>` : `<div class="help-text">У этого результата пока нет audio_url.</div>`}
        ${(!hideLyrics && lyrics) ? `<div class="music-lyrics-snippet"><strong>Текст песни</strong><pre>${escapeHtml(trimText(lyrics, 900))}</pre></div>` : ''}
        ${(!hideLyrics && timed && Array.isArray(timed.alignedWords) && timed.alignedWords.length) ? `<div class="music-lyrics-snippet"><strong>Таймкоды текста</strong><small>${escapeHtml(timed.alignedWords.slice(0, 10).map((w) => `${Number(w.startS || 0).toFixed(1)}s ${w.word || ''}`).join(' · '))}</small></div>` : ''}
        <div class="actions compact-gap" style="margin-top:10px; flex-wrap:wrap;">
          ${audioUrl ? `<a class="btn ghost small" href="${escapeHtml(audioUrl)}" target="_blank" rel="noopener">Открыть MP3</a>` : ''}
          ${videoUrl ? `<a class="btn ghost small" href="${escapeHtml(videoUrl)}" target="_blank" rel="noopener">Открыть MP4</a>` : ''}
        </div>
      </div>
    `;
  }).join('');
}


function renderMusicPrimarySettingsPanel({ isSuno, compact = false } = {}) {
  return `
    <div class="inspector-card music-inspector-card music-primary-settings ${compact ? 'music-primary-settings--inspector' : ''}">
      <div class="field-head">
        <div>
          <h4>Основные параметры</h4>
          <div class="help-text"></div>
        </div>
        <span class="badge muted">${escapeHtml(isSuno ? 'SunoAPI' : 'PiAPI / Udio')}</span>
      </div>

      <div class="music-form-grid ${compact ? 'music-form-grid--inspector' : 'music-form-grid--compact'} music-form-grid--main-fields">
        <label>Название трека
          <input id="music_title" value="${escapeHtml(state.music.title)}" placeholder="Например: Музыка будущего">
        </label>
        <label>Язык
          <select id="music_language">
            <option value="ru" ${state.music.language === 'ru' ? 'selected' : ''}>Русский</option>
            <option value="en" ${state.music.language === 'en' ? 'selected' : ''}>English</option>
            <option value="auto" ${state.music.language === 'auto' ? 'selected' : ''}>Авто</option>
          </select>
        </label>
      </div>

      <div class="music-form-stack">
        <label>Стиль / жанр
          <textarea id="music_tags" class="music-small-textarea" rows="3" placeholder="dance-pop, cinematic, commercial, female vocal">${escapeHtml(state.music.tags)}</textarea>
        </label>
        <label>Настроение
          <textarea id="music_mood" class="music-small-textarea" rows="3" placeholder="энергично, вдохновляюще, премиально, тепло">${escapeHtml(state.music.mood)}</textarea>
        </label>
        <label>Детали трека
          <textarea id="music_references" class="music-track-details-textarea" rows="${compact ? 5 : 4}" placeholder="Например: реклама школы танцев, короткий яркий припев, современный коммерческий звук, ощущение роста и премиального вайба">${escapeHtml(state.music.references)}</textarea>
        </label>
      </div>
    </div>
  `;
}


function renderMusicAdvancedSettingsPanel({ advancedSummary = '', centered = false } = {}) {
  const wrapperClass = centered
    ? 'music-advanced-card music-advanced-card--center'
    : 'inspector-card music-inspector-card music-advanced-card';
  const gridClass = centered
    ? 'music-form-grid music-form-grid--advanced'
    : 'music-form-grid music-form-grid--advanced music-inspector-advanced-grid';

  return `
    <div class="${wrapperClass} ${state.music.showAdvancedPanel ? 'open' : ''}">
      <div class="music-advanced-head">
        <div>
          <strong>Дополнительные настройки Suno</strong>
          <small>${escapeHtml(advancedSummary)}</small>
        </div>
        <button class="btn ghost small" data-action="music-toggle-advanced">${state.music.showAdvancedPanel ? 'Скрыть' : 'Показать'}</button>
      </div>

      ${state.music.showAdvancedPanel ? `
        <div class="${gridClass}">
          <label>Модель Suno
            <select id="music_model">
              <option value="V4_5" ${state.music.model === 'V4_5' ? 'selected' : ''}>V4_5</option>
              <option value="V5" ${state.music.model === 'V5' ? 'selected' : ''}>V5</option>
            </select>
          </label>
          <label>Тип вокала
            <select id="music_vocalGender" ${state.music.instrumental ? 'disabled' : ''}>
              <option value="" ${!state.music.vocalGender ? 'selected' : ''}>Авто</option>
              <option value="f" ${state.music.vocalGender === 'f' ? 'selected' : ''}>Женский</option>
              <option value="m" ${state.music.vocalGender === 'm' ? 'selected' : ''}>Мужской</option>
            </select>
          </label>
          <div class="music-segment-field">
            <span class="music-field-label">Режим трека</span>
            <div class="music-segmented" role="group" aria-label="Режим трека">
              <button class="music-segment ${!state.music.instrumental ? 'active' : ''}" type="button" data-action="music-set-vocal-mode" data-value="vocal">Вокал</button>
              <button class="music-segment ${state.music.instrumental ? 'active' : ''}" type="button" data-action="music-set-vocal-mode" data-value="instrumental">Инстр</button>
            </div>
          </div>
          <label class="music-span-2">Что исключить из звучания
            <input id="music_negativeTags" value="${escapeHtml(state.music.negativeTags)}" placeholder="aggressive drums, heavy metal, lo-fi noise">
          </label>
        </div>

        ${state.music.personaId ? `
          <div class="music-mini-card music-persona-note">
            <div>
              <strong>Активна persona</strong>
              <small>${escapeHtml(state.music.personaResult?.name || 'Сохранённый стиль/голос')} · ${escapeHtml(state.music.personaId)}</small>
            </div>
            <button class="btn ghost small" data-action="music-clear-persona">Отключить</button>
          </div>
        ` : ''}

        <div class="music-slider-grid">
          <label class="music-slider-row">
            <div class="music-slider-top"><span>Сила стиля</span><strong id="music_styleWeight_value">${Number(state.music.styleWeight ?? 0.65).toFixed(2)}</strong></div>
            <input id="music_styleWeight" type="range" min="0" max="1" step="0.01" value="${Number(state.music.styleWeight ?? 0.65)}">
          </label>
          <label class="music-slider-row">
            <div class="music-slider-top"><span>Креативность</span><strong id="music_weirdnessConstraint_value">${Number(state.music.weirdnessConstraint ?? 0.65).toFixed(2)}</strong></div>
            <input id="music_weirdnessConstraint" type="range" min="0" max="1" step="0.01" value="${Number(state.music.weirdnessConstraint ?? 0.65)}">
          </label>
          <label class="music-slider-row">
            <div class="music-slider-top"><span>Влияние аудио</span><strong id="music_audioWeight_value">${Number(state.music.audioWeight ?? 0.65).toFixed(2)}</strong></div>
            <input id="music_audioWeight" type="range" min="0" max="1" step="0.01" value="${Number(state.music.audioWeight ?? 0.65)}">
          </label>
        </div>
      ` : ''}
    </div>
  `;
}

function renderMusicWorkspace() {
  ensureMusicCompatibility({ preserveLyricsTab: false });
  const isSuno = state.music.ai === 'suno';
  const allowedTabs = isSuno
    ? ['idea', 'lyrics', 'songwriter', 'tools', 'results']
    : ['idea', 'results'];
  const fallbackTab = isSuno && state.music.mode === 'lyrics' ? 'lyrics' : 'idea';
  const activeTab = allowedTabs.includes(String(state.music.activeTab || ''))
    ? String(state.music.activeTab || '')
    : fallbackTab;
  if (state.music.activeTab !== activeTab) state.music.activeTab = activeTab;
  const editorAuxTabs = isSuno ? ['songwriter'] : [];

  const selected = musicSelectedItem();
  const selectedTracks = selected && Array.isArray(selected.tracks) ? selected.tracks : [];
  const historyItems = Array.isArray(state.musicHistory.items) ? state.musicHistory.items : [];
  const currentTracks = musicCurrentTracks();
  const expectedCount = musicExpectedTrackCount();
  const statusTone = state.music.status === 'completed' ? 'ok' : state.music.status === 'failed' ? 'warn' : 'muted';
  const toolStatusTone = state.music.toolTaskStatus === 'completed' ? 'ok' : state.music.toolTaskStatus === 'failed' ? 'warn' : 'muted';
  const songwriterSeedText = 'Давай быстро соберём вводные: 1) жанр/стиль 2) настроение 3) язык 4) о чём песня 5) нужен ли припев с хук-фразой.';
  const songwriterMessages = Array.isArray(state.music.songwriter.messages)
    ? state.music.songwriter.messages.filter((msg) => String(msg?.content || '').trim() !== songwriterSeedText)
    : [];
  const songThread = songwriterMessages.length ? songwriterMessages.map((msg) => `
    <div class="music-chat-bubble ${msg.role === 'user' ? 'user' : 'assistant'}">
      <div>${escapeHtml(msg.content || '')}</div>
    </div>
  `).join('') : '';

  const currentModeLabel = activeTab === 'tools'
    ? 'Инструменты Suno'
    : (isSuno && state.music.mode === 'lyrics' ? 'Текст песни' : 'Описание трека');
  const workzoneMeta = activeTab === 'tools'
    ? `${isSuno ? 'Suno' : 'Udio'} · ${currentModeLabel} · ${musicToolActionMeta().title}`
    : `${isSuno ? 'Suno' : 'Udio'} · ${currentModeLabel} · ${isSuno ? '2 трека' : '1 трек'}`;
  const editorTitle = activeTab === 'songwriter'
    ? 'Генератор текста песни'
    : activeTab === 'tools'
      ? 'Инструменты Suno'
      : 'Создание песни';
  const vocalLabel = state.music.instrumental
    ? 'Инструментал'
    : state.music.vocalGender === 'f'
      ? 'Женский вокал'
      : state.music.vocalGender === 'm'
        ? 'Мужской вокал'
        : 'Вокал авто';
  const advancedSummaryParts = [
    `Модель: ${state.music.model || 'V4_5'}`,
    vocalLabel,
  ];
  if (state.music.personaId) advancedSummaryParts.push('persona подключена');
  const advancedSummary = advancedSummaryParts.join(' · ');
  const showCenteredAdvanced = isSuno && ['idea', 'lyrics'].includes(activeTab);

  return `
    <div class="workspace-grid single music-workspace-grid">
      <div class="workspace-main scroll music-workspace-main">
        <div class="music-clean-shell music-layout-shell">
          <div class="music-panel music-workzone-card music-workzone-card--clean">
            ${renderMusicWorkspaceStage({
              isSuno,
              currentModeLabel,
              currentTracks,
              expectedCount,
              selected,
              toolMode: activeTab === 'tools' && isSuno,
            })}
          </div>

          <div class="music-panel music-editor-card music-editor-card--stage">


            ${activeTab === 'idea' ? `
              <div class="music-editor-shell ${showCenteredAdvanced ? 'music-editor-shell--with-advanced' : ''}">
                <div class="field-head">
                  <div>
                    <h4>${isSuno ? 'Описание трека' : 'Описание трека для Udio'}</h4>
                    <div class="help-text">${isSuno ? 'Опиши звучание, вокал, динамику и общий вайб будущего трека.' : 'Это главный текст запуска для Udio. Чем точнее описание, тем лучше результат.'}</div>
                  </div>
                  <div class="actions compact-gap music-helper-actions">
                    <button class="btn ghost small" data-action="music-fill-template">Шаблон</button>
                  </div>
                </div>
                <textarea id="music_ideaText" rows="15" placeholder="Например: современный вдохновляющий трек для рекламы школы танцев, яркий коммерческий припев, уверенный рост энергии, премиальный urban vibe, чистый современный вокал">${escapeHtml(state.music.ideaText)}</textarea>
              </div>
            ` : ''}

            ${activeTab === 'lyrics' && isSuno ? `
              <div class="music-editor-shell ${showCenteredAdvanced ? 'music-editor-shell--with-advanced' : ''}">
                <div class="field-head">
                  <div>
                    <h4>Готовый текст песни</h4>
                    <div class="help-text">Suno запустит генерацию по этому тексту. Удобно держать структуру в формате [Verse], [Chorus], [Bridge].</div>
                  </div>
                </div>
                <div class="actions compact-gap" style="margin:0 0 12px; flex-wrap:wrap;">
                  <button class="btn ghost small active" data-action="music-open-editor">Редактор</button>
                  <button class="btn ghost small" data-action="music-open-songwriter">Генератор текста</button>
                </div>
                <textarea id="music_lyricsText" rows="18" placeholder="[Verse]
...

[Chorus]
...

[Bridge]
...">${escapeHtml(state.music.lyricsText)}</textarea>
              </div>
            ` : ''}

            ${showCenteredAdvanced ? renderMusicAdvancedSettingsPanel({ advancedSummary, centered: true }) : ''}

            ${activeTab === 'songwriter' ? `
              <div class="music-editor-shell">
                <div class="field-head">
                  <div>
                    <h4>Генератор текста песни</h4>
                    <div class="help-text">Собери куплеты, припев, правки и несколько вариантов текста прямо внутри Music Studio.</div>
                  </div>
                </div>
                <div class="actions compact-gap" style="margin:0 0 12px; flex-wrap:wrap;">
                  <button class="btn ghost small" data-action="music-open-editor">Редактор</button>
                  <button class="btn ghost small active" data-action="music-open-songwriter">Генератор текста</button>
                </div>
                <div class="music-chat-thread">${songThread}</div>
                <div class="music-chat-composer">
                  <textarea id="music_songwriterInput" rows="3" placeholder="Напиши задачу, например: сделай цепкий русский припев для рекламы школы танцев, 2 куплета и припев">${escapeHtml(state.music.songwriter.input || '')}</textarea>
                  <div class="actions compact-gap" style="flex-wrap:wrap;">
                    <button class="btn primary ${state.music.songwriter.loading ? 'loading' : ''}" data-action="songwriter-send" ${state.music.songwriter.loading ? 'disabled' : ''}>${state.music.songwriter.loading ? 'Генерация...' : 'Сгенерировать текст'}</button>
                  </div>
                </div>
              </div>
            ` : ''}

            ${activeTab === 'tools' && isSuno ? `
              <div class="music-panel music-tools-panel music-tools-panel--embedded">
                <div class="field-head">
                  <div>
                    <h4>Инструменты для готового аудио</h4>
                    <div class="help-text">Выбери действие, затем заполни только нужные поля ниже. Лишние технические поля убраны.</div>
                  </div>
                  <span class="badge ${toolStatusTone}">${escapeHtml(state.music.toolTaskStatus || 'idle')}</span>
                </div>

                <div class="music-tool-action-grid">
                  <button class="music-tool-card ${state.music.toolAction === 'upload-cover' ? 'active' : ''}" data-action="music-set-tool-action" data-value="upload-cover">
                    <strong>Кавер по файлу</strong>
                    <small>Загрузи аудио и введи текст или описание для новой версии</small>
                  </button>
                  <button class="music-tool-card ${state.music.toolAction === 'upload-extend' ? 'active' : ''}" data-action="music-set-tool-action" data-value="upload-extend">
                    <strong>Продлить из файла</strong>
                    <small>Загрузи аудио и укажи, с какой секунды продолжать</small>
                  </button>
                  <button class="music-tool-card ${state.music.toolAction === 'add-vocals' ? 'active' : ''}" data-action="music-set-tool-action" data-value="add-vocals">
                    <strong>Добавить вокал</strong>
                    <small>Загрузи инструментал и впиши слова или описание нужного вокала</small>
                  </button>
                </div>


                <input id="music_sourceAudio" type="file" accept="audio/*,.mp3,.wav,.m4a,.aac,.flac" style="display:none">
                <div class="music-tool-step-card">
                  <div class="music-tool-step-index">1</div>
                  <div class="music-tool-step-body">
                    <strong>Загрузи аудиофайл</strong>
                    <small>Подходит MP3, WAV, M4A, AAC и FLAC.</small>
                    <div class="music-upload-box music-upload-box--tool">
                      <div>
                        <strong>Файл для загрузки</strong>
                        <small>${escapeHtml(state.music.uploadFileName || 'Файл ещё не выбран')}</small>
                      </div>
                      <button class="btn ghost" data-action="music-pick-source-audio">Выбрать аудио</button>
                    </div>
                  </div>
                </div>

                ${['upload-cover', 'add-vocals'].includes(state.music.toolAction) ? `
                  <div class="music-tool-step-card">
                    <div class="music-tool-step-index">2</div>
                    <div class="music-tool-step-body">
                      <strong>${escapeHtml(musicToolPromptMeta().label)}</strong>
                      <small>${escapeHtml(musicToolPromptMeta().hint)}</small>
                      <textarea id="music_toolPrompt" rows="8" placeholder="${escapeHtml(musicToolPromptMeta().placeholder)}">${escapeHtml(state.music.toolPrompt || '')}</textarea>
                    </div>
                  </div>
                ` : ''}

                ${state.music.toolAction === 'upload-extend' ? `
                  <div class="music-tool-step-card">
                    <div class="music-tool-step-index">2</div>
                    <div class="music-tool-step-body">
                      <strong>Укажи точку продолжения</strong>
                      <small>С какой секунды начать продолжение загруженного файла.</small>
                      <div class="music-form-grid music-tool-form-grid">
                        <label>Продолжить с секунды
                          <input id="music_continueAt" type="number" min="0" step="0.1" value="${Number(state.music.continueAt ?? 60)}">
                        </label>
                      </div>
                    </div>
                  </div>
                ` : ''}

                <div class="music-live-status music-live-status--tool">
                  <strong>${escapeHtml(musicToolStatusText())}</strong>
                  <small>${state.music.toolTaskId ? `ID задачи ${escapeHtml(state.music.toolTaskId)} · результат появится в верхней рабочей зоне.` : 'После запуска статус обновится здесь, а результат появится в верхней рабочей зоне.'}</small>
                </div>

                <div class="actions compact-gap music-tool-actions-row">
                  <button class="btn ghost" data-action="music-refresh-tool-status" ${!state.music.toolTaskId ? 'disabled' : ''}>Обновить статус</button>
                </div>

                ${state.music.personaResult ? `<div class="music-mini-card"><strong>Последняя persona</strong><small>${escapeHtml(state.music.personaResult.name || '')} · ${escapeHtml(state.music.personaResult.personaId || '')}</small></div>` : ''}
              </div>
            ` : ''}
          </div>

        </div>
      </div>
    </div>
  `;
}

function renderMusicInspector() {
  ensureMusicCompatibility({ preserveLyricsTab: true });
  const isSuno = state.music.ai === 'suno';
  const vocalLabel = state.music.instrumental
    ? 'Инструментал'
    : state.music.vocalGender === 'f'
      ? 'Женский вокал'
      : state.music.vocalGender === 'm'
        ? 'Мужской вокал'
        : 'Вокал авто';
  const advancedSummaryParts = [
    `Модель: ${state.music.model || 'V4_5'}`,
    vocalLabel,
  ];
  if (state.music.personaId) advancedSummaryParts.push('persona подключена');
  const advancedSummary = advancedSummaryParts.join(' · ');
  const modeLabel = isSuno
    ? (state.music.mode === 'lyrics' ? 'Текст песни' : 'Описание трека')
    : 'Описание трека';
  const runStatusTone = state.music.status === 'completed' ? 'ok' : state.music.status === 'failed' ? 'warn' : 'muted';
  const historyItems = Array.isArray(state.musicHistory.items) ? state.musicHistory.items : [];
  const selected = musicSelectedItem();

  return `
    <div class="music-inspector-shell">
      <div class="inspector-card music-inspector-card">
        <div class="field-head">
          <div>
            <h4>Модель</h4>
            <div class="help-text"></div>
          </div>
          <span class="badge muted">${escapeHtml(isSuno ? 'SunoAPI' : 'PiAPI')}</span>
        </div>

        <div class="music-ai-switch music-provider-switch music-inspector-provider">
          <button class="music-ai-pill ${isSuno ? 'active' : ''}" data-action="music-set-ai" data-value="suno" type="button">
            <span class="music-ai-title">Suno</span>
            <small></small>
          </button>
          <button class="music-ai-pill ${!isSuno ? 'active' : ''}" data-action="music-set-ai" data-value="udio" type="button">
            <span class="music-ai-title">Udio</span>
            <small></small>
          </button>
        </div>

        ${isSuno ? `
          <div class="music-segment-field">
            <span class="music-field-label">Режим Music Studio</span>
            <div class="music-segmented" role="group" aria-label="Режим Music Studio">
              <button class="music-segment ${state.music.activeTab === 'idea' ? 'active' : ''}" type="button" data-action="music-set-tab" data-tab="idea">Идея</button>
              <button class="music-segment ${state.music.activeTab === 'lyrics' ? 'active' : ''}" type="button" data-action="music-set-tab" data-tab="lyrics">Текст</button>
            </div>
          </div>
        ` : `
          <div class="music-mini-card music-compact-note">
            <strong>Udio работает через описание</strong>
            <small>Для Udio активен один понятный сценарий: генерация из описания трека.</small>
          </div>
        `}

      ${renderMusicPrimarySettingsPanel({ isSuno, compact: true })}

      <div class="inspector-card music-inspector-card">
        <div class="field-head">
          <div>
            <h4>История</h4>
            <div class="help-text"></div>
          </div>
        </div>
        <div class="music-inspector-shortcuts">
          <button class="btn ghost ${state.music.activeTab === 'results' ? 'active' : ''}" data-action="music-set-tab" data-tab="results">История и результаты</button>
        </div>
      </div>

      ${state.music.activeTab === 'results' ? `
        <div class="inspector-card music-inspector-card music-history-side-card">
          <div class="field-head">
            <div>
              <h4>История музыки</h4>
              <div class="help-text">Открывай старые запуски прямо из правого бара.</div>
            </div>
            <span class="badge muted">${historyItems.length}</span>
          </div>

          <div class="actions compact-gap" style="flex-wrap:wrap;">
            <button class="btn ghost small" data-action="refresh-music-history">Обновить</button>
            <button class="btn outline small" data-action="music-open-history">Открыть последний</button>
          </div>

          <div class="music-history-list music-history-side-list">
            ${state.musicHistory.loading ? `<div class="music-empty-card">Загружаю историю музыки...</div>` : ''}
            ${!state.musicHistory.loading && historyItems.length ? historyItems.map((item) => `
              <div class="music-history-item ${state.musicHistory.selectedId === item.id ? 'active' : ''}">
                <div class="music-history-head">
                  <strong>${escapeHtml(item.title || (item.ai === 'udio' ? 'Udio запуск' : 'Suno запуск'))}</strong>
                  <span class="badge ${item.status === 'completed' ? 'ok' : item.status === 'failed' ? 'warn' : 'muted'}">${escapeHtml(item.status || 'queued')}</span>
                </div>
                <small>${escapeHtml(trimText(item.mode === 'lyrics' ? item.lyrics_text : item.idea_text, 92) || 'Без текста')}</small>
                <div class="help-text">${escapeHtml(formatDate(item.completed_at || item.created_at || ''))}</div>
                <div class="actions compact-gap" style="margin-top:10px; flex-wrap:wrap;">
                  <button class="btn ghost small" data-action="use-music-history-item" data-generation-id="${escapeHtml(item.id || '')}">Открыть</button>
                  <button class="btn ghost small danger" data-action="delete-music-history-item" data-generation-id="${escapeHtml(item.id || '')}">Удалить</button>
                </div>
              </div>
            `).join('') : ''}
            ${!state.musicHistory.loading && !historyItems.length ? `<div class="music-empty-card">История пока пуста. После первой генерации запуски появятся здесь.</div>` : ''}
          </div>

          <div class="music-mini-card">
            <strong>${selected ? 'Выбранный запуск загружен' : 'Ничего не выбрано'}</strong>
            <small>${selected
              ? `AI: ${escapeHtml(selected.ai || '—')} · режим: ${escapeHtml(selected.mode || '—')} · ${escapeHtml(formatDate(selected.completed_at || selected.created_at || ''))}`
              : 'Выбери запуск из списка выше — он откроется в рабочей зоне.'}</small>
          </div>

          ${state.musicHistory.lastError ? `<div class="help-text">${escapeHtml(state.musicHistory.lastError)}</div>` : ''}
        </div>
      ` : ''}

      <div class="inspector-card music-inspector-card music-inspector-run">
        ${state.music.errorText ? `<div class="music-warning">${escapeHtml(state.music.errorText)}</div>` : ''}
        ${isSuno && state.music.activeTab === 'tools' ? `<div class="help-text" style="margin-bottom:10px;">Сейчас активен режим инструментов Suno. Нижняя кнопка запускает выбранное действие для готового аудио.</div>` : ''}
        <button class="btn primary music-run-btn ${musicInspectorRunConfig().loading ? 'loading' : ''}" data-action="${escapeHtml(musicInspectorRunConfig().action)}" ${musicInspectorRunConfig().disabled ? 'disabled' : ''}>${escapeHtml(musicInspectorRunConfig().label)}</button>
      </div>
    </div>
  `;
}

function promptLibraryMediaUrl(entity, fallback = '') {
  if (!entity) return fallback || '';
  return entity.preview_url || entity.cover_url || entity.image_url || entity.thumb_url || entity.thumbnail_url || entity.poster_url || fallback || '';
}

function promptLibraryVideoUrl(entity, fallback = '') {
  if (!entity) return fallback || '';
  return entity.video_url || entity.provider_video_url || entity.download_url || entity.signed_url || fallback || '';
}

function promptSelectedGroup() {
  return state.prompts.groups.find((group) => String(group.id) === String(state.prompts.selectedGroupId)) || null;
}

function shouldAutoOpenSinglePromptGroup(category = state.prompts.selectedCategory, groups = state.prompts.groups) {
  return String(category || '').toLowerCase() === 'video' && Array.isArray(groups) && groups.length === 1;
}

function promptOpenItem() {
  return state.prompts.items.find((item) => String(item.id) === String(state.prompts.openItemId)) || null;
}

function renderPromptItemModal() {
  let root = document.getElementById('promptLibraryModalRoot');
  if (!root) {
    root = document.createElement('div');
    root.id = 'promptLibraryModalRoot';
    document.body.appendChild(root);
  }

  const openItem = state.view === 'workspace' && state.studio === 'library' ? promptOpenItem() : null;
  const open = !!openItem;
  document.body.classList.toggle('prompt-library-modal-open', open);
  if (!open) {
    root.innerHTML = '';
    return;
  }

  const selectedGroup = promptSelectedGroup();
  const openItemPreview = promptLibraryMediaUrl(openItem, promptLibraryMediaUrl(selectedGroup));
  const openItemVideoUrl = promptLibraryVideoUrl(openItem);
  const useVideoPreview = String(state.prompts.selectedCategory || '').toLowerCase() === 'video' && !!openItemVideoUrl;
  const mediaMarkup = useVideoPreview
    ? `<div style="margin-bottom:14px; border-radius:22px; overflow:hidden; border:1px solid rgba(255,255,255,0.08); background:rgba(6,10,20,0.92); padding:10px;"><video src="${escapeHtml(openItemVideoUrl)}" ${openItemPreview ? `poster="${escapeHtml(openItemPreview)}"` : ''} controls playsinline preload="metadata" style="width:100%; max-height:360px; object-fit:contain; object-position:center top; display:block; border-radius:16px; background:#050811;"></video></div>`
    : (openItemPreview ? `<div style="margin-bottom:14px; border-radius:22px; overflow:hidden; border:1px solid rgba(255,255,255,0.08); background:rgba(6,10,20,0.92); padding:10px;"><img src="${escapeHtml(openItemPreview)}" alt="${escapeHtml(openItem.title || 'Карточка')}" style="width:100%; max-height:360px; object-fit:contain; object-position:center top; display:block; border-radius:16px;"></div>` : '');
  root.innerHTML = `
    <div class="auth-modal-backdrop" id="promptLibraryModalBackdrop" style="z-index:1200; padding:56px 20px 20px;">
      <div class="prompt-library-modal-card" role="dialog" aria-modal="true" aria-label="Карточка промпта" style="position:relative; width:min(100%, 620px); max-height:calc(100dvh - 76px); overflow-y:auto; overscroll-behavior:contain; -webkit-overflow-scrolling:touch; border-radius:28px; padding:18px; border:1px solid rgba(255,255,255,0.1); background:linear-gradient(180deg, rgba(7,11,28,0.985), rgba(8,12,24,0.985)); box-shadow:0 32px 80px rgba(0,0,0,0.45);">
        <div class="field-head" style="margin-bottom:14px; align-items:flex-start; gap:12px;">
          <div style="min-width:0;">
            <div style="font-size:22px; font-weight:800; line-height:1.2;">${escapeHtml(openItem.title || 'Карточка')}</div>
            <div style="margin-top:6px; color:rgba(255,255,255,0.62); font-size:13px;">${escapeHtml(openItem.model_hint || 'Без model hint')}</div>
          </div>
          <button class="btn ghost small" data-action="close-prompt-item">Закрыть</button>
        </div>
        ${mediaMarkup}
        <div style="border:1px solid rgba(255,255,255,0.08); background:rgba(255,255,255,0.03); border-radius:20px; padding:16px; color:rgba(255,255,255,0.92); font-size:14px; line-height:1.65; white-space:pre-wrap;">${escapeHtml(openItem.prompt_text || '')}</div>
        <div style="display:flex; gap:10px; flex-wrap:wrap; margin-top:14px;">
          <button class="btn primary" data-action="copy-prompt" data-prompt="${encodeURIComponent(openItem.prompt_text || '')}">Скопировать</button>
          <button class="btn outline" data-action="send-prompt-to-chat" data-prompt="${encodeURIComponent(openItem.prompt_text || '')}">В ChatGPT</button>
          <button class="btn ghost" data-action="close-prompt-item">Закрыть</button>
        </div>
      </div>
    </div>
  `;

  requestAnimationFrame(() => {
    const modalCard = root.querySelector('.prompt-library-modal-card');
    if (modalCard) modalCard.scrollTop = 0;
  });
}

function renderLibraryWorkspace() {
  const selectedGroup = promptSelectedGroup();
  const selectedCategoryTitle = state.prompts.categories.find((c) => c.slug === state.prompts.selectedCategory)?.title || state.prompts.selectedCategory || '';
  const autoOpenSingleGroup = shouldAutoOpenSinglePromptGroup();

  const categories = state.prompts.categories.map((c) => `
    <button
      class="chip ${state.prompts.selectedCategory === c.slug ? 'active' : ''}"
      data-action="select-category"
      data-category="${escapeHtml(c.slug)}"
      style="padding:8px 12px; min-height:36px; font-size:13px; border-radius:999px;"
    >${escapeHtml(c.title || c.slug)}</button>
  `).join('');

  const groups = state.prompts.groups.map((g) => {
    const coverUrl = promptLibraryMediaUrl(g);
    const isActive = String(state.prompts.selectedGroupId) === String(g.id);
    return `
      <article
        class="prompt-group-card ${isActive ? 'active' : ''}"
        style="border:1px solid ${isActive ? 'rgba(255,177,66,0.38)' : 'rgba(255,255,255,0.08)'}; background:linear-gradient(180deg, rgba(255,255,255,0.05), rgba(255,255,255,0.025)); border-radius:24px; padding:14px; display:flex; flex-direction:column; gap:10px; box-shadow:${isActive ? '0 16px 40px rgba(255,177,66,0.12)' : 'none'};"
      >
        <button data-action="select-group" data-group-id="${escapeHtml(g.id)}" style="all:unset; cursor:pointer; display:block;">
          ${coverUrl ? `<div style="width:100%; aspect-ratio: 1.28 / 1; border-radius:18px; overflow:hidden; border:1px solid rgba(255,255,255,0.07); background:rgba(255,255,255,0.03);"><img src="${escapeHtml(coverUrl)}" alt="${escapeHtml(g.title)}" style="width:100%; height:100%; object-fit:cover; display:block;"></div>` : `<div style="width:100%; aspect-ratio: 1.28 / 1; border-radius:18px; border:1px solid rgba(255,255,255,0.07); background:rgba(255,255,255,0.03); display:grid; place-items:center; color:rgba(255,255,255,0.42); font-size:12px;">Нет cover</div>`}
          <div style="margin-top:10px;">
            <div style="font-size:15px; font-weight:700; line-height:1.3;">${escapeHtml(g.title || 'Группа')}</div>
            <div style="margin-top:6px; color:rgba(255,255,255,0.64); font-size:12px;">${isActive ? 'Открыта подборка' : 'Нажми, чтобы открыть карточки'}</div>
          </div>
        </button>
      </article>
    `;
  }).join('');

  const items = state.prompts.items.map((item) => {
    const previewUrl = promptLibraryMediaUrl(item, promptLibraryMediaUrl(selectedGroup));
    const previewText = (item.prompt_text || '').trim();
    return `
      <article style="border:1px solid rgba(255,255,255,0.08); background:linear-gradient(180deg, rgba(255,255,255,0.045), rgba(255,255,255,0.02)); border-radius:22px; padding:12px; display:flex; flex-direction:column; gap:10px;">
        <button data-action="open-prompt-item" data-item-id="${escapeHtml(item.id)}" style="all:unset; cursor:pointer; display:block;">
          ${previewUrl ? `<div style="width:100%; aspect-ratio: 1 / 1; border-radius:18px; overflow:hidden; border:1px solid rgba(255,255,255,0.07); background:rgba(6,10,20,0.92); padding:8px;"><img src="${escapeHtml(previewUrl)}" alt="${escapeHtml(item.title || 'Prompt')}" style="width:100%; height:100%; object-fit:contain; object-position:center top; display:block; border-radius:12px;"></div>` : ''}
          <div style="margin-top:${previewUrl ? '10px' : '0'};">
            <div style="font-size:15px; font-weight:700; line-height:1.3;">${escapeHtml(item.title || 'Prompt')}</div>
            <div style="margin-top:4px; color:rgba(255,255,255,0.62); font-size:12px;">${escapeHtml(item.model_hint || 'Без model hint')}</div>
            <div style="margin-top:8px; color:rgba(255,255,255,0.8); font-size:13px; line-height:1.5;">${escapeHtml(previewText.slice(0, 140))}${previewText.length > 140 ? '…' : ''}</div>
          </div>
        </button>
        <div style="display:flex; gap:8px; flex-wrap:wrap;">
          <button class="btn ghost small" data-action="open-prompt-item" data-item-id="${escapeHtml(item.id)}">Открыть</button>
          <button class="btn outline small" data-action="copy-prompt" data-prompt="${encodeURIComponent(item.prompt_text || '')}">Копировать</button>
        </div>
      </article>
    `;
  }).join('');

  return `
    <div class="workspace-grid single">
      <div class="workspace-main scroll">
        <section class="library-card" style="padding:18px 18px 16px;">
          <div class="field-head" style="align-items:flex-start; gap:12px;">
            <div>
              <h4 style="margin:0;">Категории</h4>
              <div class="help-text" style="margin-top:6px;">Доступ к промтам открыт только для авторизованных пользователей.</div>
            </div>
            <button class="btn ghost small" data-action="refresh-prompts">Обновить</button>
          </div>
          <div class="quick-chips" style="margin-top:14px; gap:8px;">${categories || '<span class="muted">Категории ещё не загружены.</span>'}</div>
        </section>

        ${state.prompts.selectedCategory ? `
          ${autoOpenSingleGroup ? '' : `
            <section class="library-card" style="margin-top:16px; padding:18px;">
              <div class="field-head" style="align-items:flex-start; gap:12px;">
                <div>
                  <h4 style="margin:0;">Подборки · ${escapeHtml(selectedCategoryTitle || 'Категория')}</h4>
                  <div class="help-text" style="margin-top:6px;">Нажми на подборку — ниже откроются карточки этой категории.</div>
                </div>
              </div>
              ${groups
                ? `<div style="margin-top:14px; display:grid; grid-template-columns:repeat(auto-fit, minmax(210px, 1fr)); gap:14px;">${groups}</div>`
                : `<div class="empty-state" style="margin-top:14px;">Нет групп в этой категории.</div>`}
            </section>
          `}
        ` : `
          <section class="library-card" style="margin-top:16px; padding:18px;">
            <div class="empty-state">Выбери категорию сверху — затем откроются её подборки.</div>
          </section>
        `}

        ${state.prompts.selectedGroupId ? `
          <section class="library-card" style="margin-top:16px; padding:18px;">
            <div class="field-head" style="align-items:flex-start; gap:12px;">
              <div>
                <h4 style="margin:0;">${autoOpenSingleGroup ? `Промты · ${escapeHtml(selectedCategoryTitle || 'Видео')}` : `Карточки · ${escapeHtml(selectedGroup?.title || 'Подборка')}`}</h4>
                <div class="help-text" style="margin-top:6px;">${autoOpenSingleGroup ? 'Промты открываются сразу без промежуточной кнопки группы.' : 'Карточки открываются отдельно, не на весь экран.'}</div>
              </div>
            </div>
            ${items
              ? `<div style="margin-top:14px; display:grid; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); gap:14px; align-items:start;">${items}</div>`
              : `<div class="empty-state" style="margin-top:14px;">${autoOpenSingleGroup ? 'В этой категории пока нет карточек.' : 'В этой подборке пока нет карточек.'}</div>`}
          </section>
        ` : ''}

      </div>
    </div>
  `;
}

function renderLibraryInspector() {
  return '';
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


function siteBuilderSelectedProject() {
  const items = Array.isArray(state.siteBuilder?.projects) ? state.siteBuilder.projects : [];
  const selectedId = String(state.siteBuilder?.selectedProjectId || '').trim();
  if (state.siteBuilder?.selectedProject && String(state.siteBuilder.selectedProject.id || '') === selectedId) {
    return state.siteBuilder.selectedProject;
  }
  return items.find((item) => String(item.id || '') === selectedId) || state.siteBuilder?.selectedProject || null;
}

function siteBuilderProjectHideKey(item) {
  return String(item?.id || '').trim();
}

function siteBuilderVersionHideKey(projectId, version) {
  return [String(projectId || '').trim(), String(version?.id || version?.version_number || version?.created_at || '').trim()].filter(Boolean).join('::');
}

function siteBuilderJobHideKey(projectId, job) {
  return [
    String(projectId || '').trim(),
    String(job?.id || job?.job_id || job?.created_at || '').trim(),
    String(job?.job_type || '').trim(),
  ].filter(Boolean).join('::');
}

function siteBuilderHiddenSet(kind) {
  const list = Array.isArray(state.siteBuilder?.[kind]) ? state.siteBuilder[kind] : [];
  return new Set(list.map((item) => String(item || '').trim()).filter(Boolean));
}

function clearSiteBuilderRevisionFiles() {
  const key = 'siteBuilder.revisionFiles';
  if (runtime.files[key]) {
    revokeRuntimeFileValue(runtime.files[key]);
    delete runtime.files[key];
  }
  const input = document.getElementById('site_revision_files');
  if (input) input.value = '';
}

function dismissSiteBuilderItem(kind, key, options = {}) {
  const normalizedKind = ['hiddenProjects', 'hiddenVersions', 'hiddenJobs'].includes(kind) ? kind : '';
  const normalizedKey = String(key || '').trim();
  if (!normalizedKind || !normalizedKey) return;
  const current = Array.isArray(state.siteBuilder[normalizedKind]) ? state.siteBuilder[normalizedKind] : [];
  if (!current.includes(normalizedKey)) state.siteBuilder[normalizedKind] = [...current, normalizedKey].slice(-240);

  if (normalizedKind === 'hiddenProjects') {
    state.siteBuilder.projects = (Array.isArray(state.siteBuilder.projects) ? state.siteBuilder.projects : []).filter((item) => siteBuilderProjectHideKey(item) !== normalizedKey);
    if (String(state.siteBuilder.selectedProjectId || '').trim() === normalizedKey) {
      const next = (Array.isArray(state.siteBuilder.projects) ? state.siteBuilder.projects : [])[0] || null;
      state.siteBuilder.selectedProjectId = String(next?.id || '').trim();
      state.siteBuilder.selectedProject = next;
      if (!next) {
        state.siteBuilder.versions = [];
        state.siteBuilder.jobs = [];
      }
    }
  }

  if (normalizedKind === 'hiddenVersions') {
    const currentProjectId = String(options.projectId || state.siteBuilder.selectedProjectId || '').trim();
    state.siteBuilder.versions = (Array.isArray(state.siteBuilder.versions) ? state.siteBuilder.versions : []).filter((item) => siteBuilderVersionHideKey(currentProjectId, item) !== normalizedKey);
  }

  if (normalizedKind === 'hiddenJobs') {
    const currentProjectId = String(options.projectId || state.siteBuilder.selectedProjectId || '').trim();
    state.siteBuilder.jobs = (Array.isArray(state.siteBuilder.jobs) ? state.siteBuilder.jobs : []).filter((item) => siteBuilderJobHideKey(currentProjectId, item) !== normalizedKey);
  }

  saveState();
  render();
  toast('success', 'Убрано из списка', 'Карточка скрыта в интерфейсе этого workspace.');
}

function renderInlineUploadField(label, id, help, multiple = false, accept = 'image/*') {
  const config = FILE_INPUT_MAP[id];
  const asset = config ? getFile(config.key) : null;
  const triggerTitle = multiple ? 'Добавить файлы' : 'Добавить файл';
  const normalizedAccept = String(accept || '').toLowerCase();
  let acceptLabel = 'PNG / JPG / WEBP';
  if (normalizedAccept.includes('audio/') || normalizedAccept.includes('.mp3') || normalizedAccept.includes('.wav')) acceptLabel = 'MP3 / WAV';
  else if (normalizedAccept.includes('video/') || normalizedAccept.includes('.mp4') || normalizedAccept.includes('.mov') || normalizedAccept.includes('.webm')) acceptLabel = 'MP4 / MOV / WEBM';
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
    <div class="site-inline-upload" style="margin-top:12px;">
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
  `;
}

function siteBuilderStatusTone(status) {
  const value = String(status || '').trim().toLowerCase();
  if (['completed', 'preview_ready'].includes(value)) return 'ok';
  if (['generating', 'queued', 'payment_pending'].includes(value)) return 'warn';
  if (value === 'failed') return 'warn';
  return 'muted';
}

function siteBuilderStatusLabel(status) {
  const value = String(status || '').trim().toLowerCase();
  if (value === 'draft') return 'Черновик';
  if (value === 'preview_ready') return 'Готов к запуску';
  if (value === 'payment_pending') return 'Списаны токены';
  if (value === 'queued') return 'В очереди';
  if (value === 'generating') return 'Создаётся';
  if (value === 'completed') return 'Готово';
  if (value === 'failed') return 'Ошибка';
  return value || '—';
}

async function loadSiteBuilderMeta(options = {}) {
  if (!state.authToken) return null;
  try {
    const res = await apiFetch('/api/site-builder/meta');
    const data = await res.json();
    state.siteBuilder.prices = {
      create: Number(data?.prices?.create || state.siteBuilder.prices.create || 30),
      revision: Number(data?.prices?.revision || state.siteBuilder.prices.revision || 10),
    };
    if (typeof data?.balance_tokens !== 'undefined') state.balance = Number(data.balance_tokens || 0);
    saveState();
    renderHeader();
    renderInspector();
    return data;
  } catch (e) {
    if (!options.silent) toast('error', 'Site Creator', String(e.message || e));
    return null;
  }
}

async function loadSiteBuilderProjects(options = {}) {
  const { silent = true, keepSelection = true, selectId = '' } = options;
  if (!state.authToken) {
    state.siteBuilder.projects = [];
    state.siteBuilder.selectedProjectId = '';
    state.siteBuilder.selectedProject = null;
    state.siteBuilder.versions = [];
    state.siteBuilder.jobs = [];
    state.siteBuilder.loaded = false;
    state.siteBuilder.loading = false;
    state.siteBuilder.lastError = '';
    saveState();
    renderWorkspace();
    renderInspector();
    return;
  }
  state.siteBuilder.loading = true;
  state.siteBuilder.lastError = '';
  renderWorkspace();
  renderInspector();
  try {
    await loadSiteBuilderMeta({ silent: true });
    const res = await apiFetch('/api/site-builder/projects');
    const data = await res.json();
    const items = Array.isArray(data.items) ? data.items : [];
    state.siteBuilder.projects = items;
    state.siteBuilder.loaded = true;
    const preferredId = String(selectId || (keepSelection ? state.siteBuilder.selectedProjectId : '') || '').trim();
    let selected = preferredId ? items.find((item) => String(item.id || '') === preferredId) : null;
    if (!selected && items.length) selected = items[0];
    state.siteBuilder.selectedProjectId = selected?.id || '';
    state.siteBuilder.selectedProject = selected || null;
    if (selected?.id) await loadSiteBuilderProject(selected.id, { silent: true });
    else {
      state.siteBuilder.versions = [];
      state.siteBuilder.jobs = [];
    }
  } catch (e) {
    state.siteBuilder.lastError = String(e.message || e);
    if (!silent) toast('error', 'Не удалось загрузить проекты сайтов', state.siteBuilder.lastError);
  } finally {
    state.siteBuilder.loading = false;
    saveState();
    renderWorkspace();
    renderInspector();
    renderHeader();
  }
}

async function loadSiteBuilderProject(projectId, options = {}) {
  const { silent = true } = options;
  const projectIdText = String(projectId || '').trim();
  if (!projectIdText || !state.authToken) return null;
  try {
    const res = await apiFetch(`/api/site-builder/projects/${encodeURIComponent(projectIdText)}`);
    const data = await res.json();
    const item = data.item || null;
    if (item) {
      const idx = state.siteBuilder.projects.findIndex((entry) => String(entry.id || '') === String(item.id || ''));
      if (idx >= 0) state.siteBuilder.projects[idx] = item;
      else state.siteBuilder.projects.unshift(item);
      state.siteBuilder.selectedProjectId = String(item.id || projectIdText);
      state.siteBuilder.selectedProject = item;
    }
    state.siteBuilder.versions = Array.isArray(data.versions) ? data.versions : [];
    state.siteBuilder.jobs = Array.isArray(data.jobs) ? data.jobs : [];
    saveState();
    renderWorkspace();
    renderInspector();
    renderHeader();
    return item;
  } catch (e) {
    if (!silent) toast('error', 'Не удалось открыть проект', String(e.message || e));
    return null;
  }
}

async function createSiteBuilderProject() {
  if (!requireAuth()) return;
  const title = String(state.siteBuilder.create.title || '').trim() || 'Новый сайт';
  const briefRaw = String(state.siteBuilder.create.briefRaw || '').trim();
  const extraTextsRaw = String(state.siteBuilder.create.extraTextsRaw || '').trim();
  if (briefRaw.length < 40) {
    toast('error', 'Бриф слишком короткий', 'Опиши сайт подробнее: нишу, цель, блоки, стиль, оффер и контакты.');
    return;
  }
  try {
    const res = await apiFetch('/api/site-builder/projects', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, brief_raw: briefRaw, extra_texts_raw: extraTextsRaw }),
    });
    const data = await res.json();
    state.siteBuilder.revisionText = '';
    toast('success', 'Проект сайта создан', 'Теперь можно сразу запустить сборку сайта.');
    await loadSiteBuilderProjects({ silent: true, keepSelection: false, selectId: data?.item?.id || '' });
  } catch (e) {
    toast('error', 'Не удалось создать проект', String(e.message || e));
  }
}

async function runSiteBuilderBuild(projectId) {
  if (!requireAuth()) return;
  const projectIdText = String(projectId || '').trim();
  if (!projectIdText) {
    toast('error', 'Проект не выбран', 'Сначала создай проект или выбери его справа.');
    return;
  }
  try {
    const res = await apiFetch(`/api/site-builder/projects/${encodeURIComponent(projectIdText)}/build`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    });
    const data = await res.json();
    if (typeof data.balance_tokens !== 'undefined') state.balance = Number(data.balance_tokens || 0);
    toast('success', 'Сборка запущена', 'Сайт ушёл в очередь воркера. Готовый ZIP появится в версиях и придёт в Telegram.');
    await loadSiteBuilderProjects({ silent: true, keepSelection: true, selectId: projectIdText });
  } catch (e) {
    toast('error', 'Не удалось запустить сборку', String(e.message || e));
  }
}

async function runSiteBuilderRevision(projectId) {
  if (!requireAuth()) return;
  const projectIdText = String(projectId || '').trim();
  const requestRaw = String(state.siteBuilder.revisionText || '').trim();
  const revisionFilesRaw = getFile('siteBuilder.revisionFiles');
  const revisionFiles = Array.isArray(revisionFilesRaw) ? revisionFilesRaw.filter((item) => item?.file) : (revisionFilesRaw?.file ? [revisionFilesRaw] : []);
  if (!projectIdText) {
    toast('error', 'Проект не выбран', 'Сначала выбери проект сайта.');
    return;
  }
  if (requestRaw.length < 8) {
    toast('error', 'Добавь правки', 'Опиши изменения одним сообщением: блоки, тексты, цвета, CTA, секции и т.д.');
    return;
  }
  try {
    let options;
    if (revisionFiles.length) {
      const form = new FormData();
      form.append('request_raw', requestRaw);
      revisionFiles.forEach((item, index) => {
        form.append('reference_images', item.file, item.name || `reference-${index + 1}.png`);
      });
      options = { method: 'POST', body: form };
    } else {
      options = {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ request_raw: requestRaw }),
      };
    }
    const res = await apiFetch(`/api/site-builder/projects/${encodeURIComponent(projectIdText)}/revisions`, options);
    const data = await res.json();
    if (typeof data.balance_tokens !== 'undefined') state.balance = Number(data.balance_tokens || 0);
    state.siteBuilder.revisionText = '';
    clearSiteBuilderRevisionFiles();
    toast('success', 'Правки запущены', 'Новая версия сайта собирается в фоне.');
    await loadSiteBuilderProjects({ silent: true, keepSelection: true, selectId: projectIdText });
  } catch (e) {
    toast('error', 'Не удалось запустить правки', String(e.message || e));
  }
}

async function downloadSiteBuilderVersion(projectId, versionNumber) {
  if (!requireAuth()) return;
  const projectIdText = String(projectId || '').trim();
  const version = Number(versionNumber || 0);
  if (!projectIdText || !version) return;
  try {
    const res = await apiFetch(`/api/site-builder/projects/${encodeURIComponent(projectIdText)}/versions/${encodeURIComponent(version)}/download`);
    const blob = await res.blob();
    const href = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = href;
    a.download = `site-v${version}.zip`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(href), 1200);
  } catch (e) {
    toast('error', 'Не удалось скачать версию', String(e.message || e));
  }
}

function renderHistoryWorkspace() {
  const hiddenProjects = siteBuilderHiddenSet('hiddenProjects');
  const hiddenVersions = siteBuilderHiddenSet('hiddenVersions');
  const hiddenJobs = siteBuilderHiddenSet('hiddenJobs');
  const projects = (Array.isArray(state.siteBuilder.projects) ? state.siteBuilder.projects : []).filter((item) => !hiddenProjects.has(siteBuilderProjectHideKey(item)));
  const selected = projects.find((item) => String(item.id || '') === String(state.siteBuilder.selectedProjectId || '')) || (siteBuilderSelectedProject() && !hiddenProjects.has(siteBuilderProjectHideKey(siteBuilderSelectedProject())) ? siteBuilderSelectedProject() : null);
  const selectedProjectId = String(selected?.id || state.siteBuilder.selectedProjectId || '').trim();
  const versions = (Array.isArray(state.siteBuilder.versions) ? state.siteBuilder.versions : []).filter((item) => !hiddenVersions.has(siteBuilderVersionHideKey(selectedProjectId, item)));
  const jobs = (Array.isArray(state.siteBuilder.jobs) ? state.siteBuilder.jobs : []).filter((item) => !hiddenJobs.has(siteBuilderJobHideKey(selectedProjectId, item)));
  const canBuild = !!selected && Number(selected.current_version || 0) <= 0 && !['queued', 'payment_pending', 'generating'].includes(String(selected.status || ''));
  const revisionPrice = selected && !selected.free_revision_used ? 0 : Number(state.siteBuilder.prices?.revision || 10);
  const buildPrice = Number(state.siteBuilder.prices?.create || 30);

  if (!state.authToken || !state.me) {
    return `
      <div class="workspace-grid single">
        <div class="workspace-main scroll">
          <div class="profile-card">
            <h4>Site Creator</h4>
            <small>Чтобы создавать сайты, нужен вход через Telegram. После входа здесь появятся проекты, версии и запуск сборки через воркер.</small>
            <div class="actions compact-gap" style="margin-top:14px; flex-wrap:wrap;">
              <button class="btn primary" data-action="switch-studio" data-studio="profile">Перейти ко входу</button>
              <button class="btn ghost" data-action="show-showcase">Назад к витрине</button>
            </div>
          </div>
        </div>
      </div>
    `;
  }

  return `
    <div class="workspace-grid site-creator-grid">
      <div class="workspace-main scroll">
        <div class="profile-card site-builder-card site-builder-hero-card">
          <div class="field-head" style="align-items:flex-start; flex-wrap:wrap; gap:12px;">
            <div>
              <h4>Новый проект сайта</h4>
              <small>Собирается лендинг в ZIP: index.html, styles.css, script.js и README.txt.</small>
            </div>
            <span class="badge muted">${buildPrice} ток.</span>
          </div>
          <div class="field-grid two" style="margin-top:12px;">
            <div class="input-group">
              <label class="label">Название проекта</label>
              <input id="site_project_title" type="text" placeholder="Например: NeiroAstra Dance School" value="${escapeHtml(state.siteBuilder.create.title || '')}">
            </div>
            <div class="input-group">
              <label class="label">Баланс</label>
              <input type="text" value="${escapeHtml(state.balance == null ? '—' : `${state.balance} ток.`)}" disabled>
            </div>
          </div>
          <div class="input-group" style="margin-top:12px;">
            <div class="field-head site-brief-head">
              <label class="label" for="site_project_brief">Бриф сайта</label>
              <a
                class="btn outline small site-brief-sample-btn"
                href="${escapeHtml(SITE_BUILDER_BRIEF_SAMPLE_URL)}"
                download="brief.txt"
              >Скачать образец брифа</a>
            </div>
            <textarea id="site_project_brief" placeholder="Кто вы, что продаёте, для кого сайт, какие блоки нужны, какой стиль, какие офферы и контакты должны быть на странице.">${escapeHtml(state.siteBuilder.create.briefRaw || '')}</textarea>
          </div>
          <div class="input-group" style="margin-top:12px;">
            <label class="label">Готовые тексты и доп. материалы</label>
            <textarea id="site_project_extraTexts" placeholder="Сюда можно вставить описание компании, преимущества, тарифы, FAQ, блоки услуг, адрес, телефон, CTA и любые обязательные формулировки.">${escapeHtml(state.siteBuilder.create.extraTextsRaw || '')}</textarea>
          </div>
          <div class="actions compact-gap" style="margin-top:14px; flex-wrap:wrap;">
            <button class="btn primary" data-action="site-create-project">Создать проект</button>
            <button class="btn ghost" data-action="site-clear-draft">Очистить поля</button>
            <button class="btn outline" data-action="refresh-site-projects">Обновить список</button>
          </div>
          ${state.siteBuilder.lastError ? `<div class="help-text" style="margin-top:12px;">${escapeHtml(state.siteBuilder.lastError)}</div>` : ''}
        </div>

        <div class="profile-card site-builder-card" style="margin-top:16px;">
          <div class="field-head" style="align-items:flex-start; flex-wrap:wrap; gap:12px;">
            <div>
              <h4>${escapeHtml(selected?.title || 'Выбери проект')}</h4>
              <small>${selected ? 'Запуск сборки, версия сайта и дальнейшие правки.' : 'После создания или выбора проекта справа здесь появятся действия.'}</small>
            </div>
            ${selected ? `<span class="badge ${siteBuilderStatusTone(selected.status)}">${escapeHtml(siteBuilderStatusLabel(selected.status))}</span>` : ''}
          </div>
          ${selected ? `
            <div class="tableish" style="margin-top:12px;">
              <div class="table-row"><span class="muted">Текущая версия</span><span>v${escapeHtml(Number(selected.current_version || 0))}</span><span class="badge muted">versions</span></div>
              <div class="table-row"><span class="muted">Бесплатная правка</span><span>${selected.free_revision_used ? 'уже использована' : 'доступна'}</span><span class="badge muted">revision</span></div>
              <div class="table-row"><span class="muted">Обновлён</span><span>${escapeHtml(formatDate(selected.updated_at || selected.created_at))}</span><span class="badge muted">sync</span></div>
            </div>
            <div class="actions compact-gap" style="margin-top:14px; flex-wrap:wrap;">
              <button class="btn primary" data-action="site-run-build" data-project-id="${escapeHtml(selected.id || '')}" ${canBuild ? '' : 'disabled'}>${canBuild ? `Создать сайт за ${buildPrice} ток.` : 'Сайт уже создаётся / создан'}</button>
              <button class="btn ghost" data-action="site-open-project" data-project-id="${escapeHtml(selected.id || '')}">Обновить проект</button>
            </div>
            <div class="input-group" style="margin-top:14px;">
              <label class="label">Пакет правок</label>
              <textarea id="site_revision_text" placeholder="Например: сделать первый экран светлее, усилить CTA, добавить блок с тарифами и FAQ, сократить текст о компании.">${escapeHtml(state.siteBuilder.revisionText || '')}</textarea>
            </div>
            ${renderInlineUploadField('Фото и скриншоты для правки', 'site_revision_files', 'Можно приложить примеры блоков, скриншоты сайта и фото, чтобы правка была точнее.', true, 'image/*')}
            <div class="actions compact-gap" style="margin-top:12px; flex-wrap:wrap;">
              <button class="btn secondary" data-action="site-run-revision" data-project-id="${escapeHtml(selected.id || '')}" ${Number(selected.current_version || 0) > 0 ? '' : 'disabled'}>${revisionPrice === 0 ? 'Запустить бесплатную правку' : `Запустить правку за ${revisionPrice} ток.`}</button>
            </div>
          ` : `<div class="empty-state" style="margin-top:14px;">Пока проект не выбран.</div>`}
        </div>
      </div>

      <div class="workspace-side scroll">
        <div class="result-card site-builder-card">
          <div class="field-head"><h4>Проекты сайтов</h4><span class="badge muted">${projects.length}</span></div>
          <small>${state.siteBuilder.loading ? 'Обновляю список...' : 'Выбери проект, чтобы открыть версии и статус.'}</small>
          <div class="mini-list" style="margin-top:14px;">
            ${projects.length ? projects.map((item) => `
              <div class="history-item compact ${String(selected?.id || '') === String(item.id || '') ? 'active' : ''}">
                <button class="history-delete-btn" data-action="dismiss-site-project" data-project-id="${escapeHtml(item.id || '')}" title="Убрать проект из списка" aria-label="Убрать проект из списка">×</button>
                <div class="history-item-row"><strong>${escapeHtml(item.title || 'Сайт')}</strong><span class="badge ${siteBuilderStatusTone(item.status)}">${escapeHtml(siteBuilderStatusLabel(item.status))}</span></div>
                <small>v${escapeHtml(Number(item.current_version || 0))} · ${escapeHtml(formatDate(item.updated_at || item.created_at))}</small>
                <div class="actions compact-gap" style="margin-top:10px; flex-wrap:wrap;">
                  <button class="btn outline small" data-action="site-open-project" data-project-id="${escapeHtml(item.id || '')}">Открыть</button>
                </div>
              </div>
            `).join('') : `<div class="empty-state">Пока нет проектов сайта.</div>`}
          </div>
        </div>

        <div class="result-card site-builder-card">
          <div class="field-head"><h4>Версии</h4><span class="badge muted">${versions.length}</span></div>
          <div class="mini-list" style="margin-top:14px;">
            ${versions.length ? versions.map((version) => `
              <div class="history-item compact">
                <button class="history-delete-btn" data-action="dismiss-site-version" data-project-id="${escapeHtml(selectedProjectId)}" data-version-number="${escapeHtml(String(version.id || version.version_number || version.created_at || ''))}" title="Убрать версию из списка" aria-label="Убрать версию из списка">×</button>
                <div class="history-item-row"><strong>v${escapeHtml(Number(version.version_number || 0))}</strong><span class="badge muted">${escapeHtml(String(version.source_type || 'build'))}</span></div>
                <small>${escapeHtml(formatDate(version.created_at))}</small>
                <div class="actions compact-gap" style="margin-top:10px; flex-wrap:wrap;">
                  <button class="btn outline small" data-action="site-download-version" data-project-id="${escapeHtml(selected?.id || '')}" data-version-number="${escapeHtml(Number(version.version_number || 0))}">Скачать ZIP</button>
                </div>
              </div>
            `).join('') : `<div class="empty-state">Версий ещё нет.</div>`}
          </div>
        </div>

        <div class="result-card site-builder-card">
          <div class="field-head"><h4>Очередь и статусы</h4><span class="badge muted">${jobs.length}</span></div>
          <div class="mini-list" style="margin-top:14px;">
            ${jobs.length ? jobs.map((job) => `
              <div class="history-item compact">
                <button class="history-delete-btn" data-action="dismiss-site-job" data-project-id="${escapeHtml(selectedProjectId)}" data-job-key="${escapeHtml(String(job.id || job.job_id || job.created_at || ''))}" data-job-type="${escapeHtml(String(job.job_type || 'job'))}" title="Убрать запуск из списка" aria-label="Убрать запуск из списка">×</button>
                <div class="history-item-row"><strong>${escapeHtml(String(job.job_type || 'job').toUpperCase())}</strong><span class="badge ${siteBuilderStatusTone(job.status)}">${escapeHtml(siteBuilderStatusLabel(job.status))}</span></div>
                <small>${escapeHtml(formatDate(job.updated_at || job.created_at))}</small>
                <small>${job.is_free_revision ? 'Бесплатная правка' : `${escapeHtml(Number(job.tokens_cost || 0))} ток.`}</small>
              </div>
            `).join('') : `<div class="empty-state">Запусков пока нет.</div>`}
          </div>
        </div>
      </div>
    </div>
  `;
}

function renderHistoryInspector() {
  const selected = siteBuilderSelectedProject();
  const buildPrice = Number(state.siteBuilder.prices?.create || 30);
  const revisionPrice = selected && !selected.free_revision_used ? 0 : Number(state.siteBuilder.prices?.revision || 10);
  if (!state.authToken || !state.me) {
    return `
      <div class="inspector-card">
        <div class="section-title">Site Creator</div>
        <div class="help-text">Войди через Telegram, чтобы открыть проекты сайтов, запускать сборку и скачивать ZIP-версии.</div>
      </div>
    `;
  }
  return `
    <div class="inspector-card">
      <div class="section-title">Site Creator</div>
      <div class="tableish" style="margin-top:12px;">
        <div class="table-row"><span class="muted">Создание сайта</span><span>${escapeHtml(buildPrice)} ток.</span><span class="badge muted">build</span></div>
        <div class="table-row"><span class="muted">Следующая правка</span><span>${escapeHtml(revisionPrice)} ток.</span><span class="badge muted">revision</span></div>
        <div class="table-row"><span class="muted">Баланс</span><span>${escapeHtml(state.balance == null ? '—' : `${state.balance} ток.`)}</span><span class="badge muted">wallet</span></div>
      </div>
      <div class="actions compact-gap" style="margin-top:12px; flex-wrap:wrap;">
        <button class="btn ghost small" data-action="refresh-site-projects">Обновить</button>
      </div>
    </div>
    ${selected ? `
      <div class="inspector-card">
        <div class="section-title">Текущий проект</div>
        <div class="help-text" style="margin-top:10px;">${escapeHtml(selected.title || 'Сайт')}</div>
        <div class="tableish" style="margin-top:12px;">
          <div class="table-row"><span class="muted">Статус</span><span>${escapeHtml(siteBuilderStatusLabel(selected.status))}</span><span class="badge ${siteBuilderStatusTone(selected.status)}">state</span></div>
          <div class="table-row"><span class="muted">Версия</span><span>v${escapeHtml(Number(selected.current_version || 0))}</span><span class="badge muted">zip</span></div>
          <div class="table-row"><span class="muted">Бесплатная правка</span><span>${selected.free_revision_used ? 'использована' : 'доступна'}</span><span class="badge muted">bonus</span></div>
        </div>
      </div>
    ` : ''}
    <div class="inspector-card">
      <div class="section-title">Логика работы</div>
      <div class="help-text" style="margin-top:10px;">1 бесплатная правка уже включена в 30 токенов. После этого каждая следующая правка стоит 10 токенов. Готовый архив также приходит в Telegram через worker_site.py.</div>
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


function partnerDashboard() {
  return state.partner.dashboard || null;
}

function renderPartnerWorkspace() {
  if (!state.authToken || !state.me) {
    return `
      <div class="workspace-grid single">
        <div class="workspace-main scroll">
          <div class="profile-card">
            <h3>Партнёрская программа</h3>
            <p class="muted">Войди в аккаунт, чтобы получить реферальную ссылку, видеть рефералов, начисления и заявки на вывод.</p>
            <button class="btn primary" data-action="switch-studio" data-studio="profile">Войти</button>
          </div>
        </div>
      </div>
    `;
  }

  const data = partnerDashboard();
  if (state.partner.loading && !data) {
    return `<div class="placeholder-stage"><div class="empty-copy"><strong>Загружаю партнёрский кабинет…</strong><div>Данные подтягиваются из backend.</div></div></div>`;
  }
  if (state.partner.lastError && !data) {
    return `
      <div class="workspace-grid single"><div class="workspace-main scroll">
        <div class="profile-card"><h3>Не удалось загрузить партнёрку</h3><p class="muted">${escapeHtml(state.partner.lastError)}</p><button class="btn primary" data-action="partner-refresh">Повторить</button></div>
      </div></div>
    `;
  }

  const profile = data?.profile || {};
  const stats = data?.stats || {};
  const referrals = Array.isArray(data?.referrals) ? data.referrals : [];
  const commissions = Array.isArray(data?.commissions) ? data.commissions : [];
  const payouts = Array.isArray(data?.payouts) ? data.payouts : [];
  const canPayout = Number(stats.available_balance_rub || 0) >= Number(stats.min_payout_rub || 1000);
  const siteLink = partnerSiteLink(profile);
  const botLink = partnerBotLink(profile);

  return `
    <div class="workspace-grid">
      <div class="workspace-main scroll">
        <div class="metrics">
          <div class="metric"><strong>${escapeHtml(String(stats.total_referrals ?? 0))}</strong><span>рефералов всего</span></div>
          <div class="metric"><strong>${escapeHtml(String(stats.paid_referrals ?? 0))}</strong><span>оплативших</span></div>
          <div class="metric"><strong>${escapeHtml(formatRub(stats.available_balance_rub))}</strong><span>доступно к выводу</span></div>
        </div>

        <div class="profile-card" style="margin-top:16px;">
          <h3>Твои партнёрские ссылки</h3>
          <p class="muted">Один ref_code используется и для сайта, и для Telegram-бота. Ссылки разные.</p>
          <div class="input-group"><label class="label">Ссылка на сайт</label><input id="partnerSiteLink" readonly value="${escapeHtml(siteLink)}"></div>
          <div class="actions compact-gap" style="margin-top:10px; flex-wrap:wrap;">
            <button class="btn primary" data-action="partner-copy-site-link">Скопировать ссылку на сайт</button>
          </div>
          <div class="input-group" style="margin-top:14px;"><label class="label">Ссылка на Telegram-бота</label><input id="partnerBotLink" readonly value="${escapeHtml(botLink)}"></div>
          <div class="actions compact-gap" style="margin-top:10px; flex-wrap:wrap;">
            <button class="btn primary" data-action="partner-copy-bot-link">Скопировать ссылку на бота</button>
            <button class="btn ghost" data-action="partner-refresh">Обновить</button>
          </div>
        </div>

        <div class="profile-card" style="margin-top:16px;">
          <h3>Вывести средства</h3>
          <p class="muted">Минимальная сумма вывода — ${escapeHtml(formatRub(stats.min_payout_rub || 1000))}. После отправки заявка уходит админу, срок выплаты — до 3 рабочих дней.</p>
          <div class="upload-grid two">
            <div class="input-group"><label class="label">Сумма вывода, ₽</label><input id="partnerPayoutAmount" type="number" min="1000" step="100" placeholder="1000" ${canPayout ? '' : 'disabled'}></div>
            <div class="input-group"><label class="label">Номер карты</label><input id="partnerPayoutCard" inputmode="numeric" placeholder="2200 0000 0000 0000" ${canPayout ? '' : 'disabled'}></div>
          </div>
          <div class="input-group"><label class="label">ФИО получателя</label><input id="partnerPayoutName" placeholder="Иванов Иван Иванович" ${canPayout ? '' : 'disabled'}></div>
          <div class="input-group"><label class="label">Комментарий — необязательно</label><input id="partnerPayoutComment" placeholder="Например: СБП/банк" ${canPayout ? '' : 'disabled'}></div>
          <div class="actions compact-gap" style="margin-top:14px;"><button id="partnerPayoutSubmitBtn" class="btn primary" ${canPayout ? '' : 'disabled'}>${state.partner.payoutSending ? 'Отправляю…' : 'Отправить заявку'}</button></div>
        </div>

        <div class="profile-card" style="margin-top:16px;">
          <h3>Последние начисления</h3>
          <div class="tableish" style="margin-top:12px;">
            ${commissions.length ? commissions.map((item) => `
              <div class="table-row"><span>${escapeHtml(item.referral_label || 'Реферал')}</span><span>${escapeHtml(formatRub(item.commission_amount_rub))}</span><span class="badge ok">${escapeHtml(String(item.commission_percent || 0))}%</span></div>
            `).join('') : `<div class="help-text">Начислений пока нет.</div>`}
          </div>
        </div>
      </div>

      <div class="workspace-side scroll">
        <div class="result-card">
          <h4>Баланс партнёрки</h4>
          <div class="tableish" style="margin-top:12px;">
            <div class="table-row"><span class="muted">Заработано всего</span><span>${escapeHtml(formatRub(stats.earned_total_rub))}</span><span class="badge muted">total</span></div>
            <div class="table-row"><span class="muted">Доступно</span><span>${escapeHtml(formatRub(stats.available_balance_rub))}</span><span class="badge ok">available</span></div>
            <div class="table-row"><span class="muted">Ожидает выплаты</span><span>${escapeHtml(formatRub(stats.pending_payout_balance_rub))}</span><span class="badge warn">pending</span></div>
            <div class="table-row"><span class="muted">Выплачено</span><span>${escapeHtml(formatRub(stats.paid_total_rub))}</span><span class="badge muted">paid</span></div>
          </div>
        </div>

        <div class="result-card">
          <h4>Мои рефералы</h4>
          <div class="tableish" style="margin-top:12px;">
            ${referrals.length ? referrals.slice(0, 12).map((item) => `
              <div class="table-row"><span>${escapeHtml(item.label || 'Реферал')}</span><span>${escapeHtml(formatDate(item.created_at || ''))}</span><span class="badge ${item.paid ? 'ok' : 'muted'}">${item.paid ? 'оплатил' : 'новый'}</span></div>
            `).join('') : `<div class="help-text">Рефералов пока нет.</div>`}
          </div>
        </div>

        <div class="result-card">
          <h4>История выплат</h4>
          <div class="tableish" style="margin-top:12px;">
            ${payouts.length ? payouts.slice(0, 10).map((item) => `
              <div class="table-row"><span>${escapeHtml(formatRub(item.amount_rub))}</span><span>**** ${escapeHtml(item.card_last4 || '')}</span><span class="badge ${item.status === 'paid' ? 'ok' : item.status === 'rejected' ? 'danger' : 'warn'}">${escapeHtml(item.status || '')}</span></div>
            `).join('') : `<div class="help-text">Выплат пока нет.</div>`}
          </div>
        </div>
      </div>
    </div>
  `;
}

function renderPartnerInspector() {
  const data = partnerDashboard();
  const stats = data?.stats || {};
  return `
    <div class="inspector-card">
      <div class="section-title">Партнёрская программа</div>
      <div class="help-text" style="margin-top:10px;">Ссылки для сайта и Telegram-бота находятся в центральном блоке. Здесь только краткий баланс.</div>
      <div style="margin-top:14px; display:grid; gap:10px;">
        <div style="padding:12px; border:1px solid rgba(255,255,255,.08); border-radius:14px; background:rgba(255,255,255,.035);">
          <div class="muted" style="font-size:12px;">Доступно к выводу</div>
          <div style="margin-top:4px; font-weight:800; font-size:20px;">${escapeHtml(formatRub(stats.available_balance_rub))}</div>
        </div>
        <div style="padding:12px; border:1px solid rgba(255,255,255,.08); border-radius:14px; background:rgba(255,255,255,.035);">
          <div class="muted" style="font-size:12px;">Минимум вывода</div>
          <div style="margin-top:4px; font-weight:800; font-size:20px;">${escapeHtml(formatRub(stats.min_payout_rub || 1000))}</div>
        </div>
        <div style="padding:12px; border:1px solid rgba(255,255,255,.08); border-radius:14px; background:rgba(255,255,255,.035);">
          <div class="muted" style="font-size:12px;">Рефералов всего</div>
          <div style="margin-top:4px; font-weight:800; font-size:20px;">${escapeHtml(String(stats.total_referrals ?? 0))}</div>
        </div>
      </div>
      <div class="actions compact-gap" style="margin-top:12px;"><button class="btn ghost small" data-action="partner-refresh">Обновить</button></div>
    </div>
  `;
}

function renderProfileWorkspace() {
  const user = state.me || null;
  const registerPending = state.authUi.registerPendingEmail || '';
  const linkPending = state.authUi.linkPendingEmail || '';
  if (!user) {
    return `
      <div class="workspace-grid single">
        <div class="workspace-main scroll">
          <div class="profile-card">
            <h4>Вход и регистрация</h4>
            <div class="actions compact-gap" style="margin-bottom:14px; flex-wrap:wrap;">
              <button class="btn ${state.authUi.profileTab === 'login' ? 'primary' : 'ghost'}" data-action="profile-tab-login">Вход по почте</button>
              <button class="btn ${state.authUi.profileTab === 'register' ? 'primary' : 'ghost'}" data-action="profile-tab-register">Регистрация</button>
            </div>
            ${state.authUi.profileTab === 'login' ? `
              <div class="field-grid two">
                <div class="input-group"><label class="label">Email</label><input id="profile_login_email" type="email" placeholder="name@example.com"></div>
                <div class="input-group"><label class="label">Пароль</label><input id="profile_login_password" type="password" placeholder="Пароль"></div>
              </div>
              <div class="actions compact-gap" style="margin-top:14px;"><button id="profileLoginBtn" class="btn primary">Войти по почте</button></div>
              <small class="muted" style="display:block; margin-top:10px;">Если аккаунт уже существует в Telegram, войди через Telegram в правом верхнем углу и привяжи почту внутри профиля.</small>
            ` : `
              <div class="field-grid two">
                <div class="input-group"><label class="label">Email</label><input id="profile_register_email" type="email" placeholder="name@example.com" value="${escapeHtml(registerPending)}"></div>
                <div class="input-group"><label class="label">Пароль</label><input id="profile_register_password" type="password" placeholder="Минимум 6 символов"></div>
              </div>
              <div class="input-group" style="margin-top:12px;"><label class="label">Повтори пароль</label><input id="profile_register_password2" type="password" placeholder="Повтори пароль"></div>
              <div class="actions compact-gap" style="margin-top:14px;"><button id="profileRegisterStartBtn" class="btn primary">Отправить код</button></div>
              ${registerPending ? `
                <div class="field-grid two" style="margin-top:16px;">
                  <div class="input-group"><label class="label">Код из письма</label><input id="profile_register_code" type="text" inputmode="numeric" placeholder="6 цифр"></div>
                  <div class="input-group"><label class="label">Подтверждение</label><input type="text" value="${escapeHtml(registerPending)}" disabled></div>
                </div>
                <div class="actions compact-gap" style="margin-top:12px;"><button id="profileRegisterConfirmBtn" class="btn secondary">Подтвердить и войти</button></div>
              ` : ''}
              <small class="muted" style="display:block; margin-top:10px;">После подтверждения почты ты сразу войдёшь в аккаунт.</small>
            `}
          </div>
        </div>
      </div>
    `;
  }

  return `
    <div class="workspace-grid single">
      <div class="workspace-main scroll">
        <div class="profile-card">
          <h4>Профиль</h4>
          <div class="tableish">
            <div class="table-row"><span class="muted">Имя</span><span>${escapeHtml(formatUserName(user))}</span><span class="badge muted">account</span></div>
            <div class="table-row"><span class="muted">Способы входа</span><span>${escapeHtml(authMethodsLabel(user))}</span><span class="badge muted">auth</span></div>
            <div class="table-row"><span class="muted">Email</span><span>${escapeHtml(user.email || 'не привязан')}</span><span class="badge ${user.email_verified ? 'ok' : 'warn'}">${user.email_verified ? 'verified' : 'not set'}</span></div>
            <div class="table-row"><span class="muted">Telegram</span><span>${escapeHtml(user.username ? '@' + user.username : (user.linked_telegram_user_id ? `id ${user.linked_telegram_user_id}` : 'не привязан'))}</span><span class="badge ${user.linked_telegram_user_id ? 'ok' : 'muted'}">${user.linked_telegram_user_id ? 'linked' : 'not set'}</span></div>
          </div>
          <div class="actions compact-gap" style="margin-top:14px;">
            ${user.email ? `<button id="profileOpenResetBtn" class="btn secondary">Забыли пароль?</button><button id="profileChangePasswordScrollBtn" class="btn outline">Сменить пароль</button>` : ''}
            <button id="profileLogoutBtn" class="btn ghost">Выйти</button>
          </div>
        </div>

        ${user.email ? `
          <div class="profile-card" id="profileChangePasswordCard" style="margin-top:16px;">
            <h4>Сменить пароль</h4>
            <div class="field-grid two">
              <div class="input-group"><label class="label">Текущий пароль</label><input id="profile_change_current_password" type="password" placeholder="Текущий пароль"></div>
              <div class="input-group"><label class="label">Новый пароль</label><input id="profile_change_new_password" type="password" placeholder="Минимум 6 символов"></div>
            </div>
            <div class="input-group" style="margin-top:12px;"><label class="label">Повтори новый пароль</label><input id="profile_change_new_password2" type="password" placeholder="Повтори новый пароль"></div>
            <div class="actions compact-gap" style="margin-top:14px;"><button id="profileChangePasswordBtn" class="btn primary">Сохранить новый пароль</button></div>
            <small class="muted" style="display:block; margin-top:10px;">Если не помнишь старый пароль, используй восстановление через код на почту.</small>
          </div>
        ` : ''}

        ${!user.email ? `
          <div class="profile-card" style="margin-top:16px;">
            <h4>Привязать email и пароль</h4>
            <div class="field-grid two">
              <div class="input-group"><label class="label">Email</label><input id="profile_link_email" type="email" placeholder="name@example.com" value="${escapeHtml(linkPending)}"></div>
              <div class="input-group"><label class="label">Пароль</label><input id="profile_link_password" type="password" placeholder="Минимум 6 символов"></div>
            </div>
            <div class="input-group" style="margin-top:12px;"><label class="label">Повтори пароль</label><input id="profile_link_password2" type="password" placeholder="Повтори пароль"></div>
            <div class="actions compact-gap" style="margin-top:14px;"><button id="profileLinkEmailStartBtn" class="btn primary">Отправить код</button></div>
            ${linkPending ? `
              <div class="field-grid two" style="margin-top:16px;">
                <div class="input-group"><label class="label">Код из письма</label><input id="profile_link_code" type="text" inputmode="numeric" placeholder="6 цифр"></div>
                <div class="input-group"><label class="label">Email</label><input type="text" value="${escapeHtml(linkPending)}" disabled></div>
              </div>
              <div class="actions compact-gap" style="margin-top:12px;"><button id="profileLinkEmailConfirmBtn" class="btn secondary">Подтвердить привязку</button></div>
            ` : ''}
          </div>
        ` : ''}

        ${!user.linked_telegram_user_id ? `
          <div class="profile-card" style="margin-top:16px;">
            <h4>Привязать Telegram</h4>
            <small class="muted" style="display:block; margin-bottom:12px;">После привязки тот же аккаунт сайта можно будет открывать через Telegram Login.</small>
            <div id="profileTelegramLinkMount" class="telegram-login-mount"></div>
          </div>
        ` : ''}
      </div>
    </div>
  `;
}

function mediaCard(title, asset, isVideo = false, multiple = false, fit = 'cover') {
  if (!asset) return '';
  const thumbClass = `asset-thumb ${fit === 'contain' ? 'fit-contain' : ''}`.trim();
  const normalizedTitle = String(title || '').trim().toLowerCase();
  const isReferenceCard = normalizedTitle === 'reference images' || normalizedTitle === 'reference image';
  const singleReferenceStyle = isReferenceCard
    ? 'display:flex;align-items:center;justify-content:center;min-height:188px;padding:14px;border-radius:18px;background:linear-gradient(180deg, rgba(8,12,26,0.96) 0%, rgba(4,7,18,0.92) 100%);box-shadow:inset 0 0 0 1px rgba(255,255,255,0.04);'
    : '';
  const singleReferenceImageStyle = isReferenceCard
    ? 'display:block;width:100%;height:100%;max-width:220px;max-height:160px;object-fit:contain;object-position:center;border-radius:14px;box-shadow:0 10px 26px rgba(0,0,0,0.28);margin:0 auto;'
    : '';
  if (multiple && Array.isArray(asset)) {
    if (asset.length === 1) {
      const first = asset[0];
      return `
        <div class="asset-card">
          <h4>${escapeHtml(title)}</h4>
          <div class="media-card-preview" style="${singleReferenceStyle}">
            <img class="${thumbClass}" style="${singleReferenceImageStyle}" src="${escapeHtml(first.url)}" alt="${escapeHtml(first.name)}">
          </div>
          <small>1 файл</small>
        </div>
      `;
    }
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
      ${isVideo ? `<video class="${thumbClass}" src="${escapeHtml(asset.url)}" controls></video>` : `<img class="${thumbClass}" ${singleReferenceImageStyle ? `style="${singleReferenceImageStyle}"` : ''} src="${escapeHtml(asset.url)}" alt="${escapeHtml(asset.name)}">`}
      <small>${escapeHtml(asset.name)}</small>
    </div>
  `;
}


function renderChatInspector() {
  const modeOptions = [
    ['chat', 'Chat'],
    ['prompt_builder', 'Prompt Builder'],
  ].map(([value, label]) => `<option value="${value}" ${state.chat.mode === value ? 'selected' : ''}>${label}</option>`).join('');

  return `
    <div class="inspector-card">
      <div class="section-title">ChatGPT Studio</div>
      <div class="input-group"><label class="label">Assistant</label><input type="text" value="AI Chat" disabled></div>
      <div class="input-group"><label class="label">Model</label>
        <select id="chat_model">
          <option value="gpt-4o-mini" ${state.chat.model === 'gpt-4o-mini' ? 'selected' : ''}>GPT 4 mini</option>
          <option value="gpt-5.4" ${state.chat.model === 'gpt-5.4' ? 'selected' : ''}>GPT 5.4</option>
          <option value="claude-sonnet-4-6" ${state.chat.model === 'claude-sonnet-4-6' ? 'selected' : ''}>Claude Sonnet 4.6 · Free</option>
        </select>
      </div>
      <div class="input-group"><label class="label">Режим</label>
        <select id="chat_mode">${modeOptions}</select>
      </div>
      <button class="btn outline full chat-new-dialog-btn" data-action="start-new-chat">Новый диалог</button>
      <div class="help-text">Кнопка очищает историю. При смене GPT/Claude контекст не смешивается.</div>
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
        ${Object.keys(VIDEO_REGISTRY[state.video.provider]?.models || {}).length > 1 ? `
        <div class="input-group">
          <label class="label">Модель</label>
          <select id="video_model">${modelOptions}</select>
        </div>
        ` : ''}
        ${Object.keys(videoModelConfig().modes).length > 1 ? `<div class="input-group"><label class="label">Режим</label><select id="video_mode">${modeOptions}</select></div>` : ''}
      </div>
    </div>
    ${renderVideoModeFields(videoModelConfig())}
    <div class="inspector-card">
      <button class="btn primary full video-run-btn ${isVideoRunLocked() ? 'loading' : ''}" id="videoRunPrimaryBtn" data-action="run-video" ${isVideoRunLocked() ? 'disabled' : ''}>${escapeHtml(videoRunButtonLabel())}</button>
      <div class="help-text" style="margin-top:10px;">${escapeHtml(getVideoRunCost().helper || 'Стоимость генерации пересчитывается прямо в кнопке.')}</div>
    </div>
  `;
}

function renderKling3NewShotsPanel() {
  return '';
}

function renderKling3NewElementsPanel() {
  return '';
}

function renderKling3NewWorkspaceMultiShotBlock() {
  if (!(state.studio === 'video' && state.video.model === 'kling-3.0-new' && state.video.mode === 'multi_shot')) return '';
  const shots = getKling3NewShots();
  const total = getKling3NewShotDuration();
  const rows = shots.map((shot, index) => {
    const linked = getKling3NewShotElements(index);
    const linkedHtml = linked.length
      ? `<div style="margin-top:10px; display:grid; gap:8px;">${linked.map((el) => {
          const fullElement = getKling3NewElements().find((item) => item.name === el.name) || {};
          const imageUrls = kling3NewElementImageUrls(fullElement);
          const imageMetas = kling3NewElementImageMetas(fullElement);
          const thumbs = imageUrls.length
            ? `<div style="margin-top:10px; display:grid; grid-template-columns:repeat(auto-fill, minmax(92px, 1fr)); gap:10px;">${imageUrls.map((url, refIndex) => {
                const meta = imageMetas[refIndex] || {};
                const label = meta.name || kling3NewImageFileLabel(url) || `Фото ${refIndex + 1}`;
                const dims = meta.width && meta.height ? `${meta.width}×${meta.height}px` : '';
                const size = kling3NewFormatFileSize(meta.size);
                return `<div style="position:relative; border:1px solid rgba(255,255,255,.12); border-radius:14px; overflow:hidden; background:rgba(255,255,255,.045);">
                  <button type="button" data-action="kling3-new-preview-element-image" data-url="${escapeHtml(url)}" title="Открыть фото" style="display:block; width:100%; padding:0; border:0; background:transparent; cursor:pointer;">
                    <img src="${escapeHtml(url)}" loading="lazy" alt="${escapeHtml(label)}" style="display:block; width:100%; height:72px; object-fit:cover; background:#111;">
                  </button>
                  <button class="btn ghost small" type="button" data-action="kling3-new-remove-element-image" data-index="${index}" data-element-name="${escapeHtml(el.name)}" data-ref-index="${refIndex}" title="Удалить фото" style="position:absolute; top:5px; right:5px; min-width:26px; height:26px; padding:0; border-radius:999px; background:rgba(0,0,0,.62); border:1px solid rgba(255,255,255,.2);">×</button>
                  <div style="padding:7px 8px 8px;">
                    <div style="font-size:11px; font-weight:700; line-height:1.25; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;" title="${escapeHtml(label)}">${refIndex + 1}. ${escapeHtml(label)}</div>
                    <div class="help-text" style="font-size:10px; margin-top:2px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${escapeHtml([dims, size].filter(Boolean).join(' · ') || 'загружено')}</div>
                  </div>
                </div>`;
              }).join('')}</div>`
            : '';
          return `<div class="inspector-subcard" style="padding:10px; border:1px solid var(--border); border-radius:14px;">
            <div class="row between" style="gap:8px; align-items:center;">
              <span class="badge">@${escapeHtml(el.name)} · ${escapeHtml(el.kind === 'video' ? 'video' : `${el.files_count || 0} img`)}</span>
              <button class="btn ghost small" type="button" data-action="kling3-new-remove-element" data-index="${index}" data-element-name="${escapeHtml(el.name)}">Удалить element</button>
            </div>
            ${thumbs}
          </div>`;
        }).join('')}</div>`
      : `<div class="help-text" style="margin-top:10px;">Можно добавить image element прямо в этот shot. Фото можно докидывать по одному: для запуска нужно 2–4 фото в одном element. Имя вставится в prompt автоматически.</div>`;
    return `
      <div class="inspector-card" style="margin-top:14px;">
        <div class="row between" style="align-items:flex-start; gap:12px;">
          <div><div class="section-title" style="margin:0;">Shot ${index + 1}</div><div class="help-text">Локальные elements относятся только к этому shot. Сейчас доступны только image elements: на 1 element минимум 2 фото и максимум 4 фото.</div></div>
          ${shots.length > 2 ? `<button class="btn ghost small" type="button" data-action="kling3-new-remove-shot" data-index="${index}">Удалить</button>` : ''}
        </div>
        <div class="field-grid two" style="margin-top:12px;">
          ${fieldSelect('Duration', `video_kling3NewShotDuration_${index}`, String(shot.duration || '3'), [1,2,3,4,5,6,7,8,9,10,11,12].map((n) => [String(n), `${n} sec`]))}
        </div>
        <label class="field" style="margin-top:10px;"><span>Shot prompt</span><textarea id="video_kling3NewShotPrompt_${index}" rows="4" maxlength="500" placeholder="Опиши этот shot. Элементы будут подставляться как @shot${index + 1}_elN">${escapeHtml(shot.prompt || '')}</textarea></label>
        <div class="actions compact-gap" style="margin-top:10px; flex-wrap:wrap;">
          <label class="btn outline small" style="cursor:pointer;">+ Фото element<input id="video_kling3NewShotImageUpload_${index}" type="file" accept="image/jpeg,image/png" multiple style="display:none;"></label>
                  </div>
        ${linkedHtml}
      </div>
    `;
  }).join('');
  return `
    <div class="inspector-card" style="margin-top:16px;">
      <div class="row between" style="align-items:flex-start; gap:12px;">
        <div>
          <div class="section-title" style="margin:0;">Multi-shot scenes</div>
          <div class="help-text">Основной Prompt в Multi-shot скрыт: используется только prompt внутри каждого shot. Общая длительность должна быть 3–15 сек.</div>
        </div>
        <button class="btn ghost small" type="button" data-action="kling3-new-add-shot">+ Add shot</button>
      </div>
      <div class="help-text" style="margin-top:8px;">Сейчас: ${total} сек. Start frame остаётся справа. Last frame в Multi-shot не используется.</div>
      ${rows}
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
  const addPrompt = () => {};

  if (state.video.model === 'motion-control') {
    addUpload('Avatar photo', 'video_avatarImage', 'Фото персонажа, который должен повторять движение.');
    addUpload('Motion video', 'video_motionVideo', 'Референс-видео с движением.', false, 'video/*');
    addPrompt();
    addFields(`${fieldSelect('Quality', 'video_quality', state.video.quality, [['standard','Standard'],['pro','Pro']])}`);
    return parts.join('');
  }

  const needsStartFrame =
    (state.video.provider === 'kling' && ['image_to_video', 'multi_shot'].includes(mode)) ||
    (state.video.provider === 'veo' && mode === 'image_to_video') ||
    (state.video.provider === 'grok' && mode === 'image_to_video') ||
    (state.video.provider === 'pixverse_c1' && ['image_to_video', 'transition'].includes(mode));

  if (needsStartFrame) addUpload('Start frame', 'video_startFrame', 'Стартовый кадр для генерации.');
  if (state.video.provider === 'kling' && (
    (['image_to_video', 'multi_shot'].includes(mode) && ['kling-2.5', 'kling-3.0'].includes(state.video.model)) ||
    (state.video.model === 'kling-3.0-new' && mode === 'image_to_video')
  )) addUpload('End frame', 'video_endFrame', 'Финальный кадр, если нужен переход или финальная поза.');
  if (state.video.provider === 'veo' && state.video.model === 'veo-3.1-pro' && mode === 'image_to_video') addUpload('Last frame', 'video_lastFrame', 'Финальный кадр для Veo 3.1.');
  if (state.video.provider === 'pixverse_c1' && mode === 'transition') addUpload('Last frame', 'video_lastFrame', 'Последний кадр для режима Transition.');
  if (state.video.provider === 'veo' && state.video.model === 'veo-3.1-pro' && mode === 'image_to_video') addUpload('Reference images', 'video_referenceImages', 'До 3 референсов.', true, 'image/*');

  addPrompt();

  if (state.video.provider === 'grok') {
    addFields(`${fieldSelect('Mode', 'video_providerModeGrok', state.video.providerMode || 'normal', [['fun','Fun'],['normal','Normal'],['spicy','Spicy']])}${fieldSelect('Duration', 'video_durationGrok', state.video.duration || '6', grokDurationOptions())}${fieldSelect('Resolution', 'video_resolutionGrok', state.video.resolution || '480p', [['480p','480p'],['720p','720p']])}${fieldSelect('Aspect ratio', 'video_aspectRatioGrok', state.video.aspectRatio || '16:9', [['2:3','2:3'],['3:2','3:2'],['1:1','1:1'],['16:9','16:9'],['9:16','9:16']])}`);
    parts.push(`<div class="inspector-card"><div class="help-text">Fun / Normal / Spicy, resolution = 480p / 720p, длительности 6 / 12 / 18 / 24 / 30 сек.</div></div>`);
    return parts.join('');
  }

  if (state.video.provider === 'pixverse_c1') {
    if (mode === 'fusion') {
      addUpload('Reference images', 'video_referenceImages', 'До 7 изображений. Используй в prompt теги @image1 , @image2 и далее.', true, 'image/*');
    }
    const aspectField = ['text_to_video', 'fusion'].includes(mode)
      ? fieldSelect('Aspect ratio', 'video_aspectRatioPixVerse', state.video.aspectRatio || '16:9', [['16:9','16:9'],['4:3','4:3'],['1:1','1:1'],['3:4','3:4'],['9:16','9:16'],['2:3','2:3'],['3:2','3:2'],['21:9','21:9']])
      : '';
    addFields(`${fieldSelect('Duration', 'video_durationPixVerse', state.video.duration || '5', pixVerseDurationOptions())}${fieldSelect('Resolution', 'video_resolutionPixVerse', state.video.resolution || '720p', [['360p','360p'],['540p','540p'],['720p','720p'],['1080p','1080p']])}${aspectField}`);
    parts.push(`<div class="inspector-card"><div class="help-text">PixVerse C1 на сайте запускаем сразу со звуком. Доступно 5 / 10 / 15 сек.</div></div>`);
    return parts.join('');
  }

  if (state.video.model === 'kling-3.0-new') {
    addFields(`${fieldSelect('Quality', 'video_resolution', normalizeKling3NewModeValue(state.video.resolution || 'std'), [['std','Standard'],['pro','Pro'],['4K','4K']])}${fieldSelect('Aspect ratio', 'video_aspectRatio', state.video.aspectRatio || '16:9', [['16:9','16:9'],['9:16','9:16'],['1:1','1:1']])}${mode !== 'multi_shot' ? fieldSelect('Duration', 'video_duration', state.video.duration || '5', [['3','3 sec'],['5','5 sec'],['10','10 sec'],['15','15 sec']]) : ''}`);
    parts.push(`<div class="inspector-card">${fieldTogglePanel('Enable audio', 'video_enableAudio', state.video.enableAudio, 'Audio влияет на цену. В Multi-shot KIE может включать звук по умолчанию.', state.video.enableAudio ? 'Звук включён' : 'Без звука')}</div>`);
    return parts.join('');
  }

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
    if (mode === 'image_to_video') {
      addUpload('Reference images', 'video_referenceImages', 'До 9 изображений для Seedance 2.0 Preview.', true, 'image/*');
    }

    addFields(`${fieldSelect('Duration', 'video_durationSeedance', state.video.duration || '5', [['5','5 sec'],['10','10 sec'],['15','15 sec']])}${fieldSelect('Aspect ratio', 'video_aspectRatioSeedance', state.video.aspectRatio || '16:9', [['16:9','16:9'],['9:16','9:16'],['1:1','1:1']])}`);
    return parts.join('');
  }
  if (state.video.provider === 'seedance_kie') {
    if (mode === 'image_to_video') {
      addUpload('Reference images', 'video_referenceImages', 'До 7 изображений суммарно. Стартовый и последний кадр входят в лимит, только если включены.', true, 'image/*');
      addUpload('Reference audio', 'video_referenceAudios', 'До 3 аудиофайлов. Только MP3, максимум 15 секунд каждый. Необязательно.', true, '.mp3,.wav,audio/mpeg,audio/mp3,audio/wav,audio/x-wav,audio/wave');
    } else if (mode === 'omni_reference') {
      addUpload('Photo reference', 'video_referenceImages', 'Image refs остаются справа. Общий лимит Omni Reference: до 12 refs суммарно.', true, 'image/*');
      addUpload('Audio reference', 'video_referenceAudios', 'До 3 аудиофайлов. Только MP3 , максимум 15 секунд каждый. Audio-only нельзя.', true, '.mp3,.wav,audio/mpeg,audio/mp3,audio/wav,audio/x-wav,audio/wave');
    }

    addFields(`${fieldSelect('Duration', 'video_durationSeedance', state.video.duration || '5', [['5','5 sec'],['10','10 sec'],['15','15 sec']])}${fieldSelect('Aspect ratio', 'video_aspectRatioSeedance', state.video.aspectRatio || '16:9', [['16:9','16:9'],['9:16','9:16'],['1:1','1:1']])}`);
    return parts.join('');
  }
  if (state.video.provider === 'switchx') {
    const switchxAlphaMode = ['auto', 'fill'].includes(String(state.video.switchxAlphaMode || 'auto').toLowerCase()) ? String(state.video.switchxAlphaMode || 'auto').toLowerCase() : 'auto';
    addUpload('Source video', 'video_sourceVideo', 'Исходное видео для замены/стилизации до 8 сек.', false, 'video/mp4,video/quicktime');
    addUpload('Reference image', 'video_referenceImages', 'Можно загрузить свой референс вручную.', true, 'image/*');
    parts.push(sectionTextarea('Prompt для AI-референса', 'video_switchxRefPrompt', state.video.switchxRefPrompt || '', 'Опиши, каким должен быть reference image из 1-го кадра: персонаж, одежда, стиль, свет, окружение.'));
    parts.push(`<div class="inspector-card"><button class="btn secondary full" data-action="run-switchx-ref" ${state.video.switchxReferenceStatus === 'processing' ? 'disabled' : ''}>${state.video.switchxReferenceStatus === 'processing' ? 'Создание AI-референса...' : 'Создать AI-референс через Nano Banana Pro'}</button><div class="help-text" style="margin-top:10px;">Берём 1-й кадр из видео, генерируем ref через Nano Banana Pro и потом используем его в SwitchX. Стоимость AI-референса: 2 ток.</div></div>`);
    addFields(`${fieldSelect('Mask mode', 'video_switchxAlphaMode', switchxAlphaMode, [['auto','Auto'],['fill','Fill']])}${fieldSelect('Resolution', 'video_resolutionSwitchx', state.video.resolution || '1080', [['720','720p'],['1080','1080p']])}`);
    parts.push(`<div class="inspector-card"><div class="help-text">Auto — AI сам решает, что сохранить. Fill — меняет весь кадр целиком.</div></div>`);
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

  const providerOptions = Object.entries(IMAGE_REGISTRY).filter(([, provider]) => !provider?.hidden).map(([id, provider]) => `<option value="${escapeHtml(id)}" ${state.image.provider === id ? 'selected' : ''}>${escapeHtml(provider.name)}</option>`).join('');
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
  const addPrompt = () => {};

  if (imageNeedsBaseImage()) addUpload('Base image', 'image_baseImage', 'Главное фото или база, от которой нужно отталкиваться.');
  if (imageNeedsSourceImage() || (state.image.provider === 'posters' && state.image.mode === 'poster')) {
    const isProNewRefs = state.image.provider === 'nano_banana_pro_new' && state.image.mode === 'image_to_image';
    const isGptRefs = state.image.provider === 'gpt_image_2' && state.image.mode === 'image_to_image';
    addUpload(
      (isProNewRefs || isGptRefs) ? 'Reference images' : 'Source image',
      'image_sourceImage',
      isProNewRefs
        ? 'Можно добавить до 8 reference images для Nano Banana Pro - NEW.'
        : (isGptRefs
          ? 'Можно добавить до 4 reference images для GPT Image 2.0.'
          : (state.image.provider === 'posters' && state.image.mode === 'poster' ? 'Опционально: фото, которое нужно встроить в афишу.' : 'Основное изображение для редактирования или фотосессии.')),
      (isProNewRefs || isGptRefs),
      'image/*'
    );
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

  if (state.image.provider === 'gpt_image_2') {
    const aspectOptions = state.image.mode === 'image_to_image'
      ? [['match_input_image','Match input'],['16:9','16:9'],['9:16','9:16'],['1:1','1:1'],['4:5','4:5']]
      : [['16:9','16:9'],['9:16','9:16'],['1:1','1:1'],['4:5','4:5']];
    addFields(`${fieldSelect('Aspect ratio', state.image.mode === 'image_to_image' ? 'image_aspectRatio' : 'image_aspectRatioText', state.image.aspectRatio || (state.image.mode === 'image_to_image' ? 'match_input_image' : '1:1'), aspectOptions)}`);
    parts.push(`
      <div class="inspector-card">
        <div class="help-text">GPT Image 2.0: два режима как в боте — Text → Image и Image → Image. Для Image → Image можно загрузить до 4 reference images, затем опиши правку.</div>
      </div>
    `);
    return parts.join('');
  }

  if (state.image.provider === 'seedream') {
    let seedreamHelp = 'Промпт уйдёт как есть, без внутренней обвязки.';
    if (state.image.mode === 'single') seedreamHelp = '1 фото + чистый промпт. Сначала загрузи фото, потом опиши, что сделать.';
    if (state.image.mode === 'i2i') seedreamHelp = 'Фото 1 — основа, Фото 2 — референс. Промпт уйдёт как есть, без внутренней обвязки.';
    if (state.image.mode === 't2i') seedreamHelp = 'Только текстовый промпт, без входного фото.';
    parts.push(`<div class="inspector-card"><div class="help-text">${escapeHtml(seedreamHelp)}</div></div>`);
    addFields(`${fieldSelect('Resolution', 'image_resolution', state.image.resolution || '2K', [['2K','2K'],['4K','4K']])}${fieldSelect('Aspect ratio', state.image.mode === 't2i' ? 'image_aspectRatioText' : 'image_aspectRatio', state.image.aspectRatio || (state.image.mode === 't2i' ? '9:16' : 'match_input_image'), state.image.mode === 't2i' ? [['16:9','16:9'],['9:16','9:16'],['1:1','1:1'],['4:5','4:5']] : [['match_input_image','Match input'],['16:9','16:9'],['9:16','9:16'],['1:1','1:1'],['4:5','4:5']])}`);
    return parts.join('');
  }

  if (state.image.provider === 'midjourney') {
    parts.push(`<div class="inspector-card"><div class="help-text">Midjourney V7: один запуск вернёт 4 изображения. После генерации будут доступны Reroll и Variation для каждой карточки.</div></div>`);
    addUpload('Style ref', 'image_styleRefImage', 'Опционально: image style reference для --sref. Один файл.');
    addUpload('Omni ref', 'image_omniRefImage', 'Опционально: person/object reference для --oref. Один файл.');
    addFields(`${fieldSelect('Aspect ratio', 'image_aspectRatioText', state.image.aspectRatio || '1:1', [['1:1','1:1'],['16:9','16:9'],['9:16','9:16'],['4:5','4:5']])}${fieldSelect('Speed', 'image_mjSpeedMode', state.image.mjSpeedMode || 'fast', [['fast','Fast · 1 ток.'],['turbo','Turbo · 2 ток.']])}`);
    addFields(`${fieldInput('Stylize (0-1000)', 'image_mjStylize', state.image.mjStylize || 100)}${fieldInput('Chaos (0-100)', 'image_mjChaos', state.image.mjChaos || 0)}`);
    parts.push(`<div class="inspector-card">${fieldTogglePanel('Raw mode', 'image_mjRaw', !!state.image.mjRaw, 'Снижает авто-стилизацию Midjourney и делает результат более буквальным.', state.image.mjRaw ? 'RAW' : 'OFF')}</div>`);
    return parts.join('');
  }

  addPrompt();
  const showImageResolution = true;
  const showAspect = state.image.mode !== 'image_edit';
  const isNanoBanana2 = state.image.provider === 'nano_banana_2';
  const isNanoBananaProNew = state.image.provider === 'nano_banana_pro_new';
  const isKieNanoBanana = isNanoBanana2 || isNanoBananaProNew;
  const isSeedream = state.image.provider === 'seedream';
  const resolutionOptions = isKieNanoBanana
    ? [['2K','2K • 1 ток.'],['4K','4K • 2 ток.']]
    : [['1K','1K'],['2K','2K']];
  const aspectOptions = isKieNanoBanana
    ? (state.image.mode === 'image_to_image'
      ? [['match_input_image','Match input'],['16:9','16:9'],['9:16','9:16'],['1:1','1:1'],['4:5','4:5']]
      : [['16:9','16:9'],['9:16','9:16'],['1:1','1:1'],['4:5','4:5']])
    : (state.image.mode === 'image_to_image'
      ? [['match_input_image','Match input'],['16:9','16:9'],['9:16','9:16'],['1:1','1:1'],['3:4','3:4'],['4:3','4:3']]
      : [['16:9','16:9'],['9:16','9:16'],['1:1','1:1'],['3:4','3:4'],['4:3','4:3']]);
  const fieldParts = [];
  if (showImageResolution) fieldParts.push(fieldSelect('Resolution', 'image_resolution', state.image.resolution || '2K', resolutionOptions));
  if (showAspect) fieldParts.push(fieldSelect('Aspect ratio', state.image.mode === 'text_to_image' || state.image.mode === 't2i' ? 'image_aspectRatioText' : 'image_aspectRatio', state.image.aspectRatio || (state.image.mode === 'image_to_image' ? 'match_input_image' : '16:9'), aspectOptions));
  if (!isKieNanoBanana && !isSeedream) fieldParts.push(fieldSelect('Safety', 'image_safetyLevel', state.image.safetyLevel || 'high', [['high','High'],['medium','Medium'],['low','Low']]));
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

function balanceHistoryBaseReasonLabel(reason) {
  const normalized = String(reason || '').trim().toLowerCase();
  const labels = {
    yookassa_topup: 'Пополнение через ЮKassa',
    stars_topup: 'Пополнение через Telegram Stars',
    kling_hold: 'Генерация Kling',
    kling3_create: 'Генерация Kling 3',
    grok_video: 'Генерация Grok',
    pixverse_c1_video: 'Генерация PixVerse C1',
    veo_video: 'Генерация Veo',
    sora_video: 'Генерация Sora',
    seedance_video: 'Генерация Seedance 2.0 Preview',
    seedance_extend: 'Продление Seedance 2.0 Preview',
    seedance_kie_video: 'Генерация Seedance 2.0',
    seedance_kie_extend: 'Продление Seedance 2.0',
    switchx_video: 'Генерация SwitchX',
    nano_banana: 'Генерация Nano Banana',
    nano_banana_2: 'Генерация Nano Banana 2',
    nano_banana_pro: 'Генерация Nano Banana Pro',
    nano_banana_pro_new: 'Генерация Nano Banana Pro - NEW',
    photosession_generation: 'Нейрофотосессия',
    seedream_45_single: 'Генерация Seedream 4.5',
    two_photos: 'Генерация Two Photos',
    topaz_image_upscale: 'Апскейл фото Topaz',
    topaz_video_upscale: 'Апскейл видео Topaz',
    suno_generation: 'Генерация Suno',
    suno_music: 'Генерация музыки Suno',
    workspace_music: 'Генерация музыки',
    site_create: 'Создание сайта',
    site_revision: 'Правка сайта',
  };
  if (labels[normalized]) return labels[normalized];
  if (!normalized) return 'Операция с балансом';
  return normalized.replace(/_/g, ' ').replace(/\b\w/g, (ch) => ch.toUpperCase());
}

function balanceHistoryReasonLabel(item) {
  const normalized = String(item?.reason || '').trim().toLowerCase();
  if (!normalized) return 'Операция с балансом';
  if (normalized === 'kling_rollback') return 'Возврат • Генерация Kling';
  if (normalized.endsWith('_refund')) {
    return `Возврат • ${balanceHistoryBaseReasonLabel(normalized.replace(/_refund$/, ''))}`;
  }
  return balanceHistoryBaseReasonLabel(normalized);
}

function balanceHistoryFilteredItems() {
  const items = Array.isArray(state.balanceHistory.items) ? state.balanceHistory.items : [];
  switch (state.balanceHistory.filter) {
    case 'income':
      return items.filter((item) => Number(item?.delta_tokens || 0) > 0);
    case 'expense':
      return items.filter((item) => Number(item?.delta_tokens || 0) < 0);
    default:
      return items;
  }
}

function renderProfileInspector() {
  if (!state.authToken || !state.me) {
    return `
      <div class="inspector-card">
        <div class="section-title">Profile</div>
        <div class="help-text">В профиле можно зарегистрироваться по email, привязать email к Telegram-аккаунту и позже привязать Telegram к email-аккаунту.</div>
      </div>
    `;
  }

  const items = balanceHistoryFilteredItems();
  const totalItems = Array.isArray(state.balanceHistory.items) ? state.balanceHistory.items.length : 0;
  const incomeCount = (state.balanceHistory.items || []).filter((item) => Number(item?.delta_tokens || 0) > 0).length;
  const expenseCount = (state.balanceHistory.items || []).filter((item) => Number(item?.delta_tokens || 0) < 0).length;

  return `
    <div class="inspector-card">
      <div class="section-title">Баланс</div>
      <div class="tableish" style="margin-top:12px;">
        <div class="table-row"><span class="muted">Текущий баланс</span><span>${escapeHtml(state.balance == null ? '—' : `${state.balance} ток.`)}</span><span class="badge muted">wallet</span></div>
        <div class="table-row"><span class="muted">Операций загружено</span><span>${escapeHtml(String(totalItems))}</span><span class="badge muted">ledger</span></div>
        <div class="table-row"><span class="muted">Зачисления</span><span>${escapeHtml(String(incomeCount))}</span><span class="badge ok">+</span></div>
        <div class="table-row"><span class="muted">Списания</span><span>${escapeHtml(String(expenseCount))}</span><span class="badge warn">−</span></div>
      </div>
      <div class="actions compact-gap balance-history-filters" style="margin-top:12px; flex-wrap:wrap;">
        <button class="btn ${state.balanceHistory.filter === 'all' ? 'primary' : 'ghost'} small" data-action="balance-history-filter" data-filter="all">Все</button>
        <button class="btn ${state.balanceHistory.filter === 'income' ? 'primary' : 'ghost'} small" data-action="balance-history-filter" data-filter="income">Зачисления</button>
        <button class="btn ${state.balanceHistory.filter === 'expense' ? 'primary' : 'ghost'} small" data-action="balance-history-filter" data-filter="expense">Списания</button>
        <button class="btn ghost small" data-action="refresh-balance-history">Обновить</button>
      </div>
    </div>
    <div class="inspector-card">
      <div class="section-title">История токенов</div>
      <div class="help-text" style="margin-top:10px;">Последние операции по балансу аккаунта.</div>
      ${state.balanceHistory.loading ? `<div class="help-text" style="margin-top:12px;">Загружаю историю…</div>` : ''}
      ${!state.balanceHistory.loading && state.balanceHistory.lastError ? `<div class="help-text" style="margin-top:12px;">${escapeHtml(state.balanceHistory.lastError)}</div>` : ''}
      ${!state.balanceHistory.loading && !state.balanceHistory.lastError ? `
        <div class="mini-list balance-history-list" style="margin-top:14px;">
          ${items.length ? items.map((item) => {
            const delta = Number(item?.delta_tokens || 0);
            const deltaLabel = `${delta > 0 ? '+' : ''}${delta} ток.`;
            const deltaTone = delta > 0 ? 'ok' : (delta < 0 ? 'warn' : 'muted');
            const createdAtLabel = item?.created_at ? formatDate(item.created_at) : 'Дата не указана';
            return `
              <div class="history-item compact balance-history-item ${delta > 0 ? 'income' : (delta < 0 ? 'expense' : 'neutral')}">
                <div class="history-item-row"><strong>${escapeHtml(balanceHistoryReasonLabel(item))}</strong><span class="badge ${deltaTone}">${escapeHtml(deltaLabel)}</span></div>
                <small>${escapeHtml(createdAtLabel)}</small>
              </div>
            `;
          }).join('') : `<div class="empty-state">Операций пока нет.</div>`}
        </div>
      ` : ''}
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


function renderWorkspacePromptCard(label, id, value, placeholder = '', help = '', extraContent = '') {
  return `
    <div class="result-card workspace-prompt-card">
      <div class="input-group">
        <div class="field-head workspace-prompt-head">
          <label class="label" for="${id}" style="margin:0;">${escapeHtml(label)}</label>
          <span class="workspace-prompt-counter">${escapeHtml(String((value || '').length))} симв.</span>
        </div>
        <textarea id="${id}" class="workspace-prompt-textarea" rows="7" placeholder="${escapeHtml(placeholder)}">${escapeHtml(value || '')}</textarea>
      </div>
      ${extraContent || ''}
      ${help ? `<div class="help-text workspace-prompt-help">${escapeHtml(help)}</div>` : ''}
    </div>
  `;
}

function renderWorkspacePromptExtraTextarea(label, id, value, placeholder = '', rows = 4) {
  return `
    <div class="input-group" style="margin-top:14px;">
      <div class="field-head workspace-prompt-head">
        <label class="label" for="${id}" style="margin:0;">${escapeHtml(label)}</label>
        <span class="workspace-prompt-counter">${escapeHtml(String((value || '').length))} симв.</span>
      </div>
      <textarea id="${id}" class="workspace-prompt-textarea" rows="${escapeHtml(String(rows || 4))}" placeholder="${escapeHtml(placeholder)}">${escapeHtml(value || '')}</textarea>
    </div>
  `;
}

function renderPixverseFusionAliasBar() {
  if (!(state.video.provider === 'pixverse_c1' && state.video.mode === 'fusion')) return '';
  const refs = getFile('video.referenceImages');
  const items = Array.isArray(refs) ? refs.filter((item) => item?.file).slice(0, 7) : [];
  const chips = items.length
    ? items.map((item, index) => {
        const tag = `@image${index + 1}`;
        const filename = trimText(item.name || `Reference ${index + 1}`, 28);
        return `<button class="btn ghost small" type="button" data-action="insert-video-prompt-ref" data-ref-tag="${escapeHtml(tag)}" title="${escapeHtml(item.name || tag)}">${escapeHtml(tag)}</button><span class="help-text" style="margin:0;">${escapeHtml(filename)}</span>`;
      }).join('<span class="dot"></span>')
    : '<span class="help-text">Загрузи референсы, и сайт сам даст им теги @image1 , @image2 и далее.</span>';
  return `
    <div class="inspector-card" style="margin-top:14px;">
      <div class="field-head" style="margin-bottom:10px;"><div class="section-title" style="margin:0;">PixVerse Fusion tags</div></div>
      <div class="actions compact-gap" style="flex-wrap:wrap; align-items:center;">${chips}</div>
      <div class="help-text" style="margin-top:10px;">Используй в prompt только формат @image1 , @image2 , @image3 и далее.</div>
    </div>
  `;
}

function sectionUpload(label, id, help, multiple = false, accept = 'image/*') {
  const config = FILE_INPUT_MAP[id];
  const asset = config ? getFile(config.key) : null;
  const triggerTitle = multiple ? 'Добавить файлы' : 'Добавить файл';
  const normalizedAccept = String(accept || '').toLowerCase();
  let acceptLabel = 'PNG / JPG / WEBP';
  if (normalizedAccept.includes('audio/') || normalizedAccept.includes('.mp3') || normalizedAccept.includes('.wav')) {
    acceptLabel = 'MP3';
  } else if (normalizedAccept.includes('video/') || normalizedAccept.includes('.mp4') || normalizedAccept.includes('.mov') || normalizedAccept.includes('.webm')) {
    if (normalizedAccept.includes('quicktime') || normalizedAccept.includes('.mov')) {
      acceptLabel = normalizedAccept.includes('webm') || normalizedAccept === 'video/*' ? 'MP4 / MOV / WEBM' : 'MP4 / MOV';
    } else {
      acceptLabel = 'MP4 / MOV / WEBM';
    }
  }
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

function refreshBalanceUi() {
  const balanceValueEl = document.getElementById('balanceValue');
  if (balanceValueEl) balanceValueEl.textContent = state.balance == null ? '—' : `${state.balance} ток.`;
  const balanceHintEl = document.getElementById('balanceHint');
  if (balanceHintEl) balanceHintEl.textContent = state.authToken && state.me ? 'Данные из Личного Кабинета' : 'вход через Telegram или по почте';
}

async function loadBalance(options = {}) {
  const { silent = false, renderNow = false } = options;
  if (!requireAuth()) return;
  try {
    const res = await apiFetch('/api/workspace/balance');
    const data = await res.json();
    state.balance = Number(data.balance_tokens || 0);
    refreshBalanceUi();
    if (!silent) {
      toast('success', 'Баланс обновлён', `Текущий баланс: ${state.balance} ток.`);
      render();
    } else if (renderNow) {
      renderHeader();
      renderAuthCard();
    }
  } catch (e) {
    if (!silent) toast('error', 'Не удалось получить баланс', String(e.message || e));
  }
}

async function loadBalanceHistory(options = {}) {
  const { silent = true, force = false, renderNow = false } = options;
  if (!state.authToken || !state.me) return false;
  if (state.balanceHistory.loading) return false;
  if (state.balanceHistory.loaded && !force) {
    if (renderNow && state.studio === 'profile') renderInspector();
    return true;
  }

  state.balanceHistory.loading = true;
  state.balanceHistory.lastError = '';
  if (renderNow && state.studio === 'profile') renderInspector();

  try {
    const qs = new URLSearchParams({ limit: String(state.balanceHistory.limit || 30) });
    const res = await apiFetch(`/api/workspace/balance/history?${qs.toString()}`);
    const data = await res.json();
    state.balanceHistory.items = Array.isArray(data.items) ? data.items : [];
    state.balanceHistory.loaded = true;
    if (typeof data.balance_tokens !== 'undefined') state.balance = Number(data.balance_tokens || 0);
    refreshBalanceUi();
    if (state.studio === 'profile') renderInspector();
    return true;
  } catch (e) {
    state.balanceHistory.items = [];
    state.balanceHistory.loaded = false;
    state.balanceHistory.lastError = String(e.message || e);
    if (!silent) toast('error', 'Не удалось получить историю баланса', state.balanceHistory.lastError);
    if (state.studio === 'profile') renderInspector();
    return false;
  } finally {
    state.balanceHistory.loading = false;
    if (renderNow && state.studio === 'profile') renderInspector();
  }
}


async function bindPendingPartnerRef() {
  if (!state.authToken || !state.me) return false;
  let code = '';
  try { code = normalizePartnerRefCode(localStorage.getItem(PARTNER_REF_KEY) || ''); } catch {}
  if (!code) return false;
  try {
    await apiFetch('/api/partner/referral/bind', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ref_code: code, source: 'site' }),
    });
    localStorage.removeItem(PARTNER_REF_KEY);
    return true;
  } catch (e) {
    console.warn('partner referral bind failed', e);
    return false;
  }
}

async function loadPartnerDashboard(options = {}) {
  const { silent = true, force = false, renderNow = false } = options;
  if (!state.authToken || !state.me) return false;
  if (state.partner.loading) return false;
  if (state.partner.loaded && !force) {
    if (renderNow && state.studio === 'partner') render();
    return true;
  }
  state.partner.loading = true;
  state.partner.lastError = '';
  if (renderNow && state.studio === 'partner') render();
  try {
    const res = await apiFetch('/api/partner/me');
    const data = await res.json();
    state.partner.dashboard = data;
    state.partner.loaded = true;
    try { localStorage.setItem('astrabot:partnerState', JSON.stringify({ dashboard: data })); } catch {}
    if (state.studio === 'partner') render();
    return true;
  } catch (e) {
    state.partner.lastError = String(e.message || e);
    state.partner.loaded = false;
    if (!silent) toast('error', 'Партнёрка недоступна', state.partner.lastError);
    if (state.studio === 'partner') render();
    return false;
  } finally {
    state.partner.loading = false;
  }
}

async function submitPartnerPayout() {
  if (!requireAuth()) return;
  const amount = Number(document.getElementById('partnerPayoutAmount')?.value || '0');
  const card = (document.getElementById('partnerPayoutCard')?.value || '').trim();
  const name = (document.getElementById('partnerPayoutName')?.value || '').trim();
  const comment = (document.getElementById('partnerPayoutComment')?.value || '').trim();
  if (!Number.isFinite(amount) || amount <= 0) { toast('error', 'Укажи сумму', 'Минимальная сумма вывода — 1000 ₽.'); return; }
  if (card.replace(/\D/g, '').length < 12) { toast('error', 'Проверь карту', 'Номер карты слишком короткий.'); return; }
  if (name.length < 5) { toast('error', 'Проверь ФИО', 'Укажи ФИО получателя.'); return; }
  state.partner.payoutSending = true;
  render();
  try {
    await apiFetch('/api/partner/payouts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ amount_rub: amount, card_number: card, card_holder_name: name, comment }),
    });
    toast('success', 'Заявка отправлена', 'Выплата будет обработана до 3 рабочих дней.');
    state.partner.loaded = false;
    await loadPartnerDashboard({ silent: true, force: true, renderNow: true });
  } catch (e) {
    toast('error', 'Не удалось отправить заявку', String(e.message || e));
  } finally {
    state.partner.payoutSending = false;
    if (state.studio === 'partner') render();
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

    if (selected) {
      const selectedIdText = String(selected.id || '').trim();
      const currentGenerationId = String(state.voice.generationId || '').trim();
      const selectedStatus = voiceHistoryStatus(selected);
      const isCurrentPending = !!currentGenerationId && currentGenerationId === selectedIdText && ['queued', 'processing', 'running'].includes(selectedStatus);

      if (voiceHistoryCanHydrateWorkspace(selected) && (!state.voice.audioUrl || selectId || currentGenerationId !== selectedIdText)) {
        applyVoiceHistoryItemToWorkspace(selected, { silent: true });
        return;
      }

      if (isCurrentPending) {
        state.voice.errorText = selected.error_message || '';
        state.voice.lastGeneratedAt = selected.completed_at || selected.created_at || state.voice.lastGeneratedAt;
        state.voice.isGenerating = true;
      }
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
    if (state.prompts.selectedCategory && !state.prompts.categories.some((item) => item.slug === state.prompts.selectedCategory)) {
      state.prompts.selectedCategory = '';
      state.prompts.groups = [];
      state.prompts.selectedGroupId = '';
      state.prompts.items = [];
      state.prompts.openItemId = '';
    }
    render();
  } catch (e) {
    toast('error', 'Не удалось загрузить категории', String(e.message || e));
  } finally {
    state.prompts.loading = false;
  }
}

async function loadPromptGroups(category) {
  if (!category) return;
  state.prompts.selectedCategory = category;
  state.prompts.selectedGroupId = '';
  state.prompts.items = [];
  state.prompts.openItemId = '';
  try {
    const res = await apiFetch(`/api/workspace/prompts/groups?category=${encodeURIComponent(category)}`);
    const data = await res.json();
    state.prompts.groups = data.items || [];
    if (shouldAutoOpenSinglePromptGroup(category, state.prompts.groups)) {
      const firstGroupId = state.prompts.groups[0]?.id;
      if (firstGroupId) {
        await loadPromptItems(firstGroupId);
        return;
      }
    }
    render();
  } catch (e) {
    toast('error', 'Не удалось загрузить группы', String(e.message || e));
  }
}

async function loadPromptItems(groupId) {
  if (!groupId) return;
  state.prompts.selectedGroupId = groupId;
  state.prompts.openItemId = '';
  try {
    const res = await apiFetch(`/api/workspace/prompts/items?group_id=${encodeURIComponent(groupId)}`);
    const data = await res.json();
    state.prompts.items = data.items || [];
    render();
  } catch (e) {
    toast('error', 'Не удалось загрузить элементы', String(e.message || e));
  }
}


async function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function pollWorkspaceChatJob(jobId) {
  const cleanJobId = String(jobId || '').trim();
  if (!cleanJobId) throw new Error('Пустой job_id чата.');
  for (let attempt = 0; attempt < 160; attempt += 1) {
    await sleep(attempt < 10 ? 1200 : 2000);
    const res = await apiFetch(`/api/workspace/chat/status/${encodeURIComponent(cleanJobId)}`);
    const data = await res.json();
    const status = String(data.status || '').toLowerCase();
    if (status === 'completed') return data;
    if (status === 'failed') throw new Error(data.error || 'Чат-воркер вернул ошибку.');
  }
  throw new Error('Чат долго не отвечает. Проверь worker_chat.py и очередь Redis.');
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
  const pendingMessage = { role: 'system', content: '⏳ Думаю...', isPrompt: false };
  state.chat.messages.push(pendingMessage);
  state.chat.input = '';
  render();
  scrollChatToBottom();
  saveState();

  const replacePending = (message) => {
    const idx = state.chat.messages.indexOf(pendingMessage);
    if (idx >= 0) state.chat.messages.splice(idx, 1, message);
    else state.chat.messages.push(message);
  };

  try {
    const history = state.chat.messages.filter((m) => m.role === 'user' || m.role === 'assistant').slice(-12);
    let res;

    if (attachments.length) {
      const form = new FormData();
      form.append('text', outgoing);
      form.append('history', JSON.stringify(history));
      form.append('summary', state.chat.summary || '');
      form.append('model', state.chat.model);
      form.append('mode', state.chat.mode);
      form.append('temperature', String(state.chat.temperature));
      form.append('max_tokens', String(state.chat.maxTokens));
      attachments.forEach((item) => {
        if (item?.file) form.append('files', item.file, item.name || item.file.name || 'file');
      });
      res = await apiFetch('/api/workspace/chat/async', {
        method: 'POST',
        body: form,
      });
    } else {
      res = await apiFetch('/api/workspace/chat/async', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          text: outgoing,
          history,
          summary: state.chat.summary || '',
          model: state.chat.model,
          mode: state.chat.mode,
          temperature: state.chat.temperature,
          max_tokens: state.chat.maxTokens,
        }),
      });
    }

    let data = await res.json();
    if (data.job_id && String(data.status || '').toLowerCase() !== 'completed') {
      data = await pollWorkspaceChatJob(data.job_id);
    }
    if (typeof data.summary === 'string') state.chat.summary = data.summary;
    replacePending({ role: 'assistant', content: data.answer || 'Пустой ответ.', isPrompt: data.is_prompt !== false });
    pushRun({ studio: 'ChatGPT', title: `Chat · ${state.chat.mode === 'prompt_builder' ? 'Prompt Builder' : 'Chat'}`, summary: (outgoing || filePreview).slice(0, 100) });
    clearChatAttachments();
    render();
    scrollChatToBottom();
    saveState();
  } catch (e) {
    state.chat.input = outgoing;
    replacePending({ role: 'system', content: `Ошибка: ${String(e.message || e)}`, isPrompt: false });
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
  const imageUrls = imageHistoryUrls(selected);
  state.image.outputUrl = imageUrl;
  state.image.downloadUrl = imageUrl;
  state.image.beforeImageUrl = selected.before_image_url || selected.source_image_url || '';
  state.image.afterImageUrl = selected.after_image_url || imageUrl;
  state.image.imageUrls = imageUrls;
  state.image.availableActions = imageHistoryAvailableActions(selected);
  state.image.activeImageIndex = 0;
  state.image.compareMode = !!selected.compare_mode;
  state.image.comparePosition = 50;
  if (selected.preset_slug) state.image.upscalePreset = selected.preset_slug;
  if (selected.provider === 'midjourney') {
    state.image.provider = 'midjourney';
    state.image.model = 'midjourney-v7';
    state.image.mode = 'text_to_image';
    state.image.negativePrompt = selected.negative_prompt || '';
    state.image.mjStylize = Number.isFinite(Number(selected.mj_stylize)) ? Number(selected.mj_stylize) : state.image.mjStylize;
    state.image.mjChaos = Number.isFinite(Number(selected.mj_chaos)) ? Number(selected.mj_chaos) : state.image.mjChaos;
    state.image.mjRaw = !!selected.mj_raw;
    state.image.mjSpeedMode = selected.mj_speed_mode || state.image.mjSpeedMode || 'fast';
    state.image.mjSeed = selected.mj_seed || '';
    state.image.aspectRatio = selected.aspect_ratio || state.image.aspectRatio || '1:1';
  } else if (selected.provider === 'gpt_image_2') {
    state.image.provider = 'gpt_image_2';
    state.image.model = 'gpt-image-2';
    state.image.mode = selected.mode === 'image_to_image' ? 'image_to_image' : 'text_to_image';
    state.image.aspectRatio = selected.aspect_ratio || state.image.aspectRatio || (state.image.mode === 'image_to_image' ? 'match_input_image' : '1:1');
  } else if (selected.provider === 'posters') {
    state.image.provider = 'gpt_image_2';
    state.image.model = 'gpt-image-2';
    state.image.mode = 'image_to_image';
    state.image.aspectRatio = selected.aspect_ratio || state.image.aspectRatio || 'match_input_image';
  }
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
  if (!(state.video.model === 'kling-3.0-new' && state.video.mode === 'multi_shot') && !state.video.prompt.trim()) {
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
  if (state.video.provider === 'grok') form.append('provider_mode', String(state.video.providerMode || 'normal'));

  if (state.video.model === 'kling-3.0-new') {
    form.set('resolution', normalizeKling3NewModeValue(state.video.resolution || 'std'));
    if (state.video.mode === 'multi_shot') {
      const shots = getKling3NewShots().filter((shot) => String(shot.prompt || '').trim());
      const totalSeconds = shots.reduce((sum, shot) => sum + Math.max(1, Math.min(12, Number(shot.duration || 3) || 3)), 0);
      if (shots.length < 2) {
        toast('error', 'Нужны шоты', 'Для Multi-shot нужно минимум 2 заполненных shot prompt.');
        return;
      }
      if (totalSeconds < 3 || totalSeconds > 15) {
        toast('error', 'Проверь длительность', 'Сумма shot duration должна быть от 3 до 15 секунд.');
        return;
      }
      form.set('prompt', shots.map((shot, idx) => `Shot ${idx + 1}: ${shot.prompt}`).join('\n'));
      form.set('duration', String(totalSeconds));
      form.append('multi_shots_json', JSON.stringify(shots.map((shot) => ({ prompt: shot.prompt, duration: Number(shot.duration || 3) || 3 }))));
    }
    const kling3NewElements = getKling3NewElements().filter((el) => el.name).map((el) => ({
      name: el.name,
      description: el.description,
      element_input_urls: String(el.image_urls_text || '').split(/\r?\n/).map((x) => x.trim()).filter(Boolean),
      element_input_video_urls: el.video_url ? [el.video_url] : [],
    }));
    for (const el of kling3NewElements) {
      if (!el.element_input_video_urls.length && el.element_input_urls.length && (el.element_input_urls.length < 2 || el.element_input_urls.length > 4)) {
        toast('error', 'Проверь element', `@${el.name}: image element должен содержать 2–4 фото.`);
        return;
      }
    }
    form.append('kling_elements_json', JSON.stringify(kling3NewElements));
  }

  const startFrame = getFile('video.startFrame');
  const endFrame = getFile('video.endFrame');
  const lastFrame = getFile('video.lastFrame');
  const avatarImage = getFile('video.avatarImage');
  const motionVideo = getFile('video.motionVideo');
  const sourceVideo = getFile('video.sourceVideo');
  const refs = getFile('video.referenceImages');
  const hasManualRef = Array.isArray(refs) && refs.some((item) => item?.file);

  if (state.video.provider === 'switchx') {
    if (!sourceVideo?.file && !state.video.switchxSourceUploadId) {
      toast('error', 'Добавь исходное видео', 'Для SwitchX нужно исходное видео.');
      return;
    }
    if (!state.video.switchxReferenceImageUrl && !hasManualRef) {
      toast('error', 'Нужен reference image', 'Загрузи референс вручную или сначала создай AI-референс.');
      return;
    }
    const switchxAlphaMode = ['auto', 'fill'].includes(String(state.video.switchxAlphaMode || '').toLowerCase())
      ? String(state.video.switchxAlphaMode).toLowerCase()
      : 'auto';
    form.append('switchx_alpha_mode', switchxAlphaMode);
    if (switchxAlphaMode === 'select') {
      let selectMask = getFile('video.switchxSelectMask');
      if (!selectMask?.file) {
        toast('error', 'Нужна select mask', 'Для режима Select загрузи PNG/JPG маску 1-го кадра.');
        return;
      }
      try {
        const editor = runtime.switchxMaskEditor || {};
        if ((!editor.frameWidth || !editor.frameHeight) && sourceVideo?.url) {
          try { await ensureSwitchxMaskEditorFrame(); } catch (_e) {}
        }
        const targetWidth = Number(editor.frameWidth || 0);
        const targetHeight = Number(editor.frameHeight || 0);
        const normalizedMaskFile = await switchxMaskSourceToGrayscaleFile(selectMask.url, targetWidth, targetHeight, selectMask.name || selectMask.file.name || 'switchx_select_mask.png');
        switchxMaskEditorSetGeneratedFile(normalizedMaskFile);
        selectMask = getFile('video.switchxSelectMask') || selectMask;
      } catch (_e) {}
      form.append('switchx_select_mask', selectMask.file, selectMask.name || selectMask.file.name || 'select_mask.png');
    }
    if (state.video.switchxSourceUploadId) form.append('source_video_upload_id', state.video.switchxSourceUploadId);
    if (sourceVideo?.file && !state.video.switchxSourceUploadId) form.append('source_video', sourceVideo.file, sourceVideo.name || sourceVideo.file.name || 'source.mp4');
    if (!hasManualRef && state.video.switchxReferenceImageUrl) form.append('reference_image_url', state.video.switchxReferenceImageUrl);
  }
  if (state.video.provider === 'pixverse_c1') {
    const pixVerseRefs = Array.isArray(refs) ? refs.filter((item) => item?.file) : [];
    if (state.video.mode === 'image_to_video' && !startFrame?.file) {
      toast('error', 'Нужен start frame', 'Для PixVerse C1 Image → Video нужен стартовый кадр.');
      return;
    }
    if (state.video.mode === 'transition' && (!startFrame?.file || !lastFrame?.file)) {
      toast('error', 'Нужны 2 кадра', 'Для PixVerse C1 Transition нужны первый и последний кадр.');
      return;
    }
    if (state.video.mode === 'fusion' && !pixVerseRefs.length) {
      toast('error', 'Нужны референсы', 'Для PixVerse C1 Fusion загрузи хотя бы одно изображение.');
      return;
    }
    if (state.video.mode === 'fusion' && pixVerseRefs.length > 7) {
      toast('error', 'Слишком много референсов', 'Для PixVerse C1 Fusion доступно максимум 7 изображений.');
      return;
    }
  }
  if (videoModeUsesField('startFrame') && startFrame?.file && (state.video.provider !== 'seedance_kie' || state.video.seedanceUseStartFrame)) {
    form.append('start_frame', startFrame.file, startFrame.name || startFrame.file.name || 'start_frame');
  }
  if (videoModeUsesField('endFrame') && endFrame?.file) {
    form.append('end_frame', endFrame.file, endFrame.name || endFrame.file.name || 'end_frame');
  }
  if (videoModeUsesField('lastFrame') && lastFrame?.file && (state.video.provider !== 'seedance_kie' || state.video.seedanceUseLastFrame)) {
    form.append('last_frame', lastFrame.file, lastFrame.name || lastFrame.file.name || 'last_frame');
  }
  if (videoModeUsesField('avatarImage') && avatarImage?.file) {
    form.append('avatar_image', avatarImage.file, avatarImage.name || avatarImage.file.name || 'avatar_image');
  }
  if (videoModeUsesField('motionVideo') && motionVideo?.file) {
    form.append('motion_video', motionVideo.file, motionVideo.name || motionVideo.file.name || 'motion_video');
  }
  const audioRefs = getFile('video.referenceAudios');
  const videoRefs = getFile('video.referenceVideos');
  if (state.video.provider === 'seedance_kie' && state.video.mode === 'image_to_video') {
    const imageRefCount = (Array.isArray(refs) ? refs.length : 0) + (state.video.seedanceUseStartFrame && startFrame?.file ? 1 : 0) + (state.video.seedanceUseLastFrame && lastFrame?.file ? 1 : 0);
    const audioRefCount = Array.isArray(audioRefs) ? audioRefs.length : 0;
    if (!imageRefCount) {
      toast('error', 'Нужен image reference', 'Добавь хотя бы один референс, стартовый кадр или последний кадр.');
      return;
    }
    if (imageRefCount > 7) {
      toast('error', 'Слишком много изображений', 'Для Seedance 2.0 доступно максимум 7 изображений суммарно.');
      return;
    }
    if (audioRefCount > 3) {
      toast('error', 'Слишком много аудио', 'Для Seedance 2.0 доступно максимум 3 аудиофайла.');
      return;
    }
  }
  if (state.video.provider === 'seedance_kie' && state.video.mode === 'omni_reference') {
    const imageRefCount = Array.isArray(refs) ? refs.length : 0;
    const audioRefCount = Array.isArray(audioRefs) ? audioRefs.length : 0;
    const videoRefCount = Array.isArray(videoRefs) ? videoRefs.length : 0;
    const totalRefCount = imageRefCount + audioRefCount + videoRefCount;
    if (!totalRefCount) {
      toast('error', 'Нужны референсы', 'Для Omni Reference добавь хотя бы один image, video или audio reference.');
      return;
    }
    if (totalRefCount > 12) {
      toast('error', 'Слишком много референсов', 'Для Seedance 2.0 Omni Reference доступно максимум 12 refs суммарно.');
      return;
    }
    if (audioRefCount > 3) {
      toast('error', 'Слишком много аудио', 'Для Seedance 2.0 доступно максимум 3 аудиофайла.');
      return;
    }
    if (audioRefCount && !imageRefCount && !videoRefCount) {
      toast('error', 'Audio-only не поддерживается', 'Для Omni Reference вместе с audio нужен хотя бы один image или video reference.');
      return;
    }
  }
  if (videoModeUsesField('referenceImages') && Array.isArray(refs)) refs.forEach((item) => item?.file && form.append('reference_images', item.file, item.name || item.file.name || 'ref.jpg'));
  if (state.video.provider === 'seedance_kie' && Array.isArray(audioRefs) && ['image_to_video', 'omni_reference'].includes(state.video.mode)) audioRefs.forEach((item) => item?.file && form.append('reference_audios', item.file, item.name || item.file.name || 'ref_audio'));
  if (state.video.provider === 'seedance_kie' && state.video.mode === 'omni_reference' && Array.isArray(videoRefs)) videoRefs.forEach((item) => item?.file && form.append('reference_videos', item.file, item.name || item.file.name || 'ref_video'));

  state.video.isGenerating = true;
  state.video.outputUrl = '';
  state.video.downloadUrl = '';
  state.video.coverUrl = '';
  state.video.percent = null;
  state.video.generationId = '';
  state.video.providerTaskId = '';
  state.video.errorText = '';
  state.video.lastStatus = 'submitted';
  state.video.statusText = 'Задача отправлена. Видео появится в рабочей зоне автоматически.';
  state.video.requestStartedAt = new Date().toISOString();
  saveState();
  render();

  try {
    const res = await apiFetch('/api/workspace/video/run', { method: 'POST', body: form });
    const data = await res.json();
    state.video.generationId = data.generation_id || '';
    state.video.providerTaskId = data.task_id || data.generation_id || '';
    if (!state.video.generationId) {
      state.video.isGenerating = false;
      state.video.lastStatus = 'error';
      state.video.errorText = String(data?.detail || data?.error || data?.message || 'Backend не вернул generation_id.');
      state.video.statusText = 'Не удалось запустить генерацию.';
      state.video.requestStartedAt = '';
      saveState();
      render();
      loadBalance({ silent: true, renderNow: true }).catch(() => {});
      toast('error', 'Ошибка запуска', state.video.errorText);
      return;
    }
    state.video.statusText = data.status_text || 'Генерация началась.';
    if (data.source_video_upload_id) state.video.switchxSourceUploadId = data.source_video_upload_id;
    if (state.video.provider === 'switchx' && hasManualRef) {
      state.video.switchxReferenceImageUrl = '';
      state.video.switchxRefGenerationId = '';
      state.video.switchxReferenceStatus = 'idle';
    } else if (state.video.provider === 'switchx' && data.reference_image_url) {
      state.video.switchxReferenceImageUrl = data.reference_image_url;
    }
    if (typeof data.balance_tokens !== 'undefined') state.balance = Number(data.balance_tokens || 0);
    pushRun({ studio: 'Video', title: `${currentMeta().provider} · ${currentMeta().model}`, summary: state.video.prompt.slice(0, 120) });
    saveState();
    startVideoPolling({ immediate: true });
    loadBalance({ silent: true, renderNow: true }).catch(() => {});
    toast('success', 'Запуск выполнен', data.status_text || 'Генерация началась.');
  } catch (e) {
    stopVideoPolling();
    state.video.isGenerating = false;
    state.video.requestStartedAt = '';
    state.video.generationId = '';
    state.video.providerTaskId = '';
    state.video.percent = null;
    state.video.errorText = String(e.message || e);
    state.video.lastStatus = 'error';
    state.video.statusText = 'Не удалось запустить генерацию.';
    loadBalance({ silent: true, renderNow: true }).catch(() => {});
    toast('error', 'Ошибка запуска', state.video.errorText);
  } finally {
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
  const sourceInputValue = getFile('image.sourceImage');
  const sourceItems = Array.isArray(sourceInputValue) ? sourceInputValue.filter((item) => item?.file) : (sourceInputValue?.file ? [sourceInputValue] : []);

  if (imageNeedsSourceImage() && !sourceItems.length) {
    const sourceHint = state.image.provider === 'nano_banana_pro_new'
      ? 'Для Nano Banana Pro - NEW сначала загрузи хотя бы один reference image.'
      : (state.image.provider === 'gpt_image_2'
        ? 'Для GPT Image 2.0 Image → Image сначала загрузи от 1 до 4 reference images.'
        : 'Для выбранного режима сначала загрузи source image.');
    toast('error', 'Нужно изображение', sourceHint);
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
  if (state.image.provider === 'midjourney') {
    form.append('negative_prompt', String(state.image.negativePrompt || ''));
    form.append('mj_stylize', String(state.image.mjStylize || 100));
    form.append('mj_chaos', String(state.image.mjChaos || 0));
    form.append('mj_raw', state.image.mjRaw ? '1' : '0');
    form.append('mj_speed_mode', String(state.image.mjSpeedMode || 'fast'));
  }

  const source = getFile('image.sourceImage');
  const base = getFile('image.baseImage');
  if (imageNeedsSourceImage()) {
    if (state.image.provider === 'nano_banana_pro_new') {
      sourceItems.slice(0, 8).forEach((item, index) => {
        form.append('source_image', item.file, item.name || item.file?.name || `reference_${index + 1}.png`);
      });
    } else if (state.image.provider === 'gpt_image_2') {
      sourceItems.slice(0, 4).forEach((item, index) => {
        form.append('source_image', item.file, item.name || item.file?.name || `reference_${index + 1}.png`);
      });
    } else if (source?.file) {
      form.append('source_image', source.file, source.name || source.file.name || 'source.png');
    }
  }
  if (imageNeedsBaseImage() && base?.file) form.append('base_image', base.file, base.name || base.file.name || 'base.png');
  if (state.image.provider === 'midjourney') {
    const styleRef = getFile('image.styleRefImage');
    const omniRef = getFile('image.omniRefImage');
    if (styleRef?.file) form.append('style_ref_image', styleRef.file, styleRef.name || styleRef.file.name || 'style_ref.png');
    if (omniRef?.file) form.append('omni_ref_image', omniRef.file, omniRef.name || omniRef.file.name || 'omni_ref.png');
  }

  state.image.isGenerating = true;
  state.image.outputUrl = '';
  state.image.downloadUrl = '';
  state.image.beforeImageUrl = '';
  state.image.afterImageUrl = '';
  state.image.imageUrls = [];
  state.image.availableActions = {};
  state.image.activeImageIndex = 0;
  state.image.compareMode = false;
  state.image.comparePosition = 50;
  state.image.generationId = '';
  state.image.errorText = '';
  state.image.requestStartedAt = new Date().toISOString();
  state.image.statusText = 'Задача отправлена. Жди итоговую картинку в рабочей зоне.';
  saveState();
  render();

  try {
    const res = await apiFetch('/api/workspace/image/run', { method: 'POST', body: form });
    const data = await res.json();
    state.image.generationId = data.generation_id || '';
    state.image.outputUrl = '';
    state.image.downloadUrl = '';
    state.image.beforeImageUrl = '';
    state.image.afterImageUrl = '';
    state.image.imageUrls = [];
    state.image.availableActions = {};
    state.image.activeImageIndex = 0;
    state.image.compareMode = false;
    state.image.comparePosition = 50;
    if (data.preset_slug) state.image.upscalePreset = data.preset_slug;
    if (typeof data.balance_tokens !== 'undefined') state.balance = Number(data.balance_tokens || 0);
    state.image.statusText = data.status_text || 'Изображение поставлено в очередь.';
    state.image.panel = 'params';
    pushRun({ studio: 'Image', title: `${currentMeta().provider} · ${currentMeta().model}`, summary: prompt.slice(0, 120) });
    if (state.image.generationId) {
      startImagePolling({ immediate: true });
      loadImageHistory({ silent: true, keepSelection: true, selectId: state.image.generationId }).catch(() => {});
    }
    loadBalance({ silent: true, renderNow: true }).catch(() => {});
    toast('success', 'Запуск выполнен', state.image.statusText || 'Изображение поставлено в очередь.');
  } catch (e) {
    state.image.requestStartedAt = '';
    state.image.errorText = String(e.message || e);
    state.image.statusText = 'Не удалось выполнить генерацию.';
    loadBalance({ silent: true, renderNow: true }).catch(() => {});
    toast('error', 'Ошибка генерации', state.image.errorText);
  } finally {
    state.image.isGenerating = false;
    saveState();
    render();
  }
}

async function runMidjourneyAction(action, options = {}) {
  if (!requireAuth()) return;
  const actionName = String(action || '').trim().toLowerCase();
  if (!['reroll', 'variation'].includes(actionName)) return;
  const sourceGenerationId = String((state.image.panel === 'library' ? imageHistorySelectedItem()?.id : '') || state.image.generationId || '').trim();
  if (!sourceGenerationId) {
    toast('error', 'Нет исходной генерации', 'Сначала дождись готового Midjourney результата.');
    return;
  }
  state.image.isGenerating = true;
  state.image.errorText = '';
  state.image.outputUrl = '';
  state.image.downloadUrl = '';
  state.image.imageUrls = [];
  state.image.availableActions = {};
  state.image.activeImageIndex = 0;
  state.image.statusText = actionName === 'reroll' ? 'Запускаю Midjourney reroll...' : 'Запускаю Midjourney variation...';
  saveState();
  render();

  try {
    const body = {
      generation_id: sourceGenerationId,
      action: actionName,
      image_no: actionName === 'variation' ? Number(options.imageIndex || 0) : null,
      variation_type: actionName === 'variation' ? String(options.variationType || 'subtle') : null,
      speed_mode: String(state.image.mjSpeedMode || 'fast'),
    };
    const res = await apiFetch('/api/workspace/image/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    state.image.generationId = data.generation_id || '';
    state.image.statusText = data.status_text || 'Midjourney задача поставлена в очередь.';
    if (typeof data.balance_tokens !== 'undefined') state.balance = Number(data.balance_tokens || 0);
    startImagePolling({ immediate: true });
    loadImageHistory({ silent: true, keepSelection: true, selectId: state.image.generationId }).catch(() => {});
    loadBalance({ silent: true, renderNow: true }).catch(() => {});
    toast('success', 'Midjourney запущен', state.image.statusText);
  } catch (e) {
    state.image.errorText = String(e.message || e);
    state.image.statusText = 'Не удалось запустить Midjourney action.';
    toast('error', 'Ошибка Midjourney', state.image.errorText);
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
      state.video.isGenerating = false;
      state.video.outputUrl = readyUrl;
      state.video.downloadUrl = historyVideoDownloadUrl(item) || readyUrl;
      state.video.percent = 100;
      state.video.requestStartedAt = '';
      syncVideoEditorWithHistoryItem(item);
      stopVideoPolling();
      saveState();
      render();
      loadBalance({ silent: true, renderNow: true }).catch(() => {});
      if (!silent) toast('success', 'Видео готово', 'Результат появился в рабочей зоне.');
      return;
    }

    if (isVideoTaskFailed(state.video.lastStatus)) {
      state.video.isGenerating = false;
      state.video.requestStartedAt = '';
      stopVideoPolling();
      saveState();
      render();
      loadBalance({ silent: true, renderNow: true }).catch(() => {});
      if (!silent) toast('error', 'Ошибка генерации', state.video.errorText || 'Провайдер вернул ошибку.');
      return;
    }

    state.video.isGenerating = true;
    saveState();
    if (!silent) render();
  } catch (e) {
    if (!silent) toast('error', 'Не удалось проверить статус', String(e.message || e));
  }
}

async function pollImageTask(options = {}) {
  const { silent = false } = options;
  if (!state.image.generationId || !state.authToken) return null;
  try {
    const item = await loadImageHistoryItem(state.image.generationId, { silent: true });
    if (!item) return null;
    const status = String(item.status || 'processing').toLowerCase();
    const imageUrls = imageHistoryUrls(item);
    const imageUrl = imageUrls[0] || imageHistoryUrl(item) || item.after_image_url || item.image_url || '';
    state.image.errorText = item.error_message || '';
    state.image.statusText = item.error_message || (status === 'completed' ? 'Изображение готово и восстановлено в рабочей зоне.' : 'Изображение ещё собирается. Рабочая зона будет восстановлена автоматически.');

    if (status === 'completed' && imageUrl) {
      stopImagePolling();
      state.image.isGenerating = false;
      state.image.requestStartedAt = '';
      state.image.outputUrl = imageUrl;
      state.image.downloadUrl = imageUrl;
      state.image.beforeImageUrl = item.before_image_url || item.source_image_url || '';
      state.image.afterImageUrl = item.after_image_url || imageUrl;
      state.image.imageUrls = imageUrls;
      state.image.availableActions = imageHistoryAvailableActions(item);
      state.image.activeImageIndex = 0;
      state.image.compareMode = !!item.compare_mode;
      state.image.comparePosition = 50;
      state.image.prompt = item.prompt || state.image.prompt;
      if (item.provider === 'midjourney') {
        state.image.provider = 'midjourney';
        state.image.model = 'midjourney-v7';
        state.image.mode = 'text_to_image';
        state.image.negativePrompt = item.negative_prompt || state.image.negativePrompt || '';
        state.image.mjStylize = Number.isFinite(Number(item.mj_stylize)) ? Number(item.mj_stylize) : state.image.mjStylize;
        state.image.mjChaos = Number.isFinite(Number(item.mj_chaos)) ? Number(item.mj_chaos) : state.image.mjChaos;
        state.image.mjRaw = !!item.mj_raw;
        state.image.mjSpeedMode = item.mj_speed_mode || state.image.mjSpeedMode || 'fast';
        state.image.mjSeed = item.mj_seed || state.image.mjSeed || '';
        state.image.aspectRatio = item.aspect_ratio || state.image.aspectRatio || '1:1';
      }
      if (item.preset_slug) state.image.upscalePreset = item.preset_slug;
      state.image.panel = 'params';
      saveState();
      render();
      loadBalance({ silent: true, renderNow: true }).catch(() => {});
      if (!silent) toast('success', 'Изображение готово', 'Результат появился в рабочей зоне.');
      return item;
    }

    if (status === 'completed' && !imageUrl) {
      state.image.isGenerating = true;
      state.image.statusText = 'Провайдер уже завершил генерацию. Подтягиваем файл в рабочую зону.';
      saveState();
      if (!silent) render();
      return item;
    }

    if (['failed', 'error', 'cancelled', 'canceled'].includes(status)) {
      stopImagePolling();
      state.image.isGenerating = false;
      state.image.requestStartedAt = '';
      saveState();
      render();
      loadBalance({ silent: true, renderNow: true }).catch(() => {});
      if (!silent) toast('error', 'Ошибка генерации', state.image.errorText || 'Провайдер вернул ошибку.');
      return item;
    }

    state.image.isGenerating = true;
    saveState();
    if (!silent) render();
    return item;
  } catch (e) {
    if (!silent) toast('error', 'Не удалось проверить статус изображения', String(e.message || e));
    return null;
  }
}


async function pollVoiceTask(options = {}) {
  const { silent = false } = options;
  if (!state.voice.generationId || !state.authToken) return null;
  try {
    const item = await loadVoiceHistoryItem(state.voice.generationId, { silent: true });
    if (!item) return null;
    const status = String(item.status || 'processing').toLowerCase();
    state.voice.errorText = item.error_message || '';

    if (status === 'completed' && voiceHistoryAudioUrl(item)) {
      stopVoicePolling();
      applyVoiceHistoryItemToWorkspace(item, { silent: true });
      saveState();
      render();
      if (!silent) toast('success', 'Аудио готово', 'Файл сгенерирован и сохранён в истории.');
      return item;
    }

    if (['failed', 'error', 'cancelled', 'canceled'].includes(status)) {
      stopVoicePolling();
      state.voice.isGenerating = false;
      saveState();
      render();
      if (!silent) toast('error', 'TTS error', state.voice.errorText || 'Провайдер вернул ошибку.');
      return item;
    }

    state.voice.isGenerating = true;
    saveState();
    if (!silent) render();
    return item;
  } catch (e) {
    if (!silent) toast('error', 'Не удалось проверить статус озвучки', String(e.message || e));
    return null;
  }
}

async function runVoice() {
  state.voice.outputFormat = VOICE_FIXED_OUTPUT_FORMAT;
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
        output_format: VOICE_FIXED_OUTPUT_FORMAT,
        language_code: state.voice.languageCode || 'auto',
        manual_voice_settings: !!state.voice.manualVoiceSettings,
        stability: Number(state.voice.stability),
        similarity_boost: Number(state.voice.similarityBoost),
        style: Number(state.voice.style),
        speed: Number(state.voice.speed),
        use_speaker_boost: true,
      }),
    });
    const data = await res.json();
    revokeVoiceAudioUrl();
    state.voice.audioUrl = '';
    state.voice.downloadUrl = '';
    state.voice.generationId = data.generation_id || '';
    state.voice.lastGeneratedAt = data.created_at || new Date().toISOString();
    state.voice.isGenerating = true;
    state.voice.errorText = '';
    pushRun({ studio: 'Voice', title: 'TTS generate', summary: state.voice.text.slice(0, 100) });
    saveState();
    if (state.voice.generationId) {
      startVoicePolling({ immediate: true });
      loadVoiceHistory({ silent: true, keepSelection: true, selectId: state.voice.generationId }).catch(() => {});
    }
    toast('success', 'Запуск выполнен', data.status_text || 'Озвучка поставлена в очередь.');
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
    if (!silent) render();

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
      loadBalance({ silent: true, renderNow: true }).catch(() => {});
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
  ensureMusicCompatibility({ preserveLyricsTab: false });
  state.studio = 'music';
  saveState();
  render();
  toast('success', 'Музыка открыта', 'Запуск возвращён в музыкальную студию.');
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
  state.music.backend = state.music.ai === 'udio' ? 'piapi' : 'sunoapi';

  if (mode === 'idea' && !ideaText) {
    toast('error', 'Нужна идея', 'Заполни блок «Идея» перед генерацией.');
    setMusicTab('idea');
    return;
  }
  if (mode === 'lyrics' && !lyricsText) {
    toast('error', 'Нужен текст песни', 'Заполни «Текст песни» или собери его через генератор текста.');
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
        model: state.music.model,
        title: state.music.title,
        tags: state.music.tags,
        language: state.music.language,
        mood: state.music.mood,
        references: state.music.references,
        negative_tags: state.music.negativeTags,
        vocal_gender: state.music.vocalGender,
        style_weight: Number(state.music.styleWeight ?? 0.65),
        weirdness_constraint: Number(state.music.weirdnessConstraint ?? 0.65),
        audio_weight: Number(state.music.audioWeight ?? 0.65),
        persona_id: state.music.personaId,
        persona_model: state.music.personaModel,
        instrumental: !!state.music.instrumental,
        idea_text: ideaText,
        lyrics_text: lyricsText,
      }),
    });
    const data = await res.json();
    state.music.generationId = data.generation_id || '';
    state.music.status = data.status || 'queued';
    if (typeof data.balance_tokens !== 'undefined') state.balance = Number(data.balance_tokens || 0);
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
    loadBalance({ silent: true, renderNow: true }).catch(() => {});
  } catch (e) {
    state.music.isGenerating = false;
    state.music.status = 'failed';
    state.music.statusText = 'Не удалось запустить генерацию.';
    state.music.errorText = String(e.message || e);
    saveState();
    render();
    loadBalance({ silent: true, renderNow: true }).catch(() => {});
    toast('error', 'Music run error', state.music.errorText);
  }
}

async function runSongwriter() {
  ensureMusicCompatibility({ preserveLyricsTab: true });
  const userText = String(state.music.songwriter.input || '').trim() || musicSourceTextForSongwriter();
  if (!userText) {
    toast('error', 'Нужен текст', 'Опиши идею трека или задачу для генератора текста.');
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
    toast('success', 'GPT ответил', 'Ответ добавлен в блок помощника по тексту песни.');
  } catch (e) {
    state.music.songwriter.loading = false;
    saveState();
    render();
    toast('error', 'Songwriter error', String(e.message || e));
  }
}

function stopMusicToolPolling() {
  if (runtime.musicToolPollTimer) {
    clearTimeout(runtime.musicToolPollTimer);
    runtime.musicToolPollTimer = null;
  }
}

function startMusicToolPolling(taskId) {
  stopMusicToolPolling();
  const taskIdText = String(taskId || '').trim();
  if (!taskIdText) return;
  const tick = async () => {
    try {
      const res = await apiFetch(`/api/workspace/music/task-status?task_id=${encodeURIComponent(taskIdText)}`);
      const data = await res.json();
      const item = data.item || {};
      state.music.toolTaskId = item.task_id || taskIdText;
      state.music.toolTaskStatus = item.status || 'processing';
      state.music.toolTaskMessage = item.error_message || (item.status === 'completed' ? 'Результат готов.' : 'Задача ещё обрабатывается.');
      state.music.toolTracks = Array.isArray(item.tracks) ? item.tracks : [];
      saveState();
      const toolStatus = String(item.status || '').toLowerCase();
      const shouldRenderToolState = ['completed', 'failed', 'error', 'cancelled', 'canceled'].includes(toolStatus)
        || (state.studio === 'music' && state.music.activeTab === 'results');
      if (shouldRenderToolState) render();
      if (['completed', 'failed', 'error', 'cancelled', 'canceled'].includes(toolStatus)) {
        stopMusicToolPolling();
        if (item.status === 'completed') toast('success', 'Музыкальный инструмент готов', `Получено ${state.music.toolTracks.length || 0} ${musicTrackLabel(state.music.toolTracks.length || 0)}.`);
        else toast('error', 'Ошибка Suno tool', item.error_message || 'Задача завершилась ошибкой.');
        return;
      }
    } catch (e) {
      state.music.toolTaskMessage = String(e.message || e);
      saveState();
      if (state.studio === 'music' && state.music.activeTab === 'results') render();
    }
    runtime.musicToolPollTimer = setTimeout(tick, 5000);
  };
  runtime.musicToolPollTimer = setTimeout(tick, 2000);
}

async function runMusicLyricsGenerator() {
  if (!state.authToken) {
    toast('error', 'Нужна авторизация', 'Сначала войди через Telegram.');
    return;
  }
  const prompt = String(state.music.lyricsPrompt || '').trim();
  if (!prompt) {
    toast('error', 'Нужен prompt', 'Опиши, какой текст песни нужно сгенерировать.');
    return;
  }
  state.music.toolTaskMessage = 'Генерирую lyrics через Suno API...';
  saveState();
  render();
  try {
    const res = await apiFetch('/api/workspace/music/lyrics/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt }),
    });
    const data = await res.json();
    state.music.generatedLyrics = Array.isArray(data.items) ? data.items : [];
    if (data.text && !state.music.lyricsText) state.music.lyricsText = data.text;
    state.music.activeTab = 'tools';
    state.music.toolTaskMessage = 'Текст готов. Выбери вариант и вставь его в блок текста песни.';
    saveState();
    render();
    toast('success', 'Текст готов', 'Варианты текста добавлены в блок инструментов.');
  } catch (e) {
    toast('error', 'Lyrics error', String(e.message || e));
  }
}

async function loadMusicTimestampedLyrics(taskId, audioId) {
  const taskIdText = String(taskId || '').trim();
  const audioIdText = String(audioId || '').trim();
  if (!taskIdText || !audioIdText) {
    toast('error', 'Нет данных трека', 'Для таймкодов нужен taskId и audioId.');
    return;
  }
  try {
    const res = await apiFetch('/api/workspace/music/timestamped-lyrics', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_id: taskIdText, audio_id: audioIdText }),
    });
    const data = await res.json();
    state.music.timestampedLyrics[audioIdText] = data.data || {};
    saveState();
    render();
    toast('success', 'Таймкоды загружены', 'Данные для синхронного текста добавлены в карточку трека.');
  } catch (e) {
    toast('error', 'Timestamped lyrics error', String(e.message || e));
  }
}

async function generateMusicPersona(taskId, audioId) {
  const taskIdText = String(taskId || '').trim();
  const audioIdText = String(audioId || '').trim();
  if (!taskIdText || !audioIdText) {
    toast('error', 'Нет данных трека', 'Для persona нужен taskId и audioId.');
    return;
  }
  const name = String(state.music.personaName || '').trim() || String(state.music.title || '').trim() || 'Astra Persona';
  const description = String(state.music.personaDescription || '').trim() || String(state.music.references || '').trim() || 'Generated from current track';
  try {
    const res = await apiFetch('/api/workspace/music/persona/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        task_id: taskIdText,
        audio_id: audioIdText,
        name,
        description,
        vocal_start: 0,
        vocal_end: 30,
        style: state.music.tags,
      }),
    });
    const data = await res.json();
    state.music.personaResult = data.data || null;
    if (data.data && data.data.personaId) state.music.personaId = data.data.personaId;
    saveState();
    render();
    toast('success', 'Persona создана', data?.data?.personaId || 'Готово');
  } catch (e) {
    toast('error', 'Persona error', String(e.message || e));
  }
}

async function runMusicToolAction() {
  if (!state.authToken) {
    toast('error', 'Нужна авторизация', 'Сначала войди через Telegram.');
    return;
  }
  if (state.music.ai !== 'suno') {
    toast('error', 'Инструменты Suno', 'Эти действия доступны только для Suno.');
    return;
  }
  const action = String(state.music.toolAction || 'upload-cover');
  state.music.toolTaskStatus = 'processing';
  state.music.toolTaskMessage = 'Отправляю задачу в SunoAPI...';
  state.music.toolTracks = [];
  saveState();
  render();

  try {
    let res;
    const file = runtime.musicSourceFile || null;
    if (!file) {
      toast('error', 'Нужен аудиофайл', 'Выбери MP3/WAV перед запуском инструмента.');
      state.music.toolTaskStatus = 'idle';
      saveState();
      render();
      return;
    }
    const explicitToolPrompt = String(state.music.toolPrompt || '').trim();
    if (['upload-cover', 'add-vocals'].includes(action) && !explicitToolPrompt) {
      toast('error', 'Нужен текст', action === 'upload-cover' ? 'Впиши текст или описание для нового кавера.' : 'Впиши текст для вокала или описание нужного вокала.');
      state.music.toolTaskStatus = 'idle';
      saveState();
      render();
      return;
    }
    const fd = new FormData();
    fd.append('file', file, file.name || 'audio.bin');
    fd.append('prompt', ['upload-cover', 'add-vocals'].includes(action)
      ? explicitToolPrompt
      : (state.music.mode === 'lyrics' ? String(state.music.lyricsText || '') : String(state.music.ideaText || '')));
    fd.append('title', String(state.music.title || ''));
    fd.append('style', String(state.music.tags || ''));
    fd.append('model', String(state.music.model || 'V4_5'));
    fd.append('negative_tags', String(state.music.negativeTags || ''));
    fd.append('vocal_gender', String(state.music.vocalGender || ''));
    fd.append('style_weight', String(Number(state.music.styleWeight ?? 0.65)));
    fd.append('weirdness_constraint', String(Number(state.music.weirdnessConstraint ?? 0.65)));
    fd.append('audio_weight', String(Number(state.music.audioWeight ?? 0.65)));
    if (action === 'upload-cover') {
      fd.append('custom_mode', state.music.toolPromptMode === 'lyrics' ? 'true' : 'false');
      fd.append('instrumental', state.music.instrumental ? 'true' : 'false');
      fd.append('persona_id', String(state.music.personaId || ''));
      fd.append('persona_model', String(state.music.personaModel || 'style_persona'));
      res = await apiFetch('/api/workspace/music/upload-cover/start', { method: 'POST', body: fd });
    } else if (action === 'upload-extend') {
      fd.append('instrumental', state.music.instrumental ? 'true' : 'false');
      fd.append('continue_at', String(Number(state.music.continueAt || 0)));
      fd.append('persona_id', String(state.music.personaId || ''));
      fd.append('persona_model', String(state.music.personaModel || 'style_persona'));
      res = await apiFetch('/api/workspace/music/upload-extend/start', { method: 'POST', body: fd });
    } else {
      res = await apiFetch('/api/workspace/music/add-vocals/start', { method: 'POST', body: fd });
    }
    const data = await res.json();
    state.music.toolTaskId = data.task_id || '';
    state.music.toolTaskStatus = data.status || 'queued';
    state.music.toolTaskMessage = 'Задача принята. Ждём результат от SunoAPI.';
    state.music.activeTab = 'tools';
    saveState();
    render();
    if (state.music.toolTaskId) startMusicToolPolling(state.music.toolTaskId);
  } catch (e) {
    state.music.toolTaskStatus = 'failed';
    state.music.toolTaskMessage = String(e.message || e);
    saveState();
    render();
    toast('error', 'Music tool error', state.music.toolTaskMessage);
  }
}

function seedMusicSongwriter() {
  if (state.music.ai !== 'suno') {
    state.music.activeTab = 'idea';
    state.music.lastEditorTab = 'idea';
    saveState();
    render();
    return;
  }
  state.music.songwriter.messages = [];
  state.music.songwriter.lastAnswer = '';
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
  toast('success', 'Ответ вставлен', target === 'lyrics' ? 'Ответ GPT добавлен в текст песни.' : 'Ответ GPT добавлен в описание трека.');
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
      resetMusicWorkspaceStage();
      state.music.ideaText = '';
      state.music.lyricsText = '';
      state.music.songwriter.input = '';
      state.music.songwriter.messages = [];
      state.music.songwriter.lastAnswer = '';
      break;
    case 'workspace':
      state.workspaceNotes = '';
      break;
    case 'history':
      state.siteBuilder.create.title = '';
      state.siteBuilder.create.briefRaw = '';
      state.siteBuilder.create.extraTextsRaw = '';
      state.siteBuilder.revisionText = '';
      break;
  }
  render();
  saveState();
}

function updateMusicRangeValueLabel(id, value) {
  const valueMap = {
    music_styleWeight: 'music_styleWeight_value',
    music_weirdnessConstraint: 'music_weirdnessConstraint_value',
    music_audioWeight: 'music_audioWeight_value',
  };
  const outputId = valueMap[id];
  if (!outputId) return;
  const output = document.getElementById(outputId);
  if (output) output.textContent = Number(value ?? 0).toFixed(2);
}


function isAllowedSeedanceAudioFile(file) {
  if (!file) return false;
  const name = String(file.name || '').toLowerCase();
  const type = String(file.type || '').toLowerCase();
  if (name.endsWith('.mp3') || name.endsWith('.wav')) return true;
  return ['audio/mpeg', 'audio/mp3', 'audio/wav', 'audio/x-wav', 'audio/wave'].includes(type);
}

function isAllowedSeedanceVideoFile(file) {
  if (!file) return false;
  const name = String(file.name || '').toLowerCase();
  const type = String(file.type || '').toLowerCase();
  if (name.endsWith('.mp4') || name.endsWith('.mov')) return true;
  return ['video/mp4', 'video/quicktime'].includes(type);
}

function handleInputChange(target, eventType = 'change') {
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
    if (id === 'music_sourceAudio') {
      runtime.musicSourceFile = files && files[0] ? files[0] : null;
      state.music.uploadFileName = runtime.musicSourceFile ? runtime.musicSourceFile.name : '';
      saveState();
      render();
      target.value = '';
      return;
    }
    const { key, multiple } = fileConfig;
    const effectiveMultiple = multiple || (id === 'image_sourceImage' && isImageSourceMultipleMode());
    let normalizedFiles = effectiveMultiple ? files : files[0];
    if (id === 'video_referenceAudios') {
      const picked = Array.from(files || []);
      const valid = picked.filter((item) => isAllowedSeedanceAudioFile(item));
      if (valid.length !== picked.length) {
        toast('error', 'Неподдерживаемый формат', 'Для Seedance 2.0 audio refs доступны только MP3 и WAV.');
      }
      if (!valid.length) {
        target.value = '';
        return;
      }
      normalizedFiles = valid;
    }
    if (id === 'video_referenceVideos') {
      const picked = Array.from(files || []);
      const valid = picked.filter((item) => isAllowedSeedanceVideoFile(item));
      if (valid.length !== picked.length) {
        toast('error', 'Неподдерживаемый формат', 'Для Seedance 2.0 video refs доступны только MP4 и MOV.');
      }
      if (!valid.length) {
        target.value = '';
        return;
      }
      normalizedFiles = valid;
    }
    setFile(key, normalizedFiles, effectiveMultiple);
    if (id === 'image_sourceImage' && state.image.provider === 'gpt_image_2' && state.image.mode === 'image_to_image') {
      const refs = getFile('image.sourceImage');
      if (Array.isArray(refs) && refs.length > 4) {
        const trimmed = refs.slice(0, 4);
        refs.slice(4).forEach((entry) => revokeRuntimeFileEntry(entry));
        runtime.files['image.sourceImage'] = trimmed;
        toast('info', 'Ограничение GPT Image 2.0', 'Оставил только первые 4 reference images.');
      }
    }
    if (id === 'image_sourceImage' && state.image.provider === 'nano_banana_pro_new' && state.image.mode === 'image_to_image') {
      const refs = getFile('image.sourceImage');
      if (Array.isArray(refs) && refs.length > 8) {
        const trimmed = refs.slice(0, 8);
        refs.slice(8).forEach((entry) => revokeRuntimeFileEntry(entry));
        runtime.files['image.sourceImage'] = trimmed;
        toast('info', 'Ограничение Nano Banana Pro - NEW', 'Оставил только первые 8 reference images.');
      }
    }
    if (id === 'video_motionVideo') probeMotionDuration(getFile('video.motionVideo'));
    if (id === 'video_sourceVideo') {
      state.video.switchxSourceUploadId = '';
      state.video.switchxReferenceImageUrl = '';
      state.video.switchxRefGenerationId = '';
      state.video.switchxReferenceStatus = 'idle';
      resetSwitchxMaskEditor({ clearMaskFile: true });
      probeSourceVideoDuration(getFile('video.sourceVideo'));
    }
    if (id === 'video_referenceImages' && state.video.provider === 'switchx') {
      const nextRefs = getFile('video.referenceImages');
      const hasSwitchxManualRef = Array.isArray(nextRefs) && nextRefs.some((item) => item?.file);
      if (hasSwitchxManualRef) {
        stopSwitchxRefPolling();
        state.video.switchxReferenceImageUrl = '';
        state.video.switchxRefGenerationId = '';
        state.video.switchxReferenceStatus = 'idle';
      }
    }
    if (id === 'video_referenceImages' && state.video.provider === 'pixverse_c1') {
      const nextRefs = getFile('video.referenceImages');
      if (Array.isArray(nextRefs) && nextRefs.length > 7) {
        const trimmed = nextRefs.slice(0, 7);
        nextRefs.slice(7).forEach((entry) => revokeRuntimeFileEntry(entry));
        runtime.files['video.referenceImages'] = trimmed;
        toast('info', 'Ограничение PixVerse C1', 'Оставил только первые 7 референсов для Fusion.');
      }
    }
    if (id === 'video_referenceAudios' && state.video.provider === 'seedance_kie') {
      const nextAudios = getFile('video.referenceAudios');
      if (Array.isArray(nextAudios) && nextAudios.length > 3) {
        const trimmed = nextAudios.slice(0, 3);
        nextAudios.slice(3).forEach((entry) => revokeRuntimeFileEntry(entry));
        runtime.files['video.referenceAudios'] = trimmed;
        toast('info', 'Ограничение Seedance 2.0', 'Оставил только первые 3 аудиореференса.');
      }
    }
    if (id === 'video_referenceVideos' && state.video.provider === 'seedance_kie') {
      const nextVideos = getFile('video.referenceVideos');
      if (Array.isArray(nextVideos) && nextVideos.length > 12) {
        const trimmed = nextVideos.slice(0, 12);
        nextVideos.slice(12).forEach((entry) => revokeRuntimeFileEntry(entry));
        runtime.files['video.referenceVideos'] = trimmed;
        toast('info', 'Ограничение Seedance 2.0', 'Оставил только первые 12 video references.');
      }
    }
    if (id === 'video_switchxSelectMask') {
      const editor = runtime.switchxMaskEditor || {};
      const current = getFile('video.switchxSelectMask');
      Promise.resolve().then(async () => {
        if (!current?.url) return;
        if ((!editor.frameWidth || !editor.frameHeight) && getFile('video.sourceVideo')?.url) {
          try { await ensureSwitchxMaskEditorFrame(); } catch (_e) {}
        }
        const targetWidth = Number(editor.frameWidth || 0);
        const targetHeight = Number(editor.frameHeight || 0);
        const normalizedFile = await switchxMaskSourceToGrayscaleFile(current.url, targetWidth, targetHeight, current.name || 'switchx_select_mask.png');
        switchxMaskEditorSetGeneratedFile(normalizedFile);
        editor.maskDataUrl = await normalizeSwitchxMaskToTransparent(getFile('video.switchxSelectMask')?.url || current.url, targetWidth, targetHeight);
        renderWorkspace();
        render();
      }).catch(() => {});
    }
    render();
    return;
  }

  const update = (obj, key, val) => { obj[key] = val; };

  if (id.startsWith('video_kling3NewShotImageUpload_') || id.startsWith('video_kling3NewShotVideoUpload_')) {
    const match = id.match(/^video_kling3NewShot(ImageUpload|VideoUpload)_(\d+)$/);
    if (match && eventType === 'change') {
      const shotIndex = Number(match[2]);
      const kind = match[1] === 'VideoUpload' ? 'video' : 'image';
      handleKling3NewShotElementUpload(shotIndex, event.target.files, kind)
        .catch((err) => {
          toast('error', 'Не удалось добавить element', String(err?.message || err));
        })
        .finally(() => {
          try { event.target.value = ''; } catch (_) {}
        });
      return;
    }
  }

  if (id.startsWith('video_kling3NewShotPrompt_') || id.startsWith('video_kling3NewShotDuration_')) {
    const match = id.match(/^video_kling3NewShot(Prompt|Duration)_(\d+)$/);
    if (match) {
      const index = Number(match[2]);
      state.video.kling3NewShots = getKling3NewShots();
      if (state.video.kling3NewShots[index]) {
        if (match[1] === 'Prompt') state.video.kling3NewShots[index].prompt = value;
        if (match[1] === 'Duration') state.video.kling3NewShots[index].duration = value;
        saveState();
        if (eventType !== 'input') render();
      }
    }
    return;
  }
  if (id.startsWith('video_kling3NewElement')) {
    const match = id.match(/^video_kling3NewElement(Name|Description|ImageUrls|VideoUrl)_(\d+)$/);
    if (match) {
      const fieldMap = { Name: 'name', Description: 'description', ImageUrls: 'image_urls_text', VideoUrl: 'video_url' };
      const index = Number(match[2]);
      state.video.kling3NewElements = getKling3NewElements();
      while (state.video.kling3NewElements.length <= index) state.video.kling3NewElements.push({ name: '', description: '', image_urls_text: '', video_url: '' });
      state.video.kling3NewElements[index][fieldMap[match[1]]] = value;
      saveState();
      if (eventType !== 'input') render();
    }
    return;
  }

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
      switchChatSession(value, state.chat.mode, { showToast: true });
      break;
    case 'chat_mode':
      switchChatSession(state.chat.model, value, { showToast: true });
      break;
    case 'chat_temperature': state.chat.temperature = Number(value); break;
    case 'chat_maxTokens': state.chat.maxTokens = Number(value); break;

    case 'video_provider':
      state.video.provider = value;
      state.video.model = Object.keys(VIDEO_REGISTRY[value].models)[0];
      state.video.mode = Object.keys(VIDEO_REGISTRY[value].models[state.video.model].modes)[0];
      state.video.panel = 'params';
      resetVideoTransientState({ keepPrompt: false, keepFiles: false });
      break;
    case 'video_model':
      state.video.model = value;
      state.video.mode = Object.keys(VIDEO_REGISTRY[state.video.provider].models[value].modes)[0];
      state.video.panel = 'params';
      resetVideoTransientState({ keepPrompt: false, keepFiles: false });
      break;
    case 'video_mode':
      state.video.mode = value;
      state.video.panel = 'params';
      resetVideoTransientState({ keepPrompt: false, keepFiles: false });
      break;
    case 'video_prompt': state.video.prompt = value; break;
    case 'video_switchxRefPrompt': state.video.switchxRefPrompt = value; break;
    case 'video_switchxAlphaMode':
      state.video.switchxAlphaMode = ['auto', 'fill'].includes(String(value || '').toLowerCase()) ? String(value).toLowerCase() : 'auto';
      resetSwitchxMaskEditor({ clearMaskFile: true });
      break;
    case 'video_duration':
    case 'video_durationLegacy':
    case 'video_durationVeo':
    case 'video_durationSeedance':
    case 'video_durationSora':
    case 'video_durationGrok':
    case 'video_durationPixVerse': state.video.duration = value; break;
    case 'video_resolution':
    case 'video_resolutionGrok':
    case 'video_resolutionSwitchx':
    case 'video_resolutionPixVerse': state.video.resolution = value; break;
    case 'video_providerModeGrok': state.video.providerMode = value; break;
    case 'video_aspectRatio':
    case 'video_aspectRatioVeo':
    case 'video_aspectRatioSeedance':
    case 'video_aspectRatioSora':
    case 'video_aspectRatioGrok':
    case 'video_aspectRatioPixVerse': state.video.aspectRatio = value; break;
    case 'video_enableAudio':
    case 'video_generateAudio': state.video.enableAudio = checked; break;
    case 'video_seedanceUseStartFrame':
      state.video.seedanceUseStartFrame = checked;
      if (!checked) {
        removeUploadFile('video_startFrame');
        if (state.video.seedanceUseLastFrame) {
          state.video.seedanceUseLastFrame = false;
          removeUploadFile('video_lastFrame');
        }
      }
      break;
    case 'video_seedanceUseLastFrame':
      state.video.seedanceUseLastFrame = checked;
      if (checked) {
        state.video.seedanceUseStartFrame = true;
      } else {
        removeUploadFile('video_lastFrame');
      }
      break;
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
      clearImageRunState({ keepPrompt: false, keepFiles: false });
      break;
    case 'image_model':
      state.image.model = value;
      state.image.mode = Object.keys(IMAGE_REGISTRY[state.image.provider].models[value].modes)[0];
      state.image.panel = 'params';
      clearImageRunState({ keepPrompt: false, keepFiles: false });
      break;
    case 'image_mode':
      state.image.mode = value;
      state.image.panel = 'params';
      clearImageRunState({ keepPrompt: false, keepFiles: false });
      break;
    case 'image_prompt': state.image.prompt = value; break;
    case 'image_negativePrompt': state.image.negativePrompt = value; break;
    case 'image_resolution': state.image.resolution = value; break;
    case 'image_aspectRatio':
    case 'image_aspectRatioText': state.image.aspectRatio = value; break;
    case 'image_mjStylize': state.image.mjStylize = Number(value || 0); break;
    case 'image_mjChaos': state.image.mjChaos = Number(value || 0); break;
    case 'image_mjRaw': state.image.mjRaw = !!target.checked; break;
    case 'image_mjSpeedMode': state.image.mjSpeedMode = value; break;
    case 'image_mjSeed': state.image.mjSeed = value; break;
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
    case 'voice_languageCode': state.voice.languageCode = value || 'auto'; break;
    case 'voice_manualVoiceSettings': state.voice.manualVoiceSettings = !!target.checked; break;
    case 'voice_stability': state.voice.stability = Number(value); break;
    case 'voice_similarityBoost': state.voice.similarityBoost = Number(value); break;
    case 'voice_style': state.voice.style = Number(value); break;
    case 'voice_speed': state.voice.speed = Number(value); break;
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
    case 'music_model': state.music.model = value; break;
    case 'music_title': state.music.title = value; break;
    case 'music_tags': state.music.tags = value; break;
    case 'music_negativeTags': state.music.negativeTags = value; break;
    case 'music_vocalGender': state.music.vocalGender = value; break;
    case 'music_mood': state.music.mood = value; break;
    case 'music_references': state.music.references = value; break;
    case 'music_personaId': state.music.personaId = value; break;
    case 'music_personaModel': state.music.personaModel = value; break;
    case 'music_personaName': state.music.personaName = value; break;
    case 'music_personaDescription': state.music.personaDescription = value; break;
    case 'music_styleWeight': state.music.styleWeight = Number(value); break;
    case 'music_weirdnessConstraint': state.music.weirdnessConstraint = Number(value); break;
    case 'music_audioWeight': state.music.audioWeight = Number(value); break;
    case 'music_instrumental': state.music.instrumental = checked; break;
    case 'music_ideaText': state.music.ideaText = value; break;
    case 'music_lyricsText': state.music.lyricsText = value; break;
    case 'music_lyricsPrompt': state.music.lyricsPrompt = value; break;
    case 'music_extendAudioId': state.music.extendAudioId = value; break;
    case 'music_toolPrompt': state.music.toolPrompt = value; break;
    case 'music_continueAt': state.music.continueAt = Number(value); break;
    case 'music_songwriterInput': state.music.songwriter.input = value; break;
    case 'workspaceNotes': state.workspaceNotes = value; break;
    case 'site_project_title': state.siteBuilder.create.title = value; break;
    case 'site_project_brief': state.siteBuilder.create.briefRaw = value; break;
    case 'site_project_extraTexts': state.siteBuilder.create.extraTextsRaw = value; break;
    case 'site_revision_text': state.siteBuilder.revisionText = value; break;
    default: return;
  }
  const structuralRerenderIds = new Set([
    'chat_model',
    'chat_mode',
    'video_provider', 'video_model', 'video_mode',
    'image_provider', 'image_model', 'image_mode',
    'music_ai', 'music_backend', 'music_mode', 'music_model',
    'voice_voiceId', 'voice_modelId', 'voice_languageCode'
  ]);
  const workspaceRerenderIds = new Set(['music_styleWeight', 'music_weirdnessConstraint', 'music_audioWeight']);
  const inspectorRerenderIds = new Set([
    'voice_manualVoiceSettings',
    'voice_stability',
    'voice_similarityBoost',
    'voice_style',
    'voice_speed',
  ]);

  if (workspaceRerenderIds.has(id)) {
    updateMusicRangeValueLabel(id, value);
    if (eventType === 'input') return;
  }

  saveState();

  if (structuralRerenderIds.has(id) || target.tagName === 'SELECT') {
    render();
  } else if (workspaceRerenderIds.has(id)) {
    renderWorkspace();
  } else if (inspectorRerenderIds.has(id) || (target.type === 'checkbox' && String(id || '').startsWith('voice_'))) {
    renderInspector();
    renderHeader();
  } else if (target.type === 'checkbox') {
    render();
  }
}


function activateStudio(studio, options = {}) {
  if (studio === 'workspace' || studio === 'billing') studio = 'chat';
  if (!studio || !STUDIO_META[studio]) return;
  const previousStudio = state.studio;
  state.studio = studio;
  if (state.studio === 'video' && previousStudio !== 'video') state.video.panel = 'params';
  if (state.studio === 'library' && !state.prompts.categories.length) loadPromptCategories();
  if (state.studio === 'voice' && !state.voice.voices.length) loadVoices();
  if (state.studio === 'voice' && state.authToken) loadVoiceHistory({ silent: true, keepSelection: true }).catch(() => {});
  if (state.studio === 'music' && state.authToken) loadMusicHistory({ silent: true, keepSelection: true }).catch(() => {});
  if (state.studio === 'history' && state.authToken) {
    loadSiteBuilderMeta({ silent: true }).catch(() => {});
    loadSiteBuilderProjects({ silent: true, keepSelection: true }).catch(() => {});
  }
  if (state.studio === 'profile' && state.authToken) {
    loadBalanceHistory({ silent: true, force: true, renderNow: true }).catch(() => {});
  }
  if (state.studio === 'partner' && state.authToken) {
    loadPartnerDashboard({ silent: true, force: false, renderNow: true }).catch(() => {});
  }
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
      closeMobileOverlays();
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
    case 'login-placeholder':
    case 'open-auth-modal': {
      openAuthModal(dataset.tab || 'login');
      break;
    }
    case 'close-auth-modal': {
      closeAuthModal();
      break;
    }
    case 'auth-modal-tab-login': {
      state.authUi.modalTab = 'login';
      saveState();
      render();
      break;
    }
    case 'auth-modal-tab-register': {
      state.authUi.modalTab = 'register';
      saveState();
      render();
      break;
    }
    case 'auth-modal-tab-reset': {
      state.authUi.modalTab = 'reset';
      saveState();
      render();
      break;
    }
    case 'google-auth-placeholder': {
      toast('info', 'Google скоро', 'Кнопку Google подготовили по UI. Подключим после настройки OAuth.');
      break;
    }
    case 'send-chat': sendChat(); break;
    case 'start-new-chat':
      startNewChatSession();
      break;
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
    case 'insert-video-prompt-ref': {
      const tag = String(dataset.refTag || '').trim();
      if (!tag) break;
      const current = String(state.video.prompt || '');
      const needsSpace = current && !/\s$/.test(current);
      state.video.prompt = `${current}${needsSpace ? ' ' : ''}${tag} `;
      saveState();
      render();
      const input = document.getElementById('video_prompt');
      if (input) {
        input.focus();
        try { input.setSelectionRange(input.value.length, input.value.length); } catch (_e) {}
      }
      break;
    }
    case 'run-switchx-ref': requestSwitchxReference(); break;
    case 'remove-switchx-ai-reference':
      stopSwitchxRefPolling();
      state.video.switchxReferenceImageUrl = '';
      state.video.switchxRefGenerationId = '';
      state.video.switchxReferenceStatus = 'idle';
      state.video.errorText = '';
      saveState();
      render();
      break;
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
    case 'refresh-site-projects':
      loadSiteBuilderProjects({ silent: false, keepSelection: true });
      break;
    case 'site-clear-draft':
      state.siteBuilder.create.title = '';
      state.siteBuilder.create.briefRaw = '';
      state.siteBuilder.create.extraTextsRaw = '';
      saveState();
      render();
      break;
    case 'site-create-project':
      createSiteBuilderProject();
      break;
    case 'site-open-project':
      if (dataset.projectId) loadSiteBuilderProject(dataset.projectId, { silent: false });
      break;
    case 'site-run-build':
      runSiteBuilderBuild(dataset.projectId || state.siteBuilder.selectedProjectId || '');
      break;
    case 'site-run-revision':
      runSiteBuilderRevision(dataset.projectId || state.siteBuilder.selectedProjectId || '');
      break;
    case 'site-download-version':
      downloadSiteBuilderVersion(dataset.projectId || state.siteBuilder.selectedProjectId || '', Number(dataset.versionNumber || 0));
      break;
    case 'dismiss-site-project':
      dismissSiteBuilderItem('hiddenProjects', dataset.projectId || '');
      break;
    case 'dismiss-site-version': {
      const key = siteBuilderVersionHideKey(dataset.projectId || state.siteBuilder.selectedProjectId || '', { id: dataset.versionNumber || '' });
      dismissSiteBuilderItem('hiddenVersions', key, { projectId: dataset.projectId || state.siteBuilder.selectedProjectId || '' });
      break;
    }
    case 'dismiss-site-job': {
      const key = siteBuilderJobHideKey(dataset.projectId || state.siteBuilder.selectedProjectId || '', { id: dataset.jobKey || '', job_type: dataset.jobType || '' });
      dismissSiteBuilderItem('hiddenJobs', key, { projectId: dataset.projectId || state.siteBuilder.selectedProjectId || '' });
      break;
    }
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
    case 'download-image-result': {
      const historyItem = state.image.panel === 'library' ? imageHistorySelectedItem() : null;
      const compareState = imageCompareState(historyItem);
      const urls = imageActiveUrls(historyItem);
      const safeIndex = Math.max(0, Math.min(urls.length - 1, Number(state.image.activeImageIndex || 0)));
      const url = urls[safeIndex] || (historyItem ? imageHistoryUrl(historyItem) : (state.image.downloadUrl || state.image.outputUrl || compareState.afterUrl || ''));
      forceDownloadFile(url, 'generated-image.png');
      break;
    }
    case 'select-mj-image':
      state.image.activeImageIndex = Number(dataset.imageIndex || 0);
      saveState();
      render();
      break;
    case 'midjourney-reroll':
      runMidjourneyAction('reroll');
      break;
    case 'midjourney-variation':
      runMidjourneyAction('variation', { imageIndex: Number(dataset.imageIndex || 0), variationType: dataset.variationType || 'subtle' });
      break;
    case 'kling3-new-add-shot': {
      state.video.kling3NewShots = getKling3NewShots();
      if (state.video.kling3NewShots.length >= 5) {
        toast('info', 'Лимит', 'Максимум 5 shots.');
        break;
      }
      state.video.kling3NewShots.push({ prompt: '', duration: '3', elements: [] });
      saveState();
      render();
      break;
    }
    case 'kling3-new-remove-shot': {
      const idx = Number(dataset.index || 0);
      const currentShots = getKling3NewShots();
      const removedShot = currentShots[idx] || null;
      const removedElementNames = new Set((removedShot?.elements || []).map((el) => el.name).filter(Boolean));
      state.video.kling3NewShots = currentShots.filter((_, i) => i !== idx);
      state.video.kling3NewElements = getKling3NewElements().filter((el) => !removedElementNames.has(el.name));
      if (state.video.kling3NewShots.length < 2) state.video.kling3NewShots.push({ prompt: '', duration: '3', elements: [] });
      saveState();
      render();
      break;
    }
    case 'kling3-new-add-element': {
      toast('info', 'Перенесено в Shot', 'Теперь элементы добавляются прямо внутри нужного shot под Prompt.');
      break;
    }
    case 'kling3-new-remove-element': {
      removeKling3NewShotElement(Number(dataset.index || 0), dataset.elementName || '');
      break;
    }
    case 'kling3-new-preview-element-image': {
      const url = String(dataset.url || '').trim();
      if (url) window.open(url, '_blank', 'noopener,noreferrer');
      break;
    }
    case 'kling3-new-remove-element-image': {
      removeKling3NewShotElementImage(Number(dataset.index || 0), dataset.elementName || '', Number(dataset.refIndex || 0));
      break;
    }
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
    case 'voice-toggle-advanced':
      state.voice.showAdvancedPanel = !state.voice.showAdvancedPanel;
      saveState();
      renderInspector();
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
    case 'music-open-editor':
      state.music.activeTab = state.music.ai === 'suno' && state.music.mode === 'lyrics' ? 'lyrics' : 'idea';
      state.music.lastEditorTab = state.music.activeTab;
      saveState();
      render();
      break;
    case 'music-open-songwriter':
      if (state.music.ai !== 'suno') {
        toast('info', 'Только для Suno', 'Генератор текста песни доступен только для сценариев Suno. Для Udio используй описание трека.');
        state.music.activeTab = 'idea';
        state.music.lastEditorTab = 'idea';
        saveState();
        render();
        break;
      }
      state.music.activeTab = 'songwriter';
      state.music.lastEditorTab = 'songwriter';
      if (!state.music.songwriter.messages.length) seedMusicSongwriter();
      else {
        saveState();
        render();
      }
      break;
    case 'music-set-ai': {
      const nextAi = dataset.value || 'suno';
      const aiChanged = nextAi !== state.music.ai;
      state.music.ai = nextAi;
      if (aiChanged) resetMusicWorkspaceStage();
      ensureMusicCompatibility({ preserveLyricsTab: false });
      saveState();
      render();
      break;
    }
    case 'music-set-tool-action':
      state.music.toolAction = dataset.value || 'upload-cover';
      ensureMusicToolPromptForAction(state.music.toolAction);
      saveState();
      render();
      break;
    case 'music-toggle-advanced':
      state.music.showAdvancedPanel = !state.music.showAdvancedPanel;
      saveState();
      render();
      break;
    case 'music-set-vocal-mode':
      state.music.instrumental = dataset.value === 'instrumental';
      if (state.music.instrumental) state.music.vocalGender = '';
      saveState();
      renderWorkspace();
      renderHeader();
      break;
    case 'music-clear-persona':
      state.music.personaId = '';
      state.music.personaModel = 'style_persona';
      state.music.personaResult = null;
      saveState();
      renderWorkspace();
      renderHeader();
      toast('success', 'Persona отключена', 'Новые генерации пойдут без сохранённой persona.');
      break;
    case 'music-pick-source-audio': {
      const input = document.getElementById('music_sourceAudio');
      if (input) input.click();
      break;
    }
    case 'music-tool-fill-prompt': {
      const source = dataset.source === 'idea' ? 'idea' : 'lyrics';
      const value = bestMusicToolPrompt(source);
      if (!value) {
        toast('error', 'Нет текста', source === 'lyrics' ? 'Во вкладке «Текст песни» пока пусто.' : 'Во вкладке «Описание трека» пока пусто.');
        break;
      }
      state.music.toolPromptMode = source;
      state.music.toolPrompt = value;
      saveState();
      render();
      break;
    }
    case 'music-generate-lyrics':
      runMusicLyricsGenerator();
      break;
    case 'music-run-tool':
      runMusicToolAction();
      break;
    case 'music-refresh-tool-status':
      if (state.music.toolTaskId) startMusicToolPolling(state.music.toolTaskId);
      break;
    case 'music-apply-lyrics-variant': {
      const idx = Number(dataset.index || -1);
      const item = Array.isArray(state.music.generatedLyrics) ? state.music.generatedLyrics[idx] : null;
      if (item && item.text) {
        state.music.lyricsText = item.text;
        state.music.mode = 'lyrics';
        state.music.activeTab = 'lyrics';
        saveState();
        render();
      }
      break;
    }
    case 'music-load-timestamped-lyrics':
      loadMusicTimestampedLyrics(dataset.taskId, dataset.audioId);
      break;
    case 'music-generate-persona':
      generateMusicPersona(dataset.taskId, dataset.audioId);
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
    case 'refresh-prompts':
      state.prompts.openItemId = '';
      loadPromptCategories();
      break;
    case 'select-category': {
      const category = dataset.category || '';
      if (state.prompts.selectedCategory === category) {
        state.prompts.selectedCategory = '';
        state.prompts.groups = [];
        state.prompts.selectedGroupId = '';
        state.prompts.items = [];
        state.prompts.openItemId = '';
        render();
      } else {
        loadPromptGroups(category);
      }
      break;
    }
    case 'select-group': {
      const groupId = dataset.groupId || '';
      if (String(state.prompts.selectedGroupId) === String(groupId)) {
        state.prompts.selectedGroupId = '';
        state.prompts.items = [];
        state.prompts.openItemId = '';
        render();
      } else {
        loadPromptItems(groupId);
      }
      break;
    }
    case 'open-prompt-item':
      state.prompts.openItemId = dataset.itemId || '';
      render();
      break;
    case 'close-prompt-item':
      state.prompts.openItemId = '';
      render();
      break;
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
    case 'profile-tab-login': state.authUi.profileTab = 'login'; saveState(); render(); break;
    case 'profile-tab-register': state.authUi.profileTab = 'register'; saveState(); render(); break;
    case 'balance-history-filter':
      state.balanceHistory.filter = dataset.filter || 'all';
      renderInspector();
      break;
    case 'refresh-balance-history':
      loadBalanceHistory({ silent: false, force: true, renderNow: true }).catch(() => {});
      break;
    case 'partner-refresh':
      loadPartnerDashboard({ silent: false, force: true, renderNow: true }).catch(() => {});
      break;
    case 'partner-copy-link':
    case 'partner-copy-site-link': {
      const link = partnerSiteLink(partnerDashboard()?.profile || {});
      if (link) navigator.clipboard?.writeText(link).then(() => toast('success', 'Ссылка на сайт скопирована', link)).catch(() => toast('info', 'Ссылка на сайт', link));
      break;
    }
    case 'partner-copy-bot-link': {
      const link = partnerBotLink(partnerDashboard()?.profile || {});
      if (link) navigator.clipboard?.writeText(link).then(() => toast('success', 'Ссылка на бота скопирована', link)).catch(() => toast('info', 'Ссылка на бота', link));
      break;
    }

    case 'mobile-open-menu':
      runtime.mobileUi.navOpen = true;
      runtime.mobileUi.sheetOpen = false;
      renderMobileChrome();
      break;
    case 'mobile-close-overlays':
      closeMobileOverlays();
      renderMobileChrome();
      break;
    case 'mobile-close-sheet':
      runtime.mobileUi.sheetOpen = false;
      renderMobileChrome();
      break;
    case 'mobile-open-settings':
      openMobileSettingsPanel();
      break;
    case 'mobile-open-history':
      openMobileHistoryPanel();
      break;
    case 'mobile-show-result':
      scrollWorkspaceToResult();
      break;
    case 'mobile-show-create':
      closeMobileOverlays();
      renderMobileChrome();
      requestAnimationFrame(() => {
        const prompt = document.querySelector('.workspace-prompt-card, .chat-composer, .music-panel textarea, .voice-editor-card textarea');
        if (prompt) prompt.scrollIntoView({ behavior: 'smooth', block: 'center' });
      });
      break;
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
    case 'history': loadSiteBuilderProjects({ silent: false, keepSelection: true }); break;
    case 'billing': loadBalance(); break;
    case 'partner': loadPartnerDashboard({ silent: false, force: true, renderNow: true }); break;
    default: toast('info', 'Нет действия', 'Для этой студии глобальная кнопка пока не назначена.');
  }
}

function escapeSelectorValue(value) {
  const source = String(value || '');
  if (window.CSS && typeof window.CSS.escape === 'function') return window.CSS.escape(source);
  return source.replace(/(["\#.;?+*~':^$\[\]()=>|/@])/g, '\\$1');
}

function captureRenderSnapshot() {
  const active = document.activeElement;
  const workspaceBody = document.getElementById('workspaceBody');
  const chatMessages = document.getElementById('chatMessages');
  const snapshot = {
    windowScrollX: window.scrollX || 0,
    windowScrollY: window.scrollY || 0,
    workspaceScrollTop: workspaceBody ? workspaceBody.scrollTop : 0,
    chatScrollTop: chatMessages ? chatMessages.scrollTop : 0,
    activeId: '',
    activeName: '',
    selectionStart: null,
    selectionEnd: null,
    selectionDirection: null,
    inputScrollTop: null,
    inputScrollLeft: null,
  };
  if (!(active instanceof HTMLElement)) return snapshot;
  snapshot.activeId = active.id || '';
  snapshot.activeName = active.getAttribute('name') || '';
  if (typeof active.selectionStart === 'number') snapshot.selectionStart = active.selectionStart;
  if (typeof active.selectionEnd === 'number') snapshot.selectionEnd = active.selectionEnd;
  if (typeof active.selectionDirection === 'string') snapshot.selectionDirection = active.selectionDirection;
  if (typeof active.scrollTop === 'number') snapshot.inputScrollTop = active.scrollTop;
  if (typeof active.scrollLeft === 'number') snapshot.inputScrollLeft = active.scrollLeft;
  return snapshot;
}

function restoreRenderSnapshot(snapshot) {
  if (!snapshot) return;
  const workspaceBody = document.getElementById('workspaceBody');
  if (workspaceBody && typeof snapshot.workspaceScrollTop === 'number') workspaceBody.scrollTop = snapshot.workspaceScrollTop;
  const chatMessages = document.getElementById('chatMessages');
  if (chatMessages && typeof snapshot.chatScrollTop === 'number') chatMessages.scrollTop = snapshot.chatScrollTop;

  let target = null;
  if (snapshot.activeId) target = document.getElementById(snapshot.activeId);
  if (!target && snapshot.activeName) {
    target = document.querySelector(`[name="${escapeSelectorValue(snapshot.activeName)}"]`);
  }
  if (target instanceof HTMLElement) {
    try { target.focus({ preventScroll: true }); } catch (_) { try { target.focus(); } catch (_) {} }
    if (typeof snapshot.selectionStart === 'number' && typeof target.setSelectionRange === 'function') {
      const valueLength = String(target.value || '').length;
      const start = Math.max(0, Math.min(snapshot.selectionStart, valueLength));
      const endRaw = typeof snapshot.selectionEnd === 'number' ? snapshot.selectionEnd : snapshot.selectionStart;
      const end = Math.max(start, Math.min(endRaw, valueLength));
      try { target.setSelectionRange(start, end, snapshot.selectionDirection || 'none'); } catch (_) {}
    }
    if (typeof snapshot.inputScrollTop === 'number') target.scrollTop = snapshot.inputScrollTop;
    if (typeof snapshot.inputScrollLeft === 'number') target.scrollLeft = snapshot.inputScrollLeft;
  }
  window.scrollTo(snapshot.windowScrollX || 0, snapshot.windowScrollY || 0);
}

function render() {
  const snapshot = captureRenderSnapshot();
  ensureChatModeCompatibility();
  renderNav();
  renderHeader();
  renderRecentRuns();
  renderWorkspace();
  renderInspector();
  renderMobileChrome();
  renderPromptItemModal();
  renderAuthModal();
  mountTelegramLogin('telegramLoginMount', 'login');
  mountTelegramLogin('profileTelegramLinkMount', 'link');
  renderLandingView();
  enhanceCustomSelects();
  attachImageCompareInteractions();
  if (state.view !== 'workspace') initShowcaseMedia();
  restoreRenderSnapshot(snapshot);
}

document.addEventListener('click', (e) => {
  if (e.target?.id === 'authModalBackdrop') { closeAuthModal(); return; }
  if (e.target?.id === 'promptLibraryModalBackdrop') { state.prompts.openItemId = ''; render(); return; }
  const clickedInsideCustomSelect = e.target.closest('.ab-select');
  if (!clickedInsideCustomSelect) closeCustomSelects();
  const btn = e.target.closest('[data-action]');
  if (btn) {
    handleAction(btn.dataset.action, btn.dataset);
    return;
  }
  if (e.target.id === 'saveSettingsBtn') {
    state.apiBaseUrl = FIXED_API_BASE;
    localStorage.removeItem('astrabot:apiBaseUrl');
    saveState();
    renderHeader();
    toast('success', 'Настройки сохранены', 'API Base URL зафиксирован: https://nabex.ru');
    return;
  }
  if (e.target.id === 'checkApiBtn') { checkApi(); return; }
  if (e.target.id === 'loadBalanceBtn') { loadBalance(); return; }
  if (e.target.id === 'logoutBtn') { logoutWorkspace(); return; }
  if (e.target.id === 'profileLoginBtn') {
    const email = (document.getElementById('profile_login_email')?.value || '').trim();
    const password = document.getElementById('profile_login_password')?.value || '';
    if (!validateEmailValue(email)) { toast('error', 'Проверь email', 'Укажи корректный email.'); return; }
    if (!password) { toast('error', 'Нужен пароль', 'Введите пароль.'); return; }
    submitEmailLogin(email, password).then(() => toast('success', 'Вход выполнен', 'Ты вошёл по email.')).catch((err) => toast('error', 'Не удалось войти', String(err.message || err)));
    return;
  }
  if (e.target.id === 'profileRegisterStartBtn') {
    const email = (document.getElementById('profile_register_email')?.value || '').trim();
    const password = document.getElementById('profile_register_password')?.value || '';
    const password2 = document.getElementById('profile_register_password2')?.value || '';
    if (!validateEmailValue(email)) { toast('error', 'Проверь email', 'Укажи корректный email.'); return; }
    if (password.length < 6) { toast('error', 'Слабый пароль', 'Минимум 6 символов.'); return; }
    if (password !== password2) { toast('error', 'Пароли не совпадают', 'Проверь оба поля пароля.'); return; }
    submitEmailRegisterStart(email, password).then(() => { state.authUi.registerPendingEmail = email; saveState(); render(); toast('success', 'Код отправлен', 'Проверь почту и введи код подтверждения.'); }).catch((err) => toast('error', 'Не удалось отправить код', String(err.message || err)));
    return;
  }
  if (e.target.id === 'profileRegisterConfirmBtn') {
    const email = state.authUi.registerPendingEmail || (document.getElementById('profile_register_email')?.value || '').trim();
    const code = (document.getElementById('profile_register_code')?.value || '').trim();
    if (!email || !validateEmailValue(email)) { toast('error', 'Нет email', 'Сначала запроси код.'); return; }
    if (!code) { toast('error', 'Нет кода', 'Введи код из письма.'); return; }
    submitEmailRegisterConfirm(email, code).then(() => toast('success', 'Почта подтверждена', 'Аккаунт создан и вход выполнен.')).catch((err) => toast('error', 'Не удалось подтвердить', String(err.message || err)));
    return;
  }
  if (e.target.id === 'profileLinkEmailStartBtn') {
    const email = (document.getElementById('profile_link_email')?.value || '').trim();
    const password = document.getElementById('profile_link_password')?.value || '';
    const password2 = document.getElementById('profile_link_password2')?.value || '';
    if (!validateEmailValue(email)) { toast('error', 'Проверь email', 'Укажи корректный email.'); return; }
    if (password.length < 6) { toast('error', 'Слабый пароль', 'Минимум 6 символов.'); return; }
    if (password !== password2) { toast('error', 'Пароли не совпадают', 'Проверь оба поля пароля.'); return; }
    submitLinkEmailStart(email, password).then(() => { state.authUi.linkPendingEmail = email; saveState(); render(); toast('success', 'Код отправлен', 'Проверь почту и введи код подтверждения.'); }).catch((err) => toast('error', 'Не удалось отправить код', String(err.message || err)));
    return;
  }
  if (e.target.id === 'profileLinkEmailConfirmBtn') {
    const email = state.authUi.linkPendingEmail || (document.getElementById('profile_link_email')?.value || '').trim();
    const code = (document.getElementById('profile_link_code')?.value || '').trim();
    if (!email || !validateEmailValue(email)) { toast('error', 'Нет email', 'Сначала запроси код.'); return; }
    if (!code) { toast('error', 'Нет кода', 'Введи код из письма.'); return; }
    submitLinkEmailConfirm(email, code).then(() => toast('success', 'Email привязан', 'Теперь в этот же аккаунт можно входить по почте.')).catch((err) => toast('error', 'Не удалось подтвердить', String(err.message || err)));
    return;
  }

  if (e.target.id === 'partnerPayoutSubmitBtn') { submitPartnerPayout(); return; }

  if (e.target.id === 'profileLogoutBtn') { logoutWorkspace(); return; }
  if (e.target.id === 'profileOpenResetBtn') { openAuthModal('reset'); return; }
  if (e.target.id === 'profileChangePasswordScrollBtn') { document.getElementById('profileChangePasswordCard')?.scrollIntoView({ behavior: 'smooth', block: 'start' }); return; }
  if (e.target.id === 'profileChangePasswordBtn') {
    const currentPassword = document.getElementById('profile_change_current_password')?.value || '';
    const newPassword = document.getElementById('profile_change_new_password')?.value || '';
    const newPassword2 = document.getElementById('profile_change_new_password2')?.value || '';
    if (!currentPassword) { toast('error', 'Нужен текущий пароль', 'Введи текущий пароль.'); return; }
    if (newPassword.length < 6) { toast('error', 'Слабый пароль', 'Минимум 6 символов.'); return; }
    if (newPassword !== newPassword2) { toast('error', 'Пароли не совпадают', 'Проверь оба поля нового пароля.'); return; }
    submitChangePassword(currentPassword, newPassword).then(() => toast('success', 'Пароль обновлён', 'Теперь можно входить с новым паролем.')).catch((err) => toast('error', 'Не удалось сменить пароль', String(err.message || err)));
    return;
  }
  if (e.target.id === 'authModalLoginBtn') {
    const email = (document.getElementById('auth_modal_login_email')?.value || '').trim();
    const password = document.getElementById('auth_modal_login_password')?.value || '';
    if (!validateEmailValue(email)) { toast('error', 'Проверь email', 'Укажи корректный email.'); return; }
    if (!password) { toast('error', 'Нужен пароль', 'Введите пароль.'); return; }
    submitEmailLogin(email, password).then(() => { closeAuthModal(); toast('success', 'Вход выполнен', 'Ты вошёл по email.'); }).catch((err) => toast('error', 'Не удалось войти', String(err.message || err)));
    return;
  }
  if (e.target.id === 'authModalRegisterStartBtn') {
    const email = (document.getElementById('auth_modal_register_email')?.value || '').trim();
    const password = document.getElementById('auth_modal_register_password')?.value || '';
    const password2 = document.getElementById('auth_modal_register_password2')?.value || '';
    if (!validateEmailValue(email)) { toast('error', 'Проверь email', 'Укажи корректный email.'); return; }
    if (password.length < 6) { toast('error', 'Слабый пароль', 'Минимум 6 символов.'); return; }
    if (password !== password2) { toast('error', 'Пароли не совпадают', 'Проверь оба поля пароля.'); return; }
    submitEmailRegisterStart(email, password).then(() => { state.authUi.registerPendingEmail = email; state.authUi.modalTab = 'register'; saveState(); render(); toast('success', 'Код отправлен', 'Проверь почту и введи код подтверждения.'); }).catch((err) => toast('error', 'Не удалось отправить код', String(err.message || err)));
    return;
  }
  if (e.target.id === 'authModalRegisterConfirmBtn') {
    const email = state.authUi.registerPendingEmail || (document.getElementById('auth_modal_register_email')?.value || '').trim();
    const code = (document.getElementById('auth_modal_register_code')?.value || '').trim();
    if (!email || !validateEmailValue(email)) { toast('error', 'Нет email', 'Сначала запроси код.'); return; }
    if (!code) { toast('error', 'Нет кода', 'Введи код из письма.'); return; }
    submitEmailRegisterConfirm(email, code).then(() => { closeAuthModal(); toast('success', 'Почта подтверждена', 'Аккаунт создан и вход выполнен.'); }).catch((err) => toast('error', 'Не удалось подтвердить', String(err.message || err)));
    return;
  }
  if (e.target.id === 'authModalResetStartBtn') {
    const email = (document.getElementById('auth_modal_reset_email')?.value || '').trim();
    if (!validateEmailValue(email)) { toast('error', 'Проверь email', 'Укажи корректный email.'); return; }
    submitPasswordResetStart(email).then(() => { state.authUi.resetPendingEmail = email; state.authUi.modalTab = 'reset'; saveState(); render(); toast('success', 'Код отправлен', 'Проверь почту и введи код для смены пароля.'); }).catch((err) => toast('error', 'Не удалось отправить код', String(err.message || err)));
    return;
  }
  if (e.target.id === 'authModalResetConfirmBtn') {
    const email = state.authUi.resetPendingEmail || (document.getElementById('auth_modal_reset_email')?.value || '').trim();
    const code = (document.getElementById('auth_modal_reset_code')?.value || '').trim();
    const password = document.getElementById('auth_modal_reset_password')?.value || '';
    const password2 = document.getElementById('auth_modal_reset_password2')?.value || '';
    if (!email || !validateEmailValue(email)) { toast('error', 'Нет email', 'Сначала запроси код.'); return; }
    if (!code) { toast('error', 'Нет кода', 'Введи код из письма.'); return; }
    if (password.length < 6) { toast('error', 'Слабый пароль', 'Минимум 6 символов.'); return; }
    if (password !== password2) { toast('error', 'Пароли не совпадают', 'Проверь оба поля нового пароля.'); return; }
    submitPasswordResetConfirm(email, code, password).then(() => { closeAuthModal(); toast('success', 'Пароль обновлён', 'Теперь можно входить с новым паролем.'); }).catch((err) => toast('error', 'Не удалось сменить пароль', String(err.message || err)));
    return;
  }
  if (e.target.id === 'clearRunsBtn') { state.recentRuns = []; saveState(); renderRecentRuns(); renderWorkspace(); return; }
  if (e.target.id === 'seedDemoBtn') { seedDemo(); return; }
  if (e.target.id === 'globalRunBtn') { runCurrentStudio(); return; }
  if (e.target.id === 'resetStudioBtn') { resetCurrentStudio(); return; }
});

document.addEventListener('input', (e) => handleInputChange(e.target, 'input'));
document.addEventListener('change', (e) => handleInputChange(e.target, 'change'));
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    closeCustomSelects();
    if (state.authUi.modalOpen) closeAuthModal();
  }
  if (state.studio === 'chat' && e.target.id === 'chatInput' && e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    sendChat();
  }
});

async function restorePendingVideoRun() {
  if (!state.authToken) return;
  if (state.video.generationId && !state.video.outputUrl && !isVideoTaskFinished(state.video.lastStatus)) {
    startVideoPolling({ immediate: true });
    return;
  }
  if (!state.video.requestStartedAt || state.video.outputUrl) return;
  const items = await loadVideoHistory({ silent: true, keepSelection: true });
  const candidate = findRecentHistoryCandidate(items, {
    startedAt: state.video.requestStartedAt,
    provider: state.video.provider,
    model: state.video.model,
    prompt: state.video.prompt,
  });
  if (!candidate) return;
  state.video.generationId = candidate.id || state.video.generationId;
  state.video.providerTaskId = candidate.task_id || candidate.id || state.video.providerTaskId;
  state.video.lastStatus = String(candidate.status || 'processing').toLowerCase();
  state.video.errorText = candidate.error_message || '';
  state.video.statusText = candidate.error_message || (state.video.lastStatus === 'completed' ? 'Видео восстановлено после перезагрузки.' : 'Восстановлен активный запуск. Видео появится автоматически.');
  const readyUrl = historyVideoUrl(candidate);
  if (readyUrl && state.video.lastStatus === 'completed') {
    state.video.outputUrl = readyUrl;
    state.video.downloadUrl = historyVideoDownloadUrl(candidate) || readyUrl;
    state.video.coverUrl = candidate.thumbnail_url || '';
    state.video.percent = 100;
    state.video.requestStartedAt = '';
    syncVideoEditorWithHistoryItem(candidate);
  } else {
    startVideoPolling({ immediate: true });
  }
  saveState();
  render();
}

async function restorePendingImageRun() {
  if (!state.authToken) return;
  if (state.image.generationId && state.image.isGenerating) {
    startImagePolling({ immediate: true });
    return;
  }
  if (!state.image.requestStartedAt || state.image.outputUrl) return;
  const items = await loadImageHistory({ silent: true, keepSelection: true });
  const candidate = findRecentHistoryCandidate(items, {
    startedAt: state.image.requestStartedAt,
    provider: state.image.provider,
    model: state.image.model,
    prompt: state.image.prompt,
  });
  if (!candidate) return;

  const candidateStatus = String(candidate.status || '').toLowerCase();
  const candidateUrl = imageHistoryUrl(candidate) || candidate.after_image_url || candidate.image_url || '';

  state.image.generationId = candidate.id || state.image.generationId;
  state.image.errorText = candidate.error_message || '';

  if (candidateStatus === 'completed' && candidateUrl) {
    state.image.isGenerating = false;
    state.image.requestStartedAt = '';
    state.image.outputUrl = candidateUrl;
    state.image.downloadUrl = candidateUrl;
    state.image.beforeImageUrl = candidate.before_image_url || candidate.source_image_url || '';
    state.image.afterImageUrl = candidate.after_image_url || candidateUrl;
    state.image.compareMode = !!candidate.compare_mode;
    state.image.comparePosition = 50;
    state.image.statusText = candidate.error_message || 'Изображение восстановлено после перезагрузки.';
    state.image.panel = 'params';
    saveState();
    render();
    loadBalance({ silent: true, renderNow: true }).catch(() => {});
    return;
  }

  state.image.statusText = candidate.error_message || (candidateStatus === 'completed'
    ? 'Провайдер уже завершил генерацию. Подтягиваем файл в рабочую зону.'
    : 'Восстановлен активный запуск изображения.');
  state.image.isGenerating = !isTerminalTaskStatus(candidate.status) || candidateStatus === 'completed';
  saveState();
  render();

  await pollImageTask({ silent: true });
  if (state.image.isGenerating && !state.image.outputUrl) {
    startImagePolling({ immediate: false });
  }
}


async function restorePendingVoiceRun() {
  if (!state.authToken || !state.voice.generationId || !state.voice.isGenerating || state.voice.audioUrl) return;
  await pollVoiceTask({ silent: true });
  if (state.voice.isGenerating && !state.voice.audioUrl) {
    startVoicePolling({ immediate: false });
  }
}

async function init() {
  if (BOOT_QUERY.get('auth') === 'login' && !state.authToken) {
    state.view = 'workspace';
    state.authUi.modalOpen = true;
    state.authUi.modalTab = 'login';
  }
  render();
  await loadBootstrap();
  await checkApi();
  if (state.authToken) {
    await loadMe();
    if (state.authToken && state.me) {
      bindPendingPartnerRef().catch(() => {});
      loadBalanceHistory({ silent: true, force: state.studio === 'profile', renderNow: state.studio === 'profile' }).catch(() => {});
      if (state.studio === 'partner') loadPartnerDashboard({ silent: true, force: true, renderNow: true }).catch(() => {});
    }
    await resumePendingTopup();
    loadVideoHistory({ silent: true }).catch(() => {});
    loadVoiceHistory({ silent: true, keepSelection: true }).catch(() => {});
    if (state.image.panel === 'library') loadImageHistory({ silent: true }).catch(() => {});
    if (state.studio === 'history') {
      loadSiteBuilderMeta({ silent: true }).catch(() => {});
      loadSiteBuilderProjects({ silent: true, keepSelection: true }).catch(() => {});
    }
  }
  if (state.voice.voices.length === 0) loadVoices();
  if (state.studio === 'library' || state.prompts.categories.length === 0) loadPromptCategories();
  await restorePendingVideoRun();
  await restorePendingImageRun();
  await restorePendingVoiceRun();
  if (state.video.switchxRefGenerationId && !state.video.switchxReferenceImageUrl && !['failed', 'error', 'completed'].includes(String(state.video.switchxReferenceStatus || '').toLowerCase())) {
    startSwitchxRefPolling({ immediate: true });
  }
  if (state.music.generationId && ['queued', 'processing', 'running'].includes(String(state.music.status || '').toLowerCase())) {
    startMusicPolling(state.music.generationId);
  }
  if (state.music.toolTaskId && !['completed', 'failed', 'error', 'cancelled', 'canceled'].includes(String(state.music.toolTaskStatus || '').toLowerCase())) {
    startMusicToolPolling(state.music.toolTaskId);
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
