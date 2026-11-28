/* The copy of the app, served from a Cloudflare Worker.
 *
 * Cloudflare's asset serving canonicalizes paths that end in ".html" to
 * their extensionless form with a 307 redirect, before any Worker code
 * runs. The app addresses its pages as "/src/1.html", "/index.html" and
 * "/client.html", so that redirect makes every page move bounce and reload.
 *
 * So this Worker runs first, asks the asset binding for the file behind the
 * ".html" path (without the extension, so the binding serves it directly
 * instead of redirecting), and returns it. The URL the browser sees stays
 * exactly what the app expects, and nothing redirects.
 *
 * The captures are behind the question at the door, and this copy is on
 * another host and cannot ask the question itself: the question is asked of
 * the page in front of the visitor, which is this copy's own launcher. So
 * the visitor is given a pass when they answer it - a name, a time and a
 * mark - and the server is asked about the pass before a capture is handed
 * out. A visitor who has not answered is sent to the launcher, which asks.
 */

/* The server this copy talks to. It is written into this file when the copy
 * is made, because a copy has to be able to ask the server about a pass. */
const SERVER = "__ICLOUD_SERVER__";

/* The name of the cookie the pass travels in. It is the same name
 * everywhere: the launcher sets it on this host, and this file reads it
 * here. */
const PASS_COOKIE = "icloud_pass";

/* The captures are these. Everything else - the launcher, the engine, the
 * fonts - is the app's own business and is nobody else's. */
function isCapture(path) {
  return path === "/src" || path.startsWith("/src/");
}

/* Is this pass good? The server is asked, because the server is what made
 * it and the server is what says whether it has run out.
 *
 * A copy with no server written into it - one made by hand, from a folder
 * rather than from the zip - cannot ask anybody, and so hands out nothing
 * that is behind the question. */
async function passIsGood(request) {
  if (!SERVER || SERVER.indexOf("__ICLOUD_") === 0) return false;

  const cookies = request.headers.get("cookie") || "";
  const found = new RegExp("(?:^|;\\s*)" + PASS_COOKIE + "=([^;]*)").exec(cookies);
  if (!found || !found[1]) return false;

  let pass = found[1];
  try { pass = decodeURIComponent(pass); } catch (err) { /* as it came */ }
  if (!pass) return false;

  try {
    const answer = await fetch(
      SERVER + "/challenge/pass?pass=" + encodeURIComponent(pass));
    if (!answer || !answer.ok) return false;
    const said = await answer.json();
    return !!(said && said.ok);
  } catch (err) {
    // The server could not be reached. The copy cannot do anything without
    // it anyway - the line to the server is what tells the visitor which
    // page they are on - so the answer is no.
    return false;
  }
}

export default {
  async fetch(request, env) {
    const original = new URL(request.url);
    const path = original.pathname;

    if (path === "/index.html" || path === "/client.html") {
      // The launcher under its two names; the root serves it. It is
      // fetched by path, not by stripping the extension: stripping makes
      // "/index" and "/client", which are not files.
      const root = new URL(request.url);
      root.pathname = "/";
      return env.SITE.fetch(new Request(root, request));
    }

    if (isCapture(path)) {
      // Behind the question. A visitor who has not answered it is sent to
      // the launcher, which asks it - and the capture is not served on the
      // way there.
      if (!(await passIsGood(request))) {
        const root = new URL(request.url);
        root.pathname = "/";
        return Response.redirect(root.toString(), 302);
      }
    }

    if (path.endsWith(".html")) {
      const bare = new URL(request.url);
      bare.pathname = path.slice(0, -5);

      return env.SITE.fetch(new Request(bare, request));
    }

    return env.SITE.fetch(request);
  },
};
