"""Install and repair local agent hook configuration."""

from __future__ import annotations

import json
import shlex
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CODEX_HOOK_SCRIPT = PROJECT_ROOT / "scripts" / "codex-signal-hook"
CLAUDE_CODE_HOOK_SCRIPT = PROJECT_ROOT / "scripts" / "claude-code-signal-hook"

CODEX_EVENTS = {
    "SessionStart": 5,
    "UserPromptSubmit": 5,
    "PreToolUse": 5,
    "PostToolUse": 5,
    "PermissionRequest": 10,
    "Stop": 5,
    "SessionEnd": 5,
}

CLAUDE_CODE_EVENTS = {
    "SessionStart": 5,
    "UserPromptSubmit": 5,
    "PreToolUse": 5,
    "PostToolUse": 5,
    "PostToolUseFailure": 5,
    "PreCompact": 5,
    "SubagentStart": 5,
    "SubagentStop": 5,
    "PermissionRequest": 10,
    "Notification": 5,
    "Stop": 5,
    "SessionEnd": 5,
}


@dataclass(frozen=True)
class AgentSpec:
    key: str
    name: str
    config_path: Path
    hook_script: Path
    events: dict[str, int]
    passes_event_arg: bool
    uses_matcher: bool = False


@dataclass(frozen=True)
class AgentStatus:
    spec: AgentSpec
    installed: bool
    config_exists: bool
    valid_json: bool
    missing_events: tuple[str, ...]
    broken_events: tuple[str, ...]
    message: str


@dataclass(frozen=True)
class InstallResult:
    status: AgentStatus
    changed: bool
    backup_path: Path | None


def supported_agents(home: Path | None = None) -> dict[str, AgentSpec]:
    home_dir = home or Path.home()
    return {
        "codex": AgentSpec(
            key="codex",
            name="Codex",
            config_path=home_dir / ".codex" / "hooks.json",
            hook_script=CODEX_HOOK_SCRIPT,
            events=CODEX_EVENTS,
            passes_event_arg=True,
        ),
        "claude-code": AgentSpec(
            key="claude-code",
            name="Claude Code",
            config_path=home_dir / ".claude" / "settings.json",
            hook_script=CLAUDE_CODE_HOOK_SCRIPT,
            events=CLAUDE_CODE_EVENTS,
            passes_event_arg=False,
            uses_matcher=True,
        ),
    }


def inspect_agent(spec: AgentSpec) -> AgentStatus:
    config_exists = spec.config_path.exists()
    config, valid_json = _load_json_config(spec.config_path)

    if not config_exists:
        return AgentStatus(
            spec=spec,
            installed=False,
            config_exists=False,
            valid_json=True,
            missing_events=tuple(spec.events),
            broken_events=(),
            message="config missing",
        )

    if not valid_json:
        return AgentStatus(
            spec=spec,
            installed=False,
            config_exists=config_exists,
            valid_json=False,
            missing_events=tuple(spec.events),
            broken_events=(),
            message="invalid JSON",
        )

    hooks = config.get("hooks") if isinstance(config, dict) else None
    if not isinstance(hooks, dict):
        return AgentStatus(
            spec=spec,
            installed=False,
            config_exists=config_exists,
            valid_json=True,
            missing_events=tuple(spec.events),
            broken_events=(),
            message="hooks missing",
        )

    missing = []
    broken = []
    for event in spec.events:
        entries = hooks.get(event)
        if entries is None:
            missing.append(event)
            continue
        if not _event_has_expected_hook(entries, spec, event, spec.events[event]):
            broken.append(event)

    installed = not missing and not broken
    if installed:
        message = "installed"
    elif missing and broken:
        message = f"{len(missing)} missing, {len(broken)} broken"
    elif missing:
        message = f"{len(missing)} missing"
    else:
        message = f"{len(broken)} broken"

    return AgentStatus(
        spec=spec,
        installed=installed,
        config_exists=config_exists,
        valid_json=True,
        missing_events=tuple(missing),
        broken_events=tuple(broken),
        message=message,
    )


def install_agent(spec: AgentSpec, *, backup: bool = True) -> InstallResult:
    before = inspect_agent(spec)
    config, valid_json = _load_json_config(spec.config_path)
    if not valid_json or not isinstance(config, dict):
        config = {}

    original_text = spec.config_path.read_text() if spec.config_path.exists() else None
    hooks = config.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
        config["hooks"] = hooks

    for event, timeout in spec.events.items():
        hooks[event] = _merge_event_groups(hooks.get(event), spec, event, timeout)

    new_text = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    if original_text == new_text:
        after = inspect_agent(spec)
        return InstallResult(status=after, changed=False, backup_path=None)

    backup_path = _backup_config(spec.config_path) if backup and spec.config_path.exists() else None
    spec.config_path.parent.mkdir(parents=True, exist_ok=True)
    spec.config_path.write_text(new_text)
    after = inspect_agent(spec)
    return InstallResult(status=after, changed=original_text != new_text, backup_path=backup_path)


def run_install_wizard(
    *,
    selected_agents: Iterable[str] | None = None,
    all_agents: bool = False,
    yes: bool = False,
    dry_run: bool = False,
    home: Path | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    out = stdout or sys.stdout
    input_stream = stdin or sys.stdin
    agents = supported_agents(home)
    statuses = [inspect_agent(spec) for spec in agents.values()]

    print("Signal Light hook installer", file=out)
    print("", file=out)
    for index, status in enumerate(statuses, start=1):
        marker = "ok" if status.installed else "needs repair"
        exists = "found" if status.config_exists else "missing"
        print(f"{index}. {status.spec.name}: {marker} ({status.message}; config {exists})", file=out)
        print(f"   {status.spec.config_path}", file=out)

    selected_keys = _resolve_selection(
        statuses,
        selected_agents=selected_agents,
        all_agents=all_agents,
        yes=yes,
        input_stream=input_stream,
        out=out,
    )
    if not selected_keys:
        print("", file=out)
        print("No agents selected.", file=out)
        return 0

    print("", file=out)
    for key in selected_keys:
        spec = agents[key]
        if dry_run:
            print(f"Would install/repair {spec.name}: {spec.config_path}", file=out)
            continue
        result = install_agent(spec)
        print(f"Installed {spec.name}: {result.status.message}", file=out)
        if result.backup_path is not None:
            print(f"  backup: {result.backup_path}", file=out)

    return 0


def _resolve_selection(
    statuses: list[AgentStatus],
    *,
    selected_agents: Iterable[str] | None,
    all_agents: bool,
    yes: bool,
    input_stream: TextIO,
    out: TextIO,
) -> list[str]:
    valid_keys = {status.spec.key for status in statuses}
    if selected_agents:
        selected = []
        for value in selected_agents:
            key = value.strip().lower()
            if key in {"claude", "claudecode"}:
                key = "claude-code"
            if key not in valid_keys:
                raise ValueError(f"Unsupported agent: {value}")
            selected.append(key)
        return selected

    if all_agents:
        return [status.spec.key for status in statuses]

    suggested = [status.spec.key for status in statuses if not status.installed]
    if yes:
        return suggested or [status.spec.key for status in statuses]

    default_text = ",".join(str(index) for index, status in enumerate(statuses, start=1) if not status.installed)
    if not default_text:
        default_text = "1-" + str(len(statuses))

    print("", file=out)
    print(f"Select agents to install/repair [{default_text}] (comma separated, or 'all'):", file=out)
    answer = input_stream.readline().strip()
    if not answer:
        answer = default_text
    return _parse_selection(answer, statuses)


def _parse_selection(answer: str, statuses: list[AgentStatus]) -> list[str]:
    normalized = answer.strip().lower()
    if normalized in {"all", "a", "*"}:
        return [status.spec.key for status in statuses]
    if normalized in {"none", "n", "skip", "q", "quit"}:
        return []

    selected: list[str] = []
    by_key = {status.spec.key: status.spec.key for status in statuses}
    by_key["claude"] = "claude-code"
    by_key["claudecode"] = "claude-code"

    for chunk in normalized.replace(" ", "").split(","):
        if not chunk:
            continue
        if "-" in chunk and chunk.replace("-", "").isdigit():
            start_text, end_text = chunk.split("-", 1)
            start = int(start_text)
            end = int(end_text)
            for number in range(start, end + 1):
                selected.append(_key_by_index(statuses, number))
            continue
        if chunk.isdigit():
            selected.append(_key_by_index(statuses, int(chunk)))
            continue
        if chunk not in by_key:
            raise ValueError(f"Unsupported selection: {chunk}")
        selected.append(by_key[chunk])

    return list(dict.fromkeys(selected))


def _key_by_index(statuses: list[AgentStatus], number: int) -> str:
    if number < 1 or number > len(statuses):
        raise ValueError(f"Selection index out of range: {number}")
    return statuses[number - 1].spec.key


def _load_json_config(path: Path) -> tuple[dict[str, object], bool]:
    try:
        parsed = json.loads(path.read_text())
    except FileNotFoundError:
        return {}, True
    except json.JSONDecodeError:
        return {}, False
    if not isinstance(parsed, dict):
        return {}, False
    return parsed, True


def _backup_config(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_path = path.with_name(path.name + f".bak-signal-light-install-{stamp}")
    shutil.copy2(path, backup_path)
    return backup_path


def _event_has_expected_hook(entries: object, spec: AgentSpec, event: str, timeout: int) -> bool:
    if not isinstance(entries, list):
        return False
    expected = _hook_command(spec, event)
    for group in entries:
        if not isinstance(group, dict):
            continue
        if spec.uses_matcher and group.get("matcher") != "":
            continue
        hooks = group.get("hooks")
        if not isinstance(hooks, list):
            continue
        for hook in hooks:
            if (
                isinstance(hook, dict)
                and hook.get("type") == "command"
                and hook.get("command") == expected
                and hook.get("timeout") == timeout
            ):
                return True
    return False


def _merge_event_groups(existing_entries: object, spec: AgentSpec, event: str, timeout: int) -> list[object]:
    merged: list[object] = []
    replacement = _hook_group(spec, event, timeout)
    replaced = False

    if not isinstance(existing_entries, list):
        return [replacement]

    for group in existing_entries:
        replacement_group, cleaned_group, had_signal_light_hook = _replace_signal_light_hooks(group, spec, replacement)
        if had_signal_light_hook:
            if replacement_group is not None:
                merged.append(replacement_group)
                replaced = True
            if cleaned_group is not None:
                merged.append(cleaned_group)
            continue
        merged.append(group)

    if not replaced:
        merged.append(replacement)

    return merged


def _replace_signal_light_hooks(
    group: object, spec: AgentSpec, replacement: dict[str, object]
) -> tuple[object | None, object | None, bool]:
    if not isinstance(group, dict):
        return None, group, False
    hooks = group.get("hooks")
    if not isinstance(hooks, list):
        return None, group, False

    replacement_hooks = list(replacement["hooks"])
    updated_hooks = []
    kept_hooks = []
    replaced = False
    for hook in hooks:
        if isinstance(hook, dict) and hook.get("type") == "command" and _is_signal_light_command(
            hook.get("command"), spec
        ):
            if not replaced:
                updated_hooks.extend(replacement_hooks)
                replaced = True
            continue
        kept_hooks.append(hook)
        updated_hooks.append(hook)

    if not replaced:
        return None, group, False
    if not kept_hooks:
        replacement_group = dict(group)
        replacement_group["hooks"] = replacement_hooks
        if "matcher" in replacement:
            replacement_group["matcher"] = replacement["matcher"]
        return replacement_group, None, True

    replacement_group = dict(group)
    replacement_group["hooks"] = updated_hooks
    if "matcher" in replacement:
        replacement_group["matcher"] = replacement["matcher"]

    cleaned_group = dict(group)
    cleaned_group["hooks"] = kept_hooks
    return replacement_group, cleaned_group, True


def _is_signal_light_command(command: object, spec: AgentSpec) -> bool:
    if not isinstance(command, str) or not command.strip():
        return False
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    if not parts:
        return False

    executable = Path(parts[0])
    return executable.name == spec.hook_script.name and executable.parent.name == "scripts"


def _hook_group(spec: AgentSpec, event: str, timeout: int) -> dict[str, object]:
    group: dict[str, object] = {
        "hooks": [
            {
                "type": "command",
                "command": _hook_command(spec, event),
                "timeout": timeout,
            }
        ]
    }
    if spec.uses_matcher:
        group["matcher"] = ""
    return group


def _hook_command(spec: AgentSpec, event: str) -> str:
    quoted_script = shlex.quote(str(spec.hook_script))
    if spec.passes_event_arg:
        return f"{quoted_script} {event}"
    return quoted_script
