# 数字档案长期保存服务

仅使用 Python 3.11+ 标准库实现的独立档案保存项目，支持清单校验、真实 SHA-256 内容校验、多个离线副本、损坏检测与自动修复、格式迁移、保留期限和访问控制。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

服务地址为 <http://127.0.0.1:8102>，默认数据库 `preservation.db`。测试：

```bash
python3 -m unittest -v
```

演示用户：`owner`、`archivist`、`auditor`、`outsider`。API 使用 `X-User-Id`。文件通过 Base64 提交，单文件上限 10 MiB；这是为了保持示例自包含，生产部署应换成对象存储和流式上传。

## 主要接口

- `POST /api/archives`：创建受限档案。
- `POST /api/archives/{id}/members`：所有者授予 read/write 权限。
- `POST /api/archives/{id}/versions`：提交文件清单，服务端重新计算哈希和大小。
- `GET /api/versions/{id}`：查看版本、文件清单和副本状态。
- `POST /api/versions/{id}/copies`：创建独立副本内容。
- `POST /api/copies/{id}/verify`：校验副本；发现损坏时从健康副本修复。
- `POST /api/copies/{id}/simulate-corruption`：演示/测试介质损坏，仅 owner 或 archivist 可用。
- `POST /api/versions/{id}/migrate`：生成格式迁移后的新版本并保留派生关系。
- `GET /api/archives/{id}/status`：保留期限、版本状态和审计记录。
- `POST /api/copies/{id}/retirement-requests`：登记副本退役申请（接手位置、保管人），进入待复核。
- `GET /api/retirement-requests/{id}`：查看退役申请；待复核申请附带实时阻塞项与比对明细。
- `POST /api/retirement-requests/{id}/review`：复核申请；条件全部满足则确认退役，否则退回并列出阻塞项。
- `GET /api/versions/{id}/retirement-requests`：版本的退役申请记录。

副本退役交接分为三个业务文件：`retirement_schema.py`（数据表：退役申请单、副本 `retired_at` 标记）、`retirement_rules.py`（规则：登记、复核、确认与版本状态重算）、`web/retirement.html`（页面，地址 `/retirement`）。确认条件：至少一份其他健康副本完成内容比对且与版本清单一致，且退役副本最近核验晚于版本生成；确认后旧副本标记退役并留档，版本状态按剩余健康副本重算，全程写审计日志。

档案路径拒绝绝对路径和 `..`；同一版本副本位置唯一；没有健康副本时版本标记为 `degraded`；所有变更写入审计日志。
