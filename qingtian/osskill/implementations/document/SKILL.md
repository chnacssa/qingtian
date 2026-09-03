# 企业文档管理 SKILL.md

## 任务
管理企业公共文档，包括资质证书、合同模板、样本文件等。

## 能力
- list_documents：列出企业文档
- get_document：查看文档详情
- upload_document：上传新文档
- update_document：更新文档信息
- delete_document：删除文档

## 输入参数
- action（必需）：要执行的操作，可选 list_documents / get_document / upload_document / update_document / delete_document
- enterprise_id（必需）：企业 ID
- 其他参数根据具体操作而定

## 限制
- 仅管理企业公共文档，不涉及个人文件
- 所有文档操作需通过底座 API 完成
