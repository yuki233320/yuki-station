# 第三方来源与许可证

雪站的项目代码以仓库根目录的 [MIT 许可证](../LICENSE)发布。下列项目和素材拥有各自的作者、许可证与更新节奏，请使用时单独核对。

- [Lancer，固定比较提交 019b2f1](https://github.com/raylee-ai/lancer/tree/019b2f16f746df4160f2fa9937a4e6c50c13c6f4)。雪站参考其架构与工作区思路，未将 Lancer 整仓作为底座。
- [DeepSeek Super-agent 原型，固定比较提交 6337fc8](https://github.com/deepseek-ai/dsh-superagent-prototype/tree/6337fc823ce20251c8de8085948165c81aaa701e)。请阅读其 README 对研究原型及公开实现范围的说明。
- 模型图标的来源和许可见 [public/icons/SOURCES.txt](../lancer-local/public/icons/SOURCES.txt) 与 [public/icons/LICENSE](../lancer-local/public/icons/LICENSE)。
- 原生 Codex 与 MCP 桥接是可选组件，见各目录的 `LICENSE`、`package.json` 和 README。Node 依赖不随源码仓库提交。
- macOS 桌面测试构建使用 [python-build-standalone](https://github.com/astral-sh/python-build-standalone) 的固定运行时。来源及 SHA-256 见 [runtime-source.json](../desktop/macos/runtime-source.json)，对应许可证位于 [runtime-licenses](../desktop/macos/runtime-licenses)。运行时二进制不存放在本源码树。
