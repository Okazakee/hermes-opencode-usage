# Hermes OpenCode Usage Monitor

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that monitors your **OpenCode Go** plan usage. It scrapes the workspace dashboard to track rolling, weekly, and monthly usage, and alerts you when thresholds are exceeded.

## Features

- 📊 Checks **rolling, weekly, and monthly** usage percentages
- ⏰ **Automatic periodic checks** via Hermes cron (self-managing)
- 🔔 **Threshold alerts** — configurable warning and critical levels
- 🔄 **Auto-refreshes** expired cookies when the server sends a new one
- 🛡️ **Detects stale cookies** and clears them so you know to refresh
- 🖥️ **CLI commands** for quick checks and configuration

## Prerequisites

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) installed
- An OpenCode Go workspace with a dashboard URL like `https://opencode.ai/workspace/wrk_{id}/go`
- An **auth cookie** from your browser (see setup)

## Installation

### Option 1: Clone into plugins directory

```bash
git clone https://github.com/Okazakee/hermes-opencode-usage.git \
  ~/.hermes/plugins/opencode-usage
```

### Option 2: Manual copy

```bash
git clone https://github.com/Okazakee/hermes-opencode-usage.git
cp hermes-opencode-usage/plugin.yaml hermes-opencode-usage/__init__.py \
  ~/.hermes/plugins/opencode-usage/
```

### Enable the plugin

Enable it in Hermes:

```bash
hermes plugins enable opencode-usage
```

Restart Hermes or run `/reload-plugins` if supported. Verify:

```bash
hermes plugins list | grep opencode-usage
```

## Setup

### 1. Get your auth cookie

1. Go to [https://opencode.ai](https://opencode.ai) and log in
2. Open **DevTools** → **Application** → **Cookies** → `opencode.ai`
3. Copy the value of the `auth` cookie

### 2. Find your workspace ID

Your workspace dashboard URL looks like:
```
https://opencode.ai/workspace/wrk_abc12345/go
```
The workspace ID is `abc12345`.

### 3. Configure the plugin

Via CLI:

```bash
hermes opencode-usage setup -w abc12345 -c "your-auth-cookie-here"
```

Or via the agent in chat:

> *"Configure OpenCode usage monitor with workspace ID abc12345 and my auth cookie"*

### 4. Verify it works

```bash
hermes opencode-usage check
```

You should see your rolling, weekly, and monthly usage percentages.

## Usage

### CLI commands

```bash
# Interactive setup wizard
hermes opencode-usage setup

# Quick non-interactive setup
hermes opencode-usage setup -w abc12345 -c "cookie"

# Show current config and last check status
hermes opencode-usage status

# Run a usage check immediately
hermes opencode-usage check

# Check with override params
hermes opencode-usage check -w abc12345 -c "cookie"
```

### In Hermes chat

Ask the agent:

> *"Check my OpenCode usage"*
> *"Configure OpenCode usage monitor with workspace abc12345"*
> *"How much of my OpenCode plan have I used this week?"*

### Automatic scheduling

When you configure the plugin via `setup`, it automatically creates a Hermes cron job that checks your usage periodically (default: every 6 hours). If usage is within limits, the check runs silently. If it exceeds a threshold, you get an alert in your configured delivery channel.

## Configuration

Settings are stored in `~/.hermes/plugins/opencode-usage/config.json`:

| Setting | Default | Description |
|---|---|---|
| `workspace_id` | — | Your OpenCode workspace ID |
| `auth_cookie` | — | Auth cookie from browser |
| `check_interval_hours` | `6` | How often to check (hours) |
| `alert_thresholds.warning` | `70` | Warning threshold (%) |
| `alert_thresholds.critical` | `90` | Critical threshold (%) |

## Security

- The **auth cookie** is stored in plaintext in `~/.hermes/plugins/opencode-usage/config.json`
- Make sure your `~/.hermes` directory has appropriate permissions (`chmod 700 ~/.hermes`)
- The cookie is only sent to `opencode.ai` — never exposed to third parties
- If the cookie expires, the plugin detects it automatically and clears it so you know to refresh

## Troubleshooting

### "Cookie expired" error

1. Go to [https://opencode.ai](https://opencode.ai) and log in again
2. Copy the new `auth` cookie from DevTools
3. Run `hermes opencode-usage setup -w YOUR_ID -c "new-cookie"`

### Plugin not showing up

- Ensure the plugin files are at `~/.hermes/plugins/opencode-usage/`
- Run `hermes plugins list` to verify it's enabled
- Restart Hermes completely

### "No workspace_id configured"

- Run `hermes opencode-usage setup -w YOUR_ID -c "cookie"`
- Or ask the agent to configure it

## Project structure

```
hermes-opencode-usage/
├── plugin.yaml           # Plugin manifest
├── __init__.py           # Full plugin implementation
├── README.md             # This file
├── LICENSE               # MIT license
└── .gitignore
```

## Contributing

Contributions welcome! Please open an issue or PR on GitHub.

## License

MIT
