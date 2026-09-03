"""ACSSA可观测性 — Prometheus 格式指标采集

用法:
    from common.metrics import counter, histogram, get_metrics_text

    counter("api_calls_total", {"method": "GET", "status": "200"})
    histogram("api_duration_ms", 150.0, {"method": "GET"})
    text = get_metrics_text()
"""

import threading
import time
from typing import Dict, List, Tuple

_lock = threading.Lock()
_counters: Dict[str, Dict[Tuple, int]] = {}
_histograms: Dict[str, List[Tuple[float, Tuple]]] = {}
_registry: Dict[str, str] = {}
_label_keys: Dict[str, List[str]] = {}
_EPOCH = time.time()
_gauge_vals: Dict[str, Dict[Tuple, float]] = {}
# histogram 每个序列保留的样本上限（滚动窗口，防止无限增长 OOM）
_MAX_HISTOGRAM_SAMPLES = 2000


def _key(labels: dict) -> Tuple:
    return tuple((k, str(v)) for k, v in sorted(labels.items()))


def _lbl(key: Tuple) -> str:
    if not key:
        return ""
    parts = []
    for k, v in key:
        # Prometheus label 值转义，防文本注入（\ → \\, " → \", 换行 → \n）
        esc = str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        parts.append(f'{k}="{esc}"')
    return "{" + ",".join(parts) + "}"


def _register(name: str, mtype: str, labels: dict):
    if name not in _registry:
        _registry[name] = mtype
        _label_keys[name] = list(labels.keys())


def counter(name: str, labels: dict = None, value: int = 1):
    labels = labels or {}
    _register(name, "counter", labels)
    k = _key(labels)
    with _lock:
        if name not in _counters:
            _counters[name] = {}
        _counters[name][k] = _counters[name].get(k, 0) + value


def histogram(name: str, value: float, labels: dict = None):
    labels = labels or {}
    _register(name, "histogram", labels)
    k = _key(labels)
    with _lock:
        if name not in _histograms:
            _histograms[name] = []
        samples = _histograms[name]
        samples.append((value, k))
        # 滚动窗口：超出上限丢弃最旧样本，防止无限增长
        if len(samples) > _MAX_HISTOGRAM_SAMPLES:
            del samples[:len(samples) - _MAX_HISTOGRAM_SAMPLES]


def gauge(name: str, value: float, labels: dict = None):
    labels = labels or {}
    _register(name, "gauge", labels)
    k = _key(labels)
    with _lock:
        if name not in _gauge_vals:
            _gauge_vals[name] = {}
        _gauge_vals[name][k] = value


class Timer:
    def __init__(self, name: str, labels: dict = None):
        self._name = name
        self._labels = labels or {}
    def __enter__(self):
        self._t0 = time.monotonic()
        return self
    def __exit__(self, *a):
        histogram(self._name, (time.monotonic() - self._t0) * 1000, self._labels)


def get_metrics_text() -> str:
    lines = [f"# HELP qingtian_uptime_seconds Uptime",
             f"# TYPE qingtian_uptime_seconds gauge",
             f"qingtian_uptime_seconds {time.time() - _EPOCH:.1f}"]
    with _lock:
        for name, mtype in sorted(_registry.items()):
            lines.append(f"# HELP {name}")
            lines.append(f"# TYPE {name} {mtype}")
            if mtype in ("counter", "gauge"):
                store = _counters if mtype == "counter" else _gauge_vals
                for k, v in sorted(store.get(name, {}).items()):
                    lines.append(f"{name}{_lbl(k)} {v}")
            elif mtype == "histogram":
                for v, k in _histograms.get(name, []):
                    lines.append(f"{name}{_lbl(k)} {v}")
    return "\n".join(lines) + "\n"


# ── 快捷方法 ──

import re
_PATH_RE = [
    (re.compile(r"/v1/xihe/agents/[^/]+"), "/v1/xihe/agents/{id}"),
    (re.compile(r"/v1/huanyu/agents/[^/]+"), "/v1/huanyu/agents/{id}"),
    (re.compile(r"/v1/huanyu/reminders/\d+"), "/v1/huanyu/reminders/{id}"),
    (re.compile(r"/v1/zhice/tasks/[^/]+"), "/v1/zhice/tasks/{id}"),
    (re.compile(r"/api/v1/skills/detection/[^/]+/[^/]+"), "/api/v1/skills/detection/{action}/{name}"),
    (re.compile(r"/api/v1/skills/admin/[^/]+(/[^/]+)?"), "/api/v1/skills/admin/{action}"),
    (re.compile(r"/api/v1/skills/[^/]+"), "/api/v1/skills/{skill}"),
]


def _norm(path: str) -> str:
    for pat, rep in _PATH_RE:
        path = pat.sub(rep, path)
    return path


def record_api_call(method: str, path: str, status: int, duration_ms: float):
    labels = {"method": method, "path": _norm(path), "status": str(status)}
    counter("qingtian_api_calls_total", labels)
    histogram("qingtian_api_duration_ms", duration_ms, labels)


def record_skill_exec(skill: str, method: str, result: str, duration_ms: float):
    counter("qingtian_skill_exec_total", {"skill": skill, "method": method, "result": result})
    histogram("qingtian_skill_exec_duration_ms", duration_ms, {"skill": skill})


def record_llm_call(model: str, tokens: int, duration_ms: float):
    counter("qingtian_llm_calls_total", {"model": model})
    counter("qingtian_llm_tokens_total", {"model": model}, value=tokens)
    histogram("qingtian_llm_duration_ms", duration_ms, {"model": model})


def record_llm_cost(model: str, cost: float):
    """LLM 成本记账（元，累计 counter）。P2 成本感知。"""
    counter("qingtian_llm_cost_total", {"model": model}, value=cost)


def record_skill_detection(skill: str, result: str, duration_ms: float):
    counter("qingtian_skill_detection_total", {"skill": skill, "result": result})
    histogram("qingtian_skill_detection_duration_ms", duration_ms, {"skill": skill})


def record_skill_install(skill: str, action: str):
    counter("qingtian_skill_install_total", {"skill": skill, "action": action})
