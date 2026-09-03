"""
License 云端校验 API — 部署在管理服
客户端每 24h 回调查询付费状态；管理端更新订阅后主动推送同步
"""
import json
import logging
import urllib.request
from fastapi import APIRouter, Depends, Query, HTTPException
from common.db import get_pool
from zhenyue.auth import verify_admin_token

logger = logging.getLogger("common.license_api")

router = APIRouter(prefix="/v1/license", tags=["license"])

# ── DDL（幂等，启动时自动执行） ────────────────

SUBSCRIPTION_DDL = """
ALTER TABLE billing.subscriptions ADD COLUMN IF NOT EXISTS plan TEXT DEFAULT 'free';
ALTER TABLE billing.subscriptions ADD COLUMN IF NOT EXISTS enterprise_name TEXT DEFAULT '';
ALTER TABLE billing.subscriptions ADD COLUMN IF NOT EXISTS module TEXT DEFAULT 'bidding';
ALTER TABLE billing.subscriptions ADD COLUMN IF NOT EXISTS amount NUMERIC(10,2) DEFAULT 0;
ALTER TABLE billing.subscriptions ADD COLUMN IF NOT EXISTS start_date DATE;
ALTER TABLE billing.subscriptions ADD COLUMN IF NOT EXISTS end_date DATE;
ALTER TABLE billing.subscriptions ADD COLUMN IF NOT EXISTS invoice_needed BOOLEAN DEFAULT false;
ALTER TABLE billing.subscriptions ADD COLUMN IF NOT EXISTS remark TEXT DEFAULT '';
ALTER TABLE billing.subscriptions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE billing.subscriptions DROP CONSTRAINT IF EXISTS uq_sub_ent_module;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_sub_ent_module') THEN
    ALTER TABLE billing.subscriptions ADD CONSTRAINT uq_sub_ent_module UNIQUE (enterprise_id, module);
  END IF;
END $$;
CREATE TABLE IF NOT EXISTS billing.invoices (
    id          SERIAL PRIMARY KEY,
    enterprise_id TEXT NOT NULL,
    invoice_no  TEXT NOT NULL UNIQUE,
    amount      NUMERIC(10,2) NOT NULL DEFAULT 0,
    module      TEXT NOT NULL DEFAULT 'bidding',
    status      TEXT NOT NULL DEFAULT 'pending',
    issued_at   TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


async def ensure_license_schema():
    """幂等执行 License 相关 DDL"""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(SUBSCRIPTION_DDL)
    except Exception:
        pass


@router.get("/validate")
async def validate_license(enterprise_id: str = Query(...),
                           module: str = Query(default="bidding")):
    """云端校验 License — 查 billing.subscriptions 获取此企业在此模块的付费状态"""
    pool = await get_pool()
    # P1 (2026-08-27 review): 列名 expires_at 为旧结构残留（R11 已统一 plan/end_date，
    # 见 main.py C3 注释与 SUBSCRIPTION_DDL）—— 查不存在的列必 500，付费客户云端
    # 校验恒失败降级 free。
    row = await pool.fetchrow(
        "SELECT plan, end_date FROM billing.subscriptions "
        "WHERE enterprise_id = $1 AND module = $2 AND end_date >= NOW() "
        "ORDER BY end_date DESC LIMIT 1",
        enterprise_id, module,
    )
    if row:
        return {
            "enterprise_id": enterprise_id,
            "module": module,
            "plan": row["plan"],
            "expires_at": row["end_date"].isoformat(),
            "valid": row["plan"] in ("pro", "enterprise"),
        }
    return {
        "enterprise_id": enterprise_id,
        "module": module,
        "plan": "free",
        "expires_at": None,
        "valid": False,
    }


# ═══════════════════════════════════════════════════════
# 管理端点 — 财务/运营操作
# ═══════════════════════════════════════════════════════

@router.get("/subscriptions")
async def list_subscriptions(
    q: str = Query(default=""),
    _admin: str = Depends(verify_admin_token),
):
    """查询所有企业订阅（管理面板用）。q 可选：按企业名称/ID 搜索。"""
    pool = await get_pool()
    # P1 (2026-08-27 review): started_at/expires_at/auto_renew 均为旧列名残留
    # （DDL 实际列是 start_date/end_date，无 auto_renew）—— 查询必 500。
    if q:
        rows = await pool.fetch(
            "SELECT s.enterprise_id, a.name as enterprise_name, s.plan, "
            "s.start_date, s.end_date "
            "FROM billing.subscriptions s "
            "LEFT JOIN huanyu.agents a ON a.agent_id = s.enterprise_id "
            "WHERE s.enterprise_id ILIKE $1 OR a.name ILIKE $1 "
            "ORDER BY s.enterprise_id",
            f"%{q}%",
        )
    else:
        rows = await pool.fetch(
            "SELECT s.enterprise_id, a.name as enterprise_name, s.plan, "
            "s.start_date, s.end_date "
            "FROM billing.subscriptions s "
            "LEFT JOIN huanyu.agents a ON a.agent_id = s.enterprise_id "
            "ORDER BY s.enterprise_id"
        )
    return {"subscriptions": [dict(r) for r in rows]}


@router.post("/subscriptions")
async def upsert_subscription(
    body: dict,
    _admin: str = Depends(verify_admin_token),
):
    """创建/更新企业订阅计划（财务收款后操作）

    财务只需填写:
      {"enterprise_name": "示例企业名称",
       "module": "bidding",
       "plan": "pro",
       "start_date": "2026-06-24",
       "end_date": "2027-06-24",
       "amount": 6000.00,
       "invoice_needed": true,
       "remark": "对公转账"}

    后台自动完成:
      名称→ID 解析 → 订阅入库 → 财务记账 → 发票申请 → 推送客户
    """
    enterprise_name = body.get("enterprise_name", "")
    enterprise_id = body.get("enterprise_id", "")
    module = body.get("module", "bidding")
    plan = body.get("plan", "pro")
    start_date = body.get("start_date", "")
    end_date = body.get("end_date", "")
    amount = body.get("amount", 0)
    invoice_needed = body.get("invoice_needed", False)
    remark = body.get("remark", "")

    pool = await get_pool()

    # 1. 名称 → ID 解析
    if not enterprise_id and enterprise_name:
        row = await pool.fetchrow(
            "SELECT agent_id, name FROM huanyu.agents WHERE name ILIKE $1",
            f"%{enterprise_name}%",
        )
        if row:
            enterprise_id = row["agent_id"]
            enterprise_name = row["name"]
        else:
            raise HTTPException(404, detail=f"未找到企业：{enterprise_name}")

    if not enterprise_id or plan not in ("free", "pro", "enterprise"):
        raise HTTPException(400, detail="参数错误：enterprise_name 必填，plan 必须为 free/pro/enterprise")

    # 空日期保护 + str→date 转换（asyncpg 需要 date 类型对象）
    _dt = __import__("datetime").datetime
    if not start_date:
        start_date = _dt.now().date()
    else:
        start_date = _dt.strptime(start_date, "%Y-%m-%d").date()
    if not end_date:
        end_date = (_dt.now() + __import__("datetime").timedelta(days=365)).date()
    else:
        end_date = _dt.strptime(end_date, "%Y-%m-%d").date()

    # 2. 写入订阅表
    await pool.execute(
        """INSERT INTO billing.subscriptions
           (enterprise_id, enterprise_name, module, plan, amount,
            start_date, end_date, invoice_needed, remark)
           VALUES ($1,$2,$3,$4,$5,$6::date,$7::date,$8,$9)
           ON CONFLICT (enterprise_id, module) DO UPDATE
           SET plan=$4, amount=$5, start_date=$6::date, end_date=$7::date,
               invoice_needed=$8, remark=$9, updated_at=NOW()""",
        enterprise_id, enterprise_name, module, plan, amount,
        start_date, end_date, invoice_needed, remark,
    )

    # 3. 记财务账（如果需要发票）
    invoice_no = None
    if invoice_needed:
        invoice_no = f"INV-{enterprise_id[:8]}-{__import__('time').strftime('%Y%m%d%H%M%S')}"
        await pool.execute(
            "INSERT INTO billing.invoices (enterprise_id, invoice_no, amount, module, status, created_at) "
            "VALUES ($1,$2,$3,$4,'pending',NOW())",
            enterprise_id, invoice_no, amount, module,
        )

    # 4. 实时推送到客户服务器
    await push_sync_to_client(enterprise_id, plan)

    return {
        "ok": True,
        "enterprise_id": enterprise_id,
        "enterprise_name": enterprise_name,
        "plan": plan,
        "module": module,
        "amount": amount,
        "invoice_no": invoice_no,
    }


async def push_sync_to_client(enterprise_id: str, plan: str):
    """付费状态变更后，主动推送同步到客户服务器"""
    import asyncio
    pool = await get_pool()
    try:
        row = await pool.fetchrow(
            "SELECT server_url FROM billing.enterprises WHERE enterprise_id = $1",
            enterprise_id,
        )
    except Exception:
        # billing.enterprises 表不存在时跳过推送（不影响主流程）
        return
    if not row or not row["server_url"]:
        return

    async def _do_push():
        try:
            import hashlib, hmac
            from common.license import SIGN_KEY as SK
            payload = f"{enterprise_id}:{plan}"
            sig = hmac.new(SK.encode(), payload.encode(), hashlib.sha256).hexdigest()
            import httpx
            url = f"{row['server_url']}/v1/license/sync"
            async with httpx.AsyncClient() as client:
                await client.post(url, json={
                    "enterprise_id": enterprise_id, "plan": plan,
                    "signature": sig,
                }, timeout=10)
        except Exception:
            pass

    asyncio.create_task(_do_push())  # 非阻塞推送


# 客户端同步端点（挂载在客户服务器上）
from fastapi import APIRouter as ClientRouter
client_router = ClientRouter(prefix="/v1/license", tags=["license-client"])


@client_router.post("/sync")
async def receive_sync(body: dict):
    """接收管理服推送的付费状态变更（需 HMAC 签名校验防止伪造）"""
    from common.license import _cloud_cache, LICENSE_PATH, load_license, SIGN_KEY
    import hashlib, hmac, time as _time, yaml, os

    enterprise_id = body.get("enterprise_id", "")
    plan = body.get("plan", "free")
    module = body.get("module", "bidding")
    expires_at = body.get("expires_at", "")
    signature = body.get("signature", "")

    if not enterprise_id:
        return {"ok": False, "reason": "enterprise_id 缺失"}

    # 校验 HMAC 签名（防伪造推送）
    # 载荷格式必须与 push_sync_to_client 一致: f"{enterprise_id}:{plan}"
    # P0 (2026-08-27 review): 原实现 SIGN_KEY 为空时跳过整个校验直接放行（fail-open）
    # —— 默认部署即中招：任意人 POST {"enterprise_id":"x","plan":"pro"} 即可给
    # 客户机提权。与 license.py:48 已修的同型漏网。改 fail-closed：未配密钥一律拒绝。
    if not SIGN_KEY:
        return {"ok": False, "reason": "服务端未配置 HUANYU_SIGN_KEY，拒绝处理同步（fail-closed）"}
    payload = f"{enterprise_id}:{plan}"
    expected = hmac.new(SIGN_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return {"ok": False, "reason": "签名校验失败，拒绝更新"}

    # 1. 更新内存缓存（mtime 自动检测文件变化，无需手动刷新）
    cache_key = f"{enterprise_id}:{module}"
    _cloud_cache[cache_key] = {"plan": plan, "cached_at": _time.time()}

    # 2. 写回 license.yaml（含管理服签名）
    # P1 (2026-08-27 review #5): 原实现直接写回 → 顶层新增 modules 键后，
    # 下次 load_license 的 HMAC 载荷（json.dumps 全量 sort_keys）必与旧签名
    # 失配 → 整机降级 free（推送一次毁一次）。写回前用同口径重算顶层签名。
    try:
        lic = load_license()
        if "modules" not in lic:
            lic["modules"] = {}
        lic["modules"][module] = {
            "plan": plan,
            "expires_at": expires_at,
            "signature": signature,  # 管理服签名，下次校验用
            "synced_at": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
        }
        # 重算顶层签名（与 common.license._verify_signature 完全同口径：
        # 去 signature 键 → json.dumps(sort_keys=True) → HMAC-SHA256）
        lic.pop("signature", None)
        payload_full = json.dumps(lic, sort_keys=True)
        lic["signature"] = hmac.new(
            SIGN_KEY.encode(), payload_full.encode(), hashlib.sha256,
        ).hexdigest()
        os.makedirs(os.path.dirname(LICENSE_PATH), exist_ok=True)
        with open(LICENSE_PATH, "w") as f:
            yaml.dump(lic, f, default_flow_style=False)
        # 自检：写回后立即重新加载验证（mtime 变化触发重读），失败告警可观测
        from common.license import load_license as _reload
        reloaded = _reload()
        if reloaded.get("plan") == "free" and plan != "free":
            logger.warning(
                "[license/sync] 写回后自检验签失败 enterprise=%s module=%s", enterprise_id, module)
    except Exception as e:
        logger.warning("[license/sync] license.yaml 写回失败: %s", e)

    return {"ok": True, "plan": plan}
