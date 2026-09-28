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


def get_battery_and_thermal() -> Dict[str, Any]:
    """Get battery percentage, charging state, cycle count, and thermal throttling state."""
    batt = None
    try:
        batt = psutil.sensors_battery()
    except Exception:
        pass

    has_battery = batt is not None
    percent = round(batt.percent, 1) if batt else None
    power_plugged = batt.power_plugged if batt else True

    status = "No Battery (Desktop/Server)"
    time_remaining_str = None
    cycle_count = None
    condition = "Normal"
    thermal_state = "Nominal (Cool / No throttling)"
    thermal_throttling = False

    if has_battery:
        if power_plugged:
            status = "AC Connected (Full)" if percent and percent >= 98 else "Charging (AC Connected)"
        else:
            status = "Discharging (Battery Power)"

        if batt and batt.secsleft and batt.secsleft > 0 and batt.secsleft != psutil.POWER_TIME_UNLIMITED:
            hrs = int(batt.secsleft // 3600)
            mins = int((batt.secsleft % 3600) // 60)
            time_remaining_str = f"{hrs}h {mins:02d}m remaining"

    # macOS specific enrichment via pmset & ioreg
    if platform.system() == "Darwin":
        try:
            pm = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=1.0)
            if pm.returncode == 0 and pm.stdout:
                out = pm.stdout.lower()
                if "discharging" in out:
                    status = "Discharging (Battery Power)"
                elif "charging" in out:
                    status = "Charging (AC Connected)"
                elif "charged" in out or "finishing charge" in out:
                    status = "Fully Charged (AC Connected)"

                rem_match = re.search(r'(\d+:\d+)\s+remaining', pm.stdout)
                if rem_match:
                    parts = rem_match.group(1).split(":")
                    time_remaining_str = f"{int(parts[0])}h {int(parts[1]):02d}m remaining"

            ior = subprocess.run(["ioreg", "-rn", "AppleSmartBattery"], capture_output=True, text=True, timeout=1.0)
            if ior.returncode == 0 and ior.stdout:
                c_match = re.search(r'"CycleCount"\s*=\s*(\d+)', ior.stdout)
                if c_match:
                    cycle_count = int(c_match.group(1))
                cond_match = re.search(r'"BatteryHealthMetric"\s*=\s*"?([^",\n]+)', ior.stdout)
                if cond_match:
                    condition = cond_match.group(1).strip()

            th = subprocess.run(["pmset", "-g", "therm"], capture_output=True, text=True, timeout=1.0)
            if th.returncode == 0 and th.stdout:
                th_out = th.stdout.lower()
                if "warning" in th_out and "no thermal warning" not in th_out:
                    thermal_state = "Warning: Elevated thermal pressure"
                    thermal_throttling = True
                elif "critical" in th_out:
                    thermal_state = "Critical: Severe thermal throttling active"
                    thermal_throttling = True
                else:
                    thermal_state = "Nominal (Cool / No throttling)"
                    thermal_throttling = False
        except Exception:
            pass
    elif platform.system() == "Linux":
        try:
            temps = psutil.sensors_temperatures()
            if temps:
                max_temp = max(t.current for tlist in temps.values() for t in tlist if hasattr(t, 'current'))
                if max_temp > 85.0:
                    thermal_state = f"High: {max_temp:.1f}°C (Potential throttling)"
                    thermal_throttling = True
                else:
                    thermal_state = f"Nominal: {max_temp:.1f}°C"
        except Exception:
            pass

    return {
        "success": True,
        "has_battery": has_battery,
        "percent": percent,
        "power_plugged": power_plugged,
        "status": status,
        "time_remaining": time_remaining_str,
        "cycle_count": cycle_count,
        "condition": condition,
        "thermal_state": thermal_state,
        "thermal_throttling": thermal_throttling
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


def analyze_disk_hogs(custom_paths: Optional[List[str]] = None) -> Dict[str, Any]:
    """Scan and analyze developer caches, build artifacts, and bloated directories."""
    default_candidates = [
        ("Xcode DerivedData", os.path.expanduser("~/Library/Developer/Xcode/DerivedData"), "Intermediate Xcode build objects"),
        ("Xcode DeviceSupport", os.path.expanduser("~/Library/Developer/Xcode/iOS DeviceSupport"), "Cached iOS symbol files"),
        ("Homebrew Cache", os.path.expanduser("~/Library/Caches/Homebrew"), "Downloaded Homebrew package bottles"),
        ("Pip Package Cache", os.path.expanduser("~/Library/Caches/pip"), "Cached Python package wheels & tarballs"),
        ("Linux Pip Cache", os.path.expanduser("~/.cache/pip"), "Cached Python packages"),
        ("NPM Cache", os.path.expanduser("~/.npm"), "Cached Node package manager archives"),
        ("Yarn Cache", os.path.expanduser("~/Library/Caches/Yarn"), "Cached Yarn dependencies"),
        ("User Trash", os.path.expanduser("~/.Trash"), "Deleted files pending trash removal"),
        ("Linux Trash", os.path.expanduser("~/.local/share/Trash"), "Deleted files pending trash removal"),
    ]

    scan_list = []
    if custom_paths:
        for p in custom_paths:
            if isinstance(p, dict):
                p_path = p.get("path", "")
                p_name = p.get("name", os.path.basename(p_path) or "Custom Path")
                p_desc = p.get("category", p.get("description", "User specified target"))
                exp = os.path.expanduser(p_path)
                scan_list.append((p_name, exp, p_desc))
            else:
                exp = os.path.expanduser(str(p))
                scan_list.append((os.path.basename(exp) or "Custom Path", exp, "User specified target"))
    else:
        scan_list = default_candidates

    targets = []
    total_bytes = 0
    seen_paths = set()
    min_size = 0 if custom_paths else 10 * 1024 * 1024

    for name, p, desc in scan_list:
        if p in seen_paths or not os.path.exists(p):
            continue
        seen_paths.add(p)

        folder_size = 0
        file_count = 0
        try:
            for root, dirs, files in os.walk(p):
                for f in files:
                    try:
                        fp = os.path.join(root, f)
                        if not os.path.islink(fp):
                            folder_size += os.path.getsize(fp)
                            file_count += 1
                    except (OSError, PermissionError):
                        continue
        except Exception:
            continue

        if folder_size > min_size:
            total_bytes += folder_size
            targets.append({
                "name": name,
                "path": p,
                "size_mb": round(folder_size / (1024 ** 2), 1),
                "size_str": format_bytes(folder_size),
                "file_count": file_count,
                "description": desc,
                "safe_to_clean": True
            })

    targets.sort(key=lambda x: x["size_mb"], reverse=True)
    return {
        "success": True,
        "total_reclaimable_bytes": total_bytes,
        "total_reclaimable_str": format_bytes(total_bytes),
        "total_targets_found": len(targets),
        "targets": targets
    }