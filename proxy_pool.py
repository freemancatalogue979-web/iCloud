"""The exit address a session leaves from, and who shares it.

The point of this module is one sentence: several accounts leaving from
one address, each with a device of its own.

So there are two things in it. The first is the address itself - a proxy,
a country, and a way of asking the provider for the same exit again. The
second is the grouping: which visitor shares which exit. A visitor is put
in a group, a group is one exit address, and a group holds a few visitors
and no more - because a household has two Apple IDs and not forty, and an
address that signs in forty of them is not a household whatever the
devices say.

Groups are named after the country and a number ("us-0"), so the panel can
say which is which, and each keeps a sticky session of its own, so the
same group comes back to the same address tomorrow.

The provider's own secrets never come from here: the user and the password
are read from the environment when a connection is made, and what is
written to disk is the name of the group and the session that goes with
it.
"""

import ipaddress
import json
import os
import secrets
import shutil
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).parent

# Where the groups are remembered. The file holds which visitor is in
# which group and the address each group was last seen leaving from - not
# the provider's password, which stays in the environment.
STATE = Path(os.environ.get("ICLOUD_STATE") or (HERE / "state")).resolve()
GROUPS_FILE = STATE / "groups.json"

# Decodo's backconnect entrance. One entrance, and everything else - the
# country, the sticky session - is said in the user name.
DECODO_SERVER = "gate.decodo.com:7000"

# Where the exit address is checked. The same service the provider points
# at; the browser is asked, so the answer is the address the browser
# really leaves from.
EXIT_CHECK_URL = os.environ.get("ICLOUD_EXIT_CHECK_URL") or \
    "https://ip.decodo.com/json"


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _number(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except (TypeError, ValueError):
        return default


# The words Decodo reads as parameters in a user name. A name that is
# copied out of the dashboard's proxy-setup panel has some of these in it
# already - and the app adds its own, so they are taken off first.
PARAMETERS = ("session", "sessionduration", "session_iplock", "country",
              "city", "state", "zip", "asn", "continent", "geohash")


def base_user(user: str) -> str:
    """The user name, without the parameters that were added to it.

    The dashboard shows two things: the user name itself, and the user
    name with the country and the session in it. Both are accepted here -
    the second is cut back to the first, so that the app can put its own
    country and its own session in.
    """
    said = (user or "").strip()
    if not said:
        return ""
    parts = said.split("-")
    for index, part in enumerate(parts):
        if index and part.lower() in PARAMETERS:
            said = "-".join(parts[:index])
            break
    return said


def decodo_username(user: str, country: str = "", session: str = "",
                    minutes: int = 0, sticky: str = "session") -> str:
    """The user name Decodo wants for one sticky session in one country.

    The name is a list of words, each with its value after it, joined with
    dashes. The user's own name is first and is always prefixed with
    ``user-``; the session, how long it lasts and the country follow.

    The order is the order Decodo's own generator uses - ``user-``, then
    the session, then the duration, then the country - so that the name
    this builds can be compared with the one on their dashboard side by
    side.

    ``sticky`` chooses how the session is held:

    ``session``  the ordinary sticky session - the same address while it
                 lasts
    ``iplock``   the address is kept even if the node it was on goes
                 offline
    ``both``     the ordinary session and the lock together
    """
    name = (user or "").strip()
    if not name:
        raise ValueError("no proxy user name")
    parts = [name if name.startswith("user-") else f"user-{name}"]
    if sticky == "iplock":
        parts += ["session_iplock", session]
    elif sticky == "both":
        parts += ["session", session, "session_iplock", session]
    elif session:
        parts += ["session", session]
    if minutes:
        parts += ["sessionduration", str(int(minutes))]
    if country:
        parts += ["country", country.strip().lower()]
    return "-".join(parts)


class Pool:
    """Every address a session can leave from.

    Built once from the environment. ``ICLOUD_PROXY`` chooses the shape:
    ``none`` (the app talks to the internet itself), ``decodo`` (one
    backconnect entrance, everything else in the user name) or ``generic``
    (a server, a user and a password, said as they are).
    """

    def __init__(self, settings: dict | None = None):
        """The pool the app uses, or a pool made of settings for a test.

        With no settings it comes from the environment: that is where it
        starts. With settings it is made of them, and nothing is kept -
        which is how a proxy can be tried before it is turned on.
        """
        self.kind = "none"
        self.server = ""
        self.scheme = "socks5"
        self.user = ""
        self.trimmed = False
        self.password = ""
        self.country = "US"
        self.session_minutes = 120
        self.sticky = "session"
        self.groups_per_country = 8
        self.max_per_group = 3
        self.enabled = False
        self._state = None

        if isinstance(settings, dict) and settings:
            self.configure(settings)
            # A pool made for a test is asked whether it can be used, not
            # whether it has been switched on.
            self.enabled = self.usable()
        else:
            self._from_environment()

    def _from_environment(self) -> None:
        """Take the proxy from the environment, where it starts."""
        self.kind = (_env("ICLOUD_PROXY", "none") or "none").lower()
        self.server = _env("ICLOUD_PROXY_SERVER") or (
            DECODO_SERVER if self.kind == "decodo" else "")
        self.scheme = (_env("ICLOUD_PROXY_SCHEME", "socks5") or "socks5").lower()
        given = _env("ICLOUD_PROXY_USER")
        self.user = base_user(given)
        # Whether the name that was given had parameters in it, and they
        # were taken off. Worth saying out loud: a name with two countries
        # in it is a name that was going to be wrong.
        self.trimmed = bool(self.user) and self.user != given
        self.password = _env("ICLOUD_PROXY_PASSWORD")
        self.country = _env("ICLOUD_PROXY_COUNTRY", "US").upper()
        self.session_minutes = _number("ICLOUD_PROXY_SESSION_MINUTES", 120)
        self.sticky = (_env("ICLOUD_PROXY_STICKY", "session") or "session").lower()
        self.groups_per_country = _number("ICLOUD_PROXY_GROUPS_PER_COUNTRY", 8)
        self.max_per_group = _number("ICLOUD_PROXY_MAX_PER_GROUP", 3)
        # Whether the browsers leave through it at all. The environment says
        # where it starts; the panel is the switch from then on.
        self.enabled = self.usable()

    def usable(self) -> bool:
        """Is there enough here to connect to?

        A proxy with no address, or no name, or no password cannot be
        connected to - and a panel that says "it is off" without saying
        which of these is missing is a panel that makes the operator
        guess.
        """
        if self.kind not in ("decodo", "generic"):
            return False
        if not self.server:
            return False
        if not self.user:
            return False
        if self.kind == "decodo" and not self.password:
            return False
        return True

    # -- what the app is allowed to do ---------------------------------

    def configure(self, settings: dict) -> None:
        """Take the proxy from somewhere else: the panel, or a file.

        This is how the change is live: the pool is told what it is now,
        and the next browser that opens uses it. The browsers that are
        open keep the address they were opened with - a browser cannot be
        moved to another address while it is running.
        """
        if not isinstance(settings, dict) or not settings:
            return

        def text(name: str, default: str = "") -> str:
            value = settings.get(name)
            if isinstance(value, str) and value.strip():
                return value.strip()
            return default

        self.kind = text("kind", self.kind)
        self.server = text("server", self.server)
        self.scheme = text("scheme", self.scheme)
        given = text("username", "")
        if given:
            self.user = base_user(given)
        password = settings.get("password")
        if isinstance(password, str) and password:
            self.password = password
        self.country = text("country", self.country).upper()
        try:
            self.session_minutes = max(1, int(settings.get("session_minutes")))
        except (TypeError, ValueError):
            pass
        self.sticky = text("sticky", self.sticky)
        try:
            self.max_per_group = max(1, int(settings.get("max_per_group")))
        except (TypeError, ValueError):
            pass

        wanted = settings.get("enabled")
        if wanted is not None:
            self.enabled = bool(wanted) and self.usable()
        # The groups are read again: a different country, or a different
        # number of accounts to an address, makes a different map.
        self._state = None

    # -- the groups ----------------------------------------------------

    def _load(self) -> dict:
        if self._state is not None:
            return self._state
        state = {"groups": {}}
        try:
            if GROUPS_FILE.exists():
                said = json.loads(GROUPS_FILE.read_text("utf-8"))
                if isinstance(said, dict) and isinstance(said.get("groups"), dict):
                    state = said
        except (OSError, ValueError):
            state = {"groups": {}}
        self._state = state
        return state

    def _save(self) -> None:
        """Write the groups down, with the permissions of a secret.

        The file says which visitor is in which group and which address
        each group was last seen at. It is not a password, but it is a
        map of who goes where, and it is written as one anyway.
        """
        if self._state is None:
            return
        try:
            STATE.mkdir(parents=True, exist_ok=True)
            GROUPS_FILE.write_text(json.dumps(self._state, indent=2), "utf-8")
            GROUPS_FILE.chmod(0o600)
        except OSError:
            pass

    def groups_for(self, country: str) -> list:
        """The groups that belong to a country, in the order they were made."""
        country = (country or self.country).upper()
        groups = self._load()["groups"]
        return [gid for gid, g in sorted(groups.items())
                if str(g.get("country", "")).upper() == country]

    def assign(self, visitor: str, country: str) -> str:
        """The group a visitor belongs to, made if there is none.

        A visitor keeps their group for good: it is in their profile, so a
        return visit finds the same group and the same address. A new
        visitor goes to the group with the fewest people in it, and a new
        group is made when every group of that country is full.
        """
        country = (country or self.country).upper()
        state = self._load()
        groups = state["groups"]

        for gid, group in groups.items():
            if visitor in group.get("members", []):
                return gid

        existing = self.groups_for(country)
        chosen = None
        for gid in existing:
            group = groups[gid]
            if len(group.get("members", [])) < self.max_per_group:
                chosen = gid
                break

        if chosen is None:
            index = len(existing)
            chosen = f"{country.lower()}-{index}"
            while chosen in groups:
                index += 1
                chosen = f"{country.lower()}-{index}"
            groups[chosen] = {
                "country": country,
                # The provider's session token: what makes the same
                # address come back. It is a secret of a sort, and it is
                # only ever seen by the provider.
                "session": f"{country.lower()}{secrets.token_hex(6)}",
                "members": [],
                "exit_ip": "",
                "exit_country": "",
                "seen": 0.0,
            }

        groups[chosen].setdefault("members", []).append(visitor)
        self._save()
        return chosen

    def group(self, group_id: str) -> dict:
        """What is known about a group."""
        return self._load()["groups"].get(group_id, {})

    def observe(self, group_id: str, ip: str, country: str) -> None:
        """Remember the address a group was last seen leaving from.

        A group that changes its address has lost its node, and that is
        worth saying out loud: a visitor whose address moves between the
        password and the code is how accounts get locked.
        """
        groups = self._load()["groups"]
        group = groups.get(group_id)
        if group is None:
            return
        group["exit_ip"] = ip or ""
        group["exit_country"] = (country or "").upper()
        group["seen"] = time.time()
        self._save()

    # -- the connection -------------------------------------------------

    def settings(self, group_id: str, country: str = "",
                 force: bool = False) -> dict | None:
        """The proxy settings Playwright wants, for a group.

        ``None`` means no proxy: the app talks to the internet itself.

        ``country`` says which country the address is to be in. Left out,
        the country the group was made for is used.
        """
        if not self.enabled and not force:
            return None
        group = self.group(group_id)
        session = str(group.get("session") or group_id)
        where = country or str(group.get("country") or self.country)

        if self.kind == "decodo":
            user = decodo_username(self.user, country=where, session=session,
                                   minutes=self.session_minutes,
                                   sticky=self.sticky)
        else:
            user = self.user

        host, _, port = self.server.partition(":")
        server = f"{self.scheme}://{host}"
        if port:
            server += f":{port}"
        return {"server": server, "username": user, "password": self.password}

    def check(self, group_id: str, country: str = "",
              force: bool = False) -> dict:
        """Ask the provider what address this group is leaving from.

        This is a quick check made from the machine, before the browser is
        involved. The browser's own check is the one that counts; this one
        is here to say whether the address is there at all before a
        session is started on it.
        """
        settings = self.settings(group_id, country=country, force=force)
        if not settings or not shutil.which("curl"):
            return {"ok": False, "ip": "", "country": "", "said": "no way to ask"}

        server = settings["server"]
        host = server.split("://", 1)[-1]
        command = ["curl", "-sS", "--max-time", "25"]
        if server.startswith("socks5"):
            # socks5-hostname asks the proxy to resolve the name, so the
            # machine's own resolver is not used. The ordinary --socks5
            # would resolve here, and the address it resolved to would be
            # the machine's business and not the visitor's.
            command += ["--socks5-hostname", host]
        else:
            command += ["-x", server]
        if settings.get("username"):
            command += ["-U", f"{settings['username']}:{settings.get('password', '')}"]
        command.append(EXIT_CHECK_URL)

        try:
            done = subprocess.run(command, capture_output=True, timeout=40)
        except (subprocess.TimeoutExpired, OSError) as exc:
            return {"ok": False, "ip": "", "country": "", "said": str(exc)}

        body = done.stdout.decode("utf-8", "replace").strip()
        if done.returncode != 0 or not body:
            said = done.stderr.decode("utf-8", "replace").strip().splitlines()
            return {"ok": False, "ip": "", "country": "",
                    "said": said[-1] if said else f"curl said {done.returncode}"}
        return parse_exit(body)


def _looks_like_address(body: str) -> bool:
    """Is this nothing but an address?

    A plain-text echo answers with the address and nothing else. A page
    that said no - an error page, a sign-in wall - is text that is not an
    address, and it must not be read as one.
    """
    said = (body or "").strip()
    if not said or len(said) > 60:
        return False
    try:
        ipaddress.ip_address(said)
    except ValueError:
        return False
    return True


def parse_exit(body: str) -> dict:
    """What an address-echo service said, in the shapes they say it.

    The services differ in what they call the fields; these are the names
    they use. A body that is not JSON is read as the address itself,
    which is what a plain-text echo gives.
    """
    said = {"ok": False, "ip": "", "country": "", "said": body[:200]}
    try:
        parsed = json.loads(body)
    except ValueError:
        # A plain-text echo gives the address and nothing else. It has to
        # look like an address: anything else is a page that said no.
        if _looks_like_address(body):
            said.update(ok=True, ip=body.strip())
        return said
    if not isinstance(parsed, dict):
        return said

    for field in ("ip", "query", "ipAddress", "ip_address"):
        value = parsed.get(field)
        if isinstance(value, str) and value:
            said["ip"] = value.strip()
            break
    for field in ("country_code", "countryCode", "country", "country_name"):
        value = parsed.get(field)
        if isinstance(value, str) and value:
            said["country"] = value.strip().upper()[:2]
            break
    said["ok"] = bool(said["ip"])
    return said


# The one pool the app uses.
POOL = Pool()

# The machine's own address, as the internet sees it. It is looked up
# once and remembered: a browser that left from this address instead of
# from its proxy is the failure this whole arrangement is about, and
# telling it apart needs to know what the failure looks like.
_own_address = ""


def origin_ip() -> str:
    """This machine's own public address, or "" when it cannot be told."""
    global _own_address
    if _own_address:
        return _own_address
    if not shutil.which("curl"):
        return ""
    try:
        done = subprocess.run(
            ["curl", "-sS", "--max-time", "20", EXIT_CHECK_URL],
            capture_output=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        return ""
    body = done.stdout.decode("utf-8", "replace").strip()
    if not body:
        return ""
    _own_address = parse_exit(body).get("ip", "")
    return _own_address


def missing(pool) -> list:
    """What a proxy needs and does not have.

    Said as words the panel can put in a sentence, because "it is off" on
    its own is the operator guessing.
    """
    said = []
    if pool.kind not in ("decodo", "generic"):
        said.append("which proxy it is")
    if not pool.server:
        said.append("the server and port")
    if not pool.user:
        said.append("the user name")
    if pool.kind == "decodo" and not pool.password:
        said.append("the password")
    return said


def settings_for(group_id: str) -> dict | None:
    """The proxy settings for a group, through the one pool."""
    return POOL.settings(group_id)


def fallback_country() -> str:
    """The country to use when the visitor's own country is not known."""
    return POOL.country
