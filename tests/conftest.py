import socket

import pytest


class NetworkAccessInTests(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail any test that opens a real connection.

    The suite must run offline, so this is enforced rather than hoped for.
    """

    def refuse(*args, **kwargs):
        raise NetworkAccessInTests("tests must not touch the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
