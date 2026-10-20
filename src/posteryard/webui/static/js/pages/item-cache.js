// When a title's images last changed in this session, so pages skip the browser's cached copy (max-age=60).
const changed = new Map();

export function markChanged(key) {
  changed.set(String(key), Date.now());
}

/** A cache-busting value for imageUrl: the last change in this session, else the newest record time. */
export function bustFor(key, records = null) {
  const recent = changed.get(String(key));
  if (recent) return recent;
  const times = (records || []).map((r) => r.updated_at || 0);
  return times.length ? Math.max(...times) : null;
}
