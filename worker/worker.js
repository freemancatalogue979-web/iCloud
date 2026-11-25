/* The copy of the app, served from a Cloudflare Worker.
 *
 * The files are served as they are: the launcher at "/", the captures at
 * "/src/...", the engine beside them. Nothing here rewrites a path.
 *
 * That is the whole point. A host that answers every path with the same
 * file - the single-page fallback, say - sends the visitor round in
 * circles for ever: the launcher asks for the capture, the host answers
 * with the launcher, and the visitor watches a page reload without end.
 * Nothing about that looks wrong from inside the page, and the one thing
 * this copy must never do is it.
 *
 * A path that is not there is a 404 and nothing else. See wrangler.toml.
 */

export default {
  async fetch(request, env) {
    return env.SITE.fetch(request);
  },
};
