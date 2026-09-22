# gemini-quality-gate-jev

给 Gemini CLI 装上 [TypeSafe Jev](https://typesafe.ai/) 做的「质量门」：在关键节点问 Jev 几个结构化问题（要不要返工、这步危险吗、这个请求该不该做），再据此放行、打回、拦截或先弹出确认。

Jev 只回答判断，不生成代码或文字。安装时自己选要挂哪几个门，**至少一个、可多选**。

## 四个可选的门

| 门（Hook 事件） | 触发时机 | 问 Jev 什么 | 会做什么 |
| --- | --- | --- | --- |
| **AfterAgent** | 每轮回答结束后 | 这轮回答有没有必须补的遗漏；变更风险多高 | 遗漏明确 → 打回让 Gemini 带证据再改一轮（只一轮）；遗漏中等 + 高风险且**风险读数可信** → 只要求补验证证据；其余 → 放行（中等档会标注"暂定"） |
| **BeforeTool** | 每次工具调用前 | 这步有多危险（0–3，带 confidence）；会不会泄露/覆盖密钥 | **高危险 + 高置信 → 自动拒绝**；中危险 / 低置信 / 密钥风险存疑 → **弹确认框交给你**；低 → 放行 |
| **BeforeAgent** | 每次收到你的请求 | 这个请求是否该拒绝；是否需要先做计划 | 默认只注入上下文（可能不安全 → 劝阻提示；请求偏大 → 先计划）；改成 `escalate` 才会拒绝请求 |
| **SessionStart** | 会话开始（startup/resume） | 这个仓库当前有多高风险（带 confidence）；验证负担多重 | 风险高且**读数可信** → 注入一条会话级验证提醒；读数不确定则不打扰 |

为控制延迟和费用，BeforeTool 会先在本地过滤：读文件、`ls`、`git status` 这类只读操作**根本不会请求 Jev**，只有 shell 命令、敏感文件写入和 MCP 工具才会送审。

## 决策怎么做的：官方三档（confidence-gated routing）

判定不是"一个阈值定音"，而是按 TypeSafe 官方推荐的 [confidence-gated routing](https://docs.typesafe.ai/patterns/confidence-routing) 分三档——**阈值决定动作落在哪一档，confidence 决定要不要真的动手**：

| 档位 | 官方定义 | 我们的实现 |
| --- | --- | --- |
| 高置信 | 自动执行（Act automatically） | 高危险 + `confidence ≥ 0.85` → 自动拒绝；遗漏概率 ≥ 0.85 → 自动要求返工一轮 |
| 中置信 | 谨慎处理：让人确认 / 收集更多信息 | BeforeTool → 弹确认框（`decision: ask`）；AfterAgent → 只要求"说出你验证了什么"；BeforeAgent → 注入提醒 |
| 低置信 | 不要执行（Do not act） | `confidence < 0.6` 时**不允许自动拒绝**，一律升级；SessionStart 风险读数不确定则完全不打扰 |

另外两点也按官方做：

- **阈值随风险缩放**：每个门、每类动作的阈值各自独立（官方原话 "a confidence threshold is not one number"），全部可在 `jev.json` 覆盖。
- **Noul 没有 confidence**：`needs_retry`、`secret_exposure`、`policy_violation` 这类是/否问题本身没有 `confidence` 字段，所以用**不确定带**代替硬切——概率落在 `uncertain_low`(0.5) 与动作阈值之间时，走"收集更多信息"而不是直接判定（对应官方 [self-consistency cookbook](https://docs.typesafe.ai/cookbooks/consistency_noul_cookbook) 把阈值附近概率交给人工复核的做法）。

每个门的"谁最终决策"由 `policy.gates` 显式声明：

| 门 | 默认 stance | 含义 |
| --- | --- | --- |
| `AfterAgent` | `auto` | 阈值自决策（动作只是多返工一轮，便宜且可逆） |
| `BeforeTool` | `escalate` | 不确定档交给你；没有人可问时按 `ask_fallback` 处理（默认拒绝） |
| `BeforeAgent` | `advisory` | 只提示不拦截（拦的是你自己输入的请求，不该由阈值单独定音） |
| `SessionStart` | `advisory` | 只注入上下文 |

> 注意：`ask` 只有 BeforeTool 支持（CLI 对其它事件不处理确认请求）。用 `gemini -p` / CI 无人值守时，请把 `policy.assume_human` 设为 `never`，让 `ask_fallback` 确定性地生效，避免等待确认。

三种失败行为：**放行**（默认，Jev 不可用时不阻塞你）、**打回**（只针对 AfterAgent，且只一轮）、**拦截/确认**（BeforeTool，可选改成 fail-closed）。

## 前置条件

- Gemini CLI **0.60 或更高**
- **Python 3**（Hook 用系统 `python3` 执行）
- 一个 **TypeSafe Jev API key**，在 <https://console.typesafe.ai/settings/keys> 创建

## 安装

一条命令（只用 `github.com`，参数照抄改值即可；`rm -rf` 那半句是让它可以重复执行）：

```bash
rm -rf /tmp/jev-gqg && git clone --depth 1 https://github.com/ZongxingH/gemini-quality-gate-jev.git /tmp/jev-gqg && bash /tmp/jev-gqg/install.sh --global --events all --api-key ts_xxxxxxxx
```

克隆完成后，改门、指定项目、更新、卸载都用同一个脚本：

```bash
bash /tmp/jev-gqg/install.sh --global                          # 全局启用 + 交互选门
bash /tmp/jev-gqg/install.sh --global --events AfterAgent,BeforeTool
bash /tmp/jev-gqg/install.sh --project /path/to/project --events BeforeTool,SessionStart
bash /tmp/jev-gqg/install.sh --global --events all --api-key-file ~/keys/jev.env
bash /tmp/jev-gqg/install.sh --uninstall
```

> 为什么不用 `bash <(curl -fsSL https://github.com/…/install.sh)`？GitHub 上取文件的地址（`/raw/…`、`/blob/…?raw=true`）都会 302 跳到 `raw.githubusercontent.com`，这个域名在受限网络里连不上（就是你看到的 `curl: (35)`）。`git clone` 是唯一只走 `github.com` 的取法。

参数说明：

| 参数 | 取值 | 说明 |
| --- | --- | --- |
| `--global` | — | 当前用户的所有项目都启用 |
| `--project PATH` | 项目目录路径 | 只在该项目启用（与 `--global` 二选一，不写默认 `--global`） |
| `--events LIST` | `AfterAgent`、`BeforeTool`、`BeforeAgent`、`SessionStart` | 挂哪几个门。可多选，逗号或空格分隔；也可写序号 `1`–`4`、或 `all`。不写则交互选择，直接回车 = 只挂 `AfterAgent`。**至少要有一个** |
| `--repo URL` | Git 仓库地址，或本地目录 | 默认官方仓库；本地目录时不支持 `--ref` |
| `--ref REF` | 分支 / tag / commit | 只对 Git 源有效 |
| `--api-key KEY` | TypeSafe Jev key | 不写则交互隐藏输入（key 会留在 shell 历史里，CI 更推荐下面两种） |
| `--api-key-file PATH` | 文件路径 | 文件内容是裸 key，或一行 `TYPESAFE_API_KEY=...` |
| `--key-file PATH` | 文件路径 | key 的保存位置，默认 `${XDG_CONFIG_HOME:-~/.config}/typesafe/jev.env` |
| `--dry-run` | — | 只打印将要执行的命令，不做任何改动 |
| `--uninstall` | — | 卸载扩展，保留密钥和门配置 |
| `--purge-key` | 配合 `--uninstall` | 连密钥和门配置一起删除 |
| `-h, --help` | — | 列出全部选项 |

密钥也可以直接用环境变量给：`TYPESAFE_API_KEY=ts_xxx bash /tmp/jev-gqg/install.sh --global`。

安装脚本会：读密钥 → 写入 `${XDG_CONFIG_HOME:-~/.config}/typesafe/jev.env`（目录 700、文件 600，不进仓库）→ 从 Git 仓库安装扩展 → 把门的选择写进 `…/typesafe/jev.json` → 按选择裁剪已安装的 `hooks/hooks.json` → 按 `--global`/`--project` 设置启用范围 → 用 `gemini extensions list -o json` 复核。

装完**重启 Gemini CLI**，然后确认：

```bash
gemini extensions list        # 应看到 gemini-quality-gate-jev，enabled
cat ~/.config/typesafe/jev.json
```

## 使用

重启后正常用 `gemini` 即可，不需要额外操作：

- **AfterAgent 打回**：会出现 `JEV requested a correction pass …`，Gemini 自动再答一轮；这一轮不会再次被打回。中等遗漏 + 高风险时只会要求"给出验证证据"；概率在中间档但风险不高时，回答照常，但会标注为"暂定"（provisional）。
- **BeforeTool 拦截**：高危险 + 高置信 → 直接看到 `JEV blocked this tool call …`，模型会改方案；中危险 / 低置信 / 密钥风险存疑 → 弹出确认框，由你决定放行还是取消；没有人可确认时按 `ask_fallback`（默认拒绝）。
- **BeforeAgent 提示**：默认只注入上下文——请求偏大时是一条 `JEV pre-flight note`（先计划、跑验证），请求可能不安全时是一条劝阻提示；不会拒绝你的请求，除非把该门改成 `escalate`。
- **SessionStart 提醒**：高风险仓库且读数可信时注入一条验证提醒。
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

「挂了哪些门」写在 `~/.config/typesafe/jev.json`（安装脚本只写 `events`，其余键按需自己加；缺省值见下）：

```json
{
  "events": ["AfterAgent", "BeforeTool"],
  "policy": {
    "gates": {
      "AfterAgent": "auto",
      "BeforeTool": "escalate",
      "BeforeAgent": "advisory",
      "SessionStart": "advisory"
    },
    "confidence_floor": 0.6,
    "auto_act_confidence": 0.85,
    "uncertain_low": 0.5,
    "assume_human": "auto",
    "ask_fallback": "deny"
  },
  "thresholds": {
    "after_agent_retry": 0.85,
    "after_agent_risk_hard": 2.5,
    "before_tool_ask": 1.5,
    "before_tool_deny": 2.5,
    "before_tool_leak_deny": 0.8,
    "before_agent_policy_deny": 0.9,
    "before_agent_plan_notice": 0.6,
    "session_notice": 1.5,
    "session_burden_notice": 0.8
  },
  "before_tool": {
    "fail_mode": "open"
  },
  "log_decisions": false
}
```

改完立即生效（下次 Hook 触发时读取）。改门的话重新跑一次安装脚本并带上 `--events …`（见上面的 [`更新与卸载`](#更新与卸载)），它会保留你在这里写的其他配置。

**决策相关**

- `policy.gates.<门>`：`auto`（阈值自决策）/ `escalate`（不确定档交人）/ `advisory`（只提示不拦截）。
- `policy.confidence_floor`：全局置信度地板。低于它**不允许自动拒绝**，只升级（官方三档里的"低置信 → 不要执行"）。
- `policy.auto_act_confidence`：高风险动作要自动执行所需的置信度。
- `policy.uncertain_low`：noul 问题的"不确定带"下界（与各动作阈值之间的区间算中等档）。
- `policy.assume_human`：`auto`（探测控制终端，探测不到就认为没人可问）/ `always` / `never`。
- `policy.ask_fallback`：需要人确认但没人可问时怎么办——`deny`（默认，安全）或 `allow`（不阻塞 CI）。
- `log_decisions`：设为 `true` 后每次判定都会追加一行到 `~/.config/typesafe/jev-decisions.jsonl`，含 Jev 返回的 `probabilities` 和 confidence，用来按真实流量校准阈值（官方也建议把完整概率分布记下来再调阈值）。

**其它**

- `before_tool.fail_mode`：Jev 不可达时 `open`（默认，放行）或 `closed`（拒绝工具调用）。
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
| `JEV_ASSUME_HUMAN` | 未设置 | `1`/`0` 强制"有没有人可确认"，覆盖 `policy.assume_human` |

## 更新与卸载

都用克隆下来的那个脚本：

```bash
# 更新扩展并重设门（clone 那条命令重新执行一次即可拿到最新脚本）
git clone --depth 1 https://github.com/ZongxingH/gemini-quality-gate-jev.git /tmp/jev-gqg && bash /tmp/jev-gqg/install.sh --global --events all

# 卸载：保留密钥和门配置 / 连密钥和门配置一起删除
bash /tmp/jev-gqg/install.sh --uninstall
bash /tmp/jev-gqg/install.sh --uninstall --purge-key
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
