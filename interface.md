直接输出 \`fusion-artifacts-engine\` 的\*\*核心 API 接口清单\*\*（函数名、入参、出参）

已完成思考

以下是 `fusion-artifacts-engine` 核心 API 接口清单，基于 Python 技术栈设计，纯逻辑无 UI，对齐你「通用中间件」的定位，可直接用于 MVP 开发。

核心依赖：`litellm`（Token 计数）、`pydantic`（数据结构校验）、存储层抽象（底层可插拔）

---

## 一、核心数据结构定义

所有 API 入参出参均基于以下基础模型。

### 1. Artifact 主对象

```
class Artifact(BaseModel):
    id: str                    # 唯一ID，art_前缀
    session_id: str            # 所属会话ID
    name: str                  # 名称/文件名
    type: Literal["code", "markdown", "html", "data"]
    current_version: int       # 当前最新版本号
    summary: str               # 简短描述，用于对话引用
    created_at: int            # Unix 时间戳
    updated_at: int
    is_deleted: bool = False
```

### 2. Artifact 版本对象

```
class ArtifactVersion(BaseModel):
    id: str
    artifact_id: str
    version_num: int
    content: str               # 完整内容
    token_count: int           # 该版本 Token 数
    change_log: str            # 变更说明
    created_at: int
```

### 3. 对话引用对象

```
class ArtifactRef(BaseModel):
    raw_tag: str               # 原始 XML 标签文本
    artifact_id: str
    name: str
    type: str
    version: str               # 版本号或 "latest"
    token_count: int
    summary: str
```

---

## 二、核心 API 分模块清单

### 模块 1：引擎初始化与配置

#### 构造函数 `__init__`

```
def __init__(self, config: ArtifactEngineConfig) -> None
```

**功能**：初始化 Artifact 引擎，加载存储驱动，配置全局阈值。

**入参 `ArtifactEngineConfig` 字段**：

- `storage_driver: StorageDriver`：存储驱动实例（SQLite / 本地文件 / 云端存储，可插拔）
- `default_model: str`：默认模型名称，用于 Token 计数
- `safe_context_threshold: int = 180000`：上下文安全阈值，预留输出余量
- `auto_create_threshold_lines: int = 30`：代码超过多少行自动转 Artifact
- `auto_create_threshold_chars: int = 1500`：文本超过多少字符自动转 Artifact

**返回**：无

---

### 模块 2：Artifact 基础管理

#### 1. 创建 Artifact `create_artifact`

```
def create_artifact(
    self,
    session_id: str,
    name: str,
    artifact_type: str,
    content: str,
    summary: str = "",
    change_log: str = "初始版本"
) -> Artifact
```

**功能**：创建新 Artifact 并生成 v1 版本，自动统计 Token、生成摘要。

**入参**：

- `session_id`：所属会话 ID
- `name`：Artifact 名称 / 文件名
- `artifact_type`：类型（code/markdown/html/data）
- `content`：初始完整内容
- `summary`：简短描述，留空则自动截取前 100 字
- `change_log`：首个版本的变更说明

**返回**：完整 `Artifact` 对象

#### 2. 获取单个 Artifact `get_artifact`

```
def get_artifact(self, artifact_id: str) -> Optional[Artifact]
```

**功能**：根据 ID 查询 Artifact 元数据（不含完整内容）。

#### 3. 列出会话下所有 Artifact `list_artifacts`

```
def list_artifacts(
    self,
    session_id: str,
    include_deleted: bool = False
) -> list[Artifact]
```

**功能**：查询指定会话下的所有 Artifact 列表。

#### 4. 删除 Artifact `delete_artifact`

```
def delete_artifact(self, artifact_id: str, soft_delete: bool = True) -> bool
```

**功能**：删除指定 Artifact，默认软删除。

---

### 模块 3：版本管理

#### 1. 创建新版本 `create_version`

```
def create_version(
    self,
    artifact_id: str,
    content: str,
    change_log: str = ""
) -> ArtifactVersion
```

**功能**：基于当前最新版本创建新版本，自动递增版本号、统计 Token。

**入参**：

- `artifact_id`：目标 Artifact ID
- `content`：新版本完整内容
- `change_log`：本次变更说明

**返回**：新版本 `ArtifactVersion` 对象

#### 2. 获取指定版本内容 `get_version_content`

```
def get_version_content(
    self,
    artifact_id: str,
    version: Union[int, str] = "latest"
) -> Optional[ArtifactVersion]
```

**功能**：获取指定版本号的完整内容，支持 `latest` 指代最新版本。

#### 3. 列出所有版本 `list_versions`

```
def list_versions(self, artifact_id: str) -> list[ArtifactVersion]
```

**功能**：按版本号倒序返回该 Artifact 的所有版本列表。

#### 4. 回退到指定版本 `rollback_version`

```
def rollback_version(
    self,
    artifact_id: str,
    target_version: int,
    create_new_version: bool = True
) -> ArtifactVersion
```

**功能**：回退到指定版本，默认生成新版本（不覆盖历史记录）。

---

### 模块 4：引用协议处理（对话交互核心）

#### 1. 生成标准引用标签 `generate_ref_tag`

```
def generate_ref_tag(
    self,
    artifact: Artifact,
    version: Union[int, str] = "latest"
) -> str
```

**功能**：生成标准 XML 格式的引用标签，用于插入对话消息。

**返回示例**：

```
<artifact id="art_abc123" name="fusion_agent.py" type="code" version="latest" token_count="4200">
  <summary>基于 MLX 的本地 Agent 主逻辑</summary>
</artifact>
```

#### 2. 从消息中解析引用 `parse_refs_from_message`

```
def parse_refs_from_message(self, message_content: str) -> list[ArtifactRef]
```

**功能**：从单条消息文本中识别并解析所有 Artifact 引用标签。

#### 3. 替换引用为完整内容 `replace_refs_with_content`

```
def replace_refs_with_content(
    self,
    message_content: str,
    refs: list[ArtifactRef] = None
) -> str
```

**功能**：将消息中的引用标签替换为对应版本的完整内容（代码块包裹）；不传 `refs` 则自动解析后替换。

---

### 模块 5：上下文注入与安全校验（核心防爆满）

#### 1. 按需注入到对话消息 `inject_artifacts_to_messages`

```
def inject_artifacts_to_messages(
    self,
    messages: list[dict],
    inject_target_refs: list[ArtifactRef] = None,
    inject_position: str = "last"
) -> tuple[list[dict], int]
```

**功能**：将指定 Artifact 的完整内容临时注入到对话消息中，返回处理后的新消息列表和总 Token 数。

- **不修改原始 messages**，返回全新列表
- 不传 `inject_target_refs` 则自动解析最后一条消息中的所有引用
- `inject_position`：注入位置，默认追加到最后一条消息末尾

**返回**：`(处理后的消息列表, 总 Token 数)`

#### 2. 上下文安全校验 `check_context_safety`

```
def check_context_safety(
    self,
    messages: list[dict],
    reserve_output_tokens: int = 8192
) -> tuple[bool, int, int]
```

**功能**：校验当前消息 Token 数是否在安全阈值内，防止超限报错。

**返回**：`(是否安全, 当前输入 Token 数, 剩余可用 Token 数)`

#### 3. 计算消息 Token 数 `calculate_messages_tokens`

```
def calculate_messages_tokens(self, messages: list[dict], model: str = None) -> int
```

**功能**：基于 litellm 计算消息列表的总 Token 数。

---

### 模块 6：自动识别与分流

#### 1. 判断是否需要转 Artifact `should_create_artifact`

```
def should_create_artifact(
    self,
    content: str,
    content_type: str = "text"
) -> bool
```

**功能**：根据配置的阈值，判断内容是否应该转为 Artifact。

#### 2. 自动包装为 Artifact `auto_wrap_to_artifact`

```
def auto_wrap_to_artifact(
    self,
    session_id: str,
    content: str,
    content_type: str = "code",
    name: str = None
) -> tuple[Artifact, str]
```

**功能**：自动将内容包装为 Artifact，返回 Artifact 对象和对应的引用标签。

---

### 模块 7：导入与导出

#### 1. 导出单个 Artifact `export_artifact`

```
def export_artifact(
    self,
    artifact_id: str,
    include_all_versions: bool = False,
    format: str = "json"
) -> str
```

**功能**：导出 Artifact 为 JSON / 纯文本格式，支持全版本导出。

#### 2. 批量导出会话 Artifact `batch_export_session_artifacts`

```
def batch_export_session_artifacts(
    self,
    session_id: str,
    output_dir: str
) -> int
```

**功能**：批量导出指定会话下所有 Artifact 的最新版本到本地目录，按文件名保存。

#### 3. 导入 Artifact `import_artifact`

```
def import_artifact(
    self,
    session_id: str,
    artifact_data: dict
) -> Artifact
```

**功能**：从导出的 JSON 数据中导入 Artifact 到指定会话。

---

## 三、存储层抽象接口（可插拔）

引擎底层通过统一抽象对接存储，业务层无需关心实现，默认提供本地 SQLite 实现。

```
class StorageDriver(ABC):
    def save_artifact(self, artifact: Artifact) -> None: ...
    def get_artifact(self, artifact_id: str) -> Optional[Artifact]: ...
    def save_version(self, version: ArtifactVersion) -> None: ...
    def get_version(self, artifact_id: str, version_num: int) -> Optional[ArtifactVersion]: ...
    def list_artifacts(self, session_id: str) -> list[Artifact]: ...
    def list_versions(self, artifact_id: str) -> list[ArtifactVersion]: ...
    def delete_artifact(self, artifact_id: str) -> bool: ...
```
