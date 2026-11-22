/* Runs every suite in this directory and reports one total.
 *
 *   npm install   (once, pulls jsdom)
 *   npm test
 *
 * These exercise agent.js and view.html in a real DOM. They are the only
 * evidence the mirror works: no browser can be installed in the development
 * sandbox, so nothing here has ever run in Chromium.
 */
const { execFileSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const dir = __dirname;
const suites = fs
  .readdirSync(dir)
  .filter((f) => /^test-.*\.js$/.test(f))
  .sort();

let totalPassed = 0;
let totalFailed = 0;
const failures = [];

for (const suite of suites) {
  let out = "";
  let code = 0;
  try {
    out = execFileSync(process.execPath, [path.join(dir, suite)], {
      encoding: "utf8",
      timeout: 120000,
    });
  } catch (e) {
    code = e.status === undefined ? 1 : e.status;
    out = (e.stdout || "") + (e.stderr || "");
  }
  const match = out.match(/(\d+) passed, (\d+) failed/);
  const passed = match ? +match[1] : 0;
  const failed = match ? +match[2] : 1;
  totalPassed += passed;
  totalFailed += failed;
  const status = code === 0 && failed === 0 ? "ok  " : "FAIL";
  console.log(`${status} ${suite.padEnd(24)} ${passed} passed, ${failed} failed`);
  if (status === "FAIL") {
    failures.push(suite);
    for (const line of out.split("\n")) {
      if (line.startsWith("FAIL") || line.startsWith("     ")) console.log("       " + line);
    }
  }
}

console.log(`\n${totalPassed} passed, ${totalFailed} failed across ${suites.length} suites`);
if (failures.length) console.log("failing: " + failures.join(", "));
process.exit(totalFailed ? 1 : 0);
