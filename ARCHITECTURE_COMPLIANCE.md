# 架构合规整改计划

审计日期: 2026-08-02
关联 Issue: #22
违规等级: P1
合规评级: C+

层级定位: 二、核心网关引擎 - 结构化制品引擎
核心职责: 制品版本管理、存储、检索

违规项与整改:
1. render_artifact() HTML渲染/注入CDN脚本 - 提取到前端 - P1-S1
2. interact_artifact() 用户交互处理 - 提取到前端 - P1-S1
3. injection.py LLM消息注入 - 提取到上层应用/聊天层 - P1-S1
4. token_counter.py Token计数 - 提取到LLM基础设施 - P1-S1
5. 浏览器端点+CSP - 移除浏览器端点, 改为内部服务接口 - P1-S2
6. tool_schemas/ OpenAI function-calling - 提取到AI代理集成层 - P1-S2
7. sync_artifact_file() 代码编辑器双向文件同步 - 提取到IDE集成 - P1-S3

合规标准: artifacts-engine应只包含制品版本CRUD/存储/检索/元数据管理, 不应包含渲染/交互/LLM注入/Token计数/浏览器端点
