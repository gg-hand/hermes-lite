"""registry 装配层专项测试(B1+B2+B3,计划 §3)。

验收覆盖:
- 配置声明 → 按序实例化;enabled:false 不注册;未知名启动失败
- setup 失败 = 启动失败(向上抛);setup 收到枝干自己的配置段
- registry.build 幂等可重入(L2 热重载铺路)
- after 钩子收 AfterResponse(B3)
- 目录发现通道(extension_loader,§2.1 标注点8):本文件含 loader 与 registry 目录通道用例
- register_factory = 测试/嵌入注入通道(设计 §2.1):本文件与 runner.py 是唯二合法使用方;生产装载 = extensions_root 目录发现
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from teage_liu2.core.history import SQLiteHistoryStore
from teage_liu2.core.hooks import Branch, HookChain
from teage_liu2.core.pipeline import ChatPipeline
from teage_liu2.core.registry import BranchRegistry
from teage_liu2.core.types import AfterResponse
from .fake_llm import FakeLLMClient


def _cfg(branches: dict) -> dict:
    return {"core": {"branches": branches}}


class LogBranch(Branch):
    """记录生命周期与收到的配置。"""

    def __init__(self, name, log):
        self.name = name
        self.log = log

    async def setup(self, config, core):
        self.log.append((self.name, "setup", dict(config)))

    async def teardown(self):
        self.log.append((self.name, "teardown"))


def test_build_order_and_enabled():
    """验收:配置顺序 = 注册顺序;enabled:false 不注册。"""
    log = []
    reg = BranchRegistry()
    reg.register_factory("a", lambda cfg: LogBranch("a", log))
    reg.register_factory("b", lambda cfg: LogBranch("b", log))

    chain = reg.build(_cfg({
        "b": {"enabled": True, "k": 1},
        "a": {"enabled": False},
    }))
    assert [b.name for b in chain.branches] == ["b"]


def test_unknown_branch_fails():
    """验收:未知名枝干(工厂未注册)→ ValueError 启动失败(防 typo 静默)。"""
    reg = BranchRegistry()
    with pytest.raises(ValueError, match="未知枝干"):
        reg.build(_cfg({"ghost": {"enabled": True}}))


def test_build_idempotent_reentrant():
    """验收(L2 铺路):build 幂等可重入 —— 每次返回全新链,互不残留。"""
    log = []
    reg = BranchRegistry()
    reg.register_factory("a", lambda cfg: LogBranch("a", log))

    chain1 = reg.build(_cfg({"a": {"enabled": True}}))
    chain2 = reg.build(_cfg({"a": {"enabled": True}}))
    assert chain1 is not chain2
    assert [b.name for b in chain1.branches] == ["a"]
    assert [b.name for b in chain2.branches] == ["a"]
    # 二次 build 后 setup_all 只对新链生效(旧链不残留)
    asyncio.run(reg.setup_all(_cfg({}), None))
    assert log == [("a", "setup", {"enabled": True})]


def test_setup_receives_branch_config():
    """验收:setup 收到枝干自己的配置段(枝干自校验依据)。"""
    log = []
    reg = BranchRegistry()
    reg.register_factory("memory", lambda cfg: LogBranch("memory", log))
    reg.build(_cfg({"memory": {"enabled": True, "retrieval_top_k": 5}}))
    asyncio.run(reg.setup_all(_cfg({}), SimpleNamespace()))
    assert log[0][2] == {"enabled": True, "retrieval_top_k": 5}


def test_setup_failure_propagates():
    """验收:setup 失败向上抛 = 启动失败(不静默吞错)。"""
    class BadBranch(Branch):
        name = "bad"

        async def setup(self, config, core):
            raise RuntimeError("依赖缺失")

    reg = BranchRegistry()
    reg.register_factory("bad", lambda cfg: BadBranch())
    reg.build(_cfg({"bad": {"enabled": True}}))
    with pytest.raises(RuntimeError, match="依赖缺失"):
        asyncio.run(reg.setup_all(_cfg({}), None))


def test_teardown_all_reverse_with_warning():
    """验收:teardown_all 逆序调用;单个 teardown 异常仅告警,逆序继续。"""
    log = []

    class BadTeardown(LogBranch):
        async def teardown(self):
            log.append((self.name, "teardown"))
            raise RuntimeError("teardown 失败")

    reg = BranchRegistry()
    reg.register_factory("a", lambda cfg: LogBranch("a", log))
    reg.register_factory("b", lambda cfg: BadTeardown("b", log))
    reg.build(_cfg({"a": {"enabled": True}, "b": {"enabled": True}}))
    asyncio.run(reg.teardown_all())
    # 逆序:先 b 后 a;b 抛异常仅告警,a 仍执行
    assert [x for x in log if x[1] == "teardown"] == [("b", "teardown"), ("a", "teardown")]


def test_setup_failure_rolls_back_succeeded():
    """验收(E1):setup 失败 → 逆序 teardown 已成功枝干 → 再抛错(启动原子性)。"""
    log = []

    class BadSetup(Branch):
        name = "bad"

        async def setup(self, config, core):
            log.append(("bad", "setup"))
            raise RuntimeError("依赖缺失")

    reg = BranchRegistry()
    reg.register_factory("a", lambda cfg: LogBranch("a", log))
    reg.register_factory("bad", lambda cfg: BadSetup())
    reg.register_factory("c", lambda cfg: LogBranch("c", log))
    reg.build(_cfg({"a": {"enabled": True}, "bad": {"enabled": True}, "c": {"enabled": True}}))

    with pytest.raises(RuntimeError, match="依赖缺失"):
        asyncio.run(reg.setup_all(_cfg({}), None))
    # a 已成功 → 回滚 teardown;bad 失败不 teardown;c 未 setup 不 teardown
    assert log == [
        ("a", "setup", {"enabled": True}),
        ("bad", "setup"),
        ("a", "teardown"),
    ]


def test_shutdown_idempotent_ordered():
    """验收(L1):shutdown 幂等编排 —— ①TaskRegistry 取消 → ②teardown 逆序 → ③④close。"""
    import asyncio

    from teage_liu2.core.tasks import TaskRegistry

    log = []
    tr = TaskRegistry()
    reg = BranchRegistry()
    reg.register_factory("a", lambda cfg: LogBranch("a", log))
    reg.register_factory("b", lambda cfg: LogBranch("b", log))
    reg.build(_cfg({"a": {"enabled": True}, "b": {"enabled": True}}))

    closed = []

    class FakeClose:
        def __init__(self, tag):
            self.tag = tag

        def close(self):
            closed.append(self.tag)

    ms, sp = FakeClose("ms"), FakeClose("sp")

    async def main():
        task = tr.create_task(asyncio.sleep(30))
        await reg.shutdown(task_registry=tr, message_store=ms, storage_provider=sp)
        # 幂等:第二次调用无效果(teardown/close 不再重复)
        await reg.shutdown(task_registry=tr, message_store=ms, storage_provider=sp)
        return task

    task = asyncio.run(main())
    assert task.cancelled()  # ① 后台任务已取消
    assert [x for x in log if x[1] == "teardown"] == [("b", "teardown"), ("a", "teardown")]
    assert closed == ["ms", "sp"]  # ③④ 各一次


def test_after_receives_after_response(tmp_path):
    """验收(B3):after 钩子收到 AfterResponse(text/content_blocks/usage/done_event)。"""
    seen = {}

    class AfterBranch(Branch):
        name = "after_observer"

        async def after(self, snapshot, response):
            seen["response"] = response
            return []

    llm = FakeLLMClient([
        {
            "content": [{"type": "text", "text": "完成"}],
            "stop_reason": "end_turn",
            "usage": {"output_tokens": 7},
        },
    ])
    hooks = HookChain()
    hooks.register(AfterBranch())
    store = SQLiteHistoryStore(str(tmp_path / "after.db"))
    pipeline = ChatPipeline(llm_client=llm, history_store=store, hooks=hooks)

    async def _run():
        return [ev async for ev in pipeline.chat_stream("s_after", "问题")]

    asyncio.run(_run())
    resp = seen["response"]
    assert isinstance(resp, AfterResponse)
    assert resp.text == "完成"
    assert resp.done_event is not None
    assert resp.done_event["type"] == "done"
    assert resp.usage == {"output_tokens": 7}
    assert resp.content_blocks[0]["type"] == "text"


# ---------------------------------------------------------------------------
# extension_loader: manifest 解析 / 目录扫描 / 动态装载(2026-09-08 统一扩展目录树)
# ---------------------------------------------------------------------------
import sys
from pathlib import Path

from teage_liu2.core.extension_loader import (
    discover_extensions,
    load_python_extension,
    parse_manifest,
)


@pytest.fixture(autouse=True)
def _purge_ext_modules():
    """每用例前清动态装载缓存:测试间同名同 hash 模块不得互相污染。"""
    stale = [k for k in sys.modules if k.startswith("teage_liu2_ext_")]
    for k in stale:
        del sys.modules[k]
    yield
    for k in [k for k in sys.modules if k.startswith("teage_liu2_ext_")]:
        del sys.modules[k]


def _write_manifest(dir_path: Path, text: str) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    m = dir_path / "manifest.yaml"
    m.write_text(text, encoding="utf-8")
    # python manifest 的 entry 存在性校验先于多数失败模式断言 → 默认补一个 stub 入口
    stub = dir_path / "main.py"
    if not stub.exists():
        stub.write_text("x = 1\n", encoding="utf-8")
    return m


VALID_MANIFEST = """\
name: sample
version: 0.1.0
language: python
entry: main.py
capabilities: [observe]
description: 测试扩展
"""


def test_parse_manifest_valid(tmp_path):
    _write_manifest(tmp_path / "sample", VALID_MANIFEST)
    spec = parse_manifest(tmp_path / "sample")
    assert spec.name == "sample"
    assert spec.version == "0.1.0"
    assert spec.language == "python"
    assert spec.entry == "main.py"
    assert spec.capabilities == ["observe"]
    assert spec.transport is None and spec.command is None
    assert len(spec.manifest_hash) == 8
    assert spec.path.endswith("sample")


def test_parse_manifest_name_mismatch_rejected(tmp_path):
    _write_manifest(tmp_path / "other", VALID_MANIFEST.replace("name: sample", "name: sample2"))
    with pytest.raises(ValueError, match="目录名"):
        parse_manifest(tmp_path / "other")


def test_parse_manifest_unknown_key_rejected(tmp_path):
    _write_manifest(tmp_path / "sample", VALID_MANIFEST + "typo_key: 1\n")
    with pytest.raises(ValueError, match="未知"):
        parse_manifest(tmp_path / "sample")


def test_parse_manifest_python_missing_entry_rejected(tmp_path):
    _write_manifest(tmp_path / "sample", VALID_MANIFEST.replace("entry: main.py\n", ""))
    with pytest.raises(ValueError, match="entry"):
        parse_manifest(tmp_path / "sample")


def test_parse_manifest_other_requires_transport(tmp_path):
    m = VALID_MANIFEST.replace("language: python", "language: other")
    m = m.replace("entry: main.py\n", "")
    _write_manifest(tmp_path / "sample", m)
    with pytest.raises(ValueError, match="transport"):
        parse_manifest(tmp_path / "sample")


def test_parse_manifest_bad_capability_rejected(tmp_path):
    _write_manifest(tmp_path / "sample", VALID_MANIFEST.replace("[observe]", "[root_shell]"))
    with pytest.raises(ValueError, match="capabilities"):
        parse_manifest(tmp_path / "sample")


def test_discover_extensions_scans_and_collects_errors(tmp_path):
    good = tmp_path / "good"
    _write_manifest(good, VALID_MANIFEST.replace("name: sample", "name: good"))
    bad = tmp_path / "bad"
    _write_manifest(bad, VALID_MANIFEST.replace("name: sample", "name: wrong"))
    (tmp_path / "loose.txt").write_text("不是目录", encoding="utf-8")
    (tmp_path / "no_manifest").mkdir()

    specs, errors = discover_extensions(tmp_path)
    assert set(specs) == {"good"}
    assert "bad" in errors and "目录名" in errors["bad"]


def test_discover_extensions_missing_root_returns_empty(tmp_path):
    specs, errors = discover_extensions(tmp_path / "no_such_dir")
    assert specs == {} and errors == {}


SAMPLE_MAIN = '''\
"""测试扩展入口(计划任务 3 真实动态装载用)。"""
from teage_liu2.core.hooks import Branch


class SampleBranch(Branch):
    name = "sample"
    capabilities = ["observe"]

    def __init__(self, config):
        self.config = dict(config or {})


def create_branch(config):
    return SampleBranch(config)
'''


def test_load_python_extension(tmp_path):
    ext = tmp_path / "sample"
    _write_manifest(ext, VALID_MANIFEST)
    (ext / "main.py").write_text(SAMPLE_MAIN, encoding="utf-8")
    spec = parse_manifest(ext)

    branch = load_python_extension(spec, {"k": 1})
    assert branch.name == "sample"
    assert branch.config == {"k": 1}

    # 同 manifest 重复装载 → 命中模块缓存,类身份一致;config 各自独立
    branch2 = load_python_extension(spec, {"k": 2})
    assert type(branch2) is type(branch)
    assert branch2 is not branch
    assert branch2.config == {"k": 2}


def test_load_python_extension_missing_factory_rejected(tmp_path):
    # version 与 test_load_python_extension 不同 → manifest_hash 不同 →
    # 绕开 sys.modules 同名模块缓存(否则复用上一测试已含 create_branch 的模块,不会抛错)
    ext = tmp_path / "sample"
    _write_manifest(ext, VALID_MANIFEST.replace("version: 0.1.0", "version: 0.2.0"))
    (ext / "main.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="create_branch"):
        load_python_extension(parse_manifest(ext), {})


def test_directory_loader_channel(tmp_path):
    """验收:无工厂时目录装载器兜底;loader 返回 None = 未知名。"""
    ext = tmp_path / "sample"
    _write_manifest(ext, VALID_MANIFEST)
    (ext / "main.py").write_text(SAMPLE_MAIN, encoding="utf-8")
    spec = parse_manifest(ext)
    reg = BranchRegistry()
    reg.set_directory_loader(
        lambda name, cfg: load_python_extension(spec, cfg) if name == "sample" else None
    )

    chain = reg.build(_cfg({"sample": {"enabled": True}}))
    assert [b.name for b in chain.branches] == ["sample"]

    ghost = BranchRegistry()
    ghost.set_directory_loader(lambda name, cfg: None)
    with pytest.raises(ValueError, match="未知枝干"):
        ghost.build(_cfg({"ghost": {"enabled": True}}))


def test_factory_precedes_directory_loader(tmp_path):
    """验收(§2.1 定位):factory(测试/嵌入注入)优先于目录装载器。"""
    log = []
    reg = BranchRegistry()
    reg.register_factory("a", lambda cfg: LogBranch("from_factory", log))
    reg.set_directory_loader(lambda name, cfg: LogBranch("from_directory", log))
    chain = reg.build(_cfg({"a": {"enabled": True}}))
    assert chain.branches[0].name == "from_factory"


def test_wire_extensions_merges_stdio_and_validates(tmp_path):
    """验收:stdio manifest 字段合并进 branch_cfg(相对路径解析);
    config 声明未安装 → ValueError;未声明的已装扩展 → 列入 disabled。"""
    from teage_liu2.core.extension_loader import wire_extensions

    ext = tmp_path / "remote_ext"
    _write_manifest(ext, """\
name: remote_ext
version: 0.1.0
language: other
transport: stdio
command: [bin/run.exe, --flag]
capabilities: [observe]
""")
    (ext / "bin").mkdir()
    (ext / "bin" / "run.exe").write_text("stub", encoding="utf-8")

    # ① 合并:command 相对路径 → 相对 manifest 目录的绝对路径
    cfg = {"core": {"branches": {"remote_ext": {"enabled": True}}}}
    merged, specs, disabled = wire_extensions(cfg, extensions_root=str(tmp_path))
    assert specs["remote_ext"].transport == "stdio"
    entry = merged["core"]["branches"]["remote_ext"]
    assert entry["transport"] == "stdio"
    assert entry["command"][0] == str(ext / "bin" / "run.exe")
    assert entry["command"][1] == "--flag"
    assert disabled == []

    # ② 声明未安装 → ValueError
    with pytest.raises(ValueError, match="未安装"):
        wire_extensions({"core": {"branches": {"ghost": {"enabled": True}}}},
                        extensions_root=str(tmp_path))

    # ③ 已装未声明 → disabled 列表(安装 ≠ 激活)
    _, _, disabled = wire_extensions({"core": {"branches": {}}},
                                     extensions_root=str(tmp_path))
    assert disabled == ["remote_ext"]


def test_parse_manifest_bad_name_rejected(tmp_path):
    """验收(评审 P3):name 不匹配 ^[a-z0-9_]+$ → 解析拒绝。"""
    ext = tmp_path / "Bad-Name"
    _write_manifest(ext, VALID_MANIFEST.replace("name: sample", "name: Bad-Name"))
    with pytest.raises(ValueError, match="name"):
        parse_manifest(ext)


def test_parse_manifest_entry_traversal_rejected(tmp_path):
    """验收(评审 P3):entry 路径穿越(../)越出扩展目录 → 拒绝。"""
    ext = tmp_path / "sample"
    _write_manifest(ext, VALID_MANIFEST.replace("entry: main.py", "entry: ../evil.py"))
    with pytest.raises(ValueError, match="entry"):
        parse_manifest(ext)


def test_load_python_extension_poison_cache_recovery(tmp_path):
    """验收(评审 P3):装载失败清毒缓存;同 hash 修复代码后可重新装载。"""
    ext = tmp_path / "sample"
    _write_manifest(ext, VALID_MANIFEST)
    main = ext / "main.py"
    # 模块体级失败(exec_module 即抛):才能触发 exec 失败的毒缓存路径
    main.write_text("1 / 0  # exec 阶段即失败\n", encoding="utf-8")
    spec = parse_manifest(ext)
    with pytest.raises(Exception):
        load_python_extension(spec, {})
    # 同 manifest(hash 不变)、代码修复 → 不应再命中残缺模块缓存
    main.write_text(SAMPLE_MAIN, encoding="utf-8")
    branch = load_python_extension(spec, {})
    assert branch.name == "sample"


def test_load_python_extension_capability_mismatch_rejected(tmp_path):
    """验收(评审 P2):代码声明 capabilities 超出 manifest 授权面 → 启动失败。"""
    ext = tmp_path / "sample"
    _write_manifest(ext, VALID_MANIFEST)  # manifest 授权面: [observe]
    (ext / "main.py").write_text(
        SAMPLE_MAIN.replace('capabilities = ["observe"]', 'capabilities = ["llm"]'),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="授权面"):
        load_python_extension(parse_manifest(ext), {})
