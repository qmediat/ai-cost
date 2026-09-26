# ai-costs — what did the AI-assisted work cost?

`ai-cost` prices a block of work done with LLMs three ways: **real** (what you paid — subscriptions prorated to the
window plus pay-per-token API keys at list price), **API-only** (the same tokens at list price, as if no subscription
existed) and a **vendor** quote. It reads what is already on your disk — Claude Code transcripts, Codex CLI rollouts,
Gemini CLI and Grok Build sessions, a usage log any program can write — and sends nothing anywhere.

This npm package is the same program the Python distribution [`ai-costs`](https://pypi.org/project/ai-costs/) ships: a
single file run by **Python 3.9 or newer**, which must be on this machine (`python3`, or the path in `AI_COST_PYTHON`).
Installing the package runs nothing and downloads nothing.

```bash
npm install -g ai-costs          # the command is ai-cost
ai-cost install --init-config    # writes ~/.config/ai-cost/config.json and prints what to put in it
ai-cost doctor                   # every !! line names what is still missing
ai-cost report --hours 24 --all-projects
```

`npx ai-costs …` works for a one-off run; for the scheduled jobs (`ai-cost install --schedule-reports`) install it
globally, so the job keeps pointing at a file that stays where it is.

- Setup, step by step: https://github.com/qmediat/ai-cost/blob/main/docs/SETUP.md
- Everything else (commands, sources, configuration, the JSON contract): https://github.com/qmediat/ai-cost#readme
- Security: https://github.com/qmediat/ai-cost/blob/main/SECURITY.md

MIT © 2026 Quantum Media Technologies sp. z o.o.
