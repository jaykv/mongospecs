"""Run the storefront (or the raw baseline) as a real server process."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import subprocess
import sys
import typing as t
from dataclasses import dataclass

import httpx

APPS = {
    "storefront": "mongospecs_bench.storefront.app:create_app",
    "raw": "mongospecs_bench.storefront.raw_app:create_app",
}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class Server:
    base_url: str
    process: subprocess.Popen[bytes]
    env: dict[str, str]
    exit_code: int | None = None
    _log: str | None = None

    async def stop(self, *, grace_seconds: float = 20.0) -> int:
        """Ask the server to shut down gracefully (SIGTERM) and return its exit code."""
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            try:
                await asyncio.to_thread(self.process.wait, grace_seconds)
            except subprocess.TimeoutExpired:
                self.process.kill()
                await asyncio.to_thread(self.process.wait)
        self.exit_code = self.process.returncode
        return self.exit_code

    def stopped_gracefully(self) -> bool:
        """True when the process ended on our SIGTERM after running its shutdown handlers.

        uvicorn finishes in-flight requests, runs the ASGI lifespan shutdown and
        then re-raises the signal, so a clean stop ends with exit code 0 or -SIGTERM.
        """
        if self._log is None:
            self._log = self.output()
        return self.exit_code in (0, -signal.SIGTERM) and "shutdown complete" in self._log

    def output(self) -> str:
        """Everything the server printed. Only complete once the process has exited."""
        if self._log is not None:
            return self._log
        if self.process.stdout is None:
            return ""
        with contextlib.suppress(Exception):
            text = self.process.stdout.read().decode(errors="replace")
            if self.process.poll() is not None:
                self._log = text
            return text
        return ""


async def start_server(
    *,
    app: str = "storefront",
    database: str,
    mongo_uri: str,
    secret: str = "e2e-secret",
    plug: str = "asgi",
    backend: str = "msgspec",
    projector: bool = True,
    extra_env: t.Mapping[str, str] | None = None,
    startup_seconds: float = 30.0,
) -> Server:
    port = free_port()
    env = {
        **os.environ,
        "SHOP_MONGO_URI": mongo_uri,
        "SHOP_DB": database,
        "SHOP_SECRET": secret,
        "SHOP_PLUG": plug,
        "SHOP_BACKEND": backend,
        "SHOP_PROJECTOR": "1" if projector else "0",
        **(extra_env or {}),
    }
    process = subprocess.Popen(  # noqa: ASYNC220 - starting a server process, not doing I/O with it
        [
            sys.executable,
            "-m",
            "uvicorn",
            APPS[app],
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
            "--no-access-log",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    server = Server(f"http://127.0.0.1:{port}", process, env)
    deadline = asyncio.get_running_loop().time() + startup_seconds
    async with httpx.AsyncClient(base_url=server.base_url, timeout=2) as client:
        while True:
            if process.poll() is not None:
                raise RuntimeError(
                    f"server exited during startup with code {process.returncode}:\n{server.output()}"
                )
            try:
                if (await client.get("/healthz")).status_code == 200:
                    return server
            except httpx.HTTPError:
                pass
            if asyncio.get_running_loop().time() > deadline:
                await server.stop()
                raise RuntimeError(f"server did not become healthy in {startup_seconds}s:\n{server.output()}")
            await asyncio.sleep(0.1)


@contextlib.asynccontextmanager
async def running_server(**options: t.Any) -> t.AsyncIterator[Server]:
    server = await start_server(**options)
    try:
        yield server
    finally:
        await server.stop()
