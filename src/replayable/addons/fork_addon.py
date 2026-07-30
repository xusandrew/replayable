"""Hybrid replay addon: frozen prefix followed by redacted live capture."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from mitmproxy import http

from replayable.addons.record_addon import RecordAddon, _environment_secrets
from replayable.addons.replay_addon import (
    ReplayAddon,
    _path_from_environment,
    _write_json_atomic,
)
from replayable.redact import redact_body

SEGMENT_METADATA = "replayable.fork_segment"


class ForkReplayAddon(ReplayAddon):
    """Serve the configured prefix exactly, then let later requests go upstream."""

    def __init__(
        self,
        cassette_directory: Path | None = None,
        capture_directory: Path | None = None,
        report_path: Path | None = None,
        state_path: Path | None = None,
        rules_path: Path | None = None,
        fork_at: int | None = None,
        secrets: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            cassette_directory=cassette_directory,
            report_path=report_path,
            state_path=state_path,
            rules_path=rules_path,
            flow_limit=fork_at,
        )
        self.capture_directory = capture_directory
        self.fork_at = fork_at
        self.secrets = secrets
        self.recorder: RecordAddon | None = None
        self.live_requests = 0
        self.live_responses = 0
        self.live_errors = 0
        self.live_started_epoch: float | None = None
        self.live_completed_epoch: float | None = None

    def load(self, loader: Any) -> None:
        if self.fork_at is None:
            raw_fork_at = os.environ.get("REPLAYABLE_FORK_AT")
            try:
                self.fork_at = int(raw_fork_at or "")
            except ValueError as exc:
                raise RuntimeError("REPLAYABLE_FORK_AT must be an integer") from exc
        self.flow_limit = self.fork_at
        if self.capture_directory is None:
            self.capture_directory = _path_from_environment("REPLAYABLE_FORK_CAPTURE_DIR")
        if self.secrets is None:
            self.secrets = _environment_secrets()
        super().load(loader)
        self.recorder = RecordAddon(self.capture_directory, self.secrets)
        self.recorder.load(None)
        self._write_state()

    def request(self, flow: http.HTTPFlow) -> None:
        matcher = self._require_matcher()
        if matcher.unconsumed_sequences():
            flow.metadata[SEGMENT_METADATA] = "pinned"
            super().request(flow)
            return

        flow.metadata[SEGMENT_METADATA] = "live"
        self.live_requests += 1
        now = time.time()
        if self.live_started_epoch is None:
            self.live_started_epoch = now
        self.live_completed_epoch = now
        self._write_state()
        # Deliberately leave flow.response unset: mitmproxy connects upstream.

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        if flow.metadata.get(SEGMENT_METADATA) == "live":
            self._require_recorder().responseheaders(flow)

    def response(self, flow: http.HTTPFlow) -> None:
        if flow.metadata.get(SEGMENT_METADATA) != "live":
            return
        self._require_recorder().response(flow)
        self.live_responses += 1
        self.live_completed_epoch = time.time()
        self._write_state()

    def error(self, flow: http.HTTPFlow) -> None:
        if flow.metadata.get(SEGMENT_METADATA) != "live":
            return
        self.live_errors += 1
        self.live_completed_epoch = time.time()
        self._write_state()

    def _require_recorder(self) -> RecordAddon:
        if self.recorder is None:
            raise RuntimeError("fork capture recorder is not loaded")
        return self.recorder

    def _write_report(self, payload: dict[str, Any]) -> None:
        """Never persist live credentials embedded in mismatch diagnostics."""

        serialized = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        redacted = redact_body(serialized, self.secrets or {})
        value = json.loads(redacted)
        if not isinstance(value, dict):
            raise RuntimeError("redacted mismatch report must remain an object")
        super()._write_report(value)

    def _write_state(self) -> None:
        if self.state_path is None:
            raise RuntimeError("replay state path is not configured")
        matcher = self._require_matcher()
        unconsumed = matcher.unconsumed_sequences()
        pinned_target = self.flow_limit or 0
        _write_json_atomic(
            self.state_path,
            {
                "unconsumed_sequences": unconsumed,
                "pinned_target": pinned_target,
                "pinned_served": pinned_target - len(unconsumed),
                "live_requests": self.live_requests,
                "live_responses": self.live_responses,
                "live_errors": self.live_errors,
                "live_started_epoch": self.live_started_epoch,
                "live_completed_epoch": self.live_completed_epoch,
            },
        )


addons = [ForkReplayAddon()]
