"""轻量 DI 容器:组件单例 + 工厂延迟创建 + 热重载原子性重建 + 延迟关闭。"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable, Set

from hermes.errors import ContainerConfigError, ConfigReloadError

logger = logging.getLogger(__name__)

Factory = Callable[["Container"], Any]


def _get_grace_period(config: dict) -> int:
    """动态计算 CLOSE_GRACE_PERIOD。"""
    explicit = config.get("server", {}).get("hot_reload_grace_period")
    if explicit is not None:
        return explicit
    llm_timeout = config.get("llm", {}).get("activity_timeout", 60)
    max_loops = config.get("tools", {}).get("max_loops", 10)
    return int(llm_timeout * max_loops)


CONFIG_TO_COMPONENTS: dict[str, list[str]] = {
    "llm":        ["orchestrator"],
    "security":   ["approval_manager", "orchestrator"],
    "storage":    ["session_logger", "orchestrator"],
    "memory":     ["orchestrator"],
    "monitoring": ["metrics_collector", "metrics_store", "audit_logger"],
    "tasks":      ["task_manager", "cron_scheduler"],
    "skills":     ["mcp_manager"],
    "files":      ["upload_manager", "etl_engine"],
    "guardrails": ["orchestrator"],
    "cron":       ["orchestrator"],
    "history":    ["orchestrator"],
    "tools":      ["orchestrator"],
    "server":     [],
    "multiagent": [
        "blackboard",
        "agent_registry",
        "lock_manager",
        "multiagent_audit_logger",
        "schema_validator",
        "recovery_manager",
        "watchdog_watcher",
        "multiagent_adapter",
        "orchestrator",
    ],
}


class ComponentRef:
    """代理对象:__getattr__ 转发到容器中最新的组件实例。
    用于不参与级联重建的组件访问可重建组件,避免引用过期。"""

    def __init__(self, container: "Container", name: str):
        self._container = container
        self._name = name

    def __getattr__(self, attr: str) -> Any:
        return getattr(self._container.get(self._name), attr)


class Container:
    """轻量 DI 容器:组件单例 + 工厂延迟创建 + 热重载重建。"""

    def __init__(self, config: dict):
        self._config = config
        self._instances: dict[str, Any] = {}
        self._factories: dict[str, Factory] = {}
        self._deps: dict[str, list[str]] = {}
        self._hot_reloadable: dict[str, bool] = {}
        self._lock = threading.RLock()

    @property
    def config(self) -> dict:
        return self._config

    def register(self, name: str, factory: Factory, deps: list[str],
                 hot_reloadable: bool = False) -> None:
        with self._lock:
            self._factories[name] = factory
            self._deps[name] = deps
            self._hot_reloadable[name] = hot_reloadable

    def get(self, name: str) -> Any:
        with self._lock:
            if name not in self._factories:
                raise KeyError(f"组件 '{name}' 未注册")
            if name not in self._instances:
                self._instances[name] = self._factories[name](self)
            return self._instances[name]

    def set_instance(self, name: str, instance: Any) -> None:
        """注入已创建的实例，绕过工厂创建。

        用于 lifespan 已完成复杂初始化（ONNX 预加载、异步 MCP 设置等）
        的组件，避免工厂重复创建。后续 reload 时仍通过工厂重建。
        """
        with self._lock:
            if name not in self._factories:
                raise KeyError(f"组件 '{name}' 未注册")
            self._instances[name] = instance

    def validate(self) -> None:
        self._validate_no_cycles()
        self._validate_deps_registered()

    def _validate_no_cycles(self) -> None:
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {name: WHITE for name in self._factories}

        def dfs(name: str, path: list[str]) -> None:
            if name not in color:
                return
            if color[name] == GRAY:
                cycle = " → ".join(path + [name])
                raise ContainerConfigError(f"检测到循环依赖: {cycle}")
            if color[name] == BLACK:
                return
            color[name] = GRAY
            for dep in self._deps.get(name, []):
                dfs(dep, path + [name])
            color[name] = BLACK

        for name in self._factories:
            dfs(name, [])

    def _validate_deps_registered(self) -> None:
        for name, deps in self._deps.items():
            for dep in deps:
                if dep not in self._factories:
                    raise ContainerConfigError(
                        f"组件 '{name}' 依赖未注册的组件 '{dep}'"
                    )

    def reload(self, changed_sections: set[str], new_config: dict) -> list[str]:
        """原子性重建:先创建全部新实例,全部成功后才替换,失败则回滚。

        hot_reloadable=False 的组件不参与重建（由软重启手动处理）。
        """
        with self._lock:
            directly_affected = set()
            for section in changed_sections:
                directly_affected.update(CONFIG_TO_COMPONENTS.get(section, []))

            # 过滤掉未注册或 hot_reloadable=False 的组件
            reloadable_affected = {
                name for name in directly_affected
                if name in self._factories
                and self._hot_reloadable.get(name, True)
            }

            to_rebuild = self._topo_sort_dependents(reloadable_affected)
            if not to_rebuild:
                self._config = new_config
                return []

            old_config = self._config

            # 阶段1:先创建所有新实例(不替换 _instances)
            new_instances = {}
            try:
                self._config = new_config
                for name in to_rebuild:
                    new_instances[name] = self._factories[name](self)
            except Exception as e:
                self._config = old_config
                for inst in new_instances.values():
                    if inst and hasattr(inst, "close"):
                        try:
                            inst.close()
                        except Exception:
                            pass
                raise ConfigReloadError(f"热重载重建失败: {e}") from e

            # 阶段2:全部成功,批量替换
            old_instances = {}
            for name in to_rebuild:
                old_instances[name] = self._instances.get(name)
                self._instances[name] = new_instances[name]

            # 阶段3:延迟关闭旧实例
            grace = _get_grace_period(self._config)
            for name, old in old_instances.items():
                if old and hasattr(old, "close"):
                    timer = threading.Timer(
                        grace,
                        lambda o=old, n=name: self._safe_close(o, n)
                    )
                    timer.daemon = True
                    timer.start()

            return to_rebuild

    def _topo_sort_dependents(self, directly_affected: set[str]) -> list[str]:
        """拓扑排序:返回所有需要重建的组件(直接受影响 + 级联依赖者)。

        hot_reloadable=False 的组件不参与重建（直接和级联都不参与）。
        """
        # 构建反向依赖图:谁依赖我 → 我重建时谁也要重建
        reverse_deps: dict[str, list[str]] = {}
        for name, deps in self._deps.items():
            for dep in deps:
                reverse_deps.setdefault(dep, []).append(name)

        result: list[str] = []
        visited: set[str] = set()
        queue = list(directly_affected)

        while queue:
            name = queue.pop(0)
            if name in visited:
                continue
            visited.add(name)
            result.append(name)
            for dependent in reverse_deps.get(name, []):
                if dependent not in visited and self._hot_reloadable.get(dependent, True):
                    queue.append(dependent)

        # 拓扑排序:按依赖顺序排列(先重建被依赖的,后重建依赖者)
        ordered: list[str] = []
        temp_mark: set[str] = set()
        perm_mark: set[str] = set()

        def visit(n: str) -> None:
            if n in perm_mark:
                return
            if n in temp_mark:
                return  # 已处理
            temp_mark.add(n)
            for dep in self._deps.get(n, []):
                if dep in visited:
                    visit(dep)
            temp_mark.discard(n)
            perm_mark.add(n)
            ordered.append(n)

        for name in result:
            visit(name)

        return ordered

    @staticmethod
    def _safe_close(instance: Any, name: str) -> None:
        try:
            instance.close()
        except Exception as e:
            logger.warning("关闭组件 %s 失败: %s", name, e)

    def close(self) -> None:
        """关闭所有实例(逆序关闭)。"""
        with self._lock:
            for name in reversed(list(self._instances)):
                inst = self._instances.get(name)
                if inst and hasattr(inst, "close"):
                    self._safe_close(inst, name)
            self._instances.clear()


def detect_changed_sections(old: dict, new: dict) -> set[str]:
    """比较新旧 config 的顶层段,返回变更的段名集合。"""
    changed: set[str] = set()
    for key in set(old) | set(new):
        if json.dumps(old.get(key), sort_keys=True) != \
           json.dumps(new.get(key), sort_keys=True):
            changed.add(key)
    return changed
