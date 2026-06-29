#!/usr/bin/env python3
import argparse
import json
import re
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Condition, Lock

INITIAL_KEY_ID = "EXPIRED_TOKEN_INITIAL"
REFRESHED_KEY_ID = "EXPIRED_TOKEN_REFRESHED"
EXPIRED_TOKEN_BODY = b"""<?xml version="1.0" encoding="UTF-8"?>
<Error><Code>ExpiredToken</Code><Message>The provided token has expired.</Message></Error>"""
FORBIDDEN_BODY = b"""<?xml version="1.0" encoding="UTF-8"?>
<Error><Code>AccessDenied</Code><Message>Unexpected credentials.</Message></Error>"""


class State:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = Lock()
        self.condition = Condition(self.lock)
        self.values = {
            "expired_token_puts": 0,
            "release_expired_token_puts": 0,
            "successful_puts": 0,
            "unexpected_auth_puts": 0,
            "stored_bytes": 0,
        }
        self.write()

    def update(self, key, delta=1):
        with self.condition:
            self.values[key] += delta
            self.write_locked()
            self.condition.notify_all()
            return self.values[key]

    def set(self, key, value):
        with self.condition:
            self.values[key] = value
            self.write_locked()
            self.condition.notify_all()

    def write(self):
        with self.condition:
            self.write_locked()

    def snapshot(self):
        with self.condition:
            return dict(self.values)

    def wait_until(self, predicate, timeout):
        deadline = time.monotonic() + timeout
        with self.condition:
            while not predicate(self.values):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.condition.wait(remaining)
            return True

    def release_expired_token_puts(self):
        with self.condition:
            self.values["release_expired_token_puts"] = self.values["expired_token_puts"]
            self.write_locked()
            self.condition.notify_all()
            return self.values["release_expired_token_puts"]

    def write_locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.values, sort_keys=True))


class ExpiredTokenS3Handler(BaseHTTPRequestHandler):
    server_version = "ExpiredTokenS3/1.0"

    def do_GET(self):
        if self.path == "/__health":
            self._send_text(HTTPStatus.OK, self.server.server_id.encode())
            return
        if self.path == "/__state":
            self._send_json(HTTPStatus.OK, self.server.state.snapshot())
            return
        if self.path == "/__wait_for_initial_put":
            observed = self.server.state.wait_until(
                lambda values: values["expired_token_puts"] > values["release_expired_token_puts"],
                self.server.wait_timeout,
            )
            status = HTTPStatus.OK if observed else HTTPStatus.GATEWAY_TIMEOUT
            self._send_json(status, {"initial_put_observed": 1 if observed else 0})
            return
        if self.path == "/__release_expired_token_put":
            released = self.server.state.release_expired_token_puts()
            self._send_json(HTTPStatus.OK, {"released_expired_token_puts": released})
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_HEAD(self):
        if self.path == "/__health":
            self._send_headers(HTTPStatus.OK, "text/plain", len(self.server.server_id))
            return
        if self.path == "/__state":
            body = json.dumps(self.server.state.snapshot(), sort_keys=True).encode()
            self._send_headers(HTTPStatus.OK, "application/json", len(body))
            return
        if self.path == "/__wait_for_initial_put":
            body = json.dumps({"initial_put_observed": 1}, sort_keys=True).encode()
            self._send_headers(HTTPStatus.OK, "application/json", len(body))
            return
        if self.path == "/__release_expired_token_put":
            body = json.dumps({"released_expired_token_puts": 1}, sort_keys=True).encode()
            self._send_headers(HTTPStatus.OK, "application/json", len(body))
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_PUT(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        key_id = self._credential_key_id()

        if key_id == INITIAL_KEY_ID:
            expired_count = self.server.state.update("expired_token_puts")
            self.server.state.wait_until(
                lambda values: values["release_expired_token_puts"] >= expired_count,
                self.server.wait_timeout,
            )
            self._send_xml(HTTPStatus.BAD_REQUEST, EXPIRED_TOKEN_BODY)
            return
        if key_id == REFRESHED_KEY_ID:
            self.server.state.update("successful_puts")
            self.server.state.set("stored_bytes", len(body))
            self.send_response(HTTPStatus.OK)
            self.send_header("ETag", '"expired-token-s3-etag"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        self.server.state.update("unexpected_auth_puts")
        self._send_xml(HTTPStatus.FORBIDDEN, FORBIDDEN_BODY)

    def log_message(self, fmt, *args):
        return

    def _credential_key_id(self):
        authorization = self.headers.get("Authorization", "")
        match = re.search(r"Credential=([^/\s,]+)", authorization)
        return match.group(1) if match else ""

    def _send_xml(self, status, body):
        self._send_headers(status, "application/xml", len(body))
        self.wfile.write(body)

    def _send_text(self, status, body):
        self._send_headers(status, "text/plain", len(body))
        self.wfile.write(body)

    def _send_json(self, status, values):
        body = json.dumps(values, sort_keys=True).encode()
        self._send_headers(status, "application/json", len(body))
        self.wfile.write(body)

    def _send_headers(self, status, content_type, content_length):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(content_length))
        self.end_headers()


class ExpiredTokenS3Server(ThreadingHTTPServer):
    def __init__(self, address, handler, state, server_id, wait_timeout):
        super().__init__(address, handler)
        self.state = state
        self.server_id = server_id
        self.wait_timeout = wait_timeout


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9002)
    parser.add_argument("--state-file", default="/tmp/duckdb-httpfs-expired-token-s3-state.json")
    parser.add_argument("--server-id", default="expired-token-s3")
    parser.add_argument("--wait-timeout", type=float, default=30.0)
    args = parser.parse_args()

    state = State(args.state_file)
    server = ExpiredTokenS3Server(
        (args.host, args.port), ExpiredTokenS3Handler, state, args.server_id, args.wait_timeout
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
