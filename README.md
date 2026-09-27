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
- `POST /api/copies/{id}/retirement`：申请副本退役交接，登记接手位置（`new_location`）与保管人（`custodian`），生成待复核交接单。
- `POST /api/retirements/{id}/review`：由申请之外的管理员复核，`{"decision":"confirm|reject"}`；条件不满足时自动退回，响应 `blockers` 列出全部阻塞项。
- `GET /api/retirements/{id}`：查询交接单留档。
- `GET /retirement`：副本退役交接页面。
- `POST /api/versions/{id}/migrate`：生成格式迁移后的新版本并保留派生关系。
- `GET /api/archives/{id}/status`：保留期限、版本状态和审计记录。

档案路径拒绝绝对路径和 `..`；同一版本副本位置唯一；没有健康副本时版本标记为 `degraded`；所有变更写入审计日志。

## 副本退役交接

更换离线介质时不能只盯着副本健康就停掉旧介质，必须走交接复核：

1. 申请：选定待退役副本，登记接手位置与保管人，交接单进入 `pending`；同一副本同时只能有一张待复核单。
2. 复核确认须同时满足（复核人不能是申请人）：
   - 除退役副本外仍有至少一个健康副本，且这些健康副本最近完成过与版本权威内容的逐文件比对（路径集合、SHA-256 全部一致）；
   - 待退役副本的最近核验时间晚于该版本的生成时间。
3. 任一不满足则交接单退回（`rejected`）并在 `blockers` 中列出阻塞项（如 `no_other_healthy_copy`、`copy_content_mismatch`、`retiring_verification_stale`）；阻塞消除后可重新申请。
4. 确认后旧副本状态置为 `retired` 并保留全部内容留档（不能再核验或修复），版本状态按剩余健康副本数重算；申请、退回、确认均写入审计日志，版本详情（`GET /api/versions/{id}`）可追溯每个副本的退役记录。

实现按职责拆分为三个业务文件：`retirement_schema.py`（数据表与旧库迁移）、`retirement_rules.py`（交接规则）、`retirement_api.py`（HTTP 路由与页面接入，页面为 `web/retirement.html`）。
