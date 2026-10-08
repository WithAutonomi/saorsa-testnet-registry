import time

from pyinfra.api.state import BaseStateCallback
from rich.live import Live
from rich.table import Table

SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


class RichLiveProgressHandler(BaseStateCallback):
    """Rich Live table progress display for interactive terminals."""

    def __init__(self, console, live):
        self._console = console
        self._live = live
        self._host_status = {}
        self._host_op = {}
        self._start_times = {}
        self._spinner_tick = 0

    def _build_table(self):
        table = Table(show_header=True, header_style="bold")
        table.add_column("Host")
        table.add_column("Status")
        table.add_column("Elapsed")
        now = time.monotonic()
        self._spinner_tick += 1
        for host_name in sorted(self._host_status.keys()):
            status = self._host_status[host_name]
            start = self._start_times.get(host_name, now)
            elapsed = _format_elapsed(now - start)
            if status == "connecting":
                frame = SPINNER_FRAMES[self._spinner_tick % len(SPINNER_FRAMES)]
                symbol = f"[yellow]{frame} connecting...[/yellow]"
            elif status == "running":
                frame = SPINNER_FRAMES[self._spinner_tick % len(SPINNER_FRAMES)]
                op_name = self._host_op.get(host_name, "")
                symbol = f"[yellow]{frame} {op_name}[/yellow]"
            elif status == "done":
                symbol = "[green]✓ done[/green]"
            elif status == "failed":
                symbol = "[red]✗ failed[/red]"
            elif status == "connect_error":
                symbol = "[red]✗ connection failed[/red]"
            else:
                symbol = f"[dim]{status}[/dim]"
            table.add_row(host_name, symbol, elapsed)
        return table

    def _update(self):
        self._live.update(self._build_table())

    @staticmethod
    def host_before_connect(state, host):
        self = _get_handler(state, RichLiveProgressHandler)
        if self is None:
            return
        self._host_status[host.name] = "connecting"
        self._start_times[host.name] = time.monotonic()
        self._update()

    @staticmethod
    def host_connect(state, host):
        self = _get_handler(state, RichLiveProgressHandler)
        if self is None:
            return
        self._host_status[host.name] = "connected"
        self._update()

    @staticmethod
    def host_connect_error(state, host, error):
        self = _get_handler(state, RichLiveProgressHandler)
        if self is None:
            return
        self._host_status[host.name] = "connect_error"
        self._update()

    @staticmethod
    def operation_host_start(state, host, op_hash):
        self = _get_handler(state, RichLiveProgressHandler)
        if self is None:
            return
        op_meta = state.op_meta.get(op_hash)
        op_name = next(iter(op_meta.names)) if op_meta and op_meta.names else "running..."
        self._host_status[host.name] = "running"
        self._host_op[host.name] = op_name
        self._update()

    @staticmethod
    def operation_host_success(state, host, op_hash, retry_count=0):
        self = _get_handler(state, RichLiveProgressHandler)
        if self is None:
            return
        self._update()

    @staticmethod
    def operation_host_error(state, host, op_hash, retry_count=0, max_retries=0):
        self = _get_handler(state, RichLiveProgressHandler)
        if self is None:
            return
        self._host_status[host.name] = "failed"
        self._update()

    @staticmethod
    def operation_end(state, op_hash):
        self = _get_handler(state, RichLiveProgressHandler)
        if self is None:
            return
        for host_name, status in self._host_status.items():
            if status == "running":
                self._host_status[host_name] = "connected"
        self._update()

    def mark_all_done(self):
        for host_name, status in self._host_status.items():
            if status not in ("failed", "connect_error"):
                self._host_status[host_name] = "done"
        self._update()


class LogProgressHandler(BaseStateCallback):
    """Simple line-by-line progress output for CI environments."""

    def __init__(self, console):
        self._console = console

    @staticmethod
    def host_connect(state, host):
        self = _get_handler(state, LogProgressHandler)
        if self is None:
            return
        self._console.print(f"[{host.name}] Connected")

    @staticmethod
    def host_connect_error(state, host, error):
        self = _get_handler(state, LogProgressHandler)
        if self is None:
            return
        self._console.print(f"[{host.name}] [red]Connection failed: {error}[/red]")

    @staticmethod
    def operation_start(state, op_hash):
        self = _get_handler(state, LogProgressHandler)
        if self is None:
            return
        op_meta = state.op_meta.get(op_hash)
        op_name = next(iter(op_meta.names)) if op_meta and op_meta.names else "unknown"
        self._console.print(f"Starting: {op_name}")

    @staticmethod
    def operation_host_success(state, host, op_hash, retry_count=0):
        self = _get_handler(state, LogProgressHandler)
        if self is None:
            return
        op_meta = state.op_meta.get(op_hash)
        op_name = next(iter(op_meta.names)) if op_meta and op_meta.names else "unknown"
        self._console.print(f"[{host.name}] {op_name}... [green]success[/green]")

    @staticmethod
    def operation_host_error(state, host, op_hash, retry_count=0, max_retries=0):
        self = _get_handler(state, LogProgressHandler)
        if self is None:
            return
        op_meta = state.op_meta.get(op_hash)
        op_name = next(iter(op_meta.names)) if op_meta and op_meta.names else "unknown"
        self._console.print(f"[{host.name}] {op_name}... [red]failed[/red]")


class OpTimingHandler(BaseStateCallback):
    """Records per-operation wall-clock durations for a pyinfra run.

    pyinfra runs operations one at a time across the whole fleet, firing
    ``operation_start`` before an op and ``operation_end`` once every host has
    finished it. The gap between the two is the op's wall-clock (dominated by the
    slowest host), which is exactly what's needed to see which provisioning item
    a slow run is spending its time in. Attach alongside a progress handler and
    call :func:`print_op_timing_summary` after ``run_ops``.
    """

    def __init__(self):
        self._starts = {}
        # (op_name, duration_seconds), in completion order.
        self.timings: list[tuple[str, float]] = []

    @staticmethod
    def operation_start(state, op_hash):
        self = _get_handler(state, OpTimingHandler)
        if self is None:
            return
        self._starts[op_hash] = time.monotonic()

    @staticmethod
    def operation_end(state, op_hash):
        self = _get_handler(state, OpTimingHandler)
        if self is None:
            return
        start = self._starts.pop(op_hash, None)
        if start is None:
            return
        op_meta = state.op_meta.get(op_hash)
        name = next(iter(op_meta.names)) if op_meta and op_meta.names else str(op_hash)
        self.timings.append((name, time.monotonic() - start))


def print_op_timing_summary(console, handler, *, title, connect_seconds=None):
    """Print a slowest-first per-operation timing table for a provisioning pass."""
    if handler is None or not handler.timings:
        return
    total = sum(d for _, d in handler.timings)
    console.print()
    console.rule(f"[bold]Operation timings — {title}[/bold]")
    if connect_seconds is not None:
        console.print(
            f"  [cyan]{_format_op_duration(connect_seconds):>9}[/cyan]  [dim](SSH connect)[/dim]"
        )
    for name, dur in sorted(handler.timings, key=lambda t: t[1], reverse=True):
        console.print(f"  [cyan]{_format_op_duration(dur):>9}[/cyan]  {name}")
    console.print(f"  [bold]{_format_op_duration(total):>9}  ops total[/bold]")


def _get_handler(state, handler_type):
    """Find the registered handler instance of the given type."""
    for handler in state.callback_handlers:
        if isinstance(handler, handler_type):
            return handler
    return None


def _format_elapsed(seconds):
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def _format_op_duration(seconds):
    """Human-friendly op duration: ``8.3s`` / ``2m 14s`` / ``1h 03m 09s``."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(round(seconds)), 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m {secs:02d}s"


def create_progress_handler(console):
    """Factory that creates the appropriate progress handler based on terminal detection."""
    if console.is_terminal:
        live = Live(console=console, refresh_per_second=4)
        return RichLiveProgressHandler(console, live)
    return LogProgressHandler(console)
