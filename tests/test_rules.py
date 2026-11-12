"""Automation rules: parsing, validation, persistence and the evaluation state machine."""

from types import SimpleNamespace as NS

import pytest
from conftest import ScriptedLLM, text_reply

from sentinel.rules import Rule, RuleEngine, RuleError, RuleStore, parse_rule, parse_rule_locally
from sentinel.rules.engine import NEVER_TARGET

# --------------------------------------------------------------------------- #
# Local parser
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "scope", "metric", "op", "threshold", "duration", "name", "actions"),
    [
        ("if any process uses over 90% CPU for 5 minutes, pause it and notify me",
         "process", "cpu", ">", 90, 300, None, ["pause_process", "notify"]),
        ("rule: if memory is above 85% for 2 min, let me know",
         "system", "memory", ">", 85, 120, None, ["notify"]),
        ("when chrome uses more than 30% memory, lower its priority",
         "process", "memory", ">", 30, 0, "chrome", ["lower_priority"]),
        ("if disk is above 90%, alert me", "system", "disk", ">", 90, 0, None, ["notify"]),
        ("whenever cpu drops below 5% for 10 minutes notify me",
         "system", "cpu", "<", 5, 600, None, ["notify"]),
        ("if a process uses 300% cpu for 30 seconds, pause it",
         "process", "cpu", ">", 300, 30, None, ["pause_process"]),
        ("if gpu is over 95% for 1 hour", "system", "gpu", ">", 95, 3600, None, ["notify"]),
    ],
)
def test_local_parser(text, scope, metric, op, threshold, duration, name, actions):
    rule = parse_rule_locally(text)
    c = rule.condition
    assert (c.scope, c.metric, c.op, c.threshold, c.duration_seconds, c.process_name) == (
        scope, metric, op, threshold, duration, name)
    assert [a.type for a in rule.actions] == actions


def test_local_parser_returns_none_when_unsure():
    assert parse_rule_locally("notify me when the laptop gets slow") is None


@pytest.mark.parametrize("text", [
    "if spotify uses over 50% cpu then kill it",
    "if any process uses 90% memory, terminate it",
    "if disk is above 95%, delete my downloads",
])
def test_destructive_rules_are_refused(text):
    with pytest.raises(RuleError, match="can't kill, delete or close"):
        parse_rule_locally(text)


def test_process_action_needs_a_process_metric():
    with pytest.raises(RuleError, match="cpu or memory"):
        parse_rule_locally("if gpu is over 95% pause it")


def test_describe_is_readable():
    rule = parse_rule_locally("if any process uses over 90% CPU for 5 minutes, pause it and notify me")
    assert rule.describe() == "When any process uses above 90% CPU for 5 min: pause it, notify you"


# --------------------------------------------------------------------------- #
# Model validation (what an LLM could hand us)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("condition", "actions"),
    [
        ({"scope": "system", "metric": "cpu", "threshold": 150}, [{"type": "notify"}]),
        ({"scope": "process", "metric": "disk", "threshold": 50}, [{"type": "notify"}]),
        ({"scope": "process", "metric": "cpu", "op": "<", "threshold": 5}, [{"type": "notify"}]),
        ({"scope": "system", "metric": "cpu", "threshold": 90}, [{"type": "kill_process"}]),
        ({"scope": "system", "metric": "cpu", "threshold": 90}, []),
        ({"scope": "system", "metric": "cpu", "threshold": 90, "process_name": "x"}, [{"type": "notify"}]),
    ],
)
def test_invalid_rules_are_rejected(condition, actions):
    with pytest.raises(ValueError):
        Rule(text="t", condition=condition, actions=actions)


def test_duplicate_actions_are_collapsed():
    r = Rule(text="t", condition={"scope": "system", "metric": "cpu", "threshold": 90},
             actions=[{"type": "notify"}, {"type": "notify"}])
    assert [a.type for a in r.actions] == ["notify"]


# --------------------------------------------------------------------------- #
# LLM parser
# --------------------------------------------------------------------------- #


def test_llm_parses_what_local_parser_cannot():
    llm = ScriptedLLM([text_reply(
        '{"condition": {"scope": "system", "metric": "memory", "op": ">", "threshold": 80, '
        '"duration_seconds": 60}, "actions": [{"type": "notify"}]}'
    )])
    rule = parse_rule("tell me when my mac is running out of memory for a minute", llm)
    assert rule.condition.metric == "memory" and rule.condition.duration_seconds == 60
    assert llm.calls[0]["use_tools"] is False


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        ('{"error": "destructive"}', "can't kill"),
        ('{"error": "not_a_rule"}', "doesn't look like a monitoring rule"),
        ("sorry, no idea", "couldn't understand"),
        ('{"condition": {"scope": "system", "metric": "cpu", "threshold": 90}, '
         '"actions": [{"type": "kill_process"}]}', "couldn't turn that into a valid rule"),
    ],
)
def test_llm_output_is_validated(reply, message):
    with pytest.raises(RuleError, match=message):
        parse_rule("make my computer behave", ScriptedLLM([text_reply(reply)]))


def test_llm_unavailable_gives_helpful_error():
    with pytest.raises(RuleError, match="couldn't reach an LLM"):
        parse_rule("make my computer behave", ScriptedLLM([]))


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #


def _rule(text="if cpu is above 90%, notify me"):
    return parse_rule_locally(text)


def test_store_persists_and_reloads(tmp_path):
    path = tmp_path / "rules.json"
    store = RuleStore(path)
    r = store.add(_rule())
    store.set_enabled(r.id, False)
    reloaded = RuleStore(path)
    assert [x.id for x in reloaded.all()] == [r.id]
    assert reloaded.all()[0].enabled is False and reloaded.enabled() == []


def test_store_lookup_by_position_or_id_and_remove(tmp_path):
    store = RuleStore(tmp_path / "rules.json")
    a, b = store.add(_rule()), store.add(_rule("if disk is above 90%, alert me"))
    assert store.get(2).id == b.id and store.get(a.id).id == a.id and store.get("#1").id == a.id
    assert store.remove(a.id) and not store.remove(a.id)
    assert [r.id for r in store.all()] == [b.id]


def test_store_skips_corrupt_entries(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text('[{"nonsense": true}, ' + _rule().model_dump_json() + "]")
    assert len(RuleStore(path).all()) == 1
    path.write_text("not json")
    assert RuleStore(path).all() == []


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def metrics(cpu=10.0, mem=40.0, disk=50.0, gpu=None, procs=()):
    procs = [{"pid": pid, "name": name, "cpu_percent": c, "memory_percent": m} for pid, name, c, m in procs]
    return NS(cpu_percent=cpu, memory_percent=mem, disk_percent=disk, gpu_percent=gpu,
              top_processes=procs, top_memory_processes=sorted(procs, key=lambda p: -p["memory_percent"]))


@pytest.fixture
def engine(tmp_path):
    clock = Clock()
    return RuleEngine(RuleStore(tmp_path / "rules.json"), clock=clock), clock


def test_fires_only_after_sustained_duration_then_respects_cooldown(engine):
    eng, clock = engine
    eng.store.add(_rule("if any process uses over 90% cpu for 1 minute, pause it"))
    hot = metrics(procs=[(4242, "miner", 180.0, 2.0)])

    assert eng.evaluate(hot) == []          # t=0: condition starts
    clock.t += 30
    assert eng.evaluate(hot) == []          # 30s: not long enough
    clock.t += 31
    (f,) = eng.evaluate(hot)                # 61s: fires
    assert (f.target_pid, f.target_name, f.value) == (4242, "miner", 180.0)
    clock.t += 60
    assert eng.evaluate(hot) == []          # cooldown (300s default)
    clock.t += 300
    assert len(eng.evaluate(hot)) == 1      # cooldown over, still hot


def test_condition_dropping_resets_the_timer(engine):
    eng, clock = engine
    eng.store.add(_rule("if cpu is above 80% for 1 minute, notify me"))
    eng.evaluate(metrics(cpu=95))
    clock.t += 50
    eng.evaluate(metrics(cpu=20))           # dips: timer resets
    clock.t += 20
    assert eng.evaluate(metrics(cpu=95)) == []
    clock.t += 61
    assert len(eng.evaluate(metrics(cpu=95))) == 1


def test_process_rules_track_each_process_and_name_filter(engine):
    eng, _ = engine
    eng.store.add(_rule("when chrome uses more than 30% memory, lower its priority"))
    m = metrics(procs=[(10, "Google Chrome", 1.0, 35.0), (11, "slack", 1.0, 50.0),
                       (12, "Google Chrome Helper", 1.0, 31.0)])
    assert sorted(f.target_pid for f in eng.evaluate(m)) == [10, 12]


def test_system_rule_targets_top_consumer(engine):
    eng, _ = engine
    eng.store.add(_rule("if memory is above 90%, pause it"))
    (f,) = eng.evaluate(metrics(mem=95, procs=[(20, "a", 1, 10.0), (21, "hog", 1, 60.0)]))
    assert f.target_pid == 21 and "top consumer hog" in f.summary


def test_disabled_rules_and_missing_gpu_are_ignored(engine):
    eng, _ = engine
    r = eng.store.add(_rule("if cpu is above 50%, notify me"))
    eng.store.add(_rule("if gpu is over 50%, notify me"))
    eng.store.set_enabled(r.id, False)
    assert eng.evaluate(metrics(cpu=99, gpu=None)) == []


def test_never_targets_sentinel_or_its_parents(engine):
    eng, _ = engine
    eng.store.add(_rule("if any process uses over 10% cpu, pause it"))
    own = sorted(NEVER_TARGET)
    procs = [(pid, f"self-{pid}", 99.0, 1.0) for pid in own] + [(1, "launchd", 99.0, 1.0)]
    assert eng.evaluate(metrics(procs=procs)) == []
