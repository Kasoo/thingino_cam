"""Test setup: the integration is importable as `custom_components.thingino_cam` (the repo root goes on
sys.path), and the fake RTSP server as `fake_rtsp`."""
import datetime
import os
import ssl
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _key_pem(key) -> bytes:
    from cryptography.hazmat.primitives import serialization
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def _build(path, name: str, issuer=None, ca: bool = False):
    """Write cert.pem and key.pem for a new EC cert to `path`: self-signed, or signed by (cert, key)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    issuer_cert, issuer_key = issuer or (None, key)
    now = datetime.datetime.now(datetime.UTC)
    builder = (x509.CertificateBuilder().subject_name(subject)
               .issuer_name(issuer_cert.subject if issuer_cert else subject).public_key(key.public_key())
               .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
               .not_valid_after(now + datetime.timedelta(days=1)))
    # key identifiers: Python's default VERIFY_X509_STRICT wants them on CA-issued certs
    builder = builder.add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
    builder = builder.add_extension(
        x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()), critical=False)
    if ca:
        builder = (builder.add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                   .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                                content_commitment=False, key_encipherment=False,
                                                data_encipherment=False, key_agreement=False,
                                                encipher_only=False, decipher_only=False), critical=True))
    else:
        builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
    cert = builder.sign(issuer_key, hashes.SHA256())
    path.mkdir(parents=True, exist_ok=True)
    (path / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (path / "key.pem").write_bytes(_key_pem(key))
    return cert, key


def make_ca(path):
    """(cert, key) of a new CA."""
    return _build(path, "Test CA", ca=True)


def make_cert(path, name: str = "camera.local", issuer=None) -> tuple[ssl.SSLContext, str]:
    """(server context, PEM) for a new cert: self-signed, as stock Thingino has, or signed by `issuer`."""
    _build(path, name, issuer)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(path / "cert.pem", path / "key.pem")
    return ctx, (path / "cert.pem").read_text()


@pytest.fixture
def self_signed(tmp_path):
    return make_cert(tmp_path / "camera")
