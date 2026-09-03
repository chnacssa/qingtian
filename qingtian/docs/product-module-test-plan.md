# 产品目录/价目表/图片/文档 模块 — 部署测试方案

> 撰寫日期：2026-06-20
> 對應提交：需合併 product-module 分支到 master

---

## 一、部署步驟

### 1.1 更新代碼

```bash
# 所有伺服器（management / sales）都需要更新
cd /opt/qingtian
git checkout master
git pull origin master
```

### 1.2 檢查依賴

```bash
pip install -r requirements.txt   # 確保 httpx、openpyxl 已安裝
```

### 1.3 檢查數據庫 DDL

```bash
# 手動驗證 product schema DDL 可執行（非 production 可先跑 dry-run）
python3 -c "from product.database import ensure_schema; print('DDL OK')"
```

### 1.4 重啟服務

```bash
# 重啟ACSSA系統
systemctl restart qingtian   # 或 supervisorctl restart qingtian
```

### 1.5 檢查啟動日誌

```bash
journalctl -u qingtian -n 50 --no-pager | grep -i "product"
# 應能看到：
#   product schema init: ✔️
#   product cron started (僅 management)
```

---

## 二、影響範圍

### 修改文件（3 個）

| 文件 | 變更內容 |
|---|---|
| `qingtian/main.py` | 註冊 product router + schema 初始化 + cron 啟停 |
| `qingtian/huanyu/database.py` | message_type CHECK 增加 `file`/`image`/`structured_data` |
| `qingtian/procurement/agents.py` | SELLER_BASE_CAPABILITIES 增加目錄/價目表/文檔/圖片 |

### 新增文件（14 個）

| 文件 | 說明 |
|---|---|
| `product/__init__.py` | 模塊聲明 |
| `product/config.py` | 配置讀取（schema_name + 文件下載 URL） |
| `product/database.py` | DDL（5 張表） |
| `product/models.py` | Pydantic 模型 |
| `product/repository.py` | 數據訪問層（4 個 Repo） |
| `product/api.py` | REST API（28 個端點） |
| `product/excel_processor.py` | XLSX 解析 |
| `product/cron.py` | 定時過期處理 |
| `product/messaging.py` | 消息附件輔助函數 |
| `osskill/implementations/_base.py` | Skill 公共基類 |
| `osskill/implementations/product_catalog/product_catalog.py` | 產品目錄 Skill |
| `osskill/implementations/price_list/price_list.py` | 價目表 Skill |
| `osskill/implementations/document/document.py` | 文檔 Skill |
| `osskill/implementations/product_image/product_image.py` | 圖片 Skill |

---

## 三、測試用例

### 3.1 服務啟動檢查

| 編號 | 測試項 | 操作 | 預期結果 |
|---|---|---|---|
| TC-01 | 服務啟動 | `systemctl restart qingtian` | 無報錯，日誌無 traceback |
| TC-02 | 路由註冊 | `curl -s http://localhost:1996/v1/product/catalog?enterprise_id=test \| python3 -m json.tool` | 返回 `{"status":"ok","total":0,"page":1,...}` |
| TC-03 | DDL 初始化 | 檢查 `product` schema 下的 5 張表 | `\dt product.*` 應列出 product_catalog、product_images、price_lists、price_list_items、enterprise_documents |
| TC-04 | Skill 加載 | 在 Agent 配置中綁定任一 product Skill | Agent 啟動無報錯，Skill 可調用 |

### 3.2 產品目錄 CRUD（TC-10 至 TC-16）

測試前準備：
```bash
ENT="ent-test-001"
```

| 編號 | 測試項 | 操作 | 預期結果 |
|---|---|---|---|
| TC-10 | 創建產品 | `curl -s -X POST "http://localhost:1996/v1/product/catalog" -H "Content-Type: application/json" -d '{"enterprise_id":"'$ENT'","category":"变压器","name":"SF6断路器","model":"LW36-126","voltage_level":"110kV"}'` | 返回 `{"status":"ok","product_id":"uuid..."}` |
| TC-11 | 列出產品 | `curl -s "http://localhost:1996/v1/product/catalog?enterprise_id=$ENT"` | 返回分頁列表，items 包含 TC-10 創建的產品 |
| TC-12 | 搜索產品 | `curl -s "http://localhost:1996/v1/product/catalog?enterprise_id=$ENT&q=SF6"` | 搜索到含 "SF6" 的產品 |
| TC-13 | 按類別過濾 | `curl -s "http://localhost:1996/v1/product/catalog?enterprise_id=$ENT&category=变压器"` | 僅返回變壓器類別的產品 |
| TC-14 | 獲取產品詳情 | `curl -s "http://localhost:1996/v1/product/catalog/{product_id}"` | 返回產品詳情 + images 列表（空陣列） |
| TC-15 | 更新產品 | `curl -s -X PUT "http://localhost:1996/v1/product/catalog/{product_id}" -H "Content-Type: application/json" -d '{"unit":"套"}'` | 返回 `{"status":"ok","product_id":"..."}` |
| TC-16 | 刪除產品 | `curl -s -X DELETE "http://localhost:1996/v1/product/catalog/{product_id}"` | 返回 ok，再 GET 該產品 status 應為 archived |

### 3.3 產品圖片管理（TC-20 至 TC-24）

| 編號 | 測試項 | 操作 | 預期結果 |
|---|---|---|---|
| TC-20 | 上傳圖片文件 | 先通過 file_service 上傳：`curl -s -X POST "http://localhost:1996/v1/files/upload" -F "file=@test_img.jpg" -H "X-Enterprise-ID: $ENT"` | 返回 `{"file_id":"uuid...","filename":"test_img.jpg"}` |
| TC-21 | 添加圖片到產品 | `curl -s -X POST "http://localhost:1996/v1/product/{product_id}/images" -H "Content-Type: application/json" -d '{"file_id":"{file_id}","filename":"test_img.jpg","is_primary":true}'` | 返回 `{"status":"ok","image_id":"uuid..."}` |
| TC-22 | 列出產品圖片 | `curl -s "http://localhost:1996/v1/product/{product_id}/images"` | 返回 images 陣列，包含剛添加的圖片 |
| TC-23 | 設為主圖 | `curl -s -X PUT "http://localhost:1996/v1/product/{product_id}/images/{image_id}/primary"` | 返回 ok，is_primary 設為 true |
| TC-24 | 刪除圖片 | `curl -s -X DELETE "http://localhost:1996/v1/product/{product_id}/images/{image_id}"` | 返回 ok，再列出時該圖片應消失 |

### 3.4 價目表（TC-30 至 TC-38）

| 編號 | 測試項 | 操作 | 預期結果 |
|---|---|---|---|
| TC-30 | 創建價目表 | `curl -s -X POST "http://localhost:1996/v1/product/price-lists" -H "Content-Type: application/json" -d '{"enterprise_id":"'$ENT'","name":"2026Q3报价","valid_from":"2026-07-01","daily_update":true}'` | 返回 `{"status":"ok","price_list_id":"uuid..."}` |
| TC-31 | 列出價目表 | `curl -s "http://localhost:1996/v1/product/price-lists?enterprise_id=$ENT"` | 返回分頁列表，status 為 draft |
| TC-32 | 激活價目表 | `curl -s -X POST "http://localhost:1996/v1/product/price-lists/{id}/activate"` | status 變為 active |
| TC-33 | 添加明細行 | `curl -s -X POST "http://localhost:1996/v1/product/price-lists/{id}/items" -H "Content-Type: application/json" -d '{"product_spec":{"name":"SF6断路器","model":"LW36-126"},"unit_price":150000}'` | 返回 `{"status":"ok","item_id":1}` |
| TC-34 | 列出明細 | `curl -s "http://localhost:1996/v1/product/price-lists/{id}/items"` | 返回 items 陣列 |
| TC-35 | 批量更新明細 | `curl -s -X PUT "http://localhost:1996/v1/product/price-lists/{id}/items/batch" -H "Content-Type: application/json" -d '[{"product_spec":{"name":"SF6断路器"},"unit_price":155000},{"product_spec":{"name":"隔离开关"},"unit_price":80000}]'` | 返回 `{"status":"ok","count":2}` |
| TC-36 | 版本升級 | `curl -s -X POST "http://localhost:1996/v1/product/price-lists/{id}/supersede"` | 返回 new_id + version+1，舊表 status 變為 superseded |
| TC-37 | 刪除價目表 | `curl -s -X DELETE "http://localhost:1996/v1/product/price-lists/{id}"` | status 變為 archived |
| TC-38 | 過濾 daily_update | `curl -s "http://localhost:1996/v1/product/price-lists?enterprise_id=$ENT&daily_update=true"` | 僅返回 daily_update 為 true 的價目表 |

### 3.5 企業文檔（TC-40 至 TC-44）

| 編號 | 測試項 | 操作 | 預期結果 |
|---|---|---|---|
| TC-40 | 上傳文檔文件 | 先通過 file_service 上傳文件 | 獲得 file_id |
| TC-41 | 登記文檔 | `curl -s -X POST "http://localhost:1996/v1/product/documents" -H "Content-Type: application/json" -d '{"enterprise_id":"'$ENT'","title":"營業執照","document_type":"qualification","file_id":"...","filename":"license.pdf"}'` | 返回 document_id |
| TC-42 | 列出文檔 | `curl -s "http://localhost:1996/v1/product/documents?enterprise_id=$ENT"` | 返回文檔列表 |
| TC-43 | 按類型過濾 | `curl -s "http://localhost:1996/v1/product/documents?enterprise_id=$ENT&document_type=qualification"` | 僅返回 qualification 類型 |
| TC-44 | 刪除文檔 | `curl -s -X DELETE "http://localhost:1996/v1/product/documents/{id}"` | status 變為 archived |

### 3.6 Skill 調用測試（TC-50 至 TC-54）

測試前提：Agent 已綁定對應 Skill，並有 qingtian_url 配置。

| 編號 | 測試項 | 操作 | 預期結果 |
|---|---|---|---|
| TC-50 | ProductCatalogSkill 調用 | Agent 調用 `skill.execute({"action":"list_products","enterprise_id":"ent-001"})` | 返回 `{"ok":true,"data":{...}}` |
| TC-51 | PriceListSkill 調用 | Agent 調用 `skill.execute({"action":"list_price_lists","enterprise_id":"ent-001"})` | 返回 `{"ok":true,"data":{...}}` |
| TC-52 | DocumentSkill 調用 | Agent 調用 `skill.execute({"action":"list_documents","enterprise_id":"ent-001"})` | 返回 `{"ok":true,"data":{...}}` |
| TC-53 | ProductImageSkill 調用 | Agent 調用 `skill.execute({"action":"list_images","product_id":"uuid..."})` | 返回 `{"ok":true,"data":{...}}` |
| TC-54 | 錯誤操作處理 | Agent 調用 `skill.execute({"action":"invalid_action"})` | 返回 `{"ok":false,"error":"未知操作: ..."}` |

### 3.7 邊界與異常（TC-60 至 TC-66）

| 編號 | 測試項 | 操作 | 預期結果 |
|---|---|---|---|
| TC-60 | 企業隔離 | 用 enterprise_id=A 創建產品，用 enterprise_id=B 查詢 | B 看不到 A 的產品 |
| TC-61 | 不存在的產品 | `GET /catalog/{不存在的uuid}` | 返回 404 |
| TC-62 | 不存在的價目表 | `GET /price-lists/{不存在的uuid}` | 返回 404 |
| TC-63 | 缺少 enterprise_id | `GET /catalog` 不傳 enterprise_id | 返回 422（校驗錯誤） |
| TC-64 | 空產品名稱 | POST catalog 傳 `{"enterprise_id":"test","category":"A","name":""}` | 返回 422（min_length 校驗） |
| TC-65 | XLSX 導入無效 file_id | `POST /catalog/import?enterprise_id=test {"file_id":"invalid"}` | 返回 400「無法下載文件」 |
| TC-66 | 價目表 404 | 對不存在的價目表操作 activate/supersede | 返回 404 |

### 3.8 定時任務（僅 management 角色）

| 編號 | 測試項 | 操作 | 預期結果 |
|---|---|---|---|
| TC-70 | Cron 啟動 | 檢查 management 日誌 | 應看到 "Starting product cron: price list expiry@02:00 daily" |
| TC-71 | Cron 跳過 | 檢查 sales 伺服器日誌 | 應看到 "Product cron: skipped (not management role)" |
| TC-72 | 價目表自動過期 | 手動設置 `valid_until` 為昨天的日期 + `daily_update=true`，等待 cron 觸發（或手動調用 `_expire_price_lists_job`） | 價目表 status 變為 superseded |

### 3.9 消息附件（TC-80 至 TC-81）

| 編號 | 測試項 | 操作 | 預期結果 |
|---|---|---|---|
| TC-80 | send_product_file | Agent 調用 `send_product_file` 發送文件引用 | 對方收到 message_type=file 的消息，payload 包含 file_id |
| TC-81 | send_product_image | Agent 調用 `send_product_image` 發送圖片引用 | 對方收到 message_type=image 的消息，payload 包含 image_id + product_id |

---

## 四、驗證清單（供線上團隊填寫）

```
## 部署驗收

部署人員：__________  日期：__________

- [ ] 代碼已更新到 master (`git log --oneline -3`)
- [ ] 依賴已安裝 (`pip list \| grep -E 'httpx|openpyxl'`)
- [ ] 服務重啟成功 (`journalctl -u qingtian -n 20`)
- [ ] 所有路由已註冊 (`curl /v1/product/catalog?enterprise_id=test` 返回 200)

## 測試結果

| TC# | 測試項 | 結果（PASS/FAIL） | 備註 |
|---|---|---|---|
| TC-01 | 服務啟動 | | |
| TC-02 | 路由註冊 | | |
| TC-03 | DDL 初始化 | | |
| TC-10 | 創建產品 | | |
| TC-11 | 列出產品 | | |
| TC-12 | 搜索產品 | | |
| TC-13 | 按類別過濾 | | |
| TC-14 | 產品詳情 | | |
| TC-15 | 更新產品 | | |
| TC-16 | 刪除產品 | | |
| TC-20 | 上傳圖片文件 | | |
| TC-21 | 添加圖片 | | |
| TC-22 | 列出圖片 | | |
| TC-23 | 設為主圖 | | |
| TC-24 | 刪除圖片 | | |
| TC-30 | 創建價目表 | | |
| TC-31 | 列出價目表 | | |
| TC-32 | 激活價目表 | | |
| TC-33 | 添加明細行 | | |
| TC-34 | 列出明細 | | |
| TC-35 | 批量更新明細 | | |
| TC-36 | 版本升級 | | |
| TC-37 | 刪除價目表 | | |
| TC-38 | daily_update 過濾 | | |
| TC-40 | 上傳文檔文件 | | |
| TC-41 | 登記文檔 | | |
| TC-42 | 列出文檔 | | |
| TC-43 | 按類型過濾 | | |
| TC-44 | 刪除文檔 | | |
| TC-50 | ProductCatalogSkill | | |
| TC-51 | PriceListSkill | | |
| TC-52 | DocumentSkill | | |
| TC-53 | ProductImageSkill | | |
| TC-54 | 錯誤操作處理 | | |
| TC-60 | 企業隔離 | | |
| TC-61 | 不存在產品 404 | | |
| TC-62 | 不存在價目表 404 | | |
| TC-63 | 缺少參數 422 | | |
| TC-64 | 空名稱 422 | | |
| TC-65 | 無效 file_id 400 | | |
| TC-66 | 價目表 404 | | |
| TC-70 | Cron 啟動 (mgmt) | | |
| TC-71 | Cron 跳過 (sales) | | |

## 問題記錄

| 編號 | 問題描述 | 嚴重度 | 狀態 |
|---|---|---|---|
| | | | |

## 最終結論

- [ ] 全部 PASS — 可上線
- [ ] 有 FAIL — 需修復後重新驗證
```
