# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
uv sync

# Run tests
uv run pytest
uv run pytest tests/test_agent_signals.py::test_idle_signal_leaves_green_on  # single test

# Run the CLI (wrapper avoids __pycache__ pollution)
./scripts/signal-light list
./scripts/signal-light play working --dry-run
./scripts/signal-light test

# Install agent hooks
./scripts/install-hooks --all -y
```

## Architecture

The project is a **hardware signal light driver** for AI agent hooks. It drives a 3-color traffic light (green/yellow/red) via MCP2221A USB GPIO.

**Core data flow:**
1. Agent hooks (`codex_hook.py`, `claude_code_hook.py`) receive events from Codex/Claude Code and call `runtime.apply_session_signal(session_key, signal_name)`.
2. `runtime.py` maintains per-session state in `/private/tmp/signal-light/sessions.json` (configurable via `SIGNAL_LIGHT_STATE_DIR`), aggregates across all sessions by priority (`blocked > permission > attention > working > idle`), and drives the light.
3. `agent_signals.py` defines all lamp patterns as `AgentSignal` objects with `Frame` sequences — each signal is either a one-shot play or a `repeat=True` looping worker.
4. `hardware.py` wraps EasyMCP2221 GPIO; `LightMapping.from_env()` reads pin assignments from env vars.

**Worker process model:** Repeating signals (`working`, `blocked`, etc.) run as detached subprocesses (`python -m signal_light worker <signal_name>`). The main process spawns/stops workers via PID files in `STATE_DIR`. Three worker types exist: main worker, notice worker (session-end green flash), and sleep worker (idle auto-off after `SIGNAL_LIGHT_IDLE_SLEEP_SECONDS`, default 10 min).

**Session aggregation priority:**
```
red flashing (blocked) > yellow (permission) > yellow (attention/done) > working cycle > steady green
```
A `Stop`/`turn_end` only clears non-urgent states; `permission`/`blocked` survive until explicitly cleared.

**Key env vars:** `SIGNAL_LIGHT_GREEN_PIN`, `SIGNAL_LIGHT_YELLOW_PIN`, `SIGNAL_LIGHT_RED_PIN`, `SIGNAL_LIGHT_ACTIVE_LOW` (default 1 = active-low), `SIGNAL_LIGHT_STATE_DIR`, `SIGNAL_LIGHT_SESSION_TTL_SECONDS`, `SIGNAL_LIGHT_IDLE_SLEEP_SECONDS`, `SIGNAL_LIGHT_DRY_RUN` (set to `1` in hook env to suppress hardware writes without passing `--dry-run`).

**Dry-run mode:** Pass `--dry-run` to use a no-op hardware backend for testing without physical hardware.

**Virtual signal names:** `turn_end` and `idle_sleep` are not entries in `SIGNALS` but are valid `signal_name` values accepted by `apply_session_signal` and the `worker` subcommand. `turn_end` clears non-urgent session state (keeps `permission`/`blocked`); `idle_sleep` is the delayed auto-off timer.

**CLI clears session state on `idle`/`off`:** `cli.play_signal("idle")` and `cli.play_signal("off")` call `clear_session_state()` before applying the signal, wiping all per-session tracking. Hook calls go through `apply_session_signal` and do not clear state this way.

**Worker owner token:** Each worker process is tagged with a `sha256(PROJECT_ROOT|STATE_DIR)[:16]` token. `stop_worker` and orphan cleanup only kill workers that match this token, so multiple installs in different directories don't interfere with each other.

## Testing patterns

Tests use `RecordingLight` (defined in `test_agent_signals.py`) as a test double for the hardware. Runtime integration tests monkeypatch `runtime.apply_signal`, `runtime.STATE_DIR`, `runtime.SESSION_FILE`, and `runtime.LOCK_FILE` (pointing them at `tmp_path`) to isolate file I/O. Pass `speed=0.05` to signal `.play()` calls to keep tests fast.

## Adding a New Agent Integration

1. Create `signal_light/<agent>_hook.py` — parse the agent's event format, map events to signal names, call `runtime.apply_session_signal(session_key, signal_name)`.
2. Add a wrapper script in `scripts/` following the pattern of `claude-code-signal-hook`.
3. Register the script entry point in `pyproject.toml` under `[project.scripts]`.
4. Add hook installation support in `hook_installer.py`.
