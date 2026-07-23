"""Collect bounded live Qwythos F3 contract and resource evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .model_response import normalize_model_response, normalize_provider_error
from .route_qualification import (
    CONTRACT_SUITES,
    DEFAULT_POLICY_PATH,
    QualificationError,
    QualificationPolicy,
    load_policy,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LITELLM_BASE_URL = "http://127.0.0.1:4000/v1"
DEFAULT_LLAMA_BASE_URL = "http://127.0.0.1:8080"
DEFAULT_OUTPUT_DIR = ROOT / ".local-coder" / "qualifications"
COLLECTION_KIND = "qwythos-f3-focused-v1"
_METRIC_PATTERN = re.compile(
    r"^llamacpp:predicted_tokens_seconds(?:\{[^}]*\})?\s+([0-9.eE+-]+)$"
)


class CollectionError(RuntimeError):
    """Raised when live evidence cannot be collected without ambiguity."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def _positive_number(value: float | None, name: str) -> float | None:
    if value is None:
        return None
    if not math.isfinite(value) or value < 0:
        raise CollectionError(f"{name} must be a finite number >= 0")
    return float(value)


class HttpClient:
    """Small JSON/text client with bounded timeouts and local bearer auth."""

    def __init__(self, *, api_key: str = "local") -> None:
        self.api_key = api_key

    def get_json(self, url: str, *, timeout: float = 10.0) -> Mapping[str, Any]:
        request = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        try:
            with urllib.request.urlopen(  # noqa: S310
                request, timeout=timeout
            ) as response:
                value = json.loads(response.read().decode("utf-8"))
        except (OSError, TimeoutError, json.JSONDecodeError) as exc:
            raise CollectionError(f"GET {url} failed: {type(exc).__name__}") from exc
        if not isinstance(value, Mapping):
            raise CollectionError(f"GET {url} did not return a JSON object")
        return value

    def get_text(self, url: str, *, timeout: float = 10.0) -> str:
        request = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        try:
            with urllib.request.urlopen(  # noqa: S310
                request, timeout=timeout
            ) as response:
                return response.read().decode("utf-8")
        except (OSError, TimeoutError, UnicodeDecodeError) as exc:
            raise CollectionError(f"GET {url} failed: {type(exc).__name__}") from exc

    def post_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        timeout: float,
    ) -> Mapping[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(  # noqa: S310
                request, timeout=timeout
            ) as response:
                value = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise CollectionError(f"POST {url} failed with HTTP {exc.code}") from exc
        except (OSError, TimeoutError, json.JSONDecodeError) as exc:
            raise CollectionError(f"POST {url} failed: {type(exc).__name__}") from exc
        if not isinstance(value, Mapping):
            raise CollectionError(f"POST {url} did not return a JSON object")
        return value


@dataclass(frozen=True)
class ServiceIdentity:
    """Bounded identity and capacity data from the active llama.cpp service."""

    model_file: str
    model_alias: str
    build_info: str
    configured_context_tokens: int
    total_slots: int
    server_pid: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_file": self.model_file,
            "model_alias": self.model_alias,
            "build_info": self.build_info,
            "configured_context_tokens": self.configured_context_tokens,
            "total_slots": self.total_slots,
            "server_pid": self.server_pid,
        }


@dataclass
class ResourceSampler:
    """Sample llama-server RSS and NVIDIA process memory during collection."""

    pid: int
    peak_vram_override_mib: float | None = None
    interval_seconds: float = 0.5
    peak_system_memory_mib: float = 0.0
    peak_vram_mib: float = 0.0

    def __post_init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None
        self._saw_vram = self.peak_vram_override_mib is not None
        if self.peak_vram_override_mib is not None:
            override = _positive_number(
                self.peak_vram_override_mib,
                "peak_vram_override_mib",
            )
            if override is None or override <= 0:
                raise CollectionError(
                    "peak_vram_override_mib must be greater than zero"
                )
            self.peak_vram_mib = override

    def _sample_rss_mib(self) -> float:
        status_path = Path(f"/proc/{self.pid}/status")
        try:
            lines = status_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise CollectionError(f"Unable to read {status_path}") from exc
        for line in lines:
            if line.startswith("VmRSS:"):
                fields = line.split()
                if len(fields) >= 2 and fields[1].isdigit():
                    return int(fields[1]) / 1024.0
        raise CollectionError(f"VmRSS is unavailable for llama-server PID {self.pid}")

    def _sample_vram_mib(self) -> float | None:
        executable = shutil.which("nvidia-smi")
        if executable is None:
            wsl_executable = Path("/usr/lib/wsl/lib/nvidia-smi")
            if wsl_executable.is_file():
                executable = str(wsl_executable)
        if executable is None:
            raise CollectionError("nvidia-smi is unavailable")
        try:
            result = subprocess.run(
                [
                    executable,
                    "--query-compute-apps=pid,used_memory",
                    "--format=csv,noheader,nounits",
                ],
                check=True,
                text=True,
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise CollectionError("nvidia-smi process-memory query failed") from exc
        for line in result.stdout.splitlines():
            fields = [field.strip() for field in line.split(",")]
            if len(fields) != 2:
                continue
            try:
                pid = int(fields[0])
                memory = float(fields[1])
            except ValueError:
                continue
            if pid == self.pid:
                return memory
        return None

    def sample_once(self) -> None:
        """Record one synchronous resource sample."""
        self.peak_system_memory_mib = max(
            self.peak_system_memory_mib,
            self._sample_rss_mib(),
        )
        if self.peak_vram_override_mib is None:
            vram = self._sample_vram_mib()
            if vram is not None:
                self._saw_vram = True
                self.peak_vram_mib = max(self.peak_vram_mib, vram)

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                self.sample_once()
                self._stop.wait(self.interval_seconds)
        except BaseException as exc:  # pragma: no cover - surfaced by stop()
            self._error = exc
            self._stop.set()

    def start(self) -> None:
        """Start background sampling after one fail-fast synchronous sample."""
        self.sample_once()
        self._thread = threading.Thread(
            target=self._run,
            name="qwythos-resource-sampler",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop sampling and require both RSS and VRAM observations."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(5.0, self.interval_seconds * 4))
        if self._error is not None:
            raise CollectionError("Resource sampling failed") from self._error
        self.sample_once()
        if not self._saw_vram:
            raise CollectionError(
                f"No NVIDIA process-memory sample found for llama-server PID {self.pid}"
            )


def _read_cmdline(pid: int) -> tuple[str, ...]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ()
    return tuple(
        part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part
    )


def discover_server_pid(candidate_model: str) -> int:
    """Return the unique llama-server PID whose command names the candidate file."""
    candidates: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        command = _read_cmdline(pid)
        if not command:
            continue
        executable = Path(command[0]).name
        if executable != "llama-server":
            continue
        if any(Path(argument).name == candidate_model for argument in command[1:]):
            candidates.append(pid)
    if len(candidates) != 1:
        detail = ", ".join(str(pid) for pid in sorted(candidates)) or "none"
        raise CollectionError(
            "Unable to identify one active llama-server process for "
            f"{candidate_model}; matches: {detail}. Pass --server-pid explicitly."
        )
    return candidates[0]


def git_commit(root: Path = ROOT) -> str:
    """Return the clean current implementation commit."""
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root,
            check=True,
            text=True,
            capture_output=True,
        ).stdout
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise CollectionError("Unable to inspect the implementation Git state") from exc
    if status.strip():
        raise CollectionError(
            "Qualification collection requires a committed, clean working tree"
        )
    if len(commit) != 40 or any(
        character not in "0123456789abcdef" for character in commit
    ):
        raise CollectionError("Current Git HEAD is not a lowercase 40-character SHA")
    return commit


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise CollectionError(f"{name} must be an integer >= {minimum}")
    return value


def inspect_service(
    *,
    client: HttpClient,
    llama_base_url: str,
    policy: QualificationPolicy,
    server_pid: int | None,
) -> ServiceIdentity:
    """Verify the active model, alias, context, slot count, and server process."""
    health = client.get_json(_url(llama_base_url, "/health"))
    if health.get("status") != "ok":
        raise CollectionError("llama.cpp health endpoint did not report status=ok")
    props = client.get_json(_url(llama_base_url, "/props"))
    model_path = props.get("model_path")
    if not isinstance(model_path, str) or not model_path:
        raise CollectionError("llama.cpp /props did not report model_path")
    model_file = Path(model_path).name
    if model_file != policy.candidate_model:
        raise CollectionError(
            "Active llama.cpp model does not match the frozen policy: "
            f"expected {policy.candidate_model}, found {model_file}"
        )

    models = client.get_json(_url(llama_base_url, "/v1/models"))
    model_records = models.get("data")
    if not isinstance(model_records, Sequence) or isinstance(
        model_records, (str, bytes)
    ):
        raise CollectionError("llama.cpp /v1/models did not return a data array")
    aliases = {
        record.get("id")
        for record in model_records
        if isinstance(record, Mapping) and isinstance(record.get("id"), str)
    }
    if policy.candidate_route not in aliases:
        raise CollectionError(
            "Active llama.cpp alias does not match the frozen candidate route: "
            f"expected {policy.candidate_route}, found {sorted(aliases)}"
        )

    settings = props.get("default_generation_settings")
    if not isinstance(settings, Mapping):
        raise CollectionError("llama.cpp /props omitted default_generation_settings")
    configured_context = _integer(
        settings.get("n_ctx"),
        "llama.cpp configured context",
        minimum=1,
    )
    total_slots = _integer(props.get("total_slots"), "llama.cpp total_slots", minimum=1)
    build_info = props.get("build_info")
    if not isinstance(build_info, str) or not build_info.strip():
        raise CollectionError("llama.cpp /props did not report build_info")

    resolved_pid = (
        server_pid if server_pid is not None else discover_server_pid(model_file)
    )
    if not Path(f"/proc/{resolved_pid}").is_dir():
        raise CollectionError(f"llama-server PID does not exist: {resolved_pid}")
    command = _read_cmdline(resolved_pid)
    if not command or Path(command[0]).name != "llama-server":
        raise CollectionError(f"PID {resolved_pid} is not a llama-server process")
    if not any(Path(argument).name == model_file for argument in command[1:]):
        raise CollectionError(
            f"PID {resolved_pid} command line does not name {model_file}"
        )

    return ServiceIdentity(
        model_file=model_file,
        model_alias=policy.candidate_route,
        build_info=build_info.strip(),
        configured_context_tokens=configured_context,
        total_slots=total_slots,
        server_pid=resolved_pid,
    )


def _fixture_messages(suite: str) -> list[dict[str, str]]:
    if suite == "exact":
        return [{"role": "user", "content": "Reply with exactly ROUTE_OK."}]
    if suite == "planner":
        return [
            {
                "role": "system",
                "content": (
                    "Return only one JSON object with exactly these fields: "
                    "instruction (non-empty string), editable_files (one or two "
                    "repository-relative strings), acceptance_criteria (one to six "
                    "non-empty strings), and depends_on (an array of strings)."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Task: Fix the boundary defect in calculator.py without changing "
                    "other files. Evidence: the function currently uses "
                    "`value < limit`; "
                    "the requirement and failing test require the equal case to pass. "
                    "Produce one atomic implementation-ready plan."
                ),
            },
        ]
    if suite == "reviewer":
        return [
            {
                "role": "system",
                "content": (
                    "Return only one JSON object with exactly these fields: verdict "
                    "(pass, fail, or needs_attention), summary (non-empty string), "
                    "issues (array of strings), and unrelated_changes "
                    "(array of strings)."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Task: Accept values equal to limit in calculator.py. "
                    "Changed files: [calculator.py]. Verification failed because the "
                    "equal-boundary test still fails. Diff: `return value < limit` was "
                    "left unchanged. Review the result without editing files."
                ),
            },
        ]
    raise CollectionError(f"Unsupported contract suite: {suite}")


def _profile_payload(
    policy: QualificationPolicy,
    suite: str,
    messages: Sequence[Mapping[str, str]],
) -> dict[str, Any]:
    suite_policy = policy.contract_suites[suite]
    if suite == "exact":
        temperature = 0.0
        top_p = 1.0
        top_k = None
        repetition_penalty = None
        reasoning_tokens = 0
        timeout_seconds = 120
    else:
        profile = policy.expected_profiles[suite]
        temperature = float(profile["temperature"])
        top_p = float(profile["top_p"])
        top_k = profile["top_k"]
        repetition_penalty = profile["repetition_penalty"]
        reasoning_tokens = int(profile["reasoning_tokens"])
        timeout_seconds = int(profile["timeout_seconds"])

    extra_body: dict[str, Any] = {
        "chat_template_kwargs": {
            "enable_thinking": suite_policy.thinking_enabled,
        },
        "thinking_budget_tokens": reasoning_tokens,
    }
    if top_k is not None:
        extra_body["top_k"] = int(top_k)
    if repetition_penalty is not None:
        extra_body["repeat_penalty"] = float(repetition_penalty)

    payload: dict[str, Any] = {
        "model": policy.candidate_route,
        "messages": list(messages),
        "temperature": temperature,
        "max_tokens": suite_policy.maximum_completion_tokens,
        "extra_body": extra_body,
    }
    if top_p != 1.0:
        payload["top_p"] = top_p
    payload["_timeout_seconds"] = timeout_seconds
    return payload


def _valid_string_list(
    value: Any,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> bool:
    if not isinstance(value, list):
        return False
    if len(value) < minimum or (maximum is not None and len(value) > maximum):
        return False
    return all(isinstance(item, str) and bool(item.strip()) for item in value)


def _planner_schema_valid(value: Any) -> bool:
    if not isinstance(value, Mapping) or set(value) != {
        "instruction",
        "editable_files",
        "acceptance_criteria",
        "depends_on",
    }:
        return False
    return (
        isinstance(value["instruction"], str)
        and bool(value["instruction"].strip())
        and _valid_string_list(value["editable_files"], minimum=1, maximum=2)
        and value["editable_files"] == ["calculator.py"]
        and _valid_string_list(value["acceptance_criteria"], minimum=1, maximum=6)
        and isinstance(value["depends_on"], list)
        and not value["depends_on"]
    )


def _reviewer_schema_valid(value: Any) -> bool:
    if not isinstance(value, Mapping) or set(value) != {
        "verdict",
        "summary",
        "issues",
        "unrelated_changes",
    }:
        return False
    return (
        value["verdict"] == "fail"
        and isinstance(value["summary"], str)
        and bool(value["summary"].strip())
        and _valid_string_list(value["issues"], minimum=1)
        and isinstance(value["unrelated_changes"], list)
        and not value["unrelated_changes"]
    )


def schema_valid(suite: str, content: str) -> bool:
    """Validate one focused final answer without retaining its text."""
    if suite == "exact":
        return content.strip() == "ROUTE_OK"
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        return False
    if suite == "planner":
        return _planner_schema_valid(value)
    if suite == "reviewer":
        return _reviewer_schema_valid(value)
    raise CollectionError(f"Unsupported contract suite: {suite}")


def parse_generation_throughput(metrics: str) -> float | None:
    """Read llama.cpp's current predicted-token throughput gauge."""
    values: list[float] = []
    for line in metrics.splitlines():
        match = _METRIC_PATTERN.match(line.strip())
        if match is None:
            continue
        try:
            value = float(match.group(1))
        except ValueError:
            continue
        if math.isfinite(value) and value >= 0:
            values.append(value)
    return values[-1] if values else None


def _contract_record(
    *,
    suite: str,
    index: int,
    response: Mapping[str, Any],
    latency_seconds: float,
    throughput: float | None,
) -> dict[str, Any]:
    expected_content = "ROUTE_OK" if suite == "exact" else None
    normalized = normalize_model_response(
        response,
        expected_content=expected_content,
        accept_tool_calls=False,
    )
    if normalized.prompt_tokens is None or normalized.completion_tokens is None:
        raise CollectionError(f"contract.{suite}.{index} omitted token usage")
    measured_throughput = throughput
    if measured_throughput is None:
        if latency_seconds <= 0:
            raise CollectionError(f"contract.{suite}.{index} has zero latency")
        measured_throughput = normalized.completion_tokens / latency_seconds
    return {
        "attempt_id": f"{suite}-{index}",
        "suite": suite,
        "thinking_enabled": suite != "exact",
        "response_outcome": normalized.outcome,
        "final_answer_present": bool(normalized.content.strip()),
        "schema_valid": schema_valid(suite, normalized.content),
        "reasoning_present": normalized.reasoning_present,
        "malformed_tool_call": bool(normalized.tool_calls),
        "prompt_tokens": normalized.prompt_tokens,
        "completion_tokens": normalized.completion_tokens,
        "reasoning_tokens": normalized.reasoning_tokens,
        "latency_seconds": latency_seconds,
        "generated_tokens_per_second": measured_throughput,
    }


def run_contract_attempt(
    *,
    client: HttpClient,
    litellm_base_url: str,
    llama_base_url: str,
    policy: QualificationPolicy,
    suite: str,
    index: int,
    messages: Sequence[Mapping[str, str]] | None = None,
) -> tuple[dict[str, Any], str]:
    """Run one bounded request and return its record and throughput source."""
    payload = _profile_payload(policy, suite, messages or _fixture_messages(suite))
    timeout = float(payload.pop("_timeout_seconds")) + 15.0
    started = time.perf_counter()
    try:
        response = client.post_json(
            _url(litellm_base_url, "/chat/completions"),
            payload,
            timeout=timeout,
        )
    except CollectionError as exc:
        latency = time.perf_counter() - started
        normalized = normalize_provider_error(
            exc,
            model=policy.candidate_route,
            provider="litellm",
        )
        return (
            {
                "attempt_id": f"{suite}-{index}",
                "suite": suite,
                "thinking_enabled": suite != "exact",
                "response_outcome": normalized.outcome,
                "final_answer_present": False,
                "schema_valid": False,
                "reasoning_present": False,
                "malformed_tool_call": False,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "reasoning_tokens": None,
                "latency_seconds": latency,
                "generated_tokens_per_second": 0.0,
            },
            "request_lower_bound",
        )
    latency = time.perf_counter() - started
    try:
        metrics = client.get_text(_url(llama_base_url, "/metrics"))
    except CollectionError:
        throughput = None
    else:
        throughput = parse_generation_throughput(metrics)
    record = _contract_record(
        suite=suite,
        index=index,
        response=response,
        latency_seconds=latency,
        throughput=throughput,
    )
    source = "llama_metrics" if throughput is not None else "request_lower_bound"
    return record, source


def _formatted_prompt_tokens(
    *,
    client: HttpClient,
    llama_base_url: str,
    messages: Sequence[Mapping[str, str]],
) -> int:
    applied = client.post_json(
        _url(llama_base_url, "/apply-template"),
        {
            "messages": list(messages),
            "chat_template_kwargs": {"enable_thinking": False},
        },
        timeout=30.0,
    )
    prompt = applied.get("prompt")
    if not isinstance(prompt, str):
        raise CollectionError("llama.cpp /apply-template omitted prompt")
    tokenized = client.post_json(
        _url(llama_base_url, "/tokenize"),
        {"content": prompt, "add_special": False},
        timeout=30.0,
    )
    tokens = tokenized.get("tokens")
    if not isinstance(tokens, list):
        raise CollectionError("llama.cpp /tokenize omitted tokens")
    return len(tokens)


def build_context_messages(
    *,
    client: HttpClient,
    llama_base_url: str,
    target_tokens: int,
    maximum_tokens: int,
) -> tuple[list[dict[str, str]], int]:
    """Build a deterministic exact-response prompt at or above the target size."""
    if target_tokens <= 0 or maximum_tokens < target_tokens:
        raise CollectionError("Invalid context-probe token bounds")

    def messages(repetitions: int) -> list[dict[str, str]]:
        filler = "bounded qualification context " * repetitions
        return [
            {
                "role": "system",
                "content": "Ignore filler and obey the final exact-output instruction.",
            },
            {
                "role": "user",
                "content": f"{filler}\nReply with exactly ROUTE_OK.",
            },
        ]

    low = 0
    high = 1
    high_tokens = _formatted_prompt_tokens(
        client=client,
        llama_base_url=llama_base_url,
        messages=messages(high),
    )
    while high_tokens < target_tokens:
        low = high
        high *= 2
        high_tokens = _formatted_prompt_tokens(
            client=client,
            llama_base_url=llama_base_url,
            messages=messages(high),
        )
        if high_tokens > maximum_tokens:
            break
    if high_tokens < target_tokens:
        raise CollectionError(
            f"Unable to construct a {target_tokens}-token context probe"
        )

    while low + 1 < high:
        middle = (low + high) // 2
        middle_tokens = _formatted_prompt_tokens(
            client=client,
            llama_base_url=llama_base_url,
            messages=messages(middle),
        )
        if middle_tokens < target_tokens:
            low = middle
        else:
            high = middle
            high_tokens = middle_tokens
    if high_tokens > maximum_tokens:
        raise CollectionError(
            "Smallest context probe exceeds the configured safe prompt bound"
        )
    return messages(high), high_tokens


def collect_focused_evidence(
    *,
    policy: QualificationPolicy,
    environment_id: str,
    implementation_commit: str,
    client: HttpClient,
    litellm_base_url: str,
    llama_base_url: str,
    server_pid: int | None,
    startup_seconds: float | None,
    model_switch_seconds: float | None,
    peak_vram_mib: float | None,
    sampler_factory: Callable[[int, float | None], ResourceSampler] = ResourceSampler,
) -> dict[str, Any]:
    """Collect focused contracts and currently measurable resource evidence."""
    if not environment_id.strip():
        raise CollectionError("environment_id must be non-empty")
    startup_seconds = _positive_number(startup_seconds, "startup_seconds")
    model_switch_seconds = _positive_number(
        model_switch_seconds,
        "model_switch_seconds",
    )
    peak_vram_mib = _positive_number(peak_vram_mib, "peak_vram_mib")
    if peak_vram_mib is not None and peak_vram_mib <= 0:
        raise CollectionError("peak_vram_mib must be greater than zero")
    identity = inspect_service(
        client=client,
        llama_base_url=llama_base_url,
        policy=policy,
        server_pid=server_pid,
    )
    context_target = policy.resource_limits.minimum_context_tokens_tested
    exact_budget = policy.contract_suites["exact"].maximum_completion_tokens
    if identity.configured_context_tokens < context_target + exact_budget:
        raise CollectionError(
            "Configured llama.cpp context is too small for the frozen context probe"
        )

    sampler = sampler_factory(identity.server_pid, peak_vram_mib)
    contract_runs: list[dict[str, Any]] = []
    throughput_sources = {"llama_metrics": 0, "request_lower_bound": 0}
    sampler.start()
    try:
        for suite in CONTRACT_SUITES:
            minimum = policy.contract_suites[suite].minimum_attempts
            for index in range(1, minimum + 1):
                record, throughput_source = run_contract_attempt(
                    client=client,
                    litellm_base_url=litellm_base_url,
                    llama_base_url=llama_base_url,
                    policy=policy,
                    suite=suite,
                    index=index,
                )
                contract_runs.append(record)
                throughput_sources[throughput_source] += 1

        context_messages, estimated_context_tokens = build_context_messages(
            client=client,
            llama_base_url=llama_base_url,
            target_tokens=context_target,
            maximum_tokens=identity.configured_context_tokens - exact_budget,
        )
        context_record, throughput_source = run_contract_attempt(
            client=client,
            litellm_base_url=litellm_base_url,
            llama_base_url=llama_base_url,
            policy=policy,
            suite="exact",
            index=policy.contract_suites["exact"].minimum_attempts + 1,
            messages=context_messages,
        )
        context_record["attempt_id"] = "exact-context-1"
        contract_runs.append(context_record)
        throughput_sources[throughput_source] += 1
        final_identity = inspect_service(
            client=client,
            llama_base_url=llama_base_url,
            policy=policy,
            server_pid=identity.server_pid,
        )
        if final_identity != identity:
            raise CollectionError(
                "llama.cpp service identity changed during collection"
            )
    except BaseException as collection_error:
        try:
            sampler.stop()
        except BaseException as sampler_error:
            collection_error.add_note(f"resource sampling also failed: {sampler_error}")
        raise
    else:
        sampler.stop()

    context_tokens_tested = int(context_record["prompt_tokens"])
    if context_tokens_tested < context_target:
        raise CollectionError(
            "Provider-reported context usage is below the frozen minimum: "
            f"expected >= {context_target}, found {context_tokens_tested}"
        )

    pending = ["track_g_role_cases"]
    if startup_seconds is None:
        pending.append("startup_seconds")
    if model_switch_seconds is None:
        pending.append("model_switch_seconds")

    report = {
        "schema_version": 1,
        "collection_kind": COLLECTION_KIND,
        "policy_id": policy.policy_id,
        "policy_sha256": policy.sha256,
        "candidate_model": policy.candidate_model,
        "candidate_route": policy.candidate_route,
        "candidate_profiles": {
            role: dict(profile) for role, profile in policy.expected_profiles.items()
        },
        "implementation_commit": implementation_commit,
        "environment_id": environment_id.strip(),
        "collected_at_utc": datetime.now(UTC).isoformat(),
        "service_identity": identity.as_dict(),
        "measurement_methods": {
            "system_memory": "llama-server VmRSS sampled from /proc",
            "vram": (
                "operator-supplied peak measurement"
                if peak_vram_mib is not None
                else "llama-server process memory sampled with nvidia-smi"
            ),
            "context": (
                "provider-reported prompt tokens from a live exact-response request"
            ),
            "throughput": throughput_sources,
            "startup": (
                "operator-supplied independent measurement"
                if startup_seconds is not None
                else "pending"
            ),
            "model_switch": (
                "operator-supplied independent measurement"
                if model_switch_seconds is not None
                else "pending"
            ),
            "reasoning_storage": (
                "presence and provider token counts only; no final or reasoning text"
            ),
        },
        "contract_runs": contract_runs,
        "resources": {
            "startup_seconds": startup_seconds,
            "model_switch_seconds": model_switch_seconds,
            "peak_vram_mib": sampler.peak_vram_mib,
            "peak_system_memory_mib": sampler.peak_system_memory_mib,
            "context_tokens_tested": context_tokens_tested,
            "context_tokens_estimated_before_request": estimated_context_tokens,
        },
        "pending": pending,
    }
    report["collection_sha256"] = _sha256(report)
    return report


def write_report(path: Path, report: Mapping[str, Any]) -> None:
    """Atomically create one evidence report without replacing prior evidence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise CollectionError(f"Refusing to overwrite existing report: {path}")
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.link(temporary, path)
    except FileExistsError as exc:
        raise CollectionError(f"Refusing to overwrite existing report: {path}") from exc
    except OSError as exc:
        raise CollectionError(f"Unable to write report {path}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def default_output_path() -> Path:
    """Return a timestamped ignored-path destination for one collection."""
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return DEFAULT_OUTPUT_DIR / f"qwythos-f3-focused-{timestamp}.json"


def parser() -> argparse.ArgumentParser:
    """Build the focused live-evidence collector CLI."""
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--policy",
        type=Path,
        default=DEFAULT_POLICY_PATH,
        help="Frozen Qwythos qualification policy.",
    )
    result.add_argument(
        "--environment-id",
        required=True,
        help="Stable identifier for the tested machine and runtime configuration.",
    )
    result.add_argument(
        "--output",
        type=Path,
        help="Ignored JSON report path; defaults under .local-coder/qualifications/.",
    )
    result.add_argument(
        "--litellm-base-url",
        default=DEFAULT_LITELLM_BASE_URL,
        help="OpenAI-compatible LiteLLM base URL.",
    )
    result.add_argument(
        "--llama-base-url",
        default=DEFAULT_LLAMA_BASE_URL,
        help="Direct llama.cpp base URL for identity and resource metrics.",
    )
    result.add_argument(
        "--server-pid",
        type=int,
        help="Explicit llama-server PID when automatic discovery is ambiguous.",
    )
    result.add_argument(
        "--startup-seconds",
        type=float,
        help="Separately measured cold startup time; omitted values remain pending.",
    )
    result.add_argument(
        "--model-switch-seconds",
        type=float,
        help="Separately measured serial switch time; omitted values remain pending.",
    )
    result.add_argument(
        "--peak-vram-mib",
        type=float,
        help=(
            "Operator-measured peak VRAM for WSL or another environment where "
            "nvidia-smi cannot attribute memory to the Linux PID."
        ),
    )
    return result


def main(argv: Sequence[str] | None = None) -> int:
    """Collect and persist one bounded focused-evidence report."""
    arguments = parser().parse_args(argv)
    policy = load_policy(arguments.policy)
    commit = git_commit()
    output = arguments.output or default_output_path()
    report = collect_focused_evidence(
        policy=policy,
        environment_id=arguments.environment_id,
        implementation_commit=commit,
        client=HttpClient(),
        litellm_base_url=arguments.litellm_base_url,
        llama_base_url=arguments.llama_base_url,
        server_pid=arguments.server_pid,
        startup_seconds=arguments.startup_seconds,
        model_switch_seconds=arguments.model_switch_seconds,
        peak_vram_mib=arguments.peak_vram_mib,
    )
    write_report(output, report)
    summary = {
        "output": str(output),
        "collection_sha256": report["collection_sha256"],
        "contract_runs": len(report["contract_runs"]),
        "peak_vram_mib": report["resources"]["peak_vram_mib"],
        "peak_system_memory_mib": report["resources"]["peak_system_memory_mib"],
        "context_tokens_tested": report["resources"]["context_tokens_tested"],
        "pending": report["pending"],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CollectionError, QualificationError) as exc:
        print(f"route-qualification-collect: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
