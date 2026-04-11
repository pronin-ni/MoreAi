import asyncio
import time

from app.browser.auth import auth_bootstrapper
from app.browser.execution.errors import ExecutionTimeoutError, RetryableBrowserTaskError
from app.browser.execution.models import BrowserJob, BrowserJobResult
from app.browser.execution.runtime import WorkerBrowserRuntime
from app.browser.registry import registry as browser_registry
from app.browser.recon import FailureCategory, classify_failure
from app.browser.recon.manager import attempt_recon_recovery
from app.browser.telemetry import browser_telemetry
from app.core.config import settings
from app.core.errors import BrowserError, InternalError
from app.core.logging import get_logger

logger = get_logger(__name__)


class BrowserProviderExecutor:
    def __init__(self, runtime: WorkerBrowserRuntime, on_runtime_restart=None):
        self.runtime = runtime
        self.on_runtime_restart = on_runtime_restart

    async def _restart_runtime(self) -> None:
        await self.runtime.restart()
        if self.on_runtime_restart is not None:
            self.on_runtime_restart()

    def _classify_browser_error(self, exc: BrowserError) -> tuple[bool, str]:
        message = exc.message.lower()
        transient_markers = [
            "timeout",
            "target page",
            "has been closed",
            "browser has been closed",
            "context closed",
            "network",
            "navigation",
            "disconnected",
            "crash",
            "websocket",
        ]
        if any(marker in message for marker in transient_markers):
            return True, "transient_browser_failure"
        return False, "provider_browser_failure"

    async def _replay_provider_actions(
        self,
        provider,
        message: str,
        timeout: int,
    ) -> str:
        """Replay the core provider actions after recon recovery.

        This re-runs send_message + wait_for_response on the same page.
        """
        await provider.send_message(message)
        return await provider.wait_for_response(timeout=timeout)

    async def execute(self, job: BrowserJob) -> BrowserJobResult:
        request = job.request
        provider_class = browser_registry.get_provider_class(request.canonical_model_id)
        provider_config = browser_registry.get_provider_config(request.canonical_model_id)
        last_error: BrowserError | None = None
        auth_state_invalidated = False
        started_at = time.monotonic()
        request_start = browser_telemetry.start_request(provider_class.provider_id)

        for attempt in range(request.max_retries + 1):
            if job.is_cancelled():
                raise asyncio.CancelledError()

            try:
                storage_state_path = await auth_bootstrapper.ensure_model_authenticated(
                    request.canonical_model_id,
                    runtime=self.runtime,
                )
                async with asyncio.timeout(request.execution_timeout_seconds):
                    async with self.runtime.open_session(
                        storage_state_path=storage_state_path
                    ) as session:
                        provider = provider_class(
                            session.page,
                            request_id=request.request_id,
                            provider_config=provider_config,
                        )
                        provider.set_request_id(request.request_id)
                        await provider.navigate_to_chat()
                        await provider.start_new_chat()
                        await provider.send_message(request.message)
                        content = await provider.wait_for_response(
                            timeout=request.execution_timeout_seconds
                        )
                        finished_at = time.monotonic()
                        browser_telemetry.end_request(provider_class.provider_id, request_start)
                        return BrowserJobResult(
                            content=content,
                            started_at=started_at,
                            finished_at=finished_at,
                            queue_wait_seconds=started_at - job.enqueued_at,
                            execution_seconds=finished_at - started_at,
                            retry_count=attempt,
                        )
            except TimeoutError as exc:
                logger.warning(
                    "Browser task execution timed out",
                    task_id=request.task_id,
                    provider_id=request.provider_id,
                    attempt=attempt + 1,
                )
                last_error = ExecutionTimeoutError(
                    f"Browser task timed out after {request.execution_timeout_seconds} seconds",
                    details={
                        "task_id": request.task_id,
                        "provider_id": request.provider_id,
                        "phase": "execution",
                    },
                )
                # Timeout is classified as retryable — retry within limits
                retryable = True
                failure_kind = "transient_browser_failure"
                browser_telemetry.end_request(
                    provider_class.provider_id, request_start, timed_out=True,
                )

                if attempt < request.max_retries:
                    job.retry_count += 1
                    await self._restart_runtime()
                    await asyncio.sleep(
                        min(
                            settings.browser_retry_backoff_seconds * (2**attempt),
                            max(settings.browser_retry_backoff_seconds, 2.0),
                        )
                    )
                    continue

                raise ExecutionTimeoutError(
                    f"Browser task timed out after {request.execution_timeout_seconds} seconds",
                    details={
                        "task_id": request.task_id,
                        "provider_id": request.provider_id,
                        "phase": "execution",
                        "attempts": attempt + 1,
                    },
                ) from exc
            except BrowserError as exc:
                last_error = exc
                retryable, failure_kind = self._classify_browser_error(exc)
                auth_wall_detected = False

                if "provider" in locals():
                    try:
                        await provider.save_debug_artifacts(str(exc))
                    except Exception:
                        logger.debug(
                            "Failed to save provider artifacts",
                            request_id=request.request_id,
                            model=request.canonical_model_id,
                        )
                    try:
                        auth_wall_detected = await provider.detect_login_required()
                    except Exception:
                        logger.debug(
                            "Failed to re-check provider login state",
                            request_id=request.request_id,
                            model=request.canonical_model_id,
                        )

                if (
                    provider_class.requires_auth
                    and auth_wall_detected
                    and not auth_state_invalidated
                ):
                    auth_bootstrapper.invalidate_model_storage_state(request.canonical_model_id)
                    auth_state_invalidated = True
                    browser_telemetry.record_session_invalidation(provider_class.provider_id)
                    browser_telemetry.record_login_wall(provider_class.provider_id)
                    logger.info(
                        "Invalidated provider auth state after login wall",
                        request_id=request.request_id,
                        model=request.canonical_model_id,
                    )
                    continue

                # ── Auto-recon recovery for recon-eligible failures ──
                if "provider" in locals() and not retryable:
                    recon_category, recon_reason = classify_failure(
                        error_type=type(exc).__name__,
                        error_message=exc.message,
                        details=exc.details,
                    )

                    if recon_category == FailureCategory.RECON_ELIGIBLE:
                        logger.info(
                            "Triggering auto-recon recovery",
                            provider_id=provider_class.provider_id,
                            error_type=type(exc).__name__,
                            reason=recon_reason,
                        )
                        recon_result = await attempt_recon_recovery(
                            provider=provider,
                            page=provider.page,
                            request_id=request.request_id,
                            failed_action="browser_execution",
                            failed_error_type=type(exc).__name__,
                            failed_error_message=exc.message,
                            replay_fn=self._replay_provider_actions,
                            replay_args=(provider, request.message, request.execution_timeout_seconds),
                        )

                        if recon_result.recovered and recon_result.replay_succeeded:
                            logger.info(
                                "Auto-recon recovery succeeded",
                                provider_id=provider_class.provider_id,
                                actions=recon_result.actions_performed,
                                duration_ms=recon_result.duration_ms,
                            )
                            # Re-run the full flow after successful recon
                            try:
                                content = await provider.wait_for_response(
                                    timeout=request.execution_timeout_seconds
                                )
                                finished_at = time.monotonic()
                                browser_telemetry.end_request(provider_class.provider_id, request_start)
                                return BrowserJobResult(
                                    content=content,
                                    started_at=started_at,
                                    finished_at=finished_at,
                                    queue_wait_seconds=started_at - job.enqueued_at,
                                    execution_seconds=finished_at - started_at,
                                    retry_count=attempt,
                                )
                            except Exception:
                                pass  # Recon helped but response still failed — fall through

                        logger.warning(
                            "Auto-recon recovery did not fully succeed",
                            provider_id=provider_class.provider_id,
                            recovered=recon_result.recovered,
                            replay_succeeded=recon_result.replay_succeeded,
                            reason=recon_result.reason,
                            duration_ms=recon_result.duration_ms,
                        )
                    else:
                        logger.debug(
                            "Skipping recon: not recon-eligible",
                            provider_id=provider_class.provider_id,
                            category=recon_category.value,
                            reason=recon_reason,
                        )

                logger.warning(
                    "Browser task failed",
                    task_id=request.task_id,
                    provider_id=request.provider_id,
                    attempt=attempt + 1,
                    error=str(exc),
                    retryable=retryable,
                    failure_kind=failure_kind,
                )

                if retryable and attempt < request.max_retries:
                    job.retry_count += 1
                    await self._restart_runtime()
                    await asyncio.sleep(
                        min(
                            settings.browser_retry_backoff_seconds * (2**attempt),
                            max(settings.browser_retry_backoff_seconds, 2.0),
                        )
                    )
                    continue
                if retryable:
                    raise RetryableBrowserTaskError(
                        f"Failed to process browser task after {attempt + 1} attempts: {exc.message}",
                        details={
                            **exc.details,
                            "task_id": request.task_id,
                            "provider_id": request.provider_id,
                        },
                        failure_kind=failure_kind,
                    ) from exc
            except asyncio.CancelledError:
                logger.info(
                    "Browser task cancelled during execution",
                    task_id=request.task_id,
                    provider_id=request.provider_id,
                )
                raise
            except Exception as exc:
                logger.exception(
                    "Unexpected browser execution error",
                    task_id=request.task_id,
                    provider_id=request.provider_id,
                    error=str(exc),
                )
                await self._restart_runtime()
                raise InternalError(
                    f"Unexpected browser execution error: {str(exc)}",
                    details={
                        "task_id": request.task_id,
                        "provider_id": request.provider_id,
                    },
                ) from exc

        if last_error is not None:
            retryable, failure_kind = self._classify_browser_error(last_error)
            raise InternalError(
                (
                    f"Failed to process browser task after {request.max_retries + 1} attempts: "
                    f"{last_error.message}"
                ),
                details={
                    **last_error.details,
                    "task_id": request.task_id,
                    "provider_id": request.provider_id,
                    "retryable": retryable,
                    "failure_kind": failure_kind,
                },
            )

        raise InternalError("Unknown browser execution error")
