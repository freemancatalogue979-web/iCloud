"""The browser a session is, and the address it leaves from.

A visitor who comes back is the same visitor: the same profile, the same
cookies, the same "trust this browser" answer. For that to be true, the
browser they get has to be the same browser too - and it is, because the
identity of that browser is written into the profile the first time and
read back every time after.

The identity has two halves, and they are kept apart on purpose:

* the **device** - the screen, the pixels, the language, the time of day,
  the browser's own name for itself. This is what makes one session look
  like one machine.
* the **group** - the name of the exit address this visitor leaves from.
  This is what makes two visitors look like one household.

They are not the same thing and they must not change together. The device
is fixed for good. The group is fixed too, but a group holds several
visitors, because that is the whole point of the arrangement.

Everything here is written into the visitor's profile folder, next to the
cookies, and it holds no secret: the provider's password is read from the
environment when the browser starts, not kept here.
"""

import json
import os
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

# The file the identity is kept in, inside the visitor's profile.
IDENTITY_FILE = "identity.json"

# The screens a machine might have. They are the ordinary desktop sizes,
# because a screen that does not exist is a screen nobody has.
SCREENS = [
    (1920, 1080), (1600, 900), (1536, 864), (1440, 900),
    (1366, 768), (1680, 1050), (2560, 1440), (1280, 1024),
]

# The pixel ratios a desktop reports. Most are one; a few are a little
# more, because some machines run their screens scaled.
DPRS = [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.25, 1.5]

# The colour schemes a machine may be set to.
SCHEMES = ["light", "light", "light", "dark"]

# What a machine in a country says about itself: the language it shows
# and the time its clock keeps. A visitor whose browser says one country
# and whose address is in another is a contradiction, and contradictions
# are what the address and the device are both there to avoid.
PLACES = {
    "US": ("en-US", "America/New_York"), "GB": ("en-GB", "Europe/London"),
    "IE": ("en-IE", "Europe/Dublin"), "CA": ("en-CA", "America/Toronto"),
    "AU": ("en-AU", "Australia/Sydney"), "NZ": ("en-NZ", "Pacific/Auckland"),
    "DE": ("de-DE", "Europe/Berlin"), "FR": ("fr-FR", "Europe/Paris"),
    "ES": ("es-ES", "Europe/Madrid"), "IT": ("it-IT", "Europe/Rome"),
    "NL": ("nl-NL", "Europe/Amsterdam"), "BE": ("nl-BE", "Europe/Brussels"),
    "AT": ("de-AT", "Europe/Vienna"), "CH": ("de-CH", "Europe/Zurich"),
    "PL": ("pl-PL", "Europe/Warsaw"), "PT": ("pt-PT", "Europe/Lisbon"),
    "SE": ("sv-SE", "Europe/Stockholm"), "NO": ("nb-NO", "Europe/Oslo"),
    "DK": ("da-DK", "Europe/Copenhagen"), "FI": ("fi-FI", "Europe/Helsinki"),
    "CZ": ("cs-CZ", "Europe/Prague"), "RO": ("ro-RO", "Europe/Bucharest"),
    "HU": ("hu-HU", "Europe/Budapest"), "GR": ("el-GR", "Europe/Athens"),
    "TR": ("tr-TR", "Europe/Istanbul"), "RU": ("ru-RU", "Europe/Moscow"),
    "UA": ("uk-UA", "Europe/Kyiv"), "BG": ("bg-BG", "Europe/Sofia"),
    "IL": ("he-IL", "Asia/Jerusalem"), "AE": ("ar-AE", "Asia/Dubai"),
    "SA": ("ar-SA", "Asia/Riyadh"), "QA": ("ar-QA", "Asia/Qatar"),
    "ZA": ("en-ZA", "Africa/Johannesburg"), "EG": ("ar-EG", "Africa/Cairo"),
    "NG": ("en-NG", "Africa/Lagos"), "KE": ("en-KE", "Africa/Nairobi"),
    "MA": ("fr-MA", "Africa/Casablanca"), "IN": ("en-IN", "Asia/Kolkata"),
    "PK": ("en-PK", "Asia/Karachi"), "BD": ("bn-BD", "Asia/Dhaka"),
    "LK": ("si-LK", "Asia/Colombo"), "NP": ("ne-NP", "Asia/Kathmandu"),
    "JP": ("ja-JP", "Asia/Tokyo"), "KR": ("ko-KR", "Asia/Seoul"),
    "CN": ("zh-CN", "Asia/Shanghai"), "TW": ("zh-TW", "Asia/Taipei"),
    "HK": ("zh-HK", "Asia/Hong_Kong"), "SG": ("en-SG", "Asia/Singapore"),
    "MY": ("ms-MY", "Asia/Kuala_Lumpur"), "TH": ("th-TH", "Asia/Bangkok"),
    "VN": ("vi-VN", "Asia/Ho_Chi_Minh"), "ID": ("id-ID", "Asia/Jakarta"),
    "PH": ("en-PH", "Asia/Manila"), "BR": ("pt-BR", "America/Sao_Paulo"),
    "MX": ("es-MX", "America/Mexico_City"),
    "AR": ("es-AR", "America/Argentina/Buenos_Aires"),
    "CL": ("es-CL", "America/Santiago"), "CO": ("es-CO", "America/Bogota"),
    "PE": ("es-PE", "America/Lima"), "VE": ("es-VE", "America/Caracas"),
}


@dataclass
class Device:
    """What the browser says about the machine it is on."""

    # The browser's own name for itself. Empty means: say what the
    # browser really is, which is the truth and the safest thing.
    user_agent: str = ""
    platform: str = "Linux x86_64"
    screen: dict = field(default_factory=lambda: {"width": 1920, "height": 1080})
    dpr: float = 1.0
    locale: str = "en-US"
    timezone: str = "UTC"
    color_scheme: str = "light"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, said: dict) -> "Device":
        said = said or {}
        screen = said.get("screen") or {}
        return cls(
            user_agent=str(said.get("user_agent") or ""),
            platform=str(said.get("platform") or "Linux x86_64"),
            screen={"width": int(screen.get("width") or 1920),
                    "height": int(screen.get("height") or 1080)},
            dpr=float(said.get("dpr") or 1.0),
            locale=str(said.get("locale") or "en-US"),
            timezone=str(said.get("timezone") or "UTC"),
            color_scheme=str(said.get("color_scheme") or "light"),
        )


@dataclass
class Identity:
    """One visitor's device, and the group of address they leave from."""

    id: str
    group: str
    country: str
    device: Device
    assigned_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "group": self.group,
            "country": self.country,
            "device": self.device.to_dict(),
            "assigned_at": self.assigned_at,
        }

    @classmethod
    def from_dict(cls, said: dict) -> "Identity":
        said = said or {}
        return cls(
            id=str(said.get("id") or ""),
            group=str(said.get("group") or ""),
            country=str(said.get("country") or "").upper(),
            device=Device.from_dict(said.get("device")),
            assigned_at=float(said.get("assigned_at") or 0.0),
        )

    def summary(self) -> dict:
        """What the panel says about this identity."""
        return {
            "id": self.id,
            "group": self.group,
            "country": self.country,
            "screen": f"{self.device.screen['width']}x{self.device.screen['height']}",
            "dpr": self.device.dpr,
            "locale": self.device.locale,
            "timezone": self.device.timezone,
            "color_scheme": self.device.color_scheme,
            "user_agent": self.device.user_agent,
        }


def place_for(country: str) -> tuple:
    """The language and the clock of a country, when they are known."""
    return PLACES.get((country or "").upper(), ("en-US", "UTC"))


def make_device(country: str, seed: str = "") -> Device:
    """A device for a country: a screen, a language, a clock.

    The screen is picked from the ordinary ones, the pixel ratio from the
    ordinary ones, and the language and the clock from the country - so
    that the browser and the address agree with each other.
    """
    locale, timezone = place_for(country)
    chooser = secrets.SystemRandom()
    width, height = SCREENS[chooser.randrange(len(SCREENS))]
    dpr = DPRS[chooser.randrange(len(DPRS))]
    scheme = SCHEMES[chooser.randrange(len(SCHEMES))]
    return Device(
        user_agent="",           # the browser's own, until something says otherwise
        platform="Linux x86_64",
        screen={"width": width, "height": height},
        dpr=dpr,
        locale=locale,
        timezone=timezone,
        color_scheme=scheme,
    )


def path_of(profile: Path) -> Path:
    """Where the identity is kept, inside a visitor's profile."""
    return Path(profile) / IDENTITY_FILE


def load(profile: Path) -> Identity | None:
    """The identity a profile already has, if it has one."""
    try:
        said = json.loads(path_of(profile).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(said, dict):
        return None
    identity = Identity.from_dict(said)
    return identity if identity.id else None


def save(profile: Path, identity: Identity) -> None:
    """Keep the identity with the profile that has it."""
    try:
        Path(profile).mkdir(parents=True, exist_ok=True)
        path_of(profile).write_text(
            json.dumps(identity.to_dict(), indent=2), "utf-8")
    except OSError:
        pass


def assign(profile: Path, country: str, group: str) -> Identity:
    """The identity of a visitor's profile: made once, kept for good.

    A profile that already has one keeps it: the same screen, the same
    language, the same group, however many times the visitor comes back.
    That is what makes the browser a device rather than a costume, and
    what keeps the "trust this browser" answer in the profile true.
    """
    known = load(profile)
    if known is not None:
        return known

    identity = Identity(
        id=f"id_{secrets.token_hex(6)}",
        group=group,
        country=(country or "").upper(),
        device=make_device(country),
        assigned_at=time.time(),
    )
    save(profile, identity)
    return identity


def update_user_agent(profile: Path, real_user_agent: str) -> Identity | None:
    """Keep the browser's own name for itself, and remember it.

    The browser is the only one who knows which build of Chrome it is,
    and the client hints it sends agree with it. So the first time a
    visitor's browser opens, what it says about itself is written down,
    and it is what that browser says from then on.
    """
    identity = load(profile)
    if identity is None or not real_user_agent:
        return identity
    if identity.device.user_agent == real_user_agent:
        return identity
    identity.device.user_agent = real_user_agent
    save(profile, identity)
    return identity
