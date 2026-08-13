# 业务优先级与周期范围推荐方法

## 1. 使用目的

Phase 9 回答一个有限问题：当企业资源不足以同时完成全部 GEO 工单时，哪些事项更值得进入当前周期？

结果写入现有 `tasks.json`：

```text
tasks[].delivery.priority
tasks[].delivery.scope_recommendation
tasks.json.scope_recommendation
```

它不是事实结论、收入预测或自动审批。Project Owner 仍需对每条事项选择批准、暂缓、拒绝或补证据，
且任何偏离系统建议的决定都要记录说明。

## 2. 业务上下文

`geo.json.delivery` 使用以下规范字段：

```json
{
  "product_lines": ["AI Support"],
  "target_markets": ["US"],
  "icps": [{"id": "mid-market", "name": "Mid-market SaaS", "buyer_roles": ["VP Support"]}],
  "conversion_goals": ["demo"],
  "strategic_priorities": [
    {
      "id": "us-demo",
      "name": "US comparison demand",
      "score": 3,
      "product_lines": ["AI Support"],
      "buying_stages": ["comparison", "validation"],
      "conversion_goals": ["demo"],
      "reason": "This cycle prioritizes qualified US demo demand"
    }
  ],
  "policy": {"max_cycle_tasks": 12, "max_large_tasks": 2}
}
```

旧字段 `conversion_goal`、`customer_profile.icp`、`planning.business_priorities` 和
`policy.max_scoped_tasks` 会被兼容读取；显式同步后补齐复数字段，但不会破坏已锁定的旧周期哈希。

## 3. 问题与工单标签

问题和工单使用同一组标签：

| 字段 | 含义 |
|---|---|
| `product_line` | 对应的产品线 |
| `market` | 现有 GeoLook 市场口径，如 `global`、`cn` 或 `both` |
| `buyer_role` | 主要购买或影响角色 |
| `buying_stage` | 固定购买阶段 |
| `conversion_goal` | 期望支持的转化动作 |

固定购买阶段为：

```text
problem_aware → solution_exploration → category_search → comparison
→ validation → purchase → post_purchase
```

关键词推断只用于补默认标签，不是用户意图事实。人工可以调整标签，后续重算会使用明确标签。

## 4. Priority Score

所有维度使用整数 `0 / 1 / 2 / 3`：

```text
Priority Score = business_value
               + buyer_intent
               + visibility_gap
               + evidence_confidence
               + feasibility
               - effort_penalty
```

| 维度 | 0 | 1 | 2 | 3 |
|---|---|---|---|---|
| 业务重要性 | 明确不相关 | 未命中战略项 | 普通战略项 | 明确命中最高战略项 |
| 购买阶段价值 | problem aware | 探索或售后 | 品类搜索 | 比较、验证或购买 |
| GEO 可见性缺口 | 无信号 | 弱信号 | 内容/外部证据缺口 | 技术、事实或结果指标缺口 |
| 证据可信度 | 无证据 | 未确认或低 | confirmed medium | confirmed high |
| 实施可行性 | 阻塞/关键信息缺失 | 部分规格 | 规格完整但资源角色缺失 | 负责人、动作、验收和角色可用 |
| 执行成本扣分 | S | M | 未知 | L |

P0 不因总分被降级。P0 只表示优先占用容量，仍必须满足 confirmed 证据、范围批准、负责人确认、
审批、部署和 Required 验收门槛。

规范结构见 [`priority-schema.json`](priority-schema.json)。

## 5. 容量规则

推荐顺序为：

1. 有 confirmed 证据的 P0；
2. 分数从高到低；
3. 同分时低成本优先；
4. 最后按稳定工单 ID 排序。

系统依次检查：

- `max_cycle_tasks`：当前周期最大工单数，Public Alpha 仍不超过 12；
- `max_large_tasks`：当前周期最多 L 级任务数；
- 既有 `capacity_points`、`available_owners` 和按角色资源点；
- 诊断是否至少有一条 confirmed 证据。

不能进入当前周期的事项标记为 `next_cycle` 或 `needs_evidence`。如果 P0 因容量无法进入，摘要必须产生
显式警告，Project Owner 需要说明调整理由。系统不会自动把这些事项写成 `deferred` 或 `rejected` 决策。

## 6. 报告片段

第 4 份交付追踪报告新增“业务优先级与周期范围推荐”，展示：

- 推荐工单数 / 最大工单数；
- L 级任务数 / 最大 L 级任务数；
- P0 占用情况；
- 建议保留下周期的工单；
- 每条工单的产品线、购买阶段、分数、推荐结论和容量原因。

报告不输出内部战略说明全文、预测收入、LTV、搜索量或跨客户 Benchmark。

## 7. 明确排除

第一版不做搜索量预测、收入预测、LTV、机器学习评分、自动预算、自动拒绝、跨客户排名或自动商业决策。
改变战略优先项后应重新运行范围建议，并由 Project Owner 重新检查人工决定是否仍成立。
