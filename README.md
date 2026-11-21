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

A spinner while the browser starts, then a live remote view of it.

The view streams screenshots of the real page and forwards your clicks and
keystrokes back into it. Nothing is page-specific — there are no selectors for
any site's fields and no copy of any site's markup, so it works on whatever is
loaded.

## Files

- `server.py` — FastAPI: `/` `/start` `/status` `/view` `/shot.png` `/click` `/keys` `/key`
- `client.html` — the one page; calls `/start` on load, then iframes `/view`
- `view.html` — the remote view inside that iframe

## Note

The app clicks Sign In and then stops. You type your Apple ID and two-factor
code yourself in the browser window that opened — it never touches your
password.
