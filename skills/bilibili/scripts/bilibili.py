"""Bilibili Skill 脚本资源。

提供 B站数据查询相关工具函数，基于 `bilibili-api-python` 库实现。
依赖：``pip install bilibili-api-python``

调用方式：Python import（需安装依赖）
```python
from skills.bilibili.scripts.bilibili import bilibili_hot, bilibili_search
result = bilibili_hot()
```
"""

from __future__ import annotations

import datetime

from bilibili_api import sync
from bilibili_api import hot as bili_hot
from bilibili_api import search as bili_search
from bilibili_api.search import SearchObjectType, OrderVideo
from bilibili_api import video as bili_video
from bilibili_api import user as bili_user
from bilibili_api import rank as bili_rank


def bilibili_hot() -> str:
    """获取B站当前热门视频列表。

    Returns:
        格式化的热门视频列表文本。
    """
    try:
        result = sync(bili_hot.get_hot_videos())
        videos = result.get("list", [])
        if not videos:
            return "暂无热门视频数据"
        output = ["🔥 B站热门视频 TOP20："]
        for i, v in enumerate(videos[:20], 1):
            title = v.get("title", "未知标题")
            aid = v.get("aid", "")
            play = v.get("play", 0)
            danmaku = v.get("video_review", 0)
            output.append(f"  {i}. {title}")
            output.append(f"     AV:{aid} | 播放:{play} | 弹幕:{danmaku}")
        return "\n".join(output)
    except Exception as e:
        return f"获取热门视频失败: {e}"


def bilibili_search(keyword: str, page: int = 1, order: str = "default") -> str:
    """搜索B站视频。

    Args:
        keyword: 搜索关键词
        page: 页码，从1开始，默认1
        order: 排序方式，可选 "default"(综合), "pubdate"(最新发布), "click"(播放最多), "dm"(弹幕最多), "stow"(收藏最多), "scores"(评分)

    Returns:
        格式化搜索结果文本。
    """
    try:
        order_map = {
            "default": None,
            "pubdate": OrderVideo.PUBDATE,
            "click": OrderVideo.CLICK,
            "dm": OrderVideo.DM,
            "stow": OrderVideo.STOW,
            "scores": OrderVideo.SCORES,
        }
        order_type = order_map.get(order, None)
        result = sync(bili_search.search_by_type(
            keyword=keyword,
            search_type=SearchObjectType.VIDEO,
            order_type=order_type,
            page=page,
        ))
        items = result.get("result", [])
        if not items:
            return f"未找到「{keyword}」相关视频"
        output = [f"🔍 搜索「{keyword}」结果（第{page}页）："]
        for i, item in enumerate(items[:15], 1):
            title = item.get("title", "未知标题")
            title = title.replace("<em class=\"keyword\">", "").replace("</em>", "")
            author = item.get("author", "未知")
            play = item.get("play", 0)
            bvid = item.get("bvid", "")
            output.append(f"  {i}. {title}")
            output.append(f"     作者:{author} | 播放:{play} | BV:{bvid}")
        return "\n".join(output)
    except Exception as e:
        return f"搜索失败: {e}"


def bilibili_video_info(bvid: str) -> str:
    """获取B站视频的详细信息。

    Args:
        bvid: 视频BV号，如 "BV1GJ411x7"

    Returns:
        格式化的视频详细信息文本。
    """
    try:
        v = bili_video.Video(bvid=bvid)
        info = sync(v.get_info())
        title = info.get("title", "未知")
        desc = info.get("desc", "")[:200]
        owner = info.get("owner", {}).get("name", "未知")
        stat = info.get("stat", {})
        view = stat.get("view", 0)
        like = stat.get("like", 0)
        coin = stat.get("coin", 0)
        favorite = stat.get("favorite", 0)
        danmaku = stat.get("danmaku", 0)
        reply = stat.get("reply", 0)
        pub_ts = info.get("pubdate", 0)
        pub_time = datetime.datetime.fromtimestamp(pub_ts).strftime("%Y-%m-%d %H:%M") if pub_ts else "未知"
        return (
            f"📹 {title}\n"
            f"UP主: {owner} | 发布时间: {pub_time}\n"
            f"播放:{view} 👍{like} 🪙{coin} ⭐{favorite} 💬{danmaku} 📝{reply}\n"
            f"BV号: {bvid}\n"
            f"简介: {desc}"
        )
    except Exception as e:
        return f"获取视频信息失败: {e}"


def bilibili_user_info(uid: int) -> str:
    """获取B站用户（UP主）信息。

    Args:
        uid: 用户UID

    Returns:
        格式化的用户信息文本。
    """
    try:
        u = bili_user.User(uid)
        info = sync(u.get_user_info())
        name = info.get("name", "未知")
        sign = info.get("sign", "")
        level = info.get("level", 0)
        follower = info.get("follower", 0)
        following = info.get("following", 0)
        video_count = info.get("video_count", 0)
        like_num = info.get("like_num", 0) or info.get("likes", 0)
        return (
            f"👤 {name} (UID: {uid})\n"
            f"等级: Lv{level}\n"
            f"粉丝: {follower} | 关注: {following} | 视频: {video_count}\n"
            f"获赞: {like_num}\n"
            f"签名: {sign}"
        )
    except Exception as e:
        return f"获取用户信息失败: {e}"


def bilibili_rank(rid: int = 1) -> str:
    """获取B站分区排行榜。

    Args:
        rid: 分区ID，1=全站，其他分区ID参考B站API文档。默认1。

    Returns:
        格式化的排行榜文本。
    """
    try:
        result = sync(bili_rank.get_rank_by_rid(rid=rid))
        if not result:
            return f"分区 rid={rid} 暂无排行数据"
        output = [f"🏆 排行榜 (rid={rid})："]
        for i, item in enumerate(result[:20], 1):
            title = item.get("title", "未知")
            play = item.get("play", 0)
            bvid = item.get("bvid", "")
            output.append(f"  {i}. {title}")
            output.append(f"     播放:{play} | BV:{bvid}")
        return "\n".join(output)
    except Exception as e:
        return f"获取排行榜失败: {e}"
