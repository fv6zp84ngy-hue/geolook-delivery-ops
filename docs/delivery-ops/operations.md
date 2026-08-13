# 安装与操作手册

## 1. 应用实施单元

在干净的 GeoLook checkout 中复制核心和测试，然后按顺序应用补丁：

```bash
cp /path/to/geolook-delivery-ops/scripts/delivery.py scripts/delivery.py
cp /path/to/geolook-delivery-ops/tests/test_delivery*.py tests/
git apply /path/to/geolook-delivery-ops/patches/change-set-1.patch
git apply /path/to/geolook-delivery-ops/patches/change-set-2.patch
git apply /path/to/geolook-delivery-ops/patches/change-set-3.patch
git apply /path/to/geolook-delivery-ops/patches/change-set-4.patch
git apply /path/to/geolook-delivery-ops/patches/change-set-5.patch
git apply /path/to/geolook-delivery-ops/patches/change-set-6.patch
git apply /path/to/geolook-delivery-ops/patches/change-set-7.patch
python3 -m unittest discover -s tests -p 'test_delivery*.py'
```

补丁必须按编号应用。若上游文件发生变化，应先人工复核补丁上下文。

## 2. 示例项目

示例是离线、虚构快照，只用于查看 Schema 和界面：

```bash
cp -R examples/saas-ai-support work/saas-ai-support-demo
python3 scripts/geo.py ui
```

智能硬件示例同理。`.invalid` 域名不会解析，不要直接运行 crawl、deployment check 或 verify；
如需走完整流程，请复制示例、替换为你有权测试的官网，再开始新周期。

## 3. 七阶段常用命令

```bash
python3 scripts/geo.py delivery-baseline --slug <slug> --problem-id Q-001 --note "scope locked"
python3 scripts/geo.py delivery-diagnose --slug <slug>
python3 scripts/geo.py delivery-review-evidence --slug <slug> --task-id T-001 --evidence-id <id> --status confirmed --role reviewer --note "reviewed"
python3 scripts/geo.py delivery-scope-suggest --slug <slug>
python3 scripts/geo.py delivery-scope-decide --slug <slug> --task-id T-001 --status approved --reason "priority reason"
python3 scripts/geo.py delivery-assign-prepare --slug <slug>
python3 scripts/geo.py delivery-assign-confirm --slug <slug> --task-id T-001 --status confirmed --role <owner_role> --note "executable"
python3 scripts/geo.py generate --slug <slug> --asset llms,jsonld,snippets,outlines
python3 scripts/geo.py delivery-assets-prepare --slug <slug>
python3 scripts/geo.py delivery-deploy-prepare --slug <slug>
python3 scripts/geo.py verify --slug <slug>
```

资产审批和部署需要具体 `asset_id`、角色、URL 与说明，建议在“交付流水线”页面操作，减少字段输入错误。

## 4. 周期与账本

```bash
python3 scripts/geo.py delivery-show --slug <slug>
python3 scripts/geo.py delivery-events --slug <slug>
python3 scripts/geo.py delivery-cycle-end --slug <slug> --note "cycle reviewed"
python3 scripts/geo.py delivery-cycle-start --slug <slug>
```

生成正式客户交付物后，检查 `delivery/<日期>/manifest.json`、`ledger/` 和
`07-GEO交付追踪报告.html` 是否同时存在。

## 5. 发布前 Doctor

```bash
python3 scripts/geo.py delivery-doctor --slug <slug>
python3 scripts/geo.py delivery-doctor --slug <slug> --json
```

Doctor 不写文件。出现 `FAIL` 时退出码为 1；不要通过手工修改 `stage`、`status` 或
`verification.can_close` 消除错误，应按输出的“下一步”恢复原始对象并重跑对应流程。

常见处理：

- JSONL 坏行：保留日志，按行号从备份补回；合法事件仍可读取。
- Manifest 缺失：重新运行验收或生成交付追踪报告。
- 资产漂移/丢失：恢复或重新生成后扫描，并重新审批、部署和验收。
- `done` 无最终验收：重开工单；required 自动失败不能由 Reviewer 覆盖。
- 报告泄漏：重新生成脱敏报告并按公开白名单人工检查。
