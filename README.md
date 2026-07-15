# Sentinel

> 面向**多人协作大型代码库**的**可观测性守护 Agent**：读懂他人代码的业务意图，识别监控盲区，自动补齐埋点 / 看板 / 告警，并防止可观测性随提交而退化。

**北极星**：让大仓的可观测性不再随提交腐烂。
**术语**：建立 **Baseline（基线）** → 检测 **Drift（漂移）** → 量化 **Coverage（覆盖度）** → 自动 **Restore（修复）** → 持续 **Guard（守护）**。

完整设计见 [DESIGN.md](DESIGN.md)。本项目按《Hello-Agents》教材章节，一步步教学式重写；每步对应一章、都能独立跑通。

---

## 快速开始

```bash
# 1) 安装依赖
pip install -r requirements.txt

# 2) 配置模型（密钥只填 .env，绝不填 .env.example）
cp .env.example .env
# 编辑 .env：SENTINEL_PROVIDER=openai/deepseek/moonshot，并填 SENTINEL_API_KEY

# 3) 验证链路（没配 key 时自动离线回显）
PYTHONPATH=src python3 -m sentinel ping "你好"

# 4) 跑测试
PYTHONPATH=src python3 -m pytest tests/ -q
```

---

## 进度

| 步骤 | 章节 | 内容 | 状态 |
|---|---|---|---|
| 0 脚手架 | 1/3 | 配置 + LLM 客户端 + `sentinel ping` | ✅ |
| 1 三范式 agent core | 4 | Plan → Act(ReAct) → Reflect | ✅ |
| 2 领域工具·扫描 | — | AST 扫描找监控盲区 | ⏳ |
| 3 记忆与检索 RAG | 8 | 切块 → 向量化 → 检索 | ⏳ |
| 4 上下文工程 | 9 | top-K + 去重 + token 预算 | ⏳ |
| 5 造框架 | 6/7 | Agent/Tool/Memory/LLM 接口 | ⏳ |
| 6 git 增量 + 漂移 | — | `--changed` + `git blame` 路由 | ⏳ |
| 7 生成 + 部署 | — | 埋点/告警/看板 → Grafana + Slack | ⏳ |
| 8 评估 | 12 | fixtures + P/R/F1 | ⏳ |
| 9 通信协议 | 10 | MCP server | ⏳ |
| 10 Agentic-RL | 11 | 反馈学习 | ⏳ |
| 11 驾驶舱 + 毕设 | 13-16 | Web + 文档 | ⏳ |

---

## 架构

```
入口层     CLI（主）+ 对话 UI Web（demo）
编排层     AgentCore：Plan → Act(ReAct) → Reflect   ← 第4章
工具层     统一工具注册表（一次定义，多处复用）
认知层     检索 RAG + 上下文工程 + 分层记忆          ← 第8/9章
领域层     扫描/AST切块 · 意图判定 · 生成 · 漂移检测
基础层     LLMClient + Config/.env + SQLite/向量库    ← 第3章
```

三条安全防线：`§7.3` 防幻觉（吹错）· `§13` 容错（跑失败）· `§14` 权限隔离（做坏事）。

---

## 三范式（第 1 步实现，`engines/agent.py`）

一个 `AgentCore` = **一个大脑、三个阶段**（不是三个 Agent）：

```
plan()    Plan-and-Execute  动手前先把目标拆成有序计划
act()     ReAct             Thought → Action → Observation 循环，调用工具
reflect() Reflection        行动后自评是否达成目标，不达标则重规划
```

第 1 步用 `echo` / `add` 玩具工具验证循环骨架，第 2 步换成真实领域工具（`scan`/`retrieve`/`judge_intent`），**骨架不变，只换工具**。

### 为什么 Act 用「手搓文本 ReAct」而不用 Function-calling

**Function-calling（函数调用）** 是 OpenAI 等厂商提供的结构化工具调用能力：把工具 schema 用 JSON 声明给模型，模型直接返回结构化调用意图（`{"name":"add","arguments":{...}}`），SDK 帮你解析。

我们**故意不用它**，Act 层采用「模型输出 `Action: add[3, 4]`，我们用正则解析」的手搓文本 ReAct：

| | 手搓文本 ReAct（本项目 ✅） | Function-calling |
|---|---|---|
| 依赖 | 零依赖，任何模型可用 | 绑定 OpenAI 兼容接口 |
| 原理 | 看得见 Thought→Action→Observation | 被 SDK 黑盒 |
| 教学 | 贴《Hello-Agents》第 4 章 | 略过原理 |

Function-calling 留作后期工程优化；教学阶段先手搓，理解 ReAct 的本质。

### 内建容错（DESIGN §13）

- `max_steps` 防跑飞、相同调用去重防死循环；
- 工具异常**结构化 `{error}` 回喂**模型（不崩）、失败步不阻断整体；
- Plan/Reflect 的 JSON 解析失败都有兜底，防止死循环。
