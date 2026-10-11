from collections.abc import Sequence

from subnet_tracker.types import JsonValue

A, AAAA, CNAME = 1, 28, 5


def record(name: JsonValue, rdtype: JsonValue, data: JsonValue, ttl: JsonValue = 300) -> JsonValue:
    return {"name": name, "type": rdtype, "TTL": ttl, "data": data}


def body(name: str, rdtype: int, answer: Sequence[JsonValue] = (), status: JsonValue = 0) -> JsonValue:
    return {"Status": status, "Question": [{"name": name, "type": rdtype}], "Answer": list(answer)}


# answers as Cloudflare (https://cloudflare-dns.com/dns-query, Accept: application/dns-json) and Google (https://dns.google/resolve) return them:
# Cloudflare writes owner names without the trailing dot, Google with it; both end CNAME targets with a dot

CLOUDFLARE_A: JsonValue = {
    "Status": 0,
    "TC": False,
    "RD": True,
    "RA": True,
    "AD": True,
    "CD": False,
    "Question": [{"name": "example.com", "type": 1}],
    "Answer": [{"name": "example.com", "type": 1, "TTL": 3241, "data": "93.184.215.14"}],
}

CLOUDFLARE_CNAME_A: JsonValue = {
    "Status": 0,
    "TC": False,
    "RD": True,
    "RA": True,
    "AD": False,
    "CD": False,
    "Question": [{"name": "www.youtube.com", "type": 1}],
    "Answer": [
        {"name": "www.youtube.com", "type": 5, "TTL": 2187, "data": "youtube-ui.l.google.com."},
        {"name": "youtube-ui.l.google.com", "type": 1, "TTL": 187, "data": "142.250.74.14"},
        {"name": "youtube-ui.l.google.com", "type": 1, "TTL": 187, "data": "142.250.74.46"},
    ],
}

CLOUDFLARE_AAAA_NODATA: JsonValue = {
    "Status": 0,
    "TC": False,
    "RD": True,
    "RA": True,
    "AD": True,
    "CD": False,
    "Question": [{"name": "ipv4only.arpa", "type": 28}],
    "Authority": [{"name": "ipv4only.arpa", "type": 6, "TTL": 3600, "data": "a.iana-servers.net. nstld.iana.org. 2022011000 1800 900 604800 3600"}],
}

CLOUDFLARE_NXDOMAIN: JsonValue = {
    "Status": 3,
    "TC": False,
    "RD": True,
    "RA": True,
    "AD": True,
    "CD": False,
    "Question": [{"name": "nonexistent.example.com", "type": 1}],
    "Authority": [{"name": "example.com", "type": 6, "TTL": 3600, "data": "ns.icann.org. noc.dns.icann.org. 2024081401 7200 3600 1209600 3600"}],
}

GOOGLE_A: JsonValue = {
    "Status": 0,
    "TC": False,
    "RD": True,
    "RA": True,
    "AD": False,
    "CD": False,
    "Question": [{"name": "example.com.", "type": 1}],
    "Answer": [{"name": "example.com.", "type": 1, "TTL": 3600, "data": "93.184.215.14"}],
    "Comment": "Response from 2001:500:8d::53.",
}

GOOGLE_CNAME_AAAA: JsonValue = {
    "Status": 0,
    "TC": False,
    "RD": True,
    "RA": True,
    "AD": False,
    "CD": False,
    "Question": [{"name": "www.youtube.com.", "type": 28}],
    "Answer": [
        {"name": "www.youtube.com.", "type": 5, "TTL": 3600, "data": "youtube-ui.l.google.com."},
        {"name": "youtube-ui.l.google.com.", "type": 28, "TTL": 300, "data": "2a00:1450:4001:80b::200e"},
        {"name": "youtube-ui.l.google.com.", "type": 28, "TTL": 300, "data": "2a00:1450:4001:810::200e"},
    ],
    "edns_client_subnet": "198.51.100.0/24",
}

GOOGLE_NXDOMAIN: JsonValue = {
    "Status": 3,
    "TC": False,
    "RD": True,
    "RA": True,
    "AD": False,
    "CD": False,
    "Question": [{"name": "nonexistent.example.com.", "type": 28}],
    "Authority": [{"name": "example.com.", "type": 6, "TTL": 1800, "data": "ns.icann.org. noc.dns.icann.org. 2024081401 7200 3600 1209600 3600"}],
    "Comment": "Response from 199.43.135.53.",
}

GOOGLE_SERVFAIL: JsonValue = {
    "Status": 2,
    "TC": False,
    "RD": True,
    "RA": True,
    "AD": False,
    "CD": False,
    "Question": [{"name": "dnssec-failed.org.", "type": 1}],
    "Comment": "DNSSEC validation failure. Please check http://dnsviz.net/d/dnssec-failed.org/dnssec/.",
}

GOOGLE_HTTP_400: JsonValue = {"error": "Invalid name: Name too long"}
