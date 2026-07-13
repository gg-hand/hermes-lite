"""Pre-seed test data for Hermes Lite performance benchmarks.

Creates isolated test directories with pre-populated:
- ChromaDB: 1000 random vectors (various topics)
- SQLite: Pre-seeded sessions with 50 messages
- Memory.md: User profile text file
- History JSONL: Pre-populated conversation history
"""

from __future__ import annotations

import json
import logging
import os
import random
import shutil
import sqlite3
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

# We need to ensure we can import from hermes-lite src
SRC_DIR = str(Path(__file__).resolve().parent.parent / "hermes")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


def seed_chromadb(persist_path: str, num_vectors: int = 1000) -> None:
    """Pre-populate ChromaDB with random vectors."""
    from chroma_store import ChromaMemoryStore

    logger.info(f"Seeding ChromaDB at {persist_path} with {num_vectors} vectors...")

    # Clean any existing data
    chroma_dir = Path(persist_path)
    if chroma_dir.exists():
        shutil.rmtree(chroma_dir)
    chroma_dir.mkdir(parents=True, exist_ok=True)

    store = ChromaMemoryStore(persist_path=persist_path)

    topics = [
        "programming", "python", "data science", "machine learning",
        "web development", "database", "cloud computing", "security",
        "networking", "algorithms", "design patterns", "testing",
        "devops", "containers", "API design",
    ]

    batch_size = 100
    batch_docs = []
    batch_ids = []
    batch_metadatas = []

    t0 = time.perf_counter()
    for i in range(num_vectors):
        topic = topics[i % len(topics)]
        text = f"Test memory {i}: This is a seeded memory about {topic}."
        if i % 3 == 0:
            text += f" The user has strong experience with {topic}."
        elif i % 3 == 1:
            text += f" The user recently learned about {topic}."
        else:
            text += f" The user prefers using {topic} for their work."

        batch_docs.append(text)
        batch_ids.append(f"mem_{i}")
        batch_metadatas.append({
            "importance": round(random.uniform(0.1, 1.0), 4),
            "type": random.choice(["fact", "user_profile", "preference", "experience"]),
            "timestamp": "2024-06-01T00:00:00",
            "namespace": "user",
        })

        if len(batch_docs) >= batch_size or i == num_vectors - 1:
            store.collection.add(
                ids=batch_ids,
                documents=batch_docs,
                metadatas=batch_metadatas,
            )
            batch_docs.clear()
            batch_ids.clear()
            batch_metadatas.clear()
            logger.info(f"  Seeded {i + 1}/{num_vectors} vectors...")

    elapsed = time.perf_counter() - t0
    actual_count = store.collection.count()
    logger.info(f"ChromaDB seeding complete: {actual_count} vectors in {elapsed:.2f}s")


def seed_sqlite(db_path: str) -> None:
    """Pre-populate SQLite with sessions and messages."""
    from sqlite_log import SessionLogger

    logger.info(f"Seeding SQLite at {db_path}...")

    db_file = Path(db_path)
    if db_file.exists():
        db_file.unlink()
    db_file.parent.mkdir(parents=True, exist_ok=True)

    logger.info("  Creating SessionLogger...")
    sl = SessionLogger(str(db_path))
    time.sleep(0.1)

    logger.info("  Creating sessions...")
    for sid in range(5):
        session_id = f"bench_session_{sid}"
        sl.create_session(session_id)
        # Log 10 messages per session
        for mid in range(10):
            role = "user" if mid % 2 == 0 else "assistant"
            if mid % 3 == 0:
                content = f"Pre-seeded message {mid} in session {sid}: This is a longer message with some padding text to make it more realistic for a conversation about programming topics."
            else:
                content = f"Pre-seeded brief message {mid} in session {sid}."
            sl.log_message(session_id, role, content)
        logger.info(f"  Session {session_id}: 10 messages seeded")

    actual_count = len(sl.list_sessions())
    logger.info(f"SQLite seeding complete: {actual_count} sessions")


def seed_memory_md(md_path: str) -> None:
    """Create a pre-populated memory.md file."""
    logger.info(f"Creating memory.md at {md_path}...")

    md_file = Path(md_path)
    md_file.parent.mkdir(parents=True, exist_ok=True)

    content = """# 用户画像

## 个人信息
- 用户是一名软件工程师
- 使用 Python 作为主要编程语言
- 偏好 VS Code 编辑器
- 使用 Windows 操作系统
- 关注 AI / 机器学习领域

## 技术偏好
- 熟悉 FastAPI、Django 等 Web 框架
- 有数据库设计和优化经验
- 习惯使用 Git 进行版本控制
- 了解 Docker 容器化部署

## 兴趣
- 喜欢探索新技术
- 关注性能优化
- 有开源项目贡献经验
- 对系统架构设计感兴趣

## 习惯
- 工作时段：上午 9:00 - 下午 6:00
- 偏好简洁的代码风格
- 重视代码质量和测试覆盖
"""
    md_file.write_text(content, encoding="utf-8")
    logger.info(f"memory.md created: {len(content)} chars")


def seed_history(persist_dir: str, num_sessions: int = 3, msgs_per_session: int = 20) -> None:
    """Pre-populate history JSONL files."""
    logger.info(f"Seeding history at {persist_dir}...")

    hist_dir = Path(persist_dir)
    if hist_dir.exists():
        shutil.rmtree(hist_dir)
    hist_dir.mkdir(parents=True, exist_ok=True)

    sample_texts = [
        "你好，今天天气怎么样？",
        "帮我查找一些资料。",
        "能不能帮我写一个Python函数？",
        "解释一下FastAPI的工作原理。",
        "如何优化数据库查询性能？",
        "推荐一些学习机器学习的资源。",
        "帮我 review 一下这段代码。",
        "什么是微服务架构？",
        "Docker 和虚拟机有什么区别？",
        "如何在 Python 中处理并发？",
    ]

    for sid in range(num_sessions):
        session_id = f"bench_session_{sid}"
        file_path = hist_dir / f"{session_id}.jsonl"

        with open(file_path, "w", encoding="utf-8") as f:
            for mid in range(msgs_per_session):
                role = "user" if mid % 2 == 0 else "assistant"
                content = sample_texts[mid % len(sample_texts)]
                if mid % 4 == 0:
                    # Add longer messages
                    content += " 希望能得到一个详细的解答，最好有代码示例和实际应用场景。"
                entry = {
                    "role": role,
                    "content": content,
                    "timestamp": f"2024-06-01T00:{mid:02d}:00",
                }
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        logger.info(f"  History {session_id}: {msgs_per_session} messages")


def prepare_directories(base_dir: str = "data/test_profiling") -> Path:
    """Create and return the base test data directory."""
    data_dir = Path(base_dir)
    if data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def main():
    """Seed all test data stores."""
    base_dir = prepare_directories("data/test_profiling")

    chroma_path = str(base_dir / "chroma")
    sqlite_path = str(base_dir / "sessions.db")
    md_path = str(base_dir / "memory.md")
    history_path = str(base_dir / "history")

    total_t0 = time.perf_counter()

    seed_chromadb(chroma_path, num_vectors=1000)
    seed_sqlite(sqlite_path)
    seed_memory_md(md_path)
    seed_history(history_path)

    total_elapsed = time.perf_counter() - total_t0
    logger.info(f"\n{'='*60}")
    logger.info(f"Seed data complete in {total_elapsed:.2f}s")
    logger.info(f"  ChromaDB:   {chroma_path}")
    logger.info(f"  SQLite:     {sqlite_path}")
    logger.info(f"  Memory.md:  {md_path}")
    logger.info(f"  History:    {history_path}")
    logger.info(f"{'='*60}")


if __name__ == "__main__":
    main()
