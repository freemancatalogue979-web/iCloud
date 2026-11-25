"""The exit address a session leaves from, and who shares it.

The arrangement is one sentence: several accounts, one address, each with
a device of its own. So the name Decodo wants, the way groups are filled
and emptied, and the way an address-echo service answers are all pinned
here.

Run with: python tests/test_proxy_pool.py
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("ICLOUD_STATE", "/tmp/icloud-test-state")

import proxy_pool  # noqa: E402

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


def main():
    # -- the name Decodo wants ---------------------------------------
    said = proxy_pool.decodo_username("alice", country="us", session="abc123",
                                      minutes=120)
    check("the name says the user, the session and the country",
          said == "user-alice-session-abc123-sessionduration-120-country-us",
          said)

    check("a name that already has its prefix is not given it twice",
          proxy_pool.decodo_username("user-alice", country="us",
                                     session="abc123") ==
          "user-alice-session-abc123-country-us",
          proxy_pool.decodo_username("user-alice", country="us", session="abc123"))

    check("a locked session keeps its address when the node dies",
          "session_iplock" in proxy_pool.decodo_username(
              "alice", country="us", session="abc123", sticky="iplock"),
          proxy_pool.decodo_username("alice", country="us", session="abc123",
                                     sticky="iplock"))

    check("both can be asked for together",
          proxy_pool.decodo_username("alice", country="us", session="abc123",
                                     sticky="both").count("session") == 2)

    # -- a name copied out of the dashboard's panel --------------------
    check("a name with a session in it is cut back to the name",
          proxy_pool.base_user("user-alice-session-1-sessionduration-60-country-us")
          == "user-alice",
          proxy_pool.base_user("user-alice-session-1-sessionduration-60-country-us"))
    check("...and one with only a country in it too",
          proxy_pool.base_user("user-alice-country-de") == "user-alice")
    check("...while a name with dashes in it is left alone",
          proxy_pool.base_user("user-my-proxy-user") == "user-my-proxy-user")
    check("...and a plain name is left alone",
          proxy_pool.base_user("alice") == "alice")

    # -- a pool, and the groups in it ---------------------------------
    with tempfile.TemporaryDirectory() as scratch:
        os.environ["ICLOUD_STATE"] = scratch
        os.environ["ICLOUD_PROXY"] = "decodo"
        os.environ["ICLOUD_PROXY_USER"] = "alice"
        os.environ["ICLOUD_PROXY_PASSWORD"] = "secret"
        os.environ["ICLOUD_PROXY_MAX_PER_GROUP"] = "2"
        os.environ["ICLOUD_PROXY_SESSION_MINUTES"] = "180"

        pool = proxy_pool.Pool()
        check("the pool knows it has a proxy", pool.enabled)

        # Two visitors share an address, a third gets one of its own.
        one = pool.assign("visitor-one", "US")
        two = pool.assign("visitor-two", "US")
        three = pool.assign("visitor-three", "US")
        check("two visitors share an address", one == two, (one, two))
        check("a third visitor gets an address of its own",
              three != one, (one, three))

        check("a visitor keeps their address",
              pool.assign("visitor-one", "US") == one)

        group = pool.group(one)
        check("the group is named after its country", one.startswith("us-"), one)
        check("the group keeps a session of its own",
              bool(group.get("session")), group)

        settings = pool.settings(one)
        check("the proxy is the backconnect entrance",
              settings["server"] == "http://gate.decodo.com:7000", settings)
        check("...and http, because a browser cannot use socks5 with a "
              "password",
              pool.scheme == "http", pool.scheme)
        check("...with the country in the name",
              "-country-us" in settings["username"], settings["username"])
        check("...and the session in it",
              group["session"] in settings["username"], settings["username"])
        check("...and how long it is held",
              "-sessionduration-180" in settings["username"], settings["username"])

        check("the password comes from the environment, not from the disk",
              settings["password"] == "secret")

        # What a group is seen leaving from is remembered.
        pool.observe(one, "198.51.100.7", "us")
        check("the address a group left from is remembered",
              pool.group(one)["exit_ip"] == "198.51.100.7")

    # -- and no proxy at all -------------------------------------------
    os.environ["ICLOUD_PROXY"] = "none"
    plain = proxy_pool.Pool()
    check("a pool with no proxy has none to give",
          plain.settings("us-0") is None)
    check("...and says so",
          not plain.enabled)

    # -- what an echo service says -------------------------------------
    said = proxy_pool.parse_exit('{"ip": "198.51.100.7", "country_code": "US"}')
    check("the address is read from the answer",
          said["ip"] == "198.51.100.7", said)
    check("...and the country with it", said["country"] == "US", said)

    said = proxy_pool.parse_exit('{"status": "success", "countryCode": "DE", '
                                  '"query": "203.0.113.9"}')
    check("another shape of answer is read too",
          said["ip"] == "203.0.113.9" and said["country"] == "DE", said)

    said = proxy_pool.parse_exit("203.0.113.9")
    check("a plain address is read as an address",
          said["ok"] and said["ip"] == "203.0.113.9", said)

    said = proxy_pool.parse_exit("<html>nope</html>")
    check("an answer that is neither is not an answer", not said["ok"], said)

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


sys.exit(main())
