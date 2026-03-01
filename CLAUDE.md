# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Diet CLI is an AI-powered nutrition tracking assistant built on the Claude Agent SDK. It runs as an interactive REPL where Claude acts as an agent with MCP tools to query/manage a SQLite nutrition database, log meals, track weight, manage diet goals, and learn user preferences. The agent can also modify its own source code (self-improvement).

## Running

```bash
# Requires Claude SDK venv at /var/lib/claude-sdk/venv/bin/python
# with claude-agent-sdk and rich installed

# Via Nix (preferred):
nix develop
diet                      # Interactive REPL
diet "200g chicken breast" # Single query then REPL

# Direct:
python diet.py

# Initialize/seed database:
python init_db.py          # or: diet-init-db
```

Database lives at `~/.local/share/diet-db/diet.db`. Self-improvement log at `~/.local/share/diet-db/improvements.log`.

## Linting

```bash
ruff check . --fix
mypy *.py
```

## Architecture

```
CLI REPL (asyncio, Rich markdown output)
    ↓
ClaudeSDKClient (system prompt + goals context injected at startup)
    ↓
MCP Server with 22 tools (decorated with @tool())
    ↓
SQLite (foods, meal_log, preferences, goals, weight_log, cooking_records, cooking_combos, combo_items)
```

**Single-file design**: All tools, system prompt, and REPL loop live in `diet.py`. The `init_db.py` script handles initial schema creation and seed data separately.

**Nix flake**: Uses `writeShellScriptBin` pointing to live source — no rebuild needed for code changes. Depends on a system-level Claude SDK venv.

## Key Patterns

- **MCP tools** return `{"content": [{"type": "text", "text": ...}]}` format required by the SDK
- **All nutrition values** in the `foods` table are per 100g; the agent scales to user-requested amounts
- **Goals** are a single-row table (id=1) with merge-on-update semantics (non-zero values overwrite, zero/empty values preserve existing)
- **Preferences** use upsert on category+key composite; the agent saves them silently during conversation
- **Extra tables** (goals, weight_log) are created lazily via `ensure_extra_tables()` in `diet.py`, not in `init_db.py`
- **Cooking tables** (cooking_records, cooking_combos, combo_items) are created lazily via `ensure_cooking_tables()` with seed data auto-inserted on first run
- **Self-improvement tools** (`read_own_source`, `update_own_source`, `get_improvement_log`) let the agent modify `diet.py` at runtime with changes logged and activated on restart
- **System prompt** (~125 lines starting at `SYSTEM_PROMPT`) contains all agent behavior rules including autonomy modes for self-improvement
