# SRD 规则语料库

本 Skill 包含三套 D&D 规则集：

- `references/` — D&D 5e 2024 SRD 5.2.1（CC-BY-4.0，20 个 Markdown 源文件）
- `references-2014-en/` — D&D 5e 2014 SRD 5.1（英文）
- `references-2014-zh/` — D&D 5e 2014 SRD 5.1（中文，含规则术语与状态）

## 首次启用（零配置）

首次调用 `dnd_rules` 工具时，数据库迁移完成后自动摄入 SRD 规则，无需手动操作。

搜索默认为**词法 + 全文检索**，不依赖任何外部服务。

## 检索架构

```
首次启用
└─ database.upgrade_schema()      ← 创建表结构
└─ ensure_bundled_rules_ingested() ← 自动摄入 SRD（仅文本，无向量）

搜索时（默认，零依赖）
├─ 精确匹配 (exact)                ← 关键词完全匹配，权重最高
├─ 词法检索 (lexical)             ← 分词 + 二元字格匹配
└─ 密集向量 (dense) — 可选         ← 需要 ChromaDB + 配置的 BGE profile

Dense 向量搜索（可选，需配置）
└─ set CHROMA_DB_DISABLED=0       ← 启用 ChromaDB（默认路径 <skill>/data/chroma_db）
└─ set DND_DENSE_DISABLED=0       ← 启用配置的 BGE profile
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `CHROMA_DB_DISABLED` | 禁用 (默认不设) | 设为 `0` 强制启用（使用 `<skill>/data/chroma_db/`） |
| `CHROMA_DB_URL` | - | 远程 ChromaDB 服务地址（设置后自动启用） |
| `CHROMA_DB_PATH` | - | 自定义 ChromaDB 路径（设置后自动启用） |
| `DND_DENSE_DISABLED` | 禁用 (默认 `=1`) | 设为 `0` 启用 Dense 检索 |
| `DND_EMBEDDING_MODE` | `auto` | 设备模式：`auto`、`cpu` 或 `gpu`；不决定模型 |
| `DND_EMBEDDING_PROFILES` | `bge_m3` | 可选 `bge_m3`、`bge_small_zh_v1_5`、`bge_small_en_v1_5`，逗号分隔 |
| `DND_EMBEDDING_BATCH_SIZE` | `8` | 编码批次大小（1–128） |
| `DND_DATABASE_URL` | `<skill>/data/dnd.db` | SQLite 数据库路径（覆盖默认） |

## CLI 命令

```powershell
# 摄入规则（首次自动执行，也可手动触发）
python -m saga_domain.cli rules ingest-srd
python -m saga_domain.cli rules ingest-srd --no-embed  # 仅文本，禁用向量

# 查看规则层级
python -m saga_domain.cli rules tree

# 查看索引状态
python -m saga_domain.cli rules status

# 绑定战役规则集
python -m saga_domain.cli rules bind --campaign <id> --rule-set dnd5e-2024-srd-5.2.1

# 搜索规则
python -m saga_domain.cli rules search --query "how does grapple work" --campaign <id>
python -m saga_domain.cli rules search --query "施法" --campaign <id> --no-dense  # 纯词法
```

## 无独显机器推荐配置

```bash
# 零依赖，纯词法搜索（首次启用后立即可用）
# 无需配置任何环境变量

# 无 GPU：显式选择中英文 Small profile
set CHROMA_DB_DISABLED=0
set DND_DENSE_DISABLED=0
set DND_EMBEDDING_MODE=cpu
set DND_EMBEDDING_PROFILES=bge_small_zh_v1_5,bge_small_en_v1_5

# 有 GPU + ChromaDB：完整密集向量体验
set CHROMA_DB_DISABLED=0
set DND_DENSE_DISABLED=0
set DND_EMBEDDING_PROFILES=bge_m3
```

模型选择是显式且可选的，不由 CPU/GPU 自动强制切换。中英文 Small 分别为 512/384
维；BGE-M3 为 1024 维。每个 profile 使用独立 collection。修改 profile 后运行
`python -m saga_domain.cli vector reindex` 重建 Dense 索引。

## 2024 SRD 来源文件映射

- `001-018`：基础玩法、动作、探索与战斗
- `019-086`：职业、起源与专长
- `087-103`：装备、武器、护甲与工具
- `104-175`：施法规则与法术
- `176-191`：规则术语和状态
- `192-252`：玩法工具箱与魔法物品
- `253-364`：怪物与动物
