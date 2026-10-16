// Gallery, Time Machine, and details share preview fallback and loading feedback.
import { artworkContext, reportClientEvent } from './diagnostics.js';
// Visibility checks belong to the caller: never assign artwork before they succeed.
export function cardPreviewUrl(source, width, density = 1) {
  if (!source || !Number.isFinite(width) || width <= 0) return source;
  let url;
  try { url = new URL(source); } catch (_) { return source; }
  // Only change the CDN transformation format already produced by the archive.
  // Originals, local fixtures, data URLs, and other transformation options stay intact.
  if (url.protocol !== 'https:' || url.hostname !== 'image.civitai.com' ||
      !/\/width=\d+\/[^/]+$/.test(url.pathname)) return source;
  const required = width * Math.min(2, Math.max(1, density || 1));
  const target = [384, 512, 768, 1024, 1280].find(value => value >= required) || 1280;
  url.pathname = url.pathname.replace(/\/width=\d+\//, `/width=${target}/`);
  return url.href;
}

export function showCardArtwork(image, previewUrl, originalUrl) {
  const bounds = image.getBoundingClientRect();
  const visible = bounds.bottom > 0 && bounds.top < innerHeight;
  image.loading = visible ? 'eager' : 'lazy';
  image.fetchPriority = visible ? 'high' : 'low';
  showArtwork(image, cardPreviewUrl(previewUrl, bounds.width, devicePixelRatio), originalUrl);
}

export function wireArtworkFallback(image) {
  if (!image || image.dataset.fallbackWired) return;
  image.dataset.fallbackWired = '1';
  image.addEventListener('load', () => {
    if (image.videoPosterScope) return;
    if (image.dataset.fallbackUsed === '1') {
      reportClientEvent('image_load_recovered', { ...artworkContext(image),
        reason: 'original-loaded-after-preview-failed', url: image.currentSrc || image.src });
      image.dataset.fallbackUsed = '0';
    }
    image.classList.remove('image-error', 'image-pending');
  });
  image.addEventListener('error', () => {
    if (image.videoPosterScope) return;
    const fallback = image.dataset.fallbackUrl;
    if (fallback && image.dataset.fallbackPending === '1') {
      reportClientEvent('image_load_failed', { ...artworkContext(image),
        reason: 'preview-failed-trying-original', url: image.currentSrc || image.src });
      image.dataset.fallbackUsed = '1';
      image.dataset.fallbackPending = '0';
      image.src = fallback;
      return;
    }
    image.classList.remove('image-pending');
    image.classList.add('image-error');
    reportClientEvent('image_load_failed', { ...artworkContext(image),
      reason: image.dataset.fallbackUsed === '1' ? 'original-failed' : 'image-failed',
      url: image.currentSrc || image.src });
  });
}

export function showArtwork(image, previewUrl, originalUrl) {
  image.cancelArtwork?.();
  wireArtworkFallback(image);
  const preview = previewUrl || originalUrl || '';
  const fallback = originalUrl && originalUrl !== preview ? originalUrl : '';
  // Painting reactions or metadata must not restart the same artwork request/fallback.
  if (image.dataset.previewUrl === preview && image.getAttribute('src')) return;
  image.dataset.previewUrl = preview;
  image.dataset.fallbackUsed = '0';
  image.classList.remove('image-error');
  image.classList.toggle('image-pending', !!preview);
  image.dataset.fallbackUrl = fallback;
  image.dataset.fallbackPending = fallback ? '1' : '0';
  image.decoding = 'async';
  if (preview) image.src = preview;
  else {
    image.removeAttribute('src');
    image.classList.add('image-error');
  }
}

const posterFailures = new Map(), workingPosters = new Map();
const POSTER_TIMEOUT_MS = 15000;
function boundedSet(map, key, value) {
  map.delete(key); map.set(key, value);
  if (map.size > 256) map.delete(map.keys().next().value);
}

// A poster error never starts a media request. Content checks remain the caller's job.
export function showVideoPoster(image, item) {
  const candidates = [...new Set(item.thumbnailCandidates || [item.detailImageUrl || item.thumbnailUrl])].filter(Boolean);
  const key = JSON.stringify([item.id, item.url, candidates]);
  if (image.videoPosterScope?.key === key) return;
  const old = image.videoPosterScope;
  const reusable = old?.id === item.id && old.original === item.url && image.naturalWidth > 0 &&
    (candidates.includes(old.url) || old.url === old.candidates.at(-1)) ? old.url : workingPosters.get(key);
  image.cancelArtwork?.();
  const scope = { key, id: item.id, original: item.url, candidates, url: '', generation: 0, failed: false };
  image.videoPosterScope = scope;
  const stage = image.closest('.image-stage');
  let cleanup = () => {};
  image.cancelArtwork = () => {
    scope.generation++; cleanup();
    if (image.videoPosterScope === scope) { delete image.videoPosterScope; image.removeAttribute('src'); }
    stage?.classList.remove('video-preview-unavailable');
  };
  image.decoding = 'async';
  image.loading = 'eager';
  image.classList.remove('image-error');
  image.classList.add('image-pending');
  stage?.classList.remove('video-preview-unavailable');
  const plan = [...new Set([reusable, ...candidates])].filter(Boolean);
  function next() {
    cleanup();
    if (image.videoPosterScope !== scope) return;
    const url = plan.shift();
    if (!url) {
      image.classList.remove('image-pending'); image.classList.add('image-error');
      image.removeAttribute('src'); stage?.classList.add('video-preview-unavailable');
      return;
    }
    if (Date.now() - (posterFailures.get(url) || 0) < 300000) { next(); return; }
    scope.url = url;
    const generation = ++scope.generation;
    const valid = () => image.videoPosterScope === scope && generation === scope.generation;
    const matchingSource = () => !image.currentSrc || image.currentSrc === image.src;
    const loaded = () => {
      if (!valid() || !matchingSource() || !image.naturalWidth) return;
      cleanup(); image.classList.remove('image-pending', 'image-error');
      boundedSet(workingPosters, key, url);
      if (scope.failed) reportClientEvent('image_load_recovered', { ...artworkContext(image), reason: 'video-poster-loaded', url });
    };
    const failed = (deadline = false) => {
      if (!valid() || (deadline !== true && !matchingSource())) return;
      scope.failed = true;
      boundedSet(posterFailures, url, Date.now());
      reportClientEvent('image_load_failed', { ...artworkContext(image), reason: 'video-poster-failed', url });
      scope.generation++; image.removeAttribute('src'); next();
    };
    const timer = setTimeout(() => failed(true), POSTER_TIMEOUT_MS);
    cleanup = () => { clearTimeout(timer); image.removeEventListener('load', loaded); image.removeEventListener('error', failed); };
    image.addEventListener('load', loaded); image.addEventListener('error', failed);
    image.src = url;
  }
  next();
}
