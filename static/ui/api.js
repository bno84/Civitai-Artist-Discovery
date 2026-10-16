// One transport contract for all pages. Callers may pass AbortSignal via options.
export async function api(url, options = {}) {
  const { signal, timeoutMs = String(url).startsWith('/api/history/image?') ? 90000 : 60000, ...request } = options;
  const controller = new AbortController();
  let timedOut = false;
  const abort = () => controller.abort(signal?.reason);
  if (signal?.aborted) abort();
  else signal?.addEventListener('abort', abort, {once: true});
  const timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeoutMs);
  const headers = new Headers(request.headers);
  if (!headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  try {
    // Server read paths own retries. This layer never replays a request or mutation.
    const response = await fetch(url, { ...request, headers, signal: controller.signal });
    const body = await response.json().catch(() => null);
    if (!response.ok) {
      const error = new Error(body?.error || `The app returned HTTP ${response.status}. Try again shortly.`);
      error.status = response.status;
      throw error;
    }
    if (body === null) throw new Error('The app returned an unreadable response. Please try again.');
    return body;
  } catch (error) {
    if (timedOut) throw new Error('The request timed out. Your saved gallery is safe; please try again.');
    throw error;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', abort);
  }
}
