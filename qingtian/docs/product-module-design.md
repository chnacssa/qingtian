# 销售 Agent 产品目录/价目表/图片/文档 — 设计文档

> 撰写日期：2026-06-20
> 对应实现：Phase 1-6，19 个文件

---

## 1. 背景与目标

### 1.1 问题

销售 Agent 目前只能发文本消息做报价谈判，**无法**提交产品目录、价目表、产品图片或企业公开文档。这限制了对采购 Agent 的数字化协同能力——采购方无法获取结构化的产品数据、价格信息或资质文件。

### 1.2 目标

1. 销售 Agent 能**创建/维护产品目录**，定义电力物资的类别、型号、技术参数
2. 销售 Agent 能**管理价目表**，支持版本化更新和每日自动过期处理
3. 销售 Agent 能**上传产品图片**并与产品关联
4. 销售 Agent 能**管理企业文档**（资质、证书、样本、合同模板）
5. 支持**XLSX 批量导入**产品目录和价目表
6. Agent 之间通过**消息附件机制**传递文件引用（`file_id`），不内嵌二进制
7. 所有功能包装为 **Skill**，可绑定到任意销售 Agent

### 1.3 非目标

- 与外部 ERP 系统的双向同步（后续版本）
- 产品 BOM 多层结构
- 在线预览/转码（由 file_service 层提供）

---

## 2. 整体架构

```
┌──────────────────────────────────────────────────────┐
│                    Agent (SKill 绑定)                  │
│  ┌──────────────────────────────────────────────────┐ │
│  │ ProductCatalogSkill  PriceListSkill              │ │
│  │ DocumentSkill      ProductImageSkill             │ │
│  └──────────┬───────────────────────────────────────┘ │
└─────────────┼──────────────────────────────────────────┘
              │ execute(params) → HTTP
              ▼
┌──────────────────────────────────────────────────────┐
│              REST API (/v1/product/*)                  │
│  ┌──────────┬──────────┬──────────┬────────────────┐  │
│  │ catalog  │price-lists│ images  │  documents     │  │
│  └──────────┴──────────┴──────────┴────────────────┘  │
└──────────────────────┬──────────────────────────────────┘
                       │ asyncpg
                       ▼
┌──────────────────────────────────────────────────────┐
│              PostgreSQL (product schema)               │
│  ┌──────────┬──────────┬──────────┬────────────────┐  │
│  │product_  │price_    │price_list│  enterprise_   │  │
│  │catalog   │lists     │_items    │  documents     │  │
│  └──────────┴──────────┴──────────┴────────────────┘  │
│  ┌──────────────────────────────────────────────────┐  │
│  │ product_images                                   │  │
│  └──────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

**数据流：**

```
Agent 上传文件 → file_service 返回 file_id
  → Skill.execute({action, enterprise_id, file_id, ...})
    → API 写入 DB（文件元数据）
      → 通过消息附件传 file_id（不传二进制）
```

---

## 3. 数据库设计

5 张表，`product` schema。所有 `enterprise_id` 作为显式字段支持多企业隔离。

### 3.1 product_catalog — 产品定义

| 列 | 类型 | 说明 |
|---|---|---|
| product_id | UUID PK | 自动生成 |
| enterprise_id | TEXT NOT NULL | 企业隔离 |
| category | TEXT NOT NULL | 类别（如"变压器"、"开关柜"） |
| name | TEXT NOT NULL | 产品名称 |
| model | TEXT | 型号 |
| voltage_level | TEXT | 电压等级 |
| power_rating | TEXT | 容量/功率 |
| standards | TEXT[] | 执行标准列表 |
| technical_params | JSONB | 技术参数 KV |
| accessories | TEXT[] | 配件列表 |
| certification_required | TEXT[] | 认证要求 |
| unit | TEXT | 单位，默认"台" |
| status | TEXT | active / discontinued / archived |
| created_by / updated_by | TEXT | 创建人/更新人 |

**索引：** enterprise_id, category, status, name 全文检索（GIN tsvector）

### 3.2 product_images — 产品图片

| 列 | 类型 | 说明 |
|---|---|---|
| image_id | UUID PK | 自动生成 |
| product_id | UUID FK → product_catalog | 所属产品（CASCADE DELETE） |
| enterprise_id | TEXT NOT NULL | 企业隔离 |
| file_id | TEXT NOT NULL | file_service 返回的 ID |
| filename | TEXT NOT NULL | 原始文件名 |
| is_primary | BOOLEAN | 是否主图 |
| sort_order | INT | 排序 |
| file_size | BIGINT | 文件大小 |
| image_sha256 | TEXT | 文件哈希 |
| mime_type | TEXT | MIME 类型 |

**索引：** product_id, (product_id, is_primary) WHERE is_primary

### 3.3 price_lists — 价目表头

| 列 | 类型 | 说明 |
|---|---|---|
| price_list_id | UUID PK | 自动生成 |
| enterprise_id | TEXT NOT NULL | 企业隔离 |
| name | TEXT NOT NULL | 价目表名称 |
| version | INT | 版本号，supersede 时 +1 |
| valid_from | DATE NOT NULL | 生效日期 |
| valid_until | DATE | 失效日期 |
| status | TEXT | draft / active / superseded / archived |
| source | TEXT | manual / xlsx_import / api |
| source_file_id | TEXT | 来源文件 ID |
| daily_update | BOOLEAN | 标记每日自动处理 |
| approved_by / approved_at | TEXT / TIMESTAMPTZ | 审批人/时间 |

**索引：** (enterprise_id, status), (valid_from, valid_until), (daily_update) WHERE daily_update

### 3.4 price_list_items — 价目表明细

| 列 | 类型 | 说明 |
|---|---|---|
| item_id | BIGSERIAL PK | 自动递增 |
| price_list_id | UUID FK → price_lists | 所属价目表（CASCADE DELETE） |
| product_id | UUID FK → product_catalog | 可选关联产品 |
| product_spec | JSONB | 产品规格描述（非关联时使用） |
| unit_price | NUMERIC(14,2) | 单价，> 0 |
| currency | TEXT | 币种，默认 CNY |
| quantity_discount | JSONB | 数量折扣定义 |
| valid_from / valid_until | DATE | 行级有效期 |

**索引：** price_list_id, product_id

### 3.5 enterprise_documents — 企业文档

| 列 | 类型 | 说明 |
|---|---|---|
| document_id | UUID PK | 自动生成 |
| enterprise_id | TEXT NOT NULL | 企业隔离 |
| title | TEXT NOT NULL | 文档标题 |
| document_type | TEXT | contract_template / qualification / certificate / brochure / spec_sheet / other |
| file_id | TEXT NOT NULL | file_service 文件 ID |
| file_size | BIGINT | 文件大小 |
| file_sha256 | TEXT | 文件哈希 |
| tags | TEXT[] | 标签 |
| visibility | TEXT | public / enterprise / private |
| owner_agent | TEXT | 所属 Agent |
| valid_until | DATE | 有效截止日期 |
| status | TEXT | active / archived / revoked |

**索引：** enterprise_id, (enterprise_id, document_type), owner_agent, tags (GIN)

### 3.6 外部变更

- `huanyu.messages` 表的 `message_type CHECK` 约束增加 `'file'`、`'image'`、`'structured_data'` 枚举值
- `procurement.agents.py` 的 SELLER_BASE_CAPABILITIES 增加 `"产品目录"`、`"价目表"`、`"文档管理"`、`"产品图片"`

---

## 4. API 设计

前缀：`/v1/product`，所有端点通过 `X-Enterprise-ID` 头或查询参数 `enterprise_id` 区分企业。

### 4.1 产品目录

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/catalog` | 列表（分页、按 category/q/status 过滤） |
| GET | `/catalog/{product_id}` | 详情（含图片列表） |
| POST | `/catalog` | 创建 |
| PUT | `/catalog/{product_id}` | 更新（只传非空字段） |
| DELETE | `/catalog/{product_id}` | 软删除（→ archived） |
| POST | `/catalog/import` | XLSX 批量导入 |

### 4.2 产品图片

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/{product_id}/images` | 列出图片 |
| POST | `/{product_id}/images` | 添加图片（文件已在 file_service 中） |
| PUT | `/{product_id}/images/{image_id}/primary` | 设为主图 |
| DELETE | `/{product_id}/images/{image_id}` | 删除图片 |

**enterprise_id 策略：** 图片创建时通过 product_catalog 表反查 enterprise_id，不依赖客户端传入。

### 4.3 价目表

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/price-lists` | 列表（分页、过滤） |
| GET | `/price-lists/{id}` | 详情（含明细行） |
| POST | `/price-lists` | 创建 |
| PUT | `/price-lists/{id}` | 更新 |
| DELETE | `/price-lists/{id}` | 软删除 |
| POST | `/price-lists/{id}/activate` | 激活（draft → active） |
| POST | `/price-lists/{id}/supersede` | 版本升级（→ superseded + 新版本） |
| GET | `/price-lists/{id}/items` | 列出明细 |
| POST | `/price-lists/{id}/items` | 添加明细行 |
| PUT | `/price-lists/{id}/items/{item_id}` | 更新明细行 |
| DELETE | `/price-lists/{id}/items/{item_id}` | 删除明细行 |
| PUT | `/price-lists/{id}/items/batch` | 批量替换明细（先删后插） |
| POST | `/price-lists/{id}/import` | XLSX 导入明细 |

### 4.4 企业文档

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/documents` | 列表（分页） |
| GET | `/documents/{id}` | 详情 |
| POST | `/documents` | 登记文档（文件已在 file_service 中） |
| PUT | `/documents/{id}` | 更新元数据 |
| DELETE | `/documents/{id}` | 软删除 |

### 4.5 通用响应格式

```json
{
  "status": "ok",
  "total": 100,
  "page": 1,
  "page_size": 20,
  "items": [...]
}
```

错误时返回 `{"status": "error", "error": "..."}` 或 FastAPI HTTPException。

---

## 5. Skill 设计

4 个 Skill，均继承 `osskill.models.BaseSkill`，遵循 BiddingSkill 模式。通过 `CAPABILITIES` dict 映射 `action` → `{method, path, desc}`，`execute()` 按 action 路由到 `_call_api()` 发起 aiohttp HTTP 调用。

| Skill | actions | 功能 |
|---|---|---|
| ProductCatalogSkill | list_products / get_product / create_product / update_product / delete_product / search_products / import_products | 产品目录 CRUD + 搜索 + 导入 |
| PriceListSkill | list_price_lists / get_price_list / create / update / activate / supersede / delete + list_items / add_item / batch_update_items / import_price_list | 价目表全生命周期管理 |
| DocumentSkill | list_documents / get_document / upload / update / delete | 企业文档管理 |
| ProductImageSkill | list_images / upload_image / set_primary_image / delete_image | 产品图片管理 |

Skill 统一输入格式：

```python
await skill.execute({
    "action": "create_product",
    "enterprise_id": "ent-001",
    "category": "变压器",
    "name": "SF6断路器LW36-126",
    ...  # 其他业务字段
})
```

返回格式：`{"ok": True, "data": {...}}` 或 `{"ok": False, "error": "..."}`。

---

## 6. 价目表版本化与每日更新

### 6.1 版本化机制

```
supersede API 调用流程：
  1. 读取当前价目表完整记录
  2. 当前表 status → superseded, valid_until = 新 valid_from
  3. 插入新表：version +1, valid_from = 新日期, status = active
  4. 复制所有明细行到新表
```

### 6.2 每日自动过期

`product/cron.py` 每天 02:00（北京时间）运行（仅 management 角色）：

1. 查找 `daily_update = TRUE` 且 `valid_until < 今天` 且 `status = 'active'` 的价目表 → 标记 `superseded`
2. 查找 `superseded` 超过 180 天的价目表 → 标记 `archived`

---

## 7. 消息附件机制

Agent 间传递文件时使用 `file_id` 引用，不内嵌二进制数据。

### message_type = "file" 的 payload 格式

```python
{
    "type_hint": "product_catalog" | "price_list" | "document" | "image" | "other",
    "file_id": "abc123...",
    "filename": "catalog.xlsx",
    "file_size": 12345,
    "file_sha256": "def456...",
    "enterprise_id": "ent-001",
    "description": "2026Q3 updated catalog",
}
```

### message_type = "image" 的 payload 格式

```python
{
    "type_hint": "product_image",
    "image_id": "uuid...",
    "product_id": "uuid...",
    "product_name": "SF6断路器LW36-126",
    "file_id": "abc123...",
    "filename": "product_photo.jpg",
    "enterprise_id": "ent-001",
}
```

### 发送方流程

1. 通过 `file_service` 上传文件 → 得到 `file_id`
2. 调用 `product/messaging.py` 的 `send_product_file()` 或 `send_product_image()`
3. 发送消息时在 `payload` 中携带 `file_id`

### 接收方流程

1. 从消息 `payload` 中提取 `file_id`
2. 通过 `file_service` 下载文件

---

## 8. XLSX 导入格式

### 8.1 产品目录导入

| 类别* | 产品名称* | 型号 | 电压等级 | 容量 | 执行标准 | 单位 | 单价(元) |
|---|---|---|---|---|---|---|---|
| 变压器 | SF6断路器 | LW36-126 | 110kV | 2000A | GB/T 1984;IEC 62271 | 台 | 150000 |

- 执行标准列支持 `;` 或 `；` 分隔
- 通过 `file_service` 上传 XLSX 后调用 POST `/catalog/import`

### 8.2 价目表导入

| 产品名称* | 型号 | 单价(元)* | 币种 | 数量折扣(JSON) |
|---|---|---|---|---|
| SF6断路器 | LW36-126 | 150000 | CNY | {"100+": 0.95, "500+": 0.92} |

- 通过 `file_service` 上传 XLSX 后调用 POST `/price-lists/{id}/import`

---

## 9. 文件清单

### 新增（14 个）

| 文件 | 职责 |
|---|---|
| `product/__init__.py` | 模块声明 |
| `product/config.py` | 配置读取 |
| `product/database.py` | DDL + ensure_schema |
| `product/models.py` | Pydantic 模型 |
| `product/api.py` | REST API（~930 行） |
| `product/excel_processor.py` | XLSX 解析 |
| `product/cron.py` | 定时过期处理 |
| `product/messaging.py` | 消息附件辅助函数 |
| `osskill/implementations/product_catalog/__init__.py` | Skill 注册 |
| `osskill/implementations/product_catalog/product_catalog.py` | 产品目录 Skill |
| `osskill/implementations/price_list/__init__.py` | Skill 注册 |
| `osskill/implementations/price_list/price_list.py` | 价目表 Skill |
| `osskill/implementations/document/__init__.py` | Skill 注册 |
| `osskill/implementations/document/document.py` | 文档 Skill |

### 修改（3 个）

| 文件 | 变更 |
|---|---|
| `main.py` | 注册 product router + schema 初始化 + cron 启停 |
| `huanyu/database.py` | message_type CHECK 扩增 |
| `procurement/agents.py` | SELLER_BASE_CAPABILITIES 扩增 |

---

## 10. 验证方式

1. **语法检查：** `python -c "from product.database import ensure_schema"` — DDL 可导入
2. **Skill 加载：** `python -c "from osskill.loader import SkillLoader; SkillLoader.load('product_catalog')"` — 4 个 Skill 均可加载
3. **API 启动：** `python main.py` 启动无报错
4. **接口测试：** `curl /v1/product/catalog?enterprise_id=test` 返回 200
5. **Agent 调用：** 绑定 Skill → 调用 execute 返回正确结果

---

## 11. 日志规范

所有模块使用 `logging.getLogger("product.xxx")` 格式，日志级别：

| 场景 | 级别 |
|---|---|
| 正常 CRUD 操作 | INFO（只记录 ID 和关键摘要） |
| XLSX 导入结果 | INFO（记录成功/失败计数） |
| Cron 执行结果 | INFO（记录处理数量） |
| API 调用异常 | WARNING（记录错误摘要 ≤200 字符） |
| 数据库错误 | ERROR |

---

## 12. Bug 修复记录（Review 后）

| 问题 | 位置 | 修复 |
|---|---|---|
| enterprise_id 传空字符串 | `api.py:257` | 改为从 product_catalog 反查 enterprise_id |
| 未使用的 import | `excel_processor.py:16` | 删除 `from datetime import date` 和 `from typing import Optional` |
| 未使用的 import | `messaging.py:36` | 删除 `from typing import Optional` |
| SQL 冗余赋值 | `cron.py:65` | 删除 `valid_until = valid_until` |
