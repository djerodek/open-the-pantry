"""Claude review round 7 (0070-0077).

Starting either extra listener (What to eat?, the app's own HTTPS) silenced
the main port's request log. uvicorn's loggers are shared by every server in
the process, and each listener's uvicorn.Config re-applied uvicorn's logging
setup with log_level="warning": "uvicorn.access" dropped to WARNING, so the
main port logged no more requests, even after the listener stopped.
"""
import logging

import pytest

UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")


@pytest.fixture(autouse=True)
def _data_dir(data_dir):
    """The app reads its data folder when first imported; set it first even
    when this file runs on its own."""


def _snapshot():
    out = {}
    for name in UVICORN_LOGGERS:
        lg = logging.getLogger(name)
        out[name] = (lg.level, list(lg.handlers), lg.propagate, lg.disabled)
    return out


@pytest.fixture
def main_port_logging():
    """uvicorn's loggers as the main server sets them up (`uvicorn app.main:app`
    at the default info level), restored afterwards."""
    import uvicorn
    saved = {n: (logging.getLogger(n).level, list(logging.getLogger(n).handlers),
                 logging.getLogger(n).propagate, logging.getLogger(n).disabled) for n in UVICORN_LOGGERS}
    uvicorn.Config("app.main:app", log_level="info")      # what the main server's Config does
    assert logging.getLogger("uvicorn.access").isEnabledFor(logging.INFO)
    yield
    for n, (level, handlers, propagate, disabled) in saved.items():
        lg = logging.getLogger(n)
        lg.setLevel(level)
        lg.handlers = handlers
        lg.propagate = propagate
        lg.disabled = disabled


@pytest.mark.parametrize("module", ["picker", "https_setup"])
def test_extra_listener_leaves_the_main_ports_logging_alone(main_port_logging, module):
    import importlib
    from fastapi import FastAPI
    mod = importlib.import_module(f"app.{module}")
    before = _snapshot()
    mod._make_server(FastAPI())
    assert _snapshot() == before
    assert logging.getLogger("uvicorn.access").isEnabledFor(logging.INFO)
    assert logging.getLogger("uvicorn.access").handlers      # access_log=False would empty them


def test_switching_the_picker_on_and_off_keeps_the_request_log(client, main_port_logging, monkeypatch):
    """The real switch, with a real listener."""
    import socket
    import urllib.request
    from app import picker
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setattr(picker, "LISTEN_PORT", port)
    monkeypatch.setattr(picker, "PICKER_PUBLIC_PORT", port)
    before = _snapshot()
    try:
        assert client.put("/api/picker/settings", json={"enabled": True}).json()["enabled"] is True
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as r:
            assert r.status == 200
        assert _snapshot() == before
    finally:
        client.put("/api/picker/settings", json={"enabled": False})
    assert _snapshot() == before
    assert logging.getLogger("uvicorn.access").isEnabledFor(logging.INFO)
