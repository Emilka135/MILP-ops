"""Contract tests may execute local sinks, never remote MIS/LLM endpoints."""
import ipaddress
import socket

import pytest


@pytest.fixture(autouse=True)
def local_contract_network_only(monkeypatch):
    original = socket.socket.connect
    original_ex = socket.socket.connect_ex

    def permitted(address):
        if not isinstance(address, tuple):
            return  # Local Unix sockets are not remote services.
        host = address[0]
        try:
            allowed = ipaddress.ip_address(host).is_loopback
        except ValueError:
            allowed = host == 'localhost'
        if not allowed:
            raise AssertionError('CONTRACT_EXTERNAL_NETWORK_BLOCKED: '+str(host))

    def connect(sock, address):
        permitted(address)
        return original(sock, address)

    def connect_ex(sock, address):
        permitted(address)
        return original_ex(sock, address)

    monkeypatch.setattr(socket.socket, 'connect', connect)
    monkeypatch.setattr(socket.socket, 'connect_ex', connect_ex)
