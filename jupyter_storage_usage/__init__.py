"""jupyter-storage-usage: Jupyter server extension for monitoring home-directory storage usage."""

from ._version import __version__


def _load_jupyter_server_extension(server_app):
    """Entry point for jupyter_server extension loading.

    Called automatically by jupyter_server when the extension is enabled
    via the jupyter_server_config.d JSON file.
    """
    from .handlers import setup_handlers
    setup_handlers(server_app)

    # Start the periodic monitor after the server's event loop is running.
    # During extension loading the loop may not be active yet, so we defer
    # using tornado's IOLoop.add_callback which schedules on the next tick.
    from tornado.ioloop import IOLoop
    from .monitor import StorageMonitor

    server_app.log.info("jupyter-storage-usage: scheduling monitor start...")

    def _start_monitor():
        import asyncio
        server_app.log.info("jupyter-storage-usage: _start_monitor callback fired")
        monitor = StorageMonitor()
        # Keep a reference on server_app so the monitor isn't garbage-collected
        # when this callback returns (which would cancel its asyncio task).
        server_app._jsu_monitor = monitor
        try:
            # Get the running asyncio loop from Tornado's IOLoop.
            # In Python 3.12+, asyncio.get_event_loop() is deprecated and
            # fails with "no current event loop" if called outside a coroutine.
            # Tornado's IOLoop.current() wraps an asyncio loop — we access it
            # via the AsyncIOMainLoop which Tornado sets up during server start.
            from tornado.platform.asyncio import AsyncIOMainLoop
            ioloop = IOLoop.current()
            # Tornado 6.x: IOLoop.current() returns an AsyncIOMainLoop
            # whose underlying asyncio loop is accessible via .asyncio_loop
            if hasattr(ioloop, 'asyncio_loop'):
                loop = ioloop.asyncio_loop
            else:
                # Fallback for older Tornado or non-asyncio IOLoop
                loop = asyncio.get_event_loop()
            task = loop.create_task(monitor.start_periodic())
            server_app.log.info("jupyter-storage-usage: monitor task created")

            # Add done callback to catch any errors
            def _on_done(t):
                if t.exception():
                    server_app.log.error("jupyter-storage-usage: monitor task crashed: %s", t.exception())
                else:
                    server_app.log.info("jupyter-storage-usage: monitor task completed")

            task.add_done_callback(_on_done)
        except Exception as e:
            server_app.log.error("jupyter-storage-usage: monitor start failed: %s", e)

    IOLoop.current().add_callback(_start_monitor)


__all__ = ["__version__"]
