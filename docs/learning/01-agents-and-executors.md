# 01 · Agent、角色与执行器

## 学习目标

理解一次任务中谁判断、谁执行、谁验收，以及模型、岗位和执行器之间的区别。

## 基本概念

**Agent** 可以理解为承担特定职责的一组模型调用、上下文和工具规则。**模型**负责生成与推理，**岗位**定义工作职责与需要的能力，**执行器**把一次具体请求交给 API、原生桥接或其他运行环境。

在雪站中，Coordinator 可以理解任务并协调模块。Router 根据已保存的任务合同和规则选择工作路线。Worker 接收具体岗位范围，在自己的上下文中处理文件和反馈。质量审核者依据固定标准和可引用证据审查候选。它们即使使用同一厂商，也不能把“多个对话”直接视为独立审核。

## 两种工作形态

- **独立任务**：用户提供一个明确岗位、范围和标准。规则 Router 可直接选择一个 Worker，不必先调用规划模型。
- **复杂任务**：Coordinator 结合项目背景拆成模块并设置依赖。各岗位完成自己的部分，随后汇总和验收。

复杂任务的内部协作与大量独立任务的排队是两个层次。两者最终都要走固定合同和质量关卡。

## 子任务

在授权的 `workspace` 岗位，父 Worker 可以使用同一冻结型号派生执行子任务。子任务有独立副本和上下文，在限定路径修改及运行指定检查。父级检查候选、接纳后重新测试。子任务不是独立审核者，父级仍负责最终交付。

## 从代码入手

1. 阅读 [`task_routing.py`](../../lancer-local/task_routing.py) 的直接任务合同。
2. 阅读 [`execution_adapters.py`](../../lancer-local/execution_adapters.py) 如何选择执行器。
3. 阅读 [`worker_execution.py`](../../lancer-local/worker_execution.py) 和 [`worker_subagents.py`](../../lancer-local/worker_subagents.py)。
4. 查看 [`test_worker_child_adoption.py`](../../lancer-local/test_worker_child_adoption.py)，解释为什么子任务测试不能替代父级重验。

延伸阅读：[Super-agent 执行器说明](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/docs/003-execution.md)。
