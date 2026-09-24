# Circuit Breaker - Rate limiting for destructive actions

import time
from collections import deque
from typing import Deque


class CircuitBreaker:
    """Tracks destructive actions and enforces rate limits."""
    
    def __init__(self, max_actions: int = 5, window_seconds: int = 300):
        self.max_actions = max_actions
        self.window_seconds = window_seconds
        self.actions: Deque[float] = deque()
        self.triggered = False
    
    def record_action(self):
        """Record a destructive action."""
        now = time.time()
        self.actions.append(now)
        self._cleanup_old_actions(now)
    
    def _cleanup_old_actions(self, now: float):
        """Remove actions outside the time window."""
        while self.actions and (now - self.actions[0]) > self.window_seconds:
            self.actions.popleft()
    
    def is_triggered(self) -> bool:
        """Check if circuit breaker is triggered."""
        self._cleanup_old_actions(time.time())
        return len(self.actions) >= self.max_actions
    
    def get_remaining_actions(self) -> int:
        """Get number of actions remaining before trigger."""
        self._cleanup_old_actions(time.time())
        return max(0, self.max_actions - len(self.actions))
    
    def reset(self):
        """Reset the circuit breaker."""
        self.actions.clear()
        self.triggered = False