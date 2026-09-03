# 报价单管理 SKILL.md

## 任务
管理企业报价单，支持创建、更新、版本升级和 XLSX 导入。

## 能力
- list_price_lists：列出报价单
- get_price_list：查看报价单详情
- create_price_list：创建新报价单
- update_price_list：更新报价单
- activate_price_list：激活报价单
- supersede_price_list：版本升级
- delete_price_list：删除报价单
- list_items：列出报价单明细
- add_item：添加明细项
- batch_update_items：批量更新明细
- import_price_list：XLSX 导入

## 输入参数
- action（必需）：要执行的操作
- enterprise_id（必需）：企业 ID

## 限制
- 报价单版本管理遵循 supersede 模式，不直接修改已生效报价
