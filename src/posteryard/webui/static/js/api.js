// API client. Every page talks to the server through these helpers only.

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

const JOB_POLL_MS = 700;

async function request(method, path, body, { raw = false, contentType = null } = {}) {
  const headers = { Accept: 'application/json' };
  if (method !== 'GET' && method !== 'HEAD') headers['X-Requested-With'] = 'posteryard';
  let payload;
  if (raw) {
    payload = body;
    headers['Content-Type'] = contentType || body.type || 'application/octet-stream';
  } else if (body !== undefined) {
    payload = JSON.stringify(body);
    headers['Content-Type'] = 'application/json';
  }
  let response;
  try {
    response = await fetch(path, { method, headers, body: payload, credentials: 'same-origin' });
  } catch (err) {
    throw new ApiError(0, 'Posteryard is not reachable. Is it running?');
  }
  const text = await response.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch { data = { error: text }; }
  }
  if (!response.ok) {
    if (response.status === 401 && !path.startsWith('/api/login')) {
      window.dispatchEvent(new CustomEvent('posteryard:unauthorized'));
    }
    throw new ApiError(response.status, (data && data.error) || `${response.status} ${response.statusText}`);
  }
  return data;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export const api = {
  get: (path) => request('GET', path),
  post: (path, body) => request('POST', path, body ?? {}),
  put: (path, body) => request('PUT', path, body ?? {}),
  del: (path) => request('DELETE', path),
  upload: (path, blob) => request('POST', path, blob, { raw: true }),

  /** Wait for a job to finish. Resolves with its result; rejects with ApiError on failure. */
  async wait(jobId, { onUpdate } = {}) {
    for (;;) {
      const job = await request('GET', `/api/jobs/${encodeURIComponent(jobId)}`);
      if (onUpdate) onUpdate(job);
      if (job.state === 'done') return job.result;
      if (job.state === 'failed') throw new ApiError(500, job.error || 'The job failed');
      await sleep(JOB_POLL_MS);
    }
  },

  /** POST to a job route (or upload when `blob` is given) and wait for the result. */
  async job(path, body, { onUpdate, blob } = {}) {
    const started = blob ? await api.upload(path, blob) : await api.post(path, body);
    if (!started || !started.job) return started;
    return api.wait(started.job, { onUpdate });
  },
};

/** URL of an item image: source is auto, preview or server; w resizes on the server. */
export function imageUrl(key, { target = 'poster', source = 'auto', w = null, bust = null } = {}) {
  const params = new URLSearchParams({ target, source });
  if (w) params.set('w', String(w));
  if (bust) params.set('v', String(bust));
  return `/api/items/${encodeURIComponent(key)}/image?${params}`;
}
