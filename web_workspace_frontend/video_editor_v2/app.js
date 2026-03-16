const urlParams = new URLSearchParams(window.location.search);
const incomingApiBase = urlParams.get('api_base') || urlParams.get('apiBase') || '';
const incomingToken = urlParams.get('token') || urlParams.get('authToken') || '';
const incomingReturnUrl = urlParams.get('return_url') || urlParams.get('returnUrl') || '';

const apiBase = incomingApiBase || window.localStorage.getItem('astrabot:apiBaseUrl') || 'https://astrabot-tchj.onrender.com';
const state = {
  token: incomingToken || window.localStorage.getItem('astrabot:authToken') || '',
  libraryTab: 'videos',
  projectId: '',
  selectedClipId: '',
  selectedAudioId: '',
  renderJobId: '',
  polling: null,
  videos: [],
  audio: [],
  returnUrl: incomingReturnUrl || 'https://astrabot-workspace.onrender.com',
  project: {
    title: 'Новый видеопроект',
    video_clips: [],
    audio_tracks: [],
    text_overlays: [],
    stickers: [],
  },
};

function $(id) { return document.getElementById(id); }
function toast(message, ms = 2600) {
  const el = $('toast');
  if (!el) return;
  el.textContent = message;
  el.classList.remove('hidden');
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.add('hidden'), ms);
}
function authHeaders(json = true) {
  const headers = {};
  if (state.token) headers['Authorization'] = `Bearer ${state.token}`;
  if (json) headers['Content-Type'] = 'application/json';
  return headers;
}
async function api(path, options = {}) {
  const res = await fetch(`${apiBase}${path}`, options);
  const text = await res.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch { data = { detail: text }; }
  if (!res.ok) throw new Error(data.detail || 'API error');
  return data;
}

function currentClip() {
  return state.project.video_clips.find((x) => x.id === state.selectedClipId) || null;
}
function currentAudio() {
  return state.project.audio_tracks.find((x) => x.id === state.selectedAudioId) || null;
}
function totalDuration() {
  return state.project.video_clips.reduce((sum, item) => {
    const start = Number(item.source_start || 0);
    const end = Number(item.source_end || 0);
    return sum + Math.max(0, end - start);
  }, 0);
}
function fmtSec(v) {
  const n = Math.max(0, Number(v || 0));
  return `${n.toFixed(1)}с`;
}
function saveTokenSilently() {
  if (state.token) window.localStorage.setItem('astrabot:authToken', state.token);
  if (apiBase) window.localStorage.setItem('astrabot:apiBaseUrl', apiBase);
}

function configureLinks() {
  const backLink = $('backLink');
  if (backLink) backLink.href = state.returnUrl;
}

function renderSessionState() {
  const status = $('sessionState');
  const hint = $('libraryHint');
  const banner = $('authBanner');
  if (!status || !hint || !banner) return;
  if (state.token) {
    status.textContent = 'сессия активна';
    status.className = 'badge success';
    hint.textContent = 'Загружай видео и музыку или добавляй материалы из библиотеки.';
    banner.classList.add('hidden');
  } else {
    status.textContent = 'нет авторизации';
    status.className = 'badge muted';
    hint.textContent = 'История генераций и сохранение проекта появятся после открытия редактора из Workspace.';
    banner.classList.remove('hidden');
  }
}

function renderRuler() {
  const total = Math.max(5, Math.ceil(totalDuration()));
  $('timelineRuler').innerHTML = Array.from({ length: total }, (_, i) => `<div class="tick">${i}s</div>`).join('');
}

function previewSource() {
  const clip = currentClip() || state.project.video_clips[0];
  if (!clip) return '';
  const source = [...state.videos, ...state.audio].find((x) => x.id === clip.source_id);
  return source?.video_url || source?.download_url || source?.output_url || '';
}

function renderPreview() {
  const src = previewSource();
  const el = $('previewVideo');
  const empty = $('previewEmpty');
  if (src) {
    if (el.getAttribute('src') !== src) el.src = src;
    empty.classList.add('hidden');
    el.classList.remove('hidden');
  } else {
    el.removeAttribute('src');
    el.load?.();
    el.classList.add('hidden');
    empty.classList.remove('hidden');
  }
  $('projectTitleInput').value = state.project.title;
  $('projectStatus').textContent = state.projectId ? 'сохранён' : 'черновик';
}

function clipCard(item, idx) {
  const active = item.id === state.selectedClipId ? 'active' : '';
  const duration = Math.max(0, Number(item.source_end || 0) - Number(item.source_start || 0));
  return `
    <div class="clip-card ${active}" data-clip-id="${item.id}">
      <div class="clip-top">
        <div>
          <div class="clip-title">${item.label || `Клип ${idx + 1}`}</div>
          <div class="clip-meta">${fmtSec(duration)} · ${item.muted ? 'без звука' : `${Number(item.volume || 100)}%`}</div>
        </div>
        <span class="badge">${idx + 1}</span>
      </div>
      <div class="clip-meta">${item.filter || 'без фильтра'} · ${item.effect || 'без эффекта'}</div>
      ${idx > 0 ? `<select class="transition-select" data-transition-for="${item.id}">
        <option value="none" ${item.transition?.type === 'none' ? 'selected' : ''}>Без перехода</option>
        <option value="fade" ${item.transition?.type === 'fade' ? 'selected' : ''}>Fade</option>
        <option value="dissolve" ${item.transition?.type === 'dissolve' ? 'selected' : ''}>Dissolve</option>
        <option value="slideleft" ${item.transition?.type === 'slideleft' ? 'selected' : ''}>Slide Left</option>
        <option value="slideright" ${item.transition?.type === 'slideright' ? 'selected' : ''}>Slide Right</option>
        <option value="zoomin" ${item.transition?.type === 'zoomin' ? 'selected' : ''}>Zoom Fade</option>
      </select>` : '<div class="clip-meta small-gap">Первый клип без входного перехода</div>'}
    </div>`;
}
function audioCard(item, idx) {
  const active = item.id === state.selectedAudioId ? 'active' : '';
  const duration = Math.max(0, Number(item.source_end || 0) - Number(item.source_start || 0));
  return `<div class="audio-card ${active}" data-audio-id="${item.id}">
    <div class="clip-top"><div><div class="clip-title">${item.label || `Музыка ${idx + 1}`}</div><div class="clip-meta">старт ${fmtSec(item.timeline_start || 0)}</div></div><span class="badge">${item.volume || 100}%</span></div>
    <div class="clip-meta">${fmtSec(duration)}</div>
  </div>`;
}
function renderTimeline() {
  renderRuler();
  $('videoTrack').innerHTML = state.project.video_clips.length
    ? state.project.video_clips.map(clipCard).join('')
    : '<div class="track-empty">Добавь до 5 роликов слева, чтобы собрать монтаж.</div>';
  $('audioTrack').innerHTML = state.project.audio_tracks.length
    ? state.project.audio_tracks.map(audioCard).join('')
    : '<div class="track-empty">Музыка пока не добавлена.</div>';
}

function renderSelectionPanel() {
  const body = $('selectionBody');
  const title = $('selectionTitle');
  const clip = currentClip();
  const audio = currentAudio();
  if (clip) {
    title.textContent = 'Настройки клипа';
    body.innerHTML = `
      <div class="selection-grid">
        <label class="kv-row">Название<input id="clipLabelInput" value="${clip.label || ''}"></label>
        <label class="kv-row">Старт, сек<input id="clipStartInput" type="number" min="0" step="0.1" value="${Number(clip.source_start || 0)}"></label>
        <label class="kv-row">Конец, сек<input id="clipEndInput" type="number" min="0" step="0.1" value="${Number(clip.source_end || 0)}"></label>
        <label class="kv-row">Громкость, %<input id="clipVolumeInput" type="number" min="0" max="100" step="1" value="${Number(clip.volume || 100)}"></label>
        <label class="kv-row">Фильтр<select id="clipFilterInput"><option value="none">Без фильтра</option><option value="warm">Тёплый</option><option value="cold">Холодный</option><option value="bw">Ч/Б</option><option value="cinematic">Cinematic</option></select></label>
        <label class="kv-row">Эффект<select id="clipEffectInput"><option value="none">Без эффекта</option><option value="zoom_in">Zoom in</option><option value="zoom_out">Zoom out</option><option value="blur_intro">Blur intro</option></select></label>
        <label class="kv-row">Длительность перехода, сек<input id="transitionDurationInput" type="number" min="0" max="1" step="0.1" value="${Number(clip.transition?.duration || 0)}"></label>
      </div>`;
    $('clipFilterInput').value = clip.filter || 'none';
    $('clipEffectInput').value = clip.effect || 'none';
    return;
  }
  if (audio) {
    title.textContent = 'Настройки музыки';
    body.innerHTML = `
      <div class="selection-grid">
        <label class="kv-row">Название<input id="audioLabelInput" value="${audio.label || ''}"></label>
        <label class="kv-row">Старт на таймлайне<input id="audioTimelineInput" type="number" min="0" step="0.1" value="${Number(audio.timeline_start || 0)}"></label>
        <label class="kv-row">Старт фрагмента, сек<input id="audioStartInput" type="number" min="0" step="0.1" value="${Number(audio.source_start || 0)}"></label>
        <label class="kv-row">Конец фрагмента, сек<input id="audioEndInput" type="number" min="0" step="0.1" value="${Number(audio.source_end || 0)}"></label>
        <label class="kv-row">Громкость, %<input id="audioVolumeInput" type="number" min="0" max="100" step="1" value="${Number(audio.volume || 100)}"></label>
      </div>`;
    return;
  }
  title.textContent = 'Параметры элемента';
  body.innerHTML = '<div class="selection-empty">Выбери клип или музыкальную дорожку на таймлайне, чтобы отредактировать параметры.</div>';
}

function renderLibrary() {
  const items = state.libraryTab === 'videos' ? state.videos : state.audio;
  const html = items.length
    ? items.map((item) => `
      <div class="library-item">
        <strong>${item.filename || item.prompt || item.id}</strong>
        <small>${item.duration_sec || item.duration_sec === 0 ? fmtSec(item.duration_sec) : '—'} · ${item.provider || item.file_type || 'media'}</small>
        <button class="btn secondary full" data-library-add="${item.id}">${state.libraryTab === 'videos' ? 'Добавить в таймлайн' : 'Добавить музыку'}</button>
      </div>`).join('')
    : `<div class="library-empty">${state.token ? 'Пока пусто. Загрузите файл или сохраните генерацию в библиотеку.' : 'История генераций недоступна, потому что редактор открыт без авторизации Workspace.'}</div>`;
  $('libraryList').innerHTML = html;
}

async function loadLibrary() {
  if (!state.token) {
    state.videos = [];
    state.audio = [];
    renderLibrary();
    renderSessionState();
    return;
  }
  const [videosRes, audioRes] = await Promise.all([
    api('/api/video-editor-v2/library/videos', { headers: authHeaders(false) }),
    api('/api/video-editor-v2/library/uploads?file_type=audio', { headers: authHeaders(false) }),
  ]);
  state.videos = videosRes.items || [];
  state.audio = audioRes.items || [];
  renderLibrary();
  renderSessionState();
}

function addVideoFromLibrary(id) {
  if (state.project.video_clips.length >= 5) return toast('Максимум 5 клипов');
  const item = state.videos.find((x) => x.id === id);
  if (!item) return;
  const clip = {
    id: crypto.randomUUID(),
    source_type: item.file_type === 'video' ? 'upload' : 'generation',
    source_id: item.id,
    label: item.filename || item.prompt || `Клип ${state.project.video_clips.length + 1}`,
    source_start: 0,
    source_end: Number(item.duration_sec || 5),
    volume: 100,
    muted: false,
    filter: 'none',
    effect: 'none',
    transition: { type: state.project.video_clips.length ? 'fade' : 'none', duration: state.project.video_clips.length ? 0.5 : 0 },
  };
  state.project.video_clips.push(clip);
  state.selectedClipId = clip.id;
  state.selectedAudioId = '';
  rerender();
}
function addAudioFromLibrary(id) {
  const item = state.audio.find((x) => x.id === id);
  if (!item) return;
  state.project.audio_tracks = [{
    id: crypto.randomUUID(),
    source_id: item.id,
    label: item.filename || 'Музыка',
    timeline_start: 0,
    source_start: 0,
    source_end: Number(item.duration_sec || 0),
    volume: 100,
  }];
  state.selectedAudioId = state.project.audio_tracks[0].id;
  state.selectedClipId = '';
  rerender();
}

async function saveProject() {
  const payload = { title: $('projectTitleInput').value.trim() || 'Новый видеопроект', project_json: state.project };
  state.project.title = payload.title;
  if (!state.token) return toast('Открой редактор из Workspace, чтобы сохранить проект');
  let data;
  if (state.projectId) {
    data = await api(`/api/video-editor-v2/projects/${encodeURIComponent(state.projectId)}`, { method: 'PUT', headers: authHeaders(), body: JSON.stringify(payload) });
  } else {
    data = await api('/api/video-editor-v2/projects', { method: 'POST', headers: authHeaders(), body: JSON.stringify(payload) });
  }
  state.projectId = data.item.id;
  toast('Проект сохранён');
  rerender();
}

async function startRender() {
  if (!state.token) return toast('Открой редактор из Workspace, чтобы запустить экспорт');
  if (!state.project.video_clips.length) return toast('Сначала добавь хотя бы один клип');
  if (!state.projectId) await saveProject();
  if (!state.projectId) return;
  const data = await api('/api/video-editor-v2/render', { method: 'POST', headers: authHeaders(), body: JSON.stringify({ project_id: state.projectId }) });
  state.renderJobId = data.item.id;
  $('renderStatus').textContent = 'рендер...';
  pollRender();
}

async function pollRender() {
  clearTimeout(state.polling);
  if (!state.renderJobId) return;
  try {
    const data = await api(`/api/video-editor-v2/render/${encodeURIComponent(state.renderJobId)}`, { headers: authHeaders(false) });
    const item = data.item || {};
    $('renderStatus').textContent = `${item.status || 'queued'} ${item.progress || 0}%`;
    if (item.status === 'completed') {
      toast('Рендер завершён');
      if (item.output_url) $('previewVideo').src = item.output_url;
      $('previewVideo').classList.remove('hidden');
      $('previewEmpty').classList.add('hidden');
      return;
    }
    if (item.status === 'failed') {
      toast(item.error_text || 'Ошибка рендера', 5000);
      return;
    }
  } catch (e) {
    toast(e.message || String(e));
    return;
  }
  state.polling = setTimeout(pollRender, 2500);
}

async function uploadMedia(kind, file) {
  if (!file) return;
  if (!state.token) return toast('Загрузка доступна только из Workspace');
  const form = new FormData();
  form.append('file', file);
  await api(`/api/video-editor-v2/upload/${kind}`, { method: 'POST', headers: authHeaders(false), body: form });
  toast(kind === 'video' ? 'Видео загружено' : 'Музыка загружена');
  await loadLibrary();
  state.libraryTab = kind === 'video' ? 'videos' : 'audio';
  rerender();
}

function applySelectionChanges() {
  const clip = currentClip();
  const audio = currentAudio();
  if (clip) {
    clip.label = $('clipLabelInput')?.value || clip.label;
    clip.source_start = Number($('clipStartInput')?.value || 0);
    clip.source_end = Number($('clipEndInput')?.value || 0);
    clip.volume = Number($('clipVolumeInput')?.value || 100);
    clip.filter = $('clipFilterInput')?.value || 'none';
    clip.effect = $('clipEffectInput')?.value || 'none';
    clip.transition.duration = Number($('transitionDurationInput')?.value || 0);
  }
  if (audio) {
    audio.label = $('audioLabelInput')?.value || audio.label;
    audio.timeline_start = Number($('audioTimelineInput')?.value || 0);
    audio.source_start = Number($('audioStartInput')?.value || 0);
    audio.source_end = Number($('audioEndInput')?.value || 0);
    audio.volume = Number($('audioVolumeInput')?.value || 100);
  }
  rerender();
}

function rerender() {
  configureLinks();
  renderSessionState();
  renderPreview();
  renderLibrary();
  renderTimeline();
  renderSelectionPanel();
}

document.addEventListener('click', async (e) => {
  const addId = e.target.dataset.libraryAdd;
  if (addId) {
    if (state.libraryTab === 'videos') addVideoFromLibrary(addId); else addAudioFromLibrary(addId);
    return;
  }
  const clipCard = e.target.closest('[data-clip-id]');
  if (clipCard) {
    state.selectedClipId = clipCard.dataset.clipId;
    state.selectedAudioId = '';
    rerender();
    return;
  }
  const audioCard = e.target.closest('[data-audio-id]');
  if (audioCard) {
    state.selectedAudioId = audioCard.dataset.audioId;
    state.selectedClipId = '';
    rerender();
    return;
  }
  if (e.target.id === 'refreshLibraryBtn') { loadLibrary().catch((err) => toast(err.message)); return; }
  if (e.target.id === 'saveProjectBtn') { saveProject().catch((err) => toast(err.message, 5000)); return; }
  if (e.target.id === 'renderBtn') { startRender().catch((err) => toast(err.message, 5000)); return; }
  if (e.target.id === 'deleteClipBtn') {
    if (state.selectedClipId) state.project.video_clips = state.project.video_clips.filter((x) => x.id !== state.selectedClipId);
    state.selectedClipId = '';
    rerender();
    return;
  }
  if (e.target.id === 'muteClipBtn') {
    const clip = currentClip();
    if (!clip) return;
    clip.muted = !clip.muted;
    clip.volume = clip.muted ? 0 : 100;
    rerender();
    return;
  }
  if (e.target.id === 'splitBtn') {
    const clip = currentClip();
    if (!clip) return toast('Сначала выбери клип');
    const start = Number(clip.source_start || 0);
    const end = Number(clip.source_end || 0);
    const mid = Number(((start + end) / 2).toFixed(1));
    if (end - start < 1.0) return toast('Клип слишком короткий для split');
    const idx = state.project.video_clips.findIndex((x) => x.id === clip.id);
    const a = { ...clip, id: crypto.randomUUID(), source_end: mid, label: `${clip.label} A` };
    const b = { ...clip, id: crypto.randomUUID(), source_start: mid, label: `${clip.label} B`, transition: { ...clip.transition } };
    state.project.video_clips.splice(idx, 1, a, b);
    state.selectedClipId = a.id;
    rerender();
    return;
  }
});

document.addEventListener('change', (e) => {
  if (e.target.id === 'videoUploadInput') uploadMedia('video', e.target.files?.[0]).catch((err) => toast(err.message, 5000));
  if (e.target.id === 'audioUploadInput') uploadMedia('audio', e.target.files?.[0]).catch((err) => toast(err.message, 5000));
  const trFor = e.target.dataset.transitionFor;
  if (trFor) {
    const clip = state.project.video_clips.find((x) => x.id === trFor);
    if (clip) clip.transition.type = e.target.value;
    rerender();
    return;
  }
  if (e.target.matches('#clipLabelInput,#clipStartInput,#clipEndInput,#clipVolumeInput,#clipFilterInput,#clipEffectInput,#transitionDurationInput,#audioLabelInput,#audioTimelineInput,#audioStartInput,#audioEndInput,#audioVolumeInput')) {
    applySelectionChanges();
  }
  const tab = e.target.dataset.libraryTab;
  if (tab) {
    state.libraryTab = tab;
    document.querySelectorAll('[data-library-tab]').forEach((btn) => btn.classList.toggle('active', btn.dataset.libraryTab === tab));
    renderLibrary();
  }
});

window.addEventListener('message', async (event) => {
  try {
    const data = event.data || {};
    if (!data || data.type !== 'astrabot-workspace-auth') return;
    if (typeof data.token === 'string' && data.token.trim()) {
      state.token = data.token.trim();
      saveTokenSilently();
      rerender();
      await loadLibrary();
    }
  } catch (e) {
    console.error(e);
  }
});

window.addEventListener('load', async () => {
  saveTokenSilently();
  configureLinks();
  rerender();
  if (state.token) {
    try { await loadLibrary(); } catch (e) { toast(e.message, 5000); }
  }
});
