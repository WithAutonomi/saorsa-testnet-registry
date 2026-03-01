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


def _get_handler(state, handler_type):
    """Find the registered handler instance of the given type."""
    for handler in state.callback_handlers:
        if isinstance(handler, handler_type):
            return handler
    return None


def _format_elapsed(seconds):
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def create_progress_handler(console):
    """Factory that creates the appropriate progress handler based on terminal detection."""
    if console.is_terminal:
        live = Live(console=console, refresh_per_second=4)
        return RichLiveProgressHandler(console, live)
    return LogProgressHandler(console)
