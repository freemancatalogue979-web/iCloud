"""Where the visitor is, as far as an address can say.

An address is not a place, but it is close enough: the country an address
belongs to decides which pool of exit addresses a visitor should leave
from, and it is what their browser should say about the time of day.

Two things are asked of this module:

* the address of the visitor - the person who opened the app, as the
  machine in front of them sees it, and
* the country that address is in.

The first is easy when the app is the thing being talked to, and is a
matter of trust when there is something in front of it. Cloudflare puts
the visitor's address in a header of its own, and a header can be written
by anyone - so a header is believed only when it comes from Cloudflare's
own addresses, and the app sits behind a firewall that lets nothing else
through. A header from somewhere else is not read at all.

The second is a lookup. A local database answers it without asking anyone;
when there is no database a web service is asked, and the answer is
remembered for a while, because a visitor's country does not change.
"""

import ipaddress
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

# Cloudflare's own header. It is the one header Cloudflare sets itself, and
# it holds the address of the visitor who reached Cloudflare - which is the
# address the app wants and not the address of Cloudflare's edge.
CLOUDFLARE_HEADER = "cf-connecting-ip"

# Cloudflare's published addresses. A header is believed only when it came
# from one of these, so a visitor who sends the header themselves is
# ignored. The list is here rather than fetched because the app must be
# able to answer this question with no network at all.
#
#   https://www.cloudflare.com/ips/
CLOUDFLARE_V4 = [
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
    "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
    "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
]
CLOUDFLARE_V6 = [
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32",
    "2405:8100::/32", "2a06:98c0::/29", "2c0f:f248::/32",
]

# How long a country is remembered. A visitor's country does not change
# between one sign-in and the next.
CACHE_SECONDS = 24 * 3600

# The web service used when there is no local database. It is asked in this
# form because the answer is a plain two-letter country code.
LOOKUP_URL = os.environ.get("ICLOUD_GEOIP_URL") or "https://ipwho.is/{ip}"

# The local database, when one has been put on the machine. The reader is
# optional: the app is not a geolocation service, and it must work on a
# box where nothing has been installed for it.
DB_PATH = os.environ.get("ICLOUD_GEOIP_DB") or ""

# What was looked up, and when it may be forgotten: (country, until).
_looked_up: dict = {}


def _is_address(text: str) -> bool:
    """Is this an IP address, and nothing else?"""
    try:
        ipaddress.ip_address(text.strip())
        return True
    except (ValueError, AttributeError):
        return False


def _in_any(address: str, networks: list) -> bool:
    """Is this address inside one of these networks?"""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    for text in networks:
        try:
            if parsed in ipaddress.ip_network(text):
                return True
        except ValueError:
            continue
    return False


def peer_is_cloudflare(host: str) -> bool:
    """Is the machine that connected to the app one of Cloudflare's?

    The peer is the address the connection came from. When the app is
    behind Cloudflare this is Cloudflare's edge, and only then may the
    headers Cloudflare sets be believed.
    """
    if not host:
        return False
    host = host.strip()
    if ":" in host and host.count(":") == 1:
        # "[::1]:1234" - an address with a port on it.
        host = host.split(":", 1)[0].strip("[]")
    if _in_any(host, CLOUDFLARE_V4) or _in_any(host, CLOUDFLARE_V6):
        return True
    # A Unix socket, or a name that was never an address.
    return False


def trust_cloudflare() -> bool:
    """May the header Cloudflare sets be believed?

    They may, when the connection came from Cloudflare's own addresses -
    which is checked on every request, not assumed. This setting turns
    the whole thing off, for a machine that is not behind Cloudflare and
    would rather read the address it saw itself.
    """
    return _setting("ICLOUD_TRUST_CF_HEADER", "1") not in ("0", "off", "no", "false")


def _setting(name: str, default: str) -> str:
    return (os.environ.get(name) or default).strip().lower()


def client_ip(request) -> str:
    """The address of the visitor who made this request.

    The address the app saw is believed first - it cannot be written by
    the visitor. When that address is Cloudflare's, and only then, the
    header Cloudflare set is read: Cloudflare puts the address of the
    person who reached it there, which is the address the app wants.

    Everything else is left alone. A request that arrives from somewhere
    other than Cloudflare keeps its own address, whatever headers it
    carries, so a visitor cannot choose the country - and with it the
    proxy and the identity - by sending one.
    """
    peer = ""
    try:
        peer = request.client.host if request.client else ""
    except Exception:
        peer = ""

    if peer_is_cloudflare(peer) and trust_cloudflare():
        headers = getattr(request, "headers", {}) or {}
        said = headers.get(CLOUDFLARE_HEADER, "")
        if said and _is_address(said):
            return said.strip()
        # Cloudflare also puts the original visitor at the front of
        # X-Forwarded-For, with the addresses it went through after it.
        forwarded = headers.get("x-forwarded-for", "")
        first = forwarded.split(",")[0].strip() if forwarded else ""
        if first and _is_address(first):
            return first

    return peer


def _reader():
    """The local database, if there is one and it can be opened.

    The reader is opened once and kept: a lookup on every request would
    otherwise read the file from disk every time.
    """
    if not DB_PATH:
        return None
    path = Path(DB_PATH)
    if not path.exists():
        return None
    try:
        import geoip2.database  # noqa: PLC0415 - optional, and slow to import
    except ImportError:
        return None
    try:
        return geoip2.database.Reader(str(path))
    except Exception:
        return None


_readers: dict = {}


def _local_country(ip: str) -> str | None:
    """The country from the local database, if it has one for this address."""
    reader = _readers.get("reader")
    if reader is None:
        reader = _reader()
        if reader is None:
            return None
        _readers["reader"] = reader
    try:
        answer = reader.country(ip)
    except Exception:
        return None
    code = (answer.country.iso_code or "").upper()
    return code or None


def _remote_country(ip: str) -> str | None:
    """The country from the web service, if it answers."""
    url = LOOKUP_URL.replace("{ip}", ip)
    try:
        with urllib.request.urlopen(url, timeout=10) as reply:
            body = reply.read(200_000)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    try:
        said = json.loads(body.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(said, dict):
        return None
    # The services differ in what they call the field. These are the names
    # they have used.
    for field in ("country_code", "countryCode", "country_code_iso",
                  "country", "countryIsoCode"):
        value = said.get(field)
        if isinstance(value, str) and len(value) == 2 and value.isalpha():
            return value.upper()
    return None


def country_of(ip: str) -> str | None:
    """The country an address is in, or None when nobody knows.

    The local database is asked first: it is on the machine, it is fast,
    and it tells nobody else who is looking. The web service is the
    fallback, for a machine that has not been given a database yet.

    The answer is remembered, so a visitor is looked up once and not on
    every request.
    """
    if not ip or not _is_address(ip):
        return None

    until = _looked_up.get(ip)
    now = time.time()
    if until is not None:
        country, expires = until
        if expires > now:
            return country
        _looked_up.pop(ip, None)

    country = _local_country(ip) or _remote_country(ip)
    # A miss is remembered too, for a shorter while: the service may be
    # down, and asking it again on every request would only make that
    # worse.
    expires = now + (CACHE_SECONDS if country else 300)
    _looked_up[ip] = (country, expires)
    return country
