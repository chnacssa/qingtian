"""
吸星 — 自动部署引擎 (Auto-Deploy Engine)

从属服务器检测到能力差距后，自动 git pull + 重启 + 健康检查。
失败时自动回滚到部署前的 commit。
"""

import asyncio
import logging
import subprocess
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# 部署锁：防止并发部署
_deploy_lock = asyncio.Lock()
_last_deploy_time: datetime | None = None
_consecutive_failures: int = 0


def is_working_tree_clean() -> bool:
    """检查 git 工作区是否干净（无未提交的变更）。"""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip() == ""
    except Exception as e:
        logger.error(f"Git status check failed: {e}")
        return False


def _git_current_head() -> str | None:
    """获取当前 HEAD 的 commit hash。"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except Exception as e:
        logger.error(f"Git rev-parse failed: {e}")
        return None


def _git_fetch_and_merge(branch: str = "master") -> tuple[bool, str]:
    """git fetch origin && git merge origin/<branch>。返回 (success, error_msg)。"""
    try:
        # Step 1: fetch
        result = subprocess.run(
            ["git", "fetch", "origin"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            return False, f"git fetch failed: {result.stderr.strip()}"

        # Step 2: check if there are new commits
        result = subprocess.run(
            ["git", "rev-list", f"HEAD..origin/{branch}"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return False, f"git rev-list failed: {result.stderr.strip()}"
        if not result.stdout.strip():
            return False, "no new commits to pull"

        # Step 3: merge
        result = subprocess.run(
            ["git", "merge", f"origin/{branch}"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return False, f"git merge failed: {result.stderr.strip()}"

        return True, ""
    except Exception as e:
        return False, str(e)


def _git_reset_hard(commit: str) -> bool:
    """git reset --hard <commit>。用于回滚。"""
    try:
        result = subprocess.run(
            ["git", "reset", "--hard", commit],
            capture_output=True, text=True, timeout=30,
        )
        return result.returncode == 0
    except Exception as e:
        logger.error(f"Git reset failed: {e}")
        return False


def _restart_service(restart_cmd: str) -> bool:
    """执行重启命令。"""
    try:
        result = subprocess.run(
            restart_cmd.split(),
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            logger.error(f"Restart command failed: {result.stderr.strip()}")
            return False
        return True
    except Exception as e:
        logger.error(f"Restart command failed: {e}")
        return False


async def _health_check_poll(url: str, timeout: int = 30, interval: int = 2) -> bool:
    """轮询健康检查端点，timeout 秒内返回 200 视为健康。"""
    import httpx
    deadline = asyncio.get_event_loop().time() + timeout
    first = True
    while asyncio.get_event_loop().time() < deadline:
        if not first:
            await asyncio.sleep(interval)
        first = False
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    return True
        except Exception:
            pass  # 服务可能还在启动中
    return False


async def auto_deploy(
    mgmt_url: str,
    restart_cmd: str = "systemctl restart qingtian",
    health_url: str = "http://localhost:1996/v1/xixing/health",
    health_timeout: int = 30,
    branch: str = "master",
) -> dict:
    """执行自动部署流水线。

    Returns:
        {status: "deployed"|"skipped"|"rolled_back"|"error",
         pre_head, post_head, error, ...}
    """
    global _last_deploy_time, _consecutive_failures

    async with _deploy_lock:
        result = {
            "status": "error",
            "pre_head": None,
            "post_head": None,
            "error": "",
            "deployed_at": datetime.now(timezone.utc).isoformat(),
        }

        # ① 前置检查
        if not await asyncio.to_thread(is_working_tree_clean):
            result["status"] = "skipped"
            result["error"] = "working tree is not clean"
            logger.warning("Auto-deploy: skipped (dirty working tree)")
            return result

        pre_head = await asyncio.to_thread(_git_current_head)
        if pre_head is None:
            result["status"] = "error"
            result["error"] = "failed to read git HEAD"
            logger.error("Auto-deploy: cannot read git HEAD")
            return result
        result["pre_head"] = pre_head

        # ② git fetch + merge
        ok, err = await asyncio.to_thread(_git_fetch_and_merge, branch)
        if not ok:
            if "no new commits" in err:
                result["status"] = "skipped"
                result["error"] = err
                result["post_head"] = pre_head
                logger.info(f"Auto-deploy: skipped ({err})")
            else:
                result["status"] = "error"
                result["error"] = err
                logger.error(f"Auto-deploy: git failed: {err}")
            return result

        post_head = await asyncio.to_thread(_git_current_head)
        result["post_head"] = post_head

        # ③ 重启服务
        if not await asyncio.to_thread(_restart_service, restart_cmd):
            # 重启命令本身失败，回滚
            logger.error("Auto-deploy: restart command failed, rolling back")
            reset_ok = await asyncio.to_thread(_git_reset_hard, pre_head)
            if not reset_ok:
                logger.critical("Auto-deploy: git reset --hard FAILED during rollback, system may be in undefined state!")
            await asyncio.to_thread(_restart_service, restart_cmd)
            result["status"] = "rolled_back"
            result["error"] = "restart command failed" + (" (git reset also failed)" if not reset_ok else "")
            _consecutive_failures += 1
            _last_deploy_time = datetime.now(timezone.utc)
            return result

        # ④ 健康检查
        healthy = await _health_check_poll(health_url, timeout=health_timeout)
        if healthy:
            result["status"] = "deployed"
            _consecutive_failures = 0
            logger.info(f"Auto-deploy: success {pre_head[:8]} -> {post_head[:8]}")
        else:
            # ⑤ 回滚
            logger.error(f"Auto-deploy: health check failed, rolling back to {pre_head[:8]}")
            reset_ok = await asyncio.to_thread(_git_reset_hard, pre_head)
            if reset_ok:
                await asyncio.to_thread(_restart_service, restart_cmd)
            else:
                logger.critical("Auto-deploy: git reset --hard FAILED during rollback, system may be in undefined state!")
            # 二次健康检查
            rollback_healthy = await _health_check_poll(health_url, timeout=health_timeout) if reset_ok else False
            result["status"] = "rolled_back"
            result["error"] = "health check failed after deploy" + (" (git reset also failed)" if not reset_ok else "")
            result["rollback_healthy"] = rollback_healthy
            _consecutive_failures += 1

        _last_deploy_time = datetime.now(timezone.utc)
        return result


def get_consecutive_failures() -> int:
    return _consecutive_failures


def get_last_deploy_time() -> datetime | None:
    return _last_deploy_time


def reset_failure_count():
    global _consecutive_failures
    _consecutive_failures = 0
