# Background Monitor - System Metrics Watcher (Step 8 - Stub)

import threading
import time
import psutil
from collections import deque
from typing import Callable, Optional, List
from dataclasses import dataclass, field

from src.tools.read_only import get_gpu_usage, list_open_ports, format_speed, format_bytes


@dataclass
class SystemMetrics:
    """Current system metrics snapshot."""
    cpu_percent: float
    memory_percent: float
    disk_percent: float
    top_processes: list
    timestamp: float
    gpu_percent: Optional[float] = None
    gpu_device: Optional[str] = None
    gpu_memory_used_mb: Optional[float] = None
    memory_used_gb: Optional[float] = None
    memory_total_gb: Optional[float] = None
    disk_free_gb: Optional[float] = None
    disk_total_gb: Optional[float] = None
    uptime_hours: Optional[float] = None
    cpu_cores: Optional[int] = None
    open_ports: Optional[list] = None
    net_download_speed: str = "0.0 B/s"
    net_upload_speed: str = "0.0 B/s"
    net_download_bytes_sec: float = 0.0
    net_upload_bytes_sec: float = 0.0
    net_total_recv_str: str = "0 B"
    net_total_sent_str: str = "0 B"
    cpu_history: list = field(default_factory=list)
    mem_history: list = field(default_factory=list)
    net_history: list = field(default_factory=list)


class SystemWatcher:
    """Background thread that polls system metrics and detects anomalies."""
    
    def __init__(self, 
                 interval: float = 5.0,
                 cpu_threshold: float = 90.0,
                 memory_threshold: float = 90.0,
                 disk_threshold: float = 90.0,
                 gpu_threshold: float = 90.0,
                 on_anomaly: Optional[Callable[[str, SystemMetrics], None]] = None):
        self.interval = interval
        self.cpu_threshold = cpu_threshold
        self.memory_threshold = memory_threshold
        self.disk_threshold = disk_threshold
        self.gpu_threshold = gpu_threshold
        self.on_anomaly = on_anomaly
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._last_metrics: Optional[SystemMetrics] = None

        # Rolling history deques for sparklines (up to 20 samples)
        self.cpu_history: deque = deque(maxlen=20)
        self.mem_history: deque = deque(maxlen=20)
        self.net_history: deque = deque(maxlen=20)

        # Network I/O delta tracking
        self._last_net_bytes_recv: Optional[int] = None
        self._last_net_bytes_sent: Optional[int] = None
        self._last_net_time: Optional[float] = None

    
    def start(self):
        """Start the background monitoring thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
    
    def stop(self):
        """Stop the background monitoring thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
    
    def _run(self):
        """Main monitoring loop."""
        while self._running:
            try:
                metrics = self._collect_metrics()
                self._last_metrics = metrics
                self._check_anomalies(metrics)
            except Exception as e:
                print(f"Monitor error: {e}")
            time.sleep(self.interval)
    
    def _collect_metrics(self) -> SystemMetrics:
        """Collect current system metrics."""
        cpu = psutil.cpu_percent(interval=0.5)
        vm = psutil.virtual_memory()
        memory = vm.percent
        du = psutil.disk_usage('/')
        disk = du.percent
        
        boot = psutil.boot_time()
        uptime_h = (time.time() - boot) / 3600.0 if boot > 0 else 0.0
        cores = psutil.cpu_count(logical=True)
        
        processes = []
        for proc in psutil.process_iter(['pid', 'name', 'memory_percent', 'cpu_percent', 'memory_info']):
            try:
                info = proc.info
                if info.get('cpu_percent') and info['cpu_percent'] > 0.1:
                    processes.append(info)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        
        top_processes = sorted(processes, key=lambda x: x['cpu_percent'] or 0, reverse=True)[:7]
        
        gpu_percent = None
        gpu_device = None
        gpu_mem_used = None
        try:
            gpu_info = get_gpu_usage()
            if gpu_info.get("available"):
                gpu_percent = gpu_info.get("gpu_percent")
                gpu_device = gpu_info.get("device")
                gpu_mem_used = gpu_info.get("memory_used_mb")
        except Exception:
            pass

        open_ports = []
        try:
            port_info = list_open_ports()
            if port_info.get("success"):
                open_ports = port_info.get("connections", [])
        except Exception:
            pass

        # Network Bandwidth speed calculation
        now = time.time()
        bytes_recv_sec = 0.0
        bytes_sent_sec = 0.0
        net_recv_str = "0 B"
        net_sent_str = "0 B"
        try:
            net_io = psutil.net_io_counters()
            if self._last_net_time is not None and self._last_net_bytes_recv is not None:
                dt = max(0.001, now - self._last_net_time)
                bytes_recv_sec = max(0.0, (net_io.bytes_recv - self._last_net_bytes_recv) / dt)
                bytes_sent_sec = max(0.0, (net_io.bytes_sent - self._last_net_bytes_sent) / dt)
            self._last_net_bytes_recv = net_io.bytes_recv
            self._last_net_bytes_sent = net_io.bytes_sent
            self._last_net_time = now
            net_recv_str = format_bytes(net_io.bytes_recv)
            net_sent_str = format_bytes(net_io.bytes_sent)
        except Exception:
            pass

        # Append to sparkline rolling history
        self.cpu_history.append(cpu)
        self.mem_history.append(memory)
        self.net_history.append(bytes_recv_sec)

        return SystemMetrics(
            cpu_percent=cpu,
            memory_percent=memory,
            disk_percent=disk,
            top_processes=top_processes,
            timestamp=now,
            gpu_percent=gpu_percent,
            gpu_device=gpu_device,
            gpu_memory_used_mb=gpu_mem_used,
            memory_used_gb=round(vm.used / (1024**3), 2),
            memory_total_gb=round(vm.total / (1024**3), 2),
            disk_free_gb=round(du.free / (1024**3), 1),
            disk_total_gb=round(du.total / (1024**3), 1),
            uptime_hours=round(uptime_h, 1),
            cpu_cores=cores,
            open_ports=open_ports,
            net_download_speed=format_speed(bytes_recv_sec),
            net_upload_speed=format_speed(bytes_sent_sec),
            net_download_bytes_sec=round(bytes_recv_sec, 1),
            net_upload_bytes_sec=round(bytes_sent_sec, 1),
            net_total_recv_str=net_recv_str,
            net_total_sent_str=net_sent_str,
            cpu_history=list(self.cpu_history),
            mem_history=list(self.mem_history),
            net_history=list(self.net_history)
        )
    
    def _check_anomalies(self, metrics: SystemMetrics):
        """Check for anomalies and trigger callback."""
        anomalies = []
        
        if metrics.cpu_percent > self.cpu_threshold:
            anomalies.append(f"High CPU usage: {metrics.cpu_percent:.1f}%")
        if metrics.gpu_percent is not None and metrics.gpu_percent > self.gpu_threshold:
            anomalies.append(f"High GPU usage: {metrics.gpu_percent:.1f}%")
        if metrics.memory_percent > self.memory_threshold:
            anomalies.append(f"High memory usage: {metrics.memory_percent:.1f}%")
        if metrics.disk_percent > self.disk_threshold:
            anomalies.append(f"High disk usage: {metrics.disk_percent:.1f}%")
        
        for anomaly in anomalies:
            if self.on_anomaly:
                self.on_anomaly(anomaly, metrics)
    
    def get_current_metrics(self) -> Optional[SystemMetrics]:
        """Get the most recent metrics snapshot."""
        return self._last_metrics