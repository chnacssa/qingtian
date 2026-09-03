"""
吸星 — 自知引擎 (Self-Assessment Engine)

检查ACSSA 智能体操作系统在 9 大能力维度的实际实现程度。
每个维度有多个 check item，每个 check item 通过检查代码库
（文件存在、函数存在、AST 解析、模式计数）来确定功能完成度。
"""

import os
import ast
import importlib

# Each check item: (feature_name, weight, checker_function)
# checker_function returns float 0.0-1.0 (completion level)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _file_exists(rel_path: str) -> float:
    return 1.0 if os.path.exists(os.path.join(BASE_DIR, rel_path)) else 0.0


def _func_exists(rel_path: str, func_name: str) -> float:
    """Check if a function exists in a Python file by parsing AST."""
    try:
        fp = os.path.join(BASE_DIR, rel_path)
        if not os.path.exists(fp):
            return 0.0
        with open(fp, encoding='utf-8') as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
                return 1.0
        return 0.0
    except Exception:
        return 0.0


def _import_exists(module_path: str) -> float:
    """Check if a Python module can be imported."""
    try:
        importlib.import_module(module_path)
        return 1.0
    except ImportError:
        return 0.0


def _class_exists(rel_path: str, class_name: str) -> float:
    """Check if a class exists in a Python file."""
    try:
        fp = os.path.join(BASE_DIR, rel_path)
        if not os.path.exists(fp):
            return 0.0
        with open(fp, encoding='utf-8') as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                return 1.0
        return 0.0
    except Exception:
        return 0.0


def _has_ua_pool_min(count: int) -> float:
    """Check UA pool size in crawler.py."""
    try:
        import re
        fp = os.path.join(BASE_DIR, 'xixing/crawler.py')
        with open(fp, encoding='utf-8') as f:
            content = f.read()
        # Count UA strings in UA_POOL
        matches = re.findall(r'"Mozilla/[^"]+"', content)
        return min(len(matches) / count, 1.0)
    except Exception:
        return 0.0


def _grep_count(rel_path: str, pattern: str, threshold: int) -> float:
    """Count pattern occurrences in file, return min(count/threshold, 1.0)."""
    try:
        import re
        fp = os.path.join(BASE_DIR, rel_path)
        with open(fp, encoding='utf-8') as f:
            content = f.read()
        count = len(re.findall(pattern, content))
        return min(count / threshold, 1.0)
    except Exception:
        return 0.0


# ── 各维度检查项 ──────────────────────────────────────────

_CHECKS: dict[str, list[tuple[str, float, callable]]] = {
    "crawl_fetch": [
        ("curl_cffi TLS impersonation", 0.2, lambda: _grep_count('xixing/crawler.py', 'curl_cffi', 1)),
        ("Playwright JS rendering", 0.2, lambda: _func_exists('xixing/crawler.py', '_playwright_fetch')),
        ("UA pool >= 15", 0.1, lambda: _has_ua_pool_min(15)),
        ("Proxy support", 0.15, lambda: _func_exists('xixing/crawler.py', '_pick_proxy')),
        ("LLM quality judge", 0.15, lambda: _func_exists('xixing/crawler.py', '_llm_quality_judge')),
        ("Structure fingerprint", 0.1, lambda: _func_exists('xixing/crawler.py', '_structure_fingerprint')),
        ("Failure classification (7 types)", 0.1, lambda: _grep_count('xixing/crawler.py', r'FailType\.', 5)),
    ],
    "longterm_memory": [
        ("pgvector extension", 0.15, lambda: _grep_count('yongheng/database.py', 'pgvector', 1)),
        ("Hybrid FTS+vector search", 0.2, lambda: _func_exists('yongheng/memory_service.py', '_rrf_fusion')),
        ("Agentic LLM search", 0.1, lambda: _grep_count('yongheng/memory_service.py', 'agentic', 1)),
        ("Time decay", 0.15, lambda: _func_exists('yongheng/memory_service.py', '_apply_time_decay')),
        ("Audience-weighted distribution", 0.1, lambda: _func_exists('yongheng/memory_service.py', '_audience_weight')),
        ("Memory export/transfer", 0.1, lambda: _func_exists('yongheng/memory_service.py', 'export_memories')),
        ("Async embedding queue", 0.1, lambda: _file_exists('yongheng/embedding.py')),
        ("High-value detection", 0.1, lambda: _file_exists('yongheng/high_value.py')),
    ],
    "memory_distill": [
        ("Distiller pipeline", 0.25, lambda: _func_exists('xixing/distiller.py', 'run_distillation')),
        ("Content-based clustering", 0.2, lambda: _func_exists('xixing/distiller.py', '_cluster_by_content')),
        ("LLM structured extraction", 0.2, lambda: _grep_count('xixing/distiller.py', 'deepseek', 1)),
        ("Dreem Gate consolidation", 0.2, lambda: _func_exists('yongheng/dreem_gate.py', 'consolidate')),
        ("Target audience labeling", 0.15, lambda: _grep_count('xixing/distiller.py', 'target_audience', 1)),
    ],
    "session_memory": [
        ("Session start endpoint", 0.25, lambda: _func_exists('yongheng/api.py', 'session_start')),
        ("Session end endpoint", 0.2, lambda: _func_exists('yongheng/api.py', 'session_end')),
        ("Session recover endpoint", 0.2, lambda: _func_exists('yongheng/api.py', 'recover_session')),
        ("Trajectory service", 0.15, lambda: _file_exists('yongheng/trajectory_service.py')),
        ("Hook ingest endpoint", 0.1, lambda: _func_exists('yongheng/api.py', 'hooks_ingest')),
        ("Rate limiting", 0.1, lambda: _grep_count('yongheng/api.py', 'RateLimiter', 1)),
    ],
    "experience_pack": [
        ("Pitfall capture", 0.3, lambda: _func_exists('xixing/xizhenji.py', 'capture')),
        ("LLM root cause analysis", 0.25, lambda: _func_exists('xixing/xizhenji.py', '_llm_analyze')),
        ("Audit log detection", 0.2, lambda: _func_exists('xixing/xizhenji.py', 'detect_from_audit_log')),
        ("Auto-injection to yongheng", 0.15, lambda: _grep_count('xixing/scheduler.py', 'xizhenji', 2)),
        ("Agent report-pitfall endpoint", 0.1, lambda: _grep_count('xixing/api.py', 'report.pitfall', 1)),
    ],
    "multi_agent": [
        ("Redis Pub/Sub engine", 0.2, lambda: _func_exists('huanyu/peers.py', 'get_engine')),
        ("Agent messaging", 0.15, lambda: _file_exists('huanyu/messaging.py')),
        ("AIN resolution", 0.15, lambda: _file_exists('huanyu/resolver.py')),
        ("Agent directory", 0.1, lambda: _file_exists('huanyu/directory.py')),
        ("WebSocket notifications", 0.1, lambda: _file_exists('huanyu/api_ws.py')),
        ("Cross-base knowledge sync", 0.1, lambda: _func_exists('xixing/scheduler.py', '_pull_knowledge_job')),
        ("WireGuard secure comms", 0.1, lambda: _grep_count('main.py', 'wireguard', 1)),
        ("QACP protocol", 0.1, lambda: _grep_count('huanyu/api_rest.py', 'signature', 2)),
    ],
    "competitive_scan": [
        ("Scanner pipeline", 0.3, lambda: _func_exists('xixing/scanner.py', 'run_scan')),
        ("Multi-source fetching", 0.2, lambda: _func_exists('xixing/scanner.py', '_fetch_clawhub_skills')),
        ("9-dimension scoring", 0.2, lambda: _grep_count('xixing/scanner.py', 'CAPABILITY_DIMENSIONS', 1)),
        ("Self-assessment capability", 0.15, lambda: _file_exists('xixing/self_assess.py')),
        ("Gap analysis", 0.15, lambda: _func_exists('xixing/scanner.py', '_analyze_gaps')),
    ],
    "memory_orchestration": [
        ("Memory tiering (episodic->high_value->consolidated)", 0.25, lambda: _func_exists('yongheng/high_value.py', 'keyword_scan')),
        ("Content quality filter", 0.2, lambda: _file_exists('yongheng/filter.py')),
        ("Profile learning", 0.15, lambda: _func_exists('yongheng/profile_service.py', 'update_profile')),
        ("Dreem Gate health check", 0.1, lambda: _grep_count('yongheng/dreem_gate.py', 'health_check', 1)),
        ("Protected memory flag", 0.1, lambda: _grep_count('yongheng/memory_service.py', 'protected', 2)),
        ("Learned preference dedup", 0.1, lambda: _grep_count('yongheng/profile_service.py', 'Levenshtein|dedup', 1)),
        ("Cross-ns knowledge bridge", 0.1, lambda: _grep_count('yongheng/memory_service.py', 'include_global', 1)),
    ],
    "self_evolution": [
        ("Self-assessment engine", 0.25, lambda: _file_exists('xixing/self_assess.py')),
        ("Gap analysis", 0.25, lambda: _func_exists('xixing/scanner.py', '_analyze_gaps')),
        ("Proposal generation", 0.25, lambda: _func_exists('xixing/scanner.py', '_generate_proposals')),
        ("Closed feedback loop (scan->propose->implement->verify)", 0.15, lambda: 0.0),
        ("Performance metric tracking", 0.1, lambda: 0.0),
    ],
}


async def self_assess() -> dict:
    """Run self-assessment across all 9 capability dimensions.

    Returns:
        {dimension_id: {"score": float, "name": str, "checks": [{"feature": str, "weight": float, "score": float}, ...]}}
    """
    from .scanner import CAPABILITY_DIMENSIONS

    results = {}
    for dim_id, checks in _CHECKS.items():
        dim_name = CAPABILITY_DIMENSIONS.get(dim_id, {}).get("name", dim_id)
        total_weight = 0.0
        total_score = 0.0
        check_results = []
        for feature_name, weight, checker in checks:
            score = checker()
            total_weight += weight
            total_score += weight * score
            check_results.append({"feature": feature_name, "weight": weight, "score": score})

        final_score = round(total_score / total_weight, 2) if total_weight > 0 else 0.0
        results[dim_id] = {
            "name": dim_name,
            "score": final_score,
            "checks": check_results,
        }

    return results
