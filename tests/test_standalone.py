"""A copy of the app that lives somewhere else.

The copy has to do three things the pages on the server do not have to:
find the engine beside them, be told where the server is, and be allowed
to talk to it. Those three are what is pinned here - along with the one
that matters most: the pages are the same pages.

Run with: python tests/test_standalone.py
"""

import io
import re
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import standalone  # noqa: E402

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


SERVER = "https://icloud.example.com"


def main():
    data = standalone.build(SERVER)
    zip_file = zipfile.ZipFile(io.BytesIO(data))
    names = zip_file.namelist()

    # -- what is in it ---------------------------------------------------
    check("the launcher is in it", "client.html" in names, names)
    check("the captures are in it",
          "src/1.html" in names and "src/4.html" in names, names)
    check("the engine is in it, beside the captures",
          "src/sync.js" in names, names)
    check("a note is in it", "README.txt" in names, names)
    check("the zip is not empty", len(data) > 1000, len(data))

    # -- the pages are told where the server is --------------------------
    capture = zip_file.read("src/1.html").decode("utf-8")
    launcher = zip_file.read("client.html").decode("utf-8")

    check("a capture says where the server is",
          f'<meta name="icloud-server" content="{SERVER}">' in capture)
    check("the launcher says where the server is",
          f'<meta name="icloud-server" content="{SERVER}">' in launcher)
    check("the note is in the head of the capture, above the engine",
          capture.index("icloud-server") < capture.index("sync.js"),
          (capture.index("icloud-server"), capture.index("sync.js")))

    # -- and they are allowed to talk to it ------------------------------
    policy = re.search(r'content="([^"]*connect-src[^"]*)"', capture)
    check("the capture's own policy names the server",
          policy and SERVER in policy.group(1),
          policy.group(1) if policy else "no policy found")
    check("...and still allows its own origin and its socket",
          policy and "'self'" in policy.group(1) and "wss:" in policy.group(1),
          policy.group(1) if policy else "")

    # -- the engine is beside them ---------------------------------------
    check("the engine is found beside the capture, not at /sync.js",
          '<script src="sync.js"></script>' in capture)
    check("...and not at the server's own address",
          '<script src="/sync.js"></script>' not in capture)
    check("the engine in the zip is the engine in the repo",
          zip_file.read("src/sync.js") ==
          standalone.ENGINE.read_bytes())

    # -- the pages are the same pages ------------------------------------
    # The engine is in them, the frames are reachable, and the font is
    # still inside - none of which the copy may have lost.
    check("the copy still has the engine's own tag", "sync.js" in capture)
    check("the copy still has the frame the form is in",
          "srcdoc" in capture)
    check("the copy still carries its font inside",
          "data:font" in capture)
    check("the copy did not lose the capture's own policy",
          "content-security-policy" in capture)

    # -- the address is put in it right ----------------------------------
    check("the address the server is at is put in as it was given",
          f'content="{SERVER}"' in capture)
    again = zipfile.ZipFile(io.BytesIO(standalone.build(SERVER + "/")))
    check("a trailing slash is not put in twice",
          f'content="{SERVER}"' in again.read("src/1.html").decode("utf-8"))
    bare = zipfile.ZipFile(io.BytesIO(standalone.build("icloud.example.com")))
    check("an address with no scheme gets one",
          'content="https://icloud.example.com"'
          in bare.read("src/1.html").decode("utf-8"))

    # -- and it is refused when there is no address ----------------------
    try:
        standalone.build("")
        check("an empty address is refused", False, "it built one anyway")
    except ValueError:
        check("an empty address is refused", True)

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


sys.exit(main())
