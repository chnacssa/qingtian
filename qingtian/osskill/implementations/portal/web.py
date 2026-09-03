"""多系统门户 — 统一登录 + 采购/销售文件工作台页面。

职责：
  1. GET /portal                统一门户登录页（按账号类型跳转到对应系统）
  2. GET /v1/procurement/       采购系统文件工作台
  3. GET /v1/sales/             销售系统文件工作台
  4. /v1/portal/files*          汇川文件代理（列表/上传/下载/删除）

鉴权与 bidding 一致：HTTPBearer → 镇岳 token 校验 → (agent_id, role, perms)。
文件操作的 enterprise_id 取自 token 解析出的 agent_id，杜绝客户端伪造归属。
页面模块不跨 skill 导入，汇川/镇岳调用为自含薄封装（httpx），保持 Skill 层独立。

See also: static/index.html（门户登录页）、static/app.html（文件工作台）
"""

import logging
import os

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

logger = logging.getLogger("osskill.portal")

router = APIRouter(tags=["portal"])

_security = HTTPBearer(auto_error=False)

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def _api_base() -> str:
    return os.environ.get("QINGTIAN_API_URL", "http://127.0.0.1:1996")


# P2 (R11): 上传大小上限——portal 层钳制在汇川底层上限之下，防 file.read() 整读进内存 OOM。
# 2026-08-17 汇川底层 200MB→300MB；2026-08-31 波哥指示 300→500（客户投标文件实测超 200MB 常见）。
# 可通过 QINGTIAN_PORTAL_MAX_UPLOAD_MB 覆盖（单位 MB）。
_MAX_UPLOAD_SIZE = int(os.environ.get("QINGTIAN_PORTAL_MAX_UPLOAD_MB", "500")) * 1024 * 1024


# ── 鉴权 ──────────────────────────────────────────────


async def _resolve_identity(credentials: HTTPAuthorizationCredentials | None) -> str:
    """校验 Bearer token，返回 agent_id（即 enterprise_id）。无效返回空串。

    与 bidding `get_current_enterprise` 同款：镇岳 /v1/zhenyue/tokens/verify。
    """
    if not credentials:
        return ""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{_api_base().rstrip('/')}/v1/zhenyue/tokens/verify",
                json={"token": credentials.credentials},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("valid"):
                    return data.get("agent_id", "") or ""
            logger.warning("[trace] portal auth fail: status=%s resp=%s",
                           resp.status_code, resp.text[:120])
    except Exception as e:
        logger.warning("[trace] portal auth error: %s", str(e)[:100])
    return ""


async def _require_agent(
    credentials: HTTPAuthorizationCredentials | None = Depends(_security),
) -> str:
    """鉴权依赖：返回已登录 agent_id，未认证 401。"""
    agent_id = await _resolve_identity(credentials)
    if not agent_id:
        raise HTTPException(401, detail="无效的认证凭证")
    return agent_id


# ── 汇川文件薄封装（自含，不跨 skill 导入） ─────────────


async def _read_upload_bounded(file) -> bytes:
    """P2 (R11): 上传读取钳制——只读 MAX+1 字节即判定，超限 413，防 file.read() 无界整读 OOM。

    返回已校验的空非空、且未超上限的完整内容字节。
    """
    content = await file.read(_MAX_UPLOAD_SIZE + 1)
    if len(content) > _MAX_UPLOAD_SIZE:
        raise HTTPException(
            413,
            f"文件超过 {_MAX_UPLOAD_SIZE // (1024 * 1024)}MB 上限，已拒绝",
        )
    if not content:
        raise HTTPException(400, detail="文件为空")
    return content


async def _huichuan_search(agent_id: str, query: str = "", limit: int = 100) -> list[dict]:
    # 汇川 search 端点 limit ≤ 100（le=100 校验），超限返回 422 → 删除/下载归属校验误判为空（403）
    limit = max(1, min(100, int(limit)))
    url = f"{_api_base().rstrip('/')}/v1/huichuan/files/search"
    params = {"q": query, "limit": limit, "agent_id": agent_id}
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params=params, timeout=10)
            if resp.status_code == 200:
                return resp.json().get("files", [])
            logger.warning("[trace] portal huichuan_search fail agent=%s status=%d", agent_id, resp.status_code)
    except Exception as e:
        logger.warning("[trace] portal huichuan_search error agent=%s err=%s", agent_id, str(e)[:100])
    return []


async def _resolve_bound_bot(agent_id: str) -> str:
    """查当前登录 agent 绑定的投标 bot（agent_channel_bindings 反查，走寰宇 HTTP，不落库）。

    修复（波哥 2026-08-08）：生成标书归属投标 bot（bidding-feishu-2），用户登录 agent 是
    feishu:ou_xxx（通道身份）；剥离通道前缀后按裸 channel_id 调
    /v1/huanyu/agents/identity/resolve 拿绑定 bot，使网页版【我的标书】能看到 bot 名下文件。
    """
    if not agent_id:
        return ""
    bare = agent_id.split(":", 1)[-1] if ":" in agent_id else agent_id
    if not bare:
        return ""
    try:
        url = f"{_api_base().rstrip('/')}/v1/huanyu/agents/identity/resolve"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params={"channel_id": bare}, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                return (data.get("agent_id") or "").strip()
            logger.warning("[trace] portal resolve_bound_bot fail agent=%s status=%d",
                           agent_id, resp.status_code)
    except Exception as e:
        logger.warning("[trace] portal resolve_bound_bot error agent=%s err=%s",
                       agent_id, str(e)[:100])
    return ""


async def _huichuan_upload(agent_id: str, content: bytes, filename: str) -> dict | None:
    if not agent_id:
        return None
    url = f"{_api_base().rstrip('/')}/v1/huichuan/files/upload/{agent_id}"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, files={"file": (filename, content)}, timeout=120)
            if resp.status_code == 200:
                return resp.json()
            logger.warning("[trace] portal huichuan_upload fail agent=%s file=%s status=%d",
                           agent_id, filename, resp.status_code)
    except Exception as e:
        logger.warning("[trace] portal huichuan_upload error agent=%s file=%s err=%s",
                       agent_id, filename, str(e)[:100])
    return None


async def _is_file_owned(agent_id: str, file_id: str) -> bool:
    """P2 (R11): 精确判定 file_id 是否归当前 agent 所有（防越权删除）。

    汇川无按 file_id 取 owner 的端点，owner 信号唯一来自搜索结果的 agent_id 字段
    （= file_registry.metadata.owner_agent）。故不再走「搜索可见性」判归属：
      - 搜索窗口(≤100，服务端硬限)内：比对 agent_id == 当前 agent——共享文件(owner 为他人)
        被精确排除，不会因可见被误判 owned 可删（修 #4）。
      - 窗口外(>100 条)或不可见：portal 侧无法精确读 owner → 保守拒绝（fail-closed，
        修 #3：原 _huichuan_search(limit=200) 被服务端钳制 100，仅覆盖最近 100 个文件）。
    """
    for f in await _huichuan_search(agent_id, limit=100):
        if f.get("file_id") == file_id:
            return f.get("agent_id") == agent_id
    return False


async def _huichuan_delete(agent_id: str, file_id: str) -> bool:
    url = f"{_api_base().rstrip('/')}/v1/huichuan/files/{file_id}"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.delete(url, timeout=10)
            if resp.status_code == 200:
                return True
            logger.warning("[trace] portal huichuan_delete fail agent=%s file=%s status=%d",
                           agent_id, file_id, resp.status_code)
    except Exception as e:
        logger.warning("[trace] portal huichuan_delete error agent=%s file=%s err=%s",
                       agent_id, file_id, str(e)[:100])
    return False


async def _huichuan_download(agent_id: str, file_id: str) -> bytes | None:
    url = f"{_api_base().rstrip('/')}/v1/huichuan/files/{file_id}/download"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params={"agent_id": agent_id}, timeout=60)
            if resp.status_code == 200:
                return resp.content
            logger.warning("[trace] portal huichuan_download fail agent=%s file=%s status=%d",
                           agent_id, file_id, resp.status_code)
    except Exception as e:
        logger.warning("[trace] portal huichuan_download error agent=%s file=%s err=%s",
                       agent_id, file_id, str(e)[:100])
    return None


# ── 页面 ──────────────────────────────────────────────


def _read_static(name: str) -> str:
    path = os.path.join(_STATIC_DIR, name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


@router.get("/portal", response_class=Response)
async def portal_login_page():
    """统一门户登录页（公开，无需认证）。"""
    return Response(content=_read_static("index.html"), media_type="text/html; charset=utf-8")


@router.get("/v1/procurement/", response_class=Response)
async def procurement_page():
    """采购系统文件工作台。页面 JS 按 pathname 识别系统名。"""
    return Response(content=_read_static("app.html"), media_type="text/html; charset=utf-8")


@router.get("/v1/sales/", response_class=Response)
async def sales_page():
    """销售系统文件工作台。"""
    return Response(content=_read_static("app.html"), media_type="text/html; charset=utf-8")


@router.get("/v1/admin/", response_class=Response)
async def admin_page():
    """管理门户页（独立管理后台）。

    页面 HTML 公开（与采购/销售工作台一致），页面 JS 校验 admin 身份，
    数据接口复用投标 /admin/users（服务端 _require_admin 兜底），
    非 admin 或未登录会被页面弹回 /portal。
    """
    return Response(content=_read_static("admin.html"), media_type="text/html; charset=utf-8")


# ── 文件代理 ──────────────────────────────────────────


@router.get("/v1/portal/files")
async def portal_list_files(agent_id: str = Depends(_require_agent)):
    """列出当前账号在汇川已上传的文件。

    归属扩展（波哥 2026-08-08）：登录用户与其投标 bot（agent_channel_bindings 绑定，
    如 feishu:ou_xxx → bidding-feishu-2）名下文件一并返回——用户网页版【我的标书】
    能看到投标 bot 生成的标书。绑定查询走寰宇 identity/resolve。
    """
    files = await _huichuan_search(agent_id)
    bot_agent = await _resolve_bound_bot(agent_id)
    if bot_agent and bot_agent != agent_id:
        bot_files = await _huichuan_search(bot_agent)
        files = files + bot_files
        logger.info("[trace] portal_list_files merge bot files: user=%s bot=%s +%d",
                    agent_id, bot_agent, len(bot_files))
    return {
        "files": [
            {
                "file_id": f["file_id"],
                "file_name": f.get("filename", ""),
                "file_size": f.get("size", 0),
                "created_at": f.get("created_at", ""),
            }
            for f in files
        ]
    }


@router.post("/v1/portal/files/upload")
async def portal_upload_file(
    file: UploadFile = File(...),
    agent_id: str = Depends(_require_agent),
):
    """上传文件到汇川（按当前账号隔离），返回 file_id。"""
    content = await _read_upload_bounded(file)  # P2 (R11): 大小钳制，超限 413，防 OOM
    result = await _huichuan_upload(agent_id, content, file.filename or "unknown")
    if not result:
        raise HTTPException(500, detail="汇川上传失败")
    logger.info("[trace] portal upload: agent=%s file=%s file_id=%s",
                agent_id, file.filename or "unknown", result.get("file_id", "?"))
    return {
        "ok": True,
        "file_id": result.get("file_id", ""),
        "file_name": file.filename or "unknown",
        "size": result.get("size", len(content)),
    }


@router.delete("/v1/portal/files/{file_id}")
async def portal_delete_file(
    file_id: str,
    agent_id: str = Depends(_require_agent),
):
    """删除当前账号在汇川上传的文件。

    汇川删除端点本身不做 agent 归属校验（仅共享文件 token），
    故此处先按当前账号搜索范围内校验归属，防止跨账号删除他人文件。
    """
    owned = await _is_file_owned(agent_id, file_id)  # P2 (R11): 按 owner 精确校验，非搜索可见性
    if not owned:
        logger.warning("[trace] portal delete deny: agent=%s file=%s not owned", agent_id, file_id)
        raise HTTPException(403, detail="无权删除该文件（非本账号文件）")
    ok = await _huichuan_delete(agent_id, file_id)
    if not ok:
        raise HTTPException(500, detail="删除失败，文件不存在或汇川不可达")
    logger.info("[trace] portal delete_file: agent=%s file=%s", agent_id, file_id)
    return {"ok": True, "file_id": file_id}


@router.get("/v1/portal/files/{file_id}/download")
async def portal_download_file(
    file_id: str,
    agent_id: str = Depends(_require_agent),
):
    """下载当前账号在汇川的文件。"""
    content = await _huichuan_download(agent_id, file_id)
    if content is None:
        raise HTTPException(404, detail="文件不存在或汇川不可达")
    filename = file_id
    # 尝试从列表取真实文件名（下载用；搜索窗口服务端钳制 ≤100，取不到则回退 file_id）
    try:
        files = await _huichuan_search(agent_id, limit=100)
        for f in files:
            if f.get("file_id") == file_id:
                filename = f.get("filename") or filename
                break
    except Exception:
        pass
    import urllib.parse
    quoted = urllib.parse.quote(filename)
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"},
    )


# ── 销售资料导入代理（页面 → sales skill，agent_id 取鉴权结果防伪造） ──


# kind → sales skill 导入 action 映射
_KIND_ACTIONS: dict[str, str] = {
    "products": "import_products",
    "knowledge": "import_knowledge",
    "scripts": "import_scripts",
    "customers": "import_customers",
    "competitors": "import_competitors",
}


async def _sales_execute(agent_id: str, action: str, payload: dict) -> dict:
    """调 sales skill execute 端点（内部 loopback，body.agent_id 优先）。

    返回 skill 的原始 dict；HTTP 错误翻译为友好 message 抛 HTTPException。
    """
    url = f"{_api_base().rstrip('/')}/api/v1/skills/sales/execute"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                json={"agent_id": agent_id, "params": {"action": action, "payload": payload}},
                timeout=60,  # sales 子进程首次启动可能等 30s
            )
    except Exception as e:
        logger.warning("[trace] portal sales_execute error agent=%s action=%s err=%s",
                       agent_id, action, str(e)[:120])
        raise HTTPException(503, detail="销售服务不可达，请稍后重试")

    if resp.status_code == 200:
        return resp.json()

    # FastAPI 错误体: {"detail": {"code", "message"}}；翻译友好文案
    msg = "导入失败"
    try:
        detail = resp.json().get("detail") or {}
        if isinstance(detail, dict):
            msg = detail.get("message") or msg
            code = detail.get("code", "")
            if code == "LICENSE_EXPIRED":
                msg = "销售模块订阅/许可已到期，请联系管理员续费"
            elif code in ("LAUNCH_FAILED", "SKILL_NOT_READY", "RUNTIME_NOT_READY"):
                msg = "销售服务暂不可用，请稍后重试"
    except Exception:
        pass
    logger.warning("[trace] portal sales_execute fail agent=%s action=%s status=%d",
                   agent_id, action, resp.status_code)
    raise HTTPException(resp.status_code, detail=msg)


class SalesImportRequest(BaseModel):
    kind: str
    file_id: str
    filename: str = ""


class SalesKnowledgeRequest(BaseModel):
    content: str
    source: str = ""
    tags: list[str] = []


@router.post("/v1/sales/import")
async def sales_import(
    body: SalesImportRequest,
    agent_id: str = Depends(_require_agent),
):
    """销售资料 Excel 导入（文件已上传汇川，按 file_id 拉取解析）。

    kind ∈ products/knowledge/scripts/customers/competitors。
    两步式：前端先上传汇川拿 file_id，再调本端点触发导入。
    """
    action = _KIND_ACTIONS.get(body.kind)
    if not action:
        raise HTTPException(404, detail=f"未知资料类型: {body.kind}")
    if not body.file_id:
        raise HTTPException(400, detail="缺少 file_id")
    result = await _sales_execute(agent_id, action, {
        "file_id": body.file_id,
        "filename": body.filename,
    })
    return {
        "ok": bool(result.get("ok")),
        "total": (result.get("data") or {}).get("total", 0),
        "imported": (result.get("data") or {}).get("imported", 0),
        "errors": (result.get("data") or {}).get("errors", []) or [],
        "names": (result.get("data") or {}).get("names", []) or [],
        "error": result.get("error", "") if not result.get("ok") else "",
    }


@router.post("/v1/sales/knowledge")
async def sales_knowledge_add(
    body: SalesKnowledgeRequest,
    agent_id: str = Depends(_require_agent),
):
    """手动添加销售知识（企业背景 enterprise_profile / 还盘策略 pricing_policy）。"""
    content = body.content.strip()
    if not content:
        raise HTTPException(400, detail="知识内容不能为空")
    tags = [t for t in body.tags if t]
    if not tags:
        raise HTTPException(400, detail="请选择资料类型标签（企业背景/价格策略）")
    result = await _sales_execute(agent_id, "knowledge_add", {
        "content": content,
        "source": body.source,
        "tags": tags,
    })
    if not result.get("ok"):
        raise HTTPException(500, detail=result.get("error", "保存失败"))
    return {"ok": True, "doc_id": (result.get("data") or {}).get("doc_id", "")}


@router.get("/v1/sales/templates/{kind}")
async def sales_template_download(
    kind: str,
    tag: str = "",
    agent_id: str = Depends(_require_agent),
):
    """下载销售资料 Excel 导入模板（kind=knowledge 时可用 ?tag= 预填标签）。"""
    from .import_loader import load_build_template  # 按目录结构调整后的 skills 路径加载
    build_template = load_build_template("sales")
    content = build_template(kind, tag=tag)
    if content is None:
        raise HTTPException(404, detail=f"未知模板类型: {kind}")
    import urllib.parse
    quoted = urllib.parse.quote(f"{kind}_模板.xlsx")
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"},
    )


# ── 采购资料导入代理（页面 → procurement skill，agent_id 取鉴权结果防伪造） ──


# kind → procurement skill 导入 action 映射（2026-08-09，对齐销售五类）
_PROC_KIND_ACTIONS: dict[str, str] = {
    "price_lists": "import_price_list",
    "suppliers": "import_suppliers",
    "knowledge": "import_knowledge",
    "inquiries": "import_inquiries",
    "scripts": "import_scripts",
}


async def _procurement_execute(agent_id: str, action: str, payload: dict) -> dict:
    """调 procurement skill execute 端点（内部 loopback，body.agent_id 优先）。"""
    url = f"{_api_base().rstrip('/')}/api/v1/skills/procurement/execute"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                json={"agent_id": agent_id, "params": {"action": action, "payload": payload}},
                timeout=60,
            )
    except Exception as e:
        logger.warning("[trace] portal procurement_execute error agent=%s action=%s err=%s",
                       agent_id, action, str(e)[:120])
        raise HTTPException(503, detail="采购服务不可达，请稍后重试")

    if resp.status_code == 200:
        return resp.json()

    msg = "导入失败"
    try:
        detail = resp.json().get("detail") or {}
        if isinstance(detail, dict):
            msg = detail.get("message") or msg
            code = detail.get("code", "")
            if code in ("LAUNCH_FAILED", "SKILL_NOT_READY", "RUNTIME_NOT_READY"):
                msg = "采购服务暂不可用，请稍后重试"
    except Exception:
        pass
    logger.warning("[trace] portal procurement_execute fail agent=%s action=%s status=%d",
                   agent_id, action, resp.status_code)
    raise HTTPException(resp.status_code, detail=msg)


class ProcurementImportRequest(BaseModel):
    kind: str
    file_id: str
    filename: str = ""


class ProcurementKnowledgeRequest(BaseModel):
    content: str
    source: str = ""
    tags: list[str] = []


@router.post("/v1/procurement/import")
async def procurement_import(
    body: ProcurementImportRequest,
    agent_id: str = Depends(_require_agent),
):
    """采购资料 Excel 导入（文件已上传汇川，按 file_id 拉取解析）。

    kind ∈ price_lists/suppliers/knowledge/inquiries/scripts。
    两步式：前端先上传汇川拿 file_id，再调本端点触发导入。
    """
    action = _PROC_KIND_ACTIONS.get(body.kind)
    if not action:
        raise HTTPException(404, detail=f"未知资料类型: {body.kind}")
    if not body.file_id:
        raise HTTPException(400, detail="缺少 file_id")
    result = await _procurement_execute(agent_id, action, {
        "file_id": body.file_id,
        "filename": body.filename,
    })
    return {
        "ok": bool(result.get("ok")),
        "total": (result.get("data") or {}).get("total", 0),
        "imported": (result.get("data") or {}).get("imported", 0),
        "errors": (result.get("data") or {}).get("errors", []) or [],
        "names": (result.get("data") or {}).get("names", []) or [],
        "error": result.get("error", "") if not result.get("ok") else "",
    }


@router.post("/v1/procurement/knowledge")
async def procurement_knowledge_add(
    body: ProcurementKnowledgeRequest,
    agent_id: str = Depends(_require_agent),
):
    """手动添加采购知识（公司背景/采购制度，标签强制含 company_profile）。"""
    content = body.content.strip()
    if not content:
        raise HTTPException(400, detail="知识内容不能为空")
    tags = [t for t in body.tags if t]
    if "company_profile" not in tags:
        tags.append("company_profile")
    result = await _procurement_execute(agent_id, "knowledge_add", {
        "content": content,
        "source": body.source,
        "tags": tags,
    })
    if not result.get("ok"):
        raise HTTPException(500, detail=result.get("error", "保存失败"))
    return {"ok": True, "doc_id": (result.get("data") or {}).get("doc_id", "")}


@router.get("/v1/procurement/templates/{kind}")
async def procurement_template_download(
    kind: str,
    tag: str = "",
    agent_id: str = Depends(_require_agent),
):
    """下载采购资料 Excel 导入模板（kind=knowledge 时可用 ?tag= 预填标签）。"""
    from .import_loader import load_build_template  # 按目录结构调整后的 skills 路径加载
    build_template = load_build_template("procurement")
    content = build_template(kind, tag=tag)
    if content is None:
        raise HTTPException(404, detail=f"未知模板类型: {kind}")
    import urllib.parse
    quoted = urllib.parse.quote(f"{kind}_模板.xlsx")
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"},
    )


# ── 通道身份绑定（open_id ↔ agent 名，X 模型落地） ─────────
# 文件 owner 存 OpenClaw agent 名，飞书消息 from.open_id 是通道身份；不归一 → execute 下载 403。
# 绑定存 huanyu agent_channel_bindings，由账号绑定流程动态维护（禁止硬编码 open_id 进仓库）。
# 自助端点：agent_id 一律取自鉴权 token（_require_agent），不信任客户端传身份。


class ChannelBindRequest(BaseModel):
    channel: str
    channel_id: str


def _huanyu_err(resp) -> str:
    try:
        d = resp.json().get("detail")
        if isinstance(d, dict):
            return d.get("message") or ""
        return str(d) if d else ""
    except Exception:
        return resp.text[:100]


async def _huanyu_bindings(agent_id: str) -> list[dict]:
    url = f"{_api_base().rstrip('/')}/v1/huanyu/agents/{agent_id}/bindings"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=10)
            if resp.status_code == 200:
                return (resp.json().get("bindings") or [])
            logger.warning("[trace] portal bindings list fail agent=%s status=%d", agent_id, resp.status_code)
    except Exception as e:
        logger.warning("[trace] portal bindings list error agent=%s err=%s", agent_id, str(e)[:100])
    return []


@router.get("/v1/portal/bindings")
async def portal_list_bindings(_auth_agent: str = Depends(_require_agent)):
    """列出当前登录 agent 的通道绑定（agent_id 取自 token）。"""
    return {"status": "ok", "bindings": await _huanyu_bindings(_auth_agent)}


@router.post("/v1/portal/bindings")
async def portal_bind(
    body: ChannelBindRequest,
    _auth_agent: str = Depends(_require_agent),
):
    """绑定通道身份（open_id）→ 当前登录 agent。"""
    if not body.channel.strip() or not body.channel_id.strip():
        raise HTTPException(400, detail="channel/channel_id 不能为空")
    url = f"{_api_base().rstrip('/')}/v1/huanyu/agents/{_auth_agent}/bindings"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                json={"channel": body.channel, "channel_id": body.channel_id},
                timeout=10,
            )
    except Exception as e:
        logger.warning("[trace] portal bind error agent=%s err=%s", _auth_agent, str(e)[:100])
        raise HTTPException(503, detail="寰宇服务不可达，请稍后重试")
    if resp.status_code in (200, 201):
        return resp.json()
    raise HTTPException(resp.status_code, detail=f"绑定失败: {_huanyu_err(resp)}")


@router.delete("/v1/portal/bindings/{channel}/{channel_id}")
async def portal_unbind(
    channel: str,
    channel_id: str,
    _auth_agent: str = Depends(_require_agent),
):
    """解绑当前登录 agent 的通道绑定。"""
    import urllib.parse
    url = (f"{_api_base().rstrip('/')}/v1/huanyu/agents/{_auth_agent}/bindings/"
           f"{urllib.parse.quote(channel, safe='')}/{urllib.parse.quote(channel_id, safe='')}")
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.delete(url, timeout=10)
    except Exception as e:
        logger.warning("[trace] portal unbind error agent=%s err=%s", _auth_agent, str(e)[:100])
        raise HTTPException(503, detail="寰宇服务不可达，请稍后重试")
    if resp.status_code == 200:
        return resp.json()
    raise HTTPException(resp.status_code, detail=f"解绑失败: {_huanyu_err(resp)}")
