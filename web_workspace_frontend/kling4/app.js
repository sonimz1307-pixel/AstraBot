/* One authenticated form for the website and the inline Telegram WebApp. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const tg = window.Telegram?.WebApp;
  const initData = String(tg?.initData || '');
  const embedded = new URLSearchParams(location.search).get('embedded') === '1';
  const state = { mode: 'reference', groups: {}, elements: [], voices: [], quote: null, pending: '', uploading: 0, busy: false, account: null, enabled: false };
  const labels = { queued: 'В очереди', submitting: 'Запускаем', processing: 'Генерация', needs_review: 'Проверяем запуск', completed: 'Готово', failed: 'Токены возвращены' };
  const escape = text => String(text ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const getStored = key => { try { return localStorage.getItem(key) || ''; } catch { return ''; } };
  const setStored = (key, value) => { try { value ? localStorage.setItem(key, value) : localStorage.removeItem(key); } catch {} };
  const pendingKey = () => `nabex:kling4:pending:${state.account?.id || ''}`;
  function notice(text) { $('notice').textContent = text; $('notice').hidden = !text; resize(); }
  function resize() {
    if (!embedded || parent === window) return;
    let parentOrigin = location.origin;
    try { if (document.referrer) parentOrigin = new URL(document.referrer).origin; } catch {}
    // Measure content rather than viewport height; otherwise parent resizing loops.
    parent.postMessage({ type: 'nabex-kling4-height', height: Math.ceil(document.querySelector('main').getBoundingClientRect().height) }, parentOrigin);
  }
  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (initData) headers.set('X-Telegram-Init-Data', initData);
    if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json');
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 65000);
    try {
      const response = await fetch('/api/kling4' + path, { ...options, headers, credentials: 'include', signal: controller.signal });
      // Include body download in the timeout. An unreadable 200 response does
      // not confirm a purchase and must keep its original quote ID for retry.
      const data = await response.json().catch(() => { throw new Error('Не удалось прочитать ответ. Повторите проверку запуска.'); });
      if (!response.ok) {
        const detail = typeof data.detail === 'string' ? data.detail : data.detail?.message || 'Не удалось выполнить запрос. Проверьте параметры и попробуйте ещё раз.';
        const error = new Error(detail); error.status = response.status; throw error;
      }
      return data;
    } finally { clearTimeout(timeout); }
  }
  function controls() {
    $('controls').disabled = !state.enabled || !state.account || state.busy || !!state.pending;
    $('quoteBtn').disabled = !!state.uploading;
    $('runBtn').disabled = state.busy || !!state.uploading;
  }
  function invalidate() { if (state.pending) return; state.quote = null; $('quote').hidden = true; resize(); }
  function group(name) { return state.groups[name] ||= []; }
  function showFiles(name, container, tag) {
    $(container).innerHTML = group(name).map((f, i) => `<div class="file"><span><b>${escape(typeof tag === 'function' ? tag(i) : tag)}</b> ${escape(f.name)}${f.duration_seconds ? ` · ${Number(f.duration_seconds).toFixed(2)} сек` : ''}</span><button type="button" class="remove" data-group="${escape(name)}" data-index="${i}" aria-label="Убрать файл">×</button></div>`).join('');
    resize();
  }
  const groups = {
    images: ['images', 'image', 10, 'imageList', i => `@image_${i+1}`],
    videos: ['videos', 'video', 5, 'videoList', i => `@video_${i+1}`],
    first: ['first', 'image', 1, 'firstList', '@first_frame'],
    last: ['last', 'image', 1, 'lastList', '@last_frame'],
    keyframes: ['keyframes', 'image', 8, 'keyframeList', i => `@keyframe_${i+1}`]
  };
  async function uploadFiles(files, name, kind, max, onUpdate) {
    if (state.pending || state.busy) return;
    if (group(name).length + files.length > max) { notice(`В этом поле максимум ${max} файлов. Уберите лишние перед загрузкой.`); return; }
    if ([...files].some(f => f.size > 10 * 1024 * 1024)) { notice('Каждый файл должен быть не больше 10 МБ.'); return; }
    invalidate(); state.uploading++; controls(); notice('Загружаю и проверяю файлы…');
    try {
      // Sequential uploads keep peak memory bounded on mobile and in the API.
      for (const file of files) {
        const form = new FormData(); form.append('kind', kind); form.append('file', file);
        const asset = await api('/upload', { method: 'POST', body: form });
        group(name).push({ ...asset, name: file.name }); onUpdate();
      }
      notice('');
    } catch (error) { notice(error.message); }
    finally { state.uploading--; controls(); onUpdate(); }
  }
  for (const [inputId, config] of Object.entries(groups)) {
    $(inputId).addEventListener('change', async event => {
      const [name, kind, max, container, tag] = config;
      await uploadFiles([...event.target.files], name, kind, max, () => showFiles(name, container, tag)); event.target.value = '';
    });
  }
  function renderNamed(kind) {
    const rows = kind === 'element' ? state.elements : state.voices;
    const target = kind === 'element' ? 'elements' : 'voices';
    $(target).innerHTML = rows.map(row => `<div class="named-ref"><div class="ref-head"><input type="text" data-ref-name="${row.key}" value="${escape(row.name)}" maxlength="20" placeholder="${kind === 'element' ? 'Hero' : 'Narrator'}" aria-label="Имя референса"><button type="button" class="remove" data-remove-ref="${row.key}" aria-label="Убрать референс">×</button></div><input type="file" data-ref-upload="${row.key}" accept="${kind === 'element' ? 'image/jpeg,image/png,image/webp' : 'audio/mpeg,audio/wav'}" ${kind === 'element' ? 'multiple' : ''}><div id="list-${row.key}" class="file-list"></div></div>`).join('');
    for (const row of rows) showFiles(row.key, 'list-' + row.key, () => '@' + (row.name || '?'));
    resize();
  }
  document.addEventListener('click', event => {
    const mode = event.target.closest('[data-mode]');
    if (mode && !state.pending) {
      state.mode = mode.dataset.mode; invalidate();
      document.querySelectorAll('[data-mode]').forEach(b => b.classList.toggle('active', b === mode));
      $('referenceFields').hidden = state.mode !== 'reference'; $('keyframeFields').hidden = state.mode !== 'keyframe'; resize();
    }
    const remove = event.target.closest('[data-group]');
    if (remove && !state.pending) {
      group(remove.dataset.group).splice(Number(remove.dataset.index), 1); invalidate();
      const config = groups[remove.dataset.group];
      if (config) showFiles(config[0], config[3], config[4]); else { renderNamed('element'); renderNamed('voice'); }
    }
    const ref = event.target.closest('[data-remove-ref]');
    if (ref && !state.pending) { state.elements = state.elements.filter(r => r.key !== ref.dataset.removeRef); state.voices = state.voices.filter(r => r.key !== ref.dataset.removeRef); invalidate(); renderNamed('element'); renderNamed('voice'); }
  });
  document.addEventListener('change', async event => {
    const input = event.target;
    if (input.dataset.refUpload) {
      const row = [...state.elements, ...state.voices].find(r => r.key === input.dataset.refUpload);
      if (!row) return;
      await uploadFiles([...input.files], row.key, row.kind === 'element' ? 'image' : 'audio', row.kind === 'element' ? 4 : 1, () => renderNamed(row.kind));
    }
  });
  document.addEventListener('input', event => {
    if (event.target.dataset.refName) {
      const row = [...state.elements, ...state.voices].find(r => r.key === event.target.dataset.refName);
      if (row) row.name = event.target.value;
    }
    if (event.target.closest('#form')) invalidate();
    $('count').textContent = `${$('prompt').value.length} / 8000`;
  });
  $('form').addEventListener('change', invalidate);
  for (const kind of ['element', 'voice']) {
    $(kind === 'element' ? 'addElement' : 'addVoice').addEventListener('click', () => {
      const rows = kind === 'element' ? state.elements : state.voices;
      if (rows.length >= (kind === 'element' ? 7 : 3)) { notice('Достигнут лимит референсов.'); return; }
      rows.push({ key: `ref-${Date.now()}-${Math.random().toString(16).slice(2,8)}`, kind, name: '' }); invalidate(); renderNamed(kind);
    });
  }
  function payload() {
    const ids = name => group(name).map(f => f.id);
    const data = { mode: state.mode, prompt: $('prompt').value.trim(), duration: Number($('duration').value), resolution: $('resolution').value, aspect_ratio: $('aspect').value };
    if (state.mode === 'reference') Object.assign(data, {
      image_ids: ids('images'), video_ids: ids('videos'), keep_original_sound: $('keepSound').checked,
      elements: state.elements.map(r => ({ name: r.name.trim(), image_ids: ids(r.key) })),
      voices: state.voices.map(r => ({ name: r.name.trim(), audio_id: ids(r.key)[0] || '' }))
    });
    else Object.assign(data, { first_frame_id: ids('first')[0] || null, last_frame_id: ids('last')[0] || null, keyframe_ids: ids('keyframes') });
    return data;
  }
  $('form').addEventListener('submit', async event => {
    event.preventDefault(); if (state.uploading || state.pending) return;
    state.busy = true; controls(); notice('Рассчитываю стоимость…');
    try {
      state.quote = await api('/quote', { method: 'POST', body: JSON.stringify(payload()) });
      const q = state.quote;
      $('price').textContent = `${q.tokens} токенов`;
      $('priceDetail').textContent = q.input_video_seconds
        ? `Исходные видео: ${q.input_video_seconds.toFixed(2)} сек. Результат: ${q.output_seconds} сек. Оплачивается ${q.billable_seconds} сек × ${q.rate} ток., с округлением общей суммы вверх.`
        : `${q.output_seconds} сек × ${q.rate} ток. за секунду. Общая сумма округляется вверх. Стоимость одинакова для сайта и бота.`;
      $('runBtn').textContent = `Запустить за ${q.tokens} ток.`; $('quote').hidden = false; notice('');
    } catch (error) { invalidate(); notice(error.message); }
    finally { state.busy = false; controls(); resize(); }
  });
  function clearPending() { state.pending = ''; setStored(pendingKey(), ''); }
  function confirmedNotice(job) {
    if (job.status === 'failed') return `Задача завершилась с ошибкой. Возвращено ${job.tokens} токенов; подробности в истории.`;
    if (job.status === 'completed') return 'Видео готово и доступно в истории.';
    return `Запуск подтверждён. Списано ${job.tokens} токенов. Можно закрыть эту страницу — генерация продолжится.`;
  }
  async function run() {
    const id = state.pending || state.quote?.id; if (!id || state.busy) return;
    state.pending = id; setStored(pendingKey(), id); state.busy = true; controls(); notice('Подтверждаю запуск…');
    try {
      const job = await api('/run', { method: 'POST', body: JSON.stringify({ quote_id: id }) });
      if (!job || job.id !== id || !Object.hasOwn(labels, job.status)) throw new Error('Сервер не подтвердил исходный запуск. Повторите проверку.');
      clearPending(); invalidate(); notice(confirmedNotice(job));
      await refresh().catch(() => notice('Запуск подтверждён. Обновите историю, когда связь восстановится.'));
    } catch (error) {
      if (state.confirmedJob?.id === id) {
        // Polling can observe the committed job before this POST times out.
        // Preserve that confirmation instead of displaying an unusable retry.
        clearPending(); invalidate(); notice(confirmedNotice(state.confirmedJob));
        return;
      }
      if ([400, 402, 404, 409, 429].includes(error.status)) { clearPending(); invalidate(); }
      else { $('quote').hidden = false; $('price').textContent = 'Проверка запуска'; $('priceDetail').textContent = 'Сохраняем исходный запрос. Кнопка ниже проверит его без повторного списания.'; $('runBtn').textContent = 'Проверить запуск'; }
      notice(error.message || 'Нет связи. Нажмите «Проверить запуск», когда интернет восстановится.');
    } finally { state.busy = false; controls(); resize(); }
  }
  $('runBtn').addEventListener('click', run);
  function renderJobs(items) {
    $('jobs').innerHTML = items.length ? items.map(job => {
      const url = String(job.result_url || '');
      const safeUrl = url.startsWith('https://') ? escape(url) : '';
      return `<article class="job"><div class="job-header"><b>${escape(job.resolution)} · ${escape(job.duration)} сек · ${escape(job.tokens)} ток.</b><span class="job-status">${escape(labels[job.status] || job.status)}</span></div>${job.error_message ? `<p>${escape(job.error_message)}</p>` : ''}${job.status === 'completed' && safeUrl ? `<video controls playsinline preload="metadata" src="${safeUrl}"></video><a href="${safeUrl}" target="_blank" rel="noopener noreferrer" download>Открыть и сохранить видео</a>` : ''}<p>${escape(new Date(job.created_at).toLocaleString('ru-RU'))}</p></article>`;
    }).join('') : '<p class="hint">Здесь появятся ваши видео.</p>';
    resize();
  }
  let refreshing = false;
  async function refresh() {
    if (refreshing || !state.account) return;
    refreshing = true;
    try {
      const [me, history] = await Promise.all([api('/me'), api('/jobs')]);
      $('balance').textContent = `${me.balance} токенов`;
      // Keep video playback intact while only polling an unchanged history.
      const snapshot = JSON.stringify(history.items);
      if (snapshot !== state.lastHistory) { state.lastHistory = snapshot; renderJobs(history.items); }
      const confirmed = state.pending && history.items.find(job => job.id === state.pending);
      if (confirmed) { state.confirmedJob = confirmed; clearPending(); invalidate(); controls(); notice(confirmedNotice(confirmed)); }
    } finally { refreshing = false; }
  }
  $('refresh').addEventListener('click', () => refresh().catch(e => notice(e.message)));
  async function boot() {
    if (embedded) document.body.classList.add('embedded');
    if (initData) { tg.ready(); tg.expand(); $('back').textContent = '← Закрыть'; $('back').addEventListener('click', e => { e.preventDefault(); tg.close(); }); }
    controls();
    try {
      const config = await api('/config'); state.enabled = config.enabled;
      if (!config.enabled) { notice('Kling 4.0 пока не включён.'); $('jobs').textContent = ''; return; }
      state.account = await api('/me'); state.pending = getStored(pendingKey());
      if (state.pending) { $('quote').hidden = false; $('price').textContent = 'Незавершённый запрос'; $('priceDetail').textContent = 'Проверьте предыдущий запуск перед новой генерацией.'; $('runBtn').textContent = 'Проверить запуск'; }
      await refresh();
      setInterval(() => { if (!document.hidden) refresh().catch(() => {}); }, 10000);
    } catch (error) { notice(error.message); $('jobs').textContent = ''; }
    finally { controls(); resize(); }
  }
  if (typeof ResizeObserver !== 'undefined') new ResizeObserver(resize).observe(document.body);
  boot();
})();
