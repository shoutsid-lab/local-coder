"""Serial fail-closed llama.cpp profile switching for constrained hardware."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Mapping

DEFAULT_CONFIG_PATH = Path("profiles/model-services-v1.json")
STATE_DIRECTORY = Path(".local-coder/model-services")
INTERRUPT_GRACE_SECONDS = 1
TERMINATE_GRACE_SECONDS = 2
KILL_WAIT_SECONDS = 5


class ModelServiceError(RuntimeError):
    """Raised when a model service cannot be identified or switched safely."""


@dataclass(frozen=True)
class ServerProfile:
    """One trusted llama.cpp launch profile."""

    profile_id: str
    alias: str
    model: Path
    routes: tuple[str, ...]
    arguments: tuple[str, ...]
    context_tokens: int
    total_slots: int


@dataclass(frozen=True)
class ServiceConfig:
    """Validated serial llama.cpp service configuration."""

    binary: Path
    build_info: str
    host: str
    port: int
    startup_timeout_seconds: int
    stop_timeout_seconds: int
    profiles: Mapping[str, ServerProfile]
    route_profiles: Mapping[str, str]
    config_sha256: str


@dataclass(frozen=True)
class ServiceIdentity:
    """Bounded identity reported by one live llama.cpp server."""

    llama_alias: str
    model_file: str
    model_path: str
    configured_context_tokens: int
    total_slots: int
    build_info: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "llama_alias": self.llama_alias,
            "model_file": self.model_file,
            "model_path": self.model_path,
            "configured_context_tokens": self.configured_context_tokens,
            "total_slots": self.total_slots,
            "build_info": self.build_info,
        }


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelServiceError(f"Could not load {label}: {path}") from exc
    if not isinstance(value, dict):
        raise ModelServiceError(f"{label} must be a JSON object")
    return value


def _positive_integer(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ModelServiceError(f"{label} must be a positive integer")
    return value


def _expanded_path(value: Any, label: str, *, override: str | None = None) -> Path:
    selected = override if override is not None else value
    if not isinstance(selected, str) or not selected:
        raise ModelServiceError(f"{label} must be a nonempty path")
    return Path(selected).expanduser().resolve()


def _argument_value(
    arguments: tuple[str, ...],
    option: str,
    *,
    label: str,
) -> str:
    values: list[str] = []
    for index, argument in enumerate(arguments):
        if argument == option:
            if index + 1 >= len(arguments):
                raise ModelServiceError(f"{label} has no value")
            values.append(arguments[index + 1])
        elif argument.startswith(f"{option}="):
            values.append(argument.split("=", 1)[1])
    if len(values) != 1 or not values[0]:
        raise ModelServiceError(f"{label} must appear exactly once")
    return values[0]


def _argument_positive_integer(
    arguments: tuple[str, ...],
    option: str,
    *,
    label: str,
) -> int:
    raw = _argument_value(arguments, option, label=label)
    try:
        value = int(raw)
    except ValueError as exc:
        raise ModelServiceError(f"{label} must be a positive integer") from exc
    return _positive_integer(value, label)


def load_service_config(
    repository_root: Path,
    config_path: Path | None = None,
) -> ServiceConfig:
    """Load the trusted route-to-server launch mapping."""
    root = repository_root.resolve()
    path = (root / (config_path or DEFAULT_CONFIG_PATH)).resolve()
    raw = _load_json(path, "model service config")
    if raw.get("schema_version") != 1:
        raise ModelServiceError("Unsupported model service config schema")
    build_info = raw.get("build_info")
    if not isinstance(build_info, str) or not build_info:
        raise ModelServiceError("build_info must be a nonempty string")
    host = raw.get("host")
    if not isinstance(host, str) or not host:
        raise ModelServiceError("host must be a nonempty string")
    port = _positive_integer(raw.get("port"), "port")
    if port > 65535:
        raise ModelServiceError("port must be at most 65535")
    configured_binary = raw.get("binary")
    binary = _expanded_path(
        configured_binary,
        "binary",
        override=os.environ.get("LOCAL_CODER_LLAMA_SERVER"),
    )
    if binary.name != Path(str(configured_binary)).name:
        raise ModelServiceError("llama-server override changes the binary name")
    raw_profiles = raw.get("profiles")
    if not isinstance(raw_profiles, dict) or not raw_profiles:
        raise ModelServiceError("profiles must be a nonempty object")

    model_overrides = {
        "fast-qwen": os.environ.get("LOCAL_CODER_FAST_MODEL"),
        "reason-qwythos": os.environ.get("LOCAL_CODER_REASON_MODEL"),
    }
    profiles: dict[str, ServerProfile] = {}
    routes: dict[str, str] = {}
    for profile_id, value in raw_profiles.items():
        if not isinstance(profile_id, str) or not profile_id:
            raise ModelServiceError("profile IDs must be nonempty strings")
        if not isinstance(value, dict):
            raise ModelServiceError(f"profiles.{profile_id} must be an object")
        alias = value.get("alias")
        if not isinstance(alias, str) or not alias:
            raise ModelServiceError(f"profiles.{profile_id}.alias is invalid")
        configured_model = value.get("model")
        model = _expanded_path(
            configured_model,
            f"profiles.{profile_id}.model",
            override=model_overrides.get(profile_id),
        )
        if model.name != Path(str(configured_model)).name:
            raise ModelServiceError(
                f"profiles.{profile_id}.model override changes the model filename"
            )
        route_values = value.get("routes")
        if (
            not isinstance(route_values, list)
            or not route_values
            or any(not isinstance(route, str) or not route for route in route_values)
        ):
            raise ModelServiceError(f"profiles.{profile_id}.routes is invalid")
        argument_values = value.get("arguments")
        if not isinstance(argument_values, list) or any(
            not isinstance(argument, str) or not argument
            for argument in argument_values
        ):
            raise ModelServiceError(f"profiles.{profile_id}.arguments is invalid")
        expanded_arguments = tuple(
            str(Path(argument).expanduser()) if argument.startswith("~") else argument
            for argument in argument_values
        )
        reserved = {"--model", "--alias", "--host", "--port"}
        if any(
            argument in reserved
            or any(argument.startswith(f"{option}=") for option in reserved)
            for argument in expanded_arguments
        ):
            raise ModelServiceError(
                f"profiles.{profile_id}.arguments contains a reserved server option"
            )
        context_tokens = _argument_positive_integer(
            expanded_arguments,
            "--ctx-size",
            label=f"profiles.{profile_id}.arguments --ctx-size",
        )
        total_slots = _argument_positive_integer(
            expanded_arguments,
            "--parallel",
            label=f"profiles.{profile_id}.arguments --parallel",
        )
        profile = ServerProfile(
            profile_id=profile_id,
            alias=alias,
            model=model,
            routes=tuple(route_values),
            arguments=expanded_arguments,
            context_tokens=context_tokens,
            total_slots=total_slots,
        )
        profiles[profile_id] = profile
        for route in profile.routes:
            if route in routes:
                raise ModelServiceError(f"Route is mapped more than once: {route}")
            routes[route] = profile_id

    return ServiceConfig(
        binary=binary,
        build_info=build_info,
        host=host,
        port=port,
        startup_timeout_seconds=_positive_integer(
            raw.get("startup_timeout_seconds"), "startup_timeout_seconds"
        ),
        stop_timeout_seconds=_positive_integer(
            raw.get("stop_timeout_seconds"), "stop_timeout_seconds"
        ),
        profiles=profiles,
        route_profiles=routes,
        config_sha256=_canonical_hash(raw),
    )


class ModelServiceManager:
    """Own serial model switching and restore the previous known profile on failure."""

    def __init__(
        self,
        repository_root: Path,
        *,
        config_path: Path | None = None,
    ) -> None:
        self.root = repository_root.resolve()
        self.config = load_service_config(self.root, config_path)
        if config_path is None:
            from .role_profiles import (
                RoleProfileError,
                load_role_activation,
            )

            try:
                expected_hash = load_role_activation(
                    self.root
                ).model_service_config_sha256
            except RoleProfileError as exc:
                raise ModelServiceError(f"Role activation is invalid: {exc}") from exc
            if self.config.config_sha256 != expected_hash:
                raise ModelServiceError(
                    "Active model service config is not bound to the role activation"
                )
        self.state_root = (self.root / STATE_DIRECTORY).resolve()
        self.state_path = self.state_root / "state.json"
        self.lock_path = self.state_root / "switch.lock"
        self.events_path = self.state_root / "events.jsonl"

    @property
    def health_url(self) -> str:
        return f"http://{self.config.host}:{self.config.port}/health"

    @property
    def props_url(self) -> str:
        return f"http://{self.config.host}:{self.config.port}/props"

    def _request_json(self, url: str, *, timeout: float = 2.0) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                value = json.load(response)
        except (
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            OSError,
        ) as exc:
            raise ModelServiceError(f"Model service request failed: {url}") from exc
        if response.status != 200 or not isinstance(value, dict):
            raise ModelServiceError(f"Model service returned invalid data: {url}")
        return value

    def current_identity(self) -> ServiceIdentity | None:
        """Return the live server identity, or None when port 8080 is unavailable."""
        try:
            health = self._request_json(self.health_url)
        except ModelServiceError:
            return None
        if health.get("status") != "ok":
            return None
        props = self._request_json(self.props_url)
        alias = props.get("model_alias")
        model_path = props.get("model_path")
        context = props.get("default_generation_settings", {}).get("n_ctx")
        slots = props.get("total_slots")
        build = props.get("build_info")
        if not isinstance(alias, str) or not isinstance(model_path, str):
            raise ModelServiceError("Live llama.cpp identity is incomplete")
        if not isinstance(context, int) or not isinstance(slots, int):
            raise ModelServiceError("Live llama.cpp capacity identity is incomplete")
        if not isinstance(build, str):
            raise ModelServiceError("Live llama.cpp build identity is incomplete")
        return ServiceIdentity(
            llama_alias=alias,
            model_file=Path(model_path).name,
            model_path=model_path,
            configured_context_tokens=context,
            total_slots=slots,
            build_info=build,
        )

    def _profile_matches(
        self,
        profile: ServerProfile,
        identity: ServiceIdentity,
    ) -> bool:
        try:
            live_model = Path(identity.model_path).expanduser().resolve()
        except OSError:
            return False
        return (
            identity.llama_alias == profile.alias
            and identity.model_file == profile.model.name
            and live_model == profile.model
            and identity.configured_context_tokens == profile.context_tokens
            and identity.total_slots == profile.total_slots
            and identity.build_info == self.config.build_info
        )

    def _profile_command_matches(self, profile: ServerProfile, pid: int | None) -> bool:
        if pid is None:
            return False
        command = self._pid_command(pid)
        if not command:
            return False
        try:
            executable = Path(command[0]).expanduser().resolve()
        except OSError:
            return False
        if executable != self.config.binary:
            return False
        expected = tuple(self._launch_command(profile)[1:])
        return command[1:] == expected

    def _matching_profile(
        self,
        identity: ServiceIdentity | None,
        pid: int | None = None,
        *,
        require_command: bool = False,
    ) -> str | None:
        if identity is None:
            return None
        matches = [
            profile_id
            for profile_id, profile in self.config.profiles.items()
            if self._profile_matches(profile, identity)
            and (not require_command or self._profile_command_matches(profile, pid))
        ]
        if len(matches) > 1:
            raise ModelServiceError("Live model identity matches multiple profiles")
        return matches[0] if matches else None

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return {}
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write_state(self, value: Mapping[str, Any]) -> None:
        self.state_root.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.state_path)

    def _record_event(self, value: Mapping[str, Any]) -> None:
        self.state_root.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(value, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _pid_command(self, pid: int) -> tuple[str, ...]:
        try:
            content = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return ()
        return tuple(
            part.decode("utf-8", errors="replace")
            for part in content.split(b"\0")
            if part
        )

    def _is_server_pid(self, pid: int) -> bool:
        command = self._pid_command(pid)
        if not command or Path(command[0]).name != "llama-server":
            return False
        for index, argument in enumerate(command):
            if argument == "--port" and index + 1 < len(command):
                return command[index + 1] == str(self.config.port)
            if argument.startswith("--port="):
                return argument.split("=", 1)[1] == str(self.config.port)
        return self.config.port == 8080

    def _discover_server_pid(self) -> int | None:
        state_pid = self._read_state().get("pid")
        matches: list[int] = []
        if isinstance(state_pid, int) and self._is_server_pid(state_pid):
            matches.append(state_pid)
        for process_path in Path("/proc").iterdir():
            if process_path.name.isdigit():
                pid = int(process_path.name)
                if pid not in matches and self._is_server_pid(pid):
                    matches.append(pid)
        if len(matches) > 1:
            raise ModelServiceError(
                f"Multiple llama-server processes target port {self.config.port}"
            )
        return matches[0] if matches else None

    def _wait_for_exit(self, pid: int, timeout: int) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                reaped, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                reaped = 0
            if reaped == pid:
                return True
            try:
                state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[2]
            except (OSError, IndexError):
                return True
            if state == "Z":
                return True
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.1)
        return False

    def _stop_pid(self, pid: int) -> None:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        if not self._wait_for_exit(pid, self.config.stop_timeout_seconds):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                return
            if not self._wait_for_exit(pid, 5):
                raise ModelServiceError(f"Could not stop llama-server PID {pid}")

    def _stop_pid_for_interrupt(self, pid: int) -> None:
        """Stop a managed server promptly after an operator interrupt."""
        for interrupt_signal, timeout in (
            (signal.SIGINT, INTERRUPT_GRACE_SECONDS),
            (signal.SIGTERM, TERMINATE_GRACE_SECONDS),
            (signal.SIGKILL, KILL_WAIT_SECONDS),
        ):
            try:
                os.kill(pid, interrupt_signal)
            except ProcessLookupError:
                return
            if self._wait_for_exit(pid, timeout):
                return
        raise ModelServiceError(f"Could not interrupt llama-server PID {pid}")

    def _stop_active_server(self) -> None:
        pid = self._discover_server_pid()
        identity = self.current_identity()
        if pid is None:
            if identity is not None:
                raise ModelServiceError(
                    "A healthy server is present but its llama-server PID is unknown"
                )
            return
        self._stop_pid(pid)

    def _stop_managed_server_locked(self, *, reason: str) -> dict[str, Any]:
        """Stop only the llama.cpp process recorded as managed by local-coder."""
        state = self._read_state()
        pid = state.get("pid")
        profile_id = state.get("profile_id")
        if not isinstance(pid, int) or not isinstance(profile_id, str):
            return {
                "event": "managed_stop_skipped",
                "at_utc": datetime.now(UTC).isoformat(),
                "reason": reason,
                "stopped": False,
                "detail": "no managed llama-server is recorded",
            }
        if state.get("config_sha256") != self.config.config_sha256:
            raise ModelServiceError(
                "Refusing to stop a managed server from a different service config"
            )
        try:
            profile = self.config.profiles[profile_id]
        except KeyError as exc:
            raise ModelServiceError(
                f"Refusing to stop unknown managed profile: {profile_id}"
            ) from exc
        if not self._profile_command_matches(profile, pid):
            raise ModelServiceError(
                "Refusing to stop managed PID because its command no longer matches"
            )

        if reason == "keyboard_interrupt":
            self._stop_pid_for_interrupt(pid)
        else:
            self._stop_pid(pid)
        self.state_path.unlink(missing_ok=True)
        identity = ServiceIdentity(
            llama_alias=profile.alias,
            model_file=profile.model.name,
            model_path=str(profile.model),
            configured_context_tokens=profile.context_tokens,
            total_slots=profile.total_slots,
            build_info=self.config.build_info,
        )
        event = {
            "event": "managed_stopped",
            "at_utc": datetime.now(UTC).isoformat(),
            "reason": reason,
            "stopped": True,
            "pid": pid,
            "profile_id": profile_id,
            "service_identity": identity.to_dict() if identity else None,
        }
        self._record_event(event)
        return event

    def _stop_managed_after_interrupt_locked(self) -> None:
        """Best-effort interrupt cleanup without replacing KeyboardInterrupt."""
        try:
            event = self._stop_managed_server_locked(reason="keyboard_interrupt")
            if not event.get("stopped"):
                self._record_event(event)
        except Exception as exc:
            try:
                self._record_event(
                    {
                        "event": "managed_interrupt_stop_failed",
                        "at_utc": datetime.now(UTC).isoformat(),
                        "error": str(exc),
                    }
                )
            except Exception:
                pass

    def _launch_command(self, profile: ServerProfile) -> list[str]:
        return [
            str(self.config.binary),
            "--model",
            str(profile.model),
            "--alias",
            profile.alias,
            "--host",
            self.config.host,
            "--port",
            str(self.config.port),
            *profile.arguments,
        ]

    def _start_profile(self, profile: ServerProfile) -> int:
        if not self.config.binary.is_file():
            raise ModelServiceError(
                f"llama-server binary does not exist: {self.config.binary}"
            )
        if not profile.model.is_file():
            raise ModelServiceError(f"Model file does not exist: {profile.model}")
        self.state_root.mkdir(parents=True, exist_ok=True)
        log_path = self.state_root / f"{profile.profile_id}.log"
        with log_path.open("ab", buffering=0) as log_handle:
            process = subprocess.Popen(
                self._launch_command(profile),
                cwd=self.root,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self._write_state(
            {
                "config_sha256": self.config.config_sha256,
                "pid": process.pid,
                "profile_id": profile.profile_id,
                "started_at_utc": datetime.now(UTC).isoformat(),
            }
        )
        return process.pid

    def _wait_for_profile(self, profile: ServerProfile, pid: int) -> ServiceIdentity:
        deadline = time.monotonic() + self.config.startup_timeout_seconds
        last_error: str | None = None
        while time.monotonic() < deadline:
            if not self._pid_command(pid):
                raise ModelServiceError(
                    f"llama-server exited while loading {profile.profile_id}"
                )
            try:
                identity = self.current_identity()
                if (
                    identity is not None
                    and self._profile_matches(profile, identity)
                    and self._profile_command_matches(profile, pid)
                ):
                    return identity
                if identity is not None:
                    last_error = (
                        f"identity mismatch: {identity.llama_alias}/"
                        f"{identity.model_file}"
                    )
            except ModelServiceError as exc:
                last_error = str(exc)
            time.sleep(0.25)
        detail = f" ({last_error})" if last_error else ""
        raise ModelServiceError(
            f"Timed out starting model profile {profile.profile_id}{detail}"
        )

    def _model_fingerprint(self, profile: ServerProfile) -> dict[str, Any]:
        try:
            stat = profile.model.stat()
        except OSError:
            return {
                "model_path": str(profile.model),
                "model_size_bytes": None,
                "model_mtime_ns": None,
                "model_sha256": None,
            }
        sha_path = Path(f"{profile.model}.sha256")
        model_sha256: str | None = None
        try:
            first = sha_path.read_text(encoding="utf-8").split()[0]
            if len(first) == 64 and all(
                char in "0123456789abcdefABCDEF" for char in first
            ):
                model_sha256 = first.lower()
        except (OSError, IndexError):
            pass
        return {
            "model_path": str(profile.model),
            "model_size_bytes": stat.st_size,
            "model_mtime_ns": stat.st_mtime_ns,
            "model_sha256": model_sha256,
        }

    def _switch_evidence(
        self,
        *,
        route: str | None,
        profile: ServerProfile,
        identity: ServiceIdentity,
        switched: bool,
        duration_seconds: float,
    ) -> dict[str, Any]:
        return {
            "route": route,
            "profile_id": profile.profile_id,
            "switched": switched,
            "duration_seconds": duration_seconds,
            "config_sha256": self.config.config_sha256,
            "service_identity": identity.to_dict(),
            **self._model_fingerprint(profile),
        }

    def _switch_locked(self, profile_id: str, *, route: str | None) -> dict[str, Any]:
        try:
            desired = self.config.profiles[profile_id]
        except KeyError as exc:
            raise ModelServiceError(
                f"Unknown model service profile: {profile_id}"
            ) from exc
        started = time.monotonic()
        current = self.current_identity()
        current_pid = self._discover_server_pid()
        if (
            current is not None
            and self._profile_matches(desired, current)
            and self._profile_command_matches(desired, current_pid)
        ):
            return self._switch_evidence(
                route=route,
                profile=desired,
                identity=current,
                switched=False,
                duration_seconds=time.monotonic() - started,
            )

        if current_pid is not None and current is None:
            raise ModelServiceError(
                "Refusing to replace an unidentified live llama.cpp server"
            )
        previous_profile_id = self._matching_profile(
            current,
            current_pid,
            require_command=True,
        )
        if current is not None and previous_profile_id is None:
            raise ModelServiceError(
                "Refusing to replace an unrecognized live llama.cpp profile"
            )

        self._stop_active_server()
        started_pid: int | None = None
        try:
            started_pid = self._start_profile(desired)
            identity = self._wait_for_profile(desired, started_pid)
        except Exception as exc:
            if started_pid is not None:
                try:
                    self._stop_pid(started_pid)
                except ModelServiceError:
                    pass
            restore_error: str | None = None
            if previous_profile_id is not None:
                previous = self.config.profiles[previous_profile_id]
                try:
                    restore_pid = self._start_profile(previous)
                    self._wait_for_profile(previous, restore_pid)
                except Exception as restore_exc:
                    restore_error = str(restore_exc)
            event = {
                "event": "switch_failed",
                "at_utc": datetime.now(UTC).isoformat(),
                "from_profile": previous_profile_id,
                "to_profile": profile_id,
                "route": route,
                "error": str(exc),
                "restore_error": restore_error,
            }
            self._record_event(event)
            suffix = (
                f"; restoring {previous_profile_id} also failed: {restore_error}"
                if restore_error
                else ""
            )
            raise ModelServiceError(f"Model switch failed: {exc}{suffix}") from exc

        evidence = self._switch_evidence(
            route=route,
            profile=desired,
            identity=identity,
            switched=True,
            duration_seconds=time.monotonic() - started,
        )
        self._record_event(
            {
                "event": "switch_completed",
                "at_utc": datetime.now(UTC).isoformat(),
                "from_profile": previous_profile_id,
                "to_profile": profile_id,
                **evidence,
            }
        )
        return evidence

    def _locked_switch(self, profile_id: str, *, route: str | None) -> dict[str, Any]:
        self.state_root.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                return self._switch_locked(profile_id, route=route)
            except KeyboardInterrupt:
                self._stop_managed_after_interrupt_locked()
                raise
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def _profile_for_route(self, route: str) -> str:
        try:
            return self.config.route_profiles[route]
        except KeyError as exc:
            raise ModelServiceError(
                f"No model service profile for route: {route}"
            ) from exc

    def ensure_route(self, route: str) -> dict[str, Any]:
        """Ensure the trusted physical profile for one logical route is live."""
        return self._locked_switch(self._profile_for_route(route), route=route)

    @contextmanager
    def route_session(self, route: str) -> Iterator[dict[str, Any]]:
        """Hold the serial service lease for one complete model request."""
        profile_id = self._profile_for_route(route)
        self.state_root.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                yield self._switch_locked(profile_id, route=route)
            except KeyboardInterrupt:
                self._stop_managed_after_interrupt_locked()
                raise
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def switch_profile(self, profile_id: str) -> dict[str, Any]:
        """Switch explicitly to one trusted operator profile."""
        return self._locked_switch(profile_id, route=None)

    def stop(self) -> dict[str, Any]:
        """Stop the active managed or recognized llama.cpp server."""
        self.state_root.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                identity = self.current_identity()
                pid = self._discover_server_pid()
                profile_id = self._matching_profile(
                    identity,
                    pid,
                    require_command=True,
                )
                if pid is not None and profile_id is None:
                    raise ModelServiceError(
                        "Refusing to stop an unrecognized live llama.cpp profile"
                    )
                self._stop_active_server()
                self.state_path.unlink(missing_ok=True)
                event = {
                    "event": "stopped",
                    "at_utc": datetime.now(UTC).isoformat(),
                    "profile_id": profile_id,
                    "service_identity": identity.to_dict() if identity else None,
                }
                self._record_event(event)
                return event
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def stop_managed(self, *, reason: str = "operator_request") -> dict[str, Any]:
        """Stop only a server launched and recorded by local-coder."""
        self.state_root.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                return self._stop_managed_server_locked(reason=reason)
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def status(self) -> dict[str, Any]:
        """Return current identity and the matching trusted profile."""
        identity = self.current_identity()
        pid = self._discover_server_pid()
        return {
            "config_sha256": self.config.config_sha256,
            "profile_id": self._matching_profile(
                identity,
                pid,
                require_command=True,
            ),
            "service_identity": identity.to_dict() if identity else None,
            "managed_state": self._read_state(),
        }
