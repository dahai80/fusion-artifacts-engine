import logging

logger = logging.getLogger(__name__)

# R8: layered error codes. Callers branch on code to decide "abandon / retry / system failure".
# Standard JSON-RPC codes preserved: -32700 parse, -32600 invalid request, -32601 method not found,
# -32602 params/validation, -32603 internal.
# Extended business codes (-32000~-32099, the JSON-RPC range reserved for server-defined codes):
#   -32001 not found (not retryable, caller should abandon)
#   -32002 optimistic-lock / state conflict (retryable)
#   -32003 rate/resource limit (retryable/degrade)
#   -32004 business rule rejection (not retryable)
#   -32005 not implemented (placeholder methods retired; not retryable, callers should switch to replacement capabilities)
#   -32006 permission denied / IDOR (not retryable, caller accessed another user's resource)


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class NotFoundError(RpcError):
    def __init__(self, message: str = "Artifact not found"):
        super().__init__(-32001, message)


class ConflictError(RpcError):
    # Optimistic-lock conflict / state conflict — caller may retry
    def __init__(self, message: str = "Conflict"):
        super().__init__(-32002, message)


class ResourceLimitError(RpcError):
    # Resource limit (disk, threads, version cap, etc.) — retryable or degradable
    def __init__(self, message: str = "Resource limit reached"):
        super().__init__(-32003, message)


class BusinessRuleError(RpcError):
    # Business rule rejection (e.g. illegal state transition) — not retryable
    def __init__(self, message: str = "Business rule violated"):
        super().__init__(-32004, message)


class NotImplementedError(RpcError):
    # Ops-6: placeholder methods retired (inject/interact were advertised but never implemented; reject
    # explicitly before commercial use instead of returning a stub).
    # RPC method names stay registered (backward compatible: old clients do not get method not found), but execution always rejects.
    def __init__(self, message: str = "Method not implemented"):
        super().__init__(-32005, message)


class PermissionError(RpcError):
    # P2-3/MEDIUM-4: IDOR protection — reject when the caller's caller_user_id does not match the artifact's owner_user_id.
    # Local single-tenant defaults to no caller_user_id (validation skipped); multi-tenant deployments inject identity
    # per request and enforce ownership checks.
    # Not retryable: unauthorized access is a deterministic rejection, retrying is pointless.
    def __init__(self, message: str = "Permission denied: not artifact owner"):
        super().__init__(-32006, message)
