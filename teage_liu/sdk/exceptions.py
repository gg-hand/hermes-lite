"""SDK 异常类层级。

异常分类：
- SDKError：所有 SDK 异常的基类
- AuthenticationError：签名/认证失败
- TransportError：网络/传输层错误
- ProtocolError：协议层错误（schema 验证失败等）
"""
from __future__ import annotations


class SDKError(Exception):
    """SDK 异常基类。"""

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class AuthenticationError(SDKError):
    """认证/签名失败。"""
    pass


class TransportError(SDKError):
    """网络/传输层错误。"""
    pass


class ProtocolError(SDKError):
    """协议层错误（schema 验证失败、参数非法等）。"""
    pass
