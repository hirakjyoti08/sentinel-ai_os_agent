# Read-Only System Tools

import psutil
import os
import platform
import time
import subprocess
import re
from typing import List, Dict, Any


def list_processes() -> Dict[str, Any]:
    """List all running processes with key metrics."""
    processes = []
    now = time.time()
    for proc in psutil.process_iter(['pid', 'name', 'memory_info', 'cpu_percent', 'create_time']):
        try:
            info = proc.info
            cpu_pct = info['cpu_percent'] if info['cpu_percent'] is not None else 0
            memory_mb = info['memory_info'].rss / 1024 / 1024 if info['memory_info'] else 0
            uptime = max(0.0, now - info['create_time']) if info.get('create_time') else 0
            processes.append({
                "pid": info['pid'],
                "name": info['name'],
                "memory_mb": round(memory_mb, 2),
                "cpu_percent": round(cpu_pct or 0, 1),
                "uptime_sec": round(uptime, 0)
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return {"success": True, "processes": sorted(processes, key=lambda x: x['cpu_percent'], reverse=True)}


def get_disk_usage(path: str = "/") -> Dict[str, Any]:
    """Get disk usage for a given path."""
    usage = psutil.disk_usage(path)
    return {
        "success": True,
        "path": path,
        "total_gb": round(usage.total / 1024**3, 2),
        "used_gb": round(usage.used / 1024**3, 2),
        "free_gb": round(usage.free / 1024**3, 2),
        "percent": round(usage.percent, 1)
    }


def get_cpu_usage() -> Dict[str, Any]:
    """Get system-wide CPU usage percentage."""
    return {"success": True, "cpu_percent": psutil.cpu_percent(interval=0.1)}


def get_memory_usage() -> Dict[str, Any]:
    """Get system-wide RAM and Swap memory usage."""
    vm = psutil.virtual_memory()
    swap = psutil.swap_memory()
    return {
        "success": True,
        "total_gb": round(vm.total / 1024**3, 2),
        "used_gb": round(vm.used / 1024**3, 2),
        "available_gb": round(vm.available / 1024**3, 2),
        "percent": round(vm.percent, 1),
        "swap_total_gb": round(swap.total / 1024**3, 2),
        "swap_used_gb": round(swap.used / 1024**3, 2),
        "swap_percent": round(swap.percent, 1)
    }


def get_system_info() -> Dict[str, Any]:
    """Get hardware architecture, OS platform, CPU cores, and total RAM."""
    vm = psutil.virtual_memory()
    return {
        "success": True,
        "os": platform.system(),
        "os_release": platform.release(),
        "machine": platform.machine(),
        "cpu_count_logical": psutil.cpu_count(logical=True),
        "cpu_count_physical": psutil.cpu_count(logical=False),
        "total_ram_gb": round(vm.total / 1024**3, 2),
        "platform": platform.platform()
    }


def get_top_memory_processes(n: int = 10) -> Dict[str, Any]:
    """Get top N processes by memory usage."""
    result = list_processes()
    if result.get("success"):
        sorted_by_mem = sorted(result["processes"], key=lambda x: x["memory_mb"], reverse=True)
        return {"success": True, "processes": sorted_by_mem[:n]}
    return result


def list_open_ports() -> Dict[str, Any]:
    """List all open network ports with process info."""
    connections = []
    seen = set()
    try:
        for conn in psutil.net_connections(kind='inet'):
            if conn.status == 'LISTEN' and conn.pid:
                try:
                    proc = psutil.Process(conn.pid)
                    key = (conn.laddr.port, conn.pid)
                    if key not in seen:
                        seen.add(key)
                        connections.append({
                            "pid": conn.pid,
                            "process": proc.name(),
                            "port": conn.laddr.port,
                            "protocol": "TCP" if conn.type == 1 else "UDP"
                        })
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
    except (psutil.AccessDenied, PermissionError):
        # On macOS, psutil.net_connections requires root/full disk access
        pass
    
    # Non-root fallback for macOS via lsof
    if not connections and platform.system() == "Darwin":
        try:
            res = subprocess.run(
                ["lsof", "-iTCP", "-sTCP:LISTEN", "-n", "-P"],
                capture_output=True,
                text=True,
                timeout=1.5
            )
            if res.returncode == 0 and res.stdout.strip():
                for line in res.stdout.strip().splitlines()[1:]:
                    parts = line.split()
                    if len(parts) >= 9:
                        cmd, pid_str, name = parts[0], parts[1], parts[8]
                        port_match = re.search(r':(\d+)$', name)
                        if port_match and pid_str.isdigit():
                            pid = int(pid_str)
                            port = int(port_match.group(1))
                            key = (port, pid)
                            if key not in seen:
                                seen.add(key)
                                connections.append({
                                    "pid": pid,
                                    "process": cmd,
                                    "port": port,
                                    "protocol": "TCP"
                                })
        except Exception:
            pass
            
    connections.sort(key=lambda x: x['port'])
    if not connections:
        return {"success": True, "connections": [], "message": "No open listening ports found"}
    return {"success": True, "connections": connections}


def get_gpu_usage() -> Dict[str, Any]:
    """Get system-wide GPU utilization percentage and memory."""
    system = platform.system()
    
    # 1. macOS IOAccelerator via ioreg (Apple Silicon / Intel Mac)
    if system == "Darwin":
        try:
            res = subprocess.run(
                ["ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator"],
                capture_output=True,
                text=True,
                timeout=1.5
            )
            if res.returncode == 0 and "PerformanceStatistics" in res.stdout:
                dev_util = re.search(r'"Device Utilization %"\s*=\s*(\d+)', res.stdout)
                rend_util = re.search(r'"Renderer Utilization %"\s*=\s*(\d+)', res.stdout)
                in_use_mem = re.search(r'"In use system memory"\s*=\s*(\d+)', res.stdout)
                alloc_mem = re.search(r'"Alloc system memory"\s*=\s*(\d+)', res.stdout)
                
                util = float(dev_util.group(1)) if dev_util else (float(rend_util.group(1)) if rend_util else 0.0)
                used_mb = round(int(in_use_mem.group(1)) / 1024 / 1024, 1) if in_use_mem else 0.0
                total_mb = round(int(alloc_mem.group(1)) / 1024 / 1024, 1) if alloc_mem else 0.0
                
                return {
                    "success": True,
                    "available": True,
                    "gpu_percent": util,
                    "memory_used_mb": used_mb,
                    "memory_total_mb": total_mb,
                    "device": "Apple Integrated GPU"
                }
        except Exception:
            pass

    # 2. NVIDIA GPUs via nvidia-smi (Linux / Windows)
    try:
        res = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=1.5
        )
        if res.returncode == 0 and res.stdout.strip():
            parts = [p.strip() for p in res.stdout.strip().splitlines()[0].split(",")]
            if len(parts) >= 4:
                return {
                    "success": True,
                    "available": True,
                    "gpu_percent": float(parts[1]),
                    "memory_used_mb": float(parts[2]),
                    "memory_total_mb": float(parts[3]),
                    "device": parts[0]
                }
    except Exception:
        pass

    return {
        "success": True,
        "available": False,
        "gpu_percent": 0.0,
        "device": "Unknown / None",
        "message": "No compatible GPU telemetry detected"
    }


def format_bytes(bytes_count: float) -> str:
    """Format bytes count into human readable string (B, KB, MB, GB)."""
    if bytes_count >= 1024 ** 3:
        return f"{bytes_count / (1024 ** 3):.2f} GB"
    elif bytes_count >= 1024 ** 2:
        return f"{bytes_count / (1024 ** 2):.1f} MB"
    elif bytes_count >= 1024:
        return f"{bytes_count / 1024:.1f} KB"
    else:
        return f"{bytes_count:.0f} B"


def format_speed(bytes_per_sec: float) -> str:
    """Format speed into human readable rate string (B/s, KB/s, MB/s)."""
    if bytes_per_sec >= 1024 ** 2:
        return f"{bytes_per_sec / (1024 ** 2):.1f} MB/s"
    elif bytes_per_sec >= 1024:
        return f"{bytes_per_sec / 1024:.1f} KB/s"
    else:
        return f"{bytes_per_sec:.0f} B/s"


def get_network_bandwidth(interval: float = 0.2) -> Dict[str, Any]:
    """Get current network bandwidth transfer rates (upload/download speed) and totals."""
    try:
        n1 = psutil.net_io_counters()
        t1 = time.time()
        time.sleep(interval)
        n2 = psutil.net_io_counters()
        t2 = time.time()

        dt = max(0.001, t2 - t1)
        bytes_recv_sec = max(0.0, (n2.bytes_recv - n1.bytes_recv) / dt)
        bytes_sent_sec = max(0.0, (n2.bytes_sent - n1.bytes_sent) / dt)

        return {
            "success": True,
            "download_speed": format_speed(bytes_recv_sec),
            "upload_speed": format_speed(bytes_sent_sec),
            "download_bytes_sec": round(bytes_recv_sec, 1),
            "upload_bytes_sec": round(bytes_sent_sec, 1),
            "total_received": format_bytes(n2.bytes_recv),
            "total_sent": format_bytes(n2.bytes_sent),
            "total_recv_bytes": n2.bytes_recv,
            "total_sent_bytes": n2.bytes_sent,
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }