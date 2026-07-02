---
name: blog_monitor_joyehuang
version: 1.0.0
description: 监控 joyehuang.me/blog 的 RSS 更新，检测到新文章时通过邮件通知
input_schema:
  type: object
  properties: {}
---

# Blog Monitor - Joyehuang

监控 https://www.joyehuang.me/rss.xml 的更新情况。

检测到新文章时，会通过 SMTP 发送邮件到用户的 QQ 邮箱。

## 工作原理

1. 拉取 RSS feed，提取所有文章的标题、链接、发布日期
2. 与本地快照文件（snapshot.json）对比，找出新增文章
3. 如有新增，通过 SMTP 发送邮件通知
4. 更新快照文件

## 输出

返回新增文章列表（标题+链接+日期），如无新增则返回无更新提示。
