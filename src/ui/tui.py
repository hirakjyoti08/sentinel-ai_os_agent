import re
import json
import asyncio
from datetime import datetime
from typing import Optional

from rich.table import Table
from rich import box


def format_chat_markup(content: str) -> str:
    """Format markdown text into clean Rich markup."""
    # Convert markdown bold **text** to [bold]text[/bold]
    text = re.sub(r'\*\*(.+?)\*\*', r'[bold]\1[/bold]', content)
    # Remove any stray leftover ** asterisks
    text = text.replace('**', '')
    return text


def parse_markdown_table(lines: list[str]) -> Table | None:
    """Parse markdown pipe table lines into a styled Rich Table."""
    if len(lines) < 2:
        return None
    header_line = lines[0].strip()
    sep_line = lines[1].strip()
    if not (header_line.startswith('|') and header_line.endswith('|')):
        return None
    if not (sep_line.startswith('|') and sep_line.endswith('|')):
        return None
    
    # Check that separator contains only -, :, |, and whitespace
    sep_clean = sep_line.replace('|', '').replace('-', '').replace(':', '').replace(' ', '')
    if sep_clean:
        return None
        
    headers = [c.strip() for c in header_line.strip('|').split('|')]
    table = Table(
        box=box.ROUNDED,
        show_header=True,
        header_style="bold #58a6ff",
        border_style="#30363d",
        pad_edge=False,
        collapse_padding=True,
    )
    
    for h in headers:
        hl = h.lower()
        if "rank" in hl or "#" in hl:
            table.add_column(h, justify="center", style="#8b949e")
        elif "pid" in hl:
            table.add_column(h, style="bold cyan")
        elif "port" in hl:
            table.add_column(h, style="bold #39c5cf")
        elif "resource" in hl or "component" in hl or "metric" in hl:
            table.add_column(h, style="bold #58a6ff")
        elif "graph" in hl or "bar" in hl or "meter" in hl:
            table.add_column(h, style="bold #3fb950")
        elif "status" in hl or "state" in hl:
            table.add_column(h, style="bold #3fb950")
        elif "cpu" in hl:
            table.add_column(h, style="bold #d29922")
        elif "mem" in hl or "ram" in hl:
            table.add_column(h, style="bold #3fb950")
        elif "disk" in hl or "storage" in hl:
            table.add_column(h, style="bold #f0883e")
        elif "gpu" in hl:
            table.add_column(h, style="bold #bc8cff")
        elif "usage" in hl or "util" in hl or "%" in hl:
            table.add_column(h, style="bold #d29922")
        elif "uptime" in hl or "time" in hl:
            table.add_column(h, style="dim #8b949e")
        else:
            table.add_column(h, style="#c9d1d9")
        
    for row_line in lines[2:]:
        row_line = row_line.strip()
        if not (row_line.startswith('|') and row_line.endswith('|')):
            continue
        cols = [c.strip().replace('`', '') for c in row_line.strip('|').split('|')]
        clean_cols = []
        for c in cols:
            c = format_chat_markup(c)
            clean_cols.append(c)
        if len(clean_cols) < len(headers):
            clean_cols.extend([''] * (len(headers) - len(clean_cols)))
        table.add_row(*clean_cols[:len(headers)])
        
    return table


def render_message_to_log(write_fn, content: str, indent: str = "  "):
    """Write message lines to log, transforming any markdown tables into Rich Table objects."""
    lines = content.split("\n")
    table_buffer: list[str] = []
    in_table = False
    
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            table_buffer.append(stripped)
            in_table = True
        else:
            if in_table and table_buffer:
                tbl = parse_markdown_table(table_buffer)
                if tbl is not None:
                    write_fn(tbl)
                else:
                    for t_line in table_buffer:
                        write_fn(f"{indent}{format_chat_markup(t_line)}")
                table_buffer = []
                in_table = False
            write_fn(f"{indent}{format_chat_markup(line)}")
            
    if in_table and table_buffer:
        tbl = parse_markdown_table(table_buffer)
        if tbl is not None:
            write_fn(tbl)
        else:
            for t_line in table_buffer:
                write_fn(f"{indent}{format_chat_markup(t_line)}")

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, Container
from textual.widgets import Header, Footer, Static, Input, RichLog, Label, Button
from textual.reactive import reactive

from src.monitor.watcher import SystemWatcher, SystemMetrics
from src.storage.audit_log import get_recent_actions, init_db
from src.agent.core import Agent
from src.tools.registry import get_tool_tiers


def render_gauge_bar(percent: float, width: int = 14) -> str:
    """Render a clean colored gauge bar with status dot."""
    filled = max(0, min(width, int(round((percent / 100.0) * width))))
    bar = "█" * filled + "░" * (width - filled)
    if percent >= 85:
        color = "bright_red"
        dot = "[bright_red]●[/bright_red]"
    elif percent >= 65:
        color = "bright_yellow"
        dot = "[bright_yellow]●[/bright_yellow]"
    else:
        color = "bright_green"
        dot = "[bright_green]●[/bright_green]"
    return f"{dot} [{color}]{bar}[/{color}] [bold {color}]{percent:>5.1f}%[/bold {color}]"


SPARKLINE_BARS = [" ", "▂", "▃", "▄", "▅", "▆", "▇", "█"]


def render_sparkline(values: list[float], min_val: float = 0.0, max_val: Optional[float] = 100.0, color: str = "#58a6ff") -> str:
    """Render a clean unicode sparkline from history values."""
    if not values:
        return "[dim]──────────────[/dim]"
    
    if max_val is None or max_val <= min_val:
        real_min = min(values)
        real_max = max(values)
        if real_max <= real_min:
            real_max = real_min + 1.0
        min_val, max_val = real_min, real_max

    val_range = max_val - min_val
    if val_range <= 0:
        val_range = 1.0

    bars = []
    for v in values:
        norm = max(0.0, min(1.0, (v - min_val) / val_range))
        idx = int(norm * (len(SPARKLINE_BARS) - 1))
        bars.append(SPARKLINE_BARS[idx])
    return f"[{color}]{''.join(bars)}[/{color}]"


class ProcRow(Horizontal):
    """Interactive process row with process details and direct kill action button."""
    DEFAULT_CSS = """
    ProcRow {
        height: 1;
        layout: horizontal;
        background: transparent;
        padding: 0;
        margin: 0;
    }
    ProcRow:hover {
        background: #1f242c;
    }
    ProcRow .proc-info {
        width: 49;
        height: 1;
    }
    ProcRow .proc-kill-btn {
        width: 8;
        min-width: 8;
        height: 1;
        border: none;
        background: #2b1d1f;
        color: #ff7b72;
        padding: 0;
        margin: 0;
    }
    ProcRow .proc-kill-btn:hover {
        background: #da3633;
        color: #ffffff;
    }
    """
    def __init__(self, pid: int, name: str, cpu: float, mem: float):
        super().__init__(id=f"proc_row_{pid}", classes="proc-row")
        self.pid = pid
        self.proc_name = name
        self.cpu = cpu
        self.mem = mem

    def compose(self) -> ComposeResult:
        short_name = (self.proc_name[:18] + "..") if len(self.proc_name) > 20 else self.proc_name
        info_text = f"  [dim #58a6ff]{self.pid:<7}[/dim #58a6ff] [white]{short_name:<20}[/white] [#f0883e]{self.cpu:>7.1f}%[/#f0883e] [#bc8cff]{self.mem:>6.1f}%[/#bc8cff]  "
        yield Static(info_text, classes="proc-info", id=f"inspect_proc_{self.pid}")
        yield Button("✕ Kill", classes="proc-kill-btn", id=f"kill_pid_{self.pid}")

    def update_data(self, name: str, cpu: float, mem: float):
        self.proc_name = name
        self.cpu = cpu
        self.mem = mem
        short_name = (self.proc_name[:18] + "..") if len(self.proc_name) > 20 else self.proc_name
        info_text = f"  [dim #58a6ff]{self.pid:<7}[/dim #58a6ff] [white]{short_name:<20}[/white] [#f0883e]{self.cpu:>7.1f}%[/#f0883e] [#bc8cff]{self.mem:>6.1f}%[/#bc8cff]  "
        try:
            self.query_one(f"#inspect_proc_{self.pid}", Static).update(info_text)
        except Exception:
            pass


class PortRow(Horizontal):
    """Interactive port row with port details and direct close action button."""
    DEFAULT_CSS = """
    PortRow {
        height: 1;
        layout: horizontal;
        background: transparent;
        padding: 0;
        margin: 0;
    }
    PortRow:hover {
        background: #1f242c;
    }
    PortRow .port-info {
        width: 44;
        height: 1;
    }
    PortRow .port-close-btn {
        width: 9;
        min-width: 9;
        height: 1;
        border: none;
        background: #2b2518;
        color: #e3b341;
        padding: 0;
        margin: 0;
    }
    PortRow .port-close-btn:hover {
        background: #bb8009;
        color: #0d1117;
    }
    """
    def __init__(self, port: int, proto: str, pid: int, process: str):
        super().__init__(id=f"port_row_{port}_{pid}", classes="port-row")
        self.port = port
        self.proto = proto
        self.pid = pid
        self.process = process

    def compose(self) -> ComposeResult:
        short_name = (self.process[:16] + "..") if len(self.process) > 18 else self.process
        info_text = f"  [bold #3fb950]{self.port:<7}[/bold #3fb950] [dim]{self.proto:<6}[/dim] [cyan]{self.pid:<7}[/cyan] [white]{short_name:<18}[/white] "
        yield Static(info_text, classes="port-info", id=f"inspect_port_{self.port}")
        yield Button("✕ Close", classes="port-close-btn", id=f"close_port_{self.port}")

    def update_data(self, proto: str, pid: int, process: str):
        self.proto = proto
        self.pid = pid
        self.process = process
        short_name = (self.process[:16] + "..") if len(self.process) > 18 else self.process
        info_text = f"  [bold #3fb950]{self.port:<7}[/bold #3fb950] [dim]{self.proto:<6}[/dim] [cyan]{self.pid:<7}[/cyan] [white]{short_name:<18}[/white] "
        try:
            self.query_one(f"#inspect_port_{self.port}", Static).update(info_text)
        except Exception:
            pass


class SystemStatsPanel(Container):
    """Right-top panel: Rich live system telemetry with sparklines and clickable process/port actions."""
    
    metrics: reactive[SystemMetrics | None] = reactive(None)
    pulse_frame: reactive[int] = reactive(0)
    
    SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    HEARTBEAT_FRAMES = ["●", "◉", "○", "◌", "○", "◉"]

    def compose(self) -> ComposeResult:
        yield Static(id="stats_telemetry")
        yield Static("[dim #21262d]────────────────────────────────────────────────[/dim #21262d]", classes="panel-divider")
        yield Static("[bold #58a6ff]⚡ ACTIVE PROCESSES[/bold #58a6ff] [dim #8b949e]• Click to Inspect / Kill[/dim #8b949e]", id="procs_header")
        yield Static("[dim #8b949e]  PID     NAME                 CPU       MEM     ACTION[/dim #8b949e]", id="procs_sub_header")
        yield Vertical(id="procs_list")
        yield Static("[dim #21262d]────────────────────────────────────────────────[/dim #21262d]", classes="panel-divider")
        yield Static("[bold #58a6ff]🌐 OPEN PORTS[/bold #58a6ff] [dim #8b949e]• Click to Close[/dim #8b949e]", id="ports_header")
        yield Static("[dim #8b949e]  PORT    PROTO  PID     PROCESS            ACTION[/dim #8b949e]", id="ports_sub_header")
        yield Vertical(id="ports_list")
    
    async def watch_metrics(self, metrics: SystemMetrics | None):
        if metrics:
            self._render_telemetry_widget(metrics)
            await self._update_process_rows(metrics.top_processes)
            await self._update_port_rows(metrics.open_ports)
            
    def watch_pulse_frame(self, frame: int):
        if self.metrics:
            self._render_telemetry_widget(self.metrics)

    def _render_telemetry_widget(self, m: SystemMetrics):
        try:
            self.query_one("#stats_telemetry", Static).update(self._render_telemetry(m))
            ports_count = len(m.open_ports) if m.open_ports else 0
            ports_status = f"[#3fb950]{ports_count} listening[/#3fb950]" if ports_count > 0 else "[dim #8b949e]none[/#8b949e]"
            self.query_one("#ports_header", Static).update(
                f"[bold #58a6ff]🌐 OPEN PORTS[/bold #58a6ff] [dim #8b949e]• {ports_status} • Click to Close[/dim #8b949e]"
            )
        except Exception:
            pass

    def _render_telemetry(self, m: SystemMetrics) -> str:
        spinner = self.SPINNER_FRAMES[self.pulse_frame % len(self.SPINNER_FRAMES)]
        pulse_dot = self.HEARTBEAT_FRAMES[self.pulse_frame % len(self.HEARTBEAT_FRAMES)]
        
        # System info subtitle
        cores_str = f"{m.cpu_cores} Cores" if m.cpu_cores else "Multi-Core"
        uptime_str = f"Uptime: {m.uptime_hours:.1f}h" if m.uptime_hours is not None else ""
        sys_subtitle = f"macOS (Apple Silicon) • {cores_str}" + (f" • {uptime_str}" if uptime_str else "")
        
        # CPU meter & sparkline
        cpu_gauge = render_gauge_bar(m.cpu_percent, width=12)
        cpu_spark = render_sparkline(getattr(m, 'cpu_history', []), 0.0, 100.0, color="#d29922")
        
        # GPU meter
        if m.gpu_percent is not None:
            gpu_gauge = render_gauge_bar(m.gpu_percent, width=12)
            if m.gpu_memory_used_mb:
                gpu_detail = f" [dim]({m.gpu_memory_used_mb:.0f}MB VRAM)[/dim]"
            elif m.gpu_device:
                gpu_detail = f" [dim]({m.gpu_device})[/dim]"
            else:
                gpu_detail = ""
        elif m.gpu_device:
            gpu_gauge = "[dim]○ ░░░░░░░░░░░░  N/A[/dim]"
            gpu_detail = f" [dim]({m.gpu_device})[/dim]"
        else:
            gpu_gauge = "[dim]○ ░░░░░░░░░░░░  N/A[/dim]"
            gpu_detail = ""
        
        # Memory meter with GB breakdown & sparkline
        mem_gauge = render_gauge_bar(m.memory_percent, width=12)
        mem_detail = f" [dim]({m.memory_used_gb:.1f}/{m.memory_total_gb:.1f}GB)[/dim]" if (m.memory_used_gb and m.memory_total_gb) else ""
        mem_spark = render_sparkline(getattr(m, 'mem_history', []), 0.0, 100.0, color="#3fb950")
        
        # Disk meter with GB breakdown
        disk_gauge = render_gauge_bar(m.disk_percent, width=12)
        disk_detail = f" [dim]({m.disk_free_gb:.1f}GB free)[/dim]" if m.disk_free_gb else ""

        # Network Bandwidth & Speed Meter + Sparkline
        net_spark = render_sparkline(getattr(m, 'net_history', []), 0.0, None, color="#39c5cf")
        down_spd = getattr(m, 'net_download_speed', '0.0 B/s')
        up_spd = getattr(m, 'net_upload_speed', '0.0 B/s')
        recv_tot = getattr(m, 'net_total_recv_str', '0 B')
        net_speeds = f"[bold #39c5cf]↓ {down_spd:<9}[/bold #39c5cf] [bold #bc8cff]↑ {up_spd:<9}[/bold #bc8cff]"
        net_detail = f" [dim]({recv_tot} total)[/dim]"

        return f"""[bold #58a6ff]⚡ SYSTEM TELEMETRY[/bold #58a6ff]  [bold #3fb950]{pulse_dot} LIVE[/bold #3fb950] [dim #58a6ff]{spinner}[/dim #58a6ff]
[dim #8b949e]{sys_subtitle}[/dim #8b949e]

[bold #c9d1d9]CPU [/bold #c9d1d9] {cpu_gauge}  {cpu_spark}
[bold #c9d1d9]GPU [/bold #c9d1d9] {gpu_gauge}{gpu_detail}
[bold #c9d1d9]MEM [/bold #c9d1d9] {mem_gauge}  {mem_spark}{mem_detail}
[bold #c9d1d9]DISK[/bold #c9d1d9] {disk_gauge}{disk_detail}
[bold #c9d1d9]NET [/bold #c9d1d9] {net_speeds}  {net_spark}{net_detail}"""

    async def _update_process_rows(self, processes: list):
        try:
            procs_list = self.query_one("#procs_list", Vertical)
            top_procs = processes[:5]
            if not top_procs:
                if not procs_list.children:
                    await procs_list.mount(Static("  [dim #8b949e](collecting process telemetry...)[/dim #8b949e]", id="proc_placeholder"))
                return

            current_rows = [w for w in procs_list.children if isinstance(w, ProcRow)]
            if [r.pid for r in current_rows] == [p['pid'] for p in top_procs]:
                for r, p in zip(current_rows, top_procs):
                    r.update_data(p['name'], p.get('cpu_percent', 0.0), p.get('memory_percent', 0.0))
            else:
                await procs_list.remove_children()
                new_rows = [
                    ProcRow(p['pid'], p['name'], p.get('cpu_percent', 0.0), p.get('memory_percent', 0.0))
                    for p in top_procs
                ]
                await procs_list.mount_all(new_rows)
        except Exception:
            pass

    async def _update_port_rows(self, ports: Optional[list]):
        try:
            ports_list = self.query_one("#ports_list", Vertical)
            top_ports = (ports or [])[:4]
            if not top_ports:
                await ports_list.remove_children()
                await ports_list.mount(Static("  [dim #8b949e]● No active listening ports detected[/dim #8b949e]", id="ports_empty"))
                return

            current_rows = [w for w in ports_list.children if isinstance(w, PortRow)]
            if [(r.port, r.pid) for r in current_rows] == [(p['port'], p['pid']) for p in top_ports]:
                for r, p in zip(current_rows, top_ports):
                    r.update_data(p.get('protocol', 'TCP'), p['pid'], p['process'])
            else:
                await ports_list.remove_children()
                new_rows = [
                    PortRow(p['port'], p.get('protocol', 'TCP'), p['pid'], p['process'])
                    for p in top_ports
                ]
                await ports_list.mount_all(new_rows)
        except Exception:
            pass

    def _render_metrics(self, m: SystemMetrics) -> str:
        divider = "[dim #21262d]────────────────────────────────────────────────[/dim #21262d]"
        ports_count = len(m.open_ports) if m.open_ports else 0
        ports_status = f"[#3fb950]{ports_count} listening[/#3fb950]" if ports_count > 0 else "[dim #8b949e]none[/#8b949e]"
        return f"""{self._render_telemetry(m)}

{divider}
[bold #58a6ff]⚡ ACTIVE PROCESSES[/bold #58a6ff] [dim #8b949e]• Top CPU & Memory[/dim #8b949e]
{self._render_processes(m.top_processes)}

{divider}
[bold #58a6ff]🌐 OPEN PORTS[/bold #58a6ff] [dim #8b949e]• {ports_status}[/dim #8b949e]
{self._render_ports(m.open_ports)}"""

    def _render_processes(self, processes: list) -> str:
        if not processes:
            return "  [dim #8b949e](collecting process telemetry...)[/dim #8b949e]"
        lines = ["[dim #8b949e]  PID     NAME                 CPU       MEM[/dim #8b949e]"]
        for p in processes[:5]:
            name = (p['name'][:18] + "..") if len(p['name']) > 20 else p['name']
            lines.append(
                f"  [dim #58a6ff]{p['pid']:<7}[/dim #58a6ff] [white]{name:<20}[/white] "
                f"[#f0883e]{p.get('cpu_percent', 0.0):>7.1f}%[/#f0883e] "
                f"[#bc8cff]{p.get('memory_percent', 0.0):>6.1f}%[/#bc8cff]"
            )
        return "\n".join(lines)

    def _render_ports(self, ports: Optional[list]) -> str:
        if not ports:
            return "  [dim #8b949e]● No active listening ports detected[/dim #8b949e]"
        lines = ["[dim #8b949e]  PORT    PROTO  PID     PROCESS[/dim #8b949e]"]
        for p in ports[:4]:
            name = (p['process'][:16] + "..") if len(p['process']) > 18 else p['process']
            lines.append(
                f"  [bold #3fb950]{p['port']:<7}[/bold #3fb950] "
                f"[dim]{p.get('protocol', 'TCP'):<6}[/dim] "
                f"[cyan]{p['pid']:<7}[/cyan] "
                f"[white]{name}[/white]"
            )
        if len(ports) > 4:
            lines.append(f"  [dim #8b949e]... and {len(ports) - 4} more listening ports[/dim #8b949e]")
        return "\n".join(lines)


class AuditLogPanel(Static):
    """Right-bottom panel: Recent audit log entries."""
    
    def refresh_log(self):
        actions = get_recent_actions(10)
        self.update(self._render_actions(actions))
    
    def _render_actions(self, actions: list) -> str:
        header = "[bold #bc8cff]◈ AUDIT TRAIL[/bold #bc8cff] [dim #8b949e]• Reversible Operations Log[/dim #8b949e]"
        if not actions:
            return f"{header}\n\n[dim #8b949e]No system actions logged yet.\nOperations and safety checkpoints will appear here.[/dim #8b949e]"
        
        lines = [header]
        for a in actions[:8]:
            tier = a.get('tier', 'unknown')
            if tier == "read_only":
                tier_badge = "[#3fb950]● READ_ONLY[/#3fb950]"
            elif tier == "reversible":
                tier_badge = "[#e3b341]▲ REVERSIBLE[/#e3b341]"
            elif tier == "destructive":
                tier_badge = "[#f85149]✖ DESTRUCTIVE[/#f85149]"
            else:
                tier_badge = f"[white]{tier}[/white]"

            status_icon = {"executed": "[#3fb950]✓[/#3fb950]", "failed": "[#f85149]✗[/#f85149]", "undone": "[#e3b341]↶[/#e3b341]"}.get(a.get('status'), "?")
            raw_time = a.get('timestamp', '')
            time_str = raw_time[11:19] if len(raw_time) >= 19 else raw_time
            
            action_name = str(a.get('action', 'unknown'))
            lines.append(f"[dim #8b949e]{time_str}[/dim #8b949e] {status_icon} {tier_badge} [white]{action_name}[/white]")
            
        lines.append("\n[dim #6e7681]ℹ Destructive commands require prompt confirmation. Undo with 'Ctrl+U'.[/dim #6e7681]")
        return "\n".join(lines)


class ChatPanel(Container):
    """Left panel: Conversational chat interface with integrated action chips."""
    
    def compose(self) -> ComposeResult:
        yield Static(
            "[bold #58a6ff]🛡️ SENTINEL[/bold #58a6ff]  [dim #8b949e]• AI‑Powered OS Management Agent[/dim #8b949e]",
            id="chat_header"
        )
        yield Static(
            "[bold #58a6ff]⚡ AI ENGINES[/bold #58a6ff]  [dim #8b949e]Initialising engine mesh...[/dim #8b949e]",
            id="ai_engine_bar"
        )
        yield RichLog(id="chat_log", wrap=True, min_width=1, highlight=True, markup=True)
        yield Static(
            "[dim #3fb950]● Ready[/dim #3fb950] [dim #8b949e]| Telemetry monitor active[/dim #8b949e]",
            id="agent_status"
        )
        
        # Quick action chips placed directly above input
        with Horizontal(id="quick_chips"):
            yield Button("✦ Health", id="btn_health", classes="chip")
            yield Button("≡ Procs", id="btn_procs", classes="chip")
            yield Button("▲ Kill CPU", id="btn_kill_cpu", classes="chip")
            yield Button("⚡ Close Port", id="btn_close_port", classes="chip")
            yield Button("↺ Undo", id="btn_undo", classes="chip")
            yield Button("⌧ Clear", id="btn_clear", classes="chip")
            yield Button("✕ Exit", id="btn_exit", classes="chip chip-exit")
            
        with Horizontal(id="input_container"):
            yield Input(placeholder="Ask anything or enter a command... (e.g. 'how is my pc', 'free disk space')", id="chat_input")
            yield Button("Send ↵", id="btn_send", variant="primary")
    
    def update_ai_engines(self, content: str):
        """Update the aesthetic AI engine status bar."""
        try:
            self.query_one("#ai_engine_bar", Static).update(content)
        except Exception:
            pass

    def on_mount(self):
        self.query_one("#chat_input").focus()
    
    def add_message(self, role: str, content: str, style: str = ""):
        """Format messages conversationally with distinct author blocks and structured tables."""
        chat_log = self.query_one("#chat_log", RichLog)
        timestamp = datetime.now().strftime("%H:%M")
        
        try:
            if role == "user":
                chat_log.write(f"\n[bold #58a6ff]▸ You[/bold #58a6ff] [dim #8b949e]{timestamp}[/dim #8b949e]")
                render_message_to_log(chat_log.write, content, indent="  ")
            elif role == "agent":
                chat_log.write(f"\n[bold #3fb950]✦ OS Assistant[/bold #3fb950] [dim #8b949e]{timestamp}[/dim #8b949e]")
                render_message_to_log(chat_log.write, content, indent="  ")
            elif role == "system":
                chat_log.write(f"\n[dim #8b949e]ℹ SYSTEM • {timestamp}[/dim #8b949e]")
                render_message_to_log(chat_log.write, content, indent="  [dim #8b949e]")
            else:
                chat_log.write(f"\n[bold white]{role.upper()}[/bold white] [dim #8b949e]{timestamp}[/dim #8b949e]")
                render_message_to_log(chat_log.write, content, indent="  ")
        except Exception:
            chat_log.write(f"\n[{role.upper()} - {timestamp}] {content}")
        
        # Scroll to bottom so latest output is directly visible without manual scrolling
        chat_log.scroll_end(animate=False)


class AIOSAgentApp(App):
    """Main TUI Application."""
    
    TITLE = "SENTINEL"
    SUB_TITLE = "AI‑Powered OS Management Agent"
    
    CSS = """
    Screen {
        layout: vertical;
        background: #0d1117;
        color: #c9d1d9;
    }
    
    Header {
        dock: top;
        height: 1;
        background: #161b22;
        color: #58a6ff;
    }
    
    #main_container {
        height: 1fr;
        layout: horizontal;
        padding: 0 1;
        margin-top: 0;
    }
    
    #left_panel {
        width: 60%;
        border: round #30363d;
        background: #161b22;
        margin-right: 1;
    }
    
    #left_panel:focus-within {
        border: round #388bfd;
    }
    
    #right_panel {
        width: 40%;
        layout: vertical;
    }
    
    #stats_panel {
        height: 60%;
        border: round #30363d;
        background: #161b22;
        padding: 0 1;
        margin-bottom: 1;
        overflow-y: auto;
        scrollbar-size-vertical: 0;
    }
    
    #stats_panel:focus-within {
        border: round #58a6ff;
    }
    
    .panel-divider {
        height: 1;
        margin: 0;
        padding: 0;
        color: #30363d;
    }
    
    #procs_header, #ports_header {
        height: 1;
        margin: 0;
        padding: 0;
    }
    
    #procs_sub_header, #ports_sub_header {
        height: 1;
        margin: 0;
        padding: 0;
        color: #8b949e;
    }
    
    #procs_list, #ports_list {
        height: auto;
        margin: 0;
        padding: 0;
    }
    
    #audit_panel {
        height: 40%;
        border: round #30363d;
        background: #161b22;
        padding: 1;
    }
    
    #audit_panel:focus-within {
        border: round #bc8cff;
    }
    
    #chat_header {
        height: 1;
        padding: 0 1;
        background: #161b22;
    }
    
    #ai_engine_bar {
        height: 2;
        padding: 0 1;
        background: #161b22;
        border-bottom: solid #21262d;
    }
    
    #chat_log {
        height: 1fr;
        padding: 0 1;
        background: #0d1117;
        overflow-x: hidden;
        scrollbar-size-horizontal: 0;
    }
    
    #agent_status {
        height: 1;
        padding: 0 1;
        margin: 0 1;
        background: transparent;
        color: #58a6ff;
    }
    
    #quick_chips {
        height: 3;
        background: transparent;
        padding: 0 1;
        margin-top: 1;
        margin-bottom: 1;
        align: left middle;
    }
    
    .chip {
        height: 3;
        min-width: 9;
        margin-right: 1;
        background: #21262d;
        color: #c9d1d9;
        border: round #30363d;
    }
    
    .chip:hover {
        background: #30363d;
        color: #58a6ff;
        border: round #58a6ff;
    }
    
    .chip-exit {
        color: #f85149;
    }
    
    .chip-exit:hover {
        background: #da3633;
        color: #ffffff;
        border: round #f85149;
    }
    
    #input_container {
        height: 3;
        background: transparent;
        margin: 0 1 1 1;
        layout: horizontal;
    }
    
    #chat_input {
        width: 1fr;
        height: 3;
        border: round #30363d;
        background: #0d1117;
        color: #f0f6fc;
        padding: 0 1;
        margin-right: 1;
    }
    
    #chat_input:focus {
        border: round #388bfd;
    }
    
    #btn_send {
        width: 11;
        min-width: 9;
        height: 3;
        border: round #238636;
        background: #238636;
        color: #ffffff;
        text-style: bold;
    }
    
    #btn_send:hover {
        background: #2ea043;
        border: round #2ea043;
    }
    
    Footer {
        background: #161b22;
        color: #8b949e;
    }
    """
    
    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
        ("escape", "quit", "Quit"),
        ("ctrl+u", "undo", "Undo"),
        ("ctrl+l", "clear_chat", "Clear Chat"),
    ]
    
    SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    
    def __init__(self):
        super().__init__()
        self.agent = Agent()
        self.watcher = SystemWatcher(interval=2.0, on_anomaly=self.on_anomaly_detected)
        self._chat_panel: Optional[ChatPanel] = None
        self._stats_panel: Optional[SystemStatsPanel] = None
        self._audit_panel: Optional[AuditLogPanel] = None
        self._agent_busy: bool = False
        self._anim_frame: int = 0
    
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        
        with Horizontal(id="main_container"):
            with Vertical(id="left_panel"):
                self._chat_panel = ChatPanel(id="chat_panel")
                yield self._chat_panel
            
            with Vertical(id="right_panel"):
                self._stats_panel = SystemStatsPanel(id="stats_panel")
                yield self._stats_panel
                self._audit_panel = AuditLogPanel(id="audit_panel")
                yield self._audit_panel
            
        yield Footer()
    
    def on_mount(self):
        # Initialize database
        init_db()
        
        # Start background monitor
        self.watcher.start()
        
        # Start metrics update timer
        self.set_interval(2.0, self.update_metrics)
        
        # Refresh audit log periodically
        self.set_interval(5.0, self.refresh_audit_log)
        
        # Live animation ticker (150ms)
        self.set_interval(0.15, self._tick_animation)
        
        # Welcome message in chat log
        self._chat_panel.add_message("system", "SENTINEL OS Assistant is online. Ready to inspect, troubleshoot, or tune your Mac.")
        if self._chat_panel:
            self._chat_panel.update_ai_engines(self._render_provider_health())

    def _tick_animation(self):
        """Update animation frames for live telemetry and agent activity."""
        self._anim_frame = (self._anim_frame + 1) % 120
        if self._stats_panel:
            self._stats_panel.pulse_frame = self._anim_frame
            
        # Update thinking/idle status indicator
        try:
            status_widget = self.query_one("#agent_status", Static)
            if self._agent_busy:
                spinner = self.SPINNER_FRAMES[self._anim_frame % len(self.SPINNER_FRAMES)]
                status_widget.update(f"[bold #58a6ff]{spinner}[/bold #58a6ff] [bold white]Thinking & inspecting system state...[/bold white]")
            else:
                status_widget.update("[dim #3fb950]● Ready[/dim #3fb950] [dim #8b949e]| Live telemetry active[/dim #8b949e]")
        except Exception:
            pass

    def _render_provider_health(self) -> str:
        """Render aesthetic badge pills for AI engine statuses."""
        provider_status = getattr(self.agent, "llm_provider_status", {})
        if not provider_status:
            return "[bold #58a6ff]⚡ AI ENGINES[/bold #58a6ff]  [dim #8b949e]Status initializing...[/dim #8b949e]"

        order = [
            ("lmstudio", "LM Studio", "LOCAL"),
            ("groq", "Groq (Qwen)", "CLOUD"),
            ("gemini", "Gemini", "CLOUD"),
        ]
        badges = []
        for key, display_name, engine_type in order:
            state = provider_status.get(key, {})
            configured = bool(state.get("configured", False))
            available = bool(state.get("available", False))

            if configured and available:
                badge = f"[on #1a2f23] [#3fb950]●[/#3fb950] [bold #f0f6fc]{display_name}[/bold #f0f6fc] [#3fb950]READY[/#3fb950] [/on #1a2f23]"
            elif configured and not available:
                badge = f"[on #232018] [#d29922]○[/#d29922] [dim #8b949e]{display_name}[/dim #8b949e] [#d29922]STANDBY[/#d29922] [/on #232018]"
            else:
                badge = f"[on #201a1c] [#f85149]○[/#f85149] [dim #6e7681]{display_name}[/dim #6e7681] [#f85149]DISABLED[/#f85149] [/on #201a1c]"
            badges.append(badge)

        return "[bold #58a6ff]⚡ AI ENGINES[/bold #58a6ff]  " + "  ".join(badges)
    
    def update_metrics(self):
        """Update system metrics display."""
        metrics = self.watcher.get_current_metrics()
        if metrics and self._stats_panel:
            self._stats_panel.metrics = metrics
    
    def refresh_audit_log(self):
        """Refresh audit log display."""
        if self._audit_panel:
            self._audit_panel.refresh_log()
    
    def on_anomaly_detected(self, anomaly: str, metrics: SystemMetrics):
        """Called when watcher detects an anomaly (from background thread)."""
        if self._chat_panel:
            self.call_from_thread(
                self._chat_panel.add_message,
                "system",
                f"⚠️ Anomaly detected: {anomaly}"
            )
    
    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button click events."""
        btn_id = event.button.id
        if btn_id == "btn_exit":
            self.action_quit()
        elif btn_id == "btn_undo":
            self.action_undo()
        elif btn_id == "btn_clear":
            self.action_clear_chat()
        elif btn_id == "btn_health":
            self._trigger_quick_command("how is my pc")
        elif btn_id == "btn_procs":
            self._trigger_quick_command("show top 5 processes")
        elif btn_id == "btn_kill_cpu":
            self._trigger_quick_command("kill highest cpu")
        elif btn_id == "btn_close_port":
            input_widget = self.query_one("#chat_input", Input)
            input_widget.value = "close port "
            input_widget.focus()
            input_widget.cursor_position = len(input_widget.value)
        elif btn_id == "btn_send":
            input_widget = self.query_one("#chat_input", Input)
            val = input_widget.value.strip()
            if val:
                input_widget.value = ""
                self._handle_user_input(val)
        elif btn_id.startswith("kill_pid_"):
            pid = btn_id.replace("kill_pid_", "")
            self._trigger_quick_command(f"kill process {pid}")
        elif btn_id.startswith("close_port_"):
            port = btn_id.replace("close_port_", "")
            self._trigger_quick_command(f"close port {port}")

    def on_click(self, event) -> None:
        """Handle clicks on inspectable process and port rows in telemetry panel."""
        widget = getattr(event, "widget", None)
        if not widget or not getattr(widget, "id", None):
            return
        wid = str(widget.id)
        if wid.startswith("inspect_proc_"):
            pid = wid.replace("inspect_proc_", "")
            self._trigger_quick_command(f"inspect process {pid}")
        elif wid.startswith("inspect_port_"):
            port = wid.replace("inspect_port_", "")
            self._trigger_quick_command(f"what is running on port {port}")

    def _trigger_quick_command(self, command: str):
        """Execute a quick button action as if typed by user."""
        self._handle_user_input(command)

    async def process_command(self, user_input: str):
        """Process user command through agent with conversational commentary."""
        self._agent_busy = True
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(self.agent.chat, user_input),
                timeout=12.0
            )
            
            # Check for confirmation request
            if response.startswith("__CONFIRM_KILL__") and response.endswith("__"):
                confirm_data = json.loads(response[16:-2])
                await self._handle_kill_confirmation(confirm_data)
            else:
                self._chat_panel.add_message("agent", response)
        except asyncio.TimeoutError:
            self._chat_panel.add_message(
                "system",
                "⏱ Operation timed out after 12 seconds. System state inspection was cancelled."
            )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self._chat_panel.add_message("system", f"Encountered an issue: {e}")
        finally:
            self._agent_busy = False
            self.refresh_audit_log()
    
    async def _handle_kill_confirmation(self, confirm_data: dict):
        """Handle kill confirmation inline in chat."""
        pids = confirm_data.get('pids') or [confirm_data['pid']]
        name = confirm_data.get('name', 'process')
        reason = confirm_data.get('reason', '')
        port = confirm_data.get('port')
        
        if len(pids) > 1:
            pid_str = f"{len(pids)} processes (PIDs: {', '.join(str(p) for p in pids[:6])}{'...' if len(pids) > 6 else ''})"
        else:
            pid_str = f"PID {pids[0]}"
        
        if port:
            prompt = (
                f"🔴 Safety Confirmation: Closing port {port} by terminating {pid_str} ({name}).\n"
                f"Would you like me to proceed? Type 'yes' to confirm, or anything else to cancel."
            )
            placeholder = f"Type 'yes' to close port {port}, or anything else to cancel"
        else:
            prompt = (
                f"🔴 Safety Confirmation: Terminating {pid_str} ({name}) — {reason}.\n"
                f"Would you like me to proceed? Type 'yes' to confirm, or anything else to cancel."
            )
            placeholder = f"Type 'yes' to confirm termination of {name}, or anything else to cancel"

        self._chat_panel.add_message("system", prompt)
        
        input_widget = self._chat_panel.query_one("#chat_input", Input)
        self._pending_kill = confirm_data
        input_widget.placeholder = placeholder

    async def _execute_confirmed_kill(self, confirm_data: dict):
        """Run the destructive kill off the event loop and update chat."""
        pids = confirm_data.get('pids') or [confirm_data['pid']]
        name = confirm_data.get('name', 'process')
        port = confirm_data.get('port')
        
        success_count = 0
        failed_errors = []
        for pid in pids:
            result = await asyncio.to_thread(
                self.agent._execute_tool_with_permissions,
                'kill_process',
                {'pid': pid},
                confirmed=True,
            )
            if result.get('success'):
                success_count += 1
            else:
                failed_errors.append(f"PID {pid}: {result.get('error')}")
        
        if success_count > 0 and not failed_errors:
            if port:
                self._chat_panel.add_message("agent", f"I've successfully closed port {port} by stopping {name}.")
            elif len(pids) == 1:
                self._chat_panel.add_message("agent", f"I've successfully terminated process PID {pids[0]} ({name}).")
            else:
                self._chat_panel.add_message("agent", f"I've successfully stopped all {success_count} processes associated with {name}.")
        elif success_count > 0 and failed_errors:
            self._chat_panel.add_message("agent", f"Stopped {success_count} processes. However, {len(failed_errors)} could not be terminated: {', '.join(failed_errors)}")
        else:
            self._chat_panel.add_message("agent", f"Unable to terminate the requested process: {', '.join(failed_errors) if failed_errors else 'unknown error'}")
        self.refresh_audit_log()
    
    def _handle_user_input(self, user_input: str):
        """Route user input to kill confirmation or conversational agent."""
        if not user_input:
            return
            
        if hasattr(self, '_pending_kill') and self._pending_kill:
            confirm_data = self._pending_kill
            del self._pending_kill
            
            input_widget = self._chat_panel.query_one("#chat_input", Input)
            input_widget.placeholder = "Ask anything or enter a command... (e.g. 'how is my pc', 'free disk space')"
            
            if user_input.lower() == 'yes':
                self._chat_panel.add_message("user", user_input)
                asyncio.create_task(self._execute_confirmed_kill(confirm_data))
            else:
                self._chat_panel.add_message("user", user_input)
                self._chat_panel.add_message("system", "Process termination was cancelled.")
        else:
            self._chat_panel.add_message("user", user_input)
            self._cmd_task = asyncio.create_task(self.process_command(user_input))

    def on_input_submitted(self, event: Input.Submitted):
        """Handle enter key in input field."""
        user_input = event.value.strip()
        event.input.value = ""
        self._handle_user_input(user_input)
    
    def action_quit(self):
        """Cleanly and immediately stop watcher and exit the application."""
        try:
            self.watcher.stop()
        except Exception:
            pass
        if hasattr(self, '_cmd_task') and self._cmd_task and not self._cmd_task.done():
            self._cmd_task.cancel()
        self.exit()

    def action_undo(self):
        """Undo last action."""
        result = self.agent.undo_last()
        self._chat_panel.add_message("agent", f"↺ Reverted action: {result}")
        self.refresh_audit_log()
    
    def action_clear_chat(self):
        """Clear chat log."""
        self._chat_panel.query_one("#chat_log", RichLog).clear()
    
    def on_unmount(self):
        try:
            self.watcher.stop()
        except Exception:
            pass
        if hasattr(self, '_cmd_task') and self._cmd_task and not self._cmd_task.done():
            self._cmd_task.cancel()


def run_tui():
    """Run the TUI application."""
    app = AIOSAgentApp()
    app.run()


if __name__ == "__main__":
    run_tui()