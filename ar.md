# Fusion Artifacts 完整落地方案

本方案面向 Fusion 系列产品（本地桌面端 + 云端服务均可复用），以**解决上下文溢出、沉淀生成资产、提升开发者体验**为核心目标，采用「MVP 最小可用 → 渐进式增强」的落地路径，全程兼容你现有 litellm / MLX 技术栈。

---

## 一、项目目标与核心价值

### 1.1 核心目标

1. **根治上下文爆满**：将长代码、大文档从对话消息中物理剥离，对话 Token 占用降低 80%~90%，从根源避免 200k 上限报错。
2. **资产化沉淀**：所有生成的代码、方案、配置从「一次性聊天内容」变为「可管理、可版本、可复用的项目资产」。
3. **开发者体验升级**：左右分栏专注工作流，聊天谈思路、右侧出成品，替代在聊天记录里翻找代码的低效模式。
4. **产品差异化**：打造国内 AI 工具中少有的「原生 Artifacts 体系」，形成对同类产品的体验代差。

### 1.2 解决的核心痛点

- 长代码多轮迭代后，对话上下文迅速撑满 200k，触发 400 报错
- 代码混在聊天流中，查找、复制、版本对比极其不便
- 生成成果无法跨会话复用，每次重新生成浪费 Token
- 缺少结构化的项目产出管理，用户留存弱

---

## 二、核心设计原则

1. **无感知优先**：模型输出长内容时自动转为 Artifact，用户无需手动操作
2. **对话零侵入**：对话流只保留轻量引用，绝不塞入完整大文本
3. **版本可追溯**：每次修改生成新版本，支持一键回退与对比
4. **本地优先**：Fusion 桌面端默认本地存储，兼容离线使用；云端版可选同步
5. **标准兼容**：引用格式标准化，未来可扩展支持第三方插件与导入导出

---

## 三、产品形态与交互设计

### 3.1 整体布局：左右双栏架构

表格

| 区域 | 左侧（对话区，占比 40%） | 右侧（Artifacts 面板，占比 60%） |
| --- | --- | --- |
| **核心内容** | 用户指令、模型思路、简短说明、Artifact 引用卡片 | 完整代码 / 文档 / 预览、版本列表、操作工具栏 |
| **交互** | 正常聊天、点击引用卡片跳转右侧对应版本 | 编辑、运行、下载、分享、版本切换、回退 |
| **Token 关联** | 计入对话上下文 | 默认不计入对话上下文，按需读取 |

### 3.2 Artifact 核心属性与类型

#### 支持的 4 类核心 Artifact

1. **代码文件**：Python/JS/Shell/ 配置文件等，支持语法高亮、一键复制下载
2. **文本文档**：PRD、技术方案、报告、Markdown 文档，支持渲染预览
3. **HTML 应用**：可直接在沙箱中运行的网页、仪表盘、小工具
4. **数据文件**：JSON/CSV 数据集、Mermaid 流程图、SVG 图表

#### 每个 Artifact 必备属性

- 唯一 ID：`art_xxxxxx`
- 名称：文件名 / 文档标题
- 类型：code /markdown/html /data
- 所属会话：关联的对话 ID
- 版本号：v1 /v2 /...，支持多版本
- 创建 / 更新时间
- 摘要：100 字以内的简短描述（用于对话引用时的模型感知）

### 3.3 核心交互流程

1. **自动生成**：模型输出超过阈值（如 30 行代码 / 500 字文档），自动在右侧生成 Artifact，左侧仅显示一张引用卡片
2. **点击跳转**：点击左侧引用卡片，右侧自动定位到对应 Artifact 及版本
3. **迭代修改**：用户说「修改这个文件，增加 XX 功能」，模型直接基于当前版本修改，生成 v2 版本，对话中仅追加新引用
4. **版本回退**：右侧版本列表点击历史版本，可一键回退并继续编辑
5. **导出复用**：支持单文件下载、批量导出项目、复制为 Artifact 引用

---

## 四、技术架构总览

### 4.1 分层架构（框框图）

```
┌─────────────────────────────────────────────────────────┐
│                     前端表现层                          │
│  对话组件  │  Artifact 面板  │  代码编辑器  │  预览沙箱  │
├─────────────────────────────────────────────────────────┤
│                  Artifact 业务引擎层                    │
│  自动识别器  │  版本管理器  │  引用解析器  │  注入控制器 │
├─────────────────────────────────────────────────────────┤
│                   对话编排层 (litellm)                  │
│  Token 计数  │  消息组装  │  流式处理  │  异常拦截     │
├─────────────────────────────────────────────────────────┤
│                     存储层                              │
│  元数据 SQLite  │  内容文件系统  │  增量版本存储        │
└─────────────────────────────────────────────────────────┘
```

### 4.2 各层职责说明

1. **前端表现层**：负责双栏渲染、代码高亮、HTML 沙箱预览、版本切换交互
2. **Artifact 业务引擎层**：核心中枢，负责识别长内容、生成引用、管理版本、控制何时将完整内容注入模型上下文
3. **对话编排层**：基于 litellm 扩展，负责 Token 统计、消息组装、流式响应解析、超限拦截
4. **存储层**：本地端用 SQLite 存元数据 + 文件系统存内容；云端版可替换为 MySQL + 对象存储

---

## 五、核心机制：上下文隔离与 Token 管控（最关键）

### 5.1 物理隔离原理

**核心逻辑：对话存引用，内容存外部，按需才注入**

- 普通对话消息：只存 `<artifact>` 引用标签 + 简短摘要，单条引用仅约 50~100 Token
- Artifact 完整内容：独立存储在本地文件 / 数据库，不随对话消息每轮传递
- 仅当用户明确要求「修改 / 查看 / 基于此优化」时，引擎才将对应版本的完整内容临时注入当前轮上下文，用完即走，不沉淀到历史消息

### 5.2 引用协议规范

采用 XML 风格标签，便于模型识别与引擎解析，同时 Token 占用极低。

**对话消息中的标准引用格式：**

```
<artifact id="art_abc123" name="fusion_agent.py" type="code" version="v3" token_count="4200">
  <summary>基于 MLX 的本地 Agent 主逻辑，含工具调用与记忆模块</summary>
</artifact>
```

- 单条引用 Token 开销：约 80~120 Token（对比 4200 Token 的完整代码，节省 97%）
- 模型可通过 summary 感知内容，无需读取全文即可承接对话

### 5.3 按需注入机制

**何时注入完整内容？**

1. 首次生成：模型输出时直接生成，不计入后续对话历史
2. 用户指令包含「修改这个文件」「优化这段代码」「基于刚才的文档」等明确指向时
3. 用户主动点击「让模型修改」按钮时

**注入规则：**

- 仅注入**当前最新版本**的完整内容，历史版本不注入
- 注入仅作用于**当前一轮**请求，完成后不追加到对话历史消息中
- 注入前做 Token 校验：当前对话 + 注入内容 + 预留输出 ≤ 安全阈值（如 180k），超限则提示用户先清理历史或拆分任务

### 5.4 Token 统计规则

表格

| 内容 | 是否计入对话上下文 | 是否计费 |
| --- | --- | --- |
| 对话文本、指令、思路说明 | 是 | 是 |
| Artifact 引用标签 + 摘要 | 是 | 是（可忽略的极小开销） |
| Artifact 完整内容（首次生成输出） | - | 计入输出 Token |
| Artifact 完整内容（按需注入输入） | 临时计入当轮 | 计入当轮输入 Token |
| Artifact 历史版本内容 | 否 | 否 |

---

## 六、数据结构设计

### 6.1 Artifact 主表（本地 SQLite 示例）

```
CREATE TABLE artifacts (
    id TEXT PRIMARY KEY,           -- art_开头的唯一ID
    session_id TEXT NOT NULL,      -- 所属会话ID
    name TEXT NOT NULL,            -- 文件/文档名称
    type TEXT NOT NULL,            -- code/markdown/html/data
    current_version INTEGER NOT NULL DEFAULT 1,
    summary TEXT,                  -- 简短描述
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    is_deleted INTEGER DEFAULT 0
);
```

### 6.2 Artifact 版本表

```
CREATE TABLE artifact_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_id TEXT NOT NULL,
    version_num INTEGER NOT NULL,
    content TEXT NOT NULL,         -- 完整内容（小文件存表，大文件存磁盘）
    content_path TEXT,             -- 大文件磁盘路径
    token_count INTEGER NOT NULL,  -- 该版本Token数
    change_log TEXT,               -- 版本变更说明
    created_at INTEGER NOT NULL,
    FOREIGN KEY (artifact_id) REFERENCES artifacts(id)
);
```

### 6.3 对话消息存储规范

- 历史消息表中，仅保存带引用标签的消息文本
- **绝不保存 Artifact 完整内容到消息表**
- 引用标签可被引擎快速解析与定位

---

## 七、分阶段落地路线图

### 7.1 MVP 阶段（2 周，最小可用，优先落地）

**核心目标：先解决上下文爆满问题，跑通基础流程**

1. 前端双栏布局：左侧对话 + 右侧 Artifact 面板
2. 支持代码类 Artifact：自动识别、语法高亮、一键复制下载
3. 基础版本管理：每次修改生成新版本，可切换查看
4. 核心隔离机制：对话存引用、内容独立存储、按需注入
5. Token 防护：注入前自动校验，防止超限报错
6. 本地存储：SQLite + 文件系统，完全离线可用

**交付标准**：生成 10 轮 1000 行代码迭代，对话 Token 不超过 20k，不触发 200k 超限。

### 7.2 Beta 阶段（4 周，体验完善）

1. 扩展类型：支持 Markdown 文档渲染、Mermaid 流程图
2. HTML 沙箱预览：可直接运行生成的网页应用
3. 自动识别增强：智能判断内容长度与类型，自动分流
4. 批量管理：Artifact 列表、分类、搜索、批量导出
5. 会话间复用：跨对话引用已有 Artifact，无需重新生成
6. 性能优化：增量版本存储，减少磁盘占用

### 7.3 正式版阶段（6 周，生态能力）

1. 代码沙箱运行：支持 Python/Node 脚本本地执行与结果展示
2. 分享与协作：生成只读分享链接，支持评论
3. 项目级管理：按项目组织 Artifact，一键导出完整项目包
4. 插件扩展：支持第三方模板、自定义 Artifact 类型
5. 云端同步：多设备同步 Artifact 资产

---

## 八、核心模块详细实现

### 8.1 前端实现要点

#### 技术选型（适配桌面端）

- 框架：React / Vue 均可，配合 Tauri/Electron 桌面端
- 代码编辑器：Monaco Editor（VSCode 同款），支持语法高亮、Diff 对比
- Markdown 渲染：Marked + Shiki 代码高亮
- HTML 预览：iframe + sandbox 沙箱，严格隔离

#### 流式输出识别逻辑（前端核心）

```
// 流式响应逐块接收时，自动识别代码块边界
let inArtifact = false;
let currentArtifact = { name: '', type: '', content: '' };

function handleStreamChunk(chunk) {
  if (chunk.includes('```') && !inArtifact) {
    inArtifact = true;
    // 提取语言类型、文件名
    currentArtifact.type = extractLang(chunk);
    currentArtifact.name = extractFileName(chunk);
    return; // 不渲染到聊天区
  }
  
  if (chunk.includes('```') && inArtifact) {
    inArtifact = false;
    // 提交到 Artifact 引擎，生成引用卡片
    createArtifact(currentArtifact).then(refCard => {
      appendToChat(refCard); // 聊天区只放引用卡片
    });
    currentArtifact = { name: '', type: '', content: '' };
    return;
  }

  if (inArtifact) {
    currentArtifact.content += chunk;
    // 同步渲染到右侧面板
    updateArtifactPanel(currentArtifact);
  } else {
    // 普通文本，正常渲染到聊天区
    appendToChat(chunk);
  }
}
```

### 8.2 Artifact 引擎（本地核心服务）

#### 核心函数定义

```
class ArtifactEngine:
    # 1. 创建新Artifact
    def create(self, session_id, name, type, content, summary="") -> Artifact:
        token_count = count_tokens(content)
        artifact = Artifact(session_id=session_id, name=name, type=type, summary=summary)
        # 保存元数据 + 首个版本
        self._save_meta(artifact)
        self._save_version(artifact.id, 1, content, token_count, "初始版本")
        # 返回引用标签
        return artifact.to_ref_tag()

    # 2. 解析消息中的引用
    def parse_refs(self, message: str) -> list[ArtifactRef]:
        return re.findall(r'<artifact[^>]*>.*?</artifact>', message)

    # 3. 按需注入：将引用替换为完整内容
    def inject_content(self, messages: list, refs: list[ArtifactRef]) -> list:
        injected = messages.copy()
        for ref in refs:
            content = self.get_version_content(ref.id, ref.version)
            # 将引用标签替换为完整代码块
            injected[-1]["content"] = injected[-1]["content"].replace(
                ref.raw_tag,
                f"\n```\n{content}\n```\n"
            )
        return injected

    # 4. Token安全校验
    def check_safe(self, messages: list, max_context=180000) -> bool:
        total = litellm.token_counter(model=MODEL, messages=messages)
        return total < max_context
```

### 8.3 模型侧适配

#### 系统提示词注入

在系统 Prompt 中加入规则，引导模型规范输出：

```
你具备 Artifact 生成能力：
1. 当输出超过 30 行代码或 500 字文档时，必须使用 ``` 代码块包裹，并标注文件名
2. 不要在正文中重复粘贴完整代码，用简短语言说明思路，代码放在代码块中
3. 修改已有 Artifact 时，基于最新版本输出完整更新后的代码
4. 保持对话简洁，长内容全部放入代码块，由系统自动转为 Artifact
```

#### 流式响应拦截

在 litellm 的流式回调中增加识别逻辑，后端兜底识别长内容，避免前端漏判。

### 8.4 存储层实现（本地桌面端）

- **元数据**：SQLite 单文件存储，路径 `~/.fusion/artifacts/meta.db`
- **内容文件**：按 Artifact ID 分目录存储，路径 `~/.fusion/artifacts/content/{art_id}/v{num}.{ext}`
- **版本优化**：超过 10 个版本后，旧版本采用 diff 增量存储，减少磁盘占用
- **索引**：按会话 ID、名称、类型建索引，支持快速搜索

---

## 九、安全与沙箱方案

1. **HTML 预览沙箱**
   - 使用 `<iframe sandbox="allow-scripts">` 禁用所有危险权限
   - 禁止跨域请求、本地文件访问、弹窗
   - 内容全部内联，不加载外部资源
2. **代码运行安全（正式版）**
   - Python/JS 运行在独立沙箱进程中
   - 限制文件系统访问范围，仅允许指定工作目录
   - 限制 CPU、内存、执行时长，防止恶意代码
3. **数据安全**
   - 本地端数据全部存储在用户本地，不上传（除非用户主动开启同步）
   - 敏感内容支持加密存储

---

## 十、验收指标与效果预期

表格

| 指标 | 无 Artifacts | MVP 版本 | 正式版 |
| --- | --- | --- | --- |
| 10 轮代码迭代对话 Token 量 | ~120k | ~15k | ~10k |
| 触发 200k 超限的平均轮次 | 15~20 轮 | 80~100 轮 | 200+ 轮 |
| 用户查找代码平均耗时 | 12 秒 | 1 秒 | <1 秒 |
| 代码复用率 | <10% | ~40% | ~70% |

---

## 十一、MVP 快速启动清单

1. 建表：创建 artifacts 和 artifact\_versions 两张表
2. 前端：拆分左右双栏，右侧接入 Monaco Editor
3. 引擎：实现 create、parse\_refs、inject\_content 三个核心函数
4. 接入：在 litellm 请求前增加注入 + Token 校验逻辑
5. 前端流式识别：自动识别 \`\`\` 代码块，分流到右侧面板
6. 测试：连续 10 轮代码修改，验证 Token 占用与功能正确性

那就是说fusion-artifacts-engine是基于fusion-mlx之上的fusion-code之下的独立技术组件，不做产品，只做技术组件

已完成思考

你的核心判断完全准确，我再给你补一个更精准的层级定位和边界划分，对齐你「一核九端」的整体架构。

## 一、最终定位结论

**fusion-artifacts-engine 是「模型层之上、所有产品线之下」的通用技术中间件**：

- 它不做独立产品，没有独立用户入口，纯技术组件
- 它不绑定 Fusion Code，Fusion Code 只是第一个深度集成的首发产品线
- 它不强依赖 fusion-mlx，向下兼容所有模型源（本地 MLX、云端 GLM/Claude 等）
- 它属于 Fusion 底层「一核」的公共能力，服务上面的「九端」所有产品

## 二、精准层级关系

从下到上的完整分层：

```
┌──────────────────────────────────────────────────┐
│  产品层（九端）                                  │
│  Fusion Code │ Fusion Design │ Fusion Science …  │
│  （各自做UI + 业务联动）                         │
├──────────────────────────────────────────────────┤
│  通用中间件层（一核的公共能力）                   │
│  ┌─────────────┐  ┌─────────────┐  ┌──────────┐  │
│  │ Artifacts   │  │ 对话编排    │  │ 记忆模块 │  │
│  │ Engine      │  │ (litellm)   │  │          │  │
│  └─────────────┘  └─────────────┘  └──────────┘  │
├──────────────────────────────────────────────────┤
│  模型推理层                                      │
│  fusion-mlx（本地） │ 云端模型（GLM/Claude等）   │
└──────────────────────────────────────────────────┘
```

### 层级关系说明

1. **最底层：模型推理层**
   - fusion-mlx 负责本地模型的加载、推理、加速
   - 云端模型通过 API 对接
   - 只负责「输入 prompt → 输出结果」，不关心业务逻辑
2. **中间层：通用中间件层**
   - fusion-artifacts-engine 就在这一层，和对话编排、记忆模块平行
   - 它的输入是「对话消息 + 模型输出」，输出是「引用标签 + 结构化资产」
   - 向下只依赖模型的输入输出协议，不绑定具体模型实现
   - 向上给所有产品线提供统一的 API，所有产品调用方式一致
3. **最上层：产品层**
   - Fusion Code：深度集成，做代码编辑器、文件写入、终端联动等定制化 UI 和业务逻辑
   - 其他产品：按需接入，只做对应类型的预览和业务联动
   - 所有产品都复用中间件层的核心能力，不用重复实现

## 三、明确职责边界

### ✅ fusion-artifacts-engine 负责（纯通用逻辑，无 UI）

- Artifact 元数据与版本的增删改查
- 对话消息中的引用标签解析与替换
- 内容按需注入上下文的控制逻辑
- Token 计数与安全阈值校验
- 存储抽象层（本地文件 / SQLite / 云端存储）
- 通用的内容识别规则（长度、类型判断）
- 通用导入导出能力

### ❌ fusion-artifacts-engine 不负责

- 任何 UI 渲染、前端组件（全部放到各产品线）
- 具体业务逻辑（比如代码写入本地文件、设计稿导出图片）
- 模型推理本身（不碰 fusion-mlx 的内部逻辑）
- 特定产品线的定制规则（比如代码的 Lint、格式化）
- 用户账号、权限、付费等产品层逻辑

## 四、这么设计的核心优势

1. **复用性最大化**：一套引擎全系列用，避免每条产品线重写一遍
2. **解耦彻底**：模型层换模型、产品层改交互，都不影响中间件
3. **可测试性强**：引擎可以单独做单元测试，不依赖前端和具体模型
4. **迭代节奏分离**：引擎稳定后低频迭代，业务层可以快速上新功能
5. **符合你「一核九端」的架构理念**：把通用能力沉淀到底层，上层专注业务差异化


