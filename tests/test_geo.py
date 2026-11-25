"""Where the visitor is, as far as an address can say.

The part that matters is trust. The app sits behind Cloudflare, so the
address of the visitor is in a header - and a header is written by
whoever sends it. So a header is believed only when it came from
Cloudflare's own addresses, and a visitor who sends the header
themselves is ignored.

Run with: python tests/test_geo.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("ICLOUD_STATE", "/tmp/icloud-test-state")

import geo  # noqa: E402

passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("ok   " + name)
    else:
        failed += 1
        print("FAIL " + name + (("  " + str(detail)) if detail else ""))


class FakeRequest:
    """A request, with only the three things the app reads off one."""

    def __init__(self, host, headers=None):
        self.client = type("Address", (), {"host": host})()
        self.headers = dict(headers or {})


def main():
    # -- whose addresses are Cloudflare's ----------------------------
    check("a Cloudflare address is recognised",
          geo.peer_is_cloudflare("104.16.0.1"), geo.peer_is_cloudflare("104.16.0.1"))
    check("...one of the IPv6 ones too",
          geo.peer_is_cloudflare("2606:4700::1"))
    check("an address that is not Cloudflare's is not",
          not geo.peer_is_cloudflare("203.0.113.45"))
    check("a name is not an address",
          not geo.peer_is_cloudflare("localhost"))

    # -- the visitor's address ---------------------------------------
    direct = FakeRequest("203.0.113.45", {"cf-connecting-ip": "198.51.100.9"})
    check("a header from outside Cloudflare is not believed",
          geo.client_ip(direct) == "203.0.113.45", geo.client_ip(direct))

    behind = FakeRequest("104.16.0.1", {"cf-connecting-ip": "198.51.100.9"})
    check("a header from Cloudflare is believed",
          geo.client_ip(behind) == "198.51.100.9", geo.client_ip(behind))

    forwarded = FakeRequest("104.16.0.1", {
        "x-forwarded-for": "198.51.100.9, 104.16.0.1"})
    check("the first address in the forwarded list is the visitor",
          geo.client_ip(forwarded) == "198.51.100.9", geo.client_ip(forwarded))

    nonsense = FakeRequest("104.16.0.1", {"cf-connecting-ip": "not-an-address"})
    check("a header that is not an address is not used",
          geo.client_ip(nonsense) == "104.16.0.1", geo.client_ip(nonsense))

    # And the whole thing can be turned off, for a machine that is not
    # behind Cloudflare at all.
    os.environ["ICLOUD_TRUST_CF_HEADER"] = "0"
    try:
        check("the header can be turned off",
              geo.client_ip(behind) == "104.16.0.1", geo.client_ip(behind))
    finally:
        os.environ.pop("ICLOUD_TRUST_CF_HEADER", None)
    check("...and is on again when it is",
          geo.client_ip(behind) == "198.51.100.9", geo.client_ip(behind))

    # -- the country --------------------------------------------------
    check("an address that is not an address has no country",
          geo.country_of("hello") is None)

    asked = []

    def fake_remote(ip):
        asked.append(ip)
        return "DE"

    def fake_local(ip):
        return ""

    geo._local_country = fake_local
    geo._remote_country = fake_remote
    geo._looked_up.clear()

    check("the country comes back",
          geo.country_of("203.0.113.45") == "DE")
    check("and the address is only looked up once",
          len(asked) == 1, asked)
    geo.country_of("203.0.113.45")
    check("...still once, the second time",
          len(asked) == 1, asked)

    def local_first(ip):
        return "FR"

    geo._local_country = local_first
    geo._remote_country = fake_remote
    geo._looked_up.clear()
    check("the local database is asked first",
          geo.country_of("198.51.100.1") == "FR")
    check("...and the web service is not asked when it answers",
          asked == ["203.0.113.45"], asked)

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


sys.exit(main())
