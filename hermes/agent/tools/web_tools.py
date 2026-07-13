"""Web 工具：HTTP 请求与网页搜索。

本模块提供以下工具函数：
- ``http_request``：带反爬升级策略的 HTTP 请求，返回带质量标签的响应文本
- ``web_search``：通过百度千帆 AI 搜索 API 执行网页搜索，返回结构化结果

模块内部维护域名状态缓存（Cookie/Referer），在成功访问后持久化白名单头，
以便后续请求复用。支持三级反爬升级策略：
1. httpx + 合并头（Tier 1，默认浏览器头 + 域名缓存 + 用户自定义）
2. 增强头重试（Tier 2，403/412 反爬触发）
3. curl_cffi Chrome TLS 指纹伪装（Tier 3，可选依赖）

成功时自动将 Cookie/Referer 等白名单头存入域名状态缓存，HTML 响应会被
转换为纯文本摘要以减少 token 消耗。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# HTTP 响应体最大字符数（超出截断）
_MAX_HTTP_BODY_CHARS = 50000

# 域名状态持久化路径（存储成功访问过的域名 Cookie/Referer）
_DOMAIN_STATE_PATH = "data/domain_state.json"
# 域名状态文件锁（线程安全）
_domain_state_lock = threading.Lock()

# 可持久化的 header 白名单（不含敏感字段）
_PERSISTABLE_HEADERS = frozenset({"cookie", "referer", "origin", "user-agent"})

# P1 可选依赖：curl_cffi 提供 Chrome TLS 指纹伪装
try:
    from curl_cffi import requests as curl_requests  # noqa: F401
    _CURL_CFFI_AVAILABLE = True
except ImportError:
    _CURL_CFFI_AVAILABLE = False

# 反爬虫检测关键词（用于工具内部判断质量标签）
_ANTI_CRAWLER_QUALITY_RE = re.compile(
    "|".join(re.escape(kw) for kw in [
        "captcha", "验证码", "人机验证", "安全验证", "access denied",
        "请求被拒绝", "too many requests", "频率限制", "访问频率",
        "triggered our security", "security check", "anti-bot",
        "请完成安全验证", "verify you are human", "are you a robot",
    ]),
    re.IGNORECASE,
)


# 简单 HTML 转纯文本（正则实现，无外部依赖）
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style|noscript)[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _html_to_plain_text(html: str) -> str:
    """将 HTML 转换为纯文本摘要。

    1. 提取 ``<title>`` 内容作为标题行
    2. 移除 ``<script>/<style>/<noscript>`` 块
    3. 移除其余所有 HTML 标签
    4. 解码 HTML 实体（``&amp;`` / ``&#x27;`` 等）
    5. 压缩连续空白为单个空格

    返回:
        ``[Title: 页面标题]`` + 页面可见文本（压缩后），
        无 title 时仅返回文本。
    """
    if not html:
        return ""

    # 提取 title
    title_match = _TITLE_RE.search(html)
    title = title_match.group(1).strip() if title_match else ""

    # 移除 script / style / noscript 块
    text = _SCRIPT_STYLE_RE.sub("", html)

    # 移除剩余 HTML 标签
    text = _HTML_TAG_RE.sub("", text)

    # 解码 HTML 实体
    import html as _html_mod
    text = _html_mod.unescape(text)

    # 压缩空白
    text = re.sub(r"\s+", " ", text).strip()

    if title:
        return f"[Title: {title}]\n{text}"
    return text


def _extract_domain(url: str) -> str:
    """从 URL 提取域名（如 ``bilibili.com``）。

    参数:
        url: 完整 URL。

    返回:
        域名部分（小写）或空字符串（解析失败）。
    """
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        return host.lower()
    except Exception:
        return ""


def _load_domain_state() -> dict:
    """从 JSON 文件加载域名状态。线程安全，缺失或损坏时返回空 dict。"""
    with _domain_state_lock:
        try:
            if os.path.exists(_DOMAIN_STATE_PATH):
                with open(_DOMAIN_STATE_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
        except (json.JSONDecodeError, OSError, IOError) as e:
            logger.warning("加载域名状态失败: %s", e)
    return {}


def _save_domain_state(state: dict) -> None:
    """持久化域名状态到 JSON 文件。线程安全。"""
    with _domain_state_lock:
        try:
            os.makedirs(os.path.dirname(_DOMAIN_STATE_PATH), exist_ok=True)
            with open(_DOMAIN_STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.warning("保存域名状态失败: %s", e)


def _classify_quality(status_code: int, body: str) -> str:
    """根据状态码和 body 返回质量标签。

    返回:
        ``"OK"`` — 正常
        ``"BLOCKED"`` — 反爬虫/风控
        ``"ERROR {code}"`` — 永久错误
        ``"TRANSIENT"`` — 临时错误
    """
    if status_code in (403, 412):
        return "BLOCKED"
    if status_code == 429 or status_code >= 500:
        return "TRANSIENT"
    if status_code in (404, 410):
        return f"ERROR {status_code}"
    if 200 <= status_code < 400:
        # 检查 body 反爬关键词
        if _ANTI_CRAWLER_QUALITY_RE.search(body):
            return "BLOCKED"
        return "OK"
    # 其他状态码
    return f"ERROR {status_code}"


def _build_enhanced_headers(
    url: str, base_headers: dict, domain_state: Optional[dict] = None,
) -> dict:
    """在基础 headers 之上追加从 URL 推导的 Referer/Origin 和域名缓存 Cookie。

    参数:
        url: 请求 URL。
        base_headers: 已有 headers（会被复制）。
        domain_state: 可选的域名状态 dict。

    返回:
        增强后的 headers dict。
    """
    enhanced = dict(base_headers)

    # 收集已有键（大小写不敏感）
    existing_keys = {k.lower() for k in enhanced}

    # 从 URL 推导 Referer 与 Origin
    try:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.hostname}"
        if "referer" not in existing_keys:
            enhanced["Referer"] = origin + "/"
            existing_keys.add("referer")
        if "origin" not in existing_keys:
            enhanced["Origin"] = origin
            existing_keys.add("origin")
    except Exception:
        pass

    # 注入域名缓存的 Cookie/Referer
    if domain_state:
        domain = _extract_domain(url)
        entry = domain_state.get(domain, {})
        if isinstance(entry, dict):
            cached_cookie = entry.get("cookie")
            if cached_cookie and "cookie" not in existing_keys:
                enhanced["Cookie"] = cached_cookie
                existing_keys.add("cookie")
            cached_referer = entry.get("referer")
            if cached_referer and "referer" not in existing_keys:
                enhanced["Referer"] = cached_referer
                existing_keys.add("referer")

    return enhanced


def _persist_success_headers(url: str, headers: dict, save_state: bool) -> None:
    """将成功的 headers 中可持久化字段存入域名状态。

    参数:
        url: 请求 URL。
        headers: 本次请求使用的完整 headers。
        save_state: 是否持久化。为 False 时跳过。
    """
    if not save_state:
        return
    domain = _extract_domain(url)
    if not domain:
        return

    entry: dict = {}
    for key_lower in _PERSISTABLE_HEADERS:
        # 遍历 headers 查找匹配键（大小写不敏感）
        for k, v in headers.items():
            if k.lower() == key_lower and v:
                entry[key_lower] = v
                break
    if not entry:
        return

    state = _load_domain_state()
    # 合并到已有条目
    existing = state.get(domain, {})
    if isinstance(existing, dict):
        existing.update(entry)
    else:
        existing = entry
    from datetime import datetime
    existing["last_success"] = datetime.now().isoformat()
    state[domain] = existing
    _save_domain_state(state)


def http_request(
    url: str,
    method: str = "GET",
    headers: Optional[dict] = None,
    timeout: int = 30,
    no_cache: bool = False,
    save_state: bool = True,
) -> str:
    """HTTP 请求（升级阶梯版），返回带质量标签的响应文本。

    升级策略：
    1. httpx + 合并头（默认浏览器头 + 域名缓存 + 用户自定义） → Tier 1
    2. 若被反爬（403/412），自动以增强头重试一次 → Tier 2
    3. 若仍被反爬且 curl_cffi 可用，以 Chrome TLS 指纹重试 → Tier 3
    4. 仍失败则返回 ``[BLOCKED]`` 明确信号

    成功时自动将 Cookie/Referer 等白名单头存入域名状态缓存。

    参数:
        url: 请求 URL。
        method: HTTP 方法，默认 GET。
        headers: 可选自定义请求头 dict，覆盖默认头。
        timeout: 超时秒数，默认 30。
        no_cache: 跳过域名状态缓存（不读取已保存的 Cookie/Referer）。
        save_state: 成功后是否将白名单头持久化到域名状态。

    返回:
        首行为质量标签（``[OK]`` / ``[BLOCKED]`` / ``[ERROR nnn]`` /
        ``[TRANSIENT]``），随后是 ``[HTTP nnn]`` 等元数据行，空行后为响应体。
    """
    # Phase 9+ 中断检查
    try:
        from ._cancel_context import current_cancel_event
        ce = current_cancel_event.get()
        if ce is not None and ce.is_set():
            return "[TRANSIENT]\n[HTTP 请求被用户中断]"
    except ImportError:
        pass

    # 合并默认头 + 域名缓存 + 用户自定义
    merged_headers = dict(_DEFAULT_HTTP_HEADERS)
    if not no_cache:
        domain_state = _load_domain_state()
        domain = _extract_domain(url)
        entry = domain_state.get(domain, {}) if domain_state else {}
        if isinstance(entry, dict):
            for key in _PERSISTABLE_HEADERS:
                val = entry.get(key)
                if val and key not in {k.lower() for k in merged_headers}:
                    merged_headers[key.capitalize()] = val
    else:
        domain_state = {}

    if headers:
        merged_headers.update(headers)

    # ── Tier 1: httpx 标准请求 ──
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.request(method, url, headers=merged_headers)
            status_code = response.status_code
            content_type = response.headers.get("content-type", "unknown")
            body = response.text
    except Exception as e:
        return f"[TRANSIENT]\nHTTP 请求失败: {e}"

    quality = _classify_quality(status_code, body)

    # ── Tier 2: 增强头重试（仅反爬虫/风控触发） ──
    if quality == "BLOCKED":
        enhanced = _build_enhanced_headers(url, merged_headers, domain_state)
        # 仅当增强头与原始头不同时才尝试
        if enhanced != merged_headers:
            try:
                with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                    resp2 = client.request(method, url, headers=enhanced)
                    sc2 = resp2.status_code
                    ct2 = resp2.headers.get("content-type", "unknown")
                    body2 = resp2.text
                q2 = _classify_quality(sc2, body2)
                if q2 == "OK":
                    quality = "OK"
                    status_code = sc2
                    content_type = ct2
                    body = body2
                    merged_headers = enhanced  # 后续持久化增强头
            except Exception:
                pass  # Tier 2 失败，保持 BLOCKED

        # ── Tier 3: curl_cffi Chrome TLS 指纹 ──
        if quality == "BLOCKED" and _CURL_CFFI_AVAILABLE:
            try:
                cf_headers = _build_enhanced_headers(url, merged_headers, domain_state)
                cf_response = curl_requests.get(
                    url,
                    headers=cf_headers,
                    timeout=timeout,
                    impersonate="chrome120",
                )
                sc3 = cf_response.status_code
                ct3 = cf_response.headers.get("content-type", "unknown")
                body3 = cf_response.text
                q3 = _classify_quality(sc3, body3)
                if q3 == "OK":
                    quality = "OK"
                    status_code = sc3
                    content_type = ct3
                    body = body3
                    merged_headers = cf_headers
            except Exception:
                pass  # Tier 3 失败，保持 BLOCKED

    # ── 持久化成功 headers ──
    if quality == "OK" and save_state:
        _persist_success_headers(url, merged_headers, save_state=True)

    # ── HTML → 纯文本转换（仅 text/html，截断前） ──
    # 注意：反爬分类（_classify_quality）已在 Tier 1-3 对原始 body 完成，
    # 此处清洗不影响反爬判断结果
    if "html" in content_type.lower():
        orig_len = len(body)
        body = _html_to_plain_text(body)
        logger.debug(
            "HTML 响应已转换: 原始 %d chars → 纯文本 %d chars",
            orig_len, len(body),
        )

    # ── 构造输出 ──
    truncated = False
    if len(body) > _MAX_HTTP_BODY_CHARS:
        body = body[:_MAX_HTTP_BODY_CHARS]
        truncated = True

    prefix = f"[{quality}]"
    lines = [prefix, f"[HTTP {status_code}]", f"[URL: {url}]", f"[Type: {content_type}]"]
    result = "\n".join(lines) + "\n\n" + body
    if truncated:
        result += "\n... (响应体已截断)"
    return result


def _search_baidu(query: str, top_k: int = 5, api_key: str = "") -> str:
    """通过百度千帆 AppBuilder AI 搜索 API 搜索网页，返回结构化结果。

    API 文档：https://ai.baidu.com/ai-doc/AppBuilder/pmaxd1hvy
    需要配置 ``BAIDU_API_KEY`` 环境变量（AppBuilder API Key 或 BCE IAM Key）。
    免费额度：每日 100 次查询。

    API 使用 messages 格式（类 chat 接口），Bearer Token 认证。
    BCE IAM Key（bce-v3/ALTAK-{ak}/{sk}）可直接作为 Bearer Token 使用。
    """
    if not api_key:
        return "[ERROR] 百度 API Key 未配置"

    # 调用千帆 AI 搜索 API（网页搜索，messages 格式）
    try:
        resp = httpx.post(
            "https://qianfan.baidubce.com/v2/ai_search/web_search",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "messages": [{"role": "user", "content": query}],
                "top_n": top_k,
            },
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as e:
        body = e.response.text[:300] if e.response else ""
        return f"[TRANSIENT] 百度搜索 API 请求失败 (HTTP {e.response.status_code}): {body}"
    except Exception as e:
        return f"[TRANSIENT] 百度搜索 API 请求失败: {e}"

    # 解析搜索结果
    # 千帆 AI 搜索 API 返回格式: { "references": [...], ... }
    # 每条 reference: { "id": int, "url": str, "title": str, "date": str, "content": str }
    refs = data.get("references", data.get("search_results", data.get("results", [])))
    if not isinstance(refs, list) or not refs:
        return "[OK]\n未找到相关结果。\n[源: 百度]"

    lines = ["[OK]", "[源: 百度]", f"[查询: {query}]"]
    for i, ref in enumerate(refs[:top_k], 1):
        title = ref.get("title", "") or ""
        url = ref.get("url", ref.get("link", "")) or ""
        content = ref.get("content", ref.get("snippet", ref.get("desc", ""))) or ""
        # 截断超长摘要（单条不超过 300 字）
        if len(content) > 300:
            content = content[:297] + "..."
        lines.append(f"\n{i}. {title}")
        if url:
            lines.append(f"   URL: {url}")
        if content:
            lines.append(f"   摘要: {content}")
    return "\n".join(lines)


def web_search(
    query: str,
    top_k: int = 5,
) -> str:
    """通过百度搜索 API 执行网页搜索，返回结构化结果摘要（标题 + URL + 摘要片段）。

    与 ``web_fetch`` 的区别：``web_search`` 通过百度搜索引擎 API 返回精选结果，
    LLM 无需自行猜测 URL；``web_fetch`` 用于获取指定 URL 的完整页面内容。
    搜索公开信息应优先使用此工具。

    需要配置 ``BAIDU_API_KEY`` 环境变量（或 config.yaml 中 web_search.baidu_api_key）。
    免费额度：每日 100 次查询。

    参数:
        query: 搜索关键词（支持中文、英文等自然语言查询）。
        top_k: 返回结果条数，默认 5，最大 10。

    返回:
        首行为 ``[OK]`` / ``[ERROR]`` 质量标签及搜索来源，
        随后为结构化结果列表（标题 + URL + 摘要）。
    """
    if top_k < 1 or top_k > 10:
        top_k = 5

    # 从环境变量或 config 读取百度 API Key
    baidu_key = os.environ.get("BAIDU_API_KEY", "")
    try:
        from ...config import load_config
        cfg = load_config()
        web_cfg = cfg.get("web_search", {}) or {}
        if not baidu_key:
            baidu_key = web_cfg.get("baidu_api_key", "") or ""
    except Exception:
        pass

    if not baidu_key:
        return "[ERROR] BAIDU_API_KEY 未配置。请设置 BAIDU_API_KEY 环境变量或 config.yaml 中 web_search.baidu_api_key。"

    return _search_baidu(query, top_k, baidu_key)


__all__ = [
    "http_request",
    "web_search",
    "_html_to_plain_text",
    "_extract_domain",
    "_load_domain_state",
    "_save_domain_state",
    "_classify_quality",
    "_build_enhanced_headers",
    "_persist_success_headers",
    "_search_baidu",
]
