import os
import ssl
from base64 import standard_b64encode
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from subnet_tracker.types import Timestamp

CA_KEY = "agent-ca.key"
CA_CERT = "agent-ca.pem"
SERVER_KEY = "server.key"
SERVER_CERT = "server.pem"
CA_CN = "subnet-tracker agent CA"
SERVER_CN = "subnet-tracker agent api"
CERT_LIFETIME = 10 * 365 * 86400
_DIR_MODE = 0o700
_KEY_MODE = 0o600
_CERT_MODE = 0o644
_FILES = (CA_KEY, CA_CERT, SERVER_KEY, SERVER_CERT)
_Extension = tuple[x509.ExtensionType, bool]


class PkiError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Authority:
    directory: Path
    ca_cert_pem: str
    ca_key_pem: str = field(repr=False)
    server_cert_pem: str
    server_spki_sha256: str
    context: ssl.SSLContext = field(compare=False, hash=False, repr=False)


def init_pki(directory: Path, now: Timestamp) -> Authority:
    if any((directory / name).exists() for name in _FILES):
        raise PkiError("pki files already exist")
    created = not directory.exists()
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    try:
        os.chmod(directory, _DIR_MODE)
        ca_key = _private_key()
        ca_cert = _signed(_common_name(CA_CN), ca_key.public_key(), ca_key, _common_name(CA_CN), now, _ca_extensions(ca_key))
        server_key = _private_key()
        server_cert = _signed(_common_name(SERVER_CN), server_key.public_key(), ca_key, ca_cert.subject, now, _leaf_extensions(ca_key, ExtendedKeyUsageOID.SERVER_AUTH))
        materials = (
            (directory / CA_KEY, _pem_key(ca_key), _KEY_MODE),
            (directory / CA_CERT, _pem_cert(ca_cert), _CERT_MODE),
            (directory / SERVER_KEY, _pem_key(server_key), _KEY_MODE),
            (directory / SERVER_CERT, _pem_cert(server_cert), _CERT_MODE),
        )
        for path, data, file_mode in materials:
            _write_file(path, data, file_mode)
            written.append(path)
        _fsync_directory(directory)
        return load_pki(directory)
    except Exception:
        for path in written:
            path.unlink(missing_ok=True)
        if created:
            directory.rmdir()
        raise


def load_pki(directory: Path) -> Authority:
    missing = [name for name in _FILES if not (directory / name).is_file()]
    if missing:
        raise PkiError(f"pki files are missing: {', '.join(missing)}")
    ca_key_pem = (directory / CA_KEY).read_text(encoding="utf-8")
    ca_cert_pem = (directory / CA_CERT).read_text(encoding="utf-8")
    server_key_pem = (directory / SERVER_KEY).read_text(encoding="utf-8")
    server_cert_pem = (directory / SERVER_CERT).read_text(encoding="utf-8")
    ca_key, server_key = _load_key(ca_key_pem), _load_key(server_key_pem)
    ca_cert, server_cert = _load_cert(ca_cert_pem), _load_cert(server_cert_pem)
    _require_p256(ca_cert)
    _require_p256(server_cert)
    _require_self_signed(ca_cert)
    _require_signed_by(server_cert, ca_cert)
    if not _matches(ca_key, ca_cert):
        raise PkiError("CA certificate does not match its key")
    if not _matches(server_key, server_cert):
        raise PkiError("server certificate does not match its key")
    return Authority(directory, ca_cert_pem, ca_key_pem, server_cert_pem, _spki_sha256(server_key), _server_context(directory))


def _server_context(directory: Path) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = False
    try:
        context.load_verify_locations(cafile=str(directory / CA_CERT))
        context.load_cert_chain(str(directory / SERVER_CERT), str(directory / SERVER_KEY))
        probe = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        probe.load_cert_chain(str(directory / CA_CERT), str(directory / CA_KEY))
    except ssl.SSLError as error:
        raise PkiError("certificate does not match its key") from error
    return context


def _private_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _load_key(pem: str) -> ec.EllipticCurvePrivateKey:
    try:
        key = serialization.load_pem_private_key(pem.encode(), password=None)
    except (ValueError, TypeError) as error:
        raise PkiError("private key is not ECDSA P-256") from error
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise PkiError("private key is not ECDSA P-256")
    return key


def _load_cert(pem: str) -> x509.Certificate:
    try:
        return x509.load_pem_x509_certificate(pem.encode())
    except ValueError as error:
        raise PkiError("certificate is not PEM") from error


def _require_p256(cert: x509.Certificate) -> None:
    public = cert.public_key()
    if not isinstance(public, ec.EllipticCurvePublicKey) or not isinstance(public.curve, ec.SECP256R1):
        raise PkiError("certificate is not ECDSA P-256")


def _require_self_signed(cert: x509.Certificate) -> None:
    try:
        cert.verify_directly_issued_by(cert)
    except (InvalidSignature, ValueError, TypeError) as error:
        raise PkiError("CA certificate is not self-signed") from error


def _require_signed_by(cert: x509.Certificate, issuer: x509.Certificate) -> None:
    try:
        cert.verify_directly_issued_by(issuer)
    except (InvalidSignature, ValueError, TypeError) as error:
        raise PkiError("server certificate is not signed by the agent CA") from error


def _matches(key: ec.EllipticCurvePrivateKey, cert: x509.Certificate) -> bool:
    public = cert.public_key()
    return isinstance(public, ec.EllipticCurvePublicKey) and public.public_numbers() == key.public_key().public_numbers()


def _spki_sha256(key: ec.EllipticCurvePrivateKey) -> str:
    der = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return standard_b64encode(sha256(der).digest()).decode()


def _common_name(name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])


def _usage(*, sign: bool, cert_sign: bool, crl_sign: bool) -> x509.KeyUsage:
    return x509.KeyUsage(
        digital_signature=sign,
        content_commitment=False,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=cert_sign,
        crl_sign=crl_sign,
        encipher_only=False,
        decipher_only=False,
    )


def _ca_extensions(key: ec.EllipticCurvePrivateKey) -> tuple[_Extension, ...]:
    return (
        (x509.BasicConstraints(ca=True, path_length=0), True),
        (_usage(sign=False, cert_sign=True, crl_sign=True), True),
        (x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False),
    )


def _leaf_extensions(ca_key: ec.EllipticCurvePrivateKey, usage_oid: x509.ObjectIdentifier) -> tuple[_Extension, ...]:
    return (
        (x509.BasicConstraints(ca=False, path_length=None), True),
        (_usage(sign=True, cert_sign=False, crl_sign=False), True),
        (x509.ExtendedKeyUsage([usage_oid]), False),
        (x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False),
    )


def _signed(
    subject: x509.Name,
    public_key: ec.EllipticCurvePublicKey,
    signer: ec.EllipticCurvePrivateKey,
    issuer: x509.Name,
    now: Timestamp,
    extensions: Sequence[_Extension],
) -> x509.Certificate:
    builder = x509.CertificateBuilder().subject_name(subject).issuer_name(issuer).public_key(public_key).serial_number(x509.random_serial_number())
    builder = builder.not_valid_before(datetime.fromtimestamp(now, UTC)).not_valid_after(datetime.fromtimestamp(now + CERT_LIFETIME, UTC))
    for extension, critical in extensions:
        builder = builder.add_extension(extension, critical)
    return builder.sign(signer, hashes.SHA256())


def _pem_key(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def _pem_cert(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def _write_file(path: Path, data: bytes, file_mode: int) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, file_mode)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written == 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(fd)
        os.chmod(path, file_mode)
    except Exception:
        os.close(fd)
        path.unlink(missing_ok=True)
        raise
    os.close(fd)


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
