// Service worker mínimo, só pra satisfazer o requisito de instalabilidade
// de PWA (Chrome/Android exige um fetch handler registrado). Sem cache de
// verdade por enquanto — cross-origin (Plotly/Socket.IO via CDN) passa
// direto, sem ser interceptado, pra evitar problemas de resposta opaca no
// Safari/iOS.

self.addEventListener('install', () => {
    self.skipWaiting();
});

self.addEventListener('activate', (event) => {
    event.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', (event) => {
    const url = new URL(event.request.url);
    if (url.origin !== self.location.origin) {
        return; // deixa requests cross-origin passarem sem interceptar
    }
    event.respondWith(fetch(event.request));
});
