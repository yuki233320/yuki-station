# 03 · 事件与订阅

## 从事件到任务

**Source** 是事件来源，**Trigger** 是一个可去重的事件，**Thread** 是来源定义的一串相关事件。来源先检查身份、容器与类型，再把候选按线程合批。配置了研判时，模型判断是否接单；授权与可执行范围由保存的来源策略和任务合同给出。

雪站有三种来源：GitHub 轮询、本机认证自定义事件、带时区定时事件。观察模式用于收集与筛选，研判模式可请求模型判断，自动模式在明确配置后可以创建任务。

## 为什么需要订阅

假设一个 Worker 修复故障并提交了 PR。后续对同一 PR 的评论、review 和 CI 属于同一串工作。已启用订阅时，这些事件进入原任务的持久队列，交给同一 Worker 继续，不需要每条再次调用事件研判模型。

雪站当前用精确的来源、版本、容器和 `threadKey` 表示归属。同一个发送人出现在另一话题里，并不能证明两条消息是同一项工作。操作者可以对已有事件串进行明确认领、暂停或释放。

## 已读与未读

收到、附加、实际发送给父 Worker、完成任务及最终确认是不同状态。系统按 FIFO 交付完整消息，核验实际父请求和质量原件后确认处理；在最后一次请求之后到达的反馈留待下一轮。单次取件最多 8 条、总 12000 字符，超长原文保留并提示处理。

## GitHub 的线程身份

PR 评论与 review 关联 PR 编号。CI 必须能明确证明仓库、PR 和 head SHA 的对应关系；不能仅因 branch 名称或作者相同就路由。后续交付检查当前远端 PR 和 head，再非强制更新同一分支。

阅读 [`trigger_policy.py`](../../lancer-local/trigger_policy.py)、[`trigger_store.py`](../../lancer-local/trigger_store.py)、[`trigger_continuation.py`](../../lancer-local/trigger_continuation.py) 及 [`test_github_subscriptions.py`](../../lancer-local/test_github_subscriptions.py)。

延伸阅读：[Super-agent 领域模型](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/docs/001-domain.md)。
