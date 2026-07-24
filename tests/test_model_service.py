from __future__ import annotations

import fcntl
import json
import signal
from pathlib import Path

import pytest

from runtime.model_service import (
    ModelServiceError,
    ModelServiceManager,
    ServiceIdentity,
    load_service_config,
)
from runtime.models import ModelRegistry


def _write_config(root: Path) -> Path:
    binary = root / "llama-server"
    fast = root / "qwen.gguf"
    reason = root / "qwythos.gguf"
    for path in (binary, fast, reason):
        path.write_bytes(b"fixture")
    config = {
        "schema_version": 1,
        "binary": str(binary),
        "build_info": "test-build",
        "host": "127.0.0.1",
        "port": 8080,
        "startup_timeout_seconds": 10,
        "stop_timeout_seconds": 2,
        "profiles": {
            "fast-qwen": {
                "alias": "local-coder",
                "model": str(fast),
                "routes": ["local-fast", "local-plan", "local-review"],
                "arguments": ["--ctx-size", "32768", "--parallel", "1"],
            },
            "reason-qwythos": {
                "alias": "local-reason",
                "model": str(reason),
                "routes": ["local-reason"],
                "arguments": ["--ctx-size", "32768", "--parallel", "1"],
            },
        },
    }
    path = root / "model-services.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def _identity(
    alias: str,
    model_file: str,
    *,
    model_path: Path | None = None,
) -> ServiceIdentity:
    return ServiceIdentity(
        llama_alias=alias,
        model_file=model_file,
        model_path=str(model_path or Path("/models") / model_file),
        configured_context_tokens=32768,
        total_slots=1,
        build_info="test-build",
    )


def test_service_config_maps_each_logical_route_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LOCAL_CODER_MODEL_SERVICE_CONFIG", raising=False)
    config_path = _write_config(tmp_path)

    config = load_service_config(tmp_path, config_path)

    assert config.route_profiles == {
        "local-fast": "fast-qwen",
        "local-plan": "fast-qwen",
        "local-review": "fast-qwen",
        "local-reason": "reason-qwythos",
    }
    assert len(config.config_sha256) == 64


def test_ensure_route_is_noop_when_exact_profile_is_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _write_config(tmp_path)
    manager = ModelServiceManager(tmp_path, config_path=config_path)
    identity = _identity(
        "local-coder",
        "qwen.gguf",
        model_path=manager.config.profiles["fast-qwen"].model,
    )
    monkeypatch.setattr(manager, "current_identity", lambda: identity)
    monkeypatch.setattr(manager, "_discover_server_pid", lambda: 42)
    monkeypatch.setattr(manager, "_profile_command_matches", lambda *_: True)
    stopped: list[bool] = []
    monkeypatch.setattr(manager, "_stop_active_server", lambda: stopped.append(True))

    result = manager.ensure_route("local-plan")

    assert result["profile_id"] == "fast-qwen"
    assert result["switched"] is False
    assert result["service_identity"]["llama_alias"] == "local-coder"
    assert stopped == []


def test_switches_from_fast_to_reason_and_records_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _write_config(tmp_path)
    manager = ModelServiceManager(tmp_path, config_path=config_path)
    fast_identity = _identity(
        "local-coder",
        "qwen.gguf",
        model_path=manager.config.profiles["fast-qwen"].model,
    )
    reason_identity = _identity(
        "local-reason",
        "qwythos.gguf",
        model_path=manager.config.profiles["reason-qwythos"].model,
    )
    monkeypatch.setattr(manager, "current_identity", lambda: fast_identity)
    monkeypatch.setattr(manager, "_discover_server_pid", lambda: 42)
    monkeypatch.setattr(
        manager,
        "_profile_command_matches",
        lambda profile, _pid: profile.profile_id == "fast-qwen",
    )
    stopped: list[bool] = []
    monkeypatch.setattr(manager, "_stop_active_server", lambda: stopped.append(True))
    started: list[str] = []

    def start(profile: object) -> int:
        started.append(profile.profile_id)  # type: ignore[attr-defined]
        return 101

    monkeypatch.setattr(manager, "_start_profile", start)
    monkeypatch.setattr(manager, "_wait_for_profile", lambda *_: reason_identity)
    events: list[dict[str, object]] = []
    monkeypatch.setattr(
        manager,
        "_record_event",
        lambda event: events.append(dict(event)),
    )

    result = manager.ensure_route("local-reason")

    assert stopped == [True]
    assert started == ["reason-qwythos"]
    assert result["switched"] is True
    assert result["route"] == "local-reason"
    assert events[-1]["event"] == "switch_completed"
    assert events[-1]["from_profile"] == "fast-qwen"


def test_failed_switch_restores_previous_known_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _write_config(tmp_path)
    manager = ModelServiceManager(tmp_path, config_path=config_path)
    fast_identity = _identity(
        "local-coder",
        "qwen.gguf",
        model_path=manager.config.profiles["fast-qwen"].model,
    )
    monkeypatch.setattr(manager, "current_identity", lambda: fast_identity)
    monkeypatch.setattr(manager, "_discover_server_pid", lambda: 42)
    monkeypatch.setattr(
        manager,
        "_profile_command_matches",
        lambda profile, _pid: profile.profile_id == "fast-qwen",
    )
    monkeypatch.setattr(manager, "_stop_active_server", lambda: None)
    starts: list[str] = []

    def start(profile: object) -> int:
        starts.append(profile.profile_id)  # type: ignore[attr-defined]
        return 100 + len(starts)

    monkeypatch.setattr(manager, "_start_profile", start)
    waits = iter([ModelServiceError("load failed"), fast_identity])

    def wait(*_: object) -> ServiceIdentity:
        value = next(waits)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(manager, "_wait_for_profile", wait)
    monkeypatch.setattr(manager, "_record_event", lambda _: None)

    with pytest.raises(ModelServiceError, match="Model switch failed"):
        manager.ensure_route("local-reason")

    assert starts == ["reason-qwythos", "fast-qwen"]


def test_unrecognized_live_profile_is_not_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _write_config(tmp_path)
    manager = ModelServiceManager(tmp_path, config_path=config_path)
    unknown = _identity("other", "unknown.gguf")
    monkeypatch.setattr(manager, "current_identity", lambda: unknown)
    monkeypatch.setattr(manager, "_discover_server_pid", lambda: 42)
    monkeypatch.setattr(manager, "_profile_command_matches", lambda *_: False)

    with pytest.raises(ModelServiceError, match="unrecognized live"):
        manager.ensure_route("local-reason")


def test_unidentified_live_server_is_not_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _write_config(tmp_path)
    manager = ModelServiceManager(tmp_path, config_path=config_path)
    monkeypatch.setattr(manager, "current_identity", lambda: None)
    monkeypatch.setattr(manager, "_discover_server_pid", lambda: 42)

    with pytest.raises(ModelServiceError, match="unidentified live"):
        manager.ensure_route("local-reason")


def test_stale_state_does_not_hide_multiple_server_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _write_config(tmp_path)
    manager = ModelServiceManager(tmp_path, config_path=config_path)
    monkeypatch.setattr(manager, "_read_state", lambda: {"pid": 10})
    monkeypatch.setattr(manager, "_is_server_pid", lambda pid: pid in {10, 20})
    monkeypatch.setattr(
        Path,
        "iterdir",
        lambda self: iter((Path("/proc/10"), Path("/proc/20"))),
    )

    with pytest.raises(ModelServiceError, match="Multiple llama-server"):
        manager._discover_server_pid()


def test_stop_refuses_unrecognized_live_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _write_config(tmp_path)
    manager = ModelServiceManager(tmp_path, config_path=config_path)
    unknown = _identity("other", "unknown.gguf")
    monkeypatch.setattr(manager, "current_identity", lambda: unknown)
    monkeypatch.setattr(manager, "_discover_server_pid", lambda: 42)
    monkeypatch.setattr(manager, "_profile_command_matches", lambda *_: False)
    stopped: list[bool] = []
    monkeypatch.setattr(manager, "_stop_active_server", lambda: stopped.append(True))

    with pytest.raises(ModelServiceError, match="Refusing to stop"):
        manager.stop()

    assert stopped == []


def test_route_session_holds_lock_through_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _write_config(tmp_path)
    manager = ModelServiceManager(tmp_path, config_path=config_path)
    events: list[object] = []
    monkeypatch.setattr(
        "runtime.model_service.fcntl.flock",
        lambda _fd, operation: events.append(operation),
    )
    monkeypatch.setattr(
        manager,
        "_switch_locked",
        lambda profile_id, route: {
            "profile_id": profile_id,
            "route": route,
        },
    )

    with manager.route_session("local-reason") as evidence:
        assert evidence == {
            "profile_id": "reason-qwythos",
            "route": "local-reason",
        }
        assert events == [fcntl.LOCK_EX]

    assert events[-1] == fcntl.LOCK_UN


def test_route_session_interrupt_stops_managed_server_before_unlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _write_config(tmp_path)
    manager = ModelServiceManager(tmp_path, config_path=config_path)
    events: list[object] = []
    monkeypatch.setattr(
        "runtime.model_service.fcntl.flock",
        lambda _fd, operation: events.append(operation),
    )
    monkeypatch.setattr(
        manager,
        "_switch_locked",
        lambda profile_id, route: {
            "profile_id": profile_id,
            "route": route,
        },
    )
    monkeypatch.setattr(
        manager,
        "_stop_managed_after_interrupt_locked",
        lambda: events.append("stopped"),
    )

    with pytest.raises(KeyboardInterrupt):
        with manager.route_session("local-reason"):
            raise KeyboardInterrupt

    assert events == [fcntl.LOCK_EX, "stopped", fcntl.LOCK_UN]


def test_route_preparation_interrupt_stops_managed_server_before_unlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _write_config(tmp_path)
    manager = ModelServiceManager(tmp_path, config_path=config_path)
    events: list[object] = []
    monkeypatch.setattr(
        "runtime.model_service.fcntl.flock",
        lambda _fd, operation: events.append(operation),
    )

    def interrupt(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(manager, "_switch_locked", interrupt)
    monkeypatch.setattr(
        manager,
        "_stop_managed_after_interrupt_locked",
        lambda: events.append("stopped"),
    )

    with pytest.raises(KeyboardInterrupt):
        manager.ensure_route("local-reason")

    assert events == [fcntl.LOCK_EX, "stopped", fcntl.LOCK_UN]


def test_stop_managed_does_not_kill_unowned_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _write_config(tmp_path)
    manager = ModelServiceManager(tmp_path, config_path=config_path)
    stopped: list[int] = []
    monkeypatch.setattr(manager, "_stop_pid", lambda pid: stopped.append(pid))

    result = manager.stop_managed(reason="keyboard_interrupt")

    assert result["stopped"] is False
    assert stopped == []


def test_stop_managed_stops_recorded_exact_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _write_config(tmp_path)
    manager = ModelServiceManager(tmp_path, config_path=config_path)
    manager._write_state(
        {
            "config_sha256": manager.config.config_sha256,
            "pid": 42,
            "profile_id": "fast-qwen",
        }
    )
    identity = _identity(
        "local-coder",
        "qwen.gguf",
        model_path=manager.config.profiles["fast-qwen"].model,
    )
    monkeypatch.setattr(manager, "current_identity", lambda: identity)
    monkeypatch.setattr(manager, "_profile_command_matches", lambda *_: True)
    stopped: list[int] = []
    monkeypatch.setattr(
        manager,
        "_stop_pid_for_interrupt",
        lambda pid: stopped.append(pid),
    )
    events: list[dict[str, object]] = []
    monkeypatch.setattr(
        manager,
        "_record_event",
        lambda event: events.append(dict(event)),
    )

    result = manager.stop_managed(reason="keyboard_interrupt")

    assert result["stopped"] is True
    assert stopped == [42]
    assert not manager.state_path.exists()
    assert events[-1]["event"] == "managed_stopped"
    assert events[-1]["reason"] == "keyboard_interrupt"


def test_interrupt_stop_escalates_without_normal_twenty_second_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _write_config(tmp_path)
    manager = ModelServiceManager(tmp_path, config_path=config_path)
    signals: list[int] = []
    waits: list[int] = []
    monkeypatch.setattr(
        "runtime.model_service.os.kill",
        lambda _pid, sent_signal: signals.append(sent_signal),
    )

    def wait(_pid: int, timeout: int) -> bool:
        waits.append(timeout)
        return len(waits) == 3

    monkeypatch.setattr(manager, "_wait_for_exit", wait)

    manager._stop_pid_for_interrupt(42)

    assert signals == [signal.SIGINT, signal.SIGTERM, signal.SIGKILL]
    assert waits == [1, 2, 5]


def test_model_registry_delegates_route_preparation() -> None:
    calls: list[str] = []

    class Manager:
        def ensure_route(self, route: str) -> dict[str, object]:
            calls.append(route)
            return {"route": route, "switched": False}

    registry = ModelRegistry(service_manager=Manager())

    assert registry.prepare_route("local-reason") == {
        "route": "local-reason",
        "switched": False,
    }
    assert calls == ["local-reason"]


def test_model_registry_delegates_complete_route_session() -> None:
    calls: list[str] = []

    class Manager:
        def route_session(self, route: str):
            from contextlib import nullcontext

            calls.append(route)
            return nullcontext({"route": route, "leased": True})

    registry = ModelRegistry(service_manager=Manager())

    with registry.route_session("local-reason") as evidence:
        assert evidence == {"route": "local-reason", "leased": True}

    assert calls == ["local-reason"]


def test_exact_identity_with_untrusted_command_is_not_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _write_config(tmp_path)
    manager = ModelServiceManager(tmp_path, config_path=config_path)
    identity = _identity(
        "local-coder",
        "qwen.gguf",
        model_path=manager.config.profiles["fast-qwen"].model,
    )
    monkeypatch.setattr(manager, "current_identity", lambda: identity)
    monkeypatch.setattr(manager, "_discover_server_pid", lambda: 42)
    monkeypatch.setattr(manager, "_profile_command_matches", lambda *_: False)

    with pytest.raises(ModelServiceError, match="unrecognized live"):
        manager.ensure_route("local-reason")


def test_profile_arguments_cannot_override_bound_server_identity(
    tmp_path: Path,
) -> None:
    config_path = _write_config(tmp_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["profiles"]["fast-qwen"]["arguments"].extend(
        ["--model", str(tmp_path / "other.gguf")]
    )
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(ModelServiceError, match="reserved server option"):
        load_service_config(tmp_path, config_path)


def test_model_path_override_must_preserve_frozen_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _write_config(tmp_path)
    monkeypatch.setenv("LOCAL_CODER_FAST_MODEL", str(tmp_path / "other.gguf"))

    with pytest.raises(ModelServiceError, match="changes the model filename"):
        load_service_config(tmp_path, config_path)


def test_same_model_on_different_llama_build_is_not_recognized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _write_config(tmp_path)
    manager = ModelServiceManager(tmp_path, config_path=config_path)
    identity = ServiceIdentity(
        llama_alias="local-coder",
        model_file="qwen.gguf",
        model_path=str(manager.config.profiles["fast-qwen"].model),
        configured_context_tokens=32768,
        total_slots=1,
        build_info="different-build",
    )
    monkeypatch.setattr(manager, "current_identity", lambda: identity)
    monkeypatch.setattr(manager, "_discover_server_pid", lambda: 42)
    monkeypatch.setattr(manager, "_profile_command_matches", lambda *_: True)

    with pytest.raises(ModelServiceError, match="unrecognized live"):
        manager.ensure_route("local-reason")
