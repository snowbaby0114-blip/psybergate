"""Fixtures of the performance tests: the application served over a real socket."""

import socket
import threading
import time
from collections.abc import Iterator

import pytest
import uvicorn
from sqlalchemy import Engine

from app.config import Settings
from app.main import create_app

SERVER_START_SECONDS = 30


@pytest.fixture(scope="module")
def live_server(engine: Engine, database_url: str) -> Iterator[str]:
    """The base URL of the application served by uvicorn in a thread of this process, on a port
    the system chose, over the test database."""
    app = create_app(engine=engine, settings=Settings(database_url=database_url))
    # Bound here and handed to uvicorn already listening, so nothing can take the port between.
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + SERVER_START_SECONDS
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("the server did not start")
        time.sleep(0.01)
    yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    server.should_exit = True
    thread.join(SERVER_START_SECONDS)
    listener.close()
