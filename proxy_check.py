"""Is the proxy there, and does it say what it should?

Run it before the app is started, and again whenever the address stops
being right:

    python proxy_check.py            # the country from the environment
    python proxy_check.py de         # a country of your own
    python proxy_check.py de us-0    # a group of your own

It builds the name Decodo expects, asks the address-echo service through
the proxy, and says what came back: the address, the country, and whether
the address is the one the group was seen at last time. A sticky session
that has moved is worth knowing about before a sign-in starts rather
than in the middle of one.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import proxy_pool  # noqa: E402


def main(argv):
    country = (argv[0] if argv else "") or proxy_pool.fallback_country()
    group = argv[1] if len(argv) > 1 else f"{country.lower()}-0"

    pool = proxy_pool.POOL
    if not pool.enabled:
        print("No proxy is set up.")
        print("")
        print("These four say where the proxy is and who you are to it:")
        print("  ICLOUD_PROXY=decodo")
        print("  ICLOUD_PROXY_SERVER=gate.decodo.com:7000")
        print("  ICLOUD_PROXY_SCHEME=socks5")
        print("  ICLOUD_PROXY_USER=...      the username from the dashboard")
        print("  ICLOUD_PROXY_PASSWORD=...")
        print("")
        print("proxy.env.example has all of them, with the reasons.")
        return 1

    if not pool.user or not pool.password:
        print("The proxy is set up, but there is no user name or password for it.")
        print("The dashboard's Authentication section has both.")
        return 1

    settings = pool.settings(group, country=country)
    print(f"country     {country}")
    print(f"group       {group}")
    print(f"server      {settings['server']}")
    print(f"username    {settings['username']}")
    print(f"password    {'*' * len(settings['password'])}")

    said = pool.check(group, country=country)
    print("")
    if not said.get("ok"):
        print(f"it did not answer: {said.get('said')}")
        print("")
        print("The usual reasons:")
        print("  - the user name or the password is wrong")
        print("  - the plan does not have residential proxies (or SOCKS5) on it")
        print("  - SOCKS5 residential needs gate.decodo.com and port 7000,")
        print("    not a country endpoint such as us.decodo.com")
        return 1

    print(f"exit ip     {said.get('ip')}")
    print(f"country     {said.get('country') or '(not said)'}")

    known = pool.group(group)
    if known.get("exit_ip") and known["exit_ip"] != said.get("ip"):
        print("")
        print(f"this group was last seen at {known['exit_ip']}, and is now at "
              f"{said.get('ip')}")
        print("the address has moved - a sign-in in progress would see two")
    elif known.get("exit_ip"):
        print("")
        print(f"the same address as last time ({known['exit_ip']}) - sticky holds")
    else:
        print("")
        print("this is the first time this group has been seen; it is "
              "remembered now")

    if said.get("country") and said["country"] != country.upper():
        print("")
        print(f"the address is in {said['country']}, not {country.upper()} - "
              "the country asked for and the address got are not the same")
        return 1
    return 0


sys.exit(main(sys.argv[1:]))
