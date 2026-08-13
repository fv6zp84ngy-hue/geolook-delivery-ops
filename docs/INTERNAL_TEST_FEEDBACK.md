# 内测用户反馈数据

本页为固定合成 Persona cohort 的模型实际执行 Baseline/Treatment A/B 内部评估结果，属于 simulated-user artifact-level internal evaluation。它不是真人客户调研，也不是生产环境业务效果数据。

## 测试范围

- Persona：8 个项目自建固定合成 Persona
- Controlled cases：2 个
- Deterministic tasks：4 个
- Baseline/Treatment pairs：32（正式 gate：至少 30）
- 模型实际执行调用：64
- 有效 trials：64 / 64
- 有效 pairs：32 / 32
- Pair coverage：100%
- Ground Truth coverage：100%
- Ground Truth：deterministic runtime truth adapter
- Persona model：DeepSeek-V4-Flash
- Experiment seed：20260813
- Provider inference seed：不声明支持

本次 Persona 来自项目内置固定合成画像，不来自 Persona 8B 数据，也不代表真人参与者。

## 结果

| 指标 | Baseline | Treatment | Paired delta |
|---|---:|---:|---:|
| Close-ready 判断准确率 | 100.0% | 100.0% | 0.0pp |
| Evidence-complete 判断准确率 | 100.0% | 100.0% | 0.0pp |
| Owner-role 判断准确率 | 0.0% | 0.0% | 0.0pp |
| Reopen-required 判断准确率 | 12.5% | 31.25% | +18.75pp |

本轮唯一观察到的总体正向变化是 `reopen_required` 判断准确率由 12.5% 提升至 31.25%，即 +18.75 个百分点。

`close_ready` 与 `evidence_complete` 在 Baseline 已达到 100%，本轮没有观察到进一步提升。

`owner_role` 在 Baseline 与 Treatment 均为 0%，因此本轮没有证据支持结构化 Treatment 改善 owner-role 判断。

## Next action

当前 runtime 没有 canonical next-action code，因此：

- `next_action` 不可评分；
- 本报告不发布 next-action accuracy；
- 不为本轮结果临时构造 Ground Truth。

## 质量门槛

正式运行预先冻结的 gate：

- minimum valid pairs：30
- minimum pair coverage：90%
- minimum Ground Truth coverage：100%

实际结果：

- valid pairs：32
- pair coverage：100%
- Ground Truth coverage：100%
- invalid trials：0
- formal gates：passed

## 解释边界

这些结果只能解释为固定 synthetic Persona 在冻结 controlled artifacts 上的内部判断表现。

不能据此声称：

- 真人客户交付效率提升；
- 用户满意度提升；
- 人工工作时间减少；
- GEO 排名、提及、引用、流量、询盘或收入提升；
- 生产环境业务结果改善。

`+18.75pp` specifically refers to `reopen_required` 的 deterministic Ground Truth 判断准确率，而不是业务效率提升 18.75%。

本轮只有 32 个 persona-task pairs，且未在该 summary 中计算置信区间或统计显著性，因此结果应作为内部 artifact-level evidence 解读，而不是总体用户效果估计。

## Persona 与模型边界

8 个 Persona 均为项目自建固定合成画像：

- Founder / GM
- Technical buyer
- Security reviewer
- Developer
- Marketing operator
- Procurement
- Customer success
- Skeptical researcher

Persona prompt profile 与 deterministic Ground Truth 相互分离；外部模型 runner 不负责计算 Ground Truth。

本轮使用 DeepSeek-V4-Flash 进行模型实际执行。`seed=20260813` 是实验/cohort/workflow 配置的一部分，不代表 provider inference 支持可重复的 deterministic seed。

未来如需提高外部有效性，可增加不同 persona-model backbone 的 robustness run，并进一步进行真人目标用户校准。
