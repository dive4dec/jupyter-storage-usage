"""Cleanup portal — restricted file manager for over-limit users.

Runs INSIDE the user's cleanup pod (not the hub pod), so heavy file
operations (du, tar, rm on large directories) cannot affect JupyterHub.
The pod has resource limits (e.g. 256Mi, 0.5 CPU) set by the spawner.

In cleanup mode (JSU_CLEANUP_MODE=1), this takes over the Jupyter server,
serving a file manager that allows only browse, delete, and download.
No upload, no create, no edit — application-layer enforcement.

Authentication: Jupyter server's built-in auth (JupyterHub token).
The student can only access their own pod's filesystem (their PVC mount).
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import shutil
import stat
import subprocess
import tempfile
import time
from urllib.parse import quote, unquote

from jupyter_server.base.handlers import JupyterHandler
from tornado import web

from .config import (
    CLEANUP_DU_MAX_SECONDS,
    HARD_LIMIT_BYTES,
    HOME_DIR,
    HUB_REPORT_URL,
    USAGE_CACHE_REFRESH_SEC,
    WARN_LIMIT_BYTES,
)

logger = logging.getLogger("jupyter_storage_usage")

_GIB = 1024**3


def _fmt_bytes(b: int) -> str:
    if b >= _GIB:
        return f"{b / _GIB:.1f} GiB"
    if b >= 1024**2:
        return f"{b / 1024**2:.1f} MiB"
    if b >= 1024:
        return f"{b / 1024:.1f} KiB"
    return f"{b} B"


def _resolve_safe(base: str, sub: str) -> str:
    """Resolve sub-path under base, preventing directory traversal.

    Raises ValueError if the resolved path escapes base.
    """
    base_real = os.path.realpath(base)
    sub = unquote(sub).lstrip("/")
    full = os.path.realpath(os.path.join(base_real, sub))
    if not full.startswith(base_real + os.sep) and full != base_real:
        raise ValueError(f"Path escapes home directory: {sub}")
    return full


# ── Background du cache (portal never blocks on disk usage) ─────────────
#
# The old portal ran `du` over the home dir AND over every top-level
# directory, sequentially, on EVERY page load. On a 0.5 CPU cleanup pod
# with a large NFS-backed home, a single full du takes minutes, so the
# first render (and often every render) appeared stuck loading forever.
#
# Now: one `du --max-depth=1 --apparent-size` (home total + every
# top-level dir in a single pass) runs in a background task, started at
# server boot and refreshed every USAGE_CACHE_REFRESH_SEC. Pages render
# immediately and show cached sizes or "…" until the first du lands.

def _run_du_maxdepth1() -> tuple[int, dict[str, int]]:
    """Single du pass: home total + each top-level entry's size.

    One `du --max-depth=1 --apparent-size` over HOME_DIR replaces the old
    "1 du for the home + N du for every top-level dir" (N+1 passes) with
    one pass.  The last line of du's output is the total; preceding lines
    are the direct children, keyed by basename.
    """
    r = subprocess.run(
        [
            "du", "--max-depth=1", "--apparent-size", "--block-size=1",
            "--", HOME_DIR,
        ],
        capture_output=True, text=True,
        timeout=CLEANUP_DU_MAX_SECONDS,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return 0, {}
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    if not lines:
        return 0, {}
    total = int(lines[-1].split("\t")[0])
    sizes: dict[str, int] = {}
    for ln in lines[:-1]:
        parts = ln.split("\t", 1)
        if len(parts) != 2:
            continue
        try:
            sz = int(parts[0])
        except ValueError:
            continue
        name = os.path.basename(parts[1].rstrip("/"))
        if name:
            sizes[name] = sz
    return total, sizes


class _UsageCache:
    """In-process cache of home-dir usage from the background du."""

    def __init__(self) -> None:
        self.total: int | None = None
        self.dir_sizes: dict[str, int] = {}
        self.computed_at: float | None = None  # when the current data was computed
        self.started_at: float | None = None   # when the current du started
        self._inflight: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    @property
    def fresh(self) -> bool:
        return (
            self.total is not None
            and self.computed_at is not None
            and (time.monotonic() - self.computed_at) < USAGE_CACHE_REFRESH_SEC
        )

    def invalidate(self) -> None:
        """Drop the data (e.g. after a delete); next refresh recomputes."""
        self.total = None
        self.computed_at = None
        self.started_at = None

    async def _run_one(self) -> None:
        """Perform one du (thread) and publish the result on this cache."""
        try:
            total, sizes = await asyncio.to_thread(_run_du_maxdepth1)
        except (OSError, subprocess.TimeoutExpired, ValueError) as e:
            logger.info("Background du failed (will retry next cycle): %s", e)
            return
        self.total = total
        self.dir_sizes = sizes
        self.computed_at = time.monotonic()
        self.started_at = None
        logger.info(
            "Usage cache updated: %.2f GiB total, %d top-level entries",
            total / _GIB, len(sizes),
        )


_USAGE_CACHE = _UsageCache()


async def _compute_usage() -> None:
    """Run one du in the background; skip if data is still fresh."""
    cache = _USAGE_CACHE
    if cache.fresh:
        return
    async with cache._lock:
        if cache.fresh:
            return
        if cache._inflight is not None:
            return
        cache.started_at = time.monotonic()
        cache._inflight = asyncio.get_running_loop().create_task(cache._run_one())


async def _ensure_usage_cached(wait: bool = False) -> None:
    """Kick the background du (always non-blocking) and optionally wait.

    wait=True is used by the post-delete hub report: it waits for the
    in-flight du to land (bounded by CLEANUP_DU_MAX_SECONDS) so the
    reported number is accurate. Pages never wait.
    """
    await _compute_usage()
    if not wait or _USAGE_CACHE.fresh:
        return
    task = _USAGE_CACHE._inflight
    if task is None:
        return
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=CLEANUP_DU_MAX_SECONDS)
    except asyncio.TimeoutError:
        logger.info("Background du exceeded %.0fs — leaving stale cache", CLEANUP_DU_MAX_SECONDS)


def start_usage_refresh() -> None:
    """Schedule the background du: initial warm + periodic refresh.

    Called once from the extension loader in cleanup mode. No-op if the
    event loop isn't running yet (the loader defers this via IOLoop).
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_compute_usage())  # initial warm
    loop.create_task(_periodic_usage_refresh())


async def _periodic_usage_refresh() -> None:
    """Keep the du cache fresh in the background, forever."""
    while True:
        await asyncio.sleep(USAGE_CACHE_REFRESH_SEC)
        try:
            await _compute_usage()
            await _ensure_usage_cached(wait=False)
        except Exception as e:  # never let the periodic task die
            logger.warning("Usage refresh cycle failed: %s", e)


# ── Unified handler ─────────────────────────────────────────────────────

class CleanupHandler(JupyterHandler):
    """Handles all cleanup portal routes.

    GET  /cleanup/         → directory listing (root = home dir)
    GET  /cleanup/{path}   → browse dir, show file info, or download (?download=1)
    POST /cleanup/{path}   → delete file/directory (requires ?confirm=yes)
    """

    # Auth: JupyterHub's configurable-http-proxy authenticates the user
    # before routing requests to this server. The OAuth flow sets a cookie
    # on the server side. However, the very first request after spawn may
    # arrive before the OAuth cookie is set (the redirect to /cleanup/
    # fires before OAuth completes). We therefore don't block GET (browse)
    # — the proxy already ensures only the authenticated user can reach us.
    # POST (delete) still requires auth as a safety check.

    # Disable XSRF for the cleanup portal.  Jupyter Server enables XSRF
    # globally, which blocks our plain HTML <form method=POST> because it
    # has no _xsrf token.  XSRF is not needed here: JupyterHub's proxy
    # already authenticates the user, and delete requires ?confirm=yes.
    def check_xsrf_cookie(self):
        pass

    def _cleanup_url(self, sub_path: str = "") -> str:
        """Build a URL path with the server's base_url prefix.

        JupyterHub serves each user under a sub-path like
        /cs1302_edb/user/student0/.  All internal links, redirects, and
        form actions must include this prefix or they'll hit the hub
        instead of the cleanup portal.
        """
        base = self.base_url.rstrip("/")  # e.g. /cs1302_edb/user/student0
        p = sub_path.lstrip("/")
        return f"{base}/cleanup/{p}"

    async def _report_usage_to_hub(self):
        """Report fresh storage usage to the hub after a delete.

        Triggers the background du and POSTs the fresh total to the hub's
        /report endpoint so the cache is updated immediately (not after
        the next 30-min poll).  Best-effort: errors are logged but don't
        block the delete response.

        This is the ONE place we wait for a du — a delete just changed
        the home dir, so the cached number is stale.  The wait is bounded
        by CLEANUP_DU_MAX_SECONDS and only happens in the delete handler,
        never on page loads.
        """
        try:
            _USAGE_CACHE.invalidate()
            await _ensure_usage_cached(wait=True)
            used = _USAGE_CACHE.total
            if used is None:
                logger.info("Skipping hub usage report — du not ready yet")
                return
            import urllib.request, urllib.parse, os
            hub_url = HUB_REPORT_URL
            if not hub_url:
                return
            username = os.environ.get("JUPYTERHUB_USER", "")
            if not username:
                return
            from .config import HUB_API_TOKEN
            params = urllib.parse.urlencode({
                "username": username,
                "used_bytes": used,
                "server_url": os.environ.get("JUPYTERHUB_SERVICE_PREFIX", "/"),
            })
            # Use a simple sync call in a thread — it's fast and non-blocking
            def _report():
                full_url = f"{hub_url}?{params}"
                req = urllib.request.Request(full_url)
                if HUB_API_TOKEN:
                    req.add_header("Authorization", f"token {HUB_API_TOKEN}")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return resp.status
            status = await asyncio.to_thread(_report)
            logger.info("Reported fresh usage to hub: %d bytes (HTTP %d)", used, status)
        except Exception as e:
            logger.debug("Failed to report usage to hub: %s", e)

    async def get(self, sub_path: str = ""):
        # Auth: JupyterHub proxy already authenticated the user.
        # Don't check self.current_user — it may be None on first load
        # before the OAuth cookie is set.
        # Download mode
        if self.get_argument("download", None):
            await self._handle_download(sub_path)
            return

        try:
            fs_path = _resolve_safe(HOME_DIR, sub_path)
        except ValueError as e:
            raise web.HTTPError(403, str(e))

        # Kick the background du if it's stale, but NEVER wait for it here.
        # The page renders immediately from the cache (or with "…").
        await _compute_usage()
        usage = _USAGE_CACHE
        computing = not usage.fresh
        total = usage.total if usage.total is not None else 0
        # hard_limit 0 = no limit (standalone): never over_limit.
        if HARD_LIMIT_BYTES > 0 and total > HARD_LIMIT_BYTES:
            usage_status = "over_limit"
        elif total > WARN_LIMIT_BYTES:
            usage_status = "warning"
        else:
            usage_status = "ok"

        if os.path.isdir(fs_path):
            await self._render_dir(sub_path, fs_path, usage, computing)
        elif os.path.isfile(fs_path):
            self._render_file(sub_path, fs_path, usage, computing)
        else:
            raise web.HTTPError(404, "File not found")

    async def post(self, sub_path: str = ""):
        # Auth: same as GET — JupyterHub proxy authenticates the user.
        # The ?confirm=yes parameter prevents accidental deletes.
        if self.get_argument("confirm", "") != "yes":
            raise web.HTTPError(400, "Confirmation required")

        try:
            fs_path = _resolve_safe(HOME_DIR, sub_path)
        except ValueError as e:
            raise web.HTTPError(403, str(e))

        home_real = os.path.realpath(HOME_DIR)
        if fs_path == home_real:
            raise web.HTTPError(403, "Cannot delete your home directory")

        if not os.path.exists(fs_path):
            raise web.HTTPError(404, "File not found")

        try:
            if os.path.isdir(fs_path):
                await asyncio.to_thread(shutil.rmtree, fs_path)
            else:
                await asyncio.to_thread(os.remove, fs_path)
            logger.info("Deleted: %s", sub_path)
        except OSError as e:
            raise web.HTTPError(500, f"Delete failed: {e}")

        # Report fresh usage to hub so cache is updated immediately.
        # This prevents the "deleted files but still can't spawn" trap:
        # without this, the cache stays stale until the next 30-min poll.
        await self._report_usage_to_hub()

        parent = os.path.dirname(sub_path.rstrip("/"))
        self.redirect(self._cleanup_url(f"{quote(parent)}/"))

    # ── Download ────────────────────────────────────────────────────────

    async def _handle_download(self, sub_path: str):
        try:
            fs_path = _resolve_safe(HOME_DIR, sub_path)
        except ValueError as e:
            raise web.HTTPError(403, str(e))

        fmt = self.get_argument("format", "zip")  # zip (default) or tgz

        if os.path.isfile(fs_path):
            await self._send_file(fs_path)
        elif os.path.isdir(fs_path):
            if fmt == "tgz":
                await self._send_tar(fs_path)
            else:
                await self._send_zip(fs_path)
        else:
            raise web.HTTPError(404, "Not found")

    async def _send_file(self, fs_path: str):
        filename = os.path.basename(fs_path)
        st = os.stat(fs_path)
        self.set_header("Content-Type", "application/octet-stream")
        self.set_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.set_header("Content-Length", str(st.st_size))

        def _read():
            with open(fs_path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    yield chunk

        for chunk in _read():
            self.write(chunk)
            await self.flush()

    async def _send_zip(self, fs_path: str):
        """Stream a directory as a .zip file. Uses a temporary file to
        avoid holding the entire archive in memory (pod has 256Mi limit).
        """
        import tempfile
        dirname = os.path.basename(fs_path) or "download"
        zip_path = await asyncio.to_thread(self._make_zip, fs_path, dirname)
        try:
            st = os.stat(zip_path)
            self.set_header("Content-Type", "application/zip")
            self.set_header("Content-Disposition", f'attachment; filename="{dirname}.zip"')
            self.set_header("Content-Length", str(st.st_size))

            def _read():
                with open(zip_path, "rb") as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk:
                            break
                        yield chunk

            for chunk in _read():
                self.write(chunk)
                await self.flush()
        finally:
            await asyncio.to_thread(os.remove, zip_path)

    @staticmethod
    def _make_zip(fs_path: str, dirname: str) -> str:
        import zipfile
        fd, tmp = tempfile.mkstemp(suffix=".zip")
        os.close(fd)
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(fs_path):
                for name in files:
                    full = os.path.join(root, name)
                    arcname = os.path.join(dirname, os.path.relpath(full, fs_path))
                    zf.write(full, arcname)
                # Add empty dirs too
                for name in dirs:
                    full = os.path.join(root, name)
                    if not os.listdir(full):
                        arcname = os.path.join(dirname, os.path.relpath(full, fs_path))
                        zf.write(full + "/", arcname + "/")
        return tmp

    async def _send_tar(self, fs_path: str):
        """Stream a directory as a .tar.gz file. Uses a temporary file to
        avoid holding the entire archive in memory (pod has 256Mi limit).
        """
        dirname = os.path.basename(fs_path) or "download"

        def _make_tar():
            import tempfile, tarfile
            fd, tmp = tempfile.mkstemp(suffix=".tar.gz")
            os.close(fd)
            with tarfile.open(tmp, "w:gz") as tar:
                tar.add(fs_path, arcname=dirname)
            return tmp

        tar_path = await asyncio.to_thread(_make_tar)
        try:
            st = os.stat(tar_path)
            self.set_header("Content-Type", "application/gzip")
            self.set_header("Content-Disposition", f'attachment; filename="{dirname}.tar.gz"')
            self.set_header("Content-Length", str(st.st_size))

            def _read():
                with open(tar_path, "rb") as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk:
                            break
                        yield chunk

            for chunk in _read():
                self.write(chunk)
                await self.flush()
        finally:
            await asyncio.to_thread(os.remove, tar_path)

    # ── Rendering ───────────────────────────────────────────────────────

    async def _render_dir(self, sub_path: str, fs_path: str,
                          usage: "_UsageCache", computing: bool):
        """Render a directory listing from the du cache (never blocks on du).

        File sizes come from the background du cache when available.
        While the cache is still computing (or went stale after a delete)
        the size cells show "…" and the page auto-refreshes every few
        seconds until the du lands.
        """
        entries = await asyncio.to_thread(self._list_dir_sync, fs_path)
        at_root = not sub_path.strip("/")
        sizes = usage.dir_sizes if at_root else {}
        rows = []
        for e in entries:
            n = html.escape(e["name"])
            link = self._cleanup_url(quote(os.path.join(sub_path, e["name"])))
            if e["is_dir"]:
                sz = sizes.get(e["name"]) if at_root else None
            else:
                sz = e["size"]
            sz_str = "…" if (e["is_dir"] and sz is None) else _fmt_bytes(sz if sz is not None else e["size"])
            mt = time.strftime("%Y-%m-%d %H:%M", time.localtime(e["mtime"]))
            ic = "📁" if e["is_dir"] else "📄"
            if e["is_dir"]:
                dl = (
                    f"<a href='{link}?download=1&format=zip' title='Download as .zip'>📦</a> "
                    f"<a href='{link}?download=1&format=tgz' title='Download as .tar.gz'>🗜️</a>"
                )
            else:
                dl = f"<a href='{link}?download=1'>⬇️</a>"
            rows.append(
                f"<tr><td>{ic}</td><td><a href='{link}'>{n}</a></td>"
                f"<td>{sz_str}</td><td style='color:#888'>{mt}</td>"
                f"<td>{dl} "
                f"<form method='POST' action='{link}' style='display:inline' "
                f"onsubmit=\"return confirm('Delete {n}?')\">"
                f"<input type='hidden' name='confirm' value='yes'>"
                f"<button type='submit' class='del'>🗑️</button></form></td></tr>"
            )
        rows_html = "\n".join(rows) if rows else (
            '<tr><td colspan=5 style="text-align:center;color:#888;padding:40px">'
            "Empty directory</td></tr>"
        )
        total = usage.total if usage.total is not None else 0
        pct = (total / HARD_LIMIT_BYTES * 100) if HARD_LIMIT_BYTES else 0
        # hard_limit 0 = no limit (standalone): never over_limit.
        if HARD_LIMIT_BYTES > 0 and total > HARD_LIMIT_BYTES:
            usage_status = "over_limit"
        elif total > WARN_LIMIT_BYTES:
            usage_status = "warning"
        else:
            usage_status = "ok"
        color = "#e84855" if usage_status == "over_limit" else (
            "#f0a000" if usage_status == "warning" else "#4caf50")
        icon = "🚫" if usage_status == "over_limit" else (
            "⚠️" if usage_status == "warning" else "✅")

        # Show the student's actual limits, not confusing totals
        limit_str = _fmt_bytes(HARD_LIMIT_BYTES) if HARD_LIMIT_BYTES else "no limit"
        pct_str = f"({pct:.1f}%)" if HARD_LIMIT_BYTES else ""
        total_str = _fmt_bytes(total) if not computing else "…"
        if usage_status == "over_limit" and not computing:
            over_by = total - HARD_LIMIT_BYTES
            banner = (
                f'<div class="banner over">{icon} <b>Storage limit exceeded.</b><br>'
                f'You are using <b>{_fmt_bytes(total)}</b> of your '
                f'<b>{_fmt_bytes(HARD_LIMIT_BYTES)}</b> limit '
                f'(<b>{pct:.0f}%</b> — {over_by // (1024*1024)} MiB over).<br>'
                f'Delete files below to get under <b>{_fmt_bytes(HARD_LIMIT_BYTES)}</b>, '
                f'then restart your server from JupyterHub.'
            )
        elif usage_status == "warning" and not computing:
            banner = (
                f'<div class="banner warn">{icon} <b>Approaching storage limit.</b><br>'
                f'You are using <b>{_fmt_bytes(total)}</b> of your '
                f'<b>{_fmt_bytes(HARD_LIMIT_BYTES)}</b> limit (<b>{pct:.0f}%</b>).'
            )
        else:
            banner = ""
        if computing:
            banner = (
                f'<div class="banner warn">⏳ <b>Computing storage sizes…</b><br>'
                'Your home directory is large; sizes load in the background '
                'and this page refreshes automatically.'
            )

        crumbs = self._breadcrumbs(sub_path)
        hub_url = os.environ.get("JUPYTERHUB_BASE_URL", "/")
        # Auto-refresh the page while the background du is still running,
        # so the student never sees a frozen "computing" screen.
        meta_refresh = (
            f'<meta http-equiv="refresh" content="{USAGE_CACHE_REFRESH_SEC}">'
            if not computing else
            '<meta http-equiv="refresh" content="5">'
        )
        self.set_header("Content-Type", "text/html")
        self.write(_PAGE_CSS + meta_refresh + f"""
<div class="hdr"><h1>💾 Storage Cleanup</h1><a href="{hub_url}home">← JupyterHub</a></div>
<div class="ctn">{banner}</div>
<div class="bar-container">
  <div style="font-size:13px;margin-bottom:4px">
    <b>{total_str}</b> used / <b>{limit_str}</b> {pct_str} · warn at {_fmt_bytes(WARN_LIMIT_BYTES)}
  </div>
  <div class="bar"><div style="width:{min(pct,100):.1f}%;background:{color}"></div></div>
</div>
<div class="ctn">
<div class="crumbs">{crumbs}</div>
<table><thead><tr><th></th><th>Name</th><th>Size</th><th>Modified</th><th>Actions</th></tr></thead>
<tbody>{rows_html}</tbody></table>
<div class="ft">Browse · Delete · Download — no upload</div>
</div></body></html>""")

    @staticmethod
    def _list_dir_sync(fs_path: str) -> list[dict]:
        """Cheap directory listing: os.listdir + stat, NO recursive du.

        Directory sizes are filled in from the du cache by the caller,
        so this stays fast even on large NFS homes.
        """
        entries = []
        try:
            for name in os.listdir(fs_path):
                full = os.path.join(fs_path, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                is_dir = stat.S_ISDIR(st.st_mode)
                entries.append({
                    "name": name,
                    "size": st.st_size,
                    "is_dir": is_dir,
                    "mtime": st.st_mtime,
                })
        except OSError:
            pass
        # Directories sort first (by name) so the "biggest" listing isn't
        # gated on sizes that may still be computing; stable and predictable.
        entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        return entries

    def _render_file(self, sub_path: str, fs_path: str,
                     usage: "_UsageCache", computing: bool):
        st = os.stat(fs_path)
        name = html.escape(os.path.basename(fs_path))
        size = _fmt_bytes(st.st_size)
        mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))
        crumbs = self._breadcrumbs(sub_path)
        dl_url = self._cleanup_url(f"{quote(sub_path)}?download=1")
        del_form = (
            f"<form method='POST' action='{self._cleanup_url(quote(sub_path))}' "
            f"onsubmit=\"return confirm('Delete {name}?')\">"
            "<input type='hidden' name='confirm' value='yes'>"
            "<button type='submit' class='del'>🗑️ Delete</button></form>"
        )
        self.set_header("Content-Type", "text/html")
        self.write(_PAGE_CSS + f"""
<div class="crumbs">{crumbs}</div>
<div class="card"><h2>📄 {name}</h2>
<p>Size: {size}</p><p>Modified: {mtime}</p>
<p style="margin-top:16px"><a href="{dl_url}" class="dl">⬇️ Download</a> {del_form}</p>
</div></body></html>""")

    def _breadcrumbs(self, sub_path: str) -> str:
        parts = [f'<a href="{self._cleanup_url()}">🏠 Home</a>']
        if sub_path:
            segs = sub_path.strip("/").split("/")
            acc = ""
            for i, s in enumerate(segs):
                acc = os.path.join(acc, s) if acc else s
                if i < len(segs) - 1:
                    parts.append(f'<a href="{self._cleanup_url(f"{quote(acc)}/")}">{html.escape(s)}</a>')
                else:
                    parts.append(f"<span>{html.escape(s)}</span>")
        return " / ".join(parts)


# ── Shared CSS ──────────────────────────────────────────────────────────

_PAGE_CSS = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Storage Cleanup</title><style>
*{box-sizing:border-box}
body{font-family:system-ui,sans-serif;margin:0;background:#f5f5f5;color:#333}
.hdr{background:#2c3e50;color:#fff;padding:10px 24px;display:flex;justify-content:space-between;align-items:center}
.hdr h1{margin:0;font-size:16px}
.hdr a{color:#3498db;text-decoration:none}
.ctn{max-width:900px;margin:16px auto;padding:0 12px}
.banner{padding:10px 14px;border-radius:6px;margin-bottom:12px;font-size:13px}
.over{background:#ffe0e0;border-left:4px solid #e84855}
.warn{background:#fff8e0;border-left:4px solid #f0a000}
.bar{background:#e0e0e0;border-radius:4px;height:16px;margin:4px 0 12px;overflow:hidden}
.bar>div{height:100%;border-radius:4px}
.bar-container{max-width:900px;margin:12px auto;padding:0 12px}
.crumb{margin-bottom:8px;font-size:13px;color:#666}
.crumbs a{color:#3498db;text-decoration:none}
table{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.1)}
th{background:#f0f0f0;padding:8px 12px;text-align:left;font-size:11px;color:#555;text-transform:uppercase}
td{padding:6px 12px;border-top:1px solid #eee;font-size:13px}
tr:hover{background:#f8f8f8}
td a{color:#3498db;text-decoration:none}
.del{background:none;border:1px solid #e84855;color:#e84855;padding:2px 6px;border-radius:3px;cursor:pointer;font-size:11px}
.del:hover{background:#e84855;color:#fff}
.card{background:#fff;padding:16px;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,.1);max-width:400px}
.dl{background:#3498db;color:#fff;text-decoration:none;padding:6px 14px;border-radius:4px;display:inline-block}
.ft{text-align:center;margin-top:16px;color:#888;font-size:11px}
</style></head><body>"""


# ── Registration ────────────────────────────────────────────────────────

def setup_cleanup_handlers(server_app):
    """Register cleanup portal handlers. Called when JSU_CLEANUP_MODE=1.

    In cleanup mode, the portal takes over ALL URLs:
      GET  /               → redirect to /cleanup/
      GET  /cleanup/...    → browse / download
      POST /cleanup/...    → delete
      ANY  /<anything else> → redirect to /cleanup/ (blocks /lab/, /api/, etc.)

    We insert our handlers at the BEGINNING of the main rule group
    in Tornado's router, so they're matched before Jupyter Server's
    default handlers (Template404, MainHandler, TrailingSlashHandler).
    """
    from tornado.web import URLSpec
    import tornado.web

    web_app = server_app.web_app
    base = web_app.settings["base_url"]

    # Our handler specs — inserted at position 0 of the main rule group
    # Routes are prefixed with the server's base_url (e.g. /cs1302_edb/user/student0/)
    cleanup_specs = [
        # Root → redirect to cleanup
        URLSpec(f'^{base}$', tornado.web.RedirectHandler,
                {'url': f'{base}cleanup/'}),
        # Cleanup routes (must be before the catch-all)
        URLSpec(f'^{base}cleanup/$', CleanupHandler),
        URLSpec(f'^{base}cleanup$', CleanupHandler),
        URLSpec(f'^{base}cleanup/(.+)$', CleanupHandler),
        # Catch-all: redirect everything else to /cleanup/
        # This blocks /lab/, /api/, /terminals/, /notebooks/, etc.
        URLSpec(f'^{base}.*$', tornado.web.RedirectHandler,
                {'url': f'{base}cleanup/'}),
    ]

    # Find the main rule group (the one with the most rules — typically ~39)
    # and insert our specs at the beginning
    router = web_app.default_router
    main_group = None
    for rg in router.rules:
        if hasattr(rg, 'target') and hasattr(rg.target, 'rules') and len(rg.target.rules) > 10:
            main_group = rg.target
            break

    if main_group:
        for i, spec in enumerate(cleanup_specs):
            main_group.rules.insert(i, spec)
    else:
        # Fallback
        web_app.add_handlers(".*$", [
            (r'^/cleanup/$', CleanupHandler),
            (r'^/cleanup$', CleanupHandler),
            (r'^/cleanup/(.+)$', CleanupHandler),
            (r'^/.*$', tornado.web.RedirectHandler, {'url': '/cleanup/'}),
        ])

    server_app.log.info("jupyter-storage-usage: cleanup portal at /cleanup/ (all other routes blocked)")
