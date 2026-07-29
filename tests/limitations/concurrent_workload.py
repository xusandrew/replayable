"""Issue two behaviorally identical requests at the same instant."""

from __future__ import annotations

import concurrent.futures
import threading
import urllib.request

URL = "http://host.docker.internal:8099/identical"
BARRIER = threading.Barrier(2)


def request(label: str) -> str:
    BARRIER.wait()
    live = urllib.request.Request(
        URL,
        data=b'{"same":"request"}',
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(live, timeout=10) as response:
        return f"{label}: {response.read().decode().strip()}"


with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
    futures = [executor.submit(request, label) for label in ("A", "B")]
    for future in concurrent.futures.as_completed(futures):
        print(future.result(), flush=True)
