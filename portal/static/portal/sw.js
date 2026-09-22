// Maorif Portal — minimal PWA service worker (Phase 1: installability only).
//
// Deliberately inert: no CacheStorage, no offline data, no request
// interception. The portal always talks to the live Django backend.
//
// The empty fetch listener exists only so browsers that check for a fetch
// handler consider the app installable. It never handles the event, so
// every request goes to the network exactly as before.

self.addEventListener('install', function (event) {
    self.skipWaiting();
});

self.addEventListener('activate', function (event) {
    event.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', function () {
    // Intentionally empty — pass-through to the network.
});
