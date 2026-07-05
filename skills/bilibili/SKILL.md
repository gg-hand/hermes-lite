---
description: B站（哔哩哔哩）数据查询工具集
name: bilibili
requires: ["bilibili-api-python"]
version: 1.1.0
---

# Bilibili Skill

提供 B站（哔哩哔哩）数据查询能力，基于 `bilibili-api-python` 库实现。

## 依赖

``pip install bilibili-api-python``

## 函数列表

脚本资源位于 `scripts/bilibili.py`，可通过 `skill__resource(name="bilibili", rel_path="scripts/bilibili.py")` 读取源码。

### bilibili_hot() -> str
获取B站当前热门视频列表，返回 TOP20。

### bilibili_search(keyword: str, page: int = 1, order: str = "default") -> str
搜索B站视频内容。order 可选：default(综合), pubdate(最新发布), click(播放最多), dm(弹幕最多), stow(收藏最多)。

### bilibili_video_info(bvid: str) -> str
获取单个视频的详细信息，含播放量、点赞、弹幕等数据。

### bilibili_user_info(uid: int) -> str
获取用户/UP主信息，含粉丝数、关注数、视频数等。

### bilibili_rank(rid: int = 1) -> str
获取分区排行榜，rid=1 为全站。

## 调用方式

本 Skill 的函数需在 Python 环境中调用（依赖 bilibili-api-python 库），不支持 CLI 直接调用。如需调用，请通过 `bash_exec` 执行 Python 脚本：

```bash
python -c "from skills.bilibili.scripts.bilibili import bilibili_hot; print(bilibili_hot())"
```

## 注意事项

- 所有工具返回格式化的文本结果
- 无需额外配置即可使用公开数据
- 如需要登录态数据（如收藏夹），需在 .env 中配置 B站 Cookie
