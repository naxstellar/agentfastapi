// The only way the UI talks to /api/v1: adds the CSRF header and turns failures into ApiError with a
// message that is safe to show.
export class ApiError extends Error {
  constructor(status, message, retryAfter = null) {
    super(message);
    this.status = status;
    this.retryAfter = retryAfter;
  }
}

const networkListeners = new Set();
export function onNetworkChange(callback) {
  networkListeners.add(callback);
}
function setOnline(online) {
  networkListeners.forEach((callback) => callback(online));
}
window.addEventListener("online", () => setOnline(true));
window.addEventListener("offline", () => setOnline(false));

function detailText(body, fallback) {
  const detail = body && body.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((d) => d.msg).join("; ");
  return fallback;
}

export function resetMessage(retryAfterSeconds) {
  const when = new Date(Date.now() + retryAfterSeconds * 1000);
  return when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export async function api(path, { method = "GET", body } = {}) {
  const headers = { "X-Requested-With": "fetch" };
  const options = { method, headers, credentials: "same-origin" };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(`/api/v1${path}`, options);
  } catch {
    setOnline(false);
    throw new ApiError(0, "Can't reach the server. Check your connection and try again.");
  }
  setOnline(true);
  if (response.status === 401) {
    window.location.href = "/loginuser";
    throw new ApiError(401, "Your session ended. Redirecting to sign in…");
  }
  if (response.status === 204) return null;
  let data = null;
  try {
    data = await response.json();
  } catch {
    /* non-JSON error body */
  }
  if (response.ok) return data;

  const retryAfter = Number(response.headers.get("Retry-After")) || null;
  if (response.status === 429) {
    const message =
      retryAfter && retryAfter >= 60
        ? `Daily token budget used up. It resets at ${resetMessage(retryAfter)}.`
        : "Too many runs in progress. Try again in a few seconds.";
    const error = new ApiError(429, message, retryAfter);
    error.quota = Boolean(retryAfter && retryAfter >= 60); // the daily budget, not the concurrency limit
    throw error;
  }
  if (response.status === 503) throw new ApiError(503, detailText(data, "A service is unavailable. Try again shortly."));
  throw new ApiError(response.status, detailText(data, `Request failed (${response.status})`));
}
