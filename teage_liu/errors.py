"""统一异常体系。"""


class ToolError(Exception):
    """工具错误基类。"""

class ToolNotFoundError(ToolError):
    """工具不存在。"""

class ToolExecutionError(ToolError):
    """工具执行失败。"""

class ToolPermissionDenied(ToolError):
    """权限拒绝。"""

class ToolTimeoutError(ToolError):
    """工具超时。"""


class ConfigError(Exception):
    """配置错误基类。"""

class ConfigValidationError(ConfigError):
    """schema 校验失败(400)。"""

class ConfigReloadError(ConfigError):
    """热重载重建失败(500)。"""

class ContainerConfigError(ConfigError):
    """容器配置错误(循环依赖/未注册依赖)。"""
