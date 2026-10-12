"""TLS trust for a pinned camera certificate (api.py)."""
import asyncio
import ssl

import pytest

from custom_components.thingino_cam.api import build_connection, pinned_context
from custom_components.thingino_cam.const import CONF_MODE, CONF_SERVER_CERT, CONF_TOKEN, MODE_DIRECT
from conftest import make_ca, make_cert


@pytest.fixture(autouse=True)
def sockets_on(socket_enabled):
    yield


async def handshake(server_ctx, client_ctx):
    """Complete a TLS handshake between the two contexts on a loopback port."""
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0, ssl=server_ctx)
    port = server.sockets[0].getsockname()[1]
    try:
        _, writer = await asyncio.open_connection("127.0.0.1", port, ssl=client_ctx, server_hostname="127.0.0.1")
        writer.close()
    finally:
        server.close()
        await server.wait_closed()


@pytest.fixture
def public_cert(tmp_path, monkeypatch):
    """Server context for a cert issued by a CA in the "system" store (SSL_CERT_FILE), for another name."""
    ca, ca_key = make_ca(tmp_path / "ca")
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "ca" / "cert.pem"))
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    return make_cert(tmp_path / "public", "www.example.com", issuer=(ca, ca_key))[0]


async def test_pinned_refuses_publicly_trusted_cert(self_signed, public_cert):
    system = ssl.create_default_context()           # the fake CA is trusted as a system CA
    system.check_hostname = False
    await handshake(public_cert, system)
    with pytest.raises(ssl.SSLCertVerificationError):
        await handshake(public_cert, pinned_context(self_signed[1]))


async def test_build_connection_uses_pinned_context(self_signed, public_cert):
    conn = build_connection({"host": "192.0.2.1", CONF_MODE: MODE_DIRECT, CONF_TOKEN: "t",
                             CONF_SERVER_CERT: self_signed[1]}, lambda p: p)
    assert not conn.ssl_context.check_hostname
    await handshake(self_signed[0], conn.ssl_context)
    with pytest.raises(ssl.SSLCertVerificationError):
        await handshake(public_cert, conn.ssl_context)


async def test_pinned_accepts_pinned_cert_under_any_name(self_signed):
    await handshake(self_signed[0], pinned_context(self_signed[1]))


async def test_pinned_refuses_another_cert(self_signed, tmp_path):
    other, _ = make_cert(tmp_path / "other", "camera.local")
    with pytest.raises(ssl.SSLCertVerificationError):
        await handshake(other, pinned_context(self_signed[1]))
