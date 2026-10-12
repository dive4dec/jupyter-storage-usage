# Changelog

## 0.1.1

- **Fix**: JS status bar widget stuck on "loading…" due to browser caching stale JS. Added MD5 content hash to `<script>` URL (`?v={hash}`) so browsers bust cache automatically when the JS file changes.
- **Fix**: Python 3.14 compatibility — replaced `asyncio.get_event_loop()` with Tornado `IOLoop.asyncio_loop` in the background monitor, preventing monitor crash on pod start.

## 0.1.0

- Initial release: storage usage monitor, status bar widget, and cleanup portal.
