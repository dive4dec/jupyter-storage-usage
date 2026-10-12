"""HTTP handlers for the storage usage API + cleanup portal dispatch.

In normal mode (JSU_CLEANUP_MODE=0):
  - Registers /api/storage/v1/usage endpoint
  - Injects status bar widget JS

In cleanup mode (JSU_CLEANUP_MODE=1):
  - Takes over the root URL with the cleanup portal
  - No JupyterLab, no API — just the file manager
"""

from __future__ import annotations

import os

from jupyter_server.base.handlers import APIHandler
from jupyter_server.extension.handler import ExtensionHandlerMixin
from tornado import web

from .config import CLEANUP_MODE
from .monitor import StorageMonitor


class StorageUsageHandler(APIHandler):
    """GET /api/storage/v1/usage — returns current storage usage.

    Auth is handled by JupyterHub's OAuth2 proxy — only the pod owner
    can reach this endpoint.
    """

    async def get(self):
        monitor = StorageMonitor()
        data = await monitor.get_usage()
        if data is None:
            self.set_status(200)
            self.write("{}")  # Cache not ready yet — widget shows "loading…"
        else:
            self.write(data.to_dict())


def _inject_statusbar_widget(server_app):
    """Inject the status bar JS into every JupyterLab page.

    Wraps LabHandler.get() to capture the HTML, inject a <script> tag,
    then write the modified HTML.

    The JS URL includes an MD5 hash so browsers bust cache automatically
    when the file content changes — no manual hard-refresh needed.
    """
    import hashlib
    import os
    from jupyter_server.base.handlers import FileFindHandler

    static_dir = os.path.join(os.path.dirname(__file__), "static")
    base = server_app.web_app.settings["base_url"]

    # Serve the JS file
    server_app.web_app.add_handlers(
        ".*$",
        [
            (
                base + r"static/jupyter_storage_usage/(.*)",
                FileFindHandler,
                {"path": [static_dir]},
            ),
        ],
    )

    # Compute content hash so the URL changes when the JS changes.
    # This prevents browsers from serving stale cached JS after upgrades.
    js_path = os.path.join(static_dir, "storage_usage.js")
    with open(js_path, "rb") as f:
        js_hash = hashlib.md5(f.read()).hexdigest()[:8]

    static_url = f"{base}static/jupyter_storage_usage/storage_usage.js?v={js_hash}"
    script_tag = f'<script src="{static_url}" defer></script>'

    # Wrap LabHandler.get() to inject script into HTML
    try:
        from jupyterlab_server.handlers import LabHandler
    except ImportError:
        server_app.log.warning("jupyter-storage-usage: JupyterLab not found, skipping widget injection")
        return

    original_get = LabHandler.get

    def wrapped_get(self, *args, **kwargs):
        captured = []
        orig_write = self.write

        def capturing_write(chunk):
            captured.append(str(chunk))

        self.write = capturing_write
        try:
            original_get(self, *args, **kwargs)
        finally:
            self.write = orig_write

        # Don't modify response if it's already finished (e.g. redirect, error)
        if self._finished:
            return
        html_str = "".join(captured)
        if html_str and "storage_usage.js" not in html_str and "</head>" in html_str:
            html_str = html_str.replace("</head>", f"{script_tag}\n</head>", 1)
            orig_write(html_str)

    LabHandler.get = wrapped_get
    server_app.log.info("jupyter-storage-usage: injecting status bar widget at %s", static_url)


def setup_handlers(server_app):
    """Register handlers — dispatches based on CLEANUP_MODE."""
    import asyncio

    if CLEANUP_MODE:
        # Cleanup mode: take over with the cleanup portal
        from .cleanup import setup_cleanup_handlers
        setup_cleanup_handlers(server_app)
        server_app.log.info("jupyter-storage-usage: running in CLEANUP mode")
    else:
        # Normal mode: API endpoint + status bar widget
        web_app = server_app.web_app
        base = web_app.settings["base_url"]
        route = f"{base}api/storage/v1/usage"

        web_app.add_handlers(
            ".*$",
            [(route, StorageUsageHandler)],
        )

        _inject_statusbar_widget(server_app)

        # NOTE: periodic monitor is started in __init__.py via IOLoop.add_callback
        # to ensure the event loop is running when the task is created.

        server_app.log.info(
            "jupyter-storage-usage: registered /api/storage/v1/usage"
        )
