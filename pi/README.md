# pi-config

Portable [pi](https://pi.dev) configuration — the parts of my `~/.pi/agent/` worth
carrying between machines. **Contains no secrets, credentials, or session history.**

## What's here

| Path | What it is |
|------|------------|
| [`../secrets-guard/`](../secrets-guard/README.md) | The shared secret-leak guard (core + adapters). `install.sh` delegates to `../secrets-guard/install.sh`, which deploys the pi adapter + core into `~/.pi/agent/extensions/` (auto-discovered globally, no trust gate). |
| `extensions/cognee-first.ts` | **cognee-first**, two layers. `before_agent_start` appends the recall-first rule to every turn's system prompt; a `tool_call` gate blocks the session's *first* research tool call unless recall already ran — one block per session, fail-open, and `pi-subagents` children skip the gate (the parent already paid it). Twins ship for Claude Code (`../claude/hooks/cognee-{remind,gate}.sh`) and OpenCode (`../opencode/plugins/pnk-cognee-first.js`). |
| `mcp.json.example` | MCP server template (magellan, cognee, uptime-kuma, playwright, serena). `__HOME__`/`__MCP_HOST__` tokens + `${COGNEE_MCP_TOKEN}` env ref — no secrets or topology. |
| `settings.json.example` | The npm package list pi loads (`pi-mcp-adapter`, `pi-permission-system`, `pi-web-access`, `pi-subagents`, `rpiv-ask-user-question`) plus the default provider/model. Copy to `~/.pi/agent/settings.json` and adjust. `install.sh` does **not** write it — the default provider/model is per-machine. |
| `install.sh` | Deploys `extensions/` + `mcp.json` into `~/.pi/agent/`, backing up anything it overwrites. |

## What the guard blocks

Curated for **precision over coverage** — only patterns that are almost always a leak:

- Infisical value dumps: `infisical secrets get`, bare `infisical secrets`, unredirected `export`, `set`/`delete` without `>/dev/null`, `--plain`, `--value`.
- `curl` against secret APIs (`secrets/raw`, `universal-auth/login`) or with an inline `clientSecret`/`secretValue`.
- `set -x` / `bash -x` tracing an auth flow.
- `cat`/`head`/`less`/`strings`/… and `grep`/`sed`/`dd` of credential files (`.env`, `*.pem`, `*.key`, `id_rsa`, `credentials.json`, `*secret*.yaml`, …).
- Reading a Hermes/agent `data/config.yaml` (inline secrets).
- Bare `env`/`printenv`, `docker exec … env`, `pgrep -a`/`ps aux`/`ps -o args` (argv dumps), `/proc/<pid>/environ`.

It explicitly **allows** the safe forms the rules steer you toward: `infisical run`,
`--output-file`, `>/dev/null 2>&1`, `grep -q`/`-l`/`-c`, `awk -F= '{print $1}'`, `… | wc -c`,
`sed -i`, `printenv NAME | wc -c`. Docs/markdown/templates (`*.md`, `*.example`) stay readable.

## What is deliberately NOT here

Never committed (and never touched by `install.sh`) — these stay per-machine:

- `auth.json`, `keys.env` (provider auth / API keys)
- `models-store.json` (cached model catalog)
- `models.json` (custom provider definitions — carries a provider API key)
- `sessions/` (conversation history)

## Install on a new machine

```bash
git clone https://github.com/johnbuck/dotfiles ~/dotfiles
cd ~/dotfiles
git config core.hooksPath hooks    # turn on the repo's secret-scan hook
# MCP servers need the LAN addr of the host running them:
MCP_HOST=<mcp-host-lan-addr> ./pi/install.sh
# (without MCP_HOST, mcp.json is skipped — copy mcp.json.example by hand instead)
```

Extensions + `mcp.json` auto-load on the next `pi` start (or `/reload` in a running session).
`serena` uses `--project-from-cwd`, so it attaches to whatever repo you launch `pi` in.

## Parity

The guard is one shared codebase ([`../secrets-guard/`](../secrets-guard/README.md)): a
single rule core imported by the Claude, OpenCode, and pi adapters. `../secrets-guard/tests/parity.mjs`
runs the case battery through all three adapters plus the canonical bash reference and gates
agreement in CI (`.github/workflows/secrets-guard-parity.yml`). Edit a rule once in the core;
all three harnesses update.
