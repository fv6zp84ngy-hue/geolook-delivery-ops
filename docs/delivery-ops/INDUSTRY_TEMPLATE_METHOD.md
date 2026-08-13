# 行业诊断模板方法

## 1. 目标

Phase 8 把 SaaS、AI 软件产品和智能硬件示例中的高频判断沉淀为轻量 JSON 模板。
模板只提高候选问题、诊断、工单建议、资产选择和验收建议的行业相关性；真实站点审计、AI
采样、品牌事实、人工材料和七阶段证据链仍是唯一的执行依据。

```text
行业模板 → 候选问题 / 候选规则 / 候选资产 / 默认门槛
真实证据 → 人工确认诊断 → Project Owner 批准范围 → 正式工单与交付
```

## 2. 硬边界

每个模板必须声明 `candidate_only: true` 和 `supported_language: en`。加载器拒绝带有
`source_refs`、`scope_decision`、`approvals`、`deployments`、`verification` 或 `stage` 等运行态字段的模板，
也拒绝把任何状态写成 `approved`、`confirmed`、`done`、`deployed` 或 `verified`。

模板不得：

- 覆盖或降低真实审计结果；
- 构造证据引用或把证据标记为已确认；
- 自动批准品牌事实、诊断、范围、资产或人工验收；
- 在没有证据时创建已批准工单；
- 修改工单 `status` 或 `delivery.stage`；
- 绕过资产、部署、Required 检查和 Reviewer 门槛；
- 充当新的内容生成器、法律判断器或生产发布器。

## 3. 模板 Schema

模板位于 `references/industry-templates/`，核心字段如下：

| 字段 | 用途 |
|---|---|
| `required_facts` | 进入内容和资产前应具备的品牌事实候选 |
| `question_patterns` | 面向英文官网和 AI 采样的推荐问题 |
| `diagnosis_rules` | 用关键词把真实诊断映射到候选问题和资产，不生成诊断结论 |
| `asset_templates` | 语义资产类型及其现有生成器族 |
| `approval_defaults` | 建议的人工角色和适用声明类型 |
| `acceptance_defaults` | 建议的自动或人工检查项 |
| `risk_expressions` | 禁止或高风险表达及其证据要求 |
| `applicable_conversion_goals` | 模板适用的业务转化目标 |

所有对象使用稳定、带行业前缀的 ID。诊断规则必须声明 `required_evidence_types`，资产只能引用
模板内存在的审批与验收默认项。`scripts/industry_templates.py` 在读取时执行引用完整性和授权边界检查。

## 4. 四个接入点

### Bootstrap 后：补候选问题

`apply_bootstrap_candidates()` 根据 `delivery.industry` 和英文市场生成渲染后的问题候选，保存到：

```text
geo.json → delivery.industry_template_candidates
```

它不追加或修改现有 `questions`。GEO Operator 必须先判断问题是否符合目标市场和产品线，才能进入真实问题库。

### tasks.build 后：补工单建议

真实审计和诊断仍由运行时与 Delivery Ops 生成。`apply_task_templates_data()` 只根据工单标题、原因、动作、
包和验收描述匹配行业规则，并写入：

```text
tasks[].delivery.industry_template
```

其中 `asset_candidates`、`approval_defaults` 和 `acceptance_defaults` 都是建议。没有匹配规则时仍保留模板身份，
但不会凭空生成资产或审批。

### generate 前：确定默认资产候选

`generator_defaults()` 汇总工单匹配结果，并把候选写入 `assets/index.json` 的 `industry_template`。
它只选择已有的 `llms`、`jsonld`、`snippets`、`outlines` 生成器族，不增加内容引擎，
也不把“生成成功”解释为 `asset_ready` 或 `done`。

### delivery.normalize_task：兼容默认值

标准化只修复 `delivery.industry_template` 内缺失的模板版本、候选审批和候选验收字段。
标准化前后的真实 `source_refs`、`scope_decision`、`approvals`、`deployments`、`verification`、
`status` 和 `stage` 必须逐项相同。

## 5. 三类模板的判断重点

| 行业 | 重点事实与诊断 | 默认资产方向 |
|---|---|---|
| SaaS | 品类、定价、集成、安全合规、替代与比较、客户证明、Demo/试用、Help Center 一致性 | Category、Alternative、Comparison、Integration、Security FAQ、Customer proof、SoftwareApplication JSON-LD |
| AI Product | 产品与模型消歧、能力与限制、数据处理、隐私/部署、Benchmark、人工介入、适用边界、模型版本 | Definition、Capability boundary、Model/data FAQ、Benchmark block、Privacy/deployment、Comparison |
| Smart Hardware | 型号、参数、认证、兼容、国家版本、安装维修、保修、经销商、PDF 手册一致性 | Product JSON-LD、Specification、Certification、Compatibility FAQ、Installation、Regional availability、Dealer correction |

AI 模板不对模型安全性或隐私做自动结论；硬件模板不进行各国法律合规判断；SaaS 模板不把认证名称
当作有效认证证据。高风险表达只触发人工复核和证据要求。

## 6. 运行与验证

```bash
python3 -m unittest tests.test_industry_templates
python3 -m unittest discover -s tests -p 'test_delivery*.py'
```

测试覆盖三份 JSON 的 Schema/引用、英文市场门槛、别名、问题渲染、工单匹配、资产选择、旧字段保留、
禁止自动授权和三个虚构示例包。模板变更必须先通过验证，再用至少一个有真实证据的受控项目人工复核误判。

## 7. 示例与已知限制

`examples/industry-template-packs/` 下的三个包是结构预览，全部使用 `.invalid` 域名、空证据引用和
`not_approved` 范围状态。它们证明模板能产出什么候选，不证明真实客户项目已经审计、部署或验收。

Public Alpha 首期只支持英文官网、SaaS、AI 软件产品和智能硬件。不支持电商 SKU、移动应用、游戏、
医疗诊断产品、金融产品、十几个垂直行业或各国法律合规自动判断。
