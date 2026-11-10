雪站 0.2.8（12）独立桌面版（macOS Apple Silicon）

本包包含工作台源码、界面、CPython 3.12.16 和原生窗口。
普通API模型与GitHub观察不需要另外安装Python、Node或Codex。

新用户：
1. 解压，将“Yuki雪站.app”复制到“应用程序”，再打开。
2. 在“连接模型”填写自己的服务地址、精确型号和API Key。
3. 新建项目。GitHub来源需用户自己的令牌和仓库；默认只观察。
4. 每位用户的数据位于 ~/Library/Application Support/YukiStation/data。
   日志位于同一YukiStation文件夹的service.log。
5. 本包不会携带作者的项目、会话、模型密钥、GitHub登录或工具登记。

此构建使用本机ad-hoc签名，尚无Apple Developer ID签名和公证。
已在构建机做隔离环境和移动路径验收；未冒充另一台物理Mac的验收。
对外正式下载发行前仍需发布者完成Developer ID签名、公证与另一台Mac试装。
本包只适用于Apple Silicon，macOS 12及以上；Intel/Windows/Linux桌面版未发布。

升级既有安装：
不能把旧会话JSON重新灌回已迁移的SQLite。现有数据应完整保留。
维护者可在 ~/Library/Application Support/YukiStation/desktop-local.json 中保存：
{"schema":"yuki-desktop-local/v1","dataDirectory":"/绝对路径/现有数据目录","port":8767}
仅指定数据目录，不依赖旧源码路径或旧Python。若该目录不存在，应用拒绝创建替代空库。
兼容配置属于本机，不能放入要发给其他人的安装包。
本机端口已有其他数据或版本的服务时，应用拒绝复用，不会停止未知进程。
升级前先完成/停止工作并由维护者关闭旧后台服务；应用不自动重放未完成任务。

关闭桌面窗口后后台任务继续运行；重启电脑会停止服务。
模型与GitHub凭据当前只在服务内存，服务重启后须重新连接；来源需显式恢复。
源码内固定验收脚本不是操作系统沙箱。MCP、浏览器和原生Codex属于可选功能，
需要用户单独安装Node/运行时并登记工具与可信检查，不随基础包自动启用。

运行时来自 https://github.com/astral-sh/python-build-standalone/releases/tag/20261026
完整许可证及上游元数据保存在应用Contents/Resources/runtime-licenses；
运行时原有许可证也随runtime目录保留。雪站与图标许可证见server目录。

维护者构建：
默认执行 build_release.py --output <新目录>，应用在隐藏临时目录内构建，
完成或失败时清理临时.app；最终仅保留分发ZIP、release-manifest.json和SHA256SUMS。
不要在多个构建目录长期保留.app，以免macOS应用列表出现重复雪站图标。
仅安装或调试时显式使用 --keep-app；完成验证/安装后将副本归档为ZIP。
旧客户端备份也使用ZIP，正式安装位置只保留一个雪站.app。
默认构建后做验证时，在临时目录解压ZIP，再对解压的.app运行verify_desktop.py，
验证完成清理临时目录；不要将验证副本留在工作区或桌面。

本版支持显式订阅同一事件串。通过规则的后续消息可交给原任务的 Worker 继续处理，支持收件确认、暂停和释放。GitHub 订阅可以跟进已发布 PR 的反馈，并在质量检查通过后更新该 PR。
