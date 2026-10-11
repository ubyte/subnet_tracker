import sqlite3
import ssl
import stat
from base64 import standard_b64decode, standard_b64encode
from hashlib import sha256
from pathlib import Path

import pytest
import subnet_tracker.pki.ca as pki_ca
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from subnet_tracker.pki.ca import CERT_LIFETIME, PkiError, init_pki, load_pki
from subnet_tracker.pki.issue import ClientCertificate, issue_client

from db_rows import insert
from subnet_tracker.types import FieldError, Timestamp

NOW = Timestamp(1_760_000_000)
TEN_YEARS = 10 * 365 * 86400
PKI_FILES = ("agent-ca.key", "agent-ca.pem", "server.key", "server.pem")


@pytest.fixture
def authority(tmp_path: Path) -> pki_ca.Authority:
    return init_pki(tmp_path / "pki", NOW)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def snapshot(directory: Path) -> dict[str, tuple[int, bytes]]:
    return {path.name: (mode(path), path.read_bytes()) for path in directory.iterdir()}


def load_cert(pem: str) -> x509.Certificate:
    return x509.load_pem_x509_certificate(pem.encode())


def common_name(cert: x509.Certificate) -> str:
    value = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    assert isinstance(value, str)
    return value


def assert_p256(cert: x509.Certificate) -> None:
    public = cert.public_key()
    assert isinstance(public, ec.EllipticCurvePublicKey)
    assert isinstance(public.curve, ec.SECP256R1)
    assert cert.signature_hash_algorithm == hashes.SHA256()


def assert_window(cert: x509.Certificate, now: int = NOW) -> None:
    assert int(cert.not_valid_before_utc.timestamp()) == now
    assert int(cert.not_valid_after_utc.timestamp()) == now + TEN_YEARS


def basic(cert: x509.Certificate) -> x509.BasicConstraints:
    extension = cert.extensions.get_extension_for_class(x509.BasicConstraints)
    assert extension.critical
    return extension.value


def usage(cert: x509.Certificate) -> x509.KeyUsage:
    extension = cert.extensions.get_extension_for_class(x509.KeyUsage)
    assert extension.critical
    return extension.value


def extended(cert: x509.Certificate) -> list[x509.ObjectIdentifier]:
    return list(cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value)


def fingerprint(pem: str) -> str:
    return sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()


def pin_of(public: ec.EllipticCurvePublicKey) -> str:
    der = public.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return standard_b64encode(sha256(der).digest()).decode()


def issued(authority: pki_ca.Authority, name: str = "laptop-alex") -> ClientCertificate:
    credential = issue_client(authority, name, NOW)
    assert isinstance(credential, ClientCertificate)
    return credential


def write_pair(directory: Path, credential: ClientCertificate) -> tuple[Path, Path]:
    cert_path, key_path = directory / "client.pem", directory / "client.key"
    cert_path.write_text(credential.cert_pem)
    key_path.write_text(credential.key_pem)
    return cert_path, key_path


def peer_sha256(context: ssl.SSLContext, cert_path: Path | None, key_path: Path | None) -> str:
    client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client.minimum_version = ssl.TLSVersion.TLSv1_3
    client.check_hostname = False
    client.verify_mode = ssl.CERT_NONE
    if cert_path is not None and key_path is not None:
        client.load_cert_chain(cert_path, key_path)
    server_in, server_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    client_in, client_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    server = context.wrap_bio(server_in, server_out, server_side=True)
    peer = client.wrap_bio(client_in, client_out, server_side=False)
    server_done = client_done = False
    for _ in range(40):
        if not client_done:
            try:
                peer.do_handshake()
            except ssl.SSLWantReadError:
                pass
            else:
                client_done = True
        if client_out.pending:
            server_in.write(client_out.read())
        if not server_done:
            try:
                server.do_handshake()
            except ssl.SSLWantReadError:
                pass
            else:
                server_done = True
        if server_out.pending:
            client_in.write(server_out.read())
        if client_done and server_done:
            der = server.getpeercert(binary_form=True)
            assert isinstance(der, bytes)
            return sha256(der).hexdigest()
    raise AssertionError("handshake stalled")


def test_lifetime_is_3650_days() -> None:
    assert CERT_LIFETIME == TEN_YEARS


def test_init_creates_the_directory_and_four_files(tmp_path: Path) -> None:
    directory = tmp_path / "var" / "pki"
    authority = init_pki(directory, NOW)
    assert authority.directory == directory
    assert mode(directory) == 0o700
    assert sorted(path.name for path in directory.iterdir()) == sorted(PKI_FILES)
    assert mode(directory / "agent-ca.key") == mode(directory / "server.key") == 0o600
    assert mode(directory / "agent-ca.pem") == mode(directory / "server.pem") == 0o644
    assert (directory / "agent-ca.pem").read_text() == authority.ca_cert_pem
    assert (directory / "server.pem").read_text() == authority.server_cert_pem
    assert (directory / "agent-ca.key").read_text() == authority.ca_key_pem
    for name in ("agent-ca.key", "server.key"):
        text = (directory / name).read_text()
        assert text.startswith("-----BEGIN PRIVATE KEY-----\n")
        assert "ENCRYPTED" not in text
    assert "PRIVATE KEY" not in repr(authority)


def test_init_tightens_an_existing_directory(tmp_path: Path) -> None:
    directory = tmp_path / "pki"
    directory.mkdir()
    directory.chmod(0o755)
    authority = init_pki(directory, NOW)
    assert authority.directory == directory
    assert mode(directory) == 0o700


def test_ca_and_server_certificates(authority: pki_ca.Authority) -> None:
    ca_cert, server = load_cert(authority.ca_cert_pem), load_cert(authority.server_cert_pem)
    assert_p256(ca_cert)
    assert_p256(server)
    assert_window(ca_cert)
    assert_window(server)
    assert common_name(ca_cert) == "subnet-tracker agent CA"
    assert common_name(server) == "subnet-tracker agent api"
    assert ca_cert.issuer == ca_cert.subject
    assert server.issuer == ca_cert.subject
    ca_cert.verify_directly_issued_by(ca_cert)
    server.verify_directly_issued_by(ca_cert)
    assert basic(ca_cert).ca
    assert basic(ca_cert).path_length == 0
    assert not basic(server).ca
    assert basic(server).path_length is None
    ca_usage, server_usage = usage(ca_cert), usage(server)
    assert ca_usage.key_cert_sign
    assert ca_usage.crl_sign
    assert not ca_usage.digital_signature
    assert server_usage.digital_signature
    assert not server_usage.key_cert_sign
    assert not server_usage.crl_sign
    with pytest.raises(x509.ExtensionNotFound):
        ca_cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
    assert extended(server) == [ExtendedKeyUsageOID.SERVER_AUTH]


def test_server_pin_is_the_spki_sha256(authority: pki_ca.Authority) -> None:
    server = load_cert(authority.server_cert_pem)
    public = server.public_key()
    assert isinstance(public, ec.EllipticCurvePublicKey)
    private = serialization.load_pem_private_key((authority.directory / "server.key").read_bytes(), password=None)
    assert isinstance(private, ec.EllipticCurvePrivateKey)
    assert authority.server_spki_sha256 == pin_of(public) == pin_of(private.public_key())
    assert len(standard_b64decode(authority.server_spki_sha256)) == 32
    assert authority.server_spki_sha256 != fingerprint(authority.server_cert_pem)


def test_load_reads_the_same_authority(authority: pki_ca.Authority) -> None:
    assert load_pki(authority.directory) == authority


def test_loaded_context_requires_tls13_and_a_client_certificate(authority: pki_ca.Authority) -> None:
    context = authority.context
    assert context.protocol == ssl.PROTOCOL_TLS_SERVER
    assert context.minimum_version == ssl.TLSVersion.TLSv1_3
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is False


def test_init_refuses_to_overwrite(authority: pki_ca.Authority) -> None:
    before = snapshot(authority.directory)
    with pytest.raises(PkiError, match="already exist"):
        init_pki(authority.directory, Timestamp(NOW + 1))
    assert snapshot(authority.directory) == before


def test_init_refuses_a_partial_directory_without_touching_it(tmp_path: Path) -> None:
    directory = tmp_path / "pki"
    directory.mkdir()
    directory.chmod(0o755)
    key = directory / "agent-ca.key"
    key.write_bytes(b"keep")
    key.chmod(0o640)
    with pytest.raises(PkiError, match="already exist"):
        init_pki(directory, NOW)
    assert key.read_bytes() == b"keep"
    assert mode(key) == 0o640
    assert mode(directory) == 0o755
    assert sorted(path.name for path in directory.iterdir()) == ["agent-ca.key"]


def test_aborted_init_removes_the_directory_it_created(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = pki_ca._write_file

    def fail(path: Path, data: bytes, file_mode: int) -> None:
        if path.name == "server.key":
            raise OSError("disk full")
        real(path, data, file_mode)

    monkeypatch.setattr(pki_ca, "_write_file", fail)
    directory = tmp_path / "pki"
    with pytest.raises(OSError, match="disk full"):
        init_pki(directory, NOW)
    assert not directory.exists()


def test_aborted_init_keeps_an_existing_directory_and_unrelated_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = pki_ca._write_file

    def fail(path: Path, data: bytes, file_mode: int) -> None:
        if path.name == "server.key":
            raise OSError("disk full")
        real(path, data, file_mode)

    monkeypatch.setattr(pki_ca, "_write_file", fail)
    directory = tmp_path / "pki"
    directory.mkdir()
    (directory / "note").write_text("keep")
    with pytest.raises(OSError, match="disk full"):
        init_pki(directory, NOW)
    assert mode(directory) == 0o700
    assert sorted(path.name for path in directory.iterdir()) == ["note"]
    assert (directory / "note").read_text() == "keep"


def test_load_reports_missing_files(authority: pki_ca.Authority, tmp_path: Path) -> None:
    with pytest.raises(PkiError, match="missing"):
        load_pki(tmp_path / "absent")
    (authority.directory / "server.pem").unlink()
    with pytest.raises(PkiError, match="missing"):
        load_pki(authority.directory)


def test_load_rejects_a_corrupt_certificate(authority: pki_ca.Authority) -> None:
    (authority.directory / "agent-ca.pem").write_text("not a certificate\n")
    with pytest.raises(PkiError):
        load_pki(authority.directory)


def test_load_rejects_a_key_that_does_not_match(authority: pki_ca.Authority, tmp_path: Path) -> None:
    other = init_pki(tmp_path / "other", NOW)
    (authority.directory / "server.key").write_bytes((other.directory / "server.key").read_bytes())
    with pytest.raises(PkiError, match="does not match"):
        load_pki(authority.directory)


def test_load_rejects_a_server_certificate_from_another_ca(authority: pki_ca.Authority, tmp_path: Path) -> None:
    other = init_pki(tmp_path / "other", NOW)
    for name in ("server.pem", "server.key"):
        (authority.directory / name).write_bytes((other.directory / name).read_bytes())
    with pytest.raises(PkiError, match="not signed"):
        load_pki(authority.directory)


def test_load_rejects_a_non_p256_server_key(authority: pki_ca.Authority) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "rsa")])
    cert = load_cert(authority.server_cert_pem)
    signed = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(cert.not_valid_before_utc)
        .not_valid_after(cert.not_valid_after_utc)
        .sign(key, hashes.SHA256())
    )
    (authority.directory / "server.key").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    (authority.directory / "server.pem").write_bytes(signed.public_bytes(serialization.Encoding.PEM))
    with pytest.raises(PkiError, match="ECDSA P-256"):
        load_pki(authority.directory)


def test_issue_client_certificate(authority: pki_ca.Authority, tmp_path: Path) -> None:
    before = snapshot(authority.directory)
    credential = issued(authority)
    cert = load_cert(credential.cert_pem)
    ca_cert = load_cert(authority.ca_cert_pem)
    assert_p256(cert)
    assert_window(cert)
    assert common_name(cert) == "laptop-alex"
    assert cert.issuer == ca_cert.subject
    cert.verify_directly_issued_by(ca_cert)
    assert not basic(cert).ca
    assert basic(cert).path_length is None
    client_usage = usage(cert)
    assert client_usage.digital_signature
    assert not client_usage.key_cert_sign
    assert not client_usage.crl_sign
    assert extended(cert) == [ExtendedKeyUsageOID.CLIENT_AUTH]
    assert credential.cert_sha256 == fingerprint(credential.cert_pem)
    assert len(credential.cert_sha256) == 64
    assert credential.cert_sha256 == credential.cert_sha256.lower()
    assert credential.not_after == Timestamp(NOW + TEN_YEARS)
    assert credential.cert_pem.startswith("-----BEGIN CERTIFICATE-----\n")
    assert credential.key_pem.startswith("-----BEGIN PRIVATE KEY-----\n")
    assert "ENCRYPTED" not in credential.key_pem
    assert "PRIVATE KEY" not in repr(credential)
    private = serialization.load_pem_private_key(credential.key_pem.encode(), password=None)
    assert isinstance(private, ec.EllipticCurvePrivateKey)
    public = cert.public_key()
    assert isinstance(public, ec.EllipticCurvePublicKey)
    assert private.public_key().public_numbers() == public.public_numbers()
    assert snapshot(authority.directory) == before
    cert_path, key_path = write_pair(tmp_path, credential)
    assert peer_sha256(authority.context, cert_path, key_path) == credential.cert_sha256


@pytest.mark.parametrize("name", ["laptop-alex", "a" * 64, "я" * 64])
def test_client_common_name_is_the_agent_name(authority: pki_ca.Authority, name: str) -> None:
    assert common_name(load_cert(issued(authority, name).cert_pem)) == name


def test_issue_strips_surrounding_spaces(authority: pki_ca.Authority) -> None:
    assert common_name(load_cert(issued(authority, "  laptop-alex  ").cert_pem)) == "laptop-alex"


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("", "must be 1 to 64 characters long"),
        (" ", "must be 1 to 64 characters long"),
        ("a" * 65, "must be 1 to 64 characters long"),
        ("a" * 100, "must be 1 to 64 characters long"),
        ("я" * 65, "must be 1 to 64 characters long"),
        ("lap\ntop", "line breaks are not allowed"),
    ],
)
def test_issue_rejects_a_name_that_cannot_be_a_common_name(authority: pki_ca.Authority, name: str, message: str) -> None:
    before = snapshot(authority.directory)
    assert issue_client(authority, name, NOW) == FieldError("name", message)
    assert snapshot(authority.directory) == before


def test_reissue_is_a_new_certificate_and_the_old_one_still_handshakes(authority: pki_ca.Authority, tmp_path: Path) -> None:
    first, second = issued(authority), issued(authority)
    assert first.cert_sha256 != second.cert_sha256
    assert first.key_pem != second.key_pem
    assert load_cert(first.cert_pem).serial_number != load_cert(second.cert_pem).serial_number
    for credential in (first, second):
        cert_path, key_path = write_pair(tmp_path, credential)
        assert peer_sha256(authority.context, cert_path, key_path) == credential.cert_sha256


def test_handshake_rejects_a_missing_certificate_and_a_foreign_ca(authority: pki_ca.Authority, tmp_path: Path) -> None:
    with pytest.raises(ssl.SSLError):
        peer_sha256(authority.context, None, None)
    foreign = issued(init_pki(tmp_path / "foreign", NOW))
    with pytest.raises(InvalidSignature):
        load_cert(foreign.cert_pem).verify_directly_issued_by(load_cert(authority.ca_cert_pem))
    cert_path, key_path = write_pair(tmp_path, foreign)
    with pytest.raises(ssl.SSLError):
        peer_sha256(authority.context, cert_path, key_path)


def test_fingerprint_and_expiry_fit_the_agents_table(db: sqlite3.Connection, authority: pki_ca.Authority) -> None:
    credential = issued(authority)
    resolver_id = insert(db, "resolvers", {"name": "laptop-alex", "kind": "agent", "position": 0, "created_at": NOW, "updated_at": NOW})
    agent_id = insert(db, "agents", {"resolver_id": resolver_id, "cert_sha256": credential.cert_sha256, "cert_not_after": credential.not_after, "created_at": NOW})
    row = db.execute("SELECT cert_sha256, cert_not_after, revoked_at FROM agents WHERE id = ?", (agent_id,)).fetchone()
    assert row is not None
    assert row["cert_sha256"] == credential.cert_sha256
    assert row["cert_not_after"] == NOW + TEN_YEARS
    assert row["revoked_at"] is None
