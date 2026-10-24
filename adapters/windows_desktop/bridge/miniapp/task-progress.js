/* Isolated, read-only projection of one selected Codex task. No global refresh or fetch. */
(() => {
  'use strict';
  const POLL_MS = 5000, STALE_MS = 180000;
  const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const text = value => typeof value === 'string' ? value : '';
  const node = (tag, cls, value) => {
    const result = document.createElement(tag);
    if (cls) result.className = cls;
    if (value !== undefined) result.textContent = String(value);
    return result;
  };
  const instant = value => typeof value === 'string' && value.trim() && Number.isFinite(Date.parse(value)) ? Date.parse(value) : null;
  function timestamp(value) {
    const at = instant(value);
    return at === null ? 'не подтверждено' : new Intl.DateTimeFormat('ru-RU', {
      day:'2-digit', month:'2-digit', year:'numeric', hour:'2-digit', minute:'2-digit', second:'2-digit', timeZone:'Europe/Moscow'
    }).format(at) + ' МСК';
  }
  function duration(data) {
    const start = instant(data.started_at_msk), end = instant(data.finished_at_msk) ?? instant(data.updated_at_msk);
    if (start === null || end === null || end < start) return 'не подтверждено';
    const minutes = Math.floor((end - start) / 60000), hours = Math.floor(minutes / 60);
    return hours ? hours + ' ч ' + minutes % 60 + ' мин' : minutes + ' мин';
  }
  const localStatus = data => data?.source === 'workspace_protocol' && data?.progress_basis === 'workspace_protocol_unconfirmed_turn';
  function sourceStale(data) {
    const at = instant(data.updated_at_msk);
    const final = data.final === true && instant(data.finished_at_msk) !== null;
    return (data.stale === true && !localStatus(data)) || at === null || at - Date.now() > 60000 || (!final && Date.now() - at > STALE_MS);
  }
  function unavailable(message) {
    const error = new Error(message);error.progressUnavailable = true;return error;
  }
  const number = value => value.toLocaleString('ru-RU', {maximumFractionDigits:2});
  function checkpointProgress(item) {
    const value = typeof item?.percent === 'number' && Number.isFinite(item.percent) && item.percent >= 0 && item.percent <= 100
      ? item.percent : item?.done === true ? 100 : 0;
    const unit = text(item?.unit).trim();
    const quantity = typeof item?.count_done === 'number' && Number.isFinite(item.count_done) && item.count_done >= 0 &&
      typeof item?.count_total === 'number' && Number.isFinite(item.count_total) && item.count_total > 0 && item.count_done <= item.count_total && unit;
    return (quantity ? number(item.count_done) + ' из ' + number(item.count_total) + ' ' + unit + ' · ' : '') + number(value) + '%';
  }
  function describe(data) {
    const state = text(data?.runtime_status).toLowerCase().replace(/[- ]/g, '_');
    const running = ['running','active','in_progress','inprogress','processing'].includes(state);
    const labels = {running:'В работе',active:'В работе',in_progress:'В работе',inprogress:'В работе',processing:'В работе',
      idle:'Не выполняется',completed:'Завершено',complete:'Завершено',done:'Завершено',failed:'Ошибка',error:'Ошибка',
      waiting:'Ожидает',waiting_for_input:'Ожидает ответа',needs_input:'Ожидает ответа',needs_attention:'Требуется внимание',
      paused:'Приостановлено',interrupted:'Прервано',not_loaded:'Неизвестно',unknown:'Неизвестно'};
    const previous = data?.progress_basis === 'previous_turn' || (data?.turn_confirmed === false && running);
    const valid = typeof data?.percent === 'number' && Number.isFinite(data.percent) && data.percent >= 0 && data.percent <= 100 && !(previous && running);
    const counts = Number.isSafeInteger(data?.done) && Number.isSafeInteger(data?.total) && data.total > 0 && data.done >= 0 && data.total >= data.done && !(previous && running);
    return {runtime:labels[state] || 'Неизвестно',running,previous,percent:valid ? data.percent : null,
      counts:counts ? data.done + ' из ' + data.total : '',
      hasRuntime:Object.prototype.hasOwnProperty.call(data || {},'runtime_status'),
      hasPlan:(Array.isArray(data?.checkpoints) && data.checkpoints.length > 0) || valid};
  }
  function mount({host, threadId = '', request, onOpen} = {}) {
    if (!(host instanceof Element) || typeof request !== 'function') throw new TypeError('host and authenticated request are required');
    const root = node('section', 'codex-task-progress');
    root.setAttribute('aria-label', 'Прогресс выбранной задачи');
    root.hidden = true;
    const heading = node('div', 'codex-task-progress__heading');
    const title = node('h2', 'codex-task-progress__title', 'Прогресс задачи');
    const percent = node('strong', 'codex-task-progress__percent', '—');
    const compact = node('button','codex-task-progress__compact');compact.type='button';compact.setAttribute('aria-label','Показать прогресс этой задачи');compact.setAttribute('aria-expanded','false');
    const compactPercent=node('strong','','—'),compactState=node('span','codex-task-progress__compact-state','Данные ещё не получены');
    compact.append(node('span','','Прогресс'),compactPercent,compactState,node('span','codex-task-progress__compact-arrow','›'));compact.addEventListener('click',()=>onOpen?.());
    heading.append(title, percent);
    const bar = node('div', 'codex-task-progress__bar');
    bar.hidden = true;
    bar.setAttribute('role', 'progressbar');bar.setAttribute('aria-label', 'Готовность текущего хода');
    bar.setAttribute('aria-valuemin', '0');bar.setAttribute('aria-valuemax', '100');
    const fill = node('span');bar.append(fill);
    const connection = node('p', 'codex-task-progress__connection', 'Данные ещё не получены');
    connection.setAttribute('role', 'status');
    const summary = node('p', 'codex-task-progress__summary');
    const runtime = node('p', 'codex-task-progress__runtime');
    const basis = node('p', 'codex-task-progress__basis');
    const current = node('p', 'codex-task-progress__current');
    const forecast = node('p', 'codex-task-progress__forecast');
    const checkpoints = node('ol', 'codex-task-progress__checkpoints');
    const results = node('div', 'codex-task-progress__results');
    const times = node('div', 'codex-task-progress__times');
    root.append(compact, heading, bar, connection, runtime, basis, summary, current, forecast, checkpoints, results, times);
    host.append(root);
    let selected = text(threadId), active = false, destroyed = false, generation = 0;
    let timer = null, flight = null, queued = false, confirmed = null;
    const canRead = () => !destroyed && active && !document.hidden && UUID.test(selected);
    const clearTimer = () => { if (timer !== null) clearTimeout(timer);timer = null; };
    function showConnection(message, stale) {
      connection.textContent = message;
      root.classList.toggle('codex-task-progress--stale', Boolean(stale));
      compactState.textContent = stale ? (confirmed ? 'Снимок устарел' : 'Нет подтверждённых данных') : confirmed ? (localStatus(confirmed) ? 'Локальный статус задачи' : describe(confirmed).previous ? 'Предыдущая фаза' : text(confirmed.status)||'Подтверждено') : 'Загрузка…';
      compact.title=message;
    }
    function reset() {
      confirmed = null;title.textContent = 'Прогресс задачи';percent.textContent = '—';compactPercent.textContent='—';fill.style.width = '0%';
      bar.hidden = true;bar.removeAttribute('aria-valuenow');summary.textContent = '';runtime.textContent = '';basis.textContent = '';current.textContent = '';forecast.textContent = '';
      checkpoints.replaceChildren();results.replaceChildren();times.replaceChildren();
      showConnection('Данные ещё не получены', false);
    }
    function render(data) {
      confirmed = data;
      title.textContent = text(data.title) || 'Прогресс задачи';
      const view = describe(data), validPercent = view.percent !== null;
      percent.textContent = validPercent ? view.percent.toLocaleString('ru-RU', {maximumFractionDigits:2}) + '%' : '—';
      compactPercent.textContent=percent.textContent;
      bar.hidden = !validPercent;fill.style.width = validPercent ? view.percent + '%' : '0%';
      if (validPercent) bar.setAttribute('aria-valuenow', String(view.percent));else bar.removeAttribute('aria-valuenow');
      runtime.hidden = !view.hasRuntime;
      runtime.textContent = localStatus(data) ? 'Состояние выполнения в Codex уточняется' : 'Сейчас: ' + view.runtime + (data.runtime_stale === true ? ' · фактический статус устарел' : '');
      basis.textContent = view.previous ? 'Предыдущая фаза · готовность текущего хода не сообщалась' : '';
      basis.hidden = !basis.textContent;
      summary.textContent = text(data.status) ? (view.hasRuntime ? 'План: ' : '') + text(data.status) : '';
      current.textContent = text(data.current);forecast.textContent = text(data.forecast);
      checkpoints.replaceChildren(...(Array.isArray(data.checkpoints) ? data.checkpoints : []).map(item => {
        const row = node('li', 'codex-task-progress__checkpoint');
        const state = ['completed','in_progress','pending','waiting','blocked'].includes(item?.status) ? item.status : item?.done === true ? 'completed' : item?.done === false ? 'pending' : 'unknown';
        const done = state === 'completed';row.dataset.checkpointStatus = state;
        row.classList.toggle('codex-task-progress__checkpoint--done', done);
        const content = node('span', 'codex-task-progress__checkpoint-content');
        const details = node('span', 'codex-task-progress__checkpoint-details');
        details.append(node('small', 'codex-task-progress__checkpoint-state', {completed:'Готово',in_progress:'В работе',pending:'Далее',waiting:'Ожидание',blocked:'Заблокировано',unknown:'Не сообщалось'}[state]),
          node('strong', 'codex-task-progress__checkpoint-progress', checkpointProgress(item)));
        content.append(node('span', 'codex-task-progress__checkpoint-title', text(item?.title)), details);
        row.append(node('span', 'codex-task-progress__mark', done ? '✓' : state === 'in_progress' ? '●' : state === 'blocked' ? '!' : state === 'waiting' ? '◷' : state === 'unknown' ? '?' : '○'), content);
        return row;
      }));
      if (!checkpoints.childElementCount) checkpoints.append(node('li', 'codex-task-progress__empty', 'Этапы не заданы'));
      results.replaceChildren(...(Array.isArray(data.results) ? data.results : []).map(item => {
        const row = node('div', 'codex-task-progress__result');
        row.dataset.status = text(item?.status);
        row.append(node('strong', '', text(item?.title)), node('span', 'codex-task-progress__result-status', text(item?.status)), node('p', '', text(item?.detail)));
        if (item?.provenance === 'agent_report') row.append(node('small', 'codex-task-progress__provenance', 'Сообщено агентом · исполнение не проверено'));
        return row;
      }));
      if (!results.childElementCount) results.append(node('p', 'codex-task-progress__empty', 'Проверки не сообщались'));
      times.replaceChildren(
        node('p', '', 'Начало: ' + timestamp(data.started_at_msk)),
        node('p', '', 'Подтверждение источника: ' + timestamp(data.updated_at_msk)),
        node('p', '', 'Проверено: ' + timestamp(data.checked_at_msk)),
        node('p', '', 'По подтверждённому срезу: ' + duration(data))
      );
      if (view.hasRuntime) times.prepend(node('p', '', 'Фактический статус проверен: ' + timestamp(data.runtime_checked_at_msk)));
      if (text(data.source)) times.append(node('p', '', 'Источник: ' + text(data.source)));
      if (instant(data.finished_at_msk) !== null) times.append(node('p', '', 'Завершение: ' + timestamp(data.finished_at_msk)));
      const stale = (view.hasPlan && sourceStale(data)) || (data.runtime_stale === true && !localStatus(data));
      showConnection(stale ? 'Снимок устарел · показаны последние подтверждённые значения' : localStatus(data) ? 'Локальный статус задачи · обновление каждые 5 секунд' : 'Подтверждённый срез · обновление каждые 5 секунд', stale);
    }
    function invalidate() {
      generation++;clearTimer();queued = false;
      if (flight) flight.abort.abort();
    }
    async function update() {
      if (!canRead()) return;
      clearTimer();
      if (flight) { queued = true;return; }
      const stamp = generation, id = selected, abort = new AbortController();
      const running = {abort};flight = running;queued = false;
      if (!confirmed) showConnection('Проверяем прогресс задачи…', false);
      try {
        const data = await request('/threads/' + encodeURIComponent(id) + '/progress', {signal:abort.signal, etag:false, timeoutMs:6000});
        if (stamp !== generation || selected !== id || !canRead()) return;
        if (!data || data.available !== true) throw unavailable('Прогресс задачи недоступен');
        if (data.schema !== 'codex-task-progress/1' || data.thread_id !== id) throw unavailable('Снимок выбранной задачи не подтверждён');
        render(data);
      } catch (error) {
        if (stamp !== generation || selected !== id || !canRead()) return;
        showConnection((error?.progressUnavailable === true ? 'Прогресс недоступен' : 'Нет связи') + (confirmed ? ' · снимок устарел; сохранены последние значения' : ' · подтверждённых данных нет'), true);
      } finally {
        if (flight === running) flight = null;
        if (canRead()) {
          if (queued) { queued = false;update(); }
          else timer = setTimeout(update, POLL_MS);
        }
      }
    }
    function select(id) {
      if (destroyed) return;
      id = text(id);
      if (id === selected) return;
      invalidate();selected = id;reset();
      if (canRead()) update();
    }
    function start() {
      if (destroyed || active) return;
      active = true;root.hidden = false;invalidate();
      if (canRead()) update();
    }
    function stop() {
      if (destroyed) return;
      active = false;invalidate();root.hidden = true;
    }
    function visibility() {
      invalidate();
      if (canRead()) update();
    }
    function destroy() {
      if (destroyed) return;
      stop();destroyed = true;document.removeEventListener('visibilitychange', visibility);root.remove();
    }
    document.addEventListener('visibilitychange', visibility);
    return Object.freeze({select, start, stop, destroy});
  }
  window.CtdTaskProgress = Object.freeze({mount, describe});
})();
