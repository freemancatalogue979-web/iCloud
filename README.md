# iCloud

FastAPI on port 9002. Visit the page and it opens a browser, loads icloud.com
and clicks Sign In.

## Run

```bash
pip install -r requirements.txt
python server.py
```

Then open <http://localhost:9002>

Chromium is downloaded automatically on the first visit if it isn't already
there — the page shows a spinner while that happens. To do it up front instead:

```bash
python -m playwright install chromium
```

## The page

A spinner while it works, then the app icon. Nothing else.

## Files

- `server.py` — FastAPI, three routes: `/` `/start` `/status`
- `client.html` — the one page; calls `/start` on load

## Note

The app clicks Sign In and then stops. You type your Apple ID and two-factor
code yourself in the browser window that opened — it never touches your
password.
