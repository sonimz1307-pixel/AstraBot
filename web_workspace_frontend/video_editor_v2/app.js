const urlParams = new URLSearchParams(window.location.search);
const incomingApiBase = urlParams.get('api_base') || urlParams.get('apiBase') || '';
const incomingToken = urlParams.get('token') || urlParams.get('authToken') || '';
const incomingReturnUrl = urlParams.get('return_url') || urlParams.get('returnUrl') || '';

const apiBase = incomingApiBase || window.localStorage.getItem('astrabot:apiBaseUrl') || 'https://astrabot-tchj.onrender.com';
const FILTER_OPTIONS = [
  { value: 'none', label: 'Без фильтра' },
  { value: 'warm', label: 'Тёплый' },
  { value: 'cold', label: 'Холодный' },
  { value: 'bw', label: 'Ч/Б' },
  { value: 'cinematic', label: 'Cinematic' },
];
const EFFECT_OPTIONS = [
  { value: 'none', label: 'Без эффекта' },
  { value: 'zoomin', label: 'Zoom in' },
  { value: 'zoomout', label: 'Zoom out' },
  { value: 'panleft', label: 'Pan left' },
  { value: 'panright', label: 'Pan right' },
];
const TRANSITION_OPTIONS = [
  { value: 'none', label: 'Без перехода' },
  { value: 'fade', label: 'Fade' },
  { value: 'dissolve', label: 'Dissolve' },
  { value: 'slideleft', label: 'Slide left' },
  { value: 'slideright', label: 'Slide right' },
  { value: 'zoomin', label: 'Zoom in' },
];

const state = {
  token: incomingToken || window.localStorage.getItem('astrabot:authToken') || '',
  returnUrl: incomingReturnUrl || 'https://astrabot-workspace.onrender.com',
  libraryTab: 'videos',
  librarySearch: '',
  projectId: '',
  renderJobId: '',
  polling: null,
  selectedType: 'none',
  selectedId: '',
  videos: [],
  audioLibrary: [],
  playheadSec: 0,
  pxPerSec: 92,
  drag: {
    videoClipId: '',
    audioClipId: '',
    dropIndex: null,
    audioOffsetX: 0,
  },
  previewMode: 'timeline',
  project: {
    title: 'Новый видеопроект',
    video_clips: [],
    audio_tracks: [],
    text_overlays: [],
    stickers: [],
  },
};

function $(id) { return document.getElementById(id); }
function toast(message, ms = 3200) {
  const el = $('toast');
  if (!el) return;
  el.textContent = message;
  el.classList.remove('hidden');
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.add('hidden'), ms);
}
function authHeaders(json = true) {
  const headers = {};
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
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
function saveAuthSilently() {
  if (state.token) window.localStorage.setItem('astrabot:authToken', state.token);
  if (apiBase) window.localStorage.setItem('astrabot:apiBaseUrl', apiBase);
}
function clamp(v, min, max) { return Math.min(max, Math.max(min, v)); }
function fmtSec(v) {
  const n = Number(v || 0);
  if (!Number.isFinite(n)) return '0.0с';
  return `${n.toFixed(1)}с`;
}
function escapeHtml(v) {
  return String(v ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}
function slugText(v) {
  return String(v || '').trim().toLowerCase();
}
function findSourceById(id) {
  return [...state.videos, ...state.audioLibrary].find((x) => x.id === id) || null;
}
function currentItem() {
  if (state.selectedType === 'video') {
    return state.project.video_clips.find((x) => x.id === state.selectedId) || null;
  }
  if (state.selectedType === 'audio') {
    return state.project.audio_tracks.find((x) => x.id === state.selectedId) || null;
  }
  return null;
}
function currentVideo() {
  return state.selectedType === 'video' ? currentItem() : null;
}
function currentAudio() {
  return state.selectedType === 'audio' ? currentItem() : null;
}
function setSelection(type, id) {
  state.selectedType = type || 'none';
  state.selectedId = id || '';
}
function clearSelection() {
  setSelection('none', '');
}
function projectDuration() {
  const clips = state.project.video_clips || [];
  const videoEnd = clips.reduce((sum, clip) => sum + clipDuration(clip), 0);
  const audioEnd = (state.project.audio_tracks || []).reduce((maxEnd, item) => {
    const end = Number(item.timeline_start || 0) + audioDuration(item);
    return Math.max(maxEnd, end);
  }, 0);
  return Math.max(videoEnd, audioEnd, 5);
}
function clipDuration(clip) {
  return Math.max(0, Number(clip.source_end || 0) - Number(clip.source_start || 0));
}
function audioDuration(item) {
  return Math.max(0, Number(item.source_end || 0) - Number(item.source_start || 0));
}
function videoStartAtIndex(index) {
  let cursor = 0;
  for (let i = 0; i < index; i += 1) cursor += clipDuration(state.project.video_clips[i]);
  return cursor;
}
function getClipProjectStart(clipId) {
  const idx = state.project.video_clips.findIndex((x) => x.id === clipId);
  return idx >= 0 ? videoStartAtIndex(idx) : 0;
}
function timelineWidthPx() {
  return Math.max(760, Math.ceil(projectDuration() * state.pxPerSec) + 32);
}
function syncPlayheadLabel() {
  const label = `${state.playheadSec.toFixed(1)}с`;
  if ($('playheadBadge')) $('playheadBadge').textContent = `playhead ${label}`;
  if ($('timelinePlayheadLabel')) $('timelinePlayheadLabel').textContent = label;
  if ($('splitHint')) {
    const selected = currentItem();
    $('splitHint').textContent = selected
      ? `Playhead: ${label}. Активный элемент: ${selected.label || 'без названия'}. Разрезание пойдёт именно по этой линии.`
      : 'Выбери клип или музыку, затем поставь playhead в нужное место и нажми «Разрезать».';
  }
}
function setPlayhead(sec, { syncPreview = true } = {}) {
  const maxTime = projectDuration();
  state.playheadSec = clamp(Number(sec || 0), 0, maxTime);
  syncPlayheadLabel();
  positionPlayhead();
  if (syncPreview) syncPreviewToPlayhead();
}
function positionPlayhead() {
  const line = $('timelinePlayhead');
  if (!line) return;
  line.style.left = `${Math.round(state.playheadSec * state.pxPerSec)}px`;
}
function previewSourceForItem(item, type) {
  if (!item) return '';
  const source = findSourceById(item.source_id);
  if (!source) return '';
  return source.video_url || source.download_url || source.output_url || '';
}
function syncPreviewToPlayhead() {
  const videoEl = $('previewVideo');
  const clip = currentVideo() || state.project.video_clips[0] || null;
  if (!videoEl || !clip) return;
  if (state.selectedType !== 'video' && state.previewMode !== 'timeline') return;
  const clipStart = getClipProjectStart(clip.id);
  const localSec = clamp(state.playheadSec - clipStart, 0, clipDuration(clip));
  const targetTime = Number(clip.source_start || 0) + localSec;
  if (Number.isFinite(targetTime) && Math.abs((videoEl.currentTime || 0) - targetTime) > 0.35) {
    try { videoEl.currentTime = targetTime; } catch (_) {}
  }
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
    hint.textContent = 'Загружай видео и музыку, добавляй ролики из генераций и собирай монтаж.';
    banner.classList.add('hidden');
  } else {
    status.textContent = 'нет авторизации';
    status.className = 'badge muted';
    hint.textContent = 'История генераций и сохранение проекта появятся после открытия редактора из Workspace.';
    banner.classList.remove('hidden');
  }
}
function renderPreview() {
  const videoEl = $('previewVideo');
  const empty = $('previewEmpty');
  const meta = $('previewMeta');
  const selected = currentVideo() || state.project.video_clips[0] || null;
  const src = previewSourceForItem(selected, 'video');

  if (src && selected) {
    if (videoEl.getAttribute('src') !== src) {
      videoEl.src = src;
      videoEl.load();
    }
    videoEl.classList.remove('hidden');
    empty.classList.add('hidden');
    const source = findSourceById(selected.source_id);
    meta.textContent = `${selected.label || 'Клип'} · ${fmtSec(clipDuration(selected))} · ${source?.provider || source?.file_type || 'source'}`;
  } else {
    videoEl.removeAttribute('src');
    videoEl.load?.();
    videoEl.classList.add('hidden');
    empty.classList.remove('hidden');
    meta.textContent = 'Выберите клип на таймлайне или добавьте новый ролик.';
  }

  $('projectTitleInput').value = state.project.title || 'Новый видеопроект';
  $('projectStatus').textContent = state.projectId ? 'сохранён' : 'черновик';
  $('projectSummary').textContent = `${state.project.video_clips.length} клипов · ${fmtSec(projectDuration())}`;
  syncPlayheadLabel();
  setTimeout(() => syncPreviewToPlayhead(), 10);
}
function filteredLibraryItems() {
  const items = state.libraryTab === 'videos' ? state.videos : state.audioLibrary;
  const q = slugText(state.librarySearch);
  if (!q) return items;
  return items.filter((item) => slugText(item.filename || item.prompt || item.id).includes(q));
}
function formatLibraryMeta(item) {
  const parts = [];
  if (item.duration_sec || item.duration_sec === 0) parts.push(fmtSec(item.duration_sec));
  if (item.provider) parts.push(item.provider);
  if (item.file_type) parts.push(item.file_type);
  if (item.created_at) parts.push(new Date(item.created_at).toLocaleString('ru-RU'));
  return parts.join(' · ') || '—';
}
function renderLibrary() {
  document.querySelectorAll('[data-library-tab]').forEach((btn) => btn.classList.toggle('active', btn.dataset.libraryTab === state.libraryTab));
  const items = filteredLibraryItems();
  const list = $('libraryList');
  if (!list) return;
  if (!items.length) {
    list.innerHTML = `<div class="empty-box">${state.token ? 'Ничего не найдено. Загрузите файл или очистите поиск.' : 'История генераций и загрузки недоступны без авторизации Workspace.'}</div>`;
    return;
  }
  list.innerHTML = items.map((item) => {
    const title = escapeHtml(item.filename || item.prompt || item.id);
    const meta = escapeHtml(formatLibraryMeta(item));
    return `
      <div class="library-item">
        <div>
          <strong>${title}</strong>
          <small>${meta}</small>
        </div>
        <div class="library-actions">
          <button class="btn secondary" data-library-add="${item.id}">${state.libraryTab === 'videos' ? 'Добавить' : 'Добавить музыку'}</button>
          <button class="btn ghost" data-library-preview="${item.id}">Превью</button>
        </div>
      </div>`;
  }).join('');
}
async function loadLibrary() {
  if (!state.token) {
    state.videos = [];
    state.audioLibrary = [];
    renderLibrary();
    renderSessionState();
    return;
  }
  const [videosRes, uploadedVideosRes, audioRes] = await Promise.all([
    api('/api/video-editor-v2/library/videos', { headers: authHeaders(false) }),
    api('/api/video-editor-v2/library/uploads?file_type=video', { headers: authHeaders(false) }),
    api('/api/video-editor-v2/library/uploads?file_type=audio', { headers: authHeaders(false) }),
  ]);
  const mergedMap = new Map();
  [...(uploadedVideosRes.items || []), ...(videosRes.items || [])].forEach((item) => mergedMap.set(item.id, item));
  state.videos = Array.from(mergedMap.values());
  state.audioLibrary = audioRes.items || [];
  renderLibrary();
  renderSessionState();
}
function ensureVideoSelection() {
  if (state.selectedType === 'video' && state.project.video_clips.some((x) => x.id === state.selectedId)) return;
  if (state.project.video_clips.length) {
    setSelection('video', state.project.video_clips[0].id);
  } else if (state.project.audio_tracks.length) {
    setSelection('audio', state.project.audio_tracks[0].id);
  } else {
    clearSelection();
  }
}
function makeVideoClipFromLibrary(item) {
  const duration = Math.max(0.5, Number(item.duration_sec || 5));
  return {
    id: crypto.randomUUID(),
    source_type: item.file_type === 'video' ? 'upload' : 'generation',
    source_id: item.id,
    label: item.filename || item.prompt || `Клип ${state.project.video_clips.length + 1}`,
    source_start: 0,
    source_end: duration,
    volume: 100,
    muted: false,
    filter: 'none',
    effect: 'none',
    transition: { type: state.project.video_clips.length ? 'fade' : 'none', duration: state.project.video_clips.length ? 0.5 : 0 },
  };
}
function makeAudioClipFromLibrary(item) {
  const duration = Math.max(0.5, Number(item.duration_sec || 5));
  return {
    id: crypto.randomUUID(),
    source_id: item.id,
    label: item.filename || 'Музыка',
    timeline_start: 0,
    source_start: 0,
    source_end: duration,
    volume: 100,
  };
}
function addVideoFromLibrary(id) {
  const item = state.videos.find((x) => x.id === id);
  if (!item) return;
  const clip = makeVideoClipFromLibrary(item);
  state.project.video_clips.push(clip);
  setSelection('video', clip.id);
  setPlayhead(getClipProjectStart(clip.id), { syncPreview: false });
  rerender();
}
function addAudioFromLibrary(id) {
  const item = state.audioLibrary.find((x) => x.id === id);
  if (!item) return;
  const audio = makeAudioClipFromLibrary(item);
  const endOfVideo = state.project.video_clips.reduce((sum, clip) => sum + clipDuration(clip), 0);
  audio.timeline_start = endOfVideo > 0 ? 0 : 0;
  state.project.audio_tracks.push(audio);
  setSelection('audio', audio.id);
  setPlayhead(Number(audio.timeline_start || 0), { syncPreview: false });
  rerender();
}
function renderOptionChips(options, value, datasetName) {
  return `
    <div class="chip-row">
      ${options.map((opt) => `
        <button class="option-chip ${opt.value === value ? 'active' : ''}" data-${datasetName}="${opt.value}">${escapeHtml(opt.label)}</button>
      `).join('')}
    </div>`;
}
function renderSelectionPanel() {
  const title = $('selectionTitle');
  const body = $('selectionBody');
  if (!title || !body) return;
  const clip = currentVideo();
  const audio = currentAudio();
  if (clip) {
    const source = findSourceById(clip.source_id);
    const start = getClipProjectStart(clip.id);
    title.textContent = 'Настройки клипа';
    body.innerHTML = `
      <div class="inspector-summary">
        <strong>${escapeHtml(clip.label || 'Клип')}</strong>
        <div class="muted">Источник: ${escapeHtml(source?.filename || source?.prompt || clip.source_type || 'media')} · ${fmtSec(clipDuration(clip))} · старт в проекте ${fmtSec(start)}</div>
      </div>
      <div class="inspector-card">
        <div class="kv-grid">
          <label class="kv-row"><span>Название</span><input id="clipLabelInput" value="${escapeHtml(clip.label || '')}"></label>
          <label class="kv-row"><span>Громкость, %</span><input id="clipVolumeInput" type="number" min="0" max="100" step="1" value="${Number(clip.volume || 100)}"></label>
          <label class="kv-row"><span>Старт фрагмента, сек</span><input id="clipStartInput" type="number" min="0" step="0.1" value="${Number(clip.source_start || 0)}"></label>
          <label class="kv-row"><span>Конец фрагмента, сек</span><input id="clipEndInput" type="number" min="0" step="0.1" value="${Number(clip.source_end || 0)}"></label>
          <label class="kv-row"><span>Длит. перехода, сек</span><input id="transitionDurationInput" type="number" min="0" max="1" step="0.1" value="${Number(clip.transition?.duration || 0)}"></label>
        </div>
      </div>
      <div class="inspector-card option-block">
        <div class="inspector-label">Переход</div>
        ${renderOptionChips(TRANSITION_OPTIONS, clip.transition?.type || 'none', 'clipTransition')}
      </div>
      <div class="inspector-card option-block">
        <div class="inspector-label">Фильтр</div>
        ${renderOptionChips(FILTER_OPTIONS, clip.filter || 'none', 'clipFilter')}
      </div>
      <div class="inspector-card option-block">
        <div class="inspector-label">Эффект</div>
        ${renderOptionChips(EFFECT_OPTIONS, clip.effect || 'none', 'clipEffect')}
      </div>
      <div class="inspector-card">
        <div class="inspector-actions">
          <button class="tool" data-action="set-in-from-playhead">Поставить start по playhead</button>
          <button class="tool" data-action="set-out-from-playhead">Поставить end по playhead</button>
        </div>
      </div>`;
    return;
  }
  if (audio) {
    const source = findSourceById(audio.source_id);
    title.textContent = 'Настройки музыки';
    body.innerHTML = `
      <div class="inspector-summary">
        <strong>${escapeHtml(audio.label || 'Музыка')}</strong>
        <div class="muted">Источник: ${escapeHtml(source?.filename || 'audio')} · ${fmtSec(audioDuration(audio))} · старт в проекте ${fmtSec(audio.timeline_start || 0)}</div>
      </div>
      <div class="inspector-card">
        <div class="kv-grid">
          <label class="kv-row"><span>Название</span><input id="audioLabelInput" value="${escapeHtml(audio.label || '')}"></label>
          <label class="kv-row"><span>Громкость, %</span><input id="audioVolumeInput" type="number" min="0" max="100" step="1" value="${Number(audio.volume || 100)}"></label>
          <label class="kv-row"><span>Старт на таймлайне, сек</span><input id="audioTimelineInput" type="number" min="0" step="0.1" value="${Number(audio.timeline_start || 0)}"></label>
          <label class="kv-row"><span>Старт фрагмента, сек</span><input id="audioStartInput" type="number" min="0" step="0.1" value="${Number(audio.source_start || 0)}"></label>
          <label class="kv-row"><span>Конец фрагмента, сек</span><input id="audioEndInput" type="number" min="0" step="0.1" value="${Number(audio.source_end || 0)}"></label>
        </div>
      </div>
      <div class="inspector-card">
        <div class="inspector-actions">
          <button class="tool" data-action="set-audio-start-from-playhead">Сдвинуть start в проекте по playhead</button>
        </div>
      </div>`;
    return;
  }
  title.textContent = 'Настройки элемента';
  body.innerHTML = '<div class="empty-box">Выберите клип или музыкальный кусок на таймлайне, чтобы отредактировать параметры.</div>';
}
function renderRuler() {
  const total = Math.ceil(projectDuration());
  const ruler = $('timelineRuler');
  if (!ruler) return;
  ruler.style.width = `${timelineWidthPx()}px`;
  ruler.innerHTML = Array.from({ length: total + 1 }, (_, i) => {
    const left = i * state.pxPerSec;
    return `<div class="tick" style="left:${left}px"><span>${i}с</span></div>`;
  }).join('');
}
function renderTrackDropMarker(trackEl) {
  if (!trackEl) return;
  trackEl.querySelectorAll('.track-drop-marker').forEach((node) => node.remove());
  if (state.drag.dropIndex == null) return;
  const marker = document.createElement('div');
  marker.className = 'track-drop-marker';
  let left = 8;
  if (state.project.video_clips.length) {
    const dropIndex = Math.min(state.drag.dropIndex, state.project.video_clips.length);
    if (dropIndex > 0) {
      const prev = state.project.video_clips[dropIndex - 1];
      left = getClipProjectStart(prev.id) * state.pxPerSec + clipDuration(prev) * state.pxPerSec;
    }
  }
  marker.style.left = `${left}px`;
  trackEl.appendChild(marker);
}
function renderVideoTrack() {
  const track = $('videoTrack');
  if (!track) return;
  track.style.width = `${timelineWidthPx()}px`;
  track.innerHTML = '';
  if (!state.project.video_clips.length) {
    track.innerHTML = '<div class="track-empty">Добавьте ролик из библиотеки или загрузите видео.</div>';
    return;
  }
  state.project.video_clips.forEach((clip, index) => {
    const start = videoStartAtIndex(index);
    const width = Math.max(82, clipDuration(clip) * state.pxPerSec);
    const item = document.createElement('button');
    item.type = 'button';
    item.className = `timeline-item video ${state.selectedType === 'video' && state.selectedId === clip.id ? 'active' : ''}`;
    item.dataset.clipId = clip.id;
    item.draggable = true;
    item.style.left = `${start * state.pxPerSec}px`;
    item.style.width = `${width}px`;
    item.innerHTML = `
      <span class="item-handle left"></span>
      <span class="item-handle right"></span>
      <div class="timeline-title">${escapeHtml(clip.label || `Клип ${index + 1}`)}</div>
      <div class="timeline-meta">${fmtSec(clipDuration(clip))} · ${escapeHtml(clip.filter || 'без фильтра')} · ${clip.muted ? 'mute' : `${Number(clip.volume || 100)}%`}</div>`;
    track.appendChild(item);
  });
  renderTrackDropMarker(track);
}
function renderAudioTrack() {
  const track = $('audioTrack');
  if (!track) return;
  track.style.width = `${timelineWidthPx()}px`;
  track.innerHTML = '';
  if (!state.project.audio_tracks.length) {
    track.innerHTML = '<div class="track-empty">Добавьте музыку. Её можно разрезать по красной линии и двигать по таймлайну.</div>';
    return;
  }
  state.project.audio_tracks.forEach((item) => {
    const width = Math.max(90, audioDuration(item) * state.pxPerSec);
    const node = document.createElement('button');
    node.type = 'button';
    node.className = `timeline-item audio ${state.selectedType === 'audio' && state.selectedId === item.id ? 'active' : ''}`;
    node.dataset.audioClipId = item.id;
    node.draggable = true;
    node.style.left = `${Number(item.timeline_start || 0) * state.pxPerSec}px`;
    node.style.width = `${width}px`;
    node.innerHTML = `
      <span class="item-handle left"></span>
      <span class="item-handle right"></span>
      <div class="timeline-title">${escapeHtml(item.label || 'Музыка')}</div>
      <div class="timeline-meta">старт ${fmtSec(item.timeline_start || 0)} · ${fmtSec(audioDuration(item))} · ${Number(item.volume || 100)}%</div>`;
    track.appendChild(node);
  });
}
function renderTimeline() {
  const canvas = $('timelineCanvas');
  if (canvas) canvas.style.width = `${timelineWidthPx()}px`;
  if ($('zoomLabel')) $('zoomLabel').textContent = `${state.pxPerSec} px/с`;
  renderRuler();
  renderVideoTrack();
  renderAudioTrack();
  positionPlayhead();
}
function applyFormChanges() {
  const clip = currentVideo();
  const audio = currentAudio();
  if (clip) {
    const source = findSourceById(clip.source_id);
    const sourceDuration = Number(source?.duration_sec || clip.source_end || 0);
    clip.label = $('clipLabelInput')?.value?.trim() || clip.label;
    clip.volume = clamp(Number($('clipVolumeInput')?.value || 100), 0, 100);
    clip.source_start = clamp(Number($('clipStartInput')?.value || 0), 0, Math.max(0, sourceDuration - 0.1));
    clip.source_end = clamp(Number($('clipEndInput')?.value || sourceDuration || 0), clip.source_start + 0.1, Math.max(clip.source_start + 0.1, sourceDuration || clip.source_end || clip.source_start + 0.1));
    clip.transition.duration = clamp(Number($('transitionDurationInput')?.value || 0), 0, 1);
    if (clip.volume === 0) clip.muted = true; else if (clip.muted && clip.volume > 0) clip.muted = false;
  }
  if (audio) {
    const source = findSourceById(audio.source_id);
    const sourceDuration = Number(source?.duration_sec || audio.source_end || 0);
    audio.label = $('audioLabelInput')?.value?.trim() || audio.label;
    audio.volume = clamp(Number($('audioVolumeInput')?.value || 100), 0, 100);
    audio.timeline_start = clamp(Number($('audioTimelineInput')?.value || 0), 0, projectDuration());
    audio.source_start = clamp(Number($('audioStartInput')?.value || 0), 0, Math.max(0, sourceDuration - 0.1));
    audio.source_end = clamp(Number($('audioEndInput')?.value || sourceDuration || 0), audio.source_start + 0.1, Math.max(audio.source_start + 0.1, sourceDuration || audio.source_end || audio.source_start + 0.1));
  }
  rerender({ preservePlayhead: true });
}
function getTimelineSecondsFromClientX(clientX) {
  const scroller = $('timelineScroller');
  if (!scroller) return 0;
  const rect = scroller.getBoundingClientRect();
  const x = clientX - rect.left + scroller.scrollLeft;
  return clamp(x / state.pxPerSec, 0, projectDuration());
}
function previewLibraryItem(id) {
  const item = findSourceById(id);
  const src = item?.video_url || item?.download_url || item?.output_url || '';
  const videoEl = $('previewVideo');
  const empty = $('previewEmpty');
  if (!src || !videoEl) return;
  state.previewMode = 'library';
  videoEl.src = src;
  videoEl.load();
  videoEl.classList.remove('hidden');
  empty.classList.add('hidden');
  $('previewMeta').textContent = `${item.filename || item.prompt || item.id} · ${fmtSec(item.duration_sec || 0)}`;
}
function rerender({ preservePlayhead = false } = {}) {
  configureLinks();
  renderSessionState();
  ensureVideoSelection();
  renderLibrary();
  renderPreview();
  renderTimeline();
  renderSelectionPanel();
  if (!preservePlayhead) setPlayhead(state.playheadSec, { syncPreview: false });
  positionPlayhead();
}
async function uploadMedia(kind, file) {
  if (!file) return;
  if (!state.token) return toast('Загрузка доступна только из Workspace');
  const form = new FormData();
  form.append('file', file);
  const data = await api(`/api/video-editor-v2/upload/${kind}`, { method: 'POST', headers: authHeaders(false), body: form });
  toast(kind === 'video' ? 'Видео загружено' : 'Музыка загружена');
  await loadLibrary();
  state.libraryTab = kind === 'video' ? 'videos' : 'audio';
  const itemId = data?.item?.id;
  if (itemId) {
    if (kind === 'video') addVideoFromLibrary(itemId); else addAudioFromLibrary(itemId);
  }
  rerender({ preservePlayhead: true });
}
async function saveProject() {
  const title = $('projectTitleInput')?.value?.trim() || 'Новый видеопроект';
  state.project.title = title;
  if (!state.token) return toast('Открой редактор из Workspace, чтобы сохранить проект');
  const payload = { title, project_json: state.project };
  let data;
  if (state.projectId) {
    data = await api(`/api/video-editor-v2/projects/${encodeURIComponent(state.projectId)}`, { method: 'PUT', headers: authHeaders(), body: JSON.stringify(payload) });
  } else {
    data = await api('/api/video-editor-v2/projects', { method: 'POST', headers: authHeaders(), body: JSON.stringify(payload) });
  }
  state.projectId = data.item.id;
  toast('Проект сохранён');
  rerender({ preservePlayhead: true });
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
      if (item.output_url) {
        const videoEl = $('previewVideo');
        videoEl.src = item.output_url;
        videoEl.classList.remove('hidden');
        $('previewEmpty').classList.add('hidden');
        $('previewMeta').textContent = `Результат рендера · ${fmtSec(item.duration_sec || 0)}`;
      }
      return;
    }
    if (item.status === 'failed') {
      toast(item.error_text || 'Ошибка рендера', 6000);
      return;
    }
  } catch (e) {
    toast(e.message || String(e), 5000);
    return;
  }
  state.polling = setTimeout(pollRender, 2500);
}
function removeSelected() {
  if (state.selectedType === 'video' && state.selectedId) {
    state.project.video_clips = state.project.video_clips.filter((x) => x.id !== state.selectedId);
  }
  if (state.selectedType === 'audio' && state.selectedId) {
    state.project.audio_tracks = state.project.audio_tracks.filter((x) => x.id !== state.selectedId);
  }
  clearSelection();
  rerender({ preservePlayhead: true });
}
function moveSelectedVideo(delta) {
  const clip = currentVideo();
  if (!clip) return toast('Сначала выбери клип');
  const idx = state.project.video_clips.findIndex((x) => x.id === clip.id);
  const next = idx + delta;
  if (idx < 0 || next < 0 || next >= state.project.video_clips.length) return;
  const arr = state.project.video_clips;
  [arr[idx], arr[next]] = [arr[next], arr[idx]];
  rerender({ preservePlayhead: true });
}
function toggleMuteSelected() {
  const clip = currentVideo();
  if (!clip) return toast('Сначала выбери клип');
  clip.muted = !clip.muted;
  clip.volume = clip.muted ? 0 : Math.max(1, Number(clip.volume || 100));
  rerender({ preservePlayhead: true });
}
function splitSelectedAtPlayhead() {
  const clip = currentVideo();
  if (clip) {
    const idx = state.project.video_clips.findIndex((x) => x.id === clip.id);
    const clipStart = getClipProjectStart(clip.id);
    const local = Number((state.playheadSec - clipStart).toFixed(3));
    const duration = clipDuration(clip);
    if (local <= 0.05 || local >= duration - 0.05) return toast('Поставь playhead внутрь клипа, а не на край');
    const splitSourceTime = Number(clip.source_start || 0) + local;
    const a = {
      ...clip,
      id: crypto.randomUUID(),
      label: `${clip.label} A`,
      source_end: splitSourceTime,
      transition: { ...clip.transition },
    };
    const b = {
      ...clip,
      id: crypto.randomUUID(),
      label: `${clip.label} B`,
      source_start: splitSourceTime,
      transition: { ...clip.transition },
    };
    state.project.video_clips.splice(idx, 1, a, b);
    setSelection('video', b.id);
    rerender({ preservePlayhead: true });
    return;
  }
  const audio = currentAudio();
  if (audio) {
    const local = Number((state.playheadSec - Number(audio.timeline_start || 0)).toFixed(3));
    const duration = audioDuration(audio);
    if (local <= 0.05 || local >= duration - 0.05) return toast('Поставь playhead внутрь музыкального куска');
    const idx = state.project.audio_tracks.findIndex((x) => x.id === audio.id);
    const splitSourceTime = Number(audio.source_start || 0) + local;
    const splitTimeline = Number(audio.timeline_start || 0) + local;
    const a = { ...audio, id: crypto.randomUUID(), label: `${audio.label} A`, source_end: splitSourceTime };
    const b = { ...audio, id: crypto.randomUUID(), label: `${audio.label} B`, source_start: splitSourceTime, timeline_start: splitTimeline };
    state.project.audio_tracks.splice(idx, 1, a, b);
    setSelection('audio', b.id);
    rerender({ preservePlayhead: true });
    return;
  }
  toast('Выбери клип или музыку, чтобы разрезать по playhead');
}
function setClipBoundaryFromPlayhead(kind) {
  const clip = currentVideo();
  if (!clip) return toast('Сначала выбери клип');
  const clipStart = getClipProjectStart(clip.id);
  const local = clamp(state.playheadSec - clipStart, 0, clipDuration(clip));
  const sourceValue = Number(clip.source_start || 0) + local;
  if (kind === 'start') {
    clip.source_start = Math.min(sourceValue, Number(clip.source_end || 0) - 0.1);
  } else {
    clip.source_end = Math.max(sourceValue, Number(clip.source_start || 0) + 0.1);
  }
  rerender({ preservePlayhead: true });
}
function setAudioTimelineStartFromPlayhead() {
  const audio = currentAudio();
  if (!audio) return toast('Сначала выбери музыку');
  audio.timeline_start = clamp(state.playheadSec, 0, projectDuration());
  rerender({ preservePlayhead: true });
}
function updateClipOption(datasetName, value) {
  const clip = currentVideo();
  if (!clip) return;
  if (datasetName === 'clipFilter') clip.filter = value;
  if (datasetName === 'clipEffect') clip.effect = value;
  if (datasetName === 'clipTransition') clip.transition.type = value;
  rerender({ preservePlayhead: true });
}
function reorderClipByDrop(clipId, clientX) {
  const draggedIndex = state.project.video_clips.findIndex((x) => x.id === clipId);
  if (draggedIndex < 0) return;
  const sec = getTimelineSecondsFromClientX(clientX);
  let dropIndex = state.project.video_clips.length;
  let cursor = 0;
  for (let i = 0; i < state.project.video_clips.length; i += 1) {
    const dur = clipDuration(state.project.video_clips[i]);
    const mid = cursor + dur / 2;
    if (sec < mid) {
      dropIndex = i;
      break;
    }
    cursor += dur;
  }
  const arr = [...state.project.video_clips];
  const [item] = arr.splice(draggedIndex, 1);
  const adjusted = dropIndex > draggedIndex ? dropIndex - 1 : dropIndex;
  arr.splice(adjusted, 0, item);
  state.project.video_clips = arr;
  state.drag.videoClipId = '';
  state.drag.dropIndex = null;
  setSelection('video', item.id);
  rerender({ preservePlayhead: true });
}
function updateDropMarker(clientX) {
  const sec = getTimelineSecondsFromClientX(clientX);
  let dropIndex = state.project.video_clips.length;
  let cursor = 0;
  for (let i = 0; i < state.project.video_clips.length; i += 1) {
    const dur = clipDuration(state.project.video_clips[i]);
    const mid = cursor + dur / 2;
    if (sec < mid) {
      dropIndex = i;
      break;
    }
    cursor += dur;
  }
  state.drag.dropIndex = dropIndex;
  renderTrackDropMarker($('videoTrack'));
}
function moveAudioByDrop(audioId, clientX) {
  const audio = state.project.audio_tracks.find((x) => x.id === audioId);
  if (!audio) return;
  const sec = getTimelineSecondsFromClientX(clientX);
  const offsetSec = state.drag.audioOffsetX / state.pxPerSec;
  audio.timeline_start = clamp(sec - offsetSec, 0, projectDuration());
  state.drag.audioClipId = '';
  state.drag.audioOffsetX = 0;
  setSelection('audio', audio.id);
  rerender({ preservePlayhead: true });
}

$('timelineScroller')?.addEventListener('click', (e) => {
  if (e.target.closest('.timeline-item')) return;
  setPlayhead(getTimelineSecondsFromClientX(e.clientX));
});
$('timelineRuler')?.addEventListener('click', (e) => setPlayhead(getTimelineSecondsFromClientX(e.clientX)));
$('videoTrack')?.addEventListener('dragover', (e) => {
  if (!state.drag.videoClipId) return;
  e.preventDefault();
  updateDropMarker(e.clientX);
});
$('videoTrack')?.addEventListener('drop', (e) => {
  if (!state.drag.videoClipId) return;
  e.preventDefault();
  reorderClipByDrop(state.drag.videoClipId, e.clientX);
});
$('audioTrack')?.addEventListener('dragover', (e) => {
  if (!state.drag.audioClipId) return;
  e.preventDefault();
});
$('audioTrack')?.addEventListener('drop', (e) => {
  if (!state.drag.audioClipId) return;
  e.preventDefault();
  moveAudioByDrop(state.drag.audioClipId, e.clientX);
});
$('zoomRange')?.addEventListener('input', (e) => {
  state.pxPerSec = Number(e.target.value || 92);
  rerender({ preservePlayhead: true });
});
$('previewVideo')?.addEventListener('loadedmetadata', () => syncPreviewToPlayhead());
$('previewVideo')?.addEventListener('timeupdate', () => {
  const clip = currentVideo();
  if (!clip || document.activeElement?.id === 'previewVideo') return;
});

document.addEventListener('dragstart', (e) => {
  const clipEl = e.target.closest?.('[data-clip-id]');
  if (clipEl) {
    state.drag.videoClipId = clipEl.dataset.clipId;
    clipEl.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
    return;
  }
  const audioEl = e.target.closest?.('[data-audio-clip-id]');
  if (audioEl) {
    state.drag.audioClipId = audioEl.dataset.audioClipId;
    const rect = audioEl.getBoundingClientRect();
    state.drag.audioOffsetX = e.clientX - rect.left;
    audioEl.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
  }
});
document.addEventListener('dragend', () => {
  document.querySelectorAll('.dragging').forEach((el) => el.classList.remove('dragging'));
  state.drag.videoClipId = '';
  state.drag.audioClipId = '';
  state.drag.dropIndex = null;
  state.drag.audioOffsetX = 0;
  renderTrackDropMarker($('videoTrack'));
});

document.addEventListener('click', async (e) => {
  const addId = e.target.dataset.libraryAdd;
  if (addId) {
    if (state.libraryTab === 'videos') addVideoFromLibrary(addId); else addAudioFromLibrary(addId);
    return;
  }
  const previewId = e.target.dataset.libraryPreview;
  if (previewId) {
    previewLibraryItem(previewId);
    return;
  }
  const tab = e.target.dataset.libraryTab;
  if (tab) {
    state.libraryTab = tab;
    renderLibrary();
    return;
  }
  const clipNode = e.target.closest?.('[data-clip-id]');
  const clipId = clipNode?.dataset?.clipId;
  if (clipId) {
    state.previewMode = 'timeline';
    setSelection('video', clipId);
    const rect = clipNode.getBoundingClientRect();
    const local = clamp((e.clientX - rect.left) / state.pxPerSec, 0, clipDuration(state.project.video_clips.find((x) => x.id === clipId)));
    setPlayhead(getClipProjectStart(clipId) + local);
    rerender({ preservePlayhead: true });
    return;
  }
  const audioNode = e.target.closest?.('[data-audio-clip-id]');
  const audioClipId = audioNode?.dataset?.audioClipId;
  if (audioClipId) {
    const audio = state.project.audio_tracks.find((x) => x.id === audioClipId);
    state.previewMode = 'timeline';
    setSelection('audio', audioClipId);
    const rect = audioNode.getBoundingClientRect();
    const local = clamp((e.clientX - rect.left) / state.pxPerSec, 0, audioDuration(audio));
    setPlayhead(Number(audio?.timeline_start || 0) + local, { syncPreview: false });
    rerender({ preservePlayhead: true });
    return;
  }
  if (e.target.id === 'refreshLibraryBtn') { loadLibrary().catch((err) => toast(err.message, 5000)); return; }
  if (e.target.id === 'saveProjectBtn') { saveProject().catch((err) => toast(err.message, 5000)); return; }
  if (e.target.id === 'renderBtn') { startRender().catch((err) => toast(err.message, 6000)); return; }
  if (e.target.id === 'deleteClipBtn') { removeSelected(); return; }
  if (e.target.id === 'muteClipBtn') { toggleMuteSelected(); return; }
  if (e.target.id === 'splitBtn') { splitSelectedAtPlayhead(); return; }
  if (e.target.id === 'moveLeftBtn') { moveSelectedVideo(-1); return; }
  if (e.target.id === 'moveRightBtn') { moveSelectedVideo(1); return; }

  if (e.target.dataset.clipFilter) { updateClipOption('clipFilter', e.target.dataset.clipFilter); return; }
  if (e.target.dataset.clipEffect) { updateClipOption('clipEffect', e.target.dataset.clipEffect); return; }
  if (e.target.dataset.clipTransition) { updateClipOption('clipTransition', e.target.dataset.clipTransition); return; }

  const action = e.target.dataset.action;
  if (action === 'set-in-from-playhead') { setClipBoundaryFromPlayhead('start'); return; }
  if (action === 'set-out-from-playhead') { setClipBoundaryFromPlayhead('end'); return; }
  if (action === 'set-audio-start-from-playhead') { setAudioTimelineStartFromPlayhead(); return; }
});

document.addEventListener('change', (e) => {
  if (e.target.id === 'videoUploadInput') uploadMedia('video', e.target.files?.[0]).catch((err) => toast(err.message, 5000));
  if (e.target.id === 'audioUploadInput') uploadMedia('audio', e.target.files?.[0]).catch((err) => toast(err.message, 5000));
  if (e.target.matches('#clipLabelInput,#clipVolumeInput,#clipStartInput,#clipEndInput,#transitionDurationInput,#audioLabelInput,#audioVolumeInput,#audioTimelineInput,#audioStartInput,#audioEndInput')) {
    applyFormChanges();
  }
});
$('librarySearchInput')?.addEventListener('input', (e) => {
  state.librarySearch = e.target.value || '';
  renderLibrary();
});
$('projectTitleInput')?.addEventListener('input', (e) => {
  state.project.title = e.target.value || 'Новый видеопроект';
  $('projectSummary').textContent = `${state.project.video_clips.length} клипов · ${fmtSec(projectDuration())}`;
});
window.addEventListener('message', async (event) => {
  try {
    const data = event.data || {};
    if (!data || data.type !== 'astrabot-workspace-auth') return;
    if (typeof data.token === 'string' && data.token.trim()) {
      state.token = data.token.trim();
      saveAuthSilently();
      rerender({ preservePlayhead: true });
      await loadLibrary();
      rerender({ preservePlayhead: true });
    }
  } catch (e) {
    console.error(e);
  }
});
window.addEventListener('load', async () => {
  saveAuthSilently();
  configureLinks();
  setPlayhead(0, { syncPreview: false });
  rerender({ preservePlayhead: true });
  if (state.token) {
    try { await loadLibrary(); } catch (e) { toast(e.message, 5000); }
  }
  rerender({ preservePlayhead: true });
});
