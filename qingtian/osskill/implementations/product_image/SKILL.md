# 产品图片管理 SKILL.md

## 任务
管理产品图片，支持上传、设为主图、删除等操作。

## 能力
- list_images：列出产品图片
- upload_image：上传图片
- set_primary_image：设为主图
- delete_image：删除图片

## 输入参数
- action（必需）：要执行的操作
- product_id（必需）：产品 ID

## 限制
- 图片文件需先通过汇川上传后再绑定到产品
