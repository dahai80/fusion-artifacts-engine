# Plan: 配置外部化 — 统一配置文件替代硬编码

User instruction: "所有的项目要有一个配置文件，配置类的卸载配置文件里面，不能写死在代码里面"

Importers/callers: ArtifactEngine(config.py), RPCServer(server.py), CLI(__main__.py), fusion-code ArtifactCreateTool/ArtifactUpdateTool/artifactInjection, fusion-studio IPCClient/ArtifactsPanel
Affected API: ArtifactEngineConfig fields remain same, new load_config() function added, CLI args behavior unchanged (override config)
Data schemas: YAML config file with server/storage/mlx/thresholds/artifact sections; ArtifactEngineConfig Pydantic model unchanged

## 问题

三个项目都有硬编码的地址/端口/阈值：

| 项目 | 硬编码位置 | 值 |
|---|---|---|
| artifacts-engine | `config.py` DEFAULT_* 常量 | 127.0.0.1, 8892, 180000 等 |
| artifacts-engine | `__main__.py` argparse default | 127.0.0.1, 8892 |
| artifacts-engine | `rpc/server.py` __init__ default | 127.0.0.1, 8892 |
| fusion-code | 3个文件的 ARTIFACT_ENGINE_URL | 127.0.0.1:8892 |
| fusion-studio | IPCClient.swift artifactsEngineURL | 127.0.0.1:8892 |

## 方案

### 1. artifacts-engine: YAML 配置文件 + 加载器

**新建文件**: `fusion_artifacts_engine/default_config.yaml`

存放所有默认值，替代 `config.py` 中的 DEFAULT_* 常量：

```yaml
server:
  host: "127.0.0.1"
  port: 8892

storage:
  root: "~/.fusion/artifacts"
  db_name: "meta.db"
  small_content_limit: 10240

mlx:
  url: "http://localhost:8890"

thresholds:
  safe_context: 180000
  output_reserve: 8192
  auto_create_lines: 30
  auto_create_chars: 1500

artifact:
  id_prefix: "art_"
```

**修改文件**: `config.py`
- 删除所有 DEFAULT_* 硬编码常量
- 新增 `load_config(path) -> ArtifactEngineConfig` 函数
- 加载优先级: CLI 参数 > 用户配置 `~/.fusion/artifacts/config.yaml` > 内嵌 default_config.yaml > Pydantic 字段默认值
- 用 pyyaml 解析 YAML

**修改文件**: `__main__.py`
- argparse default 改为 None（不再硬编码）
- 从 load_config() 获取默认值
- status 命令也读取配置获取端口

**修改文件**: `rpc/server.py`
- 删除 __init__ 中的 host/port 默认值硬编码
- 从 config 获取

**新增依赖**: `pyyaml>=6.0` 加入 pyproject.toml

### 2. fusion-code: 从配置文件读取 artifacts-engine URL

**新建文件**: `src/utils/artifactConfig.ts`

统一 artifacts-engine 配置读取：
- 读取 `~/.fusion/artifacts/config.yaml` 中的 server.host / server.port
- 缓存结果，避免每次请求都读文件
- 降级: 配置文件不存在时使用 `http://127.0.0.1:8892`

**修改文件**: 3个文件中的 ARTIFACT_ENGINE_URL
- `ArtifactCreateTool.ts` → 从 artifactConfig.ts 导入
- `ArtifactUpdateTool.ts` → 从 artifactConfig.ts 导入
- `artifactInjection.ts` → 从 artifactConfig.ts 导入

### 3. fusion-studio: FusionConfig 新增 artifacts 配置段

**修改文件**: `FusionConfig.swift`

新增 MARK - Artifacts:
```swift
@AppStorage("artifactsEngineHost") var artifactsEngineHost = "127.0.0.1"
@AppStorage("artifactsEnginePort") var artifactsEnginePort = 8892
var artifactsEngineURL: String { "http://\(artifactsEngineHost):\(artifactsEnginePort)" }
```

**修改文件**: `IPCClient.swift`
- 删除硬编码 `private let artifactsEngineURL = "http://127.0.0.1:8892"`
- 改为从 FusionConfig.shared 读取

**修改文件**: `ArtifactsPanel.swift`
- 无需改动（已通过 IPCClient 间接访问）

## 文件清单

### artifacts-engine (5 files)
1. **新建** `fusion_artifacts_engine/default_config.yaml`
2. **修改** `fusion_artifacts_engine/config.py` — load_config + 删 DEFAULT_*
3. **修改** `fusion_artifacts_engine/__main__.py` — 从 config 读默认值
4. **修改** `fusion_artifacts_engine/rpc/server.py` — 从 config 读 host/port
5. **修改** `pyproject.toml` — 加 pyyaml 依赖

### fusion-code (4 files)
6. **新建** `src/utils/artifactConfig.ts`
7. **修改** `src/tools/ArtifactCreateTool/ArtifactCreateTool.ts`
8. **修改** `src/tools/ArtifactUpdateTool/ArtifactUpdateTool.ts`
9. **修改** `src/utils/artifactInjection.ts`

### fusion-studio (2 files)
10. **修改** `FusionStudio/Common/FusionConfig.swift`
11. **修改** `FusionStudio/Bridge/IPCClient.swift`

## 测试

- artifacts-engine: 启动时验证 config.yaml 加载正确，CLI 参数覆盖生效
- fusion-code: 删除/修改 config.yaml 后验证 URL 变化
- fusion-studio: Settings UI 修改端口后验证 IPCClient 连接地址变化
