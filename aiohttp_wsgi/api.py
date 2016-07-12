import asyncio
import os
import aiohttp
from aiohttp.log import access_logger
from aiohttp.web import Application, StaticRoute
from aiohttp_wsgi.wsgi import WSGIHandler, DEFAULTS, HELP
from aiohttp_wsgi.utils import parse_sockname, import_func


class Server:

    def __init__(self, shutdown_timeout, app, handler, server, loop):
        self._shutdown_timeout = shutdown_timeout
        self.app = app
        self._handler = handler
        self._server = server
        self._loop = loop
        self._close_future = None
        self._connector = None
        self._session = None

    @property
    def sockets(self):
        return self._server.sockets

    async def _wait_closed(self):
        await self._server.wait_closed()
        await self.app.shutdown()
        await self._handler.finish_connections(self._shutdown_timeout)
        await self.app.cleanup()

    def close(self):
        if self._close_future is None:
            # Clean up client session.
            if self._session is not None:
                self._session.close()
            if self._connector is not None:
                self._connector.close()
            # Clean up unix sockets.
            for socket in self.sockets:
                host, port = parse_sockname(socket.getsockname())
                if host == "unix":
                    os.unlink(port)
            # Close the server.
            self._server.close()
            self._close_future = self._loop.create_task(self._wait_closed())

    async def wait_closed(self):
        return await asyncio.shield(self._close_future, loop=self._loop)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        self.close()
        await self.wait_closed()

    async def request(self, method, path, **kwargs):
        host, port = parse_sockname(self.sockets[0].getsockname())
        if self._connector is None:
            if host == "unix":
                self._connector = aiohttp.UnixConnector(path=port, loop=self._loop)
            else:
                self._connector = aiohttp.TCPConnector(loop=self._loop)
        if self._session is None:
            self._session = aiohttp.ClientSession(connector=self._connector, loop=self._loop)
        uri = "http://{}:{}{}".format(host, port, path)
        return await self._session.request(method, uri, **kwargs)


def format_path(path):
    assert not path.endswith("/"), "{!r} name should not end with /".format(path)
    if path == "":
        path = "/"
    assert path.startswith("/"), "{!r} name should start with /".format(path)
    return path


async def start_server(
    application,
    *,
    # Server config.
    host=None,
    port=8080,
    # Unix server config.
    unix_socket=None,
    unix_socket_perms=0o600,
    # Prexisting socket config.
    socket=None,
    # Shared server config.
    backlog=1024,
    # aiohttp config.
    routes=(),
    static=(),
    on_finish=(),
    # Asyncio config.
    loop=None,
    # Aiohttp config.
    script_name="",
    access_log=access_logger,
    shutdown_timeout=60.0,
    **kwargs
):
    loop = loop or asyncio.get_event_loop()
    app = Application(
        loop=loop,
    )
    # Add routes.
    for method, path, handler in routes:
        app.router.add_route(method, path, import_func(handler))
    # Add static routes.
    for static_item in static:
        if isinstance(static_item, str):
            assert "=" in static_item, "{!r} should have format 'path=directory'"
            static_item = static_item.split("=", 1)
        path, dirname = static_item
        static_resource = app.router.add_resource("{}/{{filename:.*}}".format(format_path(path)))
        static_resource.add_route("*", StaticRoute(None, path + "/", dirname).handle)
    # Add on finish callbacks.
    for on_finish_callback in on_finish:
        app.on_shutdown.append(import_func(on_finish_callback))
    # Add the wsgi application. This has to be last.
    app.router.add_route(
        "*",
        "{}{{path_info:.*}}".format(format_path(script_name)),
        WSGIHandler(import_func(application), loop=loop, **kwargs),
    )
    handler = app.make_handler(access_log=access_log)
    # Set up the server.
    shared_server_kwargs = {
        "backlog": backlog,
    }
    if unix_socket is not None:
        server = await loop.create_unix_server(
            handler,
            path=unix_socket,
            **shared_server_kwargs
        )
    elif socket is not None:
        server = await loop.create_server(
            handler,
            sock=socket,
            **shared_server_kwargs
        )
    else:
        server = await loop.create_server(
            handler,
            host=host,
            port=port,
            **shared_server_kwargs
        )
    # Set socket permissions.
    if unix_socket is not None:
        os.chmod(unix_socket, unix_socket_perms)
    # All done!
    return Server(shutdown_timeout, app, handler, server, loop)


def serve(application, *, loop=None, **kwargs):  # pragma: no cover
    loop = loop or asyncio.get_event_loop()
    server = loop.run_until_complete(start_server(application, loop=loop, **kwargs))
    server_uri = " ".join(
        "http://{}:{}".format(*parse_sockname(socket.getsockname()))
        for socket
        in server.sockets
    )
    server.app.logger.info("Serving on %s", server_uri)
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.app.logger.debug("Waiting for server to shut down")
        server.close()
        server.app.logger.debug("Waiting for client connections to terminate")
        loop.run_until_complete(server.wait_closed())
        loop.close()
        server.app.logger.info("Stopped serving on %s", server_uri)


DEFAULTS = DEFAULTS.copy()
DEFAULTS.update(start_server.__kwdefaults__)

HELP = HELP.copy()
HELP.update({
    "host": "Host interfaces to bind. Defaults to ``'0.0.0.0'`` and ``'::'``.",
    "port": "Port to bind. Defaults to ``{port!r}``.".format(**DEFAULTS),
    "unix_socket": "Path to a unix socket to bind, cannot be used with ``host``.",
    "unix_socket_perms": (
        "Filesystem permissions to apply to the unix socket. Defaults to ``{unix_socket_perms!r}``."
    ).format(**DEFAULTS),
    "backlog": "Socket connection backlog. Defaults to {backlog!r}.".format(**DEFAULTS),
    "script_name": (
        "URL prefix for the WSGI application, should start with a slash, but not end with a slash. "
        "Defaults to ``{script_name!r}``."
    ).format(**DEFAULTS),
    "shutdown_timeout": (
        "Timeout when closing client connections on server shutdown. Defaults to ``{shutdown_timeout!r}``."
    ).format(**DEFAULTS),
})
