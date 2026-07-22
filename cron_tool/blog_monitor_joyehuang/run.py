#!/usr/bin/env python3
"""
Blog Monitor - joyehuang.me
监控 RSS 更新，检测到新文章时通过邮件通知

执行协议：
  stdin:  JSON {"input": {...}, "context": {...}}
  stdout: JSON {"result": "..."} 或 {"error": "..."}
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
import xml.etree.ElementTree as ET
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

RSS_URL = "https://www.joyehuang.me/rss.xml"
TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT_FILE = os.path.join(TOOL_DIR, "snapshot.json")


def get_smtp_config() -> dict:
    """从环境变量读取 SMTP 配置（6.2）。

    缺失时使用默认值（项目记忆约束：API 密钥不硬编码）。
    环境变量：
        SMTP_HOST: SMTP 服务器地址，默认 smtp.qq.com
        SMTP_PORT: SMTP 端口，默认 465
        SMTP_USER: 发件人邮箱，默认空字符串
        SMTP_PASSWORD: 发件人授权码，默认空字符串
        NOTIFY_EMAIL: 收件人邮箱，缺省回退到 SMTP_USER
    """
    return {
        "smtp_host": os.environ.get("SMTP_HOST", "smtp.qq.com"),
        "smtp_port": int(os.environ.get("SMTP_PORT", "465")),
        "sender_email": os.environ.get("SMTP_USER", ""),
        "sender_password": os.environ.get("SMTP_PASSWORD", ""),
        "receiver_email": os.environ.get("NOTIFY_EMAIL", os.environ.get("SMTP_USER", "")),
        "use_tls": True,
    }


# 模块加载时一次性读取配置（保持向后兼容 SMTP_HOST 等模块级常量）
SMTP_CONFIG = get_smtp_config()
SMTP_HOST = SMTP_CONFIG["smtp_host"]
SMTP_PORT = SMTP_CONFIG["smtp_port"]
SENDER_EMAIL = SMTP_CONFIG["sender_email"]
SENDER_PASSWORD = SMTP_CONFIG["sender_password"]
RECEIVER_EMAIL = SMTP_CONFIG["receiver_email"]


def fetch_rss() -> list[dict]:
    """抓取 RSS 并返回文章列表"""
    req = urllib.request.Request(RSS_URL, headers={
        "User-Agent": "TeageLiu-BlogMonitor/1.0"
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = resp.read()
    root = ET.fromstring(data)
    items = []
    for item in root.findall(".//item"):
        title = item.find("title")
        link = item.find("link")
        pub_date = item.find("pubDate")
        items.append({
            "title": (title.text or "").strip() if title is not None else "",
            "link": (link.text or "").strip() if link is not None else "",
            "pub_date": (pub_date.text or "").strip() if pub_date is not None else "",
        })
    return items


def load_snapshot() -> set:
    """加载已知文章链接的 set"""
    if not os.path.exists(SNAPSHOT_FILE):
        return set()
    with open(SNAPSHOT_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return set(data.get("known_links", []))


def save_snapshot(known_links: set) -> None:
    """保存快照"""
    with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "known_links": sorted(known_links),
            "updated_at": datetime.now().isoformat()
        }, f, ensure_ascii=False, indent=2)


def send_email(new_articles: list[dict]) -> None:
    """发送邮件通知"""
    count = len(new_articles)
    subject = f"Blog Update: joyehuang.me has {count} new post(s)"

    parts = [f"<h2>Found {count} new article(s)</h2><hr>"]
    for art in new_articles:
        parts.append(
            f'<p><b><a href="{art["link"]}">{art["title"]}</a></b><br>'
            f'<small>{art["pub_date"]}</small></p>'
        )

    msg = MIMEMultipart("alternative")
    msg["From"] = SENDER_EMAIL
    msg["To"] = RECEIVER_EMAIL
    msg["Subject"] = subject
    msg.attach(MIMEText("\n".join(parts), "html", "utf-8"))

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx) as server:
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.sendmail(SENDER_EMAIL, [RECEIVER_EMAIL], msg.as_string())


def main() -> dict:
    """执行监控逻辑，返回结果 dict"""
    current_articles = fetch_rss()
    current_links = {a["link"] for a in current_articles}

    known_links = load_snapshot()

    if not known_links:
        # 首次运行：初始化快照，不发邮件
        save_snapshot(current_links)
        return {
            "status": "initialized",
            "message": f"首次初始化完成，已记录 {len(current_links)} 篇文章",
            "total_articles": len(current_links),
        }

    # 找出新增文章
    new_links = current_links - known_links
    if not new_links:
        return {
            "status": "no_update",
            "message": f"无新增文章（共 {len(current_links)} 篇）",
            "total_articles": len(current_links),
        }

    new_articles = [a for a in current_articles if a["link"] in new_links]
    new_articles.reverse()  # 按时间正序

    # 发送邮件
    send_email(new_articles)

    # 更新快照
    save_snapshot(current_links)

    return {
        "status": "updated",
        "message": f"发现 {len(new_articles)} 篇新文章，邮件已发送至 {RECEIVER_EMAIL}",
        "total_articles": len(current_links),
        "new_articles": [
            {"title": a["title"], "link": a["link"], "pub_date": a["pub_date"]}
            for a in new_articles
        ],
    }


if __name__ == "__main__":
    # 遵循 cron_tool 执行协议：stdin JSON → stdout JSON
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"输入 JSON 解析失败: {exc}"}))
        sys.exit(1)

    _ = payload.get("input") or {}  # 本工具无需入参
    _ = payload.get("context") or {}

    try:
        result = main()
        print(json.dumps({"result": result}, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        sys.exit(1)
