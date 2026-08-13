# P1 提交范围

本轮建议提交以下源代码、配置、文档和测试：

```text
.env.example
scripts/geo.py
scripts/internal_feedback_benchmark.py
scripts/persona_runner.py
docs/PERSONA_RUNNER.md
docs/INTERNAL_TEST_FEEDBACK.md
tests/test_internal_feedback_benchmark.py
```

这些文件组成 P1 的最小可审阅闭环：CLI、固定 Persona cohort、外部
MatrAIx/Persona runner 协议、确定性 truth/analyze/publish、配置示例、
内测结果边界和回归测试。

## 暂不提交

以下内容是本地运行输出或浏览器证据，不属于源代码提交：

```text
benchmark/
tests/e2e/artifacts/page@*.webm
tests/e2e/artifacts/*.png
```

其中包括本地 benchmark manifest、pairs、persona results、summary、
publication、Playwright 录屏和测试运行生成的截图。它们不会被用于更新
Ground Truth，也不会被当作源代码或正式发布结果。

已跟踪的旧截图若因测试运行发生变化，应在提交前恢复；不要通过删除 gate
测试或忽略测试源码来让工作树“变干净”。

## 提交前检查

```bash
git status --short
git diff --check
python -m unittest tests.test_internal_feedback_benchmark
python -m unittest discover -s tests -p 'test*.py'
```

提交前确认：

- `FORMAL_MIN_PAIRS = 30`；
- 29 valid pairs 不能 publish；
- 30 valid pairs 且 coverage / Ground Truth / parity 全通过时才可 publish；
- 没有 API Key、`.env` 或本地绝对路径进入提交；
- `benchmark/`、录屏和运行截图不在提交范围。
- 受控案例测试通过 `GEO_CONTROLLED_CASE_ROOT` 指向外部私有案例目录；不把
  案例数据或本机绝对路径打进开源包。
