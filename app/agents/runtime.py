"""
Shared managed agent runtime — subprocess lifecycle management.

Used by all agent providers (OpenCode, Kilocode, etc.) to manage
external server subprocesses with healthcheck, discovery, and graceful shutdown.
"""

import asyncio
import contextlib
import os
import time
from typing import Any

import httpx

from app.core.logging import get_logger

logger = get_logger(__name__)


class ManagedAgentRuntime:
    """
    Manages the lifecycle of an external agent subprocess (e.g., opencode serve, kilocode server).

    Handles:
    - Spawning the subprocess with proper env/args
    - Polling readiness via HTTP healthcheck
    - Graceful shutdown (SIGTERM → SIGKILL)
    - Diagnostics (PID, uptime, exit code, stdout tail)

    Usage:
        runtime = ManagedAgentRuntime(
            command="myagent",
            port=5000,
            base_url="http://127.0.0.1:5000",
            password="secret",
            server_password_env="MYAGENT_SERVER_PASSWORD",
        )
        started = await runtime.start()
        ...
        await runtime.stop()
    """

    def __init__(
        self,
        command: str,
        port: int,
        base_url: str,
        username: str = "agent",
        password: str | None = None,
        startup_timeout: int = 30,
        healthcheck_interval: int = 1,
        graceful_shutdown: int = 10,
        working_dir: str | None = None,
        extra_env: dict[str, str] | None = None,
        server_password_env: str = "AGENT_SERVER_PASSWORD",
    ):
        """
        Args:
            command: CLI command to run (e.g., "opencode", "kilocode")
            port: Server port
            base_url: Healthcheck base URL (e.g., "http://127.0.0.1:4096")
            username: HTTP Basic Auth username
            password: HTTP Basic Auth password
            startup_timeout: Max seconds to wait for healthcheck
            healthcheck_interval: Seconds between healthcheck polls
            graceful_shutdown: SIGTERM grace period before SIGKILL
            working_dir: Subprocess working directory
            extra_env: Additional environment variables
            server_password_env: Env var name for server password (e.g., "OPENCODE_SERVER_PASSWORD")
        """
        self.command = command
        self.port = port
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.startup_timeout = startup_timeout
        self.healthcheck_interval = healthcheck_interval
        self.graceful_shutdown = graceful_shutdown
        self.working_dir = working_dir
        self.extra_env = extra_env or {}
        self.server_password_env = server_password_env

        self._process: asyncio.subprocess.Process | None = None
        self._start_time: float | None = None
        self._stdout_buffer: list[str] = []
        self._stderr_buffer: list[str] = []
        self._stdout_tail_max = 20
        self._exit_code: int | None = None
        self._error: str | None = None
        self._io_tasks: list[asyncio.Task] = []

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    @property
    def uptime_seconds(self) -> float | None:
        if self._start_time is None:
            return None
        return time.monotonic() - self._start_time

    @property
    def is_running(self) -> bool:
        if self._process is None:
            return False
        return self._process.returncode is None

    async def is_healthy(self) -> bool:
        """Check if the subprocess is running AND responding to healthcheck."""
        if not self.is_running:
            return False
        try:
            auth = (self.username, self.password) if self.password else None
            async with httpx.AsyncClient(timeout=5, auth=auth) as client:
                response = await client.get(f"{self.base_url}/global/health")
                if response.status_code != 200:
                    return False
                data = response.json()
                return data.get("healthy", False)
        except Exception:
            return False

    async def start(self) -> bool:
        """Spawn the subprocess and wait until it's healthy.

        Returns True if the server became healthy, False otherwise.
        """
        if self.is_running:
            logger.warning(
                "ManagedAgentRuntime: process already running, skipping start",
                pid=self.pid,
            )
            return True

        cmd = [self.command, "serve", "--port", str(self.port)]
        env = os.environ.copy()
        env.update(self.extra_env)

        # Set server password env var if password is provided
        if self.password:
            env[self.server_password_env] = self.password

        cwd = self.working_dir or None

        logger.info(
            "ManagedAgentRuntime: starting subprocess",
            command=" ".join(cmd),
            port=self.port,
            working_dir=cwd,
        )

        try:
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=cwd,
            )

            # Start stdout/stderr readers
            self._io_tasks = [
                asyncio.create_task(self._read_stream(self._process.stdout, self._stdout_buffer)),
                asyncio.create_task(self._read_stream(self._process.stderr, self._stderr_buffer)),
            ]

            self._start_time = time.monotonic()
            self._exit_code = None
            self._error = None

        except FileNotFoundError as exc:
            self._error = f"Command not found: {self.command}"
            logger.error(
                "ManagedAgentRuntime: command not found",
                command=self.command,
                error=str(exc),
            )
            return False
        except Exception as exc:
            self._error = f"Failed to start subprocess: {exc}"
            logger.exception("ManagedAgentRuntime: failed to start subprocess")
            return False

        return await self.wait_until_healthy()

    async def wait_until_healthy(self) -> bool:
        """Poll healthcheck until healthy or timeout."""
        if not self.is_running:
            return False

        deadline = time.monotonic() + self.startup_timeout
        attempt = 0

        while time.monotonic() < deadline:
            attempt += 1

            if not self.is_running:
                self._error = f"Process exited prematurely with code {self._exit_code}"
                logger.error(
                    "ManagedAgentRuntime: process exited before becoming healthy",
                    exit_code=self._exit_code,
                )
                return False

            if await self.is_healthy():
                logger.info(
                    "ManagedAgentRuntime: server is healthy",
                    pid=self.pid,
                    attempts=attempt,
                    startup_seconds=time.monotonic() - self._start_time,
                )
                return True

            await asyncio.sleep(self.healthcheck_interval)

        self._error = f"Healthcheck timed out after {self.startup_timeout}s"
        logger.error(
            "ManagedAgentRuntime: healthcheck timed out",
            timeout_seconds=self.startup_timeout,
            pid=self.pid,
            attempts=attempt,
        )
        return False

    async def stop(self) -> None:
        """Gracefully stop the subprocess (SIGTERM → SIGKILL)."""
        if self._process is None:
            return

        if not self.is_running:
            await self._cleanup()
            return

        logger.info(
            "ManagedAgentRuntime: stopping subprocess",
            pid=self.pid,
            grace_period=self.graceful_shutdown,
        )

        try:
            self._process.terminate()

            try:
                await asyncio.wait_for(
                    self._process.wait(),
                    timeout=self.graceful_shutdown,
                )
            except TimeoutError:
                logger.warning(
                    "ManagedAgentRuntime: graceful shutdown timed out, sending SIGKILL",
                    pid=self.pid,
                )
                self._process.kill()
                await self._process.wait()

        except ProcessLookupError:
            pass
        except Exception as exc:
            logger.warning(
                "ManagedAgentRuntime: error during shutdown",
                pid=self.pid,
                error=str(exc),
            )

        await self._cleanup()

    async def _read_stream(self, stream: asyncio.StreamReader, buffer: list[str]) -> None:
        """Read lines from a stream and keep the tail."""
        try:
            async for line_bytes in stream:
                line = line_bytes.decode("utf-8", errors="replace").rstrip()
                if line:
                    buffer.append(line)
                    if len(buffer) > self._stdout_tail_max:
                        buffer.pop(0)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug("ManagedAgentRuntime: stream reader error", error=str(exc))

    async def _cleanup(self) -> None:
        """Clean up resources."""
        for task in self._io_tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._io_tasks.clear()

        if self._process:
            self._exit_code = self._process.returncode
            self._process = None

    def diagnostics(self) -> dict[str, Any]:
        """Return diagnostic information about the managed process."""
        return {
            "managed": True,
            "process": {
                "status": "running" if self.is_running else "stopped",
                "pid": self.pid,
                "uptime_seconds": round(self.uptime_seconds, 1) if self.uptime_seconds else None,
                "exit_code": self._exit_code,
                "error": self._error,
                "stdout_tail": self._stdout_buffer[-5:],
            },
        }
