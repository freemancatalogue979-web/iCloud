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
 */

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

    if (path.endsWith(".html")) {
      const bare = new URL(request.url);
      bare.pathname = path.slice(0, -5);

      return env.SITE.fetch(new Request(bare, request));
    }

    return env.SITE.fetch(request);
  },
};
