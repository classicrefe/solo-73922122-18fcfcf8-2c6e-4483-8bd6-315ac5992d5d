# 验布作业卡供数台账

织厂数仓的验布供数网页应用：登记作业卡实检清单、提交批次（唯一批号、
所用卡、异常项目可空）、按项目查三种口径的批数、选两个不同项目查
**两项共检四格批数**（两类异常是否常出现在同一批布上），并在实检清单
填错时更正已入库批次的供数。Flask + SQLite，Docker Compose 启动。

## 启动

```bash
docker compose up --build
# 打开 http://localhost:8000  （WEB_PORT=8080 docker compose up 可改端口）
```

数据存放在命名卷 `ledger-data`（容器内 `/data/inspection.sqlite3`）。
同一批卡与项目字典由应用启动时幂等写入；旧库升级时共检统计会在启动阶段
从原批次**自动补齐一次**（见下文“旧库补齐”）。

本地不用 Docker 时：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
flask --app 'app:create_app()' run --debug
# 或与镜像一致：
gunicorn --workers 4 --threads 2 --bind 0.0.0.0:8000 'app:create_app()'
```

## 网页演示：清单变更前后的共检计数

1. 打开 `/query`，在“两项共检”里选 **色差(I04)** 与 **污点(I05)**
   （或直接点顶部导航“色差×污点共检” → `/co-inspection/color-stain`）。
2. 点 **“一键填入「色差 × 污点」演示数据”**：写入 14 个 `GYS-DEMO-*`
   幂等批次。更正前只有 C003 的 10 批纳入，四格为 **3 / 2 / 1 / 4**
   （两项均异常 / 仅色差 / 仅污点 / 均无异常）。
3. 在下方“分卡纳入情况”里找到 **C004（未纳入：缺后项）**，展开“更正清单”，
   勾选“污点 I05”后“保存并重算共检纳入”。页面带 `next` 回到同一查数页，
   C004 的 4 个历史批按新清单立即纳入，四格变为 **3 / 3 / 1 / 7（合计 14）**。
4. 反向取消 I05 保存，C004 又整体退出；历史批次无需任何迁移。

也可用脚本对运行中的实例演示同样的“更正前/后”：

```bash
python3 scripts/demo_coinspect.py http://127.0.0.1:8000
```

## 页面

- `/` 登记批次、更正各卡实检清单、已登记批次列表
- `/query?item_id=N` 单项目三口径批数：有异常 / 已检无异常 / 未检
- `/query?item_a=A&item_b=B` 两项共检四格批数（同页第二张表），
  别名 `/coquery`、`/co-query`、`/pair-query`、`/coinspect`；
  加 `&format=json` 或访问 `/api/co-inspection?item_a=A&item_b=B` 取 JSON
- `/co-inspection/color-stain` 按项目编码稳定跳转到 色差×污点
- `/cards/<id>` 单卡清单与引用批次
- `/batches/<id>` 批次原异常明细（更正不改动）
- `/audit` 按原始批次逐条重算，与物化计数核对（含共检行），并可一键补齐共检统计

## 两项共检的口径

- 必须选**两个不同项目**；相同/缺失时页面内联提示，JSON 接口返回 400。
- **只纳入按现行作业卡清单两项都实检的已收批**：某卡现行
  `card_items` 同时含这两项时，该卡的全部已提交批纳入；否则整卡不纳入。
- 四格（以前项 A、后项 B 的顺序定向；交换 A/B 时两个“仅”格互换）：
  - `both_abnormal` 两项均异常
  - `only_a_abnormal` 仅前项异常
  - `only_b_abnormal` 仅后项异常
  - `neither_abnormal` 两项均无异常
- 四格互斥，合计 = 现行清单同时含两项的各卡
  `submitted_batch_count` 之和。
- **清单更正即重新划界**：更正不重写、不扫描批次，查数语句 JOIN 现行
  `card_items`，所以历史批次按新清单自动纳入或退出。会让现存异常明细
  落到清单外的更正仍被“整次退回”，因此共检事实与单项目计数始终一致。
- **同一已提交状态**：全部计数只针对唯一的 `submitted` 状态；共检查数是
  单条聚合 SQL（一个语句快照），读到的是同一个已提交状态。

## 读写量为什么不随批数/项目数增长

- 单项目计数物化在 `card_item_stats`，行数 = 卡数 × 项目数（固定）。
- 共检只额外物化“两项均异常”的交集到
  `card_item_pair_stats`，行数 = 卡数 × C(项目数, 2)（固定；当前 4×15=60）。
  其余三格在查数时由 `cards.submitted_batch_count` 与两个单项目异常数
  代数推出：
  `仅A = abnA − both`，`仅B = abnB − both`，
  `均无 = N − abnA − abnB + both`。
- **查数**：每条卡/固定项目对只读固定行并走索引，不碰批次/明细表；
  progress-handler 实测 n=2 与 n=302 的 VDBE 指令数完全相等
  （聚合 114 步、整页状态 571 步）。
- **更正**：与共检出现前完全相同，只写该卡固定的项目行，不额外扫批次，
  因此读写量仍与批数无关（原有精确相等测试继续通过）。
- **报批新增的共检读写**：一批有 `k` 个异常项时，只对异常项两两组合
  `C(k,2)` 行做 UPSERT；每个异常项数固定时，新增记录读写量固定，
  不随项目总数 M 增长。实测 k=3 时 M=6 与 M=46 的 progress 步数逐拍相等
  （135 步、恰好 3 条 pair 写语句），k=2 时恰好 1 条。

## 原子性与失败回滚

报批时，批次行、原异常明细、单项目计数与共检 pair 行在**同一个
`BEGIN IMMEDIATE` 事务**内提交；任何一步失败（批号重复、清单外项目、
注入的统计异常等）整体回滚，批次/明细/两套计数都不留半成品，查数仍是
上一个已提交结果。测试用 monkeypatch 让共检写入抛错，验证了这一点。

## 旧库补齐

- 新表 `card_item_pair_stats` / `app_meta` / 兼容视图
  `co_inspection_stats` 等都用 `IF NOT EXISTS` 创建，旧卷可直接启动。
- 首次启动（`app_meta` 无 `pair_stats_backfilled_v1`）在固定字典写入的
  同一事务里，用 `batch_abnormalities` 自连接 `GROUP BY` **一次补齐**
  全部“两项均异常”，随后写标记只执行一次；新库为空扫，代价为零。
- 也可随时从原批次幂等强制重建：
  - 网页：`/audit` → “从原批次一次补齐 / 强制重建共检统计”
  - CLI：`flask --app 'app:create_app()' backfill-co-stats`
  - 函数：`service.rebuild_pair_stats(conn)`（别名
    `backfill_pair_stats` / `backfill_co_stats`）

## 口径与规则（原单项目）

- **唯一状态**：只有 `submitted` 一种已提交状态入库；提交与更正事务内先
  校验后写入，失败整体回滚。查数与更正都只针对该状态，三种口径互斥且
  合计等于该卡已收批数。
- **提交**：批号唯一；异常项目必须在所用卡的实检清单内。任何一项在清单
  外（或批号重复）则整批退回，不写批次、不写明细、不动计数。
- **更正**：替换该卡实检清单。原 `batch_abnormalities` 明细行一字不改；
  按新清单与“已收批数”重算该卡固定大小的计数行。
- **整次退回**：更正若会让任何一条已存异常明细落到清单外，整卡不写入。
  数据库触发器再兜底这两条不变量。
- **并发**：所有写操作在 `BEGIN IMMEDIATE` 下串行（SQLite 单写者 + busy
  timeout），后到者基于前一个已提交状态校验。

## 测试与试跑结果

```bash
python3 -m pytest -q
# 30 passed（原 13 + 新增 tests/test_co_inspection.py 17）
python3 scripts/http_smoke.py http://127.0.0.1:8000
python3 scripts/demo_coinspect.py http://127.0.0.1:8000
```

新增测试覆盖：四格与朴素逐批重算一致、多卡汇总、交换前后项、更正后纳入/
退出/再纳入、固定行数、共检查数对批数恒定、pair 写入对项目总数恒定、
报批中途失败整体回滚、批号重复不动 pair 计数、原批次重建幂等、**仿旧库
（含真实 HEAD 版 schema）启动自动补齐**、CLI、网页 data-count、JSON、
`next` 更正回跳、一键演示前后数值、4 线程并发共检一致。

交付前已用镜像同款 gunicorn 命令（2–3 workers）实跑：
`http_smoke.py`（含共检断言与 8 并发抢同批号）与 `demo_coinspect.py`
全部 `OK`，`/audit` 单项目与共检不一致数均为 0。

## 目录

```
app/
  __init__.py      应用工厂
  db.py            SQLite 连接、建表、多 worker 安全初始化、backfill CLI
  schema.sql       表、索引、不变量触发器、共检 pair 表与兼容视图
  seed.py          固定卡/项目字典，预建全部单项目与 pair 行，首次补齐
  service.py       提交 / 更正 / 查数 / 共检 / 补齐 / 核对 / 演示数据
  routes.py        HTTP 路由（/query 单+双项、JSON API、演示与补齐）
  templates/       页面（query.html 含两项共检四格与分卡更正）
  static/          样式与异常项目联动脚本
tests/test_ledger.py          原 13 个测试（保持通过）
tests/test_co_inspection.py   共检新特性 17 个测试
scripts/http_smoke.py         HTTP 冒烟（已加共检前后断言）
scripts/demo_coinspect.py     色差×污点 清单变更前后演示
Dockerfile, docker-compose.yml, requirements.txt
```

## 已知限制 / 尚未完成

- 未实现登录与权限；任何能访问页面的人都能提交、更正或触发演示/重建。
- 卡与项目字典固定写在 `seed.py`，页面不提供增删；共检 pair 行按当前
  字典预建，运行时新增项目后首次相关写入会用 UPSERT 自愈，但没有完整
  的项目管理页。
- 只有单一 `submitted` 状态；没有历史版本链，更正不保留清单快照
  （原异常明细保留，共检交集是不可变明细的物化，故无需随更正重算）。
- `/audit`、补齐与 `scripts/` 会扫描批次，仅用于核对/迁移/演示，不在
  在线查数或更正路径上。
- **本交付环境没有 docker CLI / daemon（也无 `/var/run/docker.sock`），
  因此 `docker compose up --build` 未能在此实际执行**；已用 PyYAML 校验
  `docker-compose.yml`，并用镜像 Dockerfile 的同款 gunicorn 启动命令在
  本机实跑全部 HTTP 关键操作通过。镜像 build/拉取基础镜像与命名卷挂载
  仍需在有 Docker 的环境最终确认一次。
