import asyncio
import time


class DeadlineTimer:
    """
    Asynchronous pausable DeadlineTimer.
    Tracks the execution budget, pausing automatically during manual interactive stages
    such as tool confirmation approvals.
    Uses high-precision monotonic clock and event-driven sleeping, completely avoiding tight polling loops.
    """

    def __init__(self, limit_seconds: float):
        self.limit_seconds = limit_seconds
        self.elapsed_seconds = 0.0
        self.paused = False
        self.aborted = False
        self._task = None
        self._start_time = None

    def start(self) -> None:
        """Starts the background timer countdown if it is not already running or paused."""
        if self._task is None and not self.aborted and not self.paused:
            self._start_time = time.perf_counter()
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            remaining = self.limit_seconds - self.elapsed_seconds
            if remaining > 0:
                await asyncio.sleep(remaining)
                self.elapsed_seconds = self.limit_seconds
                self.aborted = True
        except asyncio.CancelledError:
            pass

    def pause(self) -> None:
        """Pauses the countdown (typically during tool authorization/confirmation pauses)."""
        if not self.paused and not self.aborted:
            self.paused = True
            if self._task is not None:
                self._task.cancel()
                self._task = None
                if self._start_time is not None:
                    self.elapsed_seconds += (time.perf_counter() - self._start_time)
                    self._start_time = None

    def resume(self) -> None:
        """Resumes the countdown."""
        if self.paused and not self.aborted:
            self.paused = False
            self.start()

    def stop(self) -> None:
        """Stops the countdown and cancels the background task."""
        self.aborted = True
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self._start_time is not None:
            self.elapsed_seconds += (time.perf_counter() - self._start_time)
            self._start_time = None

    @property
    def is_triggered(self) -> bool:
        """Returns True if the time limit has expired, dynamically checking the monotonic clock."""
        current_elapsed = self.elapsed_seconds
        if not self.paused and self._start_time is not None:
            current_elapsed += (time.perf_counter() - self._start_time)
        return current_elapsed >= self.limit_seconds or self.aborted
