# cc-data-lab

CC_Codex 二期（26.11）**数据工程**主线基线：**采集任务控制台**（Flask 后端 + HTMX 前端页 + 站内 Mock 目录）。

## 领域

- **所属领域**：数据工程  
- **子方向**：数据采集、爬虫与 RPA  

## 技术栈

- Python 3.12+、Flask 3、SQLite  
- HTMX（CDN）+ Jinja 模板  
- pytest  

## 本地运行

```powershell
cd claude-workspace\cc-data-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

浏览器打开 http://127.0.0.1:5590 ，登录 **admin / 123456**。

流程：创建任务 → 选种子 `/mock/catalog/alpha`、`beta` 或 `gamma` → 详情页「运行采集」→ 查看落库（alpha 含重复标题，会演示去重跳过）。

## 异常链路（分流 → 复核栏 → 账册 → 二次校验）

`/mock/catalog/gamma` 内置缺号、短题、号相撞及复合异常，用于验收整条链路。

**分流三筐（一行只进一筐，优先级：缺号 > 号相撞 > 短题）**

| 筐 | 触发 | 可否改判 |
|----|------|----------|
| 缺号 `missing` | 源行编码为空 | 无改判入口 |
| 号相撞 `collision` | 同批非空编码重复 | 无改判入口 |
| 短题 `short` | 标题长度 < 6 | 仅短题可改判，须填原因 |

复合异常按优先级只进一筐，被压过的规则写入 `priority_note`，列表行可展开查看原因、优先说明、复核结果。

**改判与复核**

1. 先在任务页**锁定冲突口径**（锁定后不可改口）：
   - `reject`：复核通过时若编码已在账册，整笔改判失败，退回短题筐；
   - `copy`：生成冲突副本留在复核栏，禁止入账。
2. 短题改判**先进复核栏，不直接进账册**；原因必填。
3. 点「通过」时携带的口径与已锁定选择不一致 → 直接退回短题筐并写「处理不对」。
4. 账册写入在单事务内原子完成；中途失败整体回滚（无半截编码），复核栏留 `failed` 原因，筐行仍挂 `in_review`，可重新通过。
5. 并发改判同一短题行：`BEGIN IMMEDIATE` 写锁 + 部分唯一索引双保险，只留一笔进复核栏。
6. 任务 `running`（筐正在写入）期间禁止改判。

三处状态同源：复核栏 `review_queue`、原筐 `exception_bucket`、账册 `crawl_ledger`。详情页顶部亮出三筐件数、复核栏件数与当日锁定口径。

验收测试：`pytest tests/test_exception_chain.py tests/test_http_chain.py`（八段：优先级 / 改判先入复核 / 两种口径 / 口径不一致退回 / 中途写入失败 / 并发唯一 / 写入中拒改 / 件数与口径展示）。

## Docker 一键启动（推荐用于快照前验收）

需已打开 **Docker Desktop**：

```powershell
cd claude-workspace\cc-data-lab
docker compose up -d --build
docker compose ps
```

- 页面：http://127.0.0.1:5590/login  
- Mock：http://127.0.0.1:5590/mock/catalog/alpha  
- 停止：`docker compose down`（保留库卷）· 清卷：`docker compose down -v`

`docker compose ps` 中 `STATUS` 含 **healthy** 表示服务可访问。线下表「环境可复现等级」可填 **已容器化，可一键起环境**。

## 前后端说明

| 层 | 说明 |
|----|------|
| 后端 | `app/` 路由、任务状态机、同步 runner、SQLite |
| 前端 | `templates/` + `static/`，浏览器可点操作 |
| Mock 源站 | `/mock/catalog/<id>` HTML，无需外网 |

## 初始快照

在首次 Claude 单轮跑题前 commit + push 本树（不含 `.venv/`、`instance/*.db`）。后续题目在同 repo 上叠加调度、RPA 步骤、增量水位等能力。

## 基线 vs 跑题目录

| 路径 | 角色 |
|------|------|
| `claude-workspace/cc-data-lab`（本目录） | **基线**：改功能、docker 验收 |
| `ccproject/cc-data-lab/data-XX` | **单题**：复制基线后挂容器、出快照 |

复制：`..\scripts\Copy-BaselineToQuestion.ps1 -QuestionId data-01`

## 跑题 Harness

见 `CCcodex/docs/ClaudeCode_Windows使用说明.md`（镜像 2.0）。容器挂载 **ccproject 下该题目录**，不是本基线路径。
