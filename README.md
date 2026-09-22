# gemini-quality-gate-jev

给 Gemini CLI 装上 [TypeSafe Jev](https://typesafe.ai/) 做的「质量门」：在关键节点问 Jev 几个结构化问题（要不要返工、这步危险吗、这个请求该不该做），再据此放行、打回、拦截或先弹出确认。

Jev 只回答判断，不生成代码或文字。安装时自己选要挂哪几个门，**至少一个、可多选**。

## 四个可选的门

| 门（Hook 事件） | 触发时机 | 问 Jev 什么 | 会做什么 |
| --- | --- | --- | --- |
| **AfterAgent** | 每轮回答结束后 | 这轮回答有没有必须补的遗漏；变更风险多高 | 有遗漏（或高风险+中等遗漏）→ 打回，让 Gemini 带着「复查需求、跑验证、给证据」再改一轮，只补一轮 |
| **BeforeTool** | 每次工具调用前 | 这步有多危险（0–3）；会不会泄露/覆盖密钥 | 高 → 拒绝这次调用；中 → 弹出确认让你决定；低 → 放行 |
| **BeforeAgent** | 每次收到你的请求 | 这个请求是否该拒绝；是否需要先做计划 | 明确违规 → 拒绝该请求；请求偏大 → 注入一条「先计划再动手」的上下文 |
| **SessionStart** | 会话开始（startup/resume） | 这个仓库当前有多高风险；验证负担多重 | 风险高 → 注入一条会话级验证提醒 |

三种失败行为：**放行**（默认，Jev 不可用时不阻塞你）、**打回**（只针对 AfterAgent，且只一轮）、**拦截/确认**（BeforeTool，可选改成 fail-closed）。

为控制延迟和费用，BeforeTool 会先在本地过滤：读文件、`ls`、`git status` 这类只读操作**根本不会请求 Jev**，只有 shell 命令、敏感文件写入和 MCP 工具才会送审。

## 前置条件

- Gemini CLI **0.60 或更高**
- **Python 3**（Hook 用系统 `python3` 执行）
- 一个 **TypeSafe Jev API key**，在 <https://console.typesafe.ai/settings/keys> 创建

## 安装

一条命令，安装时会让你选门：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/ZongxingH/gemini-quality-gate-jev/main/install.sh) --global
```

会先问你密钥（隐藏输入），再出现选择菜单：

```
Which Jev gates should be active? Choose one or more.

  1) AfterAgent    after each answer: ask Jev whether one correction pass is needed
  2) BeforeTool    before a tool runs: block or confirm destructive commands and secret exposure
  3) BeforeAgent   before each request: refuse unsafe asks, nudge broad ones to plan first
  4) SessionStart  at session start: inject a verification advisory for risky repos

Numbers separated by spaces or commas, or "all" [default: 1 = AfterAgent]:
```

输入 `2 3`、`AfterAgent,BeforeTool`、`all` 都可以；直接回车默认只挂 AfterAgent。**至少要选一个。**

非交互（CI、脚本）用 `--events`：

```bash
# 只要返工门和工具拦截门
./install.sh --global --events AfterAgent,BeforeTool

# 用序号、大小写随意
./install.sh --global --events "3, afteragent"

# 四个门全开
./install.sh --global --events all

# 只给某个项目启用
./install.sh --project /path/to/project --events BeforeTool,SessionStart

# 指定仓库/版本
./install.sh --global --repo https://github.com/ZongxingH/gemini-quality-gate-jev.git --ref main

# 密钥从文件读，全程不交互
./install.sh --global --events all --api-key-file ~/keys/jev.env
```

安装脚本会：读密钥 → 写入 `${XDG_CONFIG_HOME:-~/.config}/typesafe/jev.env`（目录 700、文件 600，不进仓库）→ 从 Git 仓库安装扩展 → 把门的选择写进 `…/typesafe/jev.json` → 按选择裁剪已安装的 `hooks/hooks.json` → 按 `--global`/`--project` 设置启用范围 → 用 `gemini extensions list -o json` 复核。

装完**重启 Gemini CLI**，然后确认：

```bash
gemini extensions list        # 应看到 gemini-quality-gate-jev，enabled
cat ~/.config/typesafe/jev.json
```

## 使用

重启后正常用 `gemini` 即可，不需要额外操作：

- **AfterAgent 打回**：会出现 `JEV requested a correction pass …`，Gemini 自动再答一轮；这一轮不会再次被打回。
- **BeforeTool 拦截**：危险命令会看到 `JEV blocked this tool call …`，模型会知道被拦并改方案；中等风险会弹出确认框，由你决定放行还是取消。
- **BeforeAgent 注入**：请求偏大时，你会在上下文里看到一条 `JEV pre-flight note`，要求先计划、跑验证。
- **SessionStart 提醒**：高风险仓库会注入一条验证提醒。
- **Jev 不可用 / 没配 key**：全部放行，只打印原因，不会卡住会话。

只想验证脚本本身是否工作（不启动 Gemini）：

```bash
printf '%s' '{"hook_event_name":"AfterAgent","cwd":".","prompt":"修复登录接口","prompt_response":"已改代码但没跑测试","stop_hook_active":false}' \
  | python3 ~/.gemini/extensions/gemini-quality-gate-jev/scripts/jev_hook.py
```

当前挂了哪些门：

```bash
python3 ~/.gemini/extensions/gemini-quality-gate-jev/scripts/jev_hook.py --print-events
```

## 配置

「挂了哪些门」写在 `~/.config/typesafe/jev.json`（安装脚本写入 `events`，其余键可自己加）：

```json
{
  "events": ["AfterAgent", "BeforeTool"],
  "thresholds": {
    "after_agent_retry": 0.85,
    "after_agent_risk_hard": 2.5,
    "after_agent_retry_soft": 0.5,
    "before_tool_ask": 1.5,
    "before_tool_deny": 2.5,
    "before_tool_leak_deny": 0.8,
    "before_agent_policy_deny": 0.9,
    "before_agent_plan_notice": 0.6,
    "session_notice": 1.5
  },
  "before_tool": {
    "fail_mode": "open"
  }
}
```

改完立即生效（下次 Hook 触发时读取）。改门的话重新跑一次 `install.sh --events …`，它会保留你在这里写的其他配置。

- `fail_mode`：`open`（默认，Jev 不可用时放行）或 `closed`（不可用时拒绝工具调用）。
- `before_tool.safe_command_prefixes`：本地直接放行的只读命令前缀，默认已含 `ls/cat/grep/git status/git diff/...`。
- `before_tool.sensitive_path_patterns`：写入这些路径的文件才会送审（`.env`、`.ssh/`、`*.pem`、`credentials` 等）。

环境变量（可选，覆盖配置）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TYPESAFE_API_KEY` | 无 | API key；未设置时读 `~/.config/typesafe/jev.env` |
| `TYPESAFE_API_URL` | `https://api.typesafe.ai/v1/systemone` | Jev API 地址 |
| `TYPESAFE_MODEL` | `jev-latest` | 使用的 Jev 模型 |
| `JEV_HOOK_TIMEOUT_SECONDS` | 每门 3–5 秒 | 单次 Jev 请求超时 |
| `JEV_CONFIG_FILE` | `~/.config/typesafe/jev.json` | 覆盖配置文件位置 |

## 更新与卸载

```bash
./install.sh --global --events all      # 重新运行即更新到最新版本，并重设门
./install.sh --uninstall                # 卸载扩展，保留密钥和门配置
./install.sh --uninstall --purge-key    # 连同密钥和门配置一起删除
./install.sh --global --dry-run         # 只打印将要执行的命令
./install.sh --help                     # 全部选项
```

## 数据与隐私

挂上之后，会把以下内容发送到 `api.typesafe.ai`：

- **AfterAgent**：用户请求、Gemini 的最终回答、`git status --short` 与 `git diff --stat`；
- **BeforeTool**：工具名、工具参数（只读操作不会发送）、工作区状态；
- **BeforeAgent / SessionStart**：用户请求或仓库结构摘要（顶层文件名、分支、变更数等，不发送文件内容）。

在敏感仓库使用前，请确认这符合你的合规要求；不需要某个门就不要挂它。

## 测试

```bash
./tests/run_all.sh
```

包含 Hook 行为测试（本地 mock Jev，不需要真实密钥）、安装脚本的门选择测试，以及针对真实 Gemini CLI 的端到端安装/卸载检查。
