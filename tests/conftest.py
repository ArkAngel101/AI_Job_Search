"""Tests must never touch the live network: refuse any connection or DNS lookup that is not loopback."""
import socket
from collections.abc import Iterator
from typing import Any

import pytest

LOCAL = {"127.0.0.1", "::1", "localhost", "0.0.0.0", ""}


@pytest.fixture(autouse=True)
def no_live_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo

    def guarded_connect(self: socket.socket, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else address
        if self.family != socket.AF_UNIX and str(host) not in LOCAL:
            raise RuntimeError(f"test tried to connect to {host}: tests must not use the network")
        return real_connect(self, address)

    def guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host is not None and str(host) not in LOCAL:
            raise RuntimeError(f"test tried to resolve {host}: tests must not use the network")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    yield
