"""Record and replay orchestration."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from replayable.cassette import (
    CassetteError,
    CassetteReader,
    CassetteWriter,
    base_manifest,
    env_fingerprint,
)
from replayable.cassette.events import Event, EventKind, EventLogReader, event_from_flow
from replayable.core import ca as ca_core
from replayable.core import container as container_core
from replayable.core import docker as docker_core
from replayable.core import proxy as proxy_core
from replayable.core.policy import (
    PolicyError,
    build_policy_manifest,
    load_policy,
    require_enforceable,
    resolve_policy,
    validate_policy_manifest,
)
from replayable.errors import HarnessError
from replayable.exit_codes import ExitCode
from replayable.normalize_rules import (
    RulesError,
    load_rules,
)
from replayable.redact import (
    EnvFileError,
    SecretConfigError,
    load_secret_name_overrides,
    parse_env_file,
    redacted_placeholder,
    secret_names,
    secret_values,
)
from replayable.snapshot import (
    SnapshotError,
    create_snapshot,
    diff_file_manifests,
    load_recorded_snapshot,
)
from replayable.verdict.fork_result import (
    FORK_RESULT_FILE_NAME,
    ForkResultError,
    build_fork_result,
    write_fork_result,
)
from replayable.verdict.observation import (
    OBSERVATION_FILE_NAME,
    Observation,
    ObservationError,
    build_observation,
    write_observation,
)

ProxyProcess = Callable[..., AbstractContextManager[None]]


@dataclass(frozen=True)
class RunContext:
    """Runtime collaborators used by record and replay.

    Production callers use the defaults. Tests and future integrations can
    replace only the boundary they own without patching module globals.
    """

    default_ca_path: Callable[[], Path] = ca_core.default_ca_path
    require_ca: Callable[[Path], None] = ca_core._require_ca
    require_ca_valid_at: Callable[[Path, float], None] = ca_core._require_ca_valid_at
    mitmproxy_confdir_for_ca: Callable[
        [Path], Path | None
    ] = ca_core._mitmproxy_confdir_for_ca
    proxy_listen_host: Callable[[], str] = proxy_core._proxy_listen_host
    resolve_port: Callable[[int, str], int] = proxy_core._resolve_port
    proxy_process: ProxyProcess = proxy_core.proxy_process
    docker_command: Callable[..., list[str]] = docker_core.docker_command
    replay_time_environment: Callable[[float], dict[str, str]] = (
        docker_core.replay_time_environment
    )
    resolve_image_identity: Callable[[str], tuple[str, str]] = (
        docker_core._resolve_image_identity
    )
    select_replay_image: Callable[..., str] = docker_core._select_replay_image
    run_container: Callable[..., int] = container_core._run_container


DEFAULT_RUN_CONTEXT = RunContext()

__all__ = [
    "DEFAULT_RUN_CONTEXT",
    "RunContext",
    "record_run",
    "replay_run",
]

REPLAY_REPORT_FILE_NAME = "replay-report.json"
REPLAY_STATE_FILE_NAME = "replay-state.json"
RUN_LOG_FILE_NAME = "run.log"
REPLAY_LOG_FILE_NAME = "replay.log"
REPLAY_PROXY_LOG_FILE_NAME = "replay-proxy.log"
AGENT_STDOUT_FILE_NAME = "agent.stdout"
AGENT_STDERR_FILE_NAME = "agent.stderr"
REPLAY_STDOUT_FILE_NAME = "replay-agent.stdout"
REPLAY_STDERR_FILE_NAME = "replay-agent.stderr"
LAST_REPLAY_FILE_NAME = "last-replay.json"
FORK_REPORT_FILE_NAME = "fork-report.json"
FORK_STATE_FILE_NAME = "fork-state.json"
FORK_LOG_FILE_NAME = "fork.log"
FORK_PROXY_LOG_FILE_NAME = "fork-proxy.log"
FORK_STDOUT_FILE_NAME = "fork-agent.stdout"
FORK_STDERR_FILE_NAME = "fork-agent.stderr"


def _log_event(path: Path, event: str, **fields: object) -> None:
    record = {
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "event": event,
        **fields,
    }
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n")


@contextmanager
def _runtime_workspace(
    selected: Path | None,
) -> Iterator[Path]:
    if selected is None:
        with tempfile.TemporaryDirectory(prefix="replayable-workspace-") as directory:
            yield Path(directory)
        return

    path = selected.expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    yield path


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _addon_path(name: str) -> Path:
    return Path(__file__).parent.parent / "addons" / name


def _prepare_output_directory(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HarnessError(f"cannot create output directory {path}: {exc}") from exc


@contextmanager
def _secret_values_file(secrets: dict[str, str]) -> Iterator[Path | None]:
    """Expose secret values to the record addon through a private 0600 file."""

    if not secrets:
        yield None
        return
    descriptor, name = tempfile.mkstemp(prefix="replayable-secrets-", suffix=".json")
    path = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(secrets, output, separators=(",", ":"))
        os.chmod(path, 0o600)
        yield path
    finally:
        path.unlink(missing_ok=True)


def _remove_stale_replay_artifacts(out: Path) -> None:
    for stale in (
        OBSERVATION_FILE_NAME,
        REPLAY_REPORT_FILE_NAME,
        REPLAY_STATE_FILE_NAME,
        LAST_REPLAY_FILE_NAME,
        FORK_REPORT_FILE_NAME,
        FORK_STATE_FILE_NAME,
        FORK_RESULT_FILE_NAME,
        REPLAY_STDOUT_FILE_NAME,
        REPLAY_STDERR_FILE_NAME,
        REPLAY_LOG_FILE_NAME,
        REPLAY_PROXY_LOG_FILE_NAME,
        FORK_STDOUT_FILE_NAME,
        FORK_STDERR_FILE_NAME,
        FORK_LOG_FILE_NAME,
        FORK_PROXY_LOG_FILE_NAME,
    ):
        (out / stale).unlink(missing_ok=True)


def _invalidate_replay_verdicts(cassette: Path) -> None:
    """Remove every artifact that could be mistaken for this run's verdict.

    Covers both consumers. ``scripts/check_replay.py`` reads last-replay.json
    and falls back to fork-result.json, and the dashboard labels a run HYBRID
    purely from the presence of fork-result.json — so a normal replay that left
    one behind would mislabel itself.
    """

    for stale in (
        LAST_REPLAY_FILE_NAME,
        FORK_RESULT_FILE_NAME,
        REPLAY_REPORT_FILE_NAME,
        REPLAY_STATE_FILE_NAME,
        FORK_REPORT_FILE_NAME,
        FORK_STATE_FILE_NAME,
    ):
        (cassette / stale).unlink(missing_ok=True)


def _validate_network_events(
    events: list[Event],
    flows: list[dict[str, Any]],
) -> None:
    event_flows = [
        event.payload.get("flow")
        for event in events
        if event.kind is EventKind.HTTP_EXCHANGE
    ]
    if event_flows != flows:
        raise CassetteError(
            "network events do not correspond exactly to the recorded flows"
        )


def record_run(
    *,
    image: str,
    command: Sequence[str],
    workspace: Path | None = None,
    env_file: Path | None = None,
    out: Path = Path("cassettes"),
    port: int = proxy_core.DEFAULT_PROXY_PORT,
    ca_path: Path | None = None,
    timeout_seconds: float | None = None,
    context: RunContext = DEFAULT_RUN_CONTEXT,
) -> ExitCode:
    """Record traffic, transcript, immutable image identity, and workspace state."""

    if not command:
        raise HarnessError("container command is empty; pass it after `--`")
    ca_path = (ca_path or context.default_ca_path()).expanduser().resolve()
    context.require_ca(ca_path)
    if env_file is not None and not env_file.is_file():
        raise HarnessError(
            f"environment file not found at {env_file}; check --env-file"
        )

    listen_host = context.proxy_listen_host()
    port = context.resolve_port(port, listen_host)
    out = out.resolve()
    _prepare_output_directory(out)
    try:
        user_environment = parse_env_file(env_file) if env_file is not None else {}
    except EnvFileError as exc:
        raise HarnessError(f"environment file is invalid: {exc}") from exc
    project_rules_path = Path.cwd() / "replayable.toml"
    if not project_rules_path.is_file():
        project_rules_path = None
    try:
        rules = load_rules(project_rules_path)
    except RulesError as exc:
        raise HarnessError(f"normalization rules are invalid: {exc}") from exc
    try:
        secret_name_overrides = load_secret_name_overrides(project_rules_path)
    except SecretConfigError as exc:
        raise HarnessError(f"secret overrides are invalid: {exc}") from exc
    try:
        policy = load_policy(project_rules_path)
        # Fail before the container runs, not after: pinning a mode the replay
        # engine does not honour would make the manifest describe behaviour the
        # cassette never gets.
        require_enforceable(policy)
    except PolicyError as exc:
        raise HarnessError(f"policy configuration is invalid: {exc}") from exc
    classified_secret_names = secret_names(
        user_environment,
        extra_names=secret_name_overrides,
    )
    image_digest, image_id = context.resolve_image_identity(image)
    writer = CassetteWriter(out)
    manifest = base_manifest(
        created_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        t0_epoch=0.0,
        image_ref=image,
        image_digest=image_digest,
        image_id=image_id,
        command=list(command),
        environment_fingerprint=env_fingerprint(
            user_environment,
            secret_names=classified_secret_names,
        ),
        ruleset_version=rules.version,
    )
    manifest["env_names"] = sorted(user_environment)
    manifest["secret_env_names"] = sorted(classified_secret_names)
    manifest["nonsecret_env"] = {
        name: value
        for name, value in sorted(user_environment.items())
        if name not in classified_secret_names
    }
    writer.initialize(manifest)
    cassette_rules_path = out / "replayable.toml"
    if project_rules_path is None:
        cassette_rules_path.unlink(missing_ok=True)
    elif project_rules_path.resolve() != cassette_rules_path.resolve():
        shutil.copyfile(project_rules_path, cassette_rules_path)
    _remove_stale_replay_artifacts(out)
    run_log = out / RUN_LOG_FILE_NAME
    run_log.write_text("", encoding="utf-8")
    run_id = uuid.uuid4().hex[:12]
    redaction_secret_values = secret_values(
        user_environment,
        extra_names=secret_name_overrides,
    )
    with (
        _runtime_workspace(workspace) as runtime_workspace,
        _secret_values_file(redaction_secret_values) as secrets_path,
    ):
        _log_event(run_log, "proxy_start", mode="record", port=port)
        with context.proxy_process(
            addon=_addon_path("record_addon.py"),
            port=port,
            addon_environment={
                "REPLAYABLE_CASSETTE_DIR": str(out),
                "REPLAYABLE_SECRET_VALUES_FILE": (
                    str(secrets_path) if secrets_path is not None else ""
                ),
            },
            log_path=out / "proxy.log",
            listen_host=listen_host,
            confdir=context.mitmproxy_confdir_for_ca(ca_path),
        ):
            t0_epoch = time.time()
            writer.update_manifest(t0_epoch=t0_epoch)
            container_command = context.docker_command(
                image=image,
                command=command,
                port=port,
                ca_path=ca_path,
                run_id=run_id,
                workspace=runtime_workspace,
                env_file=env_file,
                extra_environment=context.replay_time_environment(t0_epoch),
            )
            _log_event(
                run_log,
                "container_start",
                mode="record",
                run_id=run_id,
                image_digest=image_digest,
                image_id=image_id,
            )
            started = time.monotonic()
            return_code = context.run_container(
                container_command,
                stdout_path=out / AGENT_STDOUT_FILE_NAME,
                stderr_path=out / AGENT_STDERR_FILE_NAME,
                redaction_secrets=redaction_secret_values,
                container_name=f"replayable-{run_id}",
                timeout_seconds=timeout_seconds,
            )
            (out / AGENT_STDOUT_FILE_NAME).touch(exist_ok=True)
            (out / AGENT_STDERR_FILE_NAME).touch(exist_ok=True)
            wall_time = time.monotonic() - started
            _log_event(
                run_log,
                "container_exit",
                mode="record",
                return_code=return_code,
                wall_time_seconds=wall_time,
            )

        try:
            workspace_snapshot = create_snapshot(runtime_workspace, out)
        except SnapshotError as exc:
            raise HarnessError(f"workspace snapshot failed: {exc}") from exc
        _log_event(
            run_log,
            "workspace_snapshot",
            sha256=workspace_snapshot.sha256,
            file_count=len(workspace_snapshot.files),
        )

    try:
        loaded = CassetteReader(out).load_flows()
        events = EventLogReader(out).load_events()
        _validate_network_events(events, loaded.flows)
        network_scopes = sorted(
            {event.scope for event in events if event.kind is EventKind.HTTP_EXCHANGE}
        )
        resolved_policy = [
            resolve_policy(policy, channel="network", scope=scope)
            for scope in network_scopes
        ]
        writer.update_manifest(
            flow_count=len(loaded.flows),
            event_count=len(events),
            record_wall_time_seconds=wall_time,
            workspace_sha256=workspace_snapshot.sha256,
            stdout_sha256=_sha256_path(out / AGENT_STDOUT_FILE_NAME),
            stderr_sha256=_sha256_path(out / AGENT_STDERR_FILE_NAME),
            policy=build_policy_manifest(policy, resolved_policy),
            record_exit_code=return_code,
        )
        write_observation(out)
    except (CassetteError, ObservationError, PolicyError) as exc:
        raise HarnessError(f"recorded cassette is invalid: {exc}") from exc
    _log_event(run_log, "record_complete", flow_count=len(loaded.flows))
    return ExitCode.SUCCESS if return_code == 0 else ExitCode.AGENT_FAILED


def replay_run(
    *,
    cassette: Path,
    strict: bool = False,
    out_workspace: Path | None = None,
    port: int = proxy_core.DEFAULT_PROXY_PORT,
    ca_path: Path | None = None,
    allow_image_mismatch: bool = False,
    timeout_seconds: float | None = None,
    fork_at: int | None = None,
    env_file: Path | None = None,
    context: RunContext = DEFAULT_RUN_CONTEXT,
) -> ExitCode:
    """Replay offline, or replay a frozen prefix before continuing live."""

    cassette = cassette.resolve()
    if not cassette.is_dir():
        raise HarnessError(
            f"cassette directory not found at {cassette}; check --cassette"
        )
    # Drop every previous verdict before anything can fail. `check_replay.py`
    # reads last-replay.json first and then falls back to fork-result.json, so
    # clearing only one still lets a failed fork report an older run as green.
    # The reports and states are part of the same result: retaining one would
    # also make the dashboard describe a previous attempt as the latest one.
    _invalidate_replay_verdicts(cassette)
    listen_host = context.proxy_listen_host()
    port = context.resolve_port(port, listen_host)
    try:
        reader = CassetteReader(cassette)
        manifest = reader.load_manifest()
        loaded = reader.load_flows()
        pinned_policy = validate_policy_manifest(manifest)
        if pinned_policy is not None:
            # A cassette recorded by an older build could pin a mode this replay
            # engine does not honour. Serving it as `freeze` anyway would make
            # the run silently disagree with its own manifest.
            require_enforceable(pinned_policy[0])
        declared_flow_count = manifest.get("flow_count")
        if (
            isinstance(declared_flow_count, bool)
            or not isinstance(declared_flow_count, int)
            or declared_flow_count != len(loaded.flows)
        ):
            raise CassetteError(
                "manifest flow_count does not match the number of recorded flows"
            )
        if manifest["cassette_version"].split(".", maxsplit=1)[0] == "2":
            events = EventLogReader(cassette).load_events()
            declared_event_count = manifest.get("event_count")
            if (
                isinstance(declared_event_count, bool)
                or not isinstance(declared_event_count, int)
                or declared_event_count != len(events)
            ):
                raise CassetteError(
                    "manifest event_count does not match the native event log"
                )
            _validate_network_events(events, loaded.flows)
            if pinned_policy is not None:
                _config, resolutions = pinned_policy
                resolved_scopes = {
                    (resolution.channel.value, resolution.scope)
                    for resolution in resolutions
                }
                recorded_scopes = {
                    (event.channel.value, event.scope)
                    for event in events
                    if event.kind is EventKind.HTTP_EXCHANGE
                }
                if not recorded_scopes.issubset(resolved_scopes):
                    raise PolicyError(
                        "manifest policy does not resolve every recorded network scope"
                    )
        image_ref = manifest["image"]["ref"]
        image_digest = manifest["image"]["digest"]
        image_id = manifest["image"].get("id", image_digest)
        command = manifest["command"]
        t0_epoch = float(manifest["t0_epoch"])
        if (
            not isinstance(image_ref, str)
            or not isinstance(image_digest, str)
            or not isinstance(image_id, str)
            or not isinstance(command, list)
            or not all(isinstance(item, str) for item in command)
        ):
            raise CassetteError("manifest image, digest, or command is invalid")
    except (CassetteError, PolicyError, KeyError, TypeError, ValueError) as exc:
        raise HarnessError(f"cassette cannot be replayed: {exc}") from exc
    # The cassette is self-describing: only a rules file pinned inside it may
    # apply. A replayable.toml in the current directory must not change how a
    # recorded cassette is matched.
    rules_path: Path | None = cassette / "replayable.toml"
    if not rules_path.is_file():
        rules_path = None
    try:
        rules = load_rules(rules_path)
    except RulesError as exc:
        raise HarnessError(f"normalization rules are invalid: {exc}") from exc
    recorded_ruleset = manifest.get("ruleset_version")
    if recorded_ruleset is not None and recorded_ruleset != rules.version:
        raise HarnessError(
            "normalization rules do not match the cassette manifest; restore the "
            "replayable.toml inside the cassette to the recorded version or "
            "record a new cassette"
        )
    ca_path = (ca_path or context.default_ca_path()).expanduser().resolve()
    context.require_ca(ca_path)
    context.require_ca_valid_at(ca_path, t0_epoch)
    image = context.select_replay_image(
        image_ref=image_ref,
        image_digest=image_digest,
        image_id=image_id,
        allow_image_mismatch=allow_image_mismatch,
    )
    # Already cleared by `_invalidate_replay_verdicts` above; nothing between
    # there and here writes them.
    report_path = cassette / REPLAY_REPORT_FILE_NAME
    state_path = cassette / REPLAY_STATE_FILE_NAME
    run_id = uuid.uuid4().hex[:12]
    environment_names = manifest.get("env_names", [])
    if not isinstance(environment_names, list) or not all(
        isinstance(name, str) for name in environment_names
    ):
        raise HarnessError("cassette manifest env_names must be an array of strings")
    secret_environment_names = manifest.get("secret_env_names", environment_names)
    nonsecret_environment = manifest.get("nonsecret_env", {})
    if not isinstance(secret_environment_names, list) or not all(
        isinstance(name, str) for name in secret_environment_names
    ):
        raise HarnessError(
            "cassette manifest secret_env_names must be an array of strings"
        )
    if not isinstance(nonsecret_environment, dict) or not all(
        isinstance(name, str) and isinstance(value, str)
        for name, value in nonsecret_environment.items()
    ):
        raise HarnessError("cassette manifest nonsecret_env must be a string mapping")
    if fork_at is not None:
        return _fork_replay_run(
            cassette=cassette,
            manifest=manifest,
            flow_count=len(loaded.flows),
            image=image,
            image_ref=image_ref,
            image_digest=image_digest,
            image_id=image_id,
            command=command,
            t0_epoch=t0_epoch,
            rules_path=rules_path,
            rules_version=rules.version,
            environment_names=environment_names,
            secret_environment_names=secret_environment_names,
            nonsecret_environment=nonsecret_environment,
            fork_at=fork_at,
            env_file=env_file,
            out_workspace=out_workspace,
            port=port,
            ca_path=ca_path,
            timeout_seconds=timeout_seconds,
            listen_host=listen_host,
            context=context,
        )
    if env_file is not None:
        raise HarnessError("--env-file is only valid with --fork-at")
    replay_user_environment = dict(nonsecret_environment)
    # Use the same token record wrote into bodies/transcripts so body-auth APIs
    # and echoed secrets stay matchable without real credentials.
    replay_user_environment.update(
        {name: redacted_placeholder(name) for name in secret_environment_names}
    )
    replay_fingerprint = env_fingerprint(
        replay_user_environment,
        secret_names=secret_environment_names,
    )
    if manifest.get("env_fingerprint") != replay_fingerprint:
        print(
            "replayable: warning: replay environment differs from the recorded "
            "environment fingerprint",
            file=sys.stderr,
        )
    replay_environment = {
        **replay_user_environment,
        **context.replay_time_environment(t0_epoch),
    }
    run_log = cassette / REPLAY_LOG_FILE_NAME
    run_log.write_text("", encoding="utf-8")

    with _runtime_workspace(out_workspace) as runtime_workspace:
        container_command = context.docker_command(
            image=image,
            command=command,
            port=port,
            ca_path=ca_path,
            run_id=run_id,
            workspace=runtime_workspace,
            extra_environment=replay_environment,
        )
        _log_event(run_log, "proxy_start", mode="replay", port=port)
        with context.proxy_process(
            addon=_addon_path("replay_addon.py"),
            port=port,
            addon_environment={
                "REPLAYABLE_CASSETTE_DIR": str(cassette),
                "REPLAYABLE_REPORT_FILE": str(report_path),
                "REPLAYABLE_STATE_FILE": str(state_path),
                "REPLAYABLE_RULES_FILE": str(rules_path) if rules_path else "",
            },
            log_path=cassette / REPLAY_PROXY_LOG_FILE_NAME,
            listen_host=listen_host,
            confdir=context.mitmproxy_confdir_for_ca(ca_path),
        ):
            _log_event(
                run_log,
                "container_start",
                mode="replay",
                run_id=run_id,
                image=image,
            )
            started = time.monotonic()
            return_code = context.run_container(
                container_command,
                stdout_path=cassette / REPLAY_STDOUT_FILE_NAME,
                stderr_path=cassette / REPLAY_STDERR_FILE_NAME,
                container_name=f"replayable-{run_id}",
                timeout_seconds=timeout_seconds,
            )
            (cassette / REPLAY_STDOUT_FILE_NAME).touch(exist_ok=True)
            (cassette / REPLAY_STDERR_FILE_NAME).touch(exist_ok=True)
            replay_wall_time = time.monotonic() - started
            _log_event(
                run_log,
                "container_exit",
                mode="replay",
                return_code=return_code,
                wall_time_seconds=replay_wall_time,
            )

        with tempfile.TemporaryDirectory(prefix="replayable-snapshot-") as snapshot_dir:
            try:
                replay_snapshot = create_snapshot(
                    runtime_workspace,
                    Path(snapshot_dir),
                )
            except SnapshotError as exc:
                raise HarnessError(f"workspace snapshot failed: {exc}") from exc
            replay_workspace_hash = replay_snapshot.sha256
            replay_files = replay_snapshot.files

    report = _load_optional_json(report_path)
    final_code: ExitCode
    if report is not None:
        _print_mismatch_summary(report)
        final_code = ExitCode.REPLAY_MISMATCH
    else:
        state = _load_optional_json(state_path)
        if state is None:
            raise HarnessError(
                f"replay state was not written at {state_path}; inspect replay-proxy.log"
            )
        unconsumed = state.get("unconsumed_sequences", [])
        if not isinstance(unconsumed, list):
            raise HarnessError(f"replay state is invalid at {state_path}")
        if unconsumed:
            message = f"replay left {len(unconsumed)} unconsumed flow(s): {unconsumed}"
            if strict:
                print(f"replayable: {message}", file=sys.stderr)
                final_code = ExitCode.REPLAY_MISMATCH
            else:
                print(f"replayable: warning: {message}", file=sys.stderr)
                final_code = (
                    ExitCode.SUCCESS if return_code == 0 else ExitCode.AGENT_FAILED
                )
        else:
            final_code = ExitCode.SUCCESS if return_code == 0 else ExitCode.AGENT_FAILED

    workspace_matches, workspace_diff = _verify_workspace(
        cassette,
        run_log,
        replay_workspace_hash=replay_workspace_hash,
        replay_files=replay_files,
    )
    if not workspace_matches:
        final_code = ExitCode.REPLAY_MISMATCH

    stdout_hash = _sha256_path(cassette / REPLAY_STDOUT_FILE_NAME)
    if not _verify_transcripts(cassette, manifest, replay_stdout_hash=stdout_hash):
        final_code = ExitCode.REPLAY_MISMATCH

    last_replay = {
        "exit_code": int(final_code),
        "wall_time_seconds": replay_wall_time,
        "workspace_sha256": replay_workspace_hash,
        "stdout_sha256": stdout_hash,
        "workspace_diff": workspace_diff,
    }
    (cassette / LAST_REPLAY_FILE_NAME).write_text(
        json.dumps(last_replay, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _log_event(run_log, "replay_complete", **last_replay)
    return final_code


def _fork_environment(
    *,
    env_file: Path | None,
    environment_names: list[str],
    secret_environment_names: list[str],
    nonsecret_environment: dict[str, str],
    live_required: bool,
) -> tuple[dict[str, str], dict[str, str]]:
    supplied: dict[str, str] = {}
    if env_file is not None:
        path = env_file.expanduser().resolve()
        if not path.is_file():
            raise HarnessError(f"environment file not found at {path}; check --env-file")
        try:
            supplied = parse_env_file(path)
        except EnvFileError as exc:
            raise HarnessError(f"environment file is invalid: {exc}") from exc

    recorded_names = set(environment_names)
    unexpected = sorted(set(supplied) - recorded_names)
    if unexpected:
        raise HarnessError(
            "fork environment contains variables absent from the recording: "
            + ", ".join(unexpected)
        )
    changed_nonsecret = sorted(
        name
        for name, value in supplied.items()
        if name in nonsecret_environment and nonsecret_environment[name] != value
    )
    if changed_nonsecret:
        raise HarnessError(
            "fork environment changes recorded non-secret variables: "
            + ", ".join(changed_nonsecret)
        )

    missing_secrets = sorted(name for name in secret_environment_names if not supplied.get(name))
    if live_required and missing_secrets:
        raise HarnessError(
            "live fork requires recorded secret variables in --env-file: "
            + ", ".join(missing_secrets)
        )
    environment = dict(nonsecret_environment)
    environment.update({name: redacted_placeholder(name) for name in secret_environment_names})
    environment.update(supplied)
    redaction_secrets = {
        name: supplied[name] for name in secret_environment_names if supplied.get(name)
    }
    return environment, redaction_secrets


def _capture_manifest(
    *,
    manifest: dict[str, Any],
    environment: dict[str, str],
    secret_names_: list[str],
    t0_epoch: float,
    rules_version: str,
) -> dict[str, Any]:
    value = base_manifest(
        created_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        t0_epoch=t0_epoch,
        image_ref=manifest["image"]["ref"],
        image_digest=manifest["image"]["digest"],
        image_id=manifest["image"].get("id"),
        command=list(manifest["command"]),
        environment_fingerprint=env_fingerprint(
            environment,
            secret_names=secret_names_,
        ),
        ruleset_version=rules_version,
    )
    value["env_names"] = sorted(environment)
    value["secret_env_names"] = sorted(secret_names_)
    value["nonsecret_env"] = {
        name: item for name, item in sorted(environment.items()) if name not in secret_names_
    }
    return value


def _copy_blob_directory(source: Path, destination: Path) -> None:
    source_blobs = source / "blobs"
    if source_blobs.is_dir():
        shutil.copytree(source_blobs, destination / "blobs", dirs_exist_ok=True)


def _build_composite_candidate(
    *,
    destination: Path,
    baseline: Path,
    live_capture: Path,
    manifest: dict[str, Any],
    fork_at: int,
    return_code: int,
    wall_time_seconds: float,
    workspace_source: Path,
    stdout_source: Path,
    stderr_source: Path,
) -> Observation:
    writer = CassetteWriter(destination)
    candidate_manifest = json.loads(json.dumps(manifest))
    candidate_manifest.update(flow_count=0, event_count=0)
    writer.initialize(candidate_manifest)
    _copy_blob_directory(baseline, destination)
    _copy_blob_directory(live_capture, destination)

    baseline_flows = CassetteReader(baseline).load_flows().flows[:fork_at]
    live_flows = CassetteReader(live_capture).load_flows().flows
    baseline_events = EventLogReader(baseline).load_events()[:fork_at]
    live_events = EventLogReader(live_capture).load_events()
    sources = list(zip(baseline_flows, baseline_events, strict=True)) + list(
        zip(live_flows, live_events, strict=True)
    )
    for sequence, (source_flow, source_event) in enumerate(sources, start=1):
        flow = json.loads(json.dumps(source_flow))
        flow["seq"] = sequence
        metrics = source_event.payload.get("metrics")
        event = event_from_flow(
            flow,
            lamport=sequence,
            metrics=metrics if isinstance(metrics, dict) else None,
        )
        writer.append_flow(flow, event=event)

    shutil.copyfile(stdout_source, destination / AGENT_STDOUT_FILE_NAME)
    shutil.copyfile(stderr_source, destination / AGENT_STDERR_FILE_NAME)
    shutil.copyfile(workspace_source / "workspace.sha256", destination / "workspace.sha256")
    shutil.copyfile(
        workspace_source / "workspace.files.json",
        destination / "workspace.files.json",
    )
    run_log = destination / RUN_LOG_FILE_NAME
    run_log.write_text("", encoding="utf-8")
    _log_event(
        run_log,
        "container_exit",
        mode="record",
        return_code=return_code,
        wall_time_seconds=wall_time_seconds,
    )
    workspace_hash = (destination / "workspace.sha256").read_text(encoding="utf-8").strip()
    writer.update_manifest(
        flow_count=len(sources),
        event_count=len(sources),
        record_exit_code=return_code,
        record_wall_time_seconds=wall_time_seconds,
        workspace_sha256=workspace_hash,
        stdout_sha256=_sha256_path(destination / AGENT_STDOUT_FILE_NAME),
        stderr_sha256=_sha256_path(destination / AGENT_STDERR_FILE_NAME),
    )
    return build_observation(destination)


def _event_summaries(events: list[Event]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for event in events:
        flow = event.payload.get("flow")
        response = flow.get("response") if isinstance(flow, dict) else None
        chunks = response.get("sse_chunks") if isinstance(response, dict) else None
        summary: dict[str, Any] = {
            "seq": event.seq,
            "lamport": event.lamport,
            "t_rel": event.t_rel,
            "channel": event.channel.value,
            "kind": event.kind.value,
            "scope": event.scope,
            "key": event.key,
            "duration_seconds": event.payload.get("duration_seconds"),
            "stream_chunk_count": len(chunks) if isinstance(chunks, list) else 0,
        }
        metrics = event.payload.get("metrics")
        if isinstance(metrics, dict):
            summary["metrics"] = metrics
        summaries.append(summary)
    return summaries


def _fork_replay_run(
    *,
    cassette: Path,
    manifest: dict[str, Any],
    flow_count: int,
    image: str,
    image_ref: str,
    image_digest: str,
    image_id: str,
    command: list[str],
    t0_epoch: float,
    rules_path: Path | None,
    rules_version: str,
    environment_names: list[str],
    secret_environment_names: list[str],
    nonsecret_environment: dict[str, str],
    fork_at: int,
    env_file: Path | None,
    out_workspace: Path | None,
    port: int,
    ca_path: Path,
    timeout_seconds: float | None,
    listen_host: str,
    context: RunContext,
) -> ExitCode:
    if (
        isinstance(fork_at, bool)
        or not isinstance(fork_at, int)
        or fork_at < 0
        or fork_at > flow_count
    ):
        raise HarnessError(f"--fork-at must be between 0 and {flow_count}")
    environment, redaction_secrets = _fork_environment(
        env_file=env_file,
        environment_names=environment_names,
        secret_environment_names=secret_environment_names,
        nonsecret_environment=nonsecret_environment,
        # Once the prefix is consumed, any additional request is live—even when
        # the boundary equals the recorded flow count.
        live_required=True,
    )
    try:
        baseline_observation = build_observation(cassette)
    except ObservationError as exc:
        raise HarnessError(f"baseline observation is invalid: {exc}") from exc

    report_path = cassette / FORK_REPORT_FILE_NAME
    state_path = cassette / FORK_STATE_FILE_NAME
    for stale in (
        report_path,
        state_path,
        cassette / FORK_RESULT_FILE_NAME,
        cassette / FORK_STDOUT_FILE_NAME,
        cassette / FORK_STDERR_FILE_NAME,
        cassette / FORK_LOG_FILE_NAME,
        cassette / FORK_PROXY_LOG_FILE_NAME,
    ):
        stale.unlink(missing_ok=True)
    run_log = cassette / FORK_LOG_FILE_NAME
    run_log.write_text("", encoding="utf-8")
    run_id = uuid.uuid4().hex[:12]

    with (
        tempfile.TemporaryDirectory(prefix="replayable-fork-live-") as live_name,
        tempfile.TemporaryDirectory(prefix="replayable-fork-candidate-") as candidate_name,
        _runtime_workspace(out_workspace) as runtime_workspace,
        _secret_values_file(redaction_secrets) as secrets_path,
    ):
        live_capture = Path(live_name)
        candidate = Path(candidate_name)
        capture_t0 = time.time()
        capture_writer = CassetteWriter(live_capture)
        capture_writer.initialize(
            _capture_manifest(
                manifest=manifest,
                environment=environment,
                secret_names_=secret_environment_names,
                t0_epoch=capture_t0,
                rules_version=rules_version,
            )
        )
        container_command = context.docker_command(
            image=image,
            command=command,
            port=port,
            ca_path=ca_path,
            run_id=run_id,
            workspace=runtime_workspace,
            # Keep credentials out of the docker process argument vector.
            # Validation above guarantees this file contains only recorded
            # variables and unchanged non-secret values.
            env_file=env_file.expanduser().resolve() if env_file else None,
            extra_environment={
                **nonsecret_environment,
                **context.replay_time_environment(t0_epoch),
            },
        )
        _log_event(run_log, "proxy_start", mode="fork", port=port, fork_at=fork_at)
        with context.proxy_process(
            addon=_addon_path("fork_addon.py"),
            port=port,
            addon_environment={
                "REPLAYABLE_CASSETTE_DIR": str(cassette),
                "REPLAYABLE_FORK_CAPTURE_DIR": str(live_capture),
                "REPLAYABLE_FORK_AT": str(fork_at),
                "REPLAYABLE_REPORT_FILE": str(report_path),
                "REPLAYABLE_STATE_FILE": str(state_path),
                "REPLAYABLE_RULES_FILE": str(rules_path) if rules_path else "",
                "REPLAYABLE_SECRET_VALUES_FILE": (
                    str(secrets_path) if secrets_path is not None else ""
                ),
            },
            log_path=cassette / FORK_PROXY_LOG_FILE_NAME,
            listen_host=listen_host,
            confdir=context.mitmproxy_confdir_for_ca(ca_path),
        ):
            _log_event(
                run_log,
                "container_start",
                mode="fork",
                run_id=run_id,
                image_ref=image_ref,
                image_digest=image_digest,
                image_id=image_id,
            )
            started = time.monotonic()
            return_code = context.run_container(
                container_command,
                stdout_path=cassette / FORK_STDOUT_FILE_NAME,
                stderr_path=cassette / FORK_STDERR_FILE_NAME,
                redaction_secrets=redaction_secrets,
                container_name=f"replayable-{run_id}",
                timeout_seconds=timeout_seconds,
            )
            (cassette / FORK_STDOUT_FILE_NAME).touch(exist_ok=True)
            (cassette / FORK_STDERR_FILE_NAME).touch(exist_ok=True)
            wall_time = time.monotonic() - started
            _log_event(
                run_log,
                "container_exit",
                mode="fork",
                return_code=return_code,
                wall_time_seconds=wall_time,
            )

        try:
            workspace_snapshot = create_snapshot(runtime_workspace, live_capture)
        except SnapshotError as exc:
            raise HarnessError(f"workspace snapshot failed: {exc}") from exc
        shutil.copyfile(
            cassette / FORK_STDOUT_FILE_NAME,
            live_capture / AGENT_STDOUT_FILE_NAME,
        )
        shutil.copyfile(
            cassette / FORK_STDERR_FILE_NAME,
            live_capture / AGENT_STDERR_FILE_NAME,
        )
        live_log = live_capture / RUN_LOG_FILE_NAME
        live_log.write_text("", encoding="utf-8")
        _log_event(
            live_log,
            "container_exit",
            mode="record",
            return_code=return_code,
            wall_time_seconds=wall_time,
        )
        try:
            live_flows = CassetteReader(live_capture).load_flows().flows
            live_events = EventLogReader(live_capture).load_events()
            _validate_network_events(live_events, live_flows)
            capture_writer.update_manifest(
                flow_count=len(live_flows),
                event_count=len(live_events),
                record_exit_code=return_code,
                record_wall_time_seconds=wall_time,
                workspace_sha256=workspace_snapshot.sha256,
                stdout_sha256=_sha256_path(live_capture / AGENT_STDOUT_FILE_NAME),
                stderr_sha256=_sha256_path(live_capture / AGENT_STDERR_FILE_NAME),
            )
            live_observation = build_observation(live_capture)
            candidate_observation = _build_composite_candidate(
                destination=candidate,
                baseline=cassette,
                live_capture=live_capture,
                manifest=manifest,
                fork_at=fork_at,
                return_code=return_code,
                wall_time_seconds=wall_time,
                workspace_source=live_capture,
                stdout_source=cassette / FORK_STDOUT_FILE_NAME,
                stderr_source=cassette / FORK_STDERR_FILE_NAME,
            )
        except (CassetteError, ObservationError, SnapshotError) as exc:
            raise HarnessError(f"fork capture is invalid: {exc}") from exc

        report = _load_optional_json(report_path)
        state = _load_optional_json(state_path)
        if state is None:
            raise HarnessError(
                f"fork state was not written at {state_path}; inspect {FORK_PROXY_LOG_FILE_NAME}"
            )
        try:
            result = build_fork_result(
                baseline=baseline_observation,
                candidate=candidate_observation,
                live=live_observation,
                state=state,
                captured_flow_count=len(live_flows),
                wall_time_seconds=wall_time,
                events=_event_summaries(live_events),
            )
            result["exit_code"] = int(ExitCode.SUCCESS)
            result["replay_mismatch"] = report
            unconsumed = state.get("unconsumed_sequences")
            if not isinstance(unconsumed, list):
                raise ForkResultError("fork state unconsumed_sequences must be an array")
            mismatch = (
                report is not None
                or bool(unconsumed)
                or state.get("live_errors") != 0
                or not result["downstream"]["matches"]
            )
            final_code = (
                ExitCode.REPLAY_MISMATCH
                if mismatch
                else ExitCode.SUCCESS
                if return_code == 0
                else ExitCode.AGENT_FAILED
            )
            result["exit_code"] = int(final_code)
            write_fork_result(cassette, result)
        except ForkResultError as exc:
            raise HarnessError(f"fork result is invalid: {exc}") from exc
    _log_event(
        run_log,
        "fork_complete",
        fork_at=fork_at,
        exit_code=int(final_code),
        live_flow_count=len(live_flows),
    )
    return final_code


def _verify_workspace(
    cassette: Path,
    run_log: Path,
    *,
    replay_workspace_hash: str,
    replay_files: list[dict[str, Any]],
) -> tuple[bool, dict[str, list[str]] | None]:
    """Compare the replay workspace with the recorded baseline, if one exists."""

    workspace_hash_path = cassette / "workspace.sha256"
    workspace_files_path = cassette / "workspace.files.json"
    if not workspace_hash_path.exists() and not workspace_files_path.exists():
        return True, None
    try:
        recorded_workspace_hash, recorded_files = load_recorded_snapshot(cassette)
    except SnapshotError as exc:
        raise HarnessError(f"workspace baseline is invalid: {exc}") from exc

    if replay_workspace_hash == recorded_workspace_hash:
        print("DETERMINISTIC ✓ (workspace sha256 matches)")
        _log_event(run_log, "workspace_match", sha256=replay_workspace_hash)
        return True, None

    workspace_diff = diff_file_manifests(recorded_files, replay_files)
    print("replayable: workspace differs from recording", file=sys.stderr)
    for category in ("added", "removed", "changed"):
        if workspace_diff[category]:
            print(
                f"  {category}: {', '.join(workspace_diff[category])}",
                file=sys.stderr,
            )
    if (
        workspace_diff["removed"]
        and not workspace_diff["added"]
        and not workspace_diff["changed"]
    ):
        print(
            "  hint: only recorded files are missing; if the workload reads "
            "input files from /workspace, pre-seed an identical workspace and "
            "pass it with --out-workspace",
            file=sys.stderr,
        )
    _log_event(
        run_log,
        "workspace_mismatch",
        sha256=replay_workspace_hash,
        diff=workspace_diff,
    )
    return False, workspace_diff


def _verify_transcripts(
    cassette: Path,
    manifest: dict[str, Any],
    *,
    replay_stdout_hash: str,
) -> bool:
    """Compare replay stdout against the recording; warn (only) on stderr drift."""

    recorded_stdout = cassette / AGENT_STDOUT_FILE_NAME
    recorded_stdout_hash = manifest.get("stdout_sha256")
    if not isinstance(recorded_stdout_hash, str) and recorded_stdout.is_file():
        recorded_stdout_hash = _sha256_path(recorded_stdout)

    recorded_stderr_hash = manifest.get("stderr_sha256")
    recorded_stderr = cassette / AGENT_STDERR_FILE_NAME
    if not isinstance(recorded_stderr_hash, str) and recorded_stderr.is_file():
        recorded_stderr_hash = _sha256_path(recorded_stderr)
    if (
        isinstance(recorded_stderr_hash, str)
        and _sha256_path(cassette / REPLAY_STDERR_FILE_NAME) != recorded_stderr_hash
    ):
        print(
            "replayable: warning: agent stderr differs from the recorded "
            "transcript (informational only)",
            file=sys.stderr,
        )

    if (
        isinstance(recorded_stdout_hash, str)
        and replay_stdout_hash != recorded_stdout_hash
    ):
        print(
            "replayable: agent stdout differs from the recorded transcript",
            file=sys.stderr,
        )
        return False
    return True


def _load_optional_json(path: Path) -> dict | None:
    if not path.is_file() or not path.stat().st_size:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessError(f"replay output is invalid at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise HarnessError(f"replay output at {path} must be a JSON object")
    return value


def _print_mismatch_summary(report: dict) -> None:
    live = report.get("live_request", {})
    print(
        f"replayable: mismatch: {live.get('method', '?')} {live.get('path', '?')}",
        file=sys.stderr,
    )
    diff = report.get("diff", "")
    if not isinstance(diff, str) or not diff:
        return
    hunks: list[list[str]] = []
    current: list[str] = []
    for line in diff.splitlines():
        if line.startswith("@@"):
            if current:
                hunks.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current:
        hunks.append(current)
    selected_lines = [line for hunk in hunks[:5] for line in hunk] or diff.splitlines()[
        :20
    ]
    print("\n".join(selected_lines), file=sys.stderr)
