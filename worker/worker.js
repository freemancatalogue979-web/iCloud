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
 * A capture is behind the question at the door, and the question is checked
 * by the server and not by this Worker. So a capture is handed out only to a
 * visitor who has answered it: the answer goes to the server, the server
 * says so with a pass, and this Worker asks the server whether the pass it
 * is holding is good before it gives the capture out. A visitor without a
 * good pass is given the door instead - the question, on a page of its own,
 * with nothing of the capture in it - and the capture is given out after
 * the answer, not before.
 *
 * The Worker asks the server at the address it was built with. A Worker
 * cannot fetch an address that is only a number: Cloudflare answers such a
 * request with "error code: 1003" - direct IP access is not allowed - so a
 * copy that is told to talk to the server by its number is built with the
 * name the server answers on. The visitor's own browser has no such
 * trouble: it reaches the server by its number, and the pages keep it.
 *
 * And a capture is for one browser and one session: the launcher says which
 * in the address of the capture, and the capture checks it against what
 * that browser knows about itself. So the names have to be there - a capture
 * asked for without them is a page that anybody may ask for, and it is not
 * served.
 */

/* Where the server is. The address is put in when the copy is built. */
const SERVER = "__ICLOUD_SERVER__";

/* The captures are these. Everything else - the launcher, the door, the
 * engine, the fonts - is the app's own business and is nobody else's. */
function isCapture(path) {
  return path === "/src" || path.startsWith("/src/");
}

/* A name a session or a browser could have. */
const NAME = /^[A-Za-z0-9_-]{8,64}$/;

/* Are the names in the address?
 *
 * A capture is for one browser and one session, and says so in its address:
 * the session id is the server's name for the browser that answered the
 * question, and the user id is that browser's own name for itself. The
 * launcher puts them there, and the capture checks them against what it
 * finds in that browser.
 *
 * A capture asked for without them is not a capture: it is a page that
 * anybody may ask for, and the whole point of the question at the door is
 * that a stranger cannot.
 */
function namedFor(url) {
  const uid = url.searchParams.get("uid") || "";
  const sid = url.searchParams.get("sid") || "";
  return NAME.test(uid) && NAME.test(sid);
}

/* The pass.
 *
 * The answer to the question at the door is checked by the server, and the
 * server says so with a pass: a name, a time and a mark. The visitor's
 * browser holds it - the door keeps it in a cookie - and this Worker asks
 * the server whether the pass it is holding is any good.
 */
function passOf(request, url) {
  const cookie = request.headers.get("cookie") || "";
  const match = /(?:^|;\s*)icloud_pass=([^;]*)/.exec(cookie);
  if (match) {
    try {
      return decodeURIComponent(match[1]);
    } catch (err) {
      return match[1];
    }
  }
  return url.searchParams.get("pass") || "";
}

/* Is this pass good?
 *
 * The server is the one that says. A pass whose session has gone - because
 * the server was restarted, or because the visitor has been gone long
 * enough - is not a pass any more, and the server says that too.
 *
 * A server that cannot be reached is a server that cannot say yes, and a
 * capture that cannot be vouched for is not handed out.
 */
async function passIsGood(pass) {
  if (!pass) return false;
  const where = SERVER.replace(/\/+$/, "") +
    "/challenge/pass?pass=" + encodeURIComponent(pass);
  try {
    const answer = await fetch(where);
    if (!answer.ok) return false;
    const said = await answer.json();
    return !!(said && said.ok);
  } catch (err) {
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
      // Behind the question, and for the browser the capture is for.
      //
      // The names in the address say which browser and which session the
      // capture is for, and the capture checks them against what it finds
      // in that browser. A capture asked for without them is a page that
      // anybody may ask for, and it is not served.
      if (!namedFor(original)) {
        const root = new URL(request.url);
        root.pathname = "/";
        root.search = "";
        return Response.redirect(root.toString(), 302);
      }

      // And behind the answer. The capture itself is only handed out to a
      // visitor whose answer the server has checked; everybody else is
      // given the door, which asks the question and comes back with a pass.
      if (!(await passIsGood(passOf(request, original)))) {
        const door = new URL(request.url);
        door.pathname = "/door";
        door.search = "";
        return env.SITE.fetch(new Request(door, request));
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
