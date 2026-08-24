import logging

logger = logging.getLogger(__name__)

# R8: 错误码分层。调用方靠 code 区分「可放弃 / 可重试 / 系统故障」。
# JSON-RPC 标准码保留：-32700 parse, -32600 invalid request, -32601 method not found,
# -32602 params/校验, -32603 internal。
# 扩展业务码（-32000~-32099，JSON-RPC 保留给 server 自定义区间）：
#   -32001 not found（不可重试，应放弃）
#   -32002 optimistic-lock / 状态冲突（可重试）
#   -32003 rate/资源受限（可重试/降级）
#   -32004 业务规则拒绝（不可重试）


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class NotFoundError(RpcError):
    def __init__(self, message: str = "Artifact not found"):
        super().__init__(-32001, message)


class ConflictError(RpcError):
    # 乐观锁冲突 / 状态冲突 —— 调用方可重试
    def __init__(self, message: str = "Conflict"):
        super().__init__(-32002, message)


class ResourceLimitError(RpcError):
    # 资源受限（磁盘、线程、版本上限等）—— 可重试或降级
    def __init__(self, message: str = "Resource limit reached"):
        super().__init__(-32003, message)


class BusinessRuleError(RpcError):
    # 业务规则拒绝（如非法状态转换）—— 不可重试
    def __init__(self, message: str = "Business rule violated"):
        super().__init__(-32004, message)
