# 当前实现分析：Gemini CLI + Jev 质量门

分析对象：本仓库当前工作区（commit `7ed691a` + 未提交的 `gemini-extension.json`、`hooks/`、`install.sh`）。
验证环境：本机 Gemini CLI **0.60.0**（`@google/gemini-cli`，bundle 源码逐段核对）、macOS、Python 3。
外部契约核对：[TypeSafe Jev API 参考（apidog 整理）](https://apidog.com/blog/jev-api-key/)、[Simon Willison: Jev / System One](https://simonwillison.net/2026/sep/21/jev/)。

---

## 0. 结论速览

| 目标 | 状态 |
|---|---|
| AfterAgent 钩子在 Gemini 完成一轮后触发，把请求/回答/Git 摘要交给 Jev | ✅ 已实现，契约与 0.60.0 一致 |
| 输出两个结构化判断（`needs_retry` 的 noul + `risk` 的 score） | ✅ 请求体与 Jev 类型系统一致，实测端点接受 |
| `deny` 触发一次自动修正，且不会无限重试 | ✅ 逻辑正确（`stop_hook_active` 兜底） |
| Jev/网络/配置异常时放行（fail-open） | ✅ 四条路径均已实测 |
| 从 Git 仓库安装、要求 `TYPESAFE_API_KEY`、支持全局或指定项目 | ✅ 脚本已重写并端到端实测 |
| **从默认远端仓库安装** | ✅ 扩展文件已推送（`b32eadd`），并已用默认远端仓库跑通完整安装 |

> 一句话：**质量控制门本身功能是完整的**；本轮补齐/修正了 3 处实现缺陷与整套安装脚本，并把扩展文件推送到 GitHub（`b32eadd`）后用默认远端仓库实测通过。

---

## 1. 功能点逐条核对

| # | 功能要求 | 实现位置 | 结论 |
|---|---|---|---|
| 1 | Gemini 完成一轮后触发 | `hooks/hooks.json` 的 `AfterAgent` | ✅ 扩展 hooks 目录、`${extensionPath}` 变量、事件名均与 CLI 0.60.0 加载器一致 |
| 2 | 取用户请求与最终回答 | `event["prompt"]` / `event["prompt_response"]` | ✅ 字段名与 `fireAfterAgentEvent()` 一致 |
| 3 | 取工作区摘要 | `git status --short` + `git diff --stat`（各 1.5s 超时、失败忽略） | ✅ |
| 4 | 问 Jev “是否需要修正” | `questions.needs_retry`（`type: noul`） | ✅ noul 返回 0–1 概率，实现按 0.85 阈值判定 |
| 5 | 问 Jev “变更风险” | `questions.risk`（`type: score`，3 档 criteria） | ✅ score 的 criteria 需 2–10 档有序描述，实现满足 |
| 6 | 需要修正时拒绝并让 Gemini 重做一次 | `decision: deny` + `reason` | ✅ CLI 的 `deny` → `AgentExecutionBlocked` → 用 `reason` 作为下一轮提示 |
| 7 | 防止无限重试 | `stop_hook_active` 为真直接放行 | ✅ CLI 重试时确实传入 `true` |
| 8 | Jev 失败不阻塞 CLI | 无 key / 网络错误 / 超时 / 异常响应体 → `decision: allow` | ✅ 四条路径实测 |
| 9 | Jev 只做判定、不生成代码 | 只看 `answers`，不拼接任何生成文本 | ✅ |
| 10 | 安装脚本：Git 仓库安装 | `install.sh` → `gemini extensions install <repo> --consent --skip-settings [--ref]` | ✅ 已重写 |
| 11 | 安装脚本：需要 `TYPESAFE_API_KEY` | 环境变量 / `--api-key-file` / `--api-key` / 隐藏交互输入 | ✅ |
| 12 | 安装脚本：全局或指定项目 | `--global`（user scope）/ `--project PATH`（workspace scope） | ✅ 语义与 0.60.0 的扩展启用模型一致 |
| 13 | 密钥不落仓库、权限收紧 | `~/.config/typesafe/jev.env`，目录 700、文件 600、原子写入 | ✅ |

---

## 2. 已核实的关键契约（为什么这样实现是对的）

1. **扩展只从用户目录加载。** 0.60.0 的 `ExtensionStorage.getUserExtensionsDir()` = `~/.gemini/extensions`，`loadExtensions()` 只扫描该目录；`gemini extensions install` 也没有 `--scope`。
   → 因此“安装到某个项目”只能实现为：扩展仍装在用户目录，但**只在该工作区启用**（`extension-enablement.json` 覆盖规则，后匹配者优先）。脚本的 `disable --scope user` + `enable --scope workspace` 正是官方语义。
2. **扩展 hooks 的约定。** 文件必须是 `<扩展根>/hooks/hooks.json`，顶层为 `{"hooks": {...}}`，命令里的 `${extensionPath}` 会被替换为实际安装路径；`hooksConfig.enabled` 默认为 `true`。仓库中的 `hooks/hooks.json` 与安装后 CLI 报出的 `command`（`python3 "/…/extensions/gemini-quality-gate-jev/scripts/jev_after_agent.py"`）已验证正确。
3. **hook 进程环境会被脱敏。** CLI 用 `sanitizeEnvironment()` 过滤 hook 的 env，规则包含 `/KEY/i`，所以 `TYPESAFE_API_KEY` **在扩展 hook 里读不到**（除非通过扩展 `settings` 注入 `hook.env`，那是官方通道，走系统钥匙串）。
   → 脚本把密钥落到用户文件、hook 再从文件读取，是当前唯一稳定且不依赖交互配置的做法。
4. **Jev 请求/响应格式。** `POST https://api.typesafe.ai/v1/systemone`，`Authorization: Bearer <key>`，body 为 `{model, state, questions}`；`noul` 返回 `answers.<name>.noul`（0–1），`score` 返回 `answers.<name>.score`。
   → 实测：用假 key 请求真实端点返回 **401**（而非 400/422），说明端点、鉴权头与请求体结构都被服务端接受。
5. **项目级配置 `.gemini/settings.json` 与扩展并存。** 项目 hooks 仅在“受信任目录”生效，扩展 hooks 只要扩展处于启用状态就会执行。两者同时存在会让同一个 `AfterAgent` 事件触发两次。

---

## 3. 本轮发现并修复的问题

| 级别 | 问题 | 处理 |
|---|---|---|
| P1 | **密钥路径不一致**：`install.sh` 写 `$XDG_CONFIG_HOME/typesafe/jev.env`，而 hook 只读 `~/.config/typesafe/jev.env`。设置了 `XDG_CONFIG_HOME` 的用户会静默失效。 | 已修复：hook 优先按 `XDG_CONFIG_HOME` 查找，脚本注释同步（`scripts/jev_after_agent.py`） |
| P1 | **超时预算超限**：hook 内部 Jev 8s + 两次 git 各 2s，最坏 12s > `hooks.json` 的 10s，会被 CLI 杀掉（表现为“hook 超时”）。 | 已修复：Jev 默认 5s、git 各 1.5s，最坏约 8s < 10s，且可用 `JEV_HOOK_TIMEOUT_SECONDS` 覆盖 |
| P2 | **异常响应体会抛异常**：`answers` 不是对象时 `float(...)`/`.get` 抛错，虽最终仍因退出码 1 被当作 non-blocking，但会打印 traceback。 | 已修复：解析包 `try/except`，失败直接放行 |
| P1 | **安装脚本会污染信任库 / 本地源会卡住**：`gemini extensions install`（git 源）在 `--consent` 下会把**当前目录**写进 `~/.gemini/trustedFolders.json`；若 `--repo` 是本地目录（CLI 会判定为 local 安装），还会再弹一次“信任该文件夹”的交互询问，脚本会一直等输入。 | 已修复：安装命令带 `GEMINI_CLI_TRUST_WORKSPACE=true`（只作用于这一条命令、等价于脚本已取得用户同意），既不阻塞也不改动信任库 |
| P1 | **项目级启用会失效（符号链接 HOME）**：CLI 用 `process.cwd()` 的物理路径与 `homedir()` 的原始字符串比较，`$HOME` 含软链时“只在某项目启用”退化为“到处启用”。 | 已修复：脚本用 `GEMINI_CLI_HOME` 固定为物理 home。实测：目标项目 `isActive=true`、兄弟项目 `isActive=false` |
| P2 | **重复安装直接报错**、无卸载、无密钥轮换、无前置校验、无 dry-run。 | 已修复：检测已装先卸载再装；新增 `--uninstall` / `--purge-key` / `--api-key-file` / `--dry-run`，并对不可达仓库、缺失 `gemini-extension.json`、不存在的 `--ref` 给出可读错误 |
| P3 | `risk` 分数只打印、不参与任何决策；`needs_retry` 阈值 0.85 硬编码。 | 建议项：把阈值/风险策略外置（环境变量或配置文件） |
| P3 | `$HOME` 之外的项目无法被“只在该项目启用”限制（CLI 的 user scope 就是 home 递归）。 | 脚本已显式告警；如需强制，只能手工维护 `extension-enablement.json` |
| P3 | 扩展 hook 不受目录信任约束（任何目录都会把 prompt/response/diff 摘要发给 `api.typesafe.ai`）。 | 建议项：README 明确数据流向与隐私边界 |
| P3 | 同时保留项目级 `.gemini/settings.json` 与扩展会出现**双重判定**（两次 Jev 调用、两个决策）。 | 建议项：安装扩展后删除项目级配置，或仅把 `.gemini/settings.json` 当本地开发模式 |
| P3 | 无自动化测试。 | 建议项：把本轮 mock 验证固化为 `tests/`（见 §5） |

---

## 4. 交付的安装脚本

`install.sh`（重写）能力：

```bash
# 全局：当前用户所有项目启用
./install.sh --global

# 指定项目：只在该项目启用
./install.sh --project /path/to/project

# 指定仓库/版本
./install.sh --global --repo https://github.com/<owner>/<repo>.git --ref main

# 非交互（CI）
TYPESAFE_API_KEY=... ./install.sh --project "$PWD"
./install.sh --global --api-key-file ~/keys/jev.env

# 预演 / 卸载 / 卸载并删密钥
./install.sh --global --dry-run
./install.sh --uninstall
./install.sh --uninstall --purge-key
```

行为要点：

* 密钥：`--api-key` > `--api-key-file` > 环境变量 > 隐藏交互输入；写入 `${XDG_CONFIG_HOME:-$HOME/.config}/typesafe/jev.env`（目录 700、文件 600、先写临时文件再 `mv`）。
* `--consent` 代表脚本代用户同意 Gemini 的扩展安装提示；`GEMINI_CLI_TRUST_WORKSPACE=true` 只作用于这一条命令，不改动用户信任库。
* 全局：安装后 `enable --scope user`。
* 项目：安装（CLI 默认已用户级启用）→ `disable --scope user` → `enable --scope workspace`，因此只有目标项目生效。
* 支持把本地目录当 `--repo`（此时 `--ref` 不适用——CLI 限制），便于离线/开发安装。
* 安装后用 `gemini extensions list -o json` 复核 `isActive`，失败给出可复制的补救命令。
* 前置校验：`gemini`/`python3`/（git 源时）`git`、仓库可达性、`--ref` 是否存在、本地源是否含 `gemini-extension.json`。

端到端实测（隔离 `HOME`，未污染真实用户目录）：

| 场景 | 结果 |
|---|---|
| `--global`（本地源） | 安装成功，`isActive=true`，兄弟项目同样启用 |
| `--project`（HOME 含 `/tmp` 软链） | 目标项目 `true`、兄弟项目 `false`，覆盖规则 `["!/private/tmp/jev-home/*","/private/tmp/jev-home/work/demo/*"]` |
| `--uninstall` | 扩展目录删除、`extension-enablement.json` 清空、密钥保留 |
| 重复安装 | 自动先卸载再安装，成功 |
| 假仓库 / 假 `--ref` / 非扩展目录 / 本地源带 `--ref` | 均快速失败并给出可读错误 |
| 真实远端（GitHub，推送前） | 克隆与 `--ref main` 检出成功；因远端缺少 `gemini-extension.json` 而失败（当时的阻塞项） |
| 真实远端（GitHub，推送 `b32eadd` 后） | `install.sh --project` 使用**默认远端仓库**安装成功：`installMetadata = {source: https://github.com/ZongxingH/gemini-quality-gate-jev.git, type: git}`，目标项目 `isActive=true`、兄弟项目 `false`，安装后的 hook 从密钥文件取 key 并成功访问 Jev 端点 |
| 安装后的 hook 读密钥 | 从 `$HOME/.config/typesafe/jev.env` 读到密钥并发起真实请求 ✅ |

---

## 5. Hook 行为实测（mock Jev 服务）

| 输入 | 输出 |
|---|---|
| `needs_retry=0.11` | `{"decision":"allow","systemMessage":"JEV quality gate passed (needs_retry=0.11, risk=1.4)"}` |
| `needs_retry=0.92` | `{"decision":"deny","reason":"JEV quality gate asks for one correction pass…","systemMessage":"JEV requested a correction pass (needs_retry=0.92, risk=1.4)."}` |
| `stop_hook_active=true` | `{"decision":"allow","systemMessage":"JEV accepted the retry result without another automatic retry"}` |
| 响应体异常（`answers` 为字符串） | `{"decision":"allow", …}`，stderr 记录原因 |
| 无密钥 | `{"decision":"allow","systemMessage":"JEV skipped: create …/jev.env …"}` |

---

## 6. 下一步（按优先级）

1. ~~提交并推送扩展文件~~ **已完成**：扩展文件已推送到 `origin/main`（`7ed691a..b32eadd`），并已用默认远端仓库跑通 `./install.sh --global` / `--project <path>`。
2. 明确“扩展模式”与“项目级 `.gemini/settings.json` 模式”二选一，避免双重判定。
3. 把阈值（0.85）、风险策略、超时外置为可配置项；把 Jev 响应做一次显式 schema 校验并落日志。
4. 把 §5 的 mock 场景固化为自动化测试（可用 `TYPESAFE_API_URL` 指向本地 mock，无需真实密钥）。
5. 可选：README 增加隐私说明（会把用户请求、模型回答与 `git diff --stat` 发送到 `api.typesafe.ai`）。
