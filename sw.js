// Scanner SMA: funcionamiento sin conexión
const CACHE = "scanner-sma-v1";
const SHELL = ["./", "index.html", "manifest.json", "icon-192.png", "icon-512.png", "apple-touch-icon.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// Red primero con tiempo límite (para Wi-Fi sin datos, como el del avión); si falla, copia guardada.
function networkFirst(req, key, ms) {
  return new Promise(resolve => {
    let done = false;
    const fallback = async () => {
      if (done) return;
      const hit = await caches.match(key);
      if (hit && !done) {
        done = true;
        const body = await hit.blob();
        const h = new Headers(hit.headers);
        h.set("X-SW-Cache", "1");
        resolve(new Response(body, {status: 200, headers: h}));
      }
    };
    const timer = setTimeout(fallback, ms);
    fetch(req, {cache: "no-store"}).then(async res => {
      if (res && res.ok) {
        const copy = res.clone();
        caches.open(CACHE).then(c => c.put(key, copy));
        if (!done) { done = true; clearTimeout(timer); resolve(res); }
      } else {
        clearTimeout(timer); await fallback();
        if (!done) { done = true; resolve(res); }
      }
    }).catch(async () => {
      clearTimeout(timer); await fallback();
      if (!done) { done = true; resolve(new Response("", {status: 503})); }
    });
  });
}

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.endsWith("/data.json")) {
    e.respondWith(networkFirst(e.request, new URL("data.json", self.registration.scope).href, 8000));
  } else if (e.request.mode === "navigate" || url.pathname.endsWith("/index.html")) {
    e.respondWith(networkFirst(e.request, new URL("index.html", self.registration.scope).href, 4000));
  } else {
    e.respondWith(caches.match(e.request).then(hit => hit || fetch(e.request)));
  }
});
