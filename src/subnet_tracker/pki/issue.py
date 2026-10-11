import ssl
from dataclasses import dataclass, field
from hashlib import sha256

from cryptography.x509.oid import ExtendedKeyUsageOID

from subnet_tracker.pki.ca import CERT_LIFETIME, Authority, _common_name, _leaf_extensions, _load_cert, _load_key, _pem_cert, _pem_key, _private_key, _signed
from subnet_tracker.types import MAX_RESOLVER_NAME, FieldError, Timestamp, one_line_name

# cryptography measures commonName in UTF-8 bytes (X.520 ub-common-name), so 64 Cyrillic characters do not fit.
CN_BYTES = 64


@dataclass(frozen=True, slots=True)
class ClientCertificate:
    cert_pem: str
    key_pem: str = field(repr=False)
    cert_sha256: str
    not_after: Timestamp


def issue_client(authority: Authority, name: str, now: Timestamp) -> ClientCertificate | FieldError:
    checked = one_line_name("name", name, MAX_RESOLVER_NAME)
    if isinstance(checked, FieldError):
        return checked
    if len(checked.encode()) > CN_BYTES:
        return FieldError("name", "must be at most 64 UTF-8 bytes")
    key = _private_key()
    ca_key = _load_key(authority.ca_key_pem)
    ca_cert = _load_cert(authority.ca_cert_pem)
    cert = _signed(_common_name(checked), key.public_key(), ca_key, ca_cert.subject, now, _leaf_extensions(ca_key, ExtendedKeyUsageOID.CLIENT_AUTH))
    cert_pem = _pem_cert(cert).decode()
    return ClientCertificate(cert_pem, _pem_key(key).decode(), sha256(ssl.PEM_cert_to_DER_cert(cert_pem)).hexdigest(), Timestamp(now + CERT_LIFETIME))
