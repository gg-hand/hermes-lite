"""扩展目录发现与动态装载(2026-09-08 统一扩展目录树)。

**生产扩展唯一装载通道**(设计 §2.1):扫描 extensions_root → 解析 manifest.yaml
→ 按语言装载(python=进程内 importlib / other=stdio 交 Supervisor)。

manifest.yaml 是扩展的安装态唯一事实源(身份/装载方式/capabilities 声明面);
运行态(enabled 开关 + 配置覆盖)在 config.yaml 的 core.branches.<name>。
本模块不感知任何具体扩展名(依赖铁律:core 不知道任何枝干)。
"""
from __future__ import annotations

import hashlib
import importlib.util
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .hooks import Branch

logger = logging.getLogger(__name__)

#: capabilities 授权面值域(与 core/hooks.py CAP_* 常量同源;新增须同步扩枚举)
_VALID_CAPABILITIES = frozenset({"observe", "tool_executor", "llm", "self_hosted_storage"})
#: manifest 允许的字段(未知键拒绝,防 typo 静默失效)
_MANIFEST_ALLOWED_KEYS = frozenset({
    "name", "version", "language", "entry", "transport", "command",
    "protocol_version", "capabilities", "description", "requirements",
    "kind", "slots",  # P-6: host-component 类型声明（storage_rust 后端）
})
_MANIFEST_NAME = "manifest.yaml"
#: 扩展名规范(与 registry is_valid_extension_name 同源,设计 §3)
_NAME_PATTERN = re.compile(r"^[a-z0-9_]+$")


@dataclass(frozen=True)
class ExtensionSpec:
    """单个已安装扩展的安装态(由 manifest.yaml 解析,只读)。"""

    name: str
    version: str
    language: str                     # python | other
    entry: Optional[str]              # python: 相对 manifest 目录的入口文件
    transport: Optional[str]          # other: "stdio"
    command: Optional[List[str]]      # other: 启动命令(相对路径待合并期解析)
    protocol_version: Optional[str]
    capabilities: List[str]
    kind: str                         # branch(缺省) | host-component(P-6)
    slots: List[str]                  # host-component: 可接管插槽;branch 恒为 []
    description: str
    requirements: List[str]
    path: str                         # manifest 所在目录(绝对路径字符串)
    manifest_hash: str                # manifest 原文 sha256 前 8 位(热重载模块隔离)


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ValueError(msg)


def parse_manifest(dir_path: Path) -> ExtensionSpec:
    """解析单个扩展目录的 manifest.yaml → ExtensionSpec(严格校验)。

    坏 manifest 抛 ValueError(调用方决定该扩展装配失败还是告警跳过)。
    """
    dir_path = Path(dir_path).resolve()
    manifest = dir_path / _MANIFEST_NAME
    _require(manifest.is_file(), f"扩展 {dir_path.name!r} 缺少 {_MANIFEST_NAME}")
    raw_bytes = manifest.read_bytes()
    try:
        data = yaml.safe_load(raw_bytes.decode("utf-8"))
    except yaml.YAMLError as e:
        raise ValueError(f"扩展 {dir_path.name!r} manifest.yaml 解析失败: {e}") from e
    _require(isinstance(data, dict), f"扩展 {dir_path.name!r} manifest 根必须是映射")
    unknown = sorted(set(data) - _MANIFEST_ALLOWED_KEYS)
    _require(not unknown, f"扩展 {dir_path.name!r} manifest 包含未知字段: {', '.join(unknown)}")

    name = data.get("name")
    _require(isinstance(name, str) and name, f"扩展 {dir_path.name!r} 缺少 name")
    _require(bool(_NAME_PATTERN.fullmatch(name)),
             f"扩展 name {name!r} 非法(必须匹配 ^[a-z0-9_]+$)")
    _require(name == dir_path.name, f"扩展 name {name!r} 必须与目录名 {dir_path.name!r} 一致")
    version = data.get("version")
    _require(isinstance(version, str) and version, f"扩展 {name!r} 缺少 version")
    language = data.get("language")
    _require(language in ("python", "other"), f"扩展 {name!r} language 必须是 python/other")

    entry = data.get("entry")
    transport = data.get("transport")
    command = data.get("command")
    protocol_version = data.get("protocol_version")
    if language == "python":
        _require(isinstance(entry, str) and entry, f"扩展 {name!r} language=python 必须声明 entry")
        entry_abs = (dir_path / entry).resolve()
        _require(entry_abs.is_file() and entry_abs.is_relative_to(dir_path),
                 f"扩展 {name!r} entry 非法或越出扩展目录: {entry}")
        _require(transport is None and command is None,
                 f"扩展 {name!r} language=python 不支持 transport/command(强隔离请用 language=other)")
    else:
        _require(transport == "stdio", f"扩展 {name!r} language=other 必须声明 transport: stdio")
        _require(isinstance(command, list) and command
                 and all(isinstance(c, str) for c in command),
                 f"扩展 {name!r} 必须声明非空 command 列表")
        _require(entry is None, f"扩展 {name!r} language=other 不支持 entry")
        if protocol_version is not None:
            _require(isinstance(protocol_version, str) and protocol_version,
                     f"扩展 {name!r} protocol_version 必须是非空字符串")

    capabilities = data.get("capabilities")
    _require(isinstance(capabilities, list)
             and all(isinstance(c, str) for c in capabilities),
             f"扩展 {name!r} capabilities 必须是字符串列表")
    bad = [c for c in capabilities if c not in _VALID_CAPABILITIES]
    _require(not bad, f"扩展 {name!r} capabilities 含非法值 {bad}(可用: {sorted(_VALID_CAPABILITIES)})")

    kind = data.get("kind", "branch")
    _require(kind in ("branch", "host-component"),
             f"扩展 {name!r} kind 必须是 branch/host-component,实际 {kind!r}")
    slots = data.get("slots", [])
    _require(isinstance(slots, list) and all(isinstance(s, str) for s in slots),
             f"扩展 {name!r} slots 必须是字符串列表")
    if kind == "host-component":
        _require(language == "other",
                 f"host-component 扩展 {name!r} 必须是 language: other(stdio 子进程形态)")
        _require(bool(slots), f"host-component 扩展 {name!r} 必须声明非空 slots")
        _require(capabilities == [],
                 f"host-component 扩展 {name!r} capabilities 必须为空(钩子授权面不适用)")
    else:
        _require(not slots, f"branch 扩展 {name!r} 不支持 slots 字段(P-6)")

    description = data.get("description", "")
    _require(isinstance(description, str), f"扩展 {name!r} description 必须是字符串")
    requirements = data.get("requirements", [])
    _require(isinstance(requirements, list)
             and all(isinstance(r, str) for r in requirements),
             f"扩展 {name!r} requirements 必须是字符串列表(仅文档声明,宿主不自动安装)")

    return ExtensionSpec(
        name=name,
        version=version,
        language=language,
        entry=entry,
        transport=transport,
        command=command,
        protocol_version=protocol_version,
        capabilities=list(capabilities),
        kind=kind,
        slots=list(slots),
        description=description,
        requirements=list(requirements),
        path=str(dir_path),
        manifest_hash=hashlib.sha256(raw_bytes).hexdigest()[:8],
    )


def discover_extensions(root: Path) -> Tuple[Dict[str, ExtensionSpec], Dict[str, str]]:
    """扫描扩展根目录 → (合法 specs, 非法项错误表)。

    根目录不存在 → 空表(由 wire_extensions 在 config 声明时统一报错);
    子目录无 manifest.yaml → 跳过;坏 manifest → 记入 errors,不中断扫描。
    """
    root = Path(root)
    specs: Dict[str, ExtensionSpec] = {}
    errors: Dict[str, str] = {}
    if not root.is_dir():
        return specs, errors
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not (child / _MANIFEST_NAME).exists():
            continue
        try:
            spec = parse_manifest(child)
        except ValueError as e:
            errors[child.name] = str(e)
            logger.warning("扩展 %s manifest 非法: %s", child.name, e)
            continue
        specs[spec.name] = spec
        logger.info("发现扩展 %s v%s (kind=%s, %s, capabilities=%s)",
                    spec.name, spec.version, spec.kind, spec.language, spec.capabilities)
    return specs, errors


def load_python_extension(spec: ExtensionSpec, config: dict) -> Branch:
    """动态装载 language=python 扩展入口 → create_branch(config) → Branch。

    模块名含 manifest_hash:热重载时 manifest 变更 → 全新模块对象(不留旧
    缓存污染);manifest 未变 → 命中 sys.modules 缓存(重复 build 不重复执行)。
    模块残留在 sys.modules 按"每次热重载一个"增长,量级可忽略(个人场景)。
    扩展子模块/资源经 __file__ 相对定位;本装载器不污染 sys.path。
    """
    module_name = f"teage_liu2_ext_{spec.name}_{spec.manifest_hash}"
    module = sys.modules.get(module_name)
    if module is None:
        entry_path = Path(spec.path) / (spec.entry or "")
        import_spec = importlib.util.spec_from_file_location(module_name, entry_path)
        if import_spec is None or import_spec.loader is None:
            raise ValueError(f"扩展 {spec.name!r} 无法创建模块加载器: {entry_path}")
        module = importlib.util.module_from_spec(import_spec)
        sys.modules[module_name] = module
        try:
            import_spec.loader.exec_module(module)
        except Exception:
            # 毒缓存清除:exec 失败的残缺模块必须移出 sys.modules,否则同 hash
            # 的后续装载全部命中残缺模块,报误导性"缺少 create_branch"且无法自愈
            sys.modules.pop(module_name, None)
            raise
        logger.info("扩展 %s 模块已装载(module=%s)", spec.name, module_name)
    factory = getattr(module, "create_branch", None)
    if not callable(factory):
        raise ValueError(
            f"扩展 {spec.name!r} 入口 {spec.entry!r} 缺少 create_branch(config) 工厂函数"
        )
    branch = factory(config)
    if not isinstance(branch, Branch):
        raise ValueError(
            f"扩展 {spec.name!r} create_branch 返回类型错误: {type(branch).__name__}(应为 Branch)"
        )
    # manifest = 授权声明面唯一事实源(设计 §3):代码类声明的 capabilities 必须
    # ⊆ manifest 授权面,不一致 = 启动失败(E1)——防声明面被代码静默架空
    declared = set(branch.capabilities or [])
    if not declared <= set(spec.capabilities):
        raise ValueError(
            f"扩展 {spec.name!r} 代码声明 capabilities {sorted(declared)} "
            f"超出 manifest 授权面 {sorted(spec.capabilities)}(manifest 为唯一事实源)"
        )
    return branch


def make_directory_loader(specs: Dict[str, ExtensionSpec]) -> Any:
    """由 specs 构造 registry 目录装载器:python 扩展动态装载,其余返回 None。

    stdio 扩展不经此装载(走 supervisor launcher);loader 对其返回 None,
    实际由 wire_extensions 合并的 transport 字段走 extension_launcher 通道。
    """
    def loader(name: str, branch_cfg: dict) -> Optional[Branch]:
        spec = specs.get(name)
        if spec is not None and spec.language == "python":
            return load_python_extension(spec, branch_cfg)
        return None

    return loader


def resolve_command(command: List[str], base_dir: Path) -> List[str]:
    """command 相对路径 → 相对 manifest 目录解析(目录边界内才绝对化,防穿越)。

    目录内存在的路径 → 绝对路径;否则原样保留(如可执行名 python / 绝对路径)。
    """
    resolved: List[str] = []
    for token in command:
        candidate = (base_dir / token).resolve()
        if candidate.exists() and candidate.is_relative_to(base_dir):
            resolved.append(str(candidate))
        else:
            resolved.append(token)
    return resolved


def wire_extensions(
    cfg: dict, extensions_root: Optional[str] = None
) -> Tuple[dict, Dict[str, ExtensionSpec], List[str]]:
    """装配期扩展解析(create_app 与 /reload 共用):

    ① 定 extensions_root(参数 > core.extensions_root > 默认 data2/extensions;
       相对路径相对 cwd 解析,与 storage.sqlite_path 等既有路径语义一致)
    ② 扫描目录 → specs + errors
    ③ 校验:config 声明且启用的扩展 → 缺失/manifest 非法 = ValueError 启动失败
    ④ 合并:language=other 的 transport/command/protocol_version 深拷贝合并进
       core.branches.<name>(command 相对路径相对 manifest 目录解析);config 侧
       已显式声明的 transport 键优先(向后兼容,不覆盖)
    ⑤ 统计已安装未启用名(安装 ≠ 激活,由调用方记日志)

    返回 (merged_cfg, specs, disabled_installed)。本函数不修改入参 cfg。
    """
    import copy

    from .config import core_config_from

    if extensions_root is None:
        extensions_root = core_config_from(cfg).extensions_root
    root = Path(extensions_root)
    if not root.is_absolute():
        root = Path.cwd() / root
    specs, errors = discover_extensions(root)

    merged = copy.deepcopy(cfg or {})
    branches_cfg = (merged.get("core") or {}).get("branches") or {}
    if not isinstance(branches_cfg, dict):
        raise ValueError(f"core.branches 必须是映射,实际 {type(branches_cfg).__name__}")
    for name, raw in branches_cfg.items():
        if not isinstance(raw, dict) or not raw.get("enabled", True):
            continue
        if name in errors:
            raise ValueError(f"扩展 {name!r} manifest 非法: {errors[name]}")
        if name not in specs:
            raise ValueError(
                f"扩展 {name!r} 未安装(extensions_root={root} 下无 {name}/{_MANIFEST_NAME})"
            )
        if specs[name].kind == "host-component":
            raise ValueError(
                f"扩展 {name!r} 是 host-component(宿主组件 backend),"
                "不得声明在 core.branches(经 host_components 接管,P-6)"
            )
    for name, raw in branches_cfg.items():
        if not isinstance(raw, dict) or not raw.get("enabled", True):
            continue
        spec = specs[name]
        if spec.language == "other" and "transport" not in raw:
            raw["transport"] = spec.transport
            raw["command"] = resolve_command(spec.command or [], Path(spec.path))
            if spec.protocol_version:
                raw.setdefault("protocol_version", spec.protocol_version)

    disabled_installed = sorted(
        n for n, s in specs.items()
        if s.kind == "branch"
        and (n not in branches_cfg
             or (isinstance(branches_cfg.get(n), dict) and not branches_cfg[n].get("enabled", True)))
    )
    return merged, specs, disabled_installed


def find_host_component(
    specs: Dict[str, ExtensionSpec], name: str, slot: str
) -> ExtensionSpec:
    """供宿主组件装载器查询(P-6):校验存在/kind/slots 后返回 spec。

    任一不满足 → ValueError(启动失败,不静默降级)。
    """
    spec = specs.get(name)
    if spec is None:
        raise ValueError(
            f"host_component 引用的扩展 {name!r} 未安装"
            "(extensions_root 下无该目录或 manifest)"
        )
    if spec.kind != "host-component":
        raise ValueError(
            f"扩展 {name!r} 的 manifest kind={spec.kind!r},不能作为宿主组件 backend 引用"
        )
    if slot not in spec.slots:
        raise ValueError(
            f"host-component 扩展 {name!r} 的 slots {spec.slots} 未声明插槽 {slot!r}"
        )
    return spec
