import { api } from './api.js';
import { bindDialog } from './dialog.js';

// Music Scout: pick a video queue, then skim it one video at a time with sound. Two video
// elements take turns, so the next video is already buffering while the current one plays
// and → swaps instantly. Only the current and next videos ever hold a download.
const PAGE = 20;
// Ask for the next page while this many videos are still ahead of the reader.
const PREFETCH_AHEAD = 6;
const STATE_KEY = 'civitaiMusicScout';
// A video counts as viewed once it has been the current one this long. Holding → to flick
// past a run of thumbnails should not mark them all.
const VIEWED_AFTER_MS = 1000;
// How far a newly opened queue looks ahead for the first video not viewed yet.
const SKIP_VIEWED_PAGES = 10;
// Civitai's CDN sometimes hangs on a file rather than failing. A video or thumbnail that
// has not arrived by then is dropped instead of holding the player or a connection.
const LOAD_TIMEOUT_MS = 10000;
// A saved place is only restored while recent; after that the queue opens fresh, since
// an old list misses what has been posted since.
const RESTORE_FRESH_MS = 5 * 60 * 1000;
const REACTIONS = [['Like', '👍', 'likeCount'], ['Heart', '❤️', 'heartCount'],
  ['Laugh', '😂', 'laughCount'], ['Cry', '😢', 'cryCount']];
const AUDIO_KEY = 'civitaiMusicScoutAudio';

const $ = id => document.getElementById(id);

function readJSON(storage, key) {
  try { return JSON.parse(storage.getItem(key) || 'null') || {}; } catch (_) { return {}; }
}

function formatCount(value) {
  const number = Math.max(0, Number(value) || 0);
  return number >= 1000 ? `${(number / 1000).toFixed(number >= 10000 ? 0 : 1)}k` : String(number);
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
    // Reaction state is the account's own, so it is only known once signed in.
    auth: { connected: false, socialWrite: false },
    // Set by the server: only the collection's owner may add to it from here.
    canAddFocus: false,
    reacted: new Map(), reacting: new Set(),
    // Whether each video is in the Music Focus collection, by id; absent means unknown.
    focus: new Map(), focusing: new Set(),
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
      clearTimeout(loadTimer);
      showVideoError('This video could not be played. Press → for the next one.');
    });
    video.addEventListener('canplay', () => { if (video === current) clearTimeout(loadTimer); });
  }

  let loadTimer = 0;
  function watchLoad(video, item) {
    clearTimeout(loadTimer);
    if (video.readyState >= HTMLMediaElement.HAVE_FUTURE_DATA) return;
    loadTimer = setTimeout(() => {
      if (video !== current || video.dataset.itemId !== String(item.id) ||
          video.readyState >= HTMLMediaElement.HAVE_FUTURE_DATA) return;
      release(video);
      showVideoError('This video did not load within 10 seconds. Press → for the next one.');
    }, LOAD_TIMEOUT_MS);
  }

  // Thumbnails load as they scroll into the strip, each with the same time limit. Native
  // lazy loading gives no start time to measure that from.
  const thumbLoader = new IntersectionObserver(entries => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      const image = entry.target;
      thumbLoader.unobserve(image);
      const fail = () => { image.remove(); image.thumb?.classList.add('ms-thumb-missing'); };
      const timer = setTimeout(() => {
        if (image.complete && image.naturalWidth) return;
        image.removeAttribute('src');
        fail();
      }, LOAD_TIMEOUT_MS);
      image.addEventListener('load', () => clearTimeout(timer), { once: true });
      image.addEventListener('error', () => { clearTimeout(timer); fail(); }, { once: true });
      image.src = image.dataset.src;
    }
  }, { root: $('msStrip'), rootMargin: '0px 400px' });

  function remember() {
    sessionStorage.setItem(STATE_KEY, JSON.stringify({
      queue: state.queue, perCreator: state.perCreator, index: state.index, savedAt: Date.now(),
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
      button.title = 'Select again to start over from the top';
      button.addEventListener('click', () => selectQueue(queue.id, { force: true }));
      host.append(button);
    }
    // Queue options sit at the right end of the bar, apart from the queue choice itself.
    const capped = state.catalog.queues.filter(queue => queue.perCreator);
    const options = $('msOptions');
    options.replaceChildren();
    if (!capped.length) return;
    const label = document.createElement('label');
    label.className = 'ms-per-creator';
    label.textContent = 'Per creator ';
    const input = document.createElement('input');
    input.type = 'number'; input.min = '1'; input.max = String(state.catalog.perCreatorMax);
    input.value = String(state.perCreator); input.id = 'msPerCreator';
    input.setAttribute('aria-label', 'Videos per creator');
    input.title = `Used by ${capped.map(queue => `“${queue.name}”`).join(' and ')}`;
    input.addEventListener('change', () => {
      const value = Math.min(state.catalog.perCreatorMax, Math.max(1, Math.round(Number(input.value)) || 1));
      input.value = String(value);
      if (value === state.perCreator) return;
      state.perCreator = value;
      if (capped.some(queue => queue.id === state.queue)) selectQueue(state.queue, { force: true });
      else remember();
    });
    label.append(input);
    options.append(label);
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
      image.decoding = 'async';
      image.alt = item.username;
      image.referrerPolicy = 'no-referrer';
      image.dataset.src = item.stillUrl;
      image.thumb = thumb;
      thumbLoader.observe(image);
      const number = document.createElement('span');
      number.textContent = String(index + 1);
      thumb.append(image, number);
      thumb.addEventListener('click', () => show(index));
      strip.append(thumb);
    }
    for (const thumb of strip.children) {
      const selected = Number(thumb.dataset.index) === state.index;
      thumb.classList.toggle('viewed', !!state.items[Number(thumb.dataset.index)]?.viewed);
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
    renderReactions();
  }

  // One request per loaded page answers "have I reacted?" for every video on it.
  function loadReactionState(items) {
    if (!state.auth.connected || !items.length) return;
    const params = new URLSearchParams();
    for (const item of items) params.append('imageId', String(item.id));
    api(`/api/reaction-status?${params}`).then(result => {
      for (const [id, value] of Object.entries(result.images || {})) {
        state.reacted.set(id, new Set(value.reactions || []));
      }
      renderReactions();
    }).catch(error => console.warn('Music Scout reaction state could not be read', error));
  }

  function loadFocusState(items) {
    if (!state.auth.connected || !items.length) return;
    const params = new URLSearchParams();
    for (const item of items) params.append('imageId', String(item.id));
    api(`/api/music/focus-status?${params}`).then(result => {
      state.canAddFocus = !!result.canAdd;
      for (const [id, value] of Object.entries(result.images || {})) state.focus.set(id, !!value);
      renderReactions();
    }).catch(error => console.warn('Music Focus state could not be read', error));
  }

  // Read-only for everyone but the collection's owner. Adding always posts a comment, so
  // it goes through a confirmation; removing is left to Civitai, where a reason is given.
  function focusButton(item) {
    const name = state.catalog?.musicFocus?.name || 'Music Focus';
    const known = state.focus.has(String(item.id));
    const inside = state.focus.get(String(item.id)) === true;
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'ms-focus';
    button.classList.toggle('selected', inside);
    button.textContent = '🎸';
    button.setAttribute('aria-label', inside ? `In ${name}` : `Not in ${name}`);
    const readOnly = !state.auth.connected ? `Sign in to see what is in ${name}`
      : !known ? `Checking ${name}…`
      : !state.canAddFocus ? (inside ? `In ${name}` : `Not in ${name}`) : '';
    if (readOnly) {
      button.classList.add('read-only');
      button.setAttribute('aria-disabled', 'true');
      button.title = readOnly;
      return button;
    }
    button.title = inside ? `In ${name}. Removing is done on Civitai.` : `Add to ${name}…`;
    button.disabled = state.focusing.has(String(item.id));
    button.addEventListener('click', () => (inside ? openRemove(item) : openAdd(item)));
    return button;
  }

  const addDialog = $('msFocusDialog');
  const removeDialog = $('msRemoveDialog');
  bindDialog(addDialog, { labelledBy: 'msFocusTitle', closeButtons: [$('msFocusClose'), $('msFocusCancel')] });
  bindDialog(removeDialog, { labelledBy: 'msRemoveTitle', closeButtons: [$('msRemoveClose'), $('msRemoveCancel')] });
  let addTarget = null;
  // A dialog pauses the video. Closing it resumes only if nothing on Civitai was opened
  // meanwhile, since that page could be playing the same audio in another tab.
  let resumeAfterDialog = false;
  function pauseForDialog() {
    resumeAfterDialog = !current.paused;
    current.pause();
  }
  for (const dialog of [addDialog, removeDialog]) {
    dialog.addEventListener('close', () => {
      if (resumeAfterDialog && state.active) play(current);
      resumeAfterDialog = false;
    });
  }
  $('msRemoveOpen').addEventListener('click', () => { resumeAfterDialog = false; });
  for (const id of ['msCreator', 'msCivitai']) $(id).addEventListener('click', () => current.pause());

  function openAdd(item) {
    addTarget = item;
    $('msFocusSummary').textContent = `Either post the default comment on ${item.username}’s video and add it to ${state.catalog.musicFocus.name}, or open it on Civitai to do both yourself.`;
    // The app's own markup, shown exactly as it will be posted, link and all.
    $('msFocusComment').innerHTML = state.catalog.musicFocus.commentHtml || '';
    $('msFocusOpen').href = item.civitaiUrl;
    $('msFocusError').classList.add('hidden');
    $('msFocusConfirm').disabled = false;
    $('msFocusCopy').textContent = 'Copy comment';
    pauseForDialog();
    addDialog.showModal();
    $('msFocusConfirm').focus();
  }

  function openRemove(item) {
    $('msRemoveOpen').href = item.civitaiUrl;
    pauseForDialog();
    removeDialog.showModal();
  }

  // Copies the comment as formatted HTML as well as text, so pasting into Civitai's
  // comment editor keeps the link and the bold. The async clipboard needs a secure
  // context; a LAN address is not one, so that falls back to copying a selection.
  $('msFocusCopy').addEventListener('click', async () => {
    const preview = $('msFocusComment');
    const button = $('msFocusCopy');
    let copied = false;
    try {
      if (!window.ClipboardItem || !navigator.clipboard?.write) throw new Error('unavailable');
      await navigator.clipboard.write([new ClipboardItem({
        'text/html': new Blob([preview.innerHTML], { type: 'text/html' }),
        'text/plain': new Blob([preview.innerText], { type: 'text/plain' }),
      })]);
      copied = true;
    } catch (_) {
      const selection = getSelection();
      const range = document.createRange();
      range.selectNodeContents(preview);
      selection.removeAllRanges();
      selection.addRange(range);
      copied = document.execCommand('copy');
      selection.removeAllRanges();
    }
    button.textContent = copied ? 'Copied ✓' : 'Copy failed';
    setTimeout(() => { button.textContent = 'Copy comment'; }, 2000);
  });

  $('msFocusOpen').addEventListener('click', () => {
    // Civitai takes over from here, in another tab: stay paused and step aside.
    resumeAfterDialog = false;
    addDialog.close();
  });

  $('msFocusConfirm').addEventListener('click', async () => {
    const item = addTarget;
    if (!item) return;
    const id = String(item.id);
    state.focusing.add(id);
    $('msFocusConfirm').disabled = true;
    renderReactions();
    try {
      const result = await api('/api/music/focus', { method: 'POST', body: JSON.stringify({ imageId: item.id }) });
      state.focus.set(id, !!result.inCollection);
      addDialog.close();
      message(result.alreadyIn ? 'That video was already in Music Focus; no comment was posted.'
        : result.inCollection ? '' : 'The comment was posted, but Civitai does not list the video in Music Focus yet.');
    } catch (error) {
      $('msFocusError').textContent = error.message;
      $('msFocusError').classList.remove('hidden');
      // A failure after the comment was posted must not invite a second comment.
      $('msFocusConfirm').disabled = /comment was posted/i.test(error.message);
    } finally {
      state.focusing.delete(id);
      renderReactions();
    }
  });

  function renderReactions() {
    const host = $('msReactions');
    const item = state.items[state.index];
    if (!item) { host.replaceChildren(); return; }
    const stats = item.stats || {};
    const mine = state.reacted.get(String(item.id));
    const reason = !state.auth.connected ? 'Sign in to see and add your reactions'
      : !state.auth.socialWrite ? 'Civitai did not grant reaction access' : '';
    const buttons = REACTIONS.map(([name, icon, key]) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.dataset.reaction = name;
      const selected = !!mine?.has(name);
      button.classList.toggle('selected', selected);
      button.setAttribute('aria-pressed', String(selected));
      button.disabled = !!reason || state.reacting.has(`${item.id}:${name}`);
      button.title = reason || (selected ? `Remove your ${name}` : name);
      const count = document.createElement('b');
      count.textContent = formatCount(stats[key]);
      button.append(`${icon} `, count);
      button.addEventListener('click', () => toggleReaction(item, name, !selected));
      return button;
    });
    const total = document.createElement('span');
    total.className = 'total-reactions';
    total.title = mine ? 'Total reactions' : state.auth.connected ? 'Checking your reactions…' : 'Total reactions';
    total.textContent = `Total ${formatCount(stats.reactionCount)}`;
    host.replaceChildren(...buttons, total, focusButton(item));
  }

  async function toggleReaction(item, name, active) {
    const key = `${item.id}:${name}`;
    if (state.reacting.has(key)) return;
    state.reacting.add(key);
    renderReactions();
    try {
      const result = await api('/api/reaction', { method: 'POST',
        body: JSON.stringify({ imageId: item.id, reaction: name, active }) });
      item.stats = { ...(item.stats || {}), ...(result.stats || {}) };
      state.reacted.set(String(item.id), new Set(result.reactions || []));
    } catch (error) {
      message(error.message);
    } finally {
      state.reacting.delete(key);
      renderReactions();
    }
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
      // MF hub answers "still reading creators" until its first build is done.
      state.building = page.building ? page : null;
      if (page.building || page.offset !== state.items.length) return;
      state.items.push(...page.items);
      state.exhausted = !!page.exhausted && state.items.length >= page.total;
      loadReactionState(page.items);
      loadFocusState(page.items);
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

  let viewedTimer = 0;
  function markViewedSoon(item) {
    clearTimeout(viewedTimer);
    if (!item || item.viewed || !state.active) return;
    viewedTimer = setTimeout(() => {
      if (!state.active || state.items[state.index] !== item) return;
      item.viewed = true;
      renderStrip();
      api('/api/music/viewed', { method: 'POST', body: JSON.stringify({ imageId: item.id }) })
        .catch(error => console.warn('Music Scout could not record a viewed video', error));
    }, VIEWED_AFTER_MS);
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
      watchLoad(current, item);
      if (next) load(spare, next, 'auto'); else release(spare);
      markViewedSoon(item);
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

  // Opening a queue starts at the first video not viewed yet; restoring after a reload
  // passes the saved position instead.
  async function selectQueue(id, { force = false, index = null, refresh = true } = {}) {
    if (id === state.queue && !force && state.items.length) return;
    state.token++;
    state.refresh = refresh;
    state.queue = id; state.items = []; state.index = 0; state.exhausted = false; state.loading = null;
    for (const button of $('msQueues').querySelectorAll('.ms-queue')) {
      button.setAttribute('aria-checked', String(button.dataset.queue === id));
    }
    // Once a queue is chosen the picker shrinks to one row of buttons.
    $('musicScout').classList.add('ms-selected');
    release(current); release(spare);
    thumbLoader.disconnect();
    $('msStrip').replaceChildren();
    $('msPlayer').classList.add('hidden');
    remember();
    message('Finding videos…');
    const token = state.token;
    try {
      await fetchMore();
      while (state.building && token === state.token) {
        message(`Reading Music Focus creators… ${state.building.done}/${state.building.total}`);
        await new Promise(resolve => setTimeout(resolve, 500));
        if (token === state.token) await fetchMore();
      }
      if (token !== state.token) return;
      // Either reach the saved position, or the first video not viewed yet.
      const reached = () => index !== null ? state.items.length > index
        : state.items.some(item => !item.viewed);
      let pages = 1;
      while (!reached() && !state.exhausted && state.items.length && pages < SKIP_VIEWED_PAGES) {
        const before = state.items.length;
        await fetchMore();
        pages++;
        if (token !== state.token || state.items.length === before) break;
      }
      if (token !== state.token) return;
      if (!state.items.length) {
        message(state.exhausted ? 'No videos are in this queue at the content levels you have selected.'
          : 'No videos were found yet. Select the queue again to keep looking.');
        return;
      }
      let start = index ?? state.items.findIndex(item => !item.viewed);
      if (start >= 0) message('');
      else {
        // Everything loaded so far was viewed: carry on from the end of it.
        start = state.items.length - 1;
        message(state.exhausted ? 'You have viewed every video in this queue.'
          : `The first ${state.items.length} videos were all viewed; carrying on from there.`);
      }
      $('msPlayer').classList.remove('hidden');
      fit();
      show(Math.min(start, state.items.length - 1));
    } catch (error) {
      if (token === state.token) message(error.message);
    }
  }

  // On desktop the page fits the window: the stage takes whatever height is left below
  // the app's own header, tabs and the queue bar, so nothing scrolls.
  function fit() {
    const section = $('musicScout');
    if (section.classList.contains('hidden')) return;
    const top = section.getBoundingClientRect().top + window.scrollY;
    section.style.setProperty('--ms-top', `${Math.round(top)}px`);
  }
  window.addEventListener('resize', fit);
  // The header grows when, say, an update notice appears, which moves the section down.
  const reflow = new ResizeObserver(() => fit());
  for (const selector of ['body > header', '.view-tabs']) {
    const element = document.querySelector(selector);
    if (element) reflow.observe(element);
  }

  async function ensureCatalog() {
    api('/api/auth-status').then(auth => {
      state.auth = { connected: !!auth.connected, socialWrite: !!auth.socialWrite };
      const unknown = state.items.filter(item => !state.reacted.has(String(item.id)));
      for (let start = 0; start < unknown.length; start += PAGE) loadReactionState(unknown.slice(start, start + PAGE));
      const unchecked = state.items.filter(item => !state.focus.has(String(item.id)));
      for (let start = 0; start < unchecked.length; start += PAGE) loadFocusState(unchecked.slice(start, start + PAGE));
      renderReactions();
    }).catch(() => {});
    if (state.catalog) return;
    state.catalog = await api('/api/music/queues');
    state.perCreator = Number(saved.perCreator) || state.catalog.perCreatorDefault;
    renderQueues();
    if (saved.queue && state.catalog.queues.some(queue => queue.id === saved.queue)) {
      const recent = Date.now() - (Number(saved.savedAt) || 0) < RESTORE_FRESH_MS;
      selectQueue(saved.queue, recent ? { index: Number(saved.index) || 0, refresh: false } : {});
    }
  }

  function setActive(value) {
    state.active = !!value;
    if (!state.active) {
      // Leaving the tab silences it and drops both downloads; coming back resumes the
      // video the reader was on.
      clearTimeout(viewedTimer);
      clearTimeout(loadTimer);
      release(current); release(spare);
      return;
    }
    fit();
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
