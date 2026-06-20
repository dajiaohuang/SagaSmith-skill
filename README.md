# SagaSmith-skill

将 SagaSmith DM Agent 的所有自定义内容打包为独立 skill 插件，安装到任意 NanoBot / OpenClaw / Hermes 实例。

## 项目定位

- **SagaSmith-agent**：完整 AI DM 运行时（主仓库）
- **SagaSmith-skill**：跨平台 skill 插件包（本仓库）

Saga = 史诗战役（强调战役管理），Smith = 工匠（强调自主创作与内容生成）。

## 目录结构

```
SagaSmith-skill/
├── skills/                          # 3 个 Skill 目录（纯 Markdown，跨平台）
│   ├── dnd-dm/                      # 核心 DM 人格，always: true
│   │   ├── SKILL.md
│   │   ├── references/              # 裁判规则、模板、角色创建、模组导航
│   │   └── srd/                     # SRD 5.2.1 CC-BY-4.0（20 文件）
│   ├── dnd-campaign-manager/        # 战役生命周期管理
│   └── dnd-module-gen/              # 模组生成（5 种类型 × 25 范式）
├── templates/                       # SOUL 模板（纯 Markdown，跨平台）
│   ├── SOUL.md                      # 明萨拉·班瑞 DM 人格
│   ├── IDENTITY.md                  # 身份约束
│   ├── AGENTS.md                    # 会话启动协议
│   ├── agent/identity.md            # 运行时注入 identity
│   └── memory/MEMORY.md             # 长期记忆模板
├── tools/                           # Agent 工具（NanoBot 参考实现）
│   ├── dnd_campaign.py              # 战役 CRUD + 一键开团
│   ├── dnd_save.py                  # 存档管理
│   ├── dnd_module.py                # 模组管理
│   └── dnd_rules.py                 # 规则检索
├── domain/                          # 业务逻辑（纯 Python，无框架依赖）
│   ├── db/                          # 数据库层（ORM + Service + CLI）
│   ├── modules/                     # 模组处理（分块、PDF解析、检索）
│   ├── rules/                       # 规则引擎（BGE-M3 嵌入、解析、检索）
│   └── engine/                      # 机制计算（骰子、检定、战斗、XP）
├── data/                            # 首次安装数据
│   ├── srd/                         # SRD 5.2.1 英文（20 文件）
│   └── srd-zh/                      # SRD 中文翻译（可选子模块）
└── README.md
```

## 各平台安装

### NanoBot

```bash
cp -r skills/*     ~/.nanobot/skills/
cp -r templates/*  ~/.nanobot/templates/
cp -r tools/*.py   ~/.nanobot/agent/tools/
cp -r domain/*     ~/.nanobot/dnd/
cp -r data/srd     ~/.nanobot/dnd/data/srd/

# 首次导入 SRD
python -m nanobot.dnd.db.cli rules ingest-srd
```

### OpenClaw

`skills/` + `templates/` + `data/` 直接复制。`tools/` 通过 TypeScript `api.registerTool()` 包装后再调 Python domain 层（subprocess JSON 桥）。

### Hermes

`skills/` 通过 `ctx.register_skill()`；`templates/` 通过 `ctx.inject_message()`；`tools/` 通过 `ctx.register_tool()` 包装 domain 层。

## 外部依赖

- Python 3.11+
- SQLAlchemy
- FlagEmbedding (BGE-M3)
- markitdown (PDF/DOCX 导入)

## 许可证

SRD 5.2.1 文件采用 CC-BY-4.0 许可。其余代码采用 MIT 许可。

## 致谢

- [ackiles/dnd-dm-skill](https://github.com/ackiles/dnd-dm-skill) — 灵感与参考
