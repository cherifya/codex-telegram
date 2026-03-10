# Codex Telegram Bot

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

A Telegram bot that gives you remote access to the Codex CLI. Chat with Codex about your codebase from anywhere, with persistent per-project sessions, tool controls, and optional automation features.

## What It Does

- Chat naturally with Codex to inspect, edit, and explain code.
- Keep context across messages with automatic session persistence.
- Work from any device that has Telegram.
- Route webhook/scheduled events to Codex and deliver responses to Telegram.
- Enforce security boundaries: user allowlist, approved directory sandboxing, rate limits, and audit logging.

## Quick Start

### 1. Prerequisites

- Python 3.11+
- Codex CLI installed and authenticated (`codex login`)
- Telegram bot token from [@BotFather](https://t.me/botfather)

### 2. Install

```bash
git clone https://github.com/cherifya/codex-telegram.git
cd codex-telegram
make dev
```

### 3. Configure

```bash
cp .env.example .env
```

Minimum required values:

```bash
TELEGRAM_BOT_TOKEN=1234567890:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
TELEGRAM_BOT_USERNAME=my_codex_bot
APPROVED_DIRECTORY=/Users/yourname/projects
ALLOWED_USERS=123456789
```

### 4. Run

```bash
make run
# or
make run-debug
```

## Interaction Modes

### Agentic Mode (default)

Natural conversation mode.

Commands:
- `/start`
- `/new`
- `/status`
- `/verbose`
- `/repo`
- `/sync_threads` (only if `ENABLE_PROJECT_THREADS=true`)

### Classic Mode

Set `AGENTIC_MODE=false` for command-driven interaction.

Commands:
- `/start`, `/help`, `/new`, `/continue`, `/end`, `/status`
- `/cd`, `/ls`, `/pwd`, `/projects`, `/export`, `/actions`, `/git`
- `/sync_threads` (only if `ENABLE_PROJECT_THREADS=true`)

## Real-Time Output

Control execution detail in chat:

- `/verbose 0` - final response only
- `/verbose 1` - tools + concise live progress (default)
- `/verbose 2` - tools with input summaries + more detail

Optional Telegram draft streaming (private chats):

```bash
ENABLE_STREAM_DRAFTS=true
STREAM_DRAFT_INTERVAL=0.3
```

## Core Features

- Codex CLI JSON stream integration
- Session persistence per user + project directory
- Directory switching via `/repo` with auto-resume
- Tool allowlist/disallowlist controls
- Project-thread routing (private/group topic modes)
- File/image/voice handling features
- Git integration and quick actions (classic mode)
- Webhook server + scheduler + proactive notifications
- SQLite persistence, usage tracking, and audit logs

## Configuration Essentials

Required:

```bash
TELEGRAM_BOT_TOKEN=...
TELEGRAM_BOT_USERNAME=...
APPROVED_DIRECTORY=...
ALLOWED_USERS=123456789,987654321
```

Common Codex settings:

```bash
CODEX_CLI_PATH=
CODEX_HOME=
CODEX_MODEL=
CODEX_TIMEOUT_SECONDS=300
CODEX_MAX_COST_PER_USER=10.0
CODEX_MAX_BUDGET_USD=5.0
CODEX_YOLO=true
CODEX_EXTRA_ARGS=
CODEX_ALLOWED_TOOLS=Read,Write,Edit,Bash,Glob,Grep,LS,Task,MultiEdit,NotebookRead,NotebookEdit,WebFetch,WebSearch,TodoRead,TodoWrite,Skill
CODEX_DISALLOWED_TOOLS=
```

Platform/automation settings:

```bash
ENABLE_API_SERVER=false
API_SERVER_PORT=8080
ENABLE_SCHEDULER=false
NOTIFICATION_CHAT_IDS=123,456
GITHUB_WEBHOOK_SECRET=
WEBHOOK_API_SECRET=
```

Project thread settings:

```bash
ENABLE_PROJECT_THREADS=false
PROJECT_THREADS_MODE=private
PROJECTS_CONFIG_PATH=config/projects.yaml
PROJECT_THREADS_CHAT_ID=
PROJECT_THREADS_SYNC_ACTION_INTERVAL_SECONDS=1.1
```

Full reference: [`.env.example`](.env.example) and [docs/configuration.md](docs/configuration.md).

Compatibility note: legacy `CLAUDE_*` variable names are still accepted, but new setups should use `CODEX_*`.

## Troubleshooting

Bot does not respond:
- Verify `TELEGRAM_BOT_TOKEN` and `TELEGRAM_BOT_USERNAME`.
- Verify your Telegram user ID is in `ALLOWED_USERS`.
- Confirm `APPROVED_DIRECTORY` exists and is accessible.
- Check logs with `make run-debug`.

Codex requests fail:
- Run `codex login` on the bot host.
- Run `codex --version` and ensure it is on `PATH` (or set `CODEX_CLI_PATH`).
- If a response is huge, narrow the request scope (single file/smaller range).

If you ever saw errors like `Separator is not found, and chunk exceed the limit`, upgrade to the latest revision of this repo. Recent changes raise subprocess stream limits and handle oversized lines more safely.

High usage/cost:
- Lower `CODEX_MAX_COST_PER_USER` and `CODEX_MAX_BUDGET_USD`.
- Use tighter prompts and smaller file scopes.
- Monitor usage via `/status`.

## Security

Security controls include:

- Allowlisted user access (`ALLOWED_USERS`)
- Approved-directory sandboxing (`APPROVED_DIRECTORY`)
- Rate limiting and cost caps
- Path/command validation for tool calls
- Webhook signature/token verification
- Audit logging of actions and violations

See [SECURITY.md](SECURITY.md) for details.

## Development

```bash
make dev
make test
make lint
make format
make run-debug
```

Main entrypoint:

```bash
poetry run codex-telegram-bot
```

## Docs

- [docs/setup.md](docs/setup.md)
- [docs/configuration.md](docs/configuration.md)
- [docs/tools.md](docs/tools.md)
- [docs/README.md](docs/README.md)

## License

MIT - see [LICENSE](LICENSE).

## Acknowledgments

- OpenAI Codex CLI
- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot)
