"""一次性清理脚本：移除 ChromaDB 向量库中已有的 type=user_profile 条目。

背景：
    user_profile 类事实原先同时写入向量库与 memory.md，导致手改 memory.md
    后向量库残留脏数据。重构后 user_profile 仅写入 memory.md（直接注入
    system prompt 缓存命中区），向量库不再需要这类条目。

    retrieval.py 已对 query_memory 结果做防御性过滤，本脚本进一步清理
    向量库中的残留数据，避免无效资源占用。

使用方式:
    cd hermes-lite
    python cleanup_user_profile_vectors.py          # 预览（dry-run）
    python cleanup_user_profile_vectors.py --apply  # 实际删除
"""

from __future__ import annotations

import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# 复用测试 mock 装配，避免强制加载 chromadb 重依赖（实际运行时再加载）
from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from src.storage.chroma_store import ChromaMemoryStore  # noqa: E402


def main() -> None:
    apply = "--apply" in sys.argv

    persist_path = os.path.join(_PROJECT_ROOT, "data", "chroma")
    store = ChromaMemoryStore(persist_path=persist_path)

    all_memories = store.get_all_memories()
    profile_items = [
        m for m in all_memories
        if str(m.get("metadata", {}).get("type", "")).lower() == "user_profile"
    ]

    print(f"向量库总数: {len(all_memories)}")
    print(f"user_profile 残留: {len(profile_items)}")
    print()

    if not profile_items:
        print("无需清理。")
        return

    print("待清理条目（前 20 条）:")
    for m in profile_items[:20]:
        content = (m.get("content") or "")[:80]
        print(f"  [{m.get('id')}] {content}")
    if len(profile_items) > 20:
        print(f"  ... 共 {len(profile_items)} 条")

    print()
    if not apply:
        print("[dry-run] 未应用 --apply 参数，仅预览不删除。")
        return

    print("[apply] 开始删除...")
    deleted = 0
    for m in profile_items:
        try:
            store.delete_memory(m["id"])
            deleted += 1
        except Exception as e:
            print(f"  删除失败 {m.get('id')}: {e}")

    print(f"已删除 {deleted} / {len(profile_items)} 条 user_profile 残留。")

    try:
        store.close()
    except Exception:
        pass


if __name__ == "__main__":
    main()
