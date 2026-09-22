# Gemini CLI + Jev AfterAgent Hook

这是一个最小的 Gemini CLI Hook：Gemini 完成一轮工作后，Hook 把用户请求、最终回答和 Git 工作区摘要交给 Jev 做两个结构化判断：是否需要再修正一次，以及变更风险等级。

Jev 不生成代码；它只负责质量门决策。Jev 请求失败时 Hook 会放行，避免网络或服务问题阻塞 Gemini CLI。

## 启用

1. 在 TypeSafe Console 创建 API key。
2. 把 key 保存到用户目录（不要放进仓库）。Gemini CLI 的 Hook 运行在精简环境中，使用下面的隐藏输入方式创建权限为 600 的文件：

   ```bash
   mkdir -p ~/.config/typesafe
   read -r -s JEV_KEY
   printf 'TYPESAFE_API_KEY=%s\n' "$JEV_KEY" > ~/.config/typesafe/jev.env
   unset JEV_KEY
   chmod 600 ~/.config/typesafe/jev.env
   ```

   设置了 `XDG_CONFIG_HOME` 时，hook 会优先读取 `$XDG_CONFIG_HOME/typesafe/jev.env`。
   也可以直接运行 [`install.sh`](install.sh)，它会替你完成这一步。

3. 从本目录启动 Gemini CLI：

   ```bash
   gemini
   ```

项目级配置位于 `.gemini/settings.json`，Hook 实现位于 `scripts/jev_after_agent.py`。配置使用 Gemini CLI 提供的 `GEMINI_PROJECT_DIR`，因此从项目根目录或子目录启动都可以。

## 手动测试 Hook

不启动 Gemini，也可以用一条模拟事件验证 Hook 的 JSON 输入/输出协议：

```bash
printf '%s' '{"hook_event_name":"AfterAgent","cwd":".","prompt":"修复登录接口","prompt_response":"已修改代码，但没有运行测试","stop_hook_active":false}' \
  | python3 scripts/jev_after_agent.py
```

未设置 API key 文件时应当看到 `decision: allow`，并提示 JEV 被跳过。手动测试时也可以临时使用 `TYPESAFE_API_KEY=... python3 scripts/jev_after_agent.py`。

## 调整策略

环境变量：

- `TYPESAFE_API_URL`：覆盖 JEV API 地址，默认 `https://api.typesafe.ai/v1/systemone`。
- `TYPESAFE_MODEL`：默认 `jev-latest`。
- `JEV_HOOK_TIMEOUT_SECONDS`：JEV 请求超时，默认 5 秒（hooks.json 的命令超时是 10 秒，两次 git 摘要各 1.5 秒，留有余量）。

当前只有 `needs_retry >= 0.85` 才会拒绝结果并触发 Gemini 自动重试；`stop_hook_active` 为真时会直接放行，防止无限重试。

# gemini-quality-gate-jev

## 从 Git 仓库安装

> ⚠️ 先推送扩展文件：远程仓库的 `main` 目前只有最初提交，还不包含 `gemini-extension.json` / `hooks/` / `install.sh`。
> 未推送前，从 Git 安装会以 `Configuration file not found … gemini-extension.json` 失败。

```bash
git add gemini-extension.json hooks install.sh scripts README.md
git commit -m "Package as a Gemini CLI extension"
git push origin main
```

仓库提供了 [`install.sh`](install.sh) 安装脚本：隐藏读取 `TYPESAFE_API_KEY`，保存到用户目录的 `${XDG_CONFIG_HOME:-~/.config}/typesafe/jev.env`（目录 700、文件 600），不会写入仓库或 Gemini 配置文件。

全局安装（当前用户的所有项目）：

```bash
git clone https://github.com/ZongxingH/gemini-quality-gate-jev.git
cd gemini-quality-gate-jev
./install.sh --global
```

只安装到指定项目（扩展装在用户目录，但只在该项目启用）：

```bash
./install.sh --project /path/to/your/project
```

指定其他仓库或版本，或在 CI 中非交互执行：

```bash
./install.sh --global --repo https://github.com/ZongxingH/gemini-quality-gate-jev.git --ref main
TYPESAFE_API_KEY=... ./install.sh --project "$PWD"
./install.sh --global --api-key-file ~/keys/jev.env
```

其他选项：

```bash
./install.sh --global --dry-run     # 只打印将执行的命令
./install.sh --uninstall            # 卸载扩展（保留密钥）
./install.sh --uninstall --purge-key
./install.sh --help
```

要点：

- 密钥来源优先级：`--api-key` > `--api-key-file` > 环境变量 `TYPESAFE_API_KEY` > 隐藏交互输入。
- 脚本会代你同意 Gemini CLI 的扩展安装提示（`--consent`），并仅为这一条命令设置 `GEMINI_CLI_TRUST_WORKSPACE=true`，不会改动你的 `~/.gemini/trustedFolders.json`。
- 项目安装会先关闭用户范围启用，再只为目标项目启用；`$HOME` 之外的项目无法被 CLI 限制，脚本会给出警告。
- 安装后可能提示 “TypeSafe API key” 扩展设置缺失，这是预期行为：hook 直接读取上面的密钥文件。
- 若同时保留本仓库 `.gemini/settings.json` 的项目级 hook，同一轮会触发两次判定；安装扩展后请二选一。
- 完整能力与验证记录见 [`ANALYSIS.md`](ANALYSIS.md)。

安装完成后需要重启 Gemini CLI。

