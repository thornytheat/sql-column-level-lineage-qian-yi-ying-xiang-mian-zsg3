# lineagex · SQL 列级血缘与迁移影响面

数仓建模组改源表字段前，先回答两个问题：**会波及哪些下游视图/任务**、**按什么顺序改才不出事**。

## 能力

- 轻量 SQL 解析（标准库词法 + 递归下降，不引 sqlparse）：CTAS / INSERT SELECT / VIEW、AS 别名、JOIN ON、单层与嵌套子查询、UNION ALL、`SELECT *` 与 `t.*` 展开；解析失败给「第几行第几列 + 原因」。
- 列级血缘：目标列 ← 源列，经别名与子查询向外传播；函数包裹（CAST/COALESCE/ROUND/SUM/AVG/COUNT/TRIM/UPPER）保留源列身份并记录变换链；`*` 展开的边打 `star_expanded` 标记；UNION ALL 分支按列序号对齐。
- 环检测：表级与列级依赖图 DFS 三色，输出环路径与产生环的脚本行。
- 影响面：输入表或列，按拓扑层级从浅到深列出全部下游对象，标注直接 / 间接与受影响列。
- 迁移清单：删列 / 改名 → 生成有序步骤（冻结 → 逐层改下游 → 回填物理表 → 最后 DDL），附导出的 JSON / CSV。

## 核心规则

| 规则 | 说明 | 异常态 |
|---|---|---|
| 解析 | 单脚本单语句；支持 CTAS / INSERT SELECT / VIEW / 子查询 / JOIN / UNION ALL / `*` | 不支持 WITH、多语句、窗口函数；错误带行列位置 |
| 血缘 | 目标列 = 别名/列名/函数_列名；子查询透传源表；UNION 各分支并入同一目标列 | 未知来源表、列不属于任何来源、JOIN 列歧义 → 诊断并跳过该脚本的边（不静默） |
| 环 | DFS 三色，输出规范化环路径 | 环内节点拓扑层级为「未知」，不冒充 0 |
| 影响面 | BFS 求距离 + 拓扑层级排序；距离 1 = 直接 | 叶子列返回「没有任何下游边」 |
| 迁移 | 按层级分步，物理表回填、视图重建，DDL 放最后 | 环未处理、改名缺新列名 → 警告 |

## 启动

```bash
cd batch-20261114b/projects/gb-625
docker compose up -d --build
curl -s http://127.0.0.1:18214/health
open http://127.0.0.1:18214/
```

- 端口：`APP_PORT`（默认 18214）。
- 数据卷：`lineagex-data:/app/data`（SQLite `lineagex.db`）；重建数据库：`docker compose down -v` 后重新 up，seed 会重放。
- seed 幂等：`meta.seed_version` 命中且脚本/schema 数量一致即跳过，不会重复插入脚本或重复建基线解析。

## 演示步骤

1. `/workbench` 点「解析样例（12 个脚本）」→ 结果页：12/12 解析成功、53 条列级边、17 条表级边、1 条表级环（`v_loop_a -> v_loop_b`）。
2. 结果页按目标表看列级关系，每条边带 `脚本:行号`、原表达式与函数变换链。
3. 工作台点「解析样例 + 坏脚本」或粘贴自定义 SQL（例如括号未闭合）→ 看到 `第 5 行第 8 列 括号未闭合` 这类定位。
4. `/review` 选 `ods_orders.amount` + 删列 → 5 个下游对象、6 步迁移清单；导出 JSON / CSV。

## API / 页面

页面：`/`（场景与样例概览）、`/workbench`（录入与执行）、`/result/{run_id}`（结论与依据）、`/review`（影响面 + 迁移 + 导出）。

API：

```bash
# 解析样例 / 样例+坏脚本 / 自定义 SQL
curl -s -X POST http://127.0.0.1:18214/api/runs -H 'Content-Type: application/json' \
  -d '{"mode":"sample"}'
curl -s -X POST http://127.0.0.1:18214/api/runs -H 'Content-Type: application/json' \
  -d '{"mode":"custom","filename":"x.sql","sql":"CREATE VIEW v AS SELECT o.amount AS amount FROM ods_orders o;"}'

# 运行详情 / 影响面 / 迁移清单 / 导出
curl -s http://127.0.0.1:18214/api/runs/<run_id>
curl -s -X POST http://127.0.0.1:18214/api/impact -H 'Content-Type: application/json' \
  -d '{"run_id":"<run_id>","table":"ods_orders","column":"amount"}'
curl -s -X POST http://127.0.0.1:18214/api/migration -H 'Content-Type: application/json' \
  -d '{"run_id":"<run_id>","table":"ods_orders","column":"amount","operation":"drop"}'
curl -s -o edges.csv "http://127.0.0.1:18214/api/runs/<run_id>/export?format=csv"
curl -s -o run.json  "http://127.0.0.1:18214/api/runs/<run_id>/export?format=json"
```

坏 SQL 返回 422，`detail.errors[]` 里带 `filename/line/col/message`。

## 目录

```text
app/
  tokenizer.py   # 词法：token + 行列位置
  parser.py      # 递归下降：语句 / 选择列表 / FROM / 子查询 / UNION
  lineage.py     # 列级血缘：别名传播、函数变换链、star 展开、诊断
  graph.py       # 环（三色 DFS）、拓扑层级、影响面
  migrate.py     # 迁移清单生成
  pipeline.py    # 编排：读样例 → 解析 → 建图 → 落库
  store.py       # SQLite 表与读写
  seed.py        # 幂等 seed + 基线解析
  report.py      # 分组视图、诊断片段、导出
  main.py        # FastAPI 路由（薄）
sample_data/sql/（12 个）、sql_broken/（1 个）、schemas/（7 张表）
tests/           # parser / lineage / graph / api
```

## 已知限制

- 单脚本单语句；不支持 WITH、窗口函数、多表 INSERT、schema 前缀以外的 DDL 解析（`USE`/`SET` 等会明确报错）。
- 视图前向引用可解析（先登记目标列名），但依赖 `SELECT *` 的循环引用无法展开，会报未知表/列诊断。
- 聚合不改变「源列身份」：`SUM(amount)` 的边仍指向 `amount`，不做粒度语义推断。
- 环内对象的拓扑层级标记为未知，迁移清单会单列一步提示先处理环。
- 仅离线样例仓库演示；不连接真实数据库、不执行 SQL。
