"""文件上传与 ETL 管道模块。

提供完整的文件上传 → 解析 → 分块 → 嵌入 → 混合检索存储 → 上下文注入链路。

组件：
- UploadManager: 上传管理与 SQLite 持久化（全局 SHA256 去重）
- WaterfallParser: 瀑布式解析器（L1 库解析 → L2 编码修复 → L3 LLM 降级）
- DocumentChunker: 段落感知文档分块
- ETLEngine: ETL 管道编排 + 混合检索（Vector + FTS5 + RRF）
- FileContextInjector: 文件摘要上下文注入器
"""
