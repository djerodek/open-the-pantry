const CACHE_NAME = "open-the-pantry-shell-v24";
const SHELL_ASSETS = [
  "/",
  "/index.html",
  "/css/styles.css",
  "/js/app.js",
  "/manifest.json",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// Network-first for everything: correctness (always get the latest shell/
// API/upload content when online) matters more here than cache-first speed,
// and the cache fallback still covers offline use. This also avoids a real
// staleness trap: browsers only re-run this service worker's install event
// when service-worker.js's OWN bytes change -- shipping a new app.js or
// styles.css alone would never re-trigger caching under a cache-first
// strategy, so users could be stuck on stale JS/CSS indefinitely even
// after upgrading the image. CACHE_NAME is still bumped on release so the
// activate handler prunes the old cache promptly.
// What's worth keeping for offline use: the app itself, photos, and plain
// API reads. It used to cache every GET -- including backup zips of
// hundreds of MB, exports, error responses, and one entry per search URL,
// none of it ever pruned until CACHE_NAME changed.
const NEVER_CACHE = [/^\/api\/backup\//, /\/export\.(pdf|html)$/, /^\/api\/email-settings/, /^\/api\/client-error/];
function shouldCache(request, response) {
  if (!response || !response.ok || response.type !== "basic") return false;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return false;
  if (NEVER_CACHE.some((re) => re.test(url.pathname))) return false;
  if (url.pathname.startsWith("/api/") && url.search) return false;  // searches, filters, sorts
  const disposition = response.headers.get("Content-Disposition") || "";
  return !disposition.startsWith("attachment");
}

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") {
    event.respondWith(fetch(event.request));
    return;
  }
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        if (shouldCache(event.request, response)) {
          const copy = response.clone();
          caches.open(CACHE_NAME)
            .then((cache) => cache.put(event.request, copy))
            .catch(() => {});  // storage full or private mode: caching is optional
        }
        return response;
      })
      .catch(() =>
        caches.match(event.request).then((cached) => {
          if (cached) return cached;
          // Offline and never cached: respondWith() can't resolve to
          // undefined, which is what an unmatched caches.match() returns --
          // that surfaces as a hard network error rather than a handled
          // response. Return an explicit fallback instead.
          const isApi = new URL(event.request.url).pathname.startsWith("/api/");
          return new Response(
            isApi ? JSON.stringify({ detail: "Offline and no cached response available." }) : "Offline",
            {
              status: 503,
              headers: { "Content-Type": isApi ? "application/json" : "text/plain" },
            }
          );
        })
      )
  );
});
