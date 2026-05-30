import io
import json
import os
from pathlib import Path

import pytest

from signal_light.agent_signals import SIGNALS
from signal_light import cli
from signal_light.codex_hook import CodexHookInput, choose_signal, session_key
from signal_light import hook_installer
from signal_light import runtime
from signal_light.runtime import aggregate_sessions, apply_session_signal


class RecordingLight:
    def __init__(self) -> None:
        self.states: list[tuple[bool, bool, bool]] = []
        self.brightness_states: list[tuple[float, float, float]] = []

    def write(self, *, green: bool = False, yellow: bool = False, red: bool = False) -> None:
        self.states.append((green, yellow, red))

    def write_brightness(self, *, green: float = 0.0, yellow: float = 0.0, red: float = 0.0) -> None:
        self.brightness_states.append((green, yellow, red))

    def off(self) -> None:
        self.write()


def test_idle_signal_leaves_green_on() -> None:
    light = RecordingLight()

    SIGNALS["idle"].play(light, speed=0.05)

    assert SIGNALS["idle"].repeat is False
    assert light.states[-1] == (True, False, False)


def test_working_signal_uses_soft_green_yellow_red_cycle() -> None:
    light = RecordingLight()

    SIGNALS["working"].play(light, speed=0.05, cycles=1)

    assert SIGNALS["working"].repeat is True
    assert len(light.brightness_states) == 27
    assert all(green > 0 and yellow == 0 and red == 0 for green, yellow, red in light.brightness_states[:9])
    assert all(green == 0 and yellow > 0 and red == 0 for green, yellow, red in light.brightness_states[9:18])
    assert all(green == 0 and yellow == 0 and red > 0 for green, yellow, red in light.brightness_states[18:27])
    assert light.brightness_states[0][0] < light.brightness_states[4][0]
    assert light.brightness_states[4][0] > light.brightness_states[8][0]


def test_attention_signal_flashes_yellow() -> None:
    light = RecordingLight()

    SIGNALS["attention"].play(light, speed=0.05, cycles=1)

    assert SIGNALS["attention"].repeat is True
    assert light.states[:2] == [(False, True, False), (False, False, False)]


def test_thinking_signal_uses_work_cycle() -> None:
    light = RecordingLight()

    SIGNALS["thinking"].play(light, speed=0.05, cycles=1)

    assert SIGNALS["thinking"].frames == SIGNALS["working"].frames
    assert len(light.brightness_states) == 27
    assert light.brightness_states[0] == (0.10, 0.0, 0.0)
    assert light.brightness_states[9] == (0.0, 0.10, 0.0)
    assert light.brightness_states[18] == (0.0, 0.0, 0.10)


def test_permission_signal_flashes_yellow() -> None:
    light = RecordingLight()

    SIGNALS["permission"].play(light, speed=0.05, cycles=1)

    assert SIGNALS["permission"].repeat is True
    assert light.states[:2] == [(False, True, False), (False, False, False)]


def test_session_end_returns_to_idle_green() -> None:
    light = RecordingLight()

    SIGNALS["session_end"].play(light, speed=0.05)

    assert light.states[-1] == (True, False, False)


def test_session_done_signal_briefly_flashes_green() -> None:
    light = RecordingLight()

    SIGNALS["session_done"].play(light, speed=0.05, cycles=1)

    assert SIGNALS["session_done"].repeat is False
    assert light.states[:2] == [(True, False, False), (False, False, False)]
    assert light.states[-1] == (False, False, False)


def test_codex_stop_maps_to_turn_end() -> None:
    signal = choose_signal(CodexHookInput(event_name="Stop", payload={}))

    assert signal == "turn_end"


def test_failed_payload_maps_to_blocked() -> None:
    signal = choose_signal(
        CodexHookInput(
            event_name="PostToolUse",
            payload={"status": "failed"},
        )
    )

    assert signal == "blocked"


def test_structured_error_payload_maps_to_blocked() -> None:
    signal = choose_signal(
        CodexHookInput(
            event_name="PostToolUse",
            payload={"error": {"message": "command failed"}},
        )
    )

    assert signal == "blocked"


def test_prompt_text_containing_error_does_not_map_to_blocked() -> None:
    signal = choose_signal(
        CodexHookInput(
            event_name="UserPromptSubmit",
            payload={"prompt": "please fix this error"},
        )
    )

    assert signal == "thinking"


def test_success_status_does_not_become_unknown_signal() -> None:
    signal = choose_signal(
        CodexHookInput(
            event_name="PostToolUse",
            payload={"status": "success"},
        )
    )

    assert signal == "tool_done"


def test_aggregate_keeps_attention_over_other_working_session() -> None:
    aggregate = aggregate_sessions(
        {
            "a": {"signal": "attention", "updated_at": 1},
            "b": {"signal": "working", "updated_at": 1},
        }
    )

    assert aggregate == "attention"


def test_aggregate_keeps_permission_over_attention_and_working() -> None:
    aggregate = aggregate_sessions(
        {
            "a": {"signal": "attention", "updated_at": 1},
            "b": {"signal": "working", "updated_at": 1},
            "c": {"signal": "permission", "updated_at": 1},
        }
    )

    assert aggregate == "permission"


def test_aggregate_returns_working_when_any_session_is_working() -> None:
    aggregate = aggregate_sessions(
        {
            "a": {"signal": "idle", "updated_at": 1},
            "b": {"signal": "tool_done", "updated_at": 1},
        }
    )

    assert aggregate == "working"


def test_aggregate_returns_idle_for_empty_sessions() -> None:
    assert aggregate_sessions({}) == "idle"


def test_session_key_prefers_payload_session_id() -> None:
    key = session_key(
        CodexHookInput(event_name="Stop", payload={"session_id": "session-a", "cwd": "/tmp/x"}),
        {},
    )

    assert key == "session-a"


def test_session_key_falls_back_to_cwd() -> None:
    key = session_key(
        CodexHookInput(event_name="Stop", payload={"cwd": "/tmp/project"}),
        {},
    )

    assert key == "cwd:/tmp/project"


def test_session_key_ignores_turn_id_and_uses_cwd() -> None:
    key = session_key(
        CodexHookInput(event_name="Stop", payload={"turn_id": "turn-a", "cwd": "/tmp/project"}),
        {"CODEX_TURN_ID": "turn-env"},
    )

    assert key == "cwd:/tmp/project"


def test_cli_codex_hook_uses_session_aware_path(monkeypatch) -> None:
    calls: list[tuple[str, str, bool, bool]] = []
    monkeypatch.setattr("sys.stdin", io.StringIO('{"session_id":"session-a","event":"Stop"}'))
    monkeypatch.setattr(
        cli,
        "play_hook_signal",
        lambda signal_name, *, session_key, dry_run=False, quiet=False: calls.append(
            (signal_name, session_key, dry_run, quiet)
        )
        or 0,
    )

    assert cli.main(["codex-hook", "--dry-run"]) == 0
    assert calls == [("turn_end", "session-a", True, True)]


def test_cli_codex_hook_without_event_uses_stdin_event(monkeypatch) -> None:
    calls: list[tuple[str, str, bool, bool]] = []
    monkeypatch.setattr("sys.stdin", io.StringIO('{"session_id":"session-a","event":"PermissionRequest"}'))
    monkeypatch.setattr(
        cli,
        "play_hook_signal",
        lambda signal_name, *, session_key, dry_run=False, quiet=False: calls.append(
            (signal_name, session_key, dry_run, quiet)
        )
        or 0,
    )

    assert cli.main(["codex-hook", "--dry-run"]) == 0
    assert calls == [("permission", "session-a", True, True)]


def test_apply_session_signal_preserves_attention_over_other_work(tmp_path, monkeypatch) -> None:
    applied: list[str] = []
    monkeypatch.setattr(runtime, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SESSION_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(runtime, "LOCK_FILE", tmp_path / "state.lock")
    monkeypatch.setattr(runtime, "apply_signal", lambda signal, speed=1.0: applied.append(signal.name))
    monkeypatch.setattr(cli, "apply_signal", lambda signal, speed=1.0: applied.append(signal.name))
    monkeypatch.setattr(cli, "apply_signal", lambda signal, speed=1.0: applied.append(signal.name))

    assert apply_session_signal("session-a", "attention") == "attention"
    assert apply_session_signal("session-b", "working") == "attention"

    assert applied == ["attention", "attention"]


def test_apply_session_signal_escalates_permission_over_attention(tmp_path, monkeypatch) -> None:
    applied: list[str] = []
    monkeypatch.setattr(runtime, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SESSION_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(runtime, "LOCK_FILE", tmp_path / "state.lock")
    monkeypatch.setattr(runtime, "apply_signal", lambda signal, speed=1.0: applied.append(signal.name))
    monkeypatch.setattr(cli, "apply_signal", lambda signal, speed=1.0: applied.append(signal.name))
    monkeypatch.setattr(cli, "apply_signal", lambda signal, speed=1.0: applied.append(signal.name))

    assert apply_session_signal("session-a", "attention") == "attention"
    assert apply_session_signal("session-b", "permission") == "permission"

    assert applied == ["attention", "permission"]


def test_apply_session_signal_removes_session_on_end(tmp_path, monkeypatch) -> None:
    applied: list[str] = []
    notices: list[str] = []
    monkeypatch.setattr(runtime, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SESSION_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(runtime, "LOCK_FILE", tmp_path / "state.lock")
    monkeypatch.setattr(runtime, "apply_signal", lambda signal, speed=1.0: applied.append(signal.name))
    monkeypatch.setattr(runtime, "apply_session_end_notice", lambda aggregate, speed=1.0: notices.append(aggregate))

    assert apply_session_signal("session-a", "working") == "working"
    assert apply_session_signal("session-a", "session_end") == "idle"

    assert runtime.read_session_snapshot() == {"aggregate": "idle", "sessions": {}}
    assert applied == ["working"]
    assert notices == ["idle"]


def test_apply_session_signal_notices_one_session_end_while_another_works(tmp_path, monkeypatch) -> None:
    applied: list[str] = []
    notices: list[str] = []
    monkeypatch.setattr(runtime, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SESSION_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(runtime, "LOCK_FILE", tmp_path / "state.lock")
    monkeypatch.setattr(runtime, "apply_signal", lambda signal, speed=1.0: applied.append(signal.name))
    monkeypatch.setattr(runtime, "apply_session_end_notice", lambda aggregate, speed=1.0: notices.append(aggregate))

    assert apply_session_signal("session-a", "working") == "working"
    assert apply_session_signal("session-b", "working") == "working"
    assert apply_session_signal("session-a", "session_end") == "working"

    assert applied == ["working", "working"]
    assert notices == ["working"]


def test_apply_session_signal_does_not_notice_unknown_session_end(tmp_path, monkeypatch) -> None:
    applied: list[str] = []
    notices: list[str] = []
    monkeypatch.setattr(runtime, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SESSION_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(runtime, "LOCK_FILE", tmp_path / "state.lock")
    monkeypatch.setattr(runtime, "apply_signal", lambda signal, speed=1.0: applied.append(signal.name))
    monkeypatch.setattr(runtime, "apply_session_end_notice", lambda aggregate, speed=1.0: notices.append(aggregate))

    assert apply_session_signal("missing-session", "session_end") == "idle"

    assert applied == ["idle"]
    assert notices == []


def test_apply_session_signal_keeps_red_alert_without_green_notice(tmp_path, monkeypatch) -> None:
    applied: list[str] = []
    notices: list[str] = []
    monkeypatch.setattr(runtime, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SESSION_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(runtime, "LOCK_FILE", tmp_path / "state.lock")
    monkeypatch.setattr(runtime, "apply_signal", lambda signal, speed=1.0: applied.append(signal.name))
    monkeypatch.setattr(runtime, "apply_session_end_notice", lambda aggregate, speed=1.0: notices.append(aggregate))

    assert apply_session_signal("session-a", "working") == "working"
    assert apply_session_signal("session-b", "permission") == "permission"
    assert apply_session_signal("session-a", "session_end") == "permission"

    assert applied == ["working", "permission"]
    assert notices == ["permission"]


def test_session_end_notice_restores_non_urgent_aggregate(monkeypatch) -> None:
    notices: list[float] = []
    monkeypatch.setattr(runtime, "start_notice_worker", lambda speed=1.0: notices.append(speed))

    runtime.apply_session_end_notice("working", speed=0.5)

    assert notices == [0.5]


def test_session_end_notice_does_not_cover_permission_alert(monkeypatch) -> None:
    applied: list[str] = []
    notices: list[float] = []
    monkeypatch.setattr(runtime, "apply_signal", lambda signal, speed=1.0: applied.append(signal.name))
    monkeypatch.setattr(runtime, "start_notice_worker", lambda speed=1.0: notices.append(speed))

    runtime.apply_session_end_notice("permission")

    assert applied == ["permission"]
    assert notices == []


def test_apply_signal_stops_in_flight_session_end_notice(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(runtime, "stop_notice_worker", lambda: calls.append("stop-notice"))
    monkeypatch.setattr(runtime, "stop_worker", lambda: calls.append("stop-worker"))
    monkeypatch.setattr(runtime, "_play_with_retries", lambda signal, speed=1.0: calls.append(signal.name))

    runtime.apply_signal(SIGNALS["idle"])

    assert calls == ["stop-notice", "stop-worker", "idle"]


def test_apply_repeating_signal_stops_in_flight_session_end_notice(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(runtime, "stop_notice_worker", lambda: calls.append("stop-notice"))
    monkeypatch.setattr(runtime, "_worker_matches", lambda _signal_name: False)
    monkeypatch.setattr(runtime, "stop_worker", lambda: calls.append("stop-worker"))
    monkeypatch.setattr(runtime, "start_worker", lambda signal_name, speed=1.0: calls.append(f"start:{signal_name}"))

    runtime.apply_signal(SIGNALS["permission"])

    assert calls == ["stop-notice", "stop-worker", "start:permission"]


def test_run_session_end_notice_worker_restores_latest_aggregate(monkeypatch) -> None:
    calls: list[str] = []
    clear_calls: list[tuple[object, int | None]] = []

    class FakeSignalLight:
        def __init__(self, _mapping: object) -> None:
            calls.append("connect")

        def __enter__(self) -> "FakeSignalLight":
            return self

        def __exit__(self, *_exc: object) -> None:
            calls.append("close")

        def write(self, *, green: bool = False, yellow: bool = False, red: bool = False) -> None:
            calls.append(f"write:{int(green)}{int(yellow)}{int(red)}")

        def off(self) -> None:
            calls.append("off")

    monkeypatch.setattr(runtime, "SignalLight", FakeSignalLight)
    monkeypatch.setattr(runtime.LightMapping, "from_env", lambda _env: object())
    monkeypatch.setattr(runtime, "stop_worker", lambda: calls.append("stop-worker"))
    monkeypatch.setattr(runtime, "_worker_pid_matches", lambda pid_file, expected_pid: True)
    monkeypatch.setattr(runtime, "_read_session_snapshot_unlocked", lambda: {"aggregate": "working", "sessions": {}})
    monkeypatch.setattr(runtime, "apply_signal_now", lambda signal, speed=1.0: calls.append(f"restore:{signal.name}"))
    monkeypatch.setattr(runtime.os, "getpid", lambda: 12345)
    monkeypatch.setattr(
        runtime,
        "_clear_worker_pid_file",
        lambda pid_file, expected_pid=None: clear_calls.append((pid_file, expected_pid))
        or calls.append("clear-notice"),
    )

    assert runtime.run_session_end_notice_worker(speed=0.05) == 0

    assert "stop-worker" in calls
    assert "restore:working" in calls
    assert calls[-1] == "clear-notice"
    assert clear_calls == [(runtime.NOTICE_PID_FILE, 12345)]


def test_run_session_end_notice_worker_restores_aggregate_after_notice_failure(monkeypatch) -> None:
    calls: list[str] = []

    class FailingSignalLight:
        def __init__(self, _mapping: object) -> None:
            calls.append("connect")

        def __enter__(self) -> "FailingSignalLight":
            return self

        def __exit__(self, *_exc: object) -> None:
            calls.append("close")

        def write(self, *, green: bool = False, yellow: bool = False, red: bool = False) -> None:
            raise runtime.SignalLightError("notice failed")

        def off(self) -> None:
            calls.append("off")

    monkeypatch.setattr(runtime, "SignalLight", FailingSignalLight)
    monkeypatch.setattr(runtime.LightMapping, "from_env", lambda _env: object())
    monkeypatch.setattr(runtime, "stop_worker", lambda: calls.append("stop-worker"))
    monkeypatch.setattr(runtime, "_worker_pid_matches", lambda pid_file, expected_pid: True)
    monkeypatch.setattr(runtime, "_read_session_snapshot_unlocked", lambda: {"aggregate": "working", "sessions": {}})
    monkeypatch.setattr(runtime, "apply_signal_now", lambda signal, speed=1.0: calls.append(f"restore:{signal.name}"))
    monkeypatch.setattr(runtime, "_clear_worker_pid_file", lambda pid_file, expected_pid=None: calls.append("clear-notice"))

    with pytest.raises(runtime.SignalLightError, match="notice failed"):
        runtime.run_session_end_notice_worker(speed=0.05)

    assert "stop-worker" in calls
    assert "restore:working" in calls
    assert calls[-1] == "clear-notice"


def test_session_end_notice_skip_restore_when_notice_pid_was_replaced(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(runtime, "_worker_pid_matches", lambda pid_file, expected_pid: False)
    monkeypatch.setattr(runtime, "_read_session_snapshot_unlocked", lambda: calls.append("read-snapshot"))
    monkeypatch.setattr(runtime, "apply_signal_now", lambda signal, speed=1.0: calls.append(f"restore:{signal.name}"))

    runtime._restore_session_end_notice(speed=0.05)

    assert calls == []


def test_clear_worker_pid_file_keeps_newer_pid_file(tmp_path) -> None:
    pid_file = tmp_path / "worker.json"
    pid_file.write_text('{"pid": 222}')

    runtime._clear_worker_pid_file(pid_file, expected_pid=111)

    assert pid_file.exists()

    runtime._clear_worker_pid_file(pid_file, expected_pid=222)

    assert not pid_file.exists()


def test_find_worker_pids_matches_signal_light_worker(monkeypatch) -> None:
    owner_token = runtime._worker_owner_token()

    class Result:
        returncode = 0
        stdout = f"""
          100 /usr/bin/python -m signal_light worker --owner-token {owner_token} working --speed 1.0
          101 /usr/bin/python -m other_module worker --owner-token {owner_token} working
          102 /usr/bin/python -m signal_light worker --owner-token {owner_token} session_done --speed 1.0
          103 /usr/bin/python -m signal_light worker --owner-token other-owner working --speed 1.0
          104 /usr/bin/python -m signal_light worker working --speed 1.0
        """

    monkeypatch.setattr(runtime.subprocess, "run", lambda *args, **kwargs: Result())
    monkeypatch.setattr(runtime.os, "getpid", lambda: 999)

    assert runtime._find_worker_pids({"working"}) == [100]
    assert runtime._find_worker_pids({"session_done"}) == [102]


def test_stop_worker_terminates_orphan_worker_process(tmp_path, monkeypatch) -> None:
    terminated: list[int] = []
    pid_file = tmp_path / "worker.json"
    pid_file.write_text('{"pid": 111}')

    monkeypatch.setattr(runtime, "_terminate", lambda pid: terminated.append(pid))
    monkeypatch.setattr(runtime, "_find_worker_pids", lambda signal_names: [222])

    runtime._stop_worker_process(pid_file=pid_file, orphan_signal_names={"working"})

    assert terminated == [111, 222]
    assert not pid_file.exists()


def test_cli_worker_accepts_session_done_signal(monkeypatch) -> None:
    calls: list[tuple[str, float]] = []
    monkeypatch.setattr(cli, "run_worker", lambda signal_name, speed=1.0: calls.append((signal_name, speed)) or 0)

    assert cli.main(["worker", "session_done", "--speed", "0.5"]) == 0

    assert calls == [("session_done", 0.5)]


def test_cli_worker_accepts_idle_sleep_signal(monkeypatch) -> None:
    calls: list[tuple[str, float]] = []
    monkeypatch.setattr(cli, "run_worker", lambda signal_name, speed=1.0: calls.append((signal_name, speed)) or 0)

    assert cli.main(["worker", "idle_sleep", "--speed", "1.0"]) == 0

    assert calls == [("idle_sleep", 1.0)]


def test_supported_agents_exposes_codex_and_claude_code(tmp_path) -> None:
    agents = hook_installer.supported_agents(home=tmp_path)

    assert set(agents) == {"codex", "claude-code"}
    assert agents["codex"].config_path == tmp_path / ".codex" / "hooks.json"
    assert agents["claude-code"].config_path == tmp_path / ".claude" / "settings.json"


def test_inspect_agent_marks_missing_config_as_needing_install(tmp_path) -> None:
    spec = hook_installer.supported_agents(home=tmp_path)["codex"]

    status = hook_installer.inspect_agent(spec)

    assert not status.installed
    assert status.message == "config missing"


def test_install_agent_writes_codex_hooks_and_backups_existing_file(tmp_path) -> None:
    spec = hook_installer.supported_agents(home=tmp_path)["codex"]
    spec.config_path.parent.mkdir(parents=True, exist_ok=True)
    existing_hook = {"hooks": [{"type": "command", "command": "echo keep-me", "timeout": 1}]}
    spec.config_path.write_text(json.dumps({"hooks": {"Stop": [existing_hook]}}, indent=2))

    result = hook_installer.install_agent(spec)

    assert result.status.installed
    assert result.backup_path is not None
    data = json.loads(spec.config_path.read_text())
    assert set(data["hooks"]) == {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PermissionRequest", "Stop", "SessionEnd"}
    assert existing_hook in data["hooks"]["Stop"]


def test_install_agent_replaces_existing_signal_light_hooks_but_keeps_other_hooks(tmp_path) -> None:
    spec = hook_installer.supported_agents(home=tmp_path)["claude-code"]
    spec.config_path.parent.mkdir(parents=True, exist_ok=True)
    existing = {
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": str(hook_installer.CLAUDE_CODE_HOOK_SCRIPT),
                            "timeout": 1,
                        }
                    ],
                    "matcher": "",
                },
                {
                    "hooks": [{"type": "command", "command": "echo keep-me", "timeout": 1}],
                    "matcher": "",
                },
            ]
        }
    }
    spec.config_path.write_text(json.dumps(existing, indent=2))

    hook_installer.install_agent(spec)

    data = json.loads(spec.config_path.read_text())
    stop_groups = data["hooks"]["Stop"]
    assert len(stop_groups) == 2
    assert stop_groups[0]["hooks"][0]["command"] == str(hook_installer.CLAUDE_CODE_HOOK_SCRIPT)
    assert stop_groups[0]["hooks"][0]["timeout"] == 5
    assert stop_groups[1]["hooks"][0]["command"] == "echo keep-me"


def test_install_agent_preserves_existing_hook_order_when_repairing(tmp_path) -> None:
    spec = hook_installer.supported_agents(home=tmp_path)["claude-code"]
    spec.config_path.parent.mkdir(parents=True, exist_ok=True)
    existing = {
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {"type": "command", "command": "echo before", "timeout": 1},
                        {
                            "type": "command",
                            "command": str(hook_installer.CLAUDE_CODE_HOOK_SCRIPT),
                            "timeout": 1,
                        },
                        {"type": "command", "command": "echo after", "timeout": 1},
                    ],
                    "matcher": "",
                }
            ]
        }
    }
    spec.config_path.write_text(json.dumps(existing, indent=2))

    hook_installer.install_agent(spec)

    data = json.loads(spec.config_path.read_text())
    hooks = data["hooks"]["Stop"][0]["hooks"]
    assert [hook["command"] for hook in hooks] == [
        "echo before",
        str(hook_installer.CLAUDE_CODE_HOOK_SCRIPT),
        "echo after",
    ]
    assert hooks[1]["timeout"] == 5


def test_inspect_agent_marks_wrong_timeout_as_broken(tmp_path) -> None:
    spec = hook_installer.supported_agents(home=tmp_path)["codex"]
    spec.config_path.parent.mkdir(parents=True, exist_ok=True)
    hook_command = f"{hook_installer.CODEX_HOOK_SCRIPT} PermissionRequest"
    spec.config_path.write_text(
        json.dumps(
            {
                "hooks": {
                    event: [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": f"{hook_installer.CODEX_HOOK_SCRIPT} {event}",
                                    "timeout": 5,
                                }
                            ]
                        }
                    ]
                    for event in hook_installer.CODEX_EVENTS
                }
            },
            indent=2,
        )
    )
    data = json.loads(spec.config_path.read_text())
    data["hooks"]["PermissionRequest"][0]["hooks"][0]["command"] = hook_command
    data["hooks"]["PermissionRequest"][0]["hooks"][0]["timeout"] = 5
    spec.config_path.write_text(json.dumps(data, indent=2))

    status = hook_installer.inspect_agent(spec)

    assert not status.installed
    assert status.broken_events == ("PermissionRequest",)


def test_hook_command_quotes_paths_with_spaces() -> None:
    spec = hook_installer.AgentSpec(
        key="codex",
        name="Codex",
        config_path=Path("/tmp/unused.json"),
        hook_script=Path("/tmp/signal light/scripts/codex-signal-hook"),
        events={},
        passes_event_arg=True,
    )

    command = hook_installer._hook_command(spec, "Stop")

    assert command == "'/tmp/signal light/scripts/codex-signal-hook' Stop"


def test_install_wizard_selects_missing_agents_by_default(tmp_path, monkeypatch) -> None:
    codex_spec = hook_installer.supported_agents(home=tmp_path)["codex"]
    claude_spec = hook_installer.supported_agents(home=tmp_path)["claude-code"]
    codex_spec.config_path.parent.mkdir(parents=True, exist_ok=True)
    codex_spec.config_path.write_text(json.dumps({"hooks": {}}, indent=2))

    written: list[str] = []

    def fake_install(spec, backup=True):
        written.append(spec.key)
        return hook_installer.InstallResult(
            status=hook_installer.inspect_agent(spec), changed=True, backup_path=None
        )

    monkeypatch.setattr(hook_installer, "install_agent", fake_install)

    stdin = io.StringIO("\n")
    stdout = io.StringIO()
    assert hook_installer.run_install_wizard(stdin=stdin, stdout=stdout, home=tmp_path, yes=True) == 0

    assert written == ["codex", "claude-code"]
    assert "Signal Light hook installer" in stdout.getvalue()


def test_install_wizard_supports_explicit_agent_selection(tmp_path, monkeypatch) -> None:
    selected: list[str] = []

    def fake_install(spec, backup=True):
        selected.append(spec.key)
        return hook_installer.InstallResult(
            status=hook_installer.inspect_agent(spec), changed=True, backup_path=None
        )

    monkeypatch.setattr(hook_installer, "install_agent", fake_install)

    assert hook_installer.run_install_wizard(selected_agents=["codex"], home=tmp_path, yes=True) == 0

    assert selected == ["codex"]


def test_install_hooks_cli_invokes_wizard(monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(hook_installer, "run_install_wizard", lambda **kwargs: calls.append(kwargs) or 0)

    assert cli.main(["install-hooks", "--agent", "codex", "--dry-run"]) == 0

    assert calls == [{"selected_agents": ["codex"], "all_agents": False, "yes": False, "dry_run": True}]


def test_apply_session_signal_clears_non_urgent_session_on_turn_end(tmp_path, monkeypatch) -> None:
    applied: list[str] = []
    notices: list[float] = []
    monkeypatch.setattr(runtime, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SESSION_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(runtime, "LOCK_FILE", tmp_path / "state.lock")
    monkeypatch.setattr(runtime, "apply_signal", lambda signal, speed=1.0: applied.append(signal.name))
    monkeypatch.setattr(runtime, "start_notice_worker", lambda speed=1.0: notices.append(speed))

    assert apply_session_signal("session-a", "working") == "working"
    assert apply_session_signal("session-a", "turn_end") == "idle"

    assert runtime.read_session_snapshot() == {"aggregate": "idle", "sessions": {}}
    assert applied == ["working"]
    assert notices == [1.0]


def test_session_done_notice_fires_when_other_sessions_still_working(tmp_path, monkeypatch) -> None:
    applied: list[str] = []
    notices: list[float] = []
    monkeypatch.setattr(runtime, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SESSION_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(runtime, "LOCK_FILE", tmp_path / "state.lock")
    monkeypatch.setattr(runtime, "apply_signal", lambda signal, speed=1.0: applied.append(signal.name))
    monkeypatch.setattr(runtime, "start_notice_worker", lambda speed=1.0: notices.append(speed))

    assert apply_session_signal("session-a", "working") == "working"
    assert apply_session_signal("session-b", "working") == "working"
    assert apply_session_signal("session-b", "turn_end") == "working"

    snapshot = runtime.read_session_snapshot()
    assert "session-a" in snapshot["sessions"]
    assert "session-b" not in snapshot["sessions"]
    assert applied == ["working", "working"]
    assert notices == [1.0]


def test_apply_session_signal_keeps_permission_on_turn_end(tmp_path, monkeypatch) -> None:
    applied: list[str] = []
    monkeypatch.setattr(runtime, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SESSION_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(runtime, "LOCK_FILE", tmp_path / "state.lock")
    monkeypatch.setattr(runtime, "apply_signal", lambda signal, speed=1.0: applied.append(signal.name))

    assert apply_session_signal("session-a", "permission") == "permission"
    assert apply_session_signal("session-a", "turn_end") == "permission"

    assert runtime.read_session_snapshot()["aggregate"] == "permission"
    assert applied == ["permission", "permission"]


def test_manual_idle_clears_all_session_state(tmp_path, monkeypatch) -> None:
    applied: list[str] = []
    monkeypatch.setattr(runtime, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SESSION_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(runtime, "LOCK_FILE", tmp_path / "state.lock")
    monkeypatch.setattr(runtime, "apply_signal", lambda signal, speed=1.0: applied.append(signal.name))
    monkeypatch.setattr(cli, "apply_signal", lambda signal, speed=1.0: applied.append(signal.name))

    assert apply_session_signal("session-a", "attention") == "attention"
    assert cli.play_signal("idle") == 0
    assert runtime.read_session_snapshot() == {"aggregate": "idle", "sessions": {}}
    assert applied == ["attention", "idle"]


def test_manual_off_clears_all_session_state(tmp_path, monkeypatch) -> None:
    applied: list[str] = []
    monkeypatch.setattr(runtime, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SESSION_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(runtime, "LOCK_FILE", tmp_path / "state.lock")
    monkeypatch.setattr(runtime, "apply_signal", lambda signal, speed=1.0: applied.append(signal.name))
    monkeypatch.setattr(cli, "apply_signal", lambda signal, speed=1.0: applied.append(signal.name))

    assert apply_session_signal("session-a", "permission") == "permission"
    assert cli.play_signal("off") == 0
    assert runtime.read_session_snapshot() == {"aggregate": "idle", "sessions": {}}
    assert applied == ["permission", "off"]


def test_session_turn_end_does_not_directly_start_sleep_worker(tmp_path, monkeypatch) -> None:
    applied: list[str] = []
    sleep_started: list[bool] = []
    monkeypatch.setattr(runtime, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SESSION_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(runtime, "LOCK_FILE", tmp_path / "state.lock")
    monkeypatch.setattr(runtime, "apply_signal", lambda signal, speed=1.0: applied.append(signal.name))
    monkeypatch.setattr(runtime, "start_sleep_worker", lambda: sleep_started.append(True))
    monkeypatch.setattr(runtime, "start_notice_worker", lambda speed=1.0: None)

    apply_session_signal("session-a", "working")
    apply_session_signal("session-a", "turn_end")

    assert applied == ["working"]
    assert sleep_started == []


def test_idle_signal_directly_starts_sleep_worker(monkeypatch) -> None:
    played: list[str] = []
    sleep_started: list[bool] = []
    monkeypatch.setattr(runtime, "stop_notice_worker", lambda: None)
    monkeypatch.setattr(runtime, "stop_sleep_worker", lambda: None)
    monkeypatch.setattr(runtime, "stop_worker", lambda: None)
    monkeypatch.setattr(runtime, "_play_with_retries", lambda signal, speed=1.0: played.append(signal.name))
    monkeypatch.setattr(runtime, "start_sleep_worker", lambda: sleep_started.append(True))

    runtime.apply_signal(SIGNALS["idle"])

    assert played == ["idle"]
    assert sleep_started == [True]


def test_non_idle_signal_does_not_start_sleep_worker(monkeypatch) -> None:
    played: list[str] = []
    sleep_started: list[bool] = []
    monkeypatch.setattr(runtime, "stop_notice_worker", lambda: None)
    monkeypatch.setattr(runtime, "stop_sleep_worker", lambda: None)
    monkeypatch.setattr(runtime, "stop_worker", lambda: None)
    monkeypatch.setattr(runtime, "_play_with_retries", lambda signal, speed=1.0: played.append(signal.name))
    monkeypatch.setattr(runtime, "start_sleep_worker", lambda: sleep_started.append(True))

    runtime.apply_signal(SIGNALS["session_end"])

    assert played == ["session_end"]
    assert sleep_started == []


def test_run_idle_sleep_worker_turns_off_lights_after_timeout(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SESSION_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(runtime, "LOCK_FILE", tmp_path / "state.lock")
    monkeypatch.setattr(runtime, "SLEEP_PID_FILE", tmp_path / "sleep-worker.json")
    monkeypatch.setattr(runtime, "IDLE_SLEEP_SECONDS", 0)

    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "sessions.json").write_text('{"sessions": {}}')
    (tmp_path / "sleep-worker.json").write_text(
        json.dumps({"pid": os.getpid(), "signal": "idle_sleep"})
    )

    played: list[str] = []
    monkeypatch.setattr(runtime, "stop_worker", lambda: None)
    monkeypatch.setattr(runtime, "_play_with_retries", lambda signal, speed=1.0: played.append(signal.name))

    result = runtime.run_idle_sleep_worker()

    assert result == 0
    assert played == ["off"]


def test_run_idle_sleep_worker_skips_if_no_longer_idle(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "STATE_DIR", tmp_path)
    monkeypatch.setattr(runtime, "SESSION_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(runtime, "LOCK_FILE", tmp_path / "state.lock")
    monkeypatch.setattr(runtime, "SLEEP_PID_FILE", tmp_path / "sleep-worker.json")
    monkeypatch.setattr(runtime, "IDLE_SLEEP_SECONDS", 0)

    import time
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "sessions.json").write_text(
        json.dumps({"sessions": {"s1": {"signal": "working", "updated_at": time.time()}}})
    )
    (tmp_path / "sleep-worker.json").write_text(
        json.dumps({"pid": os.getpid(), "signal": "idle_sleep"})
    )

    played: list[str] = []
    monkeypatch.setattr(runtime, "stop_worker", lambda: None)
    monkeypatch.setattr(runtime, "_play_with_retries", lambda signal, speed=1.0: played.append(signal.name))

    result = runtime.run_idle_sleep_worker()

    assert result == 0
    assert played == []


def test_new_signal_cancels_sleep_worker(monkeypatch) -> None:
    sleep_stopped: list[bool] = []
    monkeypatch.setattr(runtime, "stop_notice_worker", lambda: None)
    monkeypatch.setattr(runtime, "stop_sleep_worker", lambda: sleep_stopped.append(True))
    monkeypatch.setattr(runtime, "stop_worker", lambda: None)
    monkeypatch.setattr(runtime, "start_worker", lambda name, speed=1.0: None)

    runtime.apply_signal(SIGNALS["working"])

    assert sleep_stopped == [True]


def test_terminate_permission_error_raises_signal_light_error(monkeypatch) -> None:
    def fake_kill(_pid: int, sig: int) -> None:
        if sig == 0:
            return
        raise PermissionError("sandbox")

    monkeypatch.setattr(runtime.os, "kill", fake_kill)

    with pytest.raises(runtime.SignalLightError, match="Cannot stop existing signal worker"):
        runtime._terminate(12345)
