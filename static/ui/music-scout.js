import { api } from './api.js';

// Music Scout: pick a video queue, then skim it one video at a time with sound. Two video
// elements take turns, so the next video is already buffering while the current one plays
// and → swaps instantly. Only the current and next videos ever hold a download.
const PAGE = 20;
// Ask for the next page while this many videos are still ahead of the reader.
const PREFETCH_AHEAD = 6;
const STATE_KEY = 'civitaiMusicScout';
const AUDIO_KEY = 'civitaiMusicScoutAudio';

const $ = id => document.getElementById(id);

function readJSON(storage, key) {
  try { return JSON.parse(storage.getItem(key) || 'null') || {}; } catch (_) { return {}; }
}

function formatDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}

export function mountMusicScout() {
  const saved = readJSON(sessionStorage, STATE_KEY);
  const audio = readJSON(localStorage, AUDIO_KEY);
  const state = {
    catalog: null, queue: null, perCreator: 3, items: [], index: 0, exhausted: false,
    loading: null, token: 0, active: false, refresh: false,
    muted: audio.muted === true, volume: Number.isFinite(audio.volume) ? audio.volume : 1,
  };
  let current = $('msVideoA');
  let spare = $('msVideoB');
  for (const video of [current, spare]) {
    video.controls = true;
    // Each video repeats until the reader moves on.
    video.loop = true;
    video.playsInline = true;
    video.preload = 'none';
    video.addEventListener('volumechange', () => {
      if (video !== current || video.dataset.forcedMute) return;
      state.muted = video.muted; state.volume = video.volume;
      localStorage.setItem(AUDIO_KEY, JSON.stringify({ muted: state.muted, volume: state.volume }));
    });
    video.addEventListener('error', () => {
      if (video !== current || !video.getAttribute('src')) return;
      showVideoError('This video could not be played. Press → for the next one.');
    });
  }

  function remember() {
    sessionStorage.setItem(STATE_KEY, JSON.stringify({
      queue: state.queue, perCreator: state.perCreator, index: state.index,
    }));
  }

  function message(text) { $('msMessage').textContent = text || ''; }
  function showVideoError(text) {
    $('msVideoError').textContent = text || '';
    $('msVideoError').classList.toggle('hidden', !text);
  }

  // Dropping the source is what aborts a download; pausing alone keeps buffering it.
  function release(video) {
    video.pause();
    if (video.getAttribute('src')) { video.removeAttribute('src'); video.removeAttribute('poster'); video.load(); }
    delete video.dataset.itemId;
  }

  function load(video, item, preload) {
    if (video.dataset.itemId === String(item.id)) { video.preload = preload; return; }
    video.pause();
    video.poster = item.stillUrl || '';
    video.preload = preload;
    video.src = item.videoUrl;
    video.dataset.itemId = String(item.id);
  }

  function play(video) {
    video.volume = state.volume;
    video.muted = state.muted;
    delete video.dataset.forcedMute;
    $('msSound').classList.add('hidden');
    video.play().catch(error => {
      if (video !== current || !state.active || error?.name !== 'NotAllowedError') return;
      // The browser refused sound before any click on this page. Play silently and offer
      // one click to turn it on, rather than showing a frozen frame.
      video.dataset.forcedMute = '1';
      video.muted = true;
      $('msSound').classList.remove('hidden');
      video.play().catch(() => {});
    });
  }

  function renderQueues() {
    const host = $('msQueues');
    host.replaceChildren();
    for (const queue of state.catalog.queues) {
      const card = document.createElement('div');
      card.className = 'ms-queue-card';
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'ms-queue';
      button.dataset.queue = queue.id;
      button.setAttribute('role', 'radio');
      button.setAttribute('aria-checked', String(queue.id === state.queue));
      const title = document.createElement('strong');
      title.textContent = queue.name;
      const note = document.createElement('small');
      note.textContent = queue.models ? `${queue.description} ${queue.models.join(', ')}.` : queue.description;
      button.append(title, note);
      button.title = 'Select again to start over from the newest videos';
      button.addEventListener('click', () => selectQueue(queue.id, { force: true }));
      card.append(button);
      if (queue.perCreator) {
        const label = document.createElement('label');
        label.className = 'ms-per-creator';
        label.textContent = 'Per creator ';
        const input = document.createElement('input');
        input.type = 'number'; input.min = '1'; input.max = String(state.catalog.perCreatorMax);
        input.value = String(state.perCreator); input.id = 'msPerCreator';
        input.setAttribute('aria-label', 'Videos per creator');
        input.addEventListener('change', () => {
          const value = Math.min(state.catalog.perCreatorMax, Math.max(1, Math.round(Number(input.value)) || 1));
          input.value = String(value);
          if (value === state.perCreator) return;
          state.perCreator = value;
          if (state.queue === queue.id) selectQueue(queue.id, { force: true });
          else remember();
        });
        label.append(input);
        card.append(label);
      }
      host.append(card);
    }
  }

  function renderStrip() {
    const strip = $('msStrip');
    const have = strip.children.length;
    for (let index = have; index < state.items.length; index++) {
      const item = state.items[index];
      const thumb = document.createElement('button');
      thumb.type = 'button';
      thumb.className = 'ms-thumb';
      thumb.setAttribute('role', 'option');
      thumb.title = `${item.username} · ${formatDate(item.createdAt)}`;
      thumb.dataset.index = String(index);
      const image = document.createElement('img');
      image.loading = 'lazy';
      image.decoding = 'async';
      image.alt = item.username;
      image.referrerPolicy = 'no-referrer';
      image.addEventListener('error', () => { image.remove(); thumb.classList.add('ms-thumb-missing'); }, { once: true });
      image.src = item.stillUrl;
      const number = document.createElement('span');
      number.textContent = String(index + 1);
      thumb.append(image, number);
      thumb.addEventListener('click', () => show(index));
      strip.append(thumb);
    }
    for (const thumb of strip.children) {
      const selected = Number(thumb.dataset.index) === state.index;
      thumb.setAttribute('aria-selected', String(selected));
      if (selected) thumb.scrollIntoView({ block: 'nearest', inline: 'center', behavior: 'smooth' });
    }
  }

  function renderMeta() {
    const item = state.items[state.index];
    const more = state.exhausted ? '' : '+';
    $('msCounter').textContent = item ? `${state.index + 1} / ${state.items.length}${more}` : '';
    $('msPrev').disabled = state.index <= 0;
    $('msNext').disabled = state.exhausted && state.index >= state.items.length - 1;
    if (!item) return;
    $('msCreator').textContent = item.username;
    $('msCreator').href = item.profileUrl;
    $('msCivitai').href = item.civitaiUrl;
    $('msDetail').textContent = [formatDate(item.createdAt), item.source, item.baseModel !== 'Unknown' ? item.baseModel : '']
      .filter(Boolean).join(' · ');
  }

  async function fetchMore() {
    if (state.loading || state.exhausted || !state.queue) return state.loading;
    const token = state.token;
    const params = new URLSearchParams({ queue: state.queue, offset: String(state.items.length),
      limit: String(PAGE), perCreator: String(state.perCreator) });
    // Choosing a queue starts it fresh; restoring after a reload reuses the server's copy
    // so the saved position still points at the same video.
    if (!state.items.length && state.refresh) params.set('refresh', '1');
    state.loading = api(`/api/music/queue?${params}`).then(page => {
      if (token !== state.token) return;
      // The server answers by offset, so an answer is appended only where it starts.
      if (page.offset === state.items.length) state.items.push(...page.items);
      state.exhausted = !!page.exhausted && state.items.length >= page.total;
    }).finally(() => { if (token === state.token) state.loading = null; });
    return state.loading;
  }

  // Keep the queue a few videos ahead. A short answer that is not the end only means the
  // server stopped paging for this request, so ask again while still behind.
  async function topUp() {
    let guard = 0;
    while (state.queue && !state.exhausted && state.items.length - state.index <= PREFETCH_AHEAD && guard++ < 5) {
      const before = state.items.length;
      await fetchMore();
      if (state.items.length === before && guard > 1) break;
    }
    renderStrip(); renderMeta();
  }

  function show(index) {
    if (!state.items.length) return;
    index = Math.max(0, Math.min(state.items.length - 1, index));
    state.index = index;
    remember();
    showVideoError('');
    const item = state.items[index];
    const next = state.items[index + 1];
    if (state.active) {
      // The spare element already buffering this item becomes the player.
      if (spare.dataset.itemId === String(item.id)) [current, spare] = [spare, current];
      load(current, item, 'auto');
      current.hidden = false; spare.hidden = true;
      spare.pause();
      play(current);
      if (next) load(spare, next, 'auto'); else release(spare);
    }
    renderStrip(); renderMeta();
    topUp().catch(error => message(error.message));
  }

  function step(delta) {
    const target = state.index + delta;
    if (target < 0) return;
    if (target < state.items.length) { show(target); return; }
    if (state.exhausted) { message('That is the end of this queue.'); return; }
    message('Loading more videos…');
    fetchMore().then(() => {
      message('');
      if (target < state.items.length) show(target);
      else if (state.exhausted) message('That is the end of this queue.');
    }).catch(error => message(error.message));
  }

  async function selectQueue(id, { force = false, index = 0, refresh = true } = {}) {
    if (id === state.queue && !force && state.items.length) return;
    state.token++;
    state.refresh = refresh;
    state.queue = id; state.items = []; state.index = 0; state.exhausted = false; state.loading = null;
    for (const button of $('msQueues').querySelectorAll('.ms-queue')) {
      button.setAttribute('aria-checked', String(button.dataset.queue === id));
    }
    release(current); release(spare);
    $('msStrip').replaceChildren();
    $('msPlayer').classList.add('hidden');
    remember();
    message('Finding videos…');
    const token = state.token;
    try {
      await fetchMore();
      if (token !== state.token) return;
      while (state.items.length <= index && !state.exhausted && state.items.length) {
        const before = state.items.length;
        await fetchMore();
        if (token !== state.token || state.items.length === before) break;
      }
      if (token !== state.token) return;
      if (!state.items.length) {
        message(state.exhausted ? 'No videos are in this queue at the content levels you have selected.'
          : 'No videos were found yet. Select the queue again to keep looking.');
        return;
      }
      message('');
      $('msPlayer').classList.remove('hidden');
      show(Math.min(index, state.items.length - 1));
      // The stage is sized to the window, so bring all of it into view.
      $('msPlayer').scrollIntoView({ block: 'start', behavior: 'smooth' });
    } catch (error) {
      if (token === state.token) message(error.message);
    }
  }

  async function ensureCatalog() {
    if (state.catalog) return;
    state.catalog = await api('/api/music/queues');
    state.perCreator = Number(saved.perCreator) || state.catalog.perCreatorDefault;
    renderQueues();
    if (saved.queue && state.catalog.queues.some(queue => queue.id === saved.queue)) {
      selectQueue(saved.queue, { index: Number(saved.index) || 0, refresh: false });
    }
  }

  function setActive(value) {
    state.active = !!value;
    if (!state.active) {
      // Leaving the tab silences it and drops both downloads; coming back resumes the
      // video the reader was on.
      release(current); release(spare);
      return;
    }
    ensureCatalog().then(() => {
      if (state.active && state.items.length && !current.getAttribute('src')) show(state.index);
    }).catch(error => message(error.message));
  }

  $('msPrev').addEventListener('click', () => step(-1));
  $('msNext').addEventListener('click', () => step(1));
  $('msSound').addEventListener('click', () => {
    state.muted = false;
    play(current);
  });

  document.addEventListener('keydown', event => {
    if (!state.active || !state.items.length || event.defaultPrevented) return;
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    const target = event.target;
    if (target instanceof HTMLElement && (target.isContentEditable ||
        ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName))) return;
    if (document.querySelector('dialog[open]')) return;
    if (event.key === 'ArrowRight') step(1);
    else if (event.key === 'ArrowLeft') step(-1);
    else if (event.key === ' ' || event.key === 'k') {
      // The focused native control would toggle playback a second time.
      if (target === current) return;
      if (current.paused) play(current); else current.pause();
    } else if (event.key === 'm' || event.key === 'M') {
      current.muted = !current.muted;
      $('msSound').classList.add('hidden');
      delete current.dataset.forcedMute;
      state.muted = current.muted;
      localStorage.setItem(AUDIO_KEY, JSON.stringify({ muted: state.muted, volume: state.volume }));
    } else return;
    event.preventDefault();
  });

  return { setActive };
}
