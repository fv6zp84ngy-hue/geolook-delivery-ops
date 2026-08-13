# 观察性业务信号方法

Phase 10 只增加一层“交付结果旁的业务观察”。它不做收入归因，不识别跨设备用户，不读取客户 CRM，也不把单个工单描述为直接带来收入。

## 支持来源

`ga4`、`gsc`、`crm`、`forms`、`sales_manual` 和 `sales_ai_report` 只能由用户显式选择并导入 CSV。系统保留来源类型、文件哈希、行号和导入时间；联系人姓名、邮箱、电话等列不会进入统一信号对象，也不会出现在 UI 或交付报告。

## 最小统一对象

```json
{
  "signal_id": "sig-001",
  "date": "2026-08-01",
  "source_type": "ga4",
  "channel": "ai_referral",
  "platform": "chatgpt",
  "landing_page": "/product",
  "conversion_type": "demo_request",
  "count": 1,
  "market": "US",
  "product_line": "AI Support",
  "confidence": "observed"
}
```

`observed` 表示来源文件直接记录；`reported` 表示销售人工记录；`self_reported` 表示销售自报“客户从 AI 了解到我们”。这些标签是证据状态，不是因果强度。

## 映射顺序

1. `landing_page` 命中工单 `assignment.target_pages`；
2. 命中工单部署记录的 `target_url`；
3. 只有一个候选时绑定工单；
4. 没有候选或候选不唯一时保留为项目级信号。

系统不会因为产品线、问题文本或时间相近就强行归因。未映射行会在摘要和报告中单独显示。

## 报告解释

报告展示 AI referral sessions、AI 流量进入页面、表单提交、Demo/RFQ、销售自报 AI 来源、映射率和字段完整度。Before/after 只能表示同向变化或数据不足，不能写成“GEO 带来收入”。

## 恢复与边界

统一记录位于 `work/<slug>/business_signals.jsonl`，原始 CSV 仅留在 `imports/<type>/` 的本地项目目录。相同文件哈希重复导入是幂等的；损坏的 JSONL 行会被跳过并在下一次摘要中保留可读性。导入不触发抓取、发布、CRM 写回或自动任务关闭。
