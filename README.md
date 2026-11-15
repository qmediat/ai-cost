<p align="left">
  <a href="https://www.qmediat.io/open-source?utm_source=oss-readme&utm_medium=ai-cost&utm_campaign=open-source">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/qmediat/.github/b35746f6b3c933d9eeb539033ef40ea9876349ae/assets/qmediat-wordmark-light.svg">
      <img src="https://raw.githubusercontent.com/qmediat/.github/b35746f6b3c933d9eeb539033ef40ea9876349ae/assets/qmediat-wordmark-badge.svg" alt="Quantum Media Technologies" height="40">
    </picture>
  </a>
</p>

# ai-cost

One command tells what the AI-assisted work of any project cost: it reads what Claude Code, Codex CLI, Gemini CLI and
Grok Build CLI already wrote to disk and the calls your own app reports — nothing is sent anywhere.

## Install

```bash
pipx install ai-costs        # or: npm install -g ai-costs
```

## Run

```bash
ai-cost report --session latest
```

| Answer | Question | Sample |
|---|---|---|
| Real cost | What did you pay? | 54.71 USD |
| API-only cost | The same tokens with no subscription | 175.02 USD |
| Vendor quote | What an outside firm would charge | profile `consultancy-eu` |

## Your own app

```bash
ai-cost log --provider openai --model gpt-5.5 --input 1200 --output 300 --ref job-42
```

One line per request, from a shell, Python, Node or any language that can append to a file.

## Supports · version 2.11.2

| | |
|---|---|
| Reads | Claude Code · Codex CLI · Gemini CLI · Grok Build CLI · GitHub usage report and counts · your own app's usage log |
| Prices | Anthropic · OpenAI · Google · xAI · DeepSeek · Alibaba · Xiaomi · GitHub |
| Runs on | macOS · Linux |
| Needs | No Claude Code, no account, no network |

## Docs

| | |
|---|---|
| [First 10 minutes](https://github.com/qmediat/ai-cost/blob/main/docs/SETUP.md) | docs/SETUP.md |
| [Commands](https://github.com/qmediat/ai-cost/blob/main/docs/COMMANDS.md) | docs/COMMANDS.md |
| [Real, API-only, vendor](https://github.com/qmediat/ai-cost/blob/main/docs/ANSWERS.md) | docs/ANSWERS.md |
| [Sources it reads](https://github.com/qmediat/ai-cost/blob/main/docs/SOURCES.md) | docs/SOURCES.md |
| [Your plans and prices](https://github.com/qmediat/ai-cost/blob/main/docs/CONFIGURATION.md) | docs/CONFIGURATION.md |
| [Report your own app's calls](https://github.com/qmediat/ai-cost/blob/main/docs/USAGE-LOG.md) | docs/USAGE-LOG.md |
| [JSON output](https://github.com/qmediat/ai-cost/blob/main/docs/JSON.md) | docs/JSON.md |
| [Limits](https://github.com/qmediat/ai-cost/blob/main/docs/LIMITS.md) | docs/LIMITS.md |
| [For AI agents](https://github.com/qmediat/ai-cost/blob/main/AGENTS.md) | AGENTS.md |

Python 3.9 or newer · [MIT](https://github.com/qmediat/ai-cost/blob/main/LICENSE) · [Quantum Media Technologies](https://www.qmediat.io/open-source?utm_source=oss-readme&utm_medium=ai-cost&utm_campaign=open-source) · [Trademarks](https://github.com/qmediat/ai-cost/blob/main/docs/TRADEMARKS.md)
