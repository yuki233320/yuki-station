# 雪站与两个上游项目

## 比较范围

本页记录 2026-11-02 对源码的设计阅读，以固定提交为参照。版本后的功能演进应重新核对，不把上游项目的全部能力等同于当时源码。雪站通过自己的代码渐进吸收思想，保留本机项目、岗位、工具与质量工作流。

| 维度 | Lancer | DeepSeek Super-agent 原型 | 雪站 0.2.8 |
| --- | --- | --- | --- |
| 主要入口 | 以工作区、会话和执行为中心 | 事件进入评估，形成工作项，再分配执行器 | 用户项目任务与可选事件来源同时存在 |
| 角色安排 | 关注工作区和执行边界 | Router 评估工作，Executor 处理任务 | 规则 Router 支持明确的单 Worker，也可使用 Coordinator 与岗位小组 |
| 持续跟进 | 值得参考会话、文件和任务边界 | Thread、Subscription、Input 将跟进送回 Ticket | 来源显式开启订阅，已认领的同串事件延续原 Worker 会话 |
| 可验证交付 | 提供执行相关的工作区机制 | 工作项、授权、外部副作用与恢复都有领域语义 | 冻结合同、真实工具回执、宿主检查、独立复核和版本交付共同判断 |
| 部署对象 | 上游自己的产品与运行时 | 单仓库主动编码研究原型 | 本机工作台与 Apple Silicon 独立桌面测试版 |

此表描述不同关注点，并不证明任何一方在质量、速度或成本上优于另一方。上游源码明确把 Super-agent 描述为研究原型，其公开实现不等同内部系统或论文实现。

## Lancer 带来的启发

- 对**工作区、执行器与状态**做清晰的职责划分，减少界面直接承担执行控制的耦合。
- 让文件、会话和执行结果有可追溯的所有权，并让不同执行方式通过明确接口接入。
- 结合雪站自己的岗位、回放与质量要求，保留结果证据和历史语义。

## Super-agent 带来的启发

- 将 Source、Trigger、Thread、Eval、Ticket、Assignment 和 Subscription 区分为不同事实。
- 外部输入先做确定性筛选，再按授权研判。事件正文不能自行扩大任务权限。
- 后续反馈属于稳定事件串，可继续交给已有任务；记录消息进入 Worker 和完成处理的状态。
- 外部操作先持久记录意图，完成或失败后保存事实。未知结果需要核对，不能只凭重试就声称恰好执行一次。

雪站当前实现选择一个事件串对应一个 Worker 归属，允许同一任务认领多个明确事件串，并保留原项目、岗位、型号和冻结范围。单条消息过长、来源授权变化或运行状态未知时需要处理。

## 可复核的原始材料

- [Lancer 源码与 README，019b2f1](https://github.com/raylee-ai/lancer/tree/019b2f16f746df4160f2fa9937a4e6c50c13c6f4)
- [Lancer 架构，019b2f1](https://github.com/raylee-ai/lancer/blob/019b2f16f746df4160f2fa9937a4e6c50c13c6f4/docs/architecture.md)
- [Super-agent 原型说明，6337fc8](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/README.md)
- [领域模型与订阅，6337fc8](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/docs/001-domain.md)
- [执行、输入与恢复，6337fc8](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/docs/003-execution.md)
- [添加 Source 的示例，6337fc8](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/docs/008-cookbook.md)
