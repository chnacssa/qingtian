# 汇川文件处理管道 — 实施落地文档

**基线**: 现有 `ingest.py` 仅提取文本、丢弃图片；XLSX 全 Sheet 拍扁为纯文本；未知格式硬解码或跳过。

**实际代码已提交** (commit `a615b6f`)，各 Phase 对应文件和架构说明如下。

---

## Phase 1 — DDL + 格式分类 + 配置

### 1.1 `database.py` — file_images DDL

新增 `file_images` 表，`file_id` FK → `file_registry(file_id) ON DELETE CASCADE`。

关键字段：`image_id`, `source_type`(pdf/docx/xlsx), `page_num`, `source_sheet`, `image_format`, `image_size`, `image_sha256`(去重), `storage_path`, `width/height`, `context_before/after`(200字符), `alt_text`(多模态预留)。

索引：`idx_fi_file` (file_id), `idx_fi_sha256`。

### 1.2 `file_classifier.py` — 魔数检测 + 格式分类

架构：
1. **魔数优先**: `_MAGIC_SIGNATURES` 表按顺序匹配 (PDF/PK/PNG/JPEG/GIF/WEBP)
2. **ZIP 子类型**: PK 命中后通过扩展名判断 (docx/xlsx/pptx)
3. **扩展名兜底**: 魔数未命中 → `.txt/.md/.json/.csv` 按扩展名
4. **未知格式**: → `FileCategory("unknown", ...)`，`processable=False`

`FileCategory` 含 `fmt`/`mime`/`category`/`processable`/`future_processable`(image→True)。

**实际代码 vs 设计**：完全一致。`FileCategory.__init__` 已将 `fmt` 作为参数，无动态属性风险。

### 1.3 `config.py` — 配置获取器

新增 8 个 getter：图片提取开关/上限/大小/子目录；Excel Sheet 独立编译开关/Sheet上限/图表；MIME 检测模式。

---

## Phase 2 — 图片提取管道

### 2.1 `image_extractor.py` — PDF/DOCX 图片提取

架构：
- `extract_from_pdf()` → `asyncio.to_thread` 内用 pdfplumber 逐页遍历 `page.images`，提取 stream/data，调 `_detect_image_format()` 从 filter/魔数/name 推断格式
- `extract_from_docx()` → `asyncio.to_thread` 内遍历 paragraph runs，通过 `blip` XML 标签 + `related_parts` 提取 blob，调 `_detect_format_from_bytes()` 从魔数推断格式
- `_save_image()` → 保存到 `{storage_base}/{yyyy}/{mm}/images/{file_id}/{index}.{fmt}`
- `ImageRecord` → 含原始字节/fmt/上下文/PIL 尺寸/存储路径/SHA256

关键约束：`MAX_IMAGES=50`, `MAX_IMAGE_SIZE=10MB`，上下文各 200 字符。

**实际代码 vs 设计**: 
- 代码更简洁：无 `nonlocal`、单层循环、PDF 上下文取自相邻页文本
- `_detect_format_from_bytes` 为新增函数，解决 DOCX blob 格式识别
- `_detect_image_format` 增加 CCITT/JP2/TIFF/GIF 检测

### 2.2 `ingest.py` — 图片提取分支

在 `ingest_text()` 返回后、`_upsert_file_registry` 前插入图片提取：
- 仅 PDF/DOCX 进入分支
- 受 `kcfg.get_image_extraction_enabled()` / `get_max_images_per_file()` 控制
- INSERT 到 `file_images` 表含完整 context_before/context_after
- 失败时 log warning 不阻塞管道
- `result["images_registered"]` 追加到返回字典

---

## Phase 3 — Excel 处理器

### 3.1 `excel_processor.py` — Sheet 独立编译 + 图片提取

架构：
- `process_xlsx(data, storage_base, file_id, extract_images=True)` → 在线程池内解析 XLSX，返回 `list[SheetResult]`
- 每个 Sheet 转为 Markdown 表格（首行标题 → `|---|---|` → 数据行）
- `extract_images=True` 时遍历 `ws._images`，`_extract_image_data()` 兼容不同 openpyxl 版本 (`_data()` / `ref` / `blob`)
- `_save_xlsx_image()` 保存到 `{storage_base}/{yyyy}/{mm}/images/{file_id}/s{sheet_idx}_{img_idx}.{fmt}`

`xlsx_to_entries(data, source, filename, storage_path, conn, schema, storage_base)`：
- 调用 `process_xlsx(data, storage_base, "", extract_images=False)` — 仅取 Markdown
- `sheet_independent=True`(默认): 每 Sheet 独立 `ingest_text()` + 追加 sheet_name/row_count/col_count 到 metadata
- `sheet_independent=False`: 全部 Sheet 合并为一个文档

关键约束：`MAX_SHEETS=20`, `MAX_IMAGES_PER_SHEET=10`。

**实际代码 vs 设计**:
- 单 workbook 加载 (`data_only=True`) 而非双 workbook — 更轻量
- `extract_images=False` 参数避免 `xlsx_to_entries` 内部重复提取图片（外部 `ingest.py` 统一处理）
- `_extract_image_data()` 跨版本兼容 openpyxl

### 3.2 `ingest.py` — XLSX 分支调度

`ingest_file()` 中，`file_classifier` 判断 `processable=True` 且扩展名为 `.xlsx` + `sheet_independent=True` 时：
1. 调用 `xlsx_to_entries()` 获取编译结果
2. 独立调用 `process_xlsx(file_bytes, storage_base, file_id)` 提取图片
3. 图片 INSERT 到 `file_images` 表（含 source_sheet）
4. 返回 `xlsx_sheets` / `images_registered` 计数

---

## Phase 4 — 元数据提取 + 兜底

### 4.1 `metadata_extractor.py` — 不可处理格式元数据

纯函数分发表 `EXTRACTORS = {"image": extract_image_meta}`。

`extract_image_meta()` → PIL 提取 width/height/format/mode。

`extract_fallback()` → 标记 `future_processable=True`。

**实际代码 vs 设计**: 无 `extract_video_meta` / `extract_audio_meta` 桩函数 — 待 Phase 4+ 按需添加。

### 4.2 `ingest.py` — 格式兜底分支

`file_classifier.classify()` 返回 `processable=False` 时：
1. 查 `EXTRACTORS` 分发表提取元数据
2. 标记 `unknown_format = (fc.fmt == "unknown")`
3. `file_registry` 状态设为 `metadata_only`
4. 返回 `status="metadata_only"`, `future_processable` 标记

---

## Phase 5 — API + 清理

### 5.1 `api.py` — 文件图片端点

| 方法 | 路径 | 功能 |
|------|------|------|
| GET | `/v1/huichuan/files/images` | 文件图片列表（支持 file_id/source_type 过滤，分页） |
| GET | `/v1/huichuan/images/{image_id}` | 单张图片详情（含 original_filename） |
| GET | `/v1/huichuan/images/{image_id}/download` | 下载图片原文件（正确 MIME） |
| POST | `/v1/huichuan/files/{storage_path}/reprocess` | 存量文件重扫 — 重新全管道 |
| POST | `/v1/huichuan/files/reprocess-future` | 批量重扫所有 `future_processable` 文件 |

设计说明：
- 图片下载用 `Response(content=..., media_type=...)` 而非 Stream — 图片 < 10MB，单次读取即可
- `reprocess_file` 一次从 DB 取 file_registry 记录，读磁盘，走完整 ingest_file
- `reprocess-future` 扫描 `status='metadata_only' AND metadata->>'future_processable'='true'`，最多 50 条

**实际代码 vs 设计**:
- 使用 `storage_path` 而非 `file_id` 作为文件标识 — 更通用（跨存储后端）
- 增加了 `list_all_file_images` 全局列表端点（附带 original_filename 联表）
- `reprocess-future` 为新增端点

### 5.2 `cron.py` — 图片清理任务

`_cleanup_file_images_job()` 每天 04:00 运行：
- LEFT JOIN file_registry 找出孤立 file_images 记录
- 批量 DELETE (ANY($1)) + 物理文件删除
- 最多 200 条/次

---

## Phase 6 — 飞书同步

`receiver/feishu.py` — 在 return dict 追加 `images_registered` / `xlsx_sheets` 字段。

`ingest_file()` 内部已走完整管道（格式分类 → 图片提取 → LLM 编译），飞书端无需额外改动。

---

## 边界约束

| 约束 | 值 | 位置 |
|------|-----|------|
| 单文件最大图片数 | 50 | `image_extractor.MAX_IMAGES` |
| 单张图片最大大小 | 10MB | `image_extractor.MAX_IMAGE_SIZE` |
| 每文件最大 Sheet 数 | 20 | `excel_processor.MAX_SHEETS` |
| Sheet 独立编译 | 默认开 | `config.get_excel_sheet_independent()` |
| 图片上下文长度 | 200 字前后 | `ImageRecord.__init__` |
| MIME 检测 | 魔数优先 | `file_classifier.classify()` |
| 不支持格式 | 不报错，存 metadata | `ingest_file()` 兜底分支 |
| 图片提取失败 | 静默跳过，不阻塞管道 | `ingest_file()` try/except 包裹 |
| XLSX 密码保护 | openpyxl 抛异常 → 空结果 | `excel_processor._log_open_error()` |
| 空文件（0 字节） | 同损坏文件处理 | `ingest_file()` `if not text` 分支 |
| 线程池 I/O 异常 | try/except，不抛到事件循环 | 各 `asyncio.to_thread` 内部 |
| zip bomb | pdfplumber/openpyxl 受 MAX_IMAGE_SIZE 限制 | 边界常量 |

---

## 实施顺序（实际执行）

```
Phase 1 (DDL+分类+配置) → Phase 2 (图片提取) → Phase 3 (Excel) → Phase 4 (兜底) → Phase 5 (API+清理) → Phase 6 (飞书)
```

## 未完成（待后续）

- `search.py` 搜索结果附带关联图片（需搜索适配 + 性能优化后实施）
- `extract_video_meta` / `extract_audio_meta` 桩函数
- `alt_text` 多模态填充（等 Vision 模型上线）
