/**
 * Compile the pages once before any test runs.
 *
 * `next dev` compiles a route the first time it is requested. With the suite
 * fully parallel, every worker's first test asked for "/" (and some for
 * "/login") while that compile was still running, and on a busy machine the
 * first assertion timed out: the same two or three tests failed on one run
 * and passed on the next. Asking for each page here, one at a time and with a
 * generous timeout, means the tests only ever see a page that is already
 * built.
 */
export default async function warmUp(): Promise<void> {
  const base = "http://localhost:3100";
  for (const path of ["/", "/login", "/upstox/callback"]) {
    const deadline = Date.now() + 180_000;
    for (;;) {
      try {
        const response = await fetch(`${base}${path}`);
        await response.text();
        break;
      } catch (error) {
        if (Date.now() > deadline) throw error;
        await new Promise((resolve) => setTimeout(resolve, 1000));
      }
    }
  }
}
