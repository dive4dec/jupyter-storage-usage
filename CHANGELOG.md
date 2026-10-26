# Changelog

## 0.1.3

- **Performance fix (portal no longer hangs on large/NFS homes)**: the
  cleanup portal used to run a full recursive `du` over the home dir **plus**
  one `du` per top-level directory, sequentially, on **every** page load. On
  a 0.5 CPU cleanup pod with a large NFS-backed home (e.g. ~50 GiB / ~180k
  files), a single `du` took longer than its 60 s timeout, so the first
  render — and often every render — appeared stuck loading forever. The portal
  now runs **one** `du --max-depth=1 --apparent-size` (home total + every
  top-level dir in a single pass) in a **background task**, started at server
  boot and refreshed every `JSU_USAGE_CACHE_REFRESH_SEC` (default 60 s). Pages
  render immediately using cached sizes, or show "…" (with an automatic
  page-refresh) until the first `du` lands. Deleting a file invalidates the
  cache and triggers a refresh; the post-delete hub report reuses the same
  fast `du` instead of running its own. File sizes always use `stat` (cheap);
  only top-level **directory** sizes wait for the du cache.
- **Test hygiene**: `tests/test_functional.py` now picks a free port,
  registers an `atexit` reaper that SIGKILLs its server (so a failure during
  the collection-time test body can't orphan a Jupyter server and poison the
  next run), and waits for the server to accept connections instead of a blind
  `sleep(5)`. `tests/test_cleanup.py` is now order-independent with respect to
  `JSU_HOME_DIR`.

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
