# 智能硬件示例：Aster Home Demo

这是一个完全虚构、只读的离线快照，用于演示多型号事实治理、诊断复核和范围决策。公司、产品、型号、角色、事实和事件都不对应真实主体。

- 域名使用保留的 `.invalid` 后缀，不会解析；
- 不要对本示例运行抓取、部署复查或验收；
- 示例不构成客户案例、产品安全建议、认证证明或效果基准。

查看方式：

```bash
cp -R examples/smart-hardware work/smart-hardware-demo
python3 scripts/geo.py ui
```

开始真实项目时，请先复制示例，替换域名、型号事实、角色和问题库，重新完成范围确认，然后用 `start-new-cycle` 创建新周期。不要复用示例的 `cycle_id`。
