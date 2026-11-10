# 使用与开发

## 源码运行

准备 Python 3.9 或更新版本。基础服务采用标准库与 SQLite，普通模型 API 路径不需要 Node、Codex 或独立数据库服务。

```sh
git clone https://github.com/yuki233320/yuki-station.git
cd yuki-station/lancer-local
python3 app.py --data-dir ./workspace-data --port 8767
```

浏览器打开 `http://127.0.0.1:8767/studio`。数据存放在你指定的目录中。端口已被占用时，换一个端口及相应浏览器地址。`--open` 会尝试打开默认浏览器。

打开工作台后可按以下次序体验：

1. 新建项目，填写业务说明与长期约定。
2. 在连接模型中核对服务地址、协议、型号和自己的 API Key；目录查询能确认提供商是否列出该型号。
3. 在工作小组中选择协调者和成员。对明确的单模块需求，可以选择单 Worker 路线并填写岗位、范围和标准。
4. 需要真实测试时，在本机登记可信固定脚本，再为项目和岗位授权。
5. 查看任务中的工具回执、逐项质量结果和实际交付版本。

使用者应评估模型服务可能产生的费用，以及源代码、任务和工具反馈会发送到所选择的提供商。

## 目录

```text
yuki-station/
├── README.md                 项目构想、架构、功能及学习入口
├── docs/                     使用指南、状态、路线图和课程
├── lancer-local/             Python 服务、Web 界面和回归测试
├── lancer-codex-bridge/      可选的原生 Codex 桥接源码
├── lancer-mcp-bridge/        可选 MCP 与浏览器桥接源码
└── desktop/macos/            原生窗口、构建脚本与运行时许可证
```

文件夹 `lancer-local` 是历史名称，代表雪站的服务源码。`studio.py` 为项目和会话入口，`task_dispatch.py` 为队列，`trigger_*` 处理事件和订阅，`worker_*` 执行授权工具，`host_quality.py` 与 `quality_workflow.py` 负责质量与交付。

## 测试

```sh
cd lancer-local
python3 -B -m unittest discover -p 'test_*.py' -q
node --test test_studio_business_ui.js test_studio_visual.js test_flow.js test_studio_replay.js
```

Python 回归中部分测试启动本机模拟 HTTP 服务。MCP 集成测试依赖 `lancer-mcp-bridge/node_modules`；未安装可选依赖时，对应测试可能跳过，不能称为全部测试通过。不同平台、Python 和 Node 版本可能暴露兼容性差异，见[状态与验证](STATUS.md)。

## 本机数据与恢复

指定数据目录包含私有项目和会话，不应加入 Git。数据已经迁移到 SQLite 后，不要通过删除数据库或把旧 JSON 重新灌入来“恢复”。迁移前应停止服务，使用项目备份工具并检查恢复结果。备份会包含敏感会话和资料，应妥善保管。

```sh
python3 project_backup.py backup --data-dir ./workspace-data --project 你的项目编号 --output ./project-backup.zip
python3 project_backup.py restore --input ./project-backup.zip --data-dir ./restored-data
```

## macOS 桌面源码

`desktop/macos` 包含 AppKit/WKWebView 窗口、图标、固定版本运行时来源和许可证。构建脚本要求本机具备 macOS Apple Silicon 和 Xcode 命令行工具。它不会下载运行时；请根据 `runtime-source.json` 从对应上游发行页获取指定归档，并核对其 SHA-256，再放入 `desktop/macos/`。构建脚本使用白名单打包源码和静态资源，不应将数据目录或密钥放入分发包。

从仓库根目录执行以下命令生成当前源码快照的构建冻结记录，并在临时目录构建：

```sh
cd desktop/macos
python3 -c "import json; from pathlib import Path; import build_release; src=Path('../../lancer-local').resolve(); Path('source-freeze.json').write_text(json.dumps(build_release.freeze_candidate(src, '本机构建'), ensure_ascii=False, indent=2)+'\\n')"
python3 build_release.py --source ../../lancer-local --output ../../build-output
```

运行时归档、构建输出和本机冻结文件均由 `.gitignore` 排除。构建结果使用 ad-hoc 签名；发布签名与公证需要维护者另行配置和验收。

## 公开快照的处理

代码来自 0.2.8 正式版本的源码白名单。为公开发布，两个测试中的私人仓库名被替换为示例仓库，静态架构审查模板改为通用标题，并重写了项目文档。生产业务执行逻辑、触发与质量关卡的主体来自正式快照。由于报告模板文字经过处理，公开源码的运行指纹不应与个人机器上的正式桌面运行指纹直接等同。
