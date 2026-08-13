# GEO Delivery Ops 方法说明

## 1. 方法目标

GEO Delivery Ops 把“品牌在 AI 搜索里表现不好”转换为可审计的交付闭环：

```text
范围与基线 → 证据诊断 → 人工定范围 → 可分派工单
→ 资产与审批 → 真实部署 → 自动验收与复盘
```

适用对象是 10–200 人、以英文官网服务海外市场的 SaaS、AI 和智能硬件团队。
它不是排名承诺工具，也不把一次 AI 回答变化解释成业务增长因果。

## 2. 六条方法原则

1. **证据先于结论**：进入范围的诊断至少绑定一条证据；自动映射的证据先保持待复核。
2. **人工门槛不可伪造**：范围、事实、内容、技术部署和品牌合规结论由对应角色明确提交。
3. **任务状态与证据阶段分离**：`status` 表示是否在执行，`delivery.stage` 表示证据链走到哪里。
4. **资产生成不等于完成**：资产必须存在、非空、通过事实预检和必要审批，之后还要真实部署和验收。
5. **周期隔离**：诊断、事件、资产、部署和验收都绑定唯一 `cycle_id`，历史周期不混入当前周期。
6. **通过可以被推翻**：后续复跑发现资产、部署或验收回归时，系统记录回归并自动重开工单。

## 3. 七阶段定义

| 阶段 | 数据门槛 | 人工门槛 | 主要输出 |
|---|---|---|---|
| `baseline` | 唯一 cycle、范围确认、同 cycle 的基线快照 | Project Owner 确认范围与事实复核状态 | `baseline.json` |
| `diagnosed` | 至少一条已确认诊断证据 | GEO Operator 或 Reviewer 说明确认/排除原因 | 证据化诊断案例 |
| `scoped` | `scope_decision.status=approved` | Project Owner 说明批准、暂缓、拒绝或补证据原因 | 周期执行范围 |
| `assigned` | 负责人角色、动作、依赖、目标、验收规格完整 | 映射出的负责人确认可执行或说明阻塞 | 可分派工单 |
| `asset_ready` | 必需资产存在、非空、版本与哈希已记录、预检和审批通过 | Fact/Content/Web Approver 按资产类型审批 | 已批准资产包 |
| `deployed` | 当前资产版本有真实部署记录，URL 或渠道证据有效 | Web Owner 明确确认实际发布 | 部署记录与抓取快照 |
| `verified` | Required 检查综合通过，证据链完整 | 无法自动判断时 Reviewer 给出版本绑定结论 | 验收快照、关闭或回归 |

阶段由数据门槛计算，不是用户任意选择。资产审批被拒绝、部署内容消失或验收回归时，阶段会回退。

## 4. 诊断证据模型

证据引用只保存稳定索引和小型快照，不复制整页正文或完整 AI 回答。支持的来源包括：

- `audit`：站点级或页面级审计项；
- `sample`：Prompt、回答和引用域名的稳定采样索引；
- `metric`：提及率、引用份额等周期指标；
- `fact`：品牌事实库条目或版本指纹；
- `page`：目标页面；
- `external`：外部引用域名或渠道证据；
- `manual`：人工提交材料。

每条证据记录置信度和 `pending / confirmed / rejected`。产品型号、品牌别名、否定语境、
竞品语境和抓取结构误判必须优先人工复核。

## 5. 范围与优先级

技术门票问题保持 P0。其他候选项依据四个维度提供解释性排序：影响、紧迫性、执行成本和证据置信度。
Public Alpha 默认最多批准 12 条工单；排序建议不替代 Project Owner 决策。

范围锁定时生成 `scope_sha256`。目标市场、产品线、ICP、竞品、问题库或确认范围发生变化后，
原范围确认失效，不能静默沿用旧基线。

## 6. 资产、事实和审批

GeoLook 原有生成器继续负责 llms.txt、JSON-LD、定义块、FAQ、大纲和草稿。
Delivery Ops 只增加绑定、版本、SHA-256、事实预检、审批和部署记录。

审批角色按内容风险拆分：

- 品牌定义、数字、客户名称、合规声明：`fact_approver`；
- 正文、FAQ、对比内容：`content_owner` 或 `reviewer`；
- llms.txt、JSON-LD、HTML 技术片段：`web_owner`。

文件内容变化后版本递增，旧审批和旧部署证据不能漂移到新版本。

## 7. 验收与回归

每项验收保存 `before / after / target / required / verdict`。Required 检查决定关闭，Optional 失败只产生 warning。

关闭条件为：

```text
自动检查满足聚合规则
+ 必需资产当前版本有效
+ 必需审批通过
+ 当前资产版本有部署证据
+ baseline 与 cycle 一致
+ 证据链完整
+ 必需 Reviewer 结论通过
```

`pass` 只描述检查结果，`verification.can_close=true` 才表示证据链允许关闭。
已关闭工单后续不再满足门槛时，会记录 `verification_regressed` 和 `task_reopened`。

## 8. 周期账本与复盘

每个周期保留：

```text
delivery/snapshots/<cycle_id>/baseline.json
delivery/events/<cycle_id>.jsonl
verify/<timestamp>.json
delivery/manifests/<cycle_id>.json
delivery/ledger/<cycle_id>/
deliverables/4-GEO交付追踪报告.html
```

Manifest 聚合当前周期工单、事件、资产、部署、验收、回归和文件哈希。客户账本使用
`client_delivery_v1` 脱敏投影，保留可审计状态但移除凭证、邮箱、本机路径、Prompt、AI 回答、页面正文和内部说明。

## 9. 结果解释边界

- AI 回答存在采样噪声；单次变化不能代表稳定趋势。
- 流量、询盘与商机应按周期记录，但不能仅凭同期变化声称 GEO 造成增长。
- “未测”“数据不足”和“待人工”必须原样保留，不能填补成通过。
- 外部平台是否收录或引用由平台决定，本方法不承诺排名、提及或引用结果。
