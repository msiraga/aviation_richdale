"""Generate a local HTTPS certificate so iOS Safari treats the cockpit as a secure origin.

Creates a private CA plus a server certificate covering localhost and the
machine's LAN address, writing everything under data/certs/ (git-ignored).
Run:  python make_https_cert.py [lan-ip]
"""

import datetime as dt
import ipaddress
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

OUT_DIR = Path("data/certs")
CA_COMMON_NAME = "aviation_richdale local CA"
SERVER_COMMON_NAME = "aviation_richdale cockpit"


def build_name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def main() -> None:
    lan_ip = sys.argv[1] if len(sys.argv) > 1 else detect_lan_ip()

    now = dt.datetime.now(dt.timezone.utc)
    not_before = now - dt.timedelta(days=1)
    not_after = now + dt.timedelta(days=825)

    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(build_name(CA_COMMON_NAME))
        .issuer_name(build_name(CA_COMMON_NAME))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=True,
                crl_sign=True, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )

    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    san_entries: list[x509.GeneralName] = [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
    ]
    if lan_ip:
        san_entries.append(x509.IPAddress(ipaddress.ip_address(lan_ip)))

    server_cert = (
        x509.CertificateBuilder()
        .subject_name(build_name(SERVER_COMMON_NAME))
        .issuer_name(ca_cert.subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False, key_encipherment=True,
                data_encipherment=False, key_agreement=True, key_cert_sign=False,
                crl_sign=False, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "ca.crt.pem").write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    (OUT_DIR / "server.key.pem").write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    (OUT_DIR / "server.crt.pem").write_bytes(
        server_cert.public_bytes(serialization.Encoding.PEM).rstrip(b"\n")
        + b"\n"
        + ca_cert.public_bytes(serialization.Encoding.PEM)
    )
    (OUT_DIR / "ca.crt").write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))

    print(f"LAN IP covered : {lan_ip or 'none detected'}")
    print(f"CA profile     : {OUT_DIR / 'ca.crt'}   <- AirDrop this to the iPhone")
    print(f"Server cert    : {OUT_DIR / 'server.crt.pem'}")
    print(f"Server key     : {OUT_DIR / 'server.key.pem'}")
    print("Run uvicorn with:")
    print(
        "  .venv\\Scripts\\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8443 "
        "--ssl-certfile data\\certs\\server.crt.pem --ssl-keyfile data\\certs\\server.key.pem"
    )


def detect_lan_ip() -> str | None:
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.168.255.255", 1))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


if __name__ == "__main__":
    main()
