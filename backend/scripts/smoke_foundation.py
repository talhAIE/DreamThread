"""Smoke-test a real local Uvicorn process without a database or credentials."""

import json
import os
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


def main():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = {
        **os.environ,
        "APP_ENV": "test",
        "DATABASE_URL": "postgresql+psycopg://test:local-only@127.0.0.1:1/test",
        "DB_TIMEOUT_SECONDS": "0.2",
        "EXPECTED_SCHEMA_REVISION": "unversioned",
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:create_app", "--factory",
         "--host", "127.0.0.1", "--port", str(port), "--no-access-log"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                with urlopen(base + "/health/live", timeout=1) as response:
                    assert json.load(response) == {"status": "alive"}
                    assert response.headers["X-Request-ID"]
                break
            except (URLError, TimeoutError):
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("Local server did not start") from None
                time.sleep(0.1)
        with urlopen(base + "/openapi.json", timeout=2) as response:
            assert "/health/ready" in json.load(response)["paths"]
        try:
            urlopen(base + "/health/ready", timeout=3)
        except HTTPError as error:
            assert error.code == 503
            body = json.load(error)
            assert body["error"]["details"]["database"] == "unavailable"
            assert body["error"]["request_id"]
        else:
            raise AssertionError("Readiness must fail without a database")
        print("PASS: live Uvicorn HTTP, OpenAPI, request IDs, safe database-unavailable 503")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


if __name__ == "__main__":
    main()
