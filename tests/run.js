/* Runs every suite in this directory and reports one total.
 *
 *   npm install   (once, pulls jsdom)
 *   npm test
 *
 * The test-*.js suites exercise agent.js and view.html in a real DOM; the
 * test_*.py suites exercise server.py. They are the only evidence any of this
 * works: no browser can be installed in the development sandbox, so nothing
 * here has ever run in Chromium.
 */
const { execFileSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const dir = __dirname;
const root = path.join(dir, "..");

// Prefer the project virtualenv, which is the only environment with FastAPI and
// Playwright installed (PEP 668 blocks installing into the system python).
function findPython() {
  const candidates = [
    path.join(root, ".venv", "bin", "python"),
    path.join(root, ".venv", "Scripts", "python.exe"), // Windows
    "python3",
    "python",
  ];
  for (const c of candidates) {
    try {
      execFileSync(c, ["--version"], { stdio: "ignore" });
      return c;
    } catch (e) {
      /* try the next one */
    }
  }
  return null;
}

const python = findPython();
const entries = fs
  .readdirSync(dir)
  .filter((f) => /^test-.*\.js$/.test(f) || /^test_.*\.py$/.test(f))
  .sort()
  .map((f) => ({
    file: f,
    cmd: f.endsWith(".py") ? python : process.execPath,
    skip: f.endsWith(".py") && !python,
  }));

let totalPassed = 0;
let totalFailed = 0;
let skipped = 0;
const failures = [];

for (const suite of entries) {
  if (suite.skip) {
    skipped++;
    console.log(`skip ${suite.file.padEnd(24)} no python interpreter found`);
    continue;
  }
  let out = "";
  let code = 0;
  try {
    out = execFileSync(suite.cmd, [path.join(dir, suite.file)], {
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
  console.log(`${status} ${suite.file.padEnd(24)} ${passed} passed, ${failed} failed`);
  if (status === "FAIL") {
    failures.push(suite.file);
    for (const line of out.split("\n")) {
      if (line.startsWith("FAIL") || line.startsWith("     ")) console.log("       " + line);
    }
  }
}

const ran = entries.length - skipped;
console.log(`\n${totalPassed} passed, ${totalFailed} failed across ${ran} suites`);
if (failures.length) console.log("failing: " + failures.join(", "));
process.exit(totalFailed ? 1 : 0);
