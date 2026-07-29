"""Typer CLI: record, replay, inspect, doctor."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from replayable import doctor as doctor_module
from replayable.exit_codes import ExitCode
from replayable.inspection import explain_match, inspect_cassette
from replayable.runner import (
    DEFAULT_PROXY_PORT,
    HarnessError,
    default_ca_path,
    record_run,
    replay_run,
)

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
    """Replay a recorded container run without upstream network access."""
    try:
        exit_code = replay_run(
            cassette=cassette,
            strict=strict,
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
