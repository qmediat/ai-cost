#!/usr/bin/env node
// ai-cost from npm: the same single-file program the Python distribution ships (ai-cost.pyz, a zipapp), started with a
// Python 3.9+ found on this machine. Installing the package runs nothing and downloads nothing; this file only starts
// the program, passes termination signals on to it and ends the way it ended.
"use strict";

const { spawn, spawnSync } = require("node:child_process");
const { constants } = require("node:os");
const path = require("node:path");

const ZIPAPP = path.join(__dirname, "..", "ai-cost.pyz");
const MINIMUM = [3, 9]; // pyproject.toml requires-python (a test keeps the two equal)
const PROBE = `import sys; sys.exit(0 if sys.version_info >= (${MINIMUM.join(", ")}) else 1)`;
const GUIDE = "https://github.com/qmediat/ai-cost#install";
// Passed on to the program, as npm does for its scripts: a signal sent to the launcher alone (a supervisor's SIGINT or
// SIGTERM) must stop it too. A Ctrl-C at the terminal reaches the program through the process group as well, so it
// may hear SIGINT twice; it ends by the first.
const FORWARDED = ["SIGINT", "SIGTERM", "SIGHUP"];
const RERAISED = new Set(["SIGINT", "SIGTERM", "SIGHUP"]); // ended by one of these: the launcher ends the same way

/** The interpreters to try, in order: AI_COST_PYTHON alone when it is set, else the usual names on this platform. */
function candidates() {
  if (process.env.AI_COST_PYTHON) {
    return [[process.env.AI_COST_PYTHON]];
  }
  if (process.platform === "win32") {
    return [["py", "-3"], ["python3"], ["python"]];
  }
  return [["python3"], ["python"]];
}

/** The first candidate that starts and is new enough, as [command, ...leading arguments]; null if none is. */
function findPython() {
  for (const [command, ...lead] of candidates()) {
    const probe = spawnSync(command, [...lead, "-c", PROBE], { stdio: "ignore" });
    if (probe.status === 0) {
      return [command, ...lead];
    }
  }
  return null;
}

/** End as the program ended: its exit code, or its signal (re-raised when the shell should see it, else 128 + n). */
function finish(code, signal) {
  if (signal === null) {
    process.exit(code === null ? 1 : code);
  }
  if (RERAISED.has(signal)) {
    process.removeAllListeners(signal);
    process.kill(process.pid, signal);
    return;
  }
  process.exit(128 + (constants.signals[signal] || 0));
}

function run(python) {
  const [command, ...lead] = python;
  const child = spawn(command, [...lead, ZIPAPP, ...process.argv.slice(2)], { stdio: "inherit" });
  for (const signal of FORWARDED) {
    process.on(signal, () => child.kill(signal));
  }
  child.on("error", (error) => {
    process.stderr.write(`ai-cost: cannot start ${command}: ${error.message}\n`);
    process.exit(1);
  });
  child.on("exit", finish);
}

/** Why no interpreter was found: the one AI_COST_PYTHON names, or the usual names on this platform. */
function notFound() {
  const floor = MINIMUM.join(".");
  if (process.env.AI_COST_PYTHON) {
    return `AI_COST_PYTHON=${process.env.AI_COST_PYTHON} did not run as Python ${floor} or newer — fix or unset it (${GUIDE})`;
  }
  const tried = candidates()
    .map((candidate) => candidate.join(" "))
    .join(", ");
  return `needs Python ${floor} or newer (tried ${tried}); install it, or set AI_COST_PYTHON to its path — ${GUIDE}`;
}

const python = findPython();
if (python === null) {
  process.stderr.write(`ai-cost: ${notFound()}\n`);
  process.exitCode = 1;
} else {
  run(python);
}
