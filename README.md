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
- `JEV_HOOK_TIMEOUT_SECONDS`：JEV 请求超时，默认 8 秒。

当前只有 `needs_retry >= 0.85` 才会拒绝结果并触发 Gemini 自动重试；`stop_hook_active` 为真时会直接放行，防止无限重试。

# gemini-quality-gate-jev
