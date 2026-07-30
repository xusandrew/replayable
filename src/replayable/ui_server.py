"""Loopback-only JSON API and static dashboard server."""

from __future__ import annotations

import json
import mimetypes
import shutil
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from replayable.cassette import CassetteError, CassetteReader
from replayable.cassette.events import EventLogReader
from replayable.core.orchestrator import record_run, replay_run
from replayable.errors import HarnessError
from replayable.exit_codes import ExitCode
from replayable.matcher import normalize_request, raw_request_from_record
from replayable.normalize_rules import load_rules
from replayable.verdict.fork_result import FORK_RESULT_FILE_NAME
from replayable.verdict.observation import (
    OBSERVATION_FILE_NAME,
    ObservationError,
    build_observation,
)

MAX_REQUEST_BODY = 64 * 1024
JSON_HEADERS = {"content-type": "application/json; charset=utf-8"}
ReplayExecutor = Callable[..., ExitCode]
RecordExecutor = Callable[..., ExitCode]


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    headers: dict[str, str]

    @classmethod
    def json(cls, status: int, value: Any) -> Response:
        return cls(
            status,
            (
                json.dumps(
                    value,
                    allow_nan=False,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8"),
            JSON_HEADERS,
        )


class APIError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite number {value}")


def _json_file(path: Path, *, required: bool = True) -> dict[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if required:
            raise APIError(HTTPStatus.NOT_FOUND, f"{path.name} is not available") from None
        return None
    except OSError as exc:
        raise APIError(HTTPStatus.INTERNAL_SERVER_ERROR, f"cannot read {path.name}") from exc
    try:
        value = json.loads(raw, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise APIError(
            HTTPStatus.INTERNAL_SERVER_ERROR, f"{path.name} contains invalid JSON"
        ) from exc
    if not isinstance(value, dict):
        raise APIError(HTTPStatus.INTERNAL_SERVER_ERROR, f"{path.name} must contain an object")
    return value


def _summary_event(event) -> dict[str, Any]:
    flow = event.payload.get("flow")
    response = flow.get("response") if isinstance(flow, dict) else None
    chunks = response.get("sse_chunks") if isinstance(response, dict) else None
    result: dict[str, Any] = {
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
        result["metrics"] = metrics
    return result


class UIApp:
    """Pure request router; the HTTP adapter only handles transport details."""

    def __init__(
        self,
        cassette_root: Path,
        *,
        static_dir: Path,
        allow_write: bool = False,
        replay_executor: ReplayExecutor = replay_run,
        record_executor: RecordExecutor = record_run,
    ) -> None:
        self.cassette_root = cassette_root.expanduser().resolve()
        self.static_dir = static_dir.expanduser().resolve()
        self.allow_write = allow_write
        self.replay_executor = replay_executor
        self.record_executor = record_executor
        self.write_lock = threading.Lock()

    def handle(
        self,
        method: str,
        target: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
    ) -> Response:
        try:
            if len(body) > MAX_REQUEST_BODY:
                raise APIError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body is too large")
            parsed = urlsplit(target)
            if parsed.path.startswith("/api/"):
                return self._api(
                    method.upper(),
                    [unquote(part) for part in parsed.path.split("/") if part],
                    parse_qs(parsed.query),
                    {name.lower(): value for name, value in (headers or {}).items()},
                    body,
                )
            if method.upper() not in {"GET", "HEAD"}:
                raise APIError(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed")
            return self._static(parsed.path, head=method.upper() == "HEAD")
        except APIError as exc:
            return Response.json(exc.status, {"error": str(exc)})
        except (CassetteError, ObservationError, HarnessError) as exc:
            return Response.json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)})
        except OSError as exc:
            return Response.json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": f"local operation failed: {exc}"},
            )
        except (KeyError, TypeError, ValueError) as exc:
            return Response.json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": f"cassette data is invalid: {exc}"},
            )

    def _api(
        self,
        method: str,
        parts: list[str],
        query: dict[str, list[str]],
        headers: dict[str, str],
        body: bytes,
    ) -> Response:
        if parts == ["api", "cassettes"] and method == "GET":
            return Response.json(HTTPStatus.OK, {"cassettes": self._list_cassettes()})
        if len(parts) < 3 or parts[:2] != ["api", "cassettes"]:
            raise APIError(HTTPStatus.NOT_FOUND, "API route not found")
        cassette = self._cassette(parts[2])
        tail = parts[3:]

        if method == "GET":
            if tail == ["timeline"]:
                events = EventLogReader(cassette).load_events()
                return Response.json(
                    HTTPStatus.OK, {"events": [_summary_event(event) for event in events]}
                )
            if len(tail) == 2 and tail[0] == "flows":
                return Response.json(
                    HTTPStatus.OK, self._flow(cassette, self._positive_int(tail[1]))
                )
            if tail == ["explain"]:
                flow_values = query.get("flow", [])
                if len(flow_values) != 1:
                    raise APIError(HTTPStatus.BAD_REQUEST, "flow query parameter is required")
                return Response.json(
                    HTTPStatus.OK,
                    self._explain(cassette, self._positive_int(flow_values[0])),
                )
            artifact = {
                ("mismatch",): "replay-report.json",
                ("observation",): OBSERVATION_FILE_NAME,
                ("fork-result",): FORK_RESULT_FILE_NAME,
            }.get(tuple(tail))
            if artifact is not None:
                if artifact == OBSERVATION_FILE_NAME and not (cassette / artifact).is_file():
                    return Response.json(HTTPStatus.OK, build_observation(cassette).as_dict())
                return Response.json(HTTPStatus.OK, _json_file(cassette / artifact))
            if tail == ["diff"]:
                return Response.json(HTTPStatus.OK, self._diff(cassette))

        if method == "POST" and len(tail) == 1:
            self._authorize_write(headers)
            payload = self._request_json(body)
            if not self.write_lock.acquire(blocking=False):
                raise APIError(HTTPStatus.CONFLICT, "another write action is running")
            try:
                return self._write_action(cassette, tail[0], payload)
            finally:
                self.write_lock.release()
        raise APIError(HTTPStatus.NOT_FOUND, "API route not found")

    def _cassette(self, name: str) -> Path:
        if not self._valid_name(name):
            raise APIError(HTTPStatus.BAD_REQUEST, "invalid cassette name")
        path = (self.cassette_root / name).resolve()
        if path.parent != self.cassette_root or not path.is_dir():
            raise APIError(HTTPStatus.NOT_FOUND, "cassette not found")
        if not (path / "manifest.json").is_file():
            raise APIError(HTTPStatus.NOT_FOUND, "cassette manifest not found")
        return path

    def _list_cassettes(self) -> list[dict[str, Any]]:
        try:
            entries = sorted(self.cassette_root.iterdir(), key=lambda path: path.name)
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise APIError(HTTPStatus.INTERNAL_SERVER_ERROR, "cannot list cassette root") from exc
        results = []
        for entry in entries:
            if (
                entry.name.startswith(".")
                or not entry.is_dir()
                or not (entry / "manifest.json").is_file()
            ):
                continue
            manifest = CassetteReader(entry).load_manifest()
            fork_result = _json_file(entry / FORK_RESULT_FILE_NAME, required=False)
            last_replay = _json_file(entry / "last-replay.json", required=False)
            mismatch = (entry / "replay-report.json").is_file()
            exit_code = (
                fork_result.get("exit_code")
                if fork_result is not None
                else last_replay.get("exit_code")
                if last_replay is not None
                else None
            )
            results.append(
                {
                    "name": entry.name,
                    "flow_count": manifest.get("flow_count"),
                    "created_at": manifest.get("created_at"),
                    "image": manifest.get("image"),
                    "status": (
                        "mismatch"
                        if mismatch or exit_code == int(ExitCode.REPLAY_MISMATCH)
                        else "replayable"
                    ),
                    "last_exit_code": exit_code,
                    "has_observation": (entry / OBSERVATION_FILE_NAME).is_file(),
                    "has_fork_result": fork_result is not None,
                }
            )
        return results

    def _flow(self, cassette: Path, sequence: int) -> dict[str, Any]:
        reader = CassetteReader(cassette)
        flows = reader.load_flows().flows
        try:
            flow = next(item for item in flows if item.get("seq") == sequence)
        except StopIteration:
            raise APIError(HTTPStatus.NOT_FOUND, "flow not found") from None
        result = json.loads(json.dumps(flow))
        request = result.get("request")
        response = result.get("response")
        if isinstance(request, dict):
            request["body_decoded"] = reader.read_body(request.get("body")).decode(
                "utf-8", errors="replace"
            )
        if isinstance(response, dict):
            representation = response.get("body")
            response["body_decoded"] = (
                reader.read_body(representation).decode("utf-8", errors="replace")
                if representation is not None
                else ""
            )
        return result

    def _explain(self, cassette: Path, sequence: int) -> dict[str, Any]:
        reader = CassetteReader(cassette)
        flow = self._flow(cassette, sequence)
        request = raw_request_from_record(flow, reader)
        rules_path = cassette / "replayable.toml"
        rules = load_rules(rules_path if rules_path.is_file() else None)
        normalized = normalize_request(
            request,
            rules,
        )
        return {
            "flow": sequence,
            "match_key": normalized.match_key,
            "pre_hash": normalized.pre_hash,
            "canonical_body": normalized.canonical_body,
            "diff_body": normalized.diff_body,
            "rules": {
                "version": rules.version,
                "field_names": list(rules.field_names),
                "value_patterns": list(rules.value_patterns),
                "preserve": list(rules.preserve),
            },
        }

    def _diff(self, cassette: Path) -> dict[str, Any]:
        fork = _json_file(cassette / FORK_RESULT_FILE_NAME, required=False)
        if fork is not None:
            return {
                "kind": "fork",
                "exit_code": fork.get("exit_code"),
                "downstream": fork.get("downstream"),
            }
        mismatch = _json_file(cassette / "replay-report.json", required=False)
        if mismatch is not None:
            return {"kind": "mismatch", **mismatch}
        return {"kind": "none", "matches": True}

    def _authorize_write(self, headers: dict[str, str]) -> None:
        if not self.allow_write:
            raise APIError(HTTPStatus.FORBIDDEN, "write actions are disabled")
        content_type = headers.get("content-type", "").split(";", 1)[0].strip()
        if content_type != "application/json":
            raise APIError(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "write actions require application/json",
            )
        origin = headers.get("origin")
        if origin is not None:
            parsed = urlsplit(origin)
            if parsed.scheme != "http" or parsed.hostname not in {
                "127.0.0.1",
                "localhost",
                "::1",
            }:
                raise APIError(HTTPStatus.FORBIDDEN, "cross-origin write refused")
        host = headers.get("host")
        if host is not None:
            try:
                hostname = urlsplit(f"//{host}").hostname
            except ValueError as exc:
                raise APIError(HTTPStatus.FORBIDDEN, "invalid host header") from exc
            if hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise APIError(HTTPStatus.FORBIDDEN, "non-loopback host refused")

    def _request_json(self, body: bytes) -> dict[str, Any]:
        try:
            value = json.loads(body or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise APIError(HTTPStatus.BAD_REQUEST, "request body is invalid JSON") from exc
        if not isinstance(value, dict):
            raise APIError(HTTPStatus.BAD_REQUEST, "request body must be an object")
        return value

    def _write_action(self, cassette: Path, action: str, payload: dict[str, Any]) -> Response:
        if action in {"replay", "fork"}:
            strict = payload.get("strict", False)
            if not isinstance(strict, bool):
                raise APIError(HTTPStatus.BAD_REQUEST, "strict must be a boolean")
            kwargs: dict[str, Any] = {"cassette": cassette, "strict": strict}
            if action == "fork":
                fork_at = payload.get("fork_at")
                if isinstance(fork_at, bool) or not isinstance(fork_at, int):
                    raise APIError(HTTPStatus.BAD_REQUEST, "fork_at must be an integer")
                kwargs["fork_at"] = fork_at
                env_file = payload.get("env_file")
                if env_file is not None and not isinstance(env_file, str):
                    raise APIError(HTTPStatus.BAD_REQUEST, "env_file must be a path string")
                kwargs["env_file"] = Path(env_file) if env_file else None
            code = self.replay_executor(**kwargs)
            artifact = (
                _json_file(cassette / FORK_RESULT_FILE_NAME)
                if action == "fork"
                else _json_file(cassette / "last-replay.json")
            )
            return Response.json(
                HTTPStatus.OK,
                {"action": action, "exit_code": int(code), "result": artifact},
            )
        if action == "accept":
            destination = payload.get("destination")
            env_file = payload.get("env_file")
            if not isinstance(destination, str) or not destination:
                raise APIError(
                    HTTPStatus.BAD_REQUEST,
                    "accept requires a new destination cassette name",
                )
            if env_file is not None and not isinstance(env_file, str):
                raise APIError(HTTPStatus.BAD_REQUEST, "env_file must be a path string")
            target = self.cassette_root / destination
            if target.exists():
                raise APIError(
                    HTTPStatus.CONFLICT,
                    "destination exists; baseline replacement requires replayable accept",
                )
            # Validate the new name through the same single-segment rules.
            if not self._valid_name(destination) or target.resolve().parent != self.cassette_root:
                raise APIError(HTTPStatus.BAD_REQUEST, "invalid destination name")
            manifest = CassetteReader(cassette).load_manifest()
            temporary = Path(
                tempfile.mkdtemp(
                    dir=self.cassette_root,
                    prefix=f".{destination}.",
                )
            )
            try:
                code = self.record_executor(
                    image=manifest["image"]["ref"],
                    command=manifest["command"],
                    env_file=Path(env_file) if env_file else None,
                    out=temporary,
                )
                if code != ExitCode.SUCCESS:
                    raise APIError(
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        f"new baseline recording exited {int(code)}",
                    )
                temporary.replace(target)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
            return Response.json(
                HTTPStatus.CREATED,
                {
                    "action": "accept",
                    "exit_code": int(code),
                    "cassette": destination,
                },
            )
        raise APIError(HTTPStatus.NOT_FOUND, "write action not found")

    @staticmethod
    def _positive_int(value: str) -> int:
        try:
            parsed = int(value)
        except ValueError as exc:
            raise APIError(HTTPStatus.BAD_REQUEST, "flow must be an integer") from exc
        if parsed < 1:
            raise APIError(HTTPStatus.BAD_REQUEST, "flow must be positive")
        return parsed

    @staticmethod
    def _valid_name(value: str) -> bool:
        return (
            bool(value)
            and len(value.encode("utf-8")) <= 255
            and value not in {".", ".."}
            and not value.startswith(".")
            and "/" not in value
            and "\\" not in value
            and not any(ord(character) < 32 for character in value)
        )

    def _static(self, request_path: str, *, head: bool) -> Response:
        relative = PurePosixPath(request_path.lstrip("/") or "index.html")
        if relative.is_absolute() or ".." in relative.parts:
            raise APIError(HTTPStatus.NOT_FOUND, "static asset not found")
        path = (self.static_dir / Path(*relative.parts)).resolve()
        if path != self.static_dir and self.static_dir not in path.parents:
            raise APIError(HTTPStatus.NOT_FOUND, "static asset not found")
        if path.is_dir():
            path = path / "index.html"
        if not path.is_file() and "." not in relative.name:
            path = self.static_dir / "index.html"
        try:
            payload = path.read_bytes()
        except OSError:
            raise APIError(HTTPStatus.NOT_FOUND, "static asset not found") from None
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return Response(
            HTTPStatus.OK,
            b"" if head else payload,
            {
                "content-type": media_type,
                "content-length": str(len(payload)),
                "cache-control": (
                    "public, max-age=31536000, immutable"
                    if "_next" in relative.parts
                    else "no-cache"
                ),
            },
        )


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _handler(app: UIApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _respond(self) -> None:
            length_value = self.headers.get("content-length", "0")
            try:
                length = int(length_value)
            except ValueError:
                response = Response.json(
                    HTTPStatus.BAD_REQUEST, {"error": "invalid content-length"}
                )
            else:
                if length < 0 or length > MAX_REQUEST_BODY:
                    response = Response.json(
                        HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                        {"error": "request body is too large"},
                    )
                else:
                    response = app.handle(
                        self.command,
                        self.path,
                        headers=dict(self.headers.items()),
                        body=self.rfile.read(length) if length else b"",
                    )
            self.send_response(response.status)
            for name, value in response.headers.items():
                self.send_header(name, value)
            self.send_header("x-content-type-options", "nosniff")
            # Next's static export uses inline bootstrap records to hydrate.
            # No user-controlled HTML is rendered; API data is inserted by
            # React as text. Keep all network sources local while permitting
            # that generated bootstrap and Tailwind's inline style attributes.
            self.send_header(
                "content-security-policy",
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; "
                "connect-src 'self'; img-src 'self' data:",
            )
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(response.body)

        do_GET = _respond
        do_HEAD = _respond
        do_POST = _respond

        def log_message(self, format: str, *args: object) -> None:
            print(f"replayable ui: {self.address_string()} - {format % args}")

    return Handler


def create_server(
    *,
    cassette_root: Path,
    static_dir: Path,
    port: int,
    allow_write: bool,
) -> DashboardServer:
    """Create a loopback server without starting its request loop."""

    app = UIApp(
        cassette_root,
        static_dir=static_dir,
        allow_write=allow_write,
    )
    return DashboardServer(("127.0.0.1", port), _handler(app))


def serve(
    *,
    cassette_root: Path,
    static_dir: Path,
    port: int,
    allow_write: bool,
) -> None:
    """Serve until interrupted. Binding is deliberately not configurable."""

    with create_server(
        cassette_root=cassette_root,
        static_dir=static_dir,
        port=port,
        allow_write=allow_write,
    ) as server:
        print(f"replayable ui: http://127.0.0.1:{server.server_port}")
        server.serve_forever()
