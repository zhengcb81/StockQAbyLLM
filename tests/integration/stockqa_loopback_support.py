"""Shared TCP listener for the isolated native StockQA CLI tests."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Iterator


@contextmanager
def serving_loopback(handler: type[BaseHTTPRequestHandler]) -> Iterator[int]:
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
        worker.start()
        try:
            yield server.server_port
        finally:
            server.shutdown()
            worker.join(timeout=5)
            assert not worker.is_alive()
