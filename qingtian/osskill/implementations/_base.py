"""产品相关 Skill 的公共基类。

提供 execute / validate / _call_api 默认实现，消除 4 个 Skill 文件间的代码重复。
子类只需定义 CAPABILITIES dict 和类元数据。
"""

import logging
import re
import urllib.parse

import aiohttp

from osskill.models import Skill

logger = logging.getLogger("osskill.base_product")


def _quote_path_val(val) -> str:
    """P2 (R11): 路径段值编码——safe='' 连 / 也转义，防含特殊字符的 id 跨段/破坏 URL。"""
    return urllib.parse.quote(str(val), safe="")


def _quote_query_val(val) -> str:
    """P2 (R11): query 值编码——&、=、#、空格等特殊字符不保留，防 query 结构被破坏。"""
    return urllib.parse.quote(str(val), safe="")


def _encode_path(path: str, path_params: dict) -> str:
    """P2 (R11): 填充路径占位符并逐值编码，返回可直接拼 base_url 的路径。

    只编码动态值，路径模板里的字面量斜杠不受影响；
    占位符值整体按单个路径段编码（safe=''），含 / 或 ? 的 id 不再破坏 URL。
    """
    return path.format(**{k: _quote_path_val(v) for k, v in path_params.items()})


class BaseProductSkill(Skill):
    """产品相关 Skill 公共基类。

    子类需定义：
      CAPABILITIES — dict[str, dict] 操作映射
      name / display_name / description / category / version — 元数据
      input_schema / output_schema — JSON Schema
    """

    CAPABILITIES: dict = {}

    async def execute(self, params: dict) -> dict:
        action = params.get("action", "")
        caps = self.CAPABILITIES
        if action not in caps:
            return {
                "ok": False,
                "error": f"未知操作: {action}，支持: {list(caps.keys())}",
            }
        cap = caps[action]
        try:
            result = await self._call_api(
                method=cap["method"], path=cap["path"], params=params,
            )
            return {"ok": True, "data": result}
        except Exception as e:
            logger.warning(
                "%s.%s 失败: %s", type(self).__name__, action, str(e)[:200],
            )
            return {"ok": False, "error": str(e)[:500]}

    async def validate(self, params: dict) -> list[str]:
        errors = []
        action = params.get("action", "")
        caps = self.CAPABILITIES
        if action not in caps:
            errors.append(
                f"不支持的 action: {action}，可选: {list(caps.keys())}",
            )
        return errors

    async def _call_api(self, method: str, path: str, params: dict) -> dict:
        config = getattr(self, "_agent_config", {}) or {}
        base_url = config.get("qingtian_url", "http://127.0.0.1:1996")

        # C9 (R11): 填充路径占位符 {id}/{product_id}——显式 _path_params 优先，
        # 其次按占位符名 / "*_id" 兜底取 params 值；缺失则抛清晰错误，
        # 避免 URL 恒带字面量 {id} 而全部 404。
        path_params = dict(params.get("_path_params", {}) or {})
        for ph in re.findall(r"\{(\w+)\}", path):
            if ph in path_params:
                continue
            val = params.get(ph)
            if val is None or str(val) in ("", "None"):
                if ph == "id":
                    id_key = next(
                        (k for k in params if k.endswith("_id") and params[k]), None,
                    )
                    val = params.get(id_key) if id_key else None
                else:
                    val = params.get(f"{ph}_id")
            if val is not None and str(val) not in ("", "None"):
                path_params[ph] = val
        try:
            url = _encode_path(path, path_params)  # P2 (R11): 占位符值逐段 URL 编码
        except KeyError as e:
            raise ValueError(
                f"路径占位符缺参数: {e}，需要 {path}（可传 _path_params 或对应 *_id）",
            ) from e
        url = f"{base_url.rstrip('/')}{url}"

        # C8 (R11): enterprise_id 契约错位——API 端 GET/list/import 从 query 取、
        # create 从 body 取。这里 query 与 body 都带（对不需要的一侧无害）。
        enterprise_id = params.get("enterprise_id", "")
        headers = {
            "X-Enterprise-ID": enterprise_id,
            "Content-Type": "application/json",
        }

        body = {
            k: v
            for k, v in params.items()
            if k not in ("action", "enterprise_id", "_path_params")
        }

        async with aiohttp.ClientSession() as session:
            if method == "GET":
                query = []
                if enterprise_id:
                    query.append(f"enterprise_id={_quote_query_val(enterprise_id)}")  # P2 (R11)
                for k, v in body.items():
                    if v is not None and v != "" and not isinstance(v, (dict, list)):
                        query.append(f"{k}={_quote_query_val(v)}")  # P2 (R11): query 值编码
                full_url = f"{url}?{'&'.join(query)}" if query else url
                async with session.get(full_url, headers=headers) as resp:
                    return await self._handle_response(resp, full_url)
            elif method == "DELETE":
                async with session.delete(url, headers=headers) as resp:
                    return await self._handle_response(resp, url)
            else:
                # POST/PUT：create 从 body 取 enterprise_id
                if enterprise_id and "enterprise_id" not in body:
                    body["enterprise_id"] = enterprise_id
                async with session.request(
                    method, url, json=body, headers=headers,
                ) as resp:
                    return await self._handle_response(resp, url)

    @staticmethod
    async def _handle_response(resp, url: str) -> dict:
        """C8 (R11): 非 2xx 不再吞错返回 ok:True——抛带状态码的异常。"""
        try:
            data = await resp.json(content_type=None)
        except Exception:
            data = {"detail": (await resp.text())[:500]}
        if resp.status >= 400:
            detail = data.get("detail", data) if isinstance(data, dict) else data
            raise RuntimeError(f"API {resp.status} {url}: {detail}")
        return data
