# 外部工具单向同步

Phase 11 将 GeoLook 交付数据导出为执行副本，并可显式创建 GitHub Issue 或发送脱敏 Webhook。GeoLook 的 `tasks.json`、事件日志、资产、部署和验收仍是唯一证据真相源；外部状态不会覆盖内部 `status`、`delivery.stage` 或 `verification`。

## 导出

```bash
python3 scripts/geo.py delivery-export --slug <slug> --format csv --file exports/tasks.csv
python3 scripts/geo.py delivery-export --slug <slug> --format markdown --file exports/tasks.md
python3 scripts/geo.py delivery-export --slug <slug> --format json --file exports/tasks.json
python3 scripts/geo.py delivery-export --slug <slug> --format github --file exports/github-issues.json
```

`csv`、`notion_csv` 和 `jira_csv` 使用相同的扁平字段：`task_id`、`title`、`priority`、`owner_role`、`stage`、`action`、`target_url`、`asset_paths`、`acceptance`、`next_action`、`deadline`。

## GitHub Issue

配置 `GITHUB_TOKEN` 和 `GITHUB_REPOSITORY=owner/repository` 后，显式运行：

```bash
python3 scripts/geo.py github-issue --slug <slug> --task-id T-001
python3 scripts/geo.py github-refresh --slug <slug> --task-id T-001 --external-id 123
```

创建成功后只写入工单 `external_refs`：系统、Issue ID、URL、同步时间和外部状态。刷新是手动的，不会根据 GitHub `closed` 自动关闭内部工单。

## Webhook

Webhook 必须由用户显式触发，并且事件只允许：`task_approved`、`task_assigned`、`asset_pending_approval`、`deployment_pending`、`verification_failed`、`regression`。

```bash
python3 scripts/geo.py delivery-webhook --slug <slug> --task-id T-001 \
  --event task_assigned --url https://example.invalid/hook
```

Payload 会移除凭证、邮箱、电话、绝对本地路径和内部 Prompt/回答。该适配器不重试、不编排、不接受外部状态写回。
