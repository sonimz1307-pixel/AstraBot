const urlParams = new URLSearchParams(window.location.search);
const incomingApiBase = urlParams.get('api_base') || urlParams.get('apiBase') || '';
const incomingToken = urlParams.get('token') || urlParams.get('authToken') || '';
const incomingReturnUrl = urlParams.get('return_url') || urlParams.get('returnUrl') || '';
const incomingProjectId = urlParams.get('project_id') || '';

const apiBase = incomingApiBase || window.localStorage.getItem('astrabot:apiBaseUrl') || 'https://astrabot-tchj.onrender.com';
const state = {
  token: incomingToken || window.localStorage.getItem('astrabot:authToken') || '',
  authReady: false,
  libraryTab: 'videos',
  librarySearch: '',
  projectId: incomingProjectId || '',
  selectedClipId: '',
  selectedAudioId: '',
  renderJobId: '',
  renderOutputUrl: '',
  polling: null,
  videos: [],
  audio: [],
  returnUrl: incomingReturnUrl || window.localStorage.getItem('astrabot:returnUrl') || 'https://astrabot-workspace.onrender.com',
  pxPerSec: 72,
  project: {
    title: 'Новый видеопроект',
    canvas: { width: 1080, height: 1920, fps: 30 },
    video_clips: [],
    audio_tracks: [],
    text_overlays: [],
    stickers: [],
  },
};

function $(id) { return document.getElementById(id); }
function escapeHtml(value = '') {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}
function toast(message, ms = 2600) {
  const el = $('toast');
  if (!el) return;
  el.textContent = String(message || 'Готово');
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
function clearAuth() {
  state.token = '';
  state.authReady = true;
  window.localStorage.removeItem('astrabot:authToken');
}
async function api(path, options = {}) {
  const res = await fetch(`${apiBase}${path}`, options);
  const text = await res.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch { data = { detail: text }; }
  if (res.status === 401) {
    clearAuth();
    rerender();
  }
  if (!res.ok) throw new Error(data.detail || 'API error');
  return data;
}
function saveTokenSilently() {
  if (state.token) window.localStorage.setItem('astrabot:authToken', state.token);
  if (apiBase) window.localStorage.setItem('astrabot:apiBaseUrl', apiBase);
  if (state.returnUrl) window.localStorage.setItem('astrabot:returnUrl', state.returnUrl);
}

function currentClip() {
  return state.project.video_clips.find((item) => item.id === state.selectedClipId) || null;
}
function currentAudio() {
  return state.project.audio_tracks.find((item) => item.id === state.selectedAudioId) || null;
}
function fmtSec(value) {
  const n = Math.max(0, Number(value || 0));
  return `${n.toFixed(1)}с`;
}
function fmtDate(value) {
  if (!value) return '—';
  try { return new Date(value).toLocaleString('ru-RU'); } catch { return String(value); }
}
function projectDuration() {
  return state.project.video_clips.reduce((sum, item) => {
    const start = Number(item.source_start || 0);
    const end = Number(item.source_end || 0);
    return sum + Math.max(0, end - start);
  }, 0);
}
function timelineWidthPx() {
  return Math.max(5, Math.ceil(projectDuration() || 0)) * state.pxPerSec;
}
function syncSelection() {
  const clipExists = state.project.video_clips.some((item) => item.id === state.selectedClipId);
  const audioExists = state.project.audio_tracks.some((item) => item.id === state.selectedAudioId);
  if (!clipExists) state.selectedClipId = state.project.video_clips[0]?.id || '';
  if (!audioExists) state.selectedAudioId = state.project.audio_tracks[0]?.id || '';
  if (state.selectedClipId) state.selectedAudioId = '';
}
function normalizeProject(project) {
  const data = project && typeof project === 'object' ? project : {};
  return {
    title: String(data.title || 'Новый видеопроект'),
    canvas: data.canvas || { width: 1080, height: 1920, fps: 30 },
    video_clips: Array.isArray(data.video_clips) ? data.video_clips.map((item, index) => ({
      id: String(item.id || crypto.randomUUID()),
      source_type: String(item.source_type || 'generation'),
      source_id: String(item.source_id || ''),
      label: String(item.label || `Клип ${index + 1}`),
      source_start: Number(item.source_start || 0),
      source_end: Number(item.source_end || 0),
      volume: Number(item.volume ?? 100),
      muted: !!item.muted,
      filter: String(item.filter || 'none'),
      effect: String(item.effect || 'none'),
      transition: {
        type: String(item.transition?.type || (index ? 'fade' : 'none')),
        duration: Number(item.transition?.duration ?? (index ? 0.4 : 0)),
      },
    })) : [],
    audio_tracks: Array.isArray(data.audio_tracks) ? data.audio_tracks.map((item, index) => ({
      id: String(item.id || crypto.randomUUID()),
      source_id: String(item.source_id || ''),
      label: String(item.label || `Музыка ${index + 1}`),
      timeline_start: Number(item.timeline_start || 0),
      source_start: Number(item.source_start || 0),
      source_end: Number(item.source_end || 0),
      volume: Number(item.volume ?? 100),
    })) : [],
    text_overlays: Array.isArray(data.text_overlays) ? data.text_overlays : [],
    stickers: Array.isArray(data.stickers) ? data.stickers : [],
  };
}
function configureLinks() {
  const backLink = $('backLink');
  if (backLink) backLink.href = state.returnUrl;
}
function setButtonsState() {
  const hasClip = !!currentClip();
  const hasSelection = !!(currentClip() || currentAudio());
  $('splitBtn')?.toggleAttribute('disabled', !hasClip);
  $('muteClipBtn')?.toggleAttribute('disabled', !hasSelection);
  $('deleteClipBtn')?.toggleAttribute('disabled', !hasSelection);
  $('moveClipLeftBtn')?.toggleAttribute('disabled', !hasClip);
  $('moveClipRightBtn')?.toggleAttribute('disabled', !hasClip);
}
function renderSessionState() {
  const status = $('sessionState');
  const hint = $('libraryHint');
  const banner = $('authBanner');
  const isAuthed = !!state.token;
  if (status) {
    status.textContent = isAuthed ? 'сессия активна' : 'нет авторизации';
    status.className = isAuthed ? 'badge success' : 'badge muted';
  }
  if (hint) {
    hint.textContent = isAuthed
      ? 'Загружай свои видео и музыку, добавляй ролики из генераций и собирай монтаж.'
      : 'Без авторизации редактор покажет layout, но не сможет загрузить медиатеку и сохранить проект.';
  }
  if (banner) banner.classList.toggle('hidden', isAuthed);
}
function sourceUrlForClip(clip) {
  if (!clip) return '';
  const source = state.videos.find((item) => item.id === clip.source_id) || state.audio.find((item) => item.id === clip.source_id);
  return source?.video_url || source?.download_url || source?.output_url || '';
}
function previewSource() {
  const clip = currentClip() || state.project.video_clips[0] || null;
  return sourceUrlForClip(clip) || state.renderOutputUrl || '';
}
function previewMetaText() {
  const clip = currentClip();
  const audio = currentAudio();
  if (clip) {
    return `${clip.label || 'Клип'} · ${fmtSec(Math.max(0, Number(clip.source_end || 0) - Number(clip.source_start || 0)))} · ${clip.muted ? 'без звука' : `${Number(clip.volume || 100)}%`}`;
  }
  if (audio) {
    return `${audio.label || 'Музыка'} · старт ${fmtSec(audio.timeline_start || 0)} · ${Number(audio.volume || 100)}%`;
  }
  if (state.renderOutputUrl) return 'Последний экспорт проекта';
  return 'Выбери клип на таймлайне или добавь ролик из медиатеки.';
}
function renderPreview() {
  const src = previewSource();
  const video = $('previewVideo');
  const empty = $('previewEmpty');
  const meta = $('previewMeta');
  const titleInput = $('projectTitleInput');
  const summary = $('projectSummary');
  if (meta) meta.textContent = previewMetaText();
  if (titleInput && titleInput.value !== state.project.title) titleInput.value = state.project.title;
  if (summary) summary.textContent = `${state.project.video_clips.length} клипов · ${fmtSec(projectDuration())}`;
  $('projectStatus').textContent = state.projectId ? 'сохранён' : 'черновик';
  if (src) {
    if (video.getAttribute('src') !== src) video.src = src;
    video.classList.remove('hidden');
    empty.classList.add('hidden');
  } else {
    video.pause?.();
    video.removeAttribute('src');
    video.load?.();
    video.classList.add('hidden');
    empty.classList.remove('hidden');
  }
}
function filterLibraryItems(items) {
  const q = String(state.librarySearch || '').trim().toLowerCase();
  if (!q) return items;
  return items.filter((item) => {
    const text = `${item.filename || ''} ${item.prompt || ''} ${item.id || ''}`.toLowerCase();
    return text.includes(q);
  });
}
function itemMeta(item) {
  const parts = [fmtSec(item.duration_sec || 0)];
  if (item.source_type === 'generation') parts.push(item.provider || item.model || 'generation');
  if (item.source_type === 'upload') parts.push(item.file_type || 'upload');
  if (item.created_at) parts.push(fmtDate(item.created_at));
  return parts.filter(Boolean).join(' · ');
}
function renderLibrary() {
  const allItems = state.libraryTab === 'videos' ? state.videos : state.audio;
  const items = filterLibraryItems(allItems);
  $('videoLibraryCount').textContent = String(state.videos.length);
  $('audioLibraryCount').textContent = String(state.audio.length);
  document.querySelectorAll('[data-library-tab]').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.libraryTab === state.libraryTab);
  });
  const html = items.length ? items.map((item) => `
    <article class="library-item">
      <div class="library-item-title">${escapeHtml(item.filename || item.prompt || item.id)}</div>
      <div class="library-item-meta">${escapeHtml(itemMeta(item))}</div>
      <div class="library-item-actions">
        <button class="btn secondary" type="button" data-library-add="${escapeHtml(item.id)}">${state.libraryTab === 'videos' ? 'Добавить' : 'Использовать'}</button>
        ${state.libraryTab === 'videos' ? `<button class="btn ghost" type="button" data-library-preview="${escapeHtml(item.id)}">Превью</button>` : `<button class="btn ghost" type="button" data-library-audition="${escapeHtml(item.id)}">Выбрать</button>`}
      </div>
    </article>
  `).join('') : `<div class="library-empty">${state.token ? 'Пока пусто. Загрузите файл или вернитесь к своим генерациям.' : 'Медиатека станет доступна после открытия редактора из авторизованного Workspace.'}</div>`;
  $('libraryList').innerHTML = html;
}
function renderRuler() {
  const width = timelineWidthPx();
  const totalSec = Math.max(5, Math.ceil(projectDuration() || 0));
  const ticks = Array.from({ length: totalSec + 1 }, (_, i) => `<div class="tick" style="left:${i * state.pxPerSec}px">${i}с</div>`).join('');
  const ruler = $('timelineRuler');
  ruler.style.width = `${width}px`;
  ruler.innerHTML = ticks;
}
function clipDuration(clip) {
  return Math.max(0, Number(clip.source_end || 0) - Number(clip.source_start || 0));
}
function renderTimeline() {
  renderRuler();
  const canvasWidth = timelineWidthPx();
  $('timelineCanvas').style.width = `${canvasWidth}px`;
  const videoTrack = $('videoTrack');
  const audioTrack = $('audioTrack');
  videoTrack.style.width = `${canvasWidth}px`;
  audioTrack.style.width = `${canvasWidth}px`;

  if (!state.project.video_clips.length) {
    videoTrack.innerHTML = '<div class="track-empty">Добавь клипы из медиатеки. Теперь они занимают место на timeline пропорционально длительности.</div>';
  } else {
    let cursor = 0;
    videoTrack.innerHTML = state.project.video_clips.map((clip, index) => {
      const duration = clipDuration(clip);
      const width = Math.max(180, duration * state.pxPerSec);
      const left = cursor * state.pxPerSec;
      cursor += duration;
      const active = clip.id === state.selectedClipId ? 'active' : '';
      return `
        <div class="timeline-item video ${active}" data-clip-id="${escapeHtml(clip.id)}" style="left:${left}px; width:${width}px;">
          <div class="timeline-item-head">
            <div class="timeline-item-title">${escapeHtml(clip.label || `Клип ${index + 1}`)}</div>
            <span class="badge">${index + 1}</span>
          </div>
          <div class="timeline-item-meta">${escapeHtml(fmtSec(duration))} · ${escapeHtml(clip.transition?.type || 'none')} · ${clip.muted ? 'mute' : `${Number(clip.volume || 100)}%`}</div>
        </div>`;
    }).join('');
  }

  if (!state.project.audio_tracks.length) {
    audioTrack.innerHTML = '<div class="track-empty">Добавь музыку — она появится отдельной дорожкой и ляжет по времени старта.</div>';
  } else {
    audioTrack.innerHTML = state.project.audio_tracks.map((track, index) => {
      const duration = Math.max(0, Number(track.source_end || 0) - Number(track.source_start || 0));
      const width = Math.max(180, duration * state.pxPerSec);
      const left = Math.max(0, Number(track.timeline_start || 0) * state.pxPerSec);
      const active = track.id === state.selectedAudioId ? 'active' : '';
      return `
        <div class="timeline-item audio ${active}" data-audio-id="${escapeHtml(track.id)}" style="left:${left}px; width:${width}px;">
          <div class="timeline-item-head">
            <div class="timeline-item-title">${escapeHtml(track.label || `Музыка ${index + 1}`)}</div>
            <span class="badge">${Number(track.volume || 100)}%</span>
          </div>
          <div class="timeline-item-meta">старт ${escapeHtml(fmtSec(track.timeline_start || 0))} · ${escapeHtml(fmtSec(duration))}</div>
        </div>`;
    }).join('');
  }

  const timelineMeta = $('timelineMeta');
  if (timelineMeta) {
    timelineMeta.textContent = `Длина проекта: ${fmtSec(projectDuration())}. Видео располагаются последовательно, музыка — по абсолютному времени старта.`;
  }
}
function renderSelectionPanel() {
  const body = $('selectionBody');
  const title = $('selectionTitle');
  const clip = currentClip();
  const audio = currentAudio();
  if (clip) {
    title.textContent = 'Настройки клипа';
    body.innerHTML = `
      <div class="inspector-card">
        <h3>${escapeHtml(clip.label || 'Клип')}</h3>
        <p>Источник: ${escapeHtml(clip.source_type || 'generation')} · ${escapeHtml(fmtSec(clipDuration(clip)))}</p>
      </div>
      <div class="inspector-grid">
        <label>Название<input id="clipLabelInput" value="${escapeHtml(clip.label || '')}"></label>
        <label>Громкость, %<input id="clipVolumeInput" type="number" min="0" max="100" step="1" value="${Number(clip.volume || 100)}"></label>
        <label>Старт фрагмента, сек<input id="clipStartInput" type="number" min="0" step="0.1" value="${Number(clip.source_start || 0)}"></label>
        <label>Конец фрагмента, сек<input id="clipEndInput" type="number" min="0" step="0.1" value="${Number(clip.source_end || 0)}"></label>
        <label>Фильтр
          <select id="clipFilterInput">
            <option value="none">Без фильтра</option>
            <option value="warm">Тёплый</option>
            <option value="cold">Холодный</option>
            <option value="bw">Ч/Б</option>
            <option value="cinematic">Cinematic</option>
          </select>
        </label>
        <label>Эффект
          <select id="clipEffectInput">
            <option value="none">Без эффекта</option>
            <option value="zoom_in">Zoom in</option>
            <option value="zoom_out">Zoom out</option>
            <option value="blur_intro">Blur intro</option>
          </select>
        </label>
        <label>Переход
          <select id="clipTransitionTypeInput">
            <option value="none">Без перехода</option>
            <option value="fade">Fade</option>
            <option value="dissolve">Dissolve</option>
            <option value="slideleft">Slide Left</option>
            <option value="slideright">Slide Right</option>
            <option value="zoomin">Zoom Fade</option>
          </select>
        </label>
        <label>Длит. перехода, сек<input id="transitionDurationInput" type="number" min="0" max="1" step="0.1" value="${Number(clip.transition?.duration || 0)}"></label>
      </div>
      <div class="inspector-actions">
        <button class="tool" type="button" id="inspectorMoveLeftBtn">← Переместить</button>
        <button class="tool" type="button" id="inspectorMoveRightBtn">Переместить →</button>
        <button class="tool danger" type="button" id="inspectorDeleteBtn">Удалить клип</button>
      </div>`;
    $('clipFilterInput').value = clip.filter || 'none';
    $('clipEffectInput').value = clip.effect || 'none';
    $('clipTransitionTypeInput').value = clip.transition?.type || 'none';
    return;
  }
  if (audio) {
    title.textContent = 'Настройки музыки';
    body.innerHTML = `
      <div class="inspector-card">
        <h3>${escapeHtml(audio.label || 'Музыка')}</h3>
        <p>Музыкальная дорожка проекта. Сейчас backend поддерживает 1 активный audio track.</p>
      </div>
      <div class="inspector-grid">
        <label>Название<input id="audioLabelInput" value="${escapeHtml(audio.label || '')}"></label>
        <label>Громкость, %<input id="audioVolumeInput" type="number" min="0" max="100" step="1" value="${Number(audio.volume || 100)}"></label>
        <label>Старт на timeline, сек<input id="audioTimelineInput" type="number" min="0" step="0.1" value="${Number(audio.timeline_start || 0)}"></label>
        <label>Старт фрагмента, сек<input id="audioStartInput" type="number" min="0" step="0.1" value="${Number(audio.source_start || 0)}"></label>
        <label>Конец фрагмента, сек<input id="audioEndInput" type="number" min="0" step="0.1" value="${Number(audio.source_end || 0)}"></label>
      </div>
      <div class="inspector-actions">
        <button class="tool danger" type="button" id="inspectorDeleteAudioBtn">Удалить музыку</button>
      </div>`;
    return;
  }
  title.textContent = 'Параметры проекта';
  body.innerHTML = `
    <div class="inspector-card">
      <h3>Сводка проекта</h3>
      <p>Справа теперь нет бесполезной пустоты: здесь всегда видны параметры выбранного элемента или общая сводка проекта.</p>
    </div>
    <div class="mini-stats">
      <div class="mini-stat"><span class="mini-stat-label">Клипы</span><strong>${state.project.video_clips.length}</strong></div>
      <div class="mini-stat"><span class="mini-stat-label">Музыка</span><strong>${state.project.audio_tracks.length}</strong></div>
      <div class="mini-stat"><span class="mini-stat-label">Длительность</span><strong>${fmtSec(projectDuration())}</strong></div>
      <div class="mini-stat"><span class="mini-stat-label">Canvas</span><strong>${Number(state.project.canvas?.width || 1080)}×${Number(state.project.canvas?.height || 1920)}</strong></div>
    </div>`;
}
function rerender() {
  syncSelection();
  configureLinks();
  renderSessionState();
  renderPreview();
  renderLibrary();
  renderTimeline();
  renderSelectionPanel();
  setButtonsState();
}

async function loadProjectIfNeeded() {
  if (!state.projectId || !state.token) return;
  const data = await api(`/api/video-editor-v2/projects/${encodeURIComponent(state.projectId)}`, { headers: authHeaders(false) });
  const item = data.item || {};
  state.projectId = item.id || state.projectId;
  state.project = normalizeProject(item.project_json || item.project || state.project);
  state.project.title = String(item.title || state.project.title || 'Новый видеопроект');
}
function sortItemsByDateDesc(items) {
  return [...items].sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')));
}
async function loadLibrary() {
  if (!state.token) {
    state.authReady = true;
    state.videos = [];
    state.audio = [];
    rerender();
    return;
  }
  const [generationVideos, uploadVideos, uploadAudio] = await Promise.all([
    api('/api/video-editor-v2/library/videos', { headers: authHeaders(false) }),
    api('/api/video-editor-v2/library/uploads?file_type=video', { headers: authHeaders(false) }),
    api('/api/video-editor-v2/library/uploads?file_type=audio', { headers: authHeaders(false) }),
  ]);
  state.videos = sortItemsByDateDesc([
    ...((uploadVideos.items || []).map((item) => ({ ...item, source_type: 'upload' }))),
    ...((generationVideos.items || []).map((item) => ({ ...item, source_type: 'generation' }))),
  ]);
  state.audio = sortItemsByDateDesc((uploadAudio.items || []).map((item) => ({ ...item, source_type: 'upload' })));
  state.authReady = true;
  rerender();
}
function addVideoFromLibrary(id) {
  if (state.project.video_clips.length >= 5) return toast('Максимум 5 клипов в этом редакторе');
  const item = state.videos.find((x) => x.id === id);
  if (!item) return;
  const duration = Math.max(0.1, Number(item.duration_sec || 5));
  const clip = {
    id: crypto.randomUUID(),
    source_type: item.source_type || (item.file_type === 'video' ? 'upload' : 'generation'),
    source_id: item.id,
    label: item.filename || item.prompt || `Клип ${state.project.video_clips.length + 1}`,
    source_start: 0,
    source_end: duration,
    volume: 100,
    muted: false,
    filter: 'none',
    effect: 'none',
    transition: { type: state.project.video_clips.length ? 'fade' : 'none', duration: state.project.video_clips.length ? 0.4 : 0 },
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
  toast('Музыка добавлена на дорожку');
  rerender();
}
function moveClip(direction) {
  const clip = currentClip();
  if (!clip) return;
  const idx = state.project.video_clips.findIndex((item) => item.id === clip.id);
  if (idx < 0) return;
  const nextIndex = idx + direction;
  if (nextIndex < 0 || nextIndex >= state.project.video_clips.length) return;
  const items = [...state.project.video_clips];
  [items[idx], items[nextIndex]] = [items[nextIndex], items[idx]];
  state.project.video_clips = items;
  rerender();
}
function deleteSelection() {
  if (state.selectedClipId) {
    state.project.video_clips = state.project.video_clips.filter((item) => item.id !== state.selectedClipId);
    state.selectedClipId = '';
    rerender();
    return;
  }
  if (state.selectedAudioId) {
    state.project.audio_tracks = state.project.audio_tracks.filter((item) => item.id !== state.selectedAudioId);
    state.selectedAudioId = '';
    rerender();
  }
}
function splitCurrentClip() {
  const clip = currentClip();
  if (!clip) return toast('Сначала выбери клип');
  const start = Number(clip.source_start || 0);
  const end = Number(clip.source_end || 0);
  if (end - start < 1.0) return toast('Клип слишком короткий для разрезания');
  const mid = Number(((start + end) / 2).toFixed(1));
  const index = state.project.video_clips.findIndex((item) => item.id === clip.id);
  const first = { ...clip, id: crypto.randomUUID(), source_end: mid, label: `${clip.label} A` };
  const second = { ...clip, id: crypto.randomUUID(), source_start: mid, label: `${clip.label} B`, transition: { ...clip.transition } };
  state.project.video_clips.splice(index, 1, first, second);
  state.selectedClipId = first.id;
  rerender();
}
function toggleMuteSelection() {
  const clip = currentClip();
  if (clip) {
    clip.muted = !clip.muted;
    clip.volume = clip.muted ? 0 : (clip.volume > 0 ? clip.volume : 100);
    rerender();
    return;
  }
  const audio = currentAudio();
  if (audio) {
    audio.volume = Number(audio.volume || 0) > 0 ? 0 : 100;
    rerender();
  }
}
function applySelectionChanges() {
  const clip = currentClip();
  const audio = currentAudio();
  if (clip) {
    clip.label = $('clipLabelInput')?.value || clip.label;
    clip.source_start = Math.max(0, Number($('clipStartInput')?.value || 0));
    clip.source_end = Math.max(clip.source_start + 0.1, Number($('clipEndInput')?.value || 0));
    clip.volume = Math.max(0, Math.min(100, Number($('clipVolumeInput')?.value || 100)));
    clip.muted = clip.volume <= 0 ? true : clip.muted;
    clip.filter = $('clipFilterInput')?.value || 'none';
    clip.effect = $('clipEffectInput')?.value || 'none';
    clip.transition = clip.transition || { type: 'none', duration: 0 };
    clip.transition.type = $('clipTransitionTypeInput')?.value || 'none';
    clip.transition.duration = Math.max(0, Math.min(1, Number($('transitionDurationInput')?.value || 0)));
  }
  if (audio) {
    audio.label = $('audioLabelInput')?.value || audio.label;
    audio.timeline_start = Math.max(0, Number($('audioTimelineInput')?.value || 0));
    audio.source_start = Math.max(0, Number($('audioStartInput')?.value || 0));
    audio.source_end = Math.max(audio.source_start + 0.1, Number($('audioEndInput')?.value || 0));
    audio.volume = Math.max(0, Math.min(100, Number($('audioVolumeInput')?.value || 100)));
  }
  rerender();
}
async function saveProject() {
  state.project.title = $('projectTitleInput')?.value.trim() || 'Новый видеопроект';
  const payload = { title: state.project.title, project_json: state.project };
  if (!state.token) return toast('Открой редактор из Workspace, чтобы сохранить проект');
  let data;
  if (state.projectId) {
    data = await api(`/api/video-editor-v2/projects/${encodeURIComponent(state.projectId)}`, {
      method: 'PUT', headers: authHeaders(), body: JSON.stringify(payload),
    });
  } else {
    data = await api('/api/video-editor-v2/projects', {
      method: 'POST', headers: authHeaders(), body: JSON.stringify(payload),
    });
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
  const data = await api('/api/video-editor-v2/render', {
    method: 'POST', headers: authHeaders(), body: JSON.stringify({ project_id: state.projectId }),
  });
  state.renderJobId = data.item.id;
  state.renderOutputUrl = '';
  $('renderStatus').textContent = 'рендер...';
  toast('Рендер запущен');
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
      state.renderOutputUrl = item.output_url || '';
      rerender();
      toast('Рендер завершён');
      return;
    }
    if (item.status === 'failed') {
      toast(item.error_text || 'Ошибка рендера', 5000);
      return;
    }
  } catch (error) {
    toast(error.message || String(error), 5000);
    return;
  }
  state.polling = setTimeout(pollRender, 2500);
}
async function uploadMedia(kind, file) {
  if (!file) return;
  if (!state.token) return toast('Загрузка доступна только из авторизованного Workspace');
  const form = new FormData();
  form.append('file', file);
  const data = await api(`/api/video-editor-v2/upload/${kind}`, { method: 'POST', headers: authHeaders(false), body: form });
  const uploaded = data.item || null;
  toast(kind === 'video' ? 'Видео загружено' : 'Музыка загружена');
  await loadLibrary();
  if (uploaded?.id) {
    if (kind === 'video') state.libraryTab = 'videos';
    if (kind === 'audio') state.libraryTab = 'audio';
  }
  rerender();
}
function selectLibraryPreview(id) {
  const item = state.videos.find((entry) => entry.id === id);
  if (!item) return;
  state.selectedClipId = '';
  state.selectedAudioId = '';
  state.renderOutputUrl = item.video_url || item.download_url || item.output_url || '';
  rerender();
}


document.addEventListener('click', async (event) => {
  const addBtn = event.target.closest('[data-library-add]');
  const addId = addBtn?.dataset?.libraryAdd;
  if (addId) {
    if (state.libraryTab === 'videos') addVideoFromLibrary(addId); else addAudioFromLibrary(addId);
    return;
  }
  const previewBtn = event.target.closest('[data-library-preview]');
  const previewId = previewBtn?.dataset?.libraryPreview;
  if (previewId) { selectLibraryPreview(previewId); return; }
  const auditionBtn = event.target.closest('[data-library-audition]');
  const auditionId = auditionBtn?.dataset?.libraryAudition;
  if (auditionId) { addAudioFromLibrary(auditionId); return; }

  const clipCard = event.target.closest('[data-clip-id]');
  if (clipCard) {
    state.selectedClipId = clipCard.dataset.clipId;
    state.selectedAudioId = '';
    state.renderOutputUrl = '';
    rerender();
    return;
  }
  const audioCard = event.target.closest('[data-audio-id]');
  if (audioCard) {
    state.selectedAudioId = audioCard.dataset.audioId;
    state.selectedClipId = '';
    rerender();
    return;
  }

  if (event.target.id === 'refreshLibraryBtn') {
    try { await loadLibrary(); toast('Библиотека обновлена'); } catch (error) { toast(error.message || String(error), 5000); }
    return;
  }
  if (event.target.id === 'saveProjectBtn') { saveProject().catch((error) => toast(error.message || String(error), 5000)); return; }
  if (event.target.id === 'renderBtn') { startRender().catch((error) => toast(error.message || String(error), 5000)); return; }
  if (event.target.id === 'moveClipLeftBtn' || event.target.id === 'inspectorMoveLeftBtn') { moveClip(-1); return; }
  if (event.target.id === 'moveClipRightBtn' || event.target.id === 'inspectorMoveRightBtn') { moveClip(1); return; }
  if (event.target.id === 'deleteClipBtn' || event.target.id === 'inspectorDeleteBtn' || event.target.id === 'inspectorDeleteAudioBtn') { deleteSelection(); return; }
  if (event.target.id === 'muteClipBtn') { toggleMuteSelection(); return; }
  if (event.target.id === 'splitBtn') { splitCurrentClip(); return; }
});

document.addEventListener('input', (event) => {
  if (event.target.id === 'librarySearchInput') {
    state.librarySearch = event.target.value || '';
    renderLibrary();
    return;
  }
  if (event.target.id === 'projectTitleInput') {
    state.project.title = event.target.value || 'Новый видеопроект';
    renderPreview();
    return;
  }
  if (event.target.matches('#clipLabelInput,#clipStartInput,#clipEndInput,#clipVolumeInput,#clipFilterInput,#clipEffectInput,#clipTransitionTypeInput,#transitionDurationInput,#audioLabelInput,#audioTimelineInput,#audioStartInput,#audioEndInput,#audioVolumeInput')) {
    applySelectionChanges();
  }
});

document.addEventListener('change', (event) => {
  if (event.target.id === 'videoUploadInput') {
    uploadMedia('video', event.target.files?.[0]).catch((error) => toast(error.message || String(error), 5000)).finally(() => { event.target.value = ''; });
    return;
  }
  if (event.target.id === 'audioUploadInput') {
    uploadMedia('audio', event.target.files?.[0]).catch((error) => toast(error.message || String(error), 5000)).finally(() => { event.target.value = ''; });
    return;
  }
});

document.addEventListener('click', (event) => {
  const tabBtn = event.target.closest('[data-library-tab]');
  const tab = tabBtn?.dataset?.libraryTab;
  if (!tab) return;
  state.libraryTab = tab;
  renderLibrary();
});

window.addEventListener('message', async (event) => {
  try {
    const data = event.data || {};
    if (data?.type !== 'astrabot-workspace-auth') return;
    if (typeof data.token === 'string' && data.token.trim()) {
      state.token = data.token.trim();
      saveTokenSilently();
      await loadLibrary();
    }
  } catch (error) {
    console.error(error);
  }
});

window.addEventListener('load', async () => {
  saveTokenSilently();
  configureLinks();
  rerender();
  if (state.token) {
    try {
      await Promise.all([loadLibrary(), loadProjectIfNeeded()]);
    } catch (error) {
      toast(error.message || String(error), 5000);
    }
  }
  rerender();
});
