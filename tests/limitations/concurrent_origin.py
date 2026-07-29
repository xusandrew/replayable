"""Number identical POST responses to expose concurrent FIFO assignment."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    counter = 0
    lock = threading.Lock()

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        self.rfile.read(length)
        with self.lock:
            self.__class__.counter += 1
            body = f"response-{self.counter}\n".encode()
        self.send_response(200)
        self.send_header("content-type", "text/plain")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8099), Handler)
    print("concurrent limitation origin listening on :8099", flush=True)
    server.serve_forever()
