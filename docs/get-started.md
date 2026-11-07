---
doc_id: fusion-artifacts-engine/get-started
product: fusion-artifacts-engine
class: app
entry: service
doc_type: get-started
title: { zh: "快速开始", en: "Get Started" }
audience: engineer
status: draft
version: 0.7.13
updated: 2026-11-07
owner: @dahai
source_of_truth:
  - README.md
---

# 快速开始 / Get Started

前提:Python >= 3.11(仓库共享 `~/fusion/.venv`)。

## 1. 安装并启动

```bash
git clone https://github.com/dahai80/fusion-artifacts-engine.git
cd fusion-artifacts-engine
pip install -e ".[test]"

fusion-artifacts-engine start --port 11451
fusion-artifacts-engine status    # exit 0 = healthy, 1 = not running
```

配置从 YAML 读取(见 `config.example.yaml`),环境变量 `FUSION_ARTIFACTS_HOST` / `FUSION_ARTIFACTS_PORT` 可覆盖监听地址。容器部署:`docker build -t fusion-artifacts-engine .`(数据在 `/data` 卷)。

## 2. 认证

服务默认 fail-closed:请求带 `X-API-Key` 头,密钥在 `security` 配置段。开发环境可临时 `allow_no_auth: true`,生产必须关掉。公开分享链接走独立限流桶,不需要密钥。

## 3. 走通第一个产物(JSON-RPC)

```bash
curl -s http://127.0.0.1:11451/ -H "X-API-Key: $FAE_KEY" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"artifact.create","params":{"session_id":"s1","name":"demo","type":"code","content":"print(1)"}}'
```

然后按这个顺序体验核心能力(同一 JSON-RPC 信封,认证头同上):

1. `artifact.version_list` / `artifact.version_diff` —— 改内容后再 `artifact.update`,查看版本差异;`artifact.version_rollback` 可回滚。
2. `artifact.get_content` —— 从引用取回全量内容(对话里只有轻量引用)。
3. `context.budget` —— 计算当前会话的上下文预算;`artifact.auto_compact` 触发自动压缩。
4. `artifact.create_share` → 打开分享链接 → `artifact.revoke_share`。
5. `artifact.delete` → `artifact.list_recycle` → `artifact.restore`。

健康检查:`fusion-artifacts-engine status`;指标:`GET /metrics`(可配 `X-Metrics-Token` 保护)。

## 4. 场景化深入

[USAGE_GUIDE.md](../docs/USAGE_GUIDE.md) 按场景给出 create/version/share/render/budget/lifecycle/recycle/ops 的可运行示例(EN + CN)。

下一步:[api-reference.md](api-reference.md) 查方法清单,[troubleshooting.md](troubleshooting.md) 解决启动/认证问题。
