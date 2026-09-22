# gemini-quality-gate-jev

给 Gemini CLI 装一个「质量门」：每轮回答结束后，用 [TypeSafe Jev](https://typesafe.ai/) 判断这次交付是否还缺关键步骤，缺就让 Gemini 自动再改一轮。

Jev 在这里只回答两个结构化判断（要不要返工、风险多高），不生成任何代码或文字。

## 它做什么

Gemini 完成一轮回答时（`AfterAgent` 事件），扩展把三样东西交给 Jev：

| 交给 Jev 的内容 | 说明 |
| --- | --- |
| 用户请求 | 这一轮要 Gemini 做什么 |
| 最终回答 | Gemini 这一轮的交付说明 |
| 工作区摘要 | `git status --short` 与 `git diff --stat` |

Jev 返回两个判断：

- **`needs_retry`**：这轮回答有没有必须补上的遗漏（0–1 的概率）
- **`risk`**：这次变更的风险等级（分数）

当 `needs_retry >= 0.85` 时，扩展会**打回这轮结果**，让 Gemini 带着「复查需求、跑验证、给出证据」的要求自动再答一轮；只补一轮，不会无限重试。其余情况直接放行。

**异常一定放行**：没配 API key、网络不通、Jev 超时或返回异常，扩展都会放行，不会把 Gemini CLI 卡住。

## 前置条件

- Gemini CLI **0.60 或更高**
- **Python 3**（hook 用系统 `python3` 执行）
- 一个 **TypeSafe Jev API key**，在 <https://console.typesafe.ai/settings/keys> 创建
- git（可选：用于采集工作区摘要，不在 git 仓库时会自动跳过）

## 安装

一条命令（当前用户的所有项目都启用）：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/ZongxingH/gemini-quality-gate-jev/main/install.sh) --global
```

只给某个项目启用：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/ZongxingH/gemini-quality-gate-jev/main/install.sh) --project /path/to/your/project
```

> 扩展文件装在用户目录（Gemini CLI 只从 `~/.gemini/extensions` 加载扩展），但只在这个项目里启用，其他项目不受影响。

克隆后本地运行、锁定版本，或在 CI 里非交互执行：

```bash
git clone https://github.com/ZongxingH/gemini-quality-gate-jev.git
cd gemini-quality-gate-jev

./install.sh --global
./install.sh --global --repo https://github.com/ZongxingH/gemini-quality-gate-jev.git --ref main
TYPESAFE_API_KEY=... ./install.sh --project "$PWD"
./install.sh --global --api-key-file ~/keys/jev.env
```

安装脚本会：

1. 读取 `TYPESAFE_API_KEY`（优先级：`--api-key` > `--api-key-file` > 环境变量 > 隐藏交互输入），写入 `${XDG_CONFIG_HOME:-~/.config}/typesafe/jev.env`（目录 700、文件 600，不会进仓库）；
2. 从 Git 仓库安装扩展；
3. 按 `--global` / `--project` 设置启用范围；
4. 用 `gemini extensions list -o json` 复核扩展是否真的启用。

安装完**重启 Gemini CLI**，然后验证：

```bash
gemini extensions list        # 应看到 gemini-quality-gate-jev，且为 enabled
```

## 使用

重启后不需要额外操作，正常使用 `gemini` 即可：

- **判定通过**：直接通过，不影响回答；
- **判定需要修正**：你会看到 `JEV requested a correction pass …`，Gemini 会自动再答一轮，并在这一轮里复查需求、运行验证、给出证据；
- **Jev 不可用或没配 key**：正常回答，只是没有质量门（会在提示里说明原因）。

想看它是否在工作，可以在任意目录跑一条模拟事件：

```bash
printf '%s' '{"hook_event_name":"AfterAgent","cwd":".","prompt":"修复登录接口","prompt_response":"已修改代码，但没有运行测试","stop_hook_active":false}' \
  | python3 ~/.gemini/extensions/gemini-quality-gate-jev/scripts/jev_after_agent.py
```

没配 key 时会输出 `decision: allow` 并提示 Jev 被跳过；配了 key 就会真实访问 Jev 并给出判定。

## 配置

用环境变量调整（写在 shell profile 或 Gemini 的运行环境里）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TYPESAFE_API_KEY` | 无 | API key；未设置时读取 `~/.config/typesafe/jev.env` |
| `TYPESAFE_API_URL` | `https://api.typesafe.ai/v1/systemone` | Jev API 地址 |
| `TYPESAFE_MODEL` | `jev-latest` | 使用的 Jev 模型 |
| `JEV_HOOK_TIMEOUT_SECONDS` | `5` | 单次 Jev 请求的超时（秒） |

判定策略目前是固定的：`needs_retry >= 0.85` 才打回重做，重做后的那一轮不再二次打回。

## 更新与卸载

```bash
./install.sh --global                  # 重新运行即更新到仓库最新版本
./install.sh --uninstall               # 卸载扩展，保留密钥文件
./install.sh --uninstall --purge-key   # 卸载扩展并删除密钥文件
./install.sh --global --dry-run        # 只打印将要执行的命令
./install.sh --help                    # 全部选项
```

## 手动配置密钥

不使用安装脚本时，可以自己写入密钥文件：

```bash
mkdir -p ~/.config/typesafe
read -r -s JEV_KEY
printf 'TYPESAFE_API_KEY=%s\n' "$JEV_KEY" > ~/.config/typesafe/jev.env
unset JEV_KEY
chmod 600 ~/.config/typesafe/jev.env
```

## 数据与隐私

启用后，每一轮回答结束都会把**用户请求、Gemini 的最终回答、git 工作区摘要**发送到 `api.typesafe.ai`。在敏感仓库中使用前，请先确认这符合你的合规要求。
