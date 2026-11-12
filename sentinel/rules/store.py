"""Persist rules as JSON in Sentinel's data directory."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path

from pydantic import ValidationError

from sentinel.paths import data_dir
from sentinel.rules.models import Rule

logger = logging.getLogger(__name__)


def default_rules_path() -> Path:
    return data_dir() / "rules.json"


class RuleStore:
    """Thread-safe list of rules backed by a JSON file (atomic writes)."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else default_rules_path()
        self._lock = threading.Lock()
        self._rules: list[Rule] = self._load()

    def _load(self) -> list[Rule]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError) as e:
            logger.warning("Could not read rules from %s: %s", self.path, e)
            return []
        rules = []
        for item in raw if isinstance(raw, list) else []:
            try:
                rules.append(Rule.model_validate(item))
            except ValidationError as e:
                logger.warning("Skipping invalid rule in %s: %s", self.path, e)
        return rules

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps([r.model_dump() for r in self._rules], indent=2)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".rules-", suffix=".json")
        with os.fdopen(fd, "w") as f:
            f.write(payload)
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------ #

    def all(self) -> list[Rule]:
        with self._lock:
            return list(self._rules)

    def enabled(self) -> list[Rule]:
        return [r for r in self.all() if r.enabled]

    def add(self, rule: Rule) -> Rule:
        with self._lock:
            self._rules.append(rule)
            self._save()
        return rule

    def get(self, ref: str | int) -> Rule | None:
        """Look up by id or by 1-based position in :meth:`all`."""
        rules = self.all()
        ref = str(ref).strip().lstrip("#")
        if ref.isdigit() and 1 <= int(ref) <= len(rules):
            return rules[int(ref) - 1]
        return next((r for r in rules if r.id == ref), None)

    def remove(self, rule_id: str) -> bool:
        with self._lock:
            before = len(self._rules)
            self._rules = [r for r in self._rules if r.id != rule_id]
            if len(self._rules) == before:
                return False
            self._save()
            return True

    def set_enabled(self, rule_id: str, enabled: bool) -> bool:
        with self._lock:
            for i, r in enumerate(self._rules):
                if r.id == rule_id:
                    self._rules[i] = r.model_copy(update={"enabled": enabled})
                    self._save()
                    return True
            return False
