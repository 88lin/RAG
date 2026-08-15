# 数据库迁移

数据库地址从 `config.DATABASE_URL` 读取，**不在 `alembic.ini` 里配**——
配置的唯一入口是 `config.py`，且这样 `alembic.ini` 不含密码可安全提交。

## 常用命令

```bash
# 建表 / 升到最新
venv/Scripts/python.exe -m alembic upgrade head

# 改了 backend/db/models.py 后生成迁移
venv/Scripts/python.exe -m alembic revision --autogenerate -m "说明"

# 看当前版本与历史
venv/Scripts/python.exe -m alembic current
venv/Scripts/python.exe -m alembic history

# 回退一个版本
venv/Scripts/python.exe -m alembic downgrade -1
```

换库只改 `DATABASE_URL`：

```bash
# SQLite（默认，本地开发）
DATABASE_URL=sqlite+aiosqlite:///./data/app.db

# PostgreSQL
DATABASE_URL=postgresql+asyncpg://raguser:密码@localhost:5432/ragdb
```

## 注意

**`alembic.ini` 必须保持纯 ASCII**。configparser 以 `encoding="locale"`
读它，Windows 上即 GBK，文件里有中文会在 alembic 启动前就抛
`UnicodeDecodeError`，且报错信息指向 configparser 内部，看不出是编码问题。
说明写在本文件里，不写进 ini。

同理，`alembic/env.py` 开头把 stdout/stderr 切成 UTF-8 ——
迁移说明是中文，GBK 控制台遇中文即 `UnicodeEncodeError`。

**autogenerate 的产物必须人工审阅**。它检测不到表/列改名——
会生成"删旧的 + 建新的"，直接跑会丢数据。也检测不到 CHECK 约束
与部分索引的变更。

**`init_models()`（`create_all`）只用于测试**。它与迁移并存的风险是
schema 漂移：`create_all` 不更新已存在的表，改了模型却只跑它会得到旧表。
生产路径只有 `alembic upgrade head`。

SQLite 上迁移以 batch 模式渲染（`render_as_batch`）：SQLite 的
`ALTER TABLE` 只支持加列与改表名，改列类型或加约束需要建新表拷数据。
