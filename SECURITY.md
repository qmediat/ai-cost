# Security policy

## Supported versions

The latest release on PyPI (and the matching GitHub Release) receives security fixes.

## Reporting a vulnerability

Preferred: GitHub's private **[Report a vulnerability](https://github.com/qmediat/ai-cost/security/advisories/new)** form
(Security tab → Advisories). Alternatively email **security@qmediat.io** with the subject `[SECURITY] ai-cost`.
Do not open public issues for security reports. We acknowledge receipt within 48 hours and aim to publish a fix
within 7 days for high-severity issues.

## What the tool touches

- Reads only files on the machine it runs on: your own CLI transcripts and session logs, the usage log your programs
  write, and your configuration. Nothing is sent anywhere; there is no telemetry and no account.
- The network is touched in two cases only. The price drift check fetches the vendors' public pricing pages,
  identifying itself as `ai-cost/<version>`: on request (`prices check` / `prices update`), on the schedule you
  install, and by default in the background when a report finds the last check older than `auto_check_days`
  (7; set it to 0 to disable). `--github` queries GitHub through the `gh` CLI you are already logged into.
- The statements above describe the core. A plugin you configure (an entry point, the `plugins` list,
  `AI_COST_PLUGINS`, or the marker of a bundled build) is third-party code that runs in this process with its own
  file and network access; review a plugin as you would any other program you run.
- No dependencies outside the Python standard library; the release is built and published from GitHub Actions with
  Trusted Publishing, and every action in the workflows is pinned to a commit.
