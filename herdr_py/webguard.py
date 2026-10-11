"""What every herdr-py web server checks before it answers. Standard library only; Python 3.6+.

- Host: a server that listens on this machine only answers requests addressed to this machine (localhost, a name
  under .localhost, 127.0.0.0/8, ::1) or to a name given with --allow-host. A web page elsewhere can point its own
  domain at 127.0.0.1 (DNS rebinding) and then read whatever the server shows without a token; the browser still sends
  that domain in Host, so such requests are refused. A server bound to any other address was exposed on purpose and
  answers any Host.
- token: compared as bytes in constant time; a header with characters outside ASCII is a wrong token, not a crash.
- body: Content-Length must be a whole number (ASCII digits only) no larger than the endpoint's limit.
"""
import hmac
import ipaddress
import re

DIGITS = re.compile(r"[0-9]+")
PORT = re.compile(r"(?::[0-9]*)?")


def _loopback_name(name):
    if name == "localhost" or name.endswith(".localhost"):  # RFC 6761: always this machine; browsers never ask DNS
        return True
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:  # ::ffff:127.0.0.1 is 127.0.0.1 (Python before 3.13 says otherwise)
        ip = ip.ipv4_mapped
    return ip.is_loopback


def is_loopback(bound):
    """True when the address a server is bound to can only be reached from this machine."""
    return _loopback_name((bound or "").strip().strip("[]").lower())


def host_name(header):
    """The name in a Host header, lower case and without the port ('Example.com:80' -> 'example.com', '[::1]:8790'
    -> '::1'); None when the header is missing or malformed."""
    header = (header or "").strip().lower()
    if header.startswith("["):
        name, bracket, rest = header[1:].partition("]")
        if not bracket:
            return None
    else:
        name, colon, port = header.partition(":")
        rest = colon + port
    if not name or not PORT.fullmatch(rest):
        return None
    return name


def host_allowed(header, bound, allow=()):
    """Whether a request with this Host header may be answered by a server bound to `bound` (see the module notes)."""
    if not is_loopback(bound):
        return True
    name = host_name(header)
    if name is None:
        return False
    return _loopback_name(name) or name in {a.strip().lower() for a in allow if a}


def refused(handler, bound, allow=()):
    """Answer 403 and return True when the request's Host is not allowed; return False to go on. `handler` is an
    http.server.BaseHTTPRequestHandler."""
    header = handler.headers.get("Host")
    if host_allowed(header, bound, allow):
        return False
    body = ("this server answers requests for localhost, 127.0.0.1 or ::1 only (Host was %r); behind a proxy that "
            "passes on a public name, start it with --allow-host NAME\n" % (header,)).encode("utf-8")
    handler.send_response(403)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    handler.wfile.write(body)
    return True


def token_ok(given, token):
    """Constant-time comparison of a token from a request with the server's; anything that is not it is False."""
    if not token:
        return False
    return hmac.compare_digest((given or "").encode("utf-8", "replace"), token.encode("utf-8"))


def bearer(headers):
    """The token in "Authorization: Bearer <token>", or ""."""
    header = headers.get("Authorization") or ""
    return header[7:] if header.startswith("Bearer ") else ""


def body_length(headers, limit):
    """(length, None) for a body this endpoint takes, or (None, (status, message)). No Content-Length is an empty body."""
    raw = headers.get("Content-Length")
    if raw is None:
        return 0, None
    raw = raw.strip()
    if not DIGITS.fullmatch(raw):
        return None, (400, "Content-Length must be a whole number")
    length = int(raw)
    if length > limit:
        return None, (413, "too long: at most %d bytes" % limit)
    return length, None
