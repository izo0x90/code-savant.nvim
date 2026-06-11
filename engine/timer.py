import asyncio


class DeadlineTimer:
    """
    Asynchronous pausable DeadlineTimer (chunk-DN4XSYRG.js Lines 312359-312365).
    Tracks the execution budget, pausing automatically during manual interactive stages
    such as tool confirmation approvals.
    """

    def __init__(self, limit_seconds: float):
        self.limit_seconds = limit_seconds
        self.elapsed_seconds = 0.0
        self.paused = False
        self.aborted = False
        self._task = None

    def start(self) -> None:
        """Starts the background countdown task if it is not already running."""
        if self._task is None and not self.aborted:
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        tick = 0.05
        try:
            while not self.aborted:
                await asyncio.sleep(tick)
                if not self.paused:
                    self.elapsed_seconds += tick
                    if self.elapsed_seconds >= self.limit_seconds:
                        self.aborted = True
                        break
        except asyncio.CancelledError:
            pass

    def pause(self) -> None:
        """Pauses the countdown (typically during tool authorization/confirmation pauses)."""
        self.paused = True

    def resume(self) -> None:
        """Resumes the countdown."""
        self.paused = False

    def stop(self) -> None:
        """Stops the countdown and cancels the background task."""
        self.aborted = True
        if self._task is not None:
            self._task.cancel()

    @property
    def is_triggered(self) -> bool:
        """Returns True if the time limit has expired."""
        return self.elapsed_seconds >= self.limit_seconds or self.aborted
