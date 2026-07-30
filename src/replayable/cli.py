"""Typer CLI: record, replay, accept, inspect, UI, and diagnostics."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from replayable import doctor as doctor_module
from replayable.baseline import BaselineError, prepare_baseline
from replayable.exit_codes import ExitCode
from replayable.inspection import explain_match, inspect_cassette
from replayable.runner import (
    DEFAULT_PROXY_PORT,
    HarnessError,
    default_ca_path,
    record_run,
    replay_run,
)
from replayable.ui_server import serve

app = typer.Typer(
    name="replayable",
    help="Deterministic record/replay harness for containerized agent workloads.",
    no_args_is_help=True,
)


@app.command()
def record(
    image: Annotated[str, typer.Option("--image", help="Container image to run.")],
    command: Annotated[
        list[str],
        typer.Argument(help="Command and args to run inside the container."),
    ],
    workspace: Annotated[
        Path | None,
        typer.Option("--workspace", help="Host directory mounted at /workspace."),
    ] = None,
    env_file: Annotated[
        Path | None,
        typer.Option("--env-file", help="Env file passed into the container."),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Cassette output directory."),
    ] = None,
    port: Annotated[
        int,
        typer.Option("--port", help="Proxy port; 0 picks a free ephemeral port."),
    ] = DEFAULT_PROXY_PORT,
    ca_path: Annotated[
        Path | None,
        typer.Option("--ca-path", help="mitmproxy CA certificate path."),
    ] = None,
    timeout: Annotated[
        float | None,
        typer.Option("--timeout", help="Kill the container after this many seconds."),
    ] = None,
) -> None:
    """Record a container's HTTP traffic."""
    try:
        exit_code = record_run(
            image=image,
            command=command,
            workspace=workspace,
            env_file=env_file,
            out=out or Path("cassette"),
            port=port,
            ca_path=ca_path,
            timeout_seconds=timeout,
        )
    except HarnessError as exc:
        typer.echo(f"replayable: {exc}", err=True)
        raise typer.Exit(ExitCode.HARNESS_ERROR) from exc
    except OSError as exc:
        typer.echo(
            f"replayable: host filesystem or process error: {exc}; "
            "check the supplied paths and permissions",
            err=True,
        )
        raise typer.Exit(ExitCode.HARNESS_ERROR) from exc
    raise typer.Exit(exit_code)


@app.command()
def replay(
    cassette: Annotated[
        Path,
        typer.Option("--cassette", help="Cassette directory to replay."),
    ],
    strict: Annotated[
        bool,
        typer.Option("--strict", help="Treat unconsumed flows as a mismatch."),
    ] = False,
    fork_at: Annotated[
        int | None,
        typer.Option(
            "--fork-at",
            min=0,
            help="Serve N recorded flows, then continue against the live upstream.",
        ),
    ] = None,
    env_file: Annotated[
        Path | None,
        typer.Option(
            "--env-file",
            help="Recorded secrets required for the live part of a fork.",
        ),
    ] = None,
    out_workspace: Annotated[
        Path | None,
        typer.Option("--out-workspace", help="Directory for the replay workspace."),
    ] = None,
    allow_image_mismatch: Annotated[
        bool,
        typer.Option(
            "--allow-image-mismatch",
            help="Use the recorded image tag if its exact digest is unavailable.",
        ),
    ] = False,
    port: Annotated[
        int,
        typer.Option("--port", help="Proxy port; 0 picks a free ephemeral port."),
    ] = DEFAULT_PROXY_PORT,
    ca_path: Annotated[
        Path | None,
        typer.Option("--ca-path", help="mitmproxy CA certificate path."),
    ] = None,
    timeout: Annotated[
        float | None,
        typer.Option("--timeout", help="Kill the container after this many seconds."),
    ] = None,
) -> None:
    """Replay offline, or fork to live traffic after a recorded prefix."""
    try:
        exit_code = replay_run(
            cassette=cassette,
            strict=strict,
            fork_at=fork_at,
            env_file=env_file,
            out_workspace=out_workspace,
            allow_image_mismatch=allow_image_mismatch,
            port=port,
            ca_path=ca_path,
            timeout_seconds=timeout,
        )
    except HarnessError as exc:
        typer.echo(f"replayable: {exc}", err=True)
        raise typer.Exit(ExitCode.HARNESS_ERROR) from exc
    except OSError as exc:
        typer.echo(
            f"replayable: host filesystem or process error: {exc}; "
            "check the supplied paths and permissions",
            err=True,
        )
        raise typer.Exit(ExitCode.HARNESS_ERROR) from exc
    raise typer.Exit(exit_code)


@app.command()
def accept(
    cassette: Annotated[
        Path,
        typer.Option("--cassette", help="Existing cassette baseline to replace."),
    ],
    env_file: Annotated[
        Path | None,
        typer.Option(
            "--env-file",
            help="Secrets required to record the replacement baseline.",
        ),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Replace after preview without prompting."),
    ] = False,
    port: Annotated[
        int,
        typer.Option("--port", help="Proxy port; 0 picks a free ephemeral port."),
    ] = DEFAULT_PROXY_PORT,
    ca_path: Annotated[
        Path | None,
        typer.Option("--ca-path", help="mitmproxy CA certificate path."),
    ] = None,
    timeout: Annotated[
        float | None,
        typer.Option("--timeout", help="Kill the recording after this many seconds."),
    ] = None,
) -> None:
    """Record, review, and atomically replace an existing baseline."""

    try:
        with prepare_baseline(
            source=cassette,
            destination=cassette,
            env_file=env_file,
            record_executor=record_run,
            port=port,
            ca_path=ca_path,
            timeout_seconds=timeout,
        ) as candidate:
            typer.echo(candidate.preview)
            approved = yes or typer.confirm(
                f"Replace {candidate.destination} with this recording?",
                default=False,
            )
            if not approved:
                typer.echo("Baseline unchanged; candidate recording discarded.")
                raise typer.Exit(ExitCode.SUCCESS)
            candidate.publish(replace=True)
    except typer.Exit:
        raise
    except (BaselineError, HarnessError, OSError) as exc:
        typer.echo(f"replayable: baseline not accepted: {exc}", err=True)
        raise typer.Exit(ExitCode.HARNESS_ERROR) from exc
    typer.echo(f"Accepted replacement baseline at {cassette.expanduser().absolute()}.")
    raise typer.Exit(ExitCode.SUCCESS)


@app.command()
def inspect(
    cassette: Annotated[
        Path | None,
        typer.Option("--cassette", help="Cassette directory to inspect."),
    ] = None,
    flow: Annotated[
        int | None,
        typer.Option("--flow", help="Pretty-print a single flow by sequence number."),
    ] = None,
    explain_match_path: Annotated[
        Path | None,
        typer.Option(
            "--explain-match",
            help="Explain normalization for a request JSON file.",
        ),
    ] = None,
) -> None:
    """Inspect a cassette manifest and flows."""
    try:
        if explain_match_path is not None:
            typer.echo(explain_match(explain_match_path, cassette))
        elif cassette is not None:
            typer.echo(inspect_cassette(cassette, flow))
        else:
            raise HarnessError("--cassette or --explain-match is required")
    except HarnessError as exc:
        typer.echo(f"replayable: {exc}", err=True)
        raise typer.Exit(ExitCode.HARNESS_ERROR) from exc


@app.command()
def ui(
    cassette_root: Annotated[
        Path,
        typer.Option(
            "--cassette-root",
            help="Directory containing cassette subdirectories.",
        ),
    ] = Path("cassettes"),
    port: Annotated[
        int,
        typer.Option("--port", min=0, max=65535, help="Loopback HTTP port."),
    ] = 8765,
    static_dir: Annotated[
        Path | None,
        typer.Option(
            "--static-dir",
            help="Built dashboard assets; defaults to the packaged export.",
        ),
    ] = None,
    allow_write: Annotated[
        bool,
        typer.Option(
            "--allow-write",
            help="Enable replay, fork, and new-baseline API actions.",
        ),
    ] = False,
) -> None:
    """Serve the local cassette dashboard and API on 127.0.0.1."""

    assets = static_dir or Path(__file__).parent / "ui_static"
    try:
        serve(
            cassette_root=cassette_root,
            static_dir=assets,
            port=port,
            allow_write=allow_write,
        )
    except (HarnessError, OSError) as exc:
        typer.echo(f"replayable: UI server failed: {exc}", err=True)
        raise typer.Exit(ExitCode.HARNESS_ERROR) from exc


@app.command()
def doctor(
    ca_path: Annotated[
        Path | None,
        typer.Option("--ca-path", help="mitmproxy CA certificate path."),
    ] = None,
    port: Annotated[
        int,
        typer.Option("--port", help="Proxy port to check for availability."),
    ] = DEFAULT_PROXY_PORT,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON instead of a report."),
    ] = False,
    skip_container_checks: Annotated[
        bool,
        typer.Option(
            "--skip-container-checks",
            help="Skip diagnostics that need to start a container.",
        ),
    ] = False,
) -> None:
    """Diagnose the local environment before recording or replaying."""

    results = doctor_module.run_checks(
        ca_path=ca_path or default_ca_path(),
        port=port,
        include_docker_run=not skip_container_checks,
    )
    if json_output:
        typer.echo(doctor_module.render_json(results))
    else:
        typer.echo(doctor_module.render(results))

    if doctor_module.worst_status(results) is doctor_module.Status.FAIL:
        raise typer.Exit(ExitCode.HARNESS_ERROR)
    raise typer.Exit(ExitCode.SUCCESS)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
