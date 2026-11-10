"""Self-check tests verifying outbound network calls fail closed under pytest."""

from __future__ import annotations

import http.client
import socket
import urllib.request

import aiohttp
import pytest

from tests.helpers import OfflineNetworkDisabledError

pytestmark = pytest.mark.unit


def test_raw_inet_socket_connect_is_blocked():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(OfflineNetworkDisabledError):
            sock.connect(("192.0.2.1", 80))
    finally:
        sock.close()


def test_urllib_and_http_client_are_blocked():
    with pytest.raises(OfflineNetworkDisabledError):
        urllib.request.urlopen("https://example.invalid/")

    conn = http.client.HTTPSConnection("example.invalid")
    with pytest.raises(OfflineNetworkDisabledError):
        conn.request("GET", "/")


async def test_unmocked_aiohttp_request_is_blocked():
    async with aiohttp.ClientSession() as session:
        with pytest.raises(OfflineNetworkDisabledError):
            async with session.get("https://example.invalid/unmocked"):
                pass

