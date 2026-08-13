# Public Alpha 发布检查清单

当前发布身份：`0.1.0-rc.2` · Schema `2.0` · `public_alpha` · `Public Alpha RC`

本清单适用于当前 Release Candidate。任何未来阶段计划文档均不属于本次发布流程。

本清单是发布阻断门槛。`WARN` 需要负责人说明，任何 `FAIL` 都不得发布。

## 1. 安装与代码边界

- [ ] 基于 GeoLook 提交 `f8bd3656a1b38c4fcba30b5cd46de4b61b8e9796` 的干净副本验证。
- [ ] 在 macOS / Ubuntu 的 Python 3.11 / 3.12 矩阵运行 `python3 scripts/release_validate.py --output release`。
- [ ] `release/SHA256SUMS.txt`、`BUILD_MANIFEST.json`、`RELEASE_NOTES.md` 和脱敏验证日志均已生成。
- [ ] Tester Kit 解压后 `bash run_tester_smoke.sh` 通过，且不包含完整运行时、`.env`、`work/` 或本机绝对路径。
- [ ] Change Set 1–7 按编号顺序应用，`git diff --check` 无错误。
- [ ] 未增加数据库、账号、OAuth、云控制面、新 AI 引擎或通用工作流框架。
- [ ] `scripts/delivery.py`、`scripts/geo.py` 和全部 `test_delivery*.py` 可编译/导入。
- [ ] 单文件前端的内联 JavaScript 通过语法检查。

## 2. 全链路门槛

- [ ] SaaS fixture 完成 baseline → verified，并在回归后自动重开。
- [ ] AI 产品 fixture 完成同一闭环。
- [ ] 智能硬件 fixture 完成同一闭环。
- [ ] 每条路径都包含资产拒绝、重新审批和 Web Owner 部署确认。
- [ ] 旧 GeoLook 项目首次 `delivery-sync` 保留未知字段，第二次执行结果幂等。
- [ ] 自动 required 失败无法被 Reviewer 覆盖为通过。
- [ ] 三套行业模板只产生候选项，不改写真实问题、证据、范围、审批、主状态或交付阶段。
- [ ] SaaS、AI Product、Smart Hardware 模板的引用、默认资产和英文市场门槛通过专项测试。
- [ ] Priority Score 六个维度均为 0–3，分数明细和解释可回放。
- [ ] P0 先占容量、最大工单数和最大 L 级任务数生效；超额只警告或建议下周期，不自动拒绝。
- [ ] 系统推荐不会写入 `scope_decision`；Project Owner 的人工调整有必填理由。
- [ ] GA4、GSC、CRM、表单和销售记录 CSV 只通过显式导入进入项目，重复文件导入幂等。
- [ ] 未映射信号保留为项目级观察；报告不会把任何工单写成直接带来收入。
- [ ] CSV/Markdown/JSON/GitHub Issue 导出包含固定字段，外部副本不包含 Prompt、回答、邮箱或本机绝对路径。
- [ ] GitHub 创建、手动刷新和 Webhook 失败不会覆盖内部 `status`、`delivery.stage` 或 `verification`。

## 3. 恢复门槛

- [ ] 模拟 `tasks.json` 写入中断时，原文件仍可读取且 `.geo.bak/` 有备份。
- [ ] JSONL 单行损坏不会遮蔽其他合法事件，doctor 显示 WARN。
- [ ] 删除周期 Manifest 后可从当前对象重新生成。
- [ ] 删除必需资产后工单回退并自动重开。
- [ ] 首次部署抓取超时会保存 `failed` 快照，不会静默丢失提交。
- [ ] 验收检查器异常落为 `error`，不会误判通过或中断其他检查。

## 4. 项目级 doctor

对准备发布的每个项目执行：

```bash
python3 scripts/geo.py delivery-doctor --slug <slug>
```

- [ ] `SUMMARY` 中 `FAIL 0`。
- [ ] 配置、baseline、任务结构、事件、审批、部署和路径检查均通过。
- [ ] `done` 工单均为 `verified` 且 `verification.can_close=true`。
- [ ] 所有 WARN 都有负责人、原因和处理决定。
- [ ] 如需 CI 读取，使用 `--json`；存在 FAIL 时退出码仍为 1。

## 5. 报告与公开包

- [ ] `4-GEO交付追踪报告.html` 与周期 Manifest 可从当前对象重建。
- [ ] doctor 未发现密钥、认证头、邮箱、本机绝对路径或未批准事实。
- [ ] 客户账本不包含 Prompt、完整 AI 回答、页面正文和内部说明。
- [ ] 人工检查交付资产正文；脱敏器不替代内容负责人审核。
- [ ] 只按发布白名单打包，排除 `.git`、`.env*`、`work/`、缓存和真实客户材料。

## 6. 发布决定

- 发布负责人：________________
- 检查日期：________________
- Doctor 输出或日志路径：________________
- 未关闭 WARN 与接受理由：________________
- 决定：`GO / NO-GO`
