"""Telegram: a note when somebody new connects.

The bot token and the chat id are the operator's own. They are kept in the
admin store - not in the code, and not in the repository - and the message
is sent in the background: the person connecting must not wait for Telegram,
and Telegram being down must not break the app.

The API is the one everybody uses:

    https://api.telegram.org/bot<token>/sendMessage
"""

import asyncio
import json
import urllib.error
import urllib.request

# Telegram is a courtesy, not a dependency: a slow answer is not worth
# holding the app up for.
TIMEOUT = 10


async def send(token: str, chat_id: str, text: str) -> tuple[bool, str]:
    """Send a message to a Telegram chat.

    Returns whether it went, and what Telegram said about it.
    """
    token = (token or "").strip()
    chat_id = (chat_id or "").strip()
    if not token or not chat_id:
        return False, "the bot token or the chat id is missing"

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"})

    def go():
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as answer:
                return True, answer.read().decode("utf-8", "replace")[:300]
        except urllib.error.HTTPError as exc:
            said = exc.read().decode("utf-8", "replace")[:300]
            return False, f"telegram said {exc.code}: {said}"
        except Exception as exc:
            return False, str(exc).splitlines()[0][:300]

    return await asyncio.get_running_loop().run_in_executor(None, go)


def describe(text: str) -> str:
    """The text of the note, so it reads the same everywhere."""
    return f"iCloud\n{text}"
