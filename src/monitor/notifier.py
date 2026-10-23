# System Notification Dispatcher - Native OS Alerts & Toast Notifications

import os
import platform
import subprocess
import threading
import time
import logging
from typing import Optional, Dict

logger = logging.getLogger(__name__)


def _escape_applescript_str(text: str) -> str:
    """Escape special characters for safe inclusion in an AppleScript double-quoted string."""
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _dispatch_macos_notification(title: str, message: str, subtitle: str = "", sound: bool = True) -> None:
    """Send a native macOS notification banner via osascript."""
    try:
        title_esc = _escape_applescript_str(title)
        msg_esc = _escape_applescript_str(message)
        sub_esc = _escape_applescript_str(subtitle)

        script = f'display notification "{msg_esc}" with title "{title_esc}"'
        if sub_esc:
            script += f' subtitle "{sub_esc}"'
        if sound:
            script += ' sound name "Ping"'

        subprocess.run(
            ["osascript", "-e", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2.0
        )
    except Exception as e:
        logger.debug("Failed to dispatch macOS notification: %s", e)


def _dispatch_linux_notification(title: str, message: str, sound: bool = True) -> None:
    """Send a native Linux desktop notification via notify-send."""
    try:
        cmd = ["notify-send", "-a", "Sentinel", "-u", "critical", title, message]
        subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2.0
        )
    except Exception as e:
        logger.debug("Failed to dispatch Linux notification: %s", e)


def send_desktop_notification(
    title: str, 
    message: str, 
    subtitle: str = "", 
    sound: Optional[bool] = None
) -> None:
    """
    Dispatch a native desktop notification asynchronously without blocking the caller.
    Supported on macOS (via osascript) and Linux (via notify-send).
    """
    # Check if desktop alerts are globally enabled in environment
    alerts_enabled = os.getenv("SENTINEL_DESKTOP_ALERTS", "true").lower() in ("true", "1", "yes")
    if not alerts_enabled:
        return

    if sound is None:
        sound = os.getenv("SENTINEL_ALERT_SOUND", "true").lower() in ("true", "1", "yes")

    system = platform.system().lower()

    def _worker():
        if system == "darwin":
            _dispatch_macos_notification(title, message, subtitle, sound=sound)
        elif system == "linux":
            full_msg = f"{subtitle}\n{message}" if subtitle else message
            _dispatch_linux_notification(title, full_msg, sound=sound)

    # Launch daemon thread so calling telemetry loop is never blocked
    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()


class NotificationCooldownManager:
    """
    Tracks and throttles notifications to avoid alerting spam.
    Maintains per-category cooldowns and detects when offending PIDs change.
    """

    def __init__(self, default_cooldown: float = 60.0):
        self.cooldown = default_cooldown
        # Map: category -> (last_timestamp, last_pid)
        self._history: Dict[str, tuple[float, Optional[int]]] = {}
        self._lock = threading.Lock()

    def should_notify(self, category: str, pid: Optional[int] = None) -> bool:
        """
        Determine if an alert for a given category should be dispatched.
        Returns True if cooldown expired OR if the offending process PID changed.
        """
        now = time.time()
        with self._lock:
            if category not in self._history:
                return True

            last_time, last_pid = self._history[category]

            # If a new, different process is causing the anomaly, notify immediately
            if pid is not None and last_pid is not None and pid != last_pid:
                return True

            return (now - last_time) >= self.cooldown

    def record_notification(self, category: str, pid: Optional[int] = None) -> None:
        """Record the timestamp and offending PID of a dispatched alert."""
        with self._lock:
            self._history[category] = (time.time(), pid)

    def reset(self) -> None:
        """Clear all cooldown history."""
        with self._lock:
            self._history.clear()
