# SaaS 示例：Northstar Desk Demo

这是一个完全虚构、只读的离线快照，用于查看 GEO Delivery Ops 的项目、基线、诊断和范围决策 Schema。公司、产品、角色、事实和事件都不对应真实主体。

- 域名使用保留的 `.invalid` 后缀，不会解析；
- 不要对本示例运行抓取、部署复查或验收；
- 示例不构成客户案例、效果基准或排名承诺。

查看方式：

```bash
cp -R examples/saas-ai-support work/saas-ai-support-demo
python3 scripts/geo.py ui
```

开始真实项目时，请先复制示例，替换域名、品牌事实、角色和问题库，重新完成范围确认，然后用 `start-new-cycle` 创建新周期。不要复用示例的 `cycle_id`。
