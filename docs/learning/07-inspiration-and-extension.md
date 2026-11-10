# 07 · 开源融合与新增来源

## 阅读上游时先固定版本

Lancer 可用于学习工作区和执行边界，Super-agent 可用于学习事件、任务和执行生命周期。先记录提交版本，再阅读 README、架构文档与源码，区分上游实际实现和设计设想。

雪站的参考版本与差异见[开源项目对比](../COMPARISON.md)。上游各自的许可证、运行时和适用范围应独立核对。

## 设计一个新的 Source

假设团队准备接入聊天系统：

1. 定义来源配置与身份来源，例如固定工作空间、频道、发送人和机器人身份。
2. 将消息与回复映射为稳定事件 ID 和 `threadKey`，保证重复收取不会生成重复工作。
3. 设计本地规则，过滤无关事件并合批；确定何时调用模型研判。
4. 定义授权的项目、模型、路径和外部动作，正文不授予权限。
5. 测试同串反馈进入原 Worker，另一话题仍作为独立事件；测试丢失回执、重启和权限撤销。
6. 先用本机模拟服务验证，再在有授权的独立来源上测试真实行为。

当前项目提供 GitHub、自定义本机事件和定时来源。群聊、邮箱及日历的真实账号适配还需要具体的协议、认证、权限与测试。

## 扩展阅读

- [Super-agent 添加 Source 的示例](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/docs/008-cookbook.md)
- [`trigger_policy.py`](../../lancer-local/trigger_policy.py) 与 [`trigger_engine.py`](../../lancer-local/trigger_engine.py)
- [`github_source.py`](../../lancer-local/github_source.py) 与 [`test_github_subscriptions.py`](../../lancer-local/test_github_subscriptions.py)
