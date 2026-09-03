# 产品目录管理 SKILL.md

## 任务
管理企业产品目录信息，支持创建、查询、更新和 XLSX 批量导入。

## 能力
- list_products：列出产品
- get_product：产品详情
- create_product：创建产品
- update_product：更新产品
- delete_product：删除产品
- search_products：搜索产品
- import_products：XLSX 批量导入

## 输入参数
- action（必需）：要执行的操作
- enterprise_id（必需）：企业 ID

## 限制
- 产品信息通过底座 API 管理
- XLSX 导入文件需先通过汇川上传
