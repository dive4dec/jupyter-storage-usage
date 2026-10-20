# Changelog

## 0.1.2

- **Feature**: "no limit" support for standalone (non-JupyterHub) runs.
  `JSU_HARD_LIMIT_BYTES` now defaults to `0` (= unlimited) instead of 20 GiB.
  On JupyterHub the pre-spawn hook always injects the resolved per-user/group
  limit, so hub behaviour is unchanged. `parse_quantity` accepts
  `unlimited`/`none`/`infinity`/`inf`/`-` (case-insensitive) → 0. The widget
  and tooltips show "unlimited" / usage-only instead of a confusing "0Gi",
  and never reports `over_limit` when the limit is 0.

## 0.1.1

- **Fix**: JS status bar widget stuck on "loading…" due to browser caching stale JS. Added MD5 content hash to `<script>` URL (`?v={hash}`) so browsers bust cache automatically when the JS file changes.
- **Fix**: Python 3.14 compatibility — replaced `asyncio.get_event_loop()` with Tornado `IOLoop.asyncio_loop` in the background monitor, preventing monitor crash on pod start.

## 0.1.0

- Initial release: storage usage monitor, status bar widget, and cleanup portal.
