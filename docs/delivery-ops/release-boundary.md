# Public Alpha 发布边界

## 1. 本次发布包含

- 单机、自托管、原生 Python HTTP 服务和单文件前端；
- 本地 `work/` 文件存储、项目锁、原子 JSON 写入和 JSONL 事件日志；
- baseline → verified 七阶段、阶段回退和自动重开；
- 站点审计、AI 采样、工单、资产、审批、部署证据和多检查验收；
- 当前周期 Manifest、第 4 份 HTML 报告和脱敏客户账本；
- SaaS 与智能硬件虚构示例。
- SaaS、AI 产品和智能硬件的本地全链路验证 fixture；
- 只读 `delivery-doctor` 发布检查命令。

## 2. 明确不包含

- 多租户、账号系统、团队权限、SSO、云数据库或托管服务；
- 自动修改生产 CMS、自动发布、自动通过审批或替代人工合规判断；
- 对 AI 平台排名、提及、引用、流量、询盘或商机的保证；
- 对所有 SPA、登录墙、反爬页面或第三方平台的稳定抓取；
- 法律、监管、隐私、医疗、安全或产品认证意见；
- 将示例数据作为真实基准、客户案例或商业效果证明。

## 3. 运行与安全边界

- 支持 macOS / Linux；Windows 建议通过 WSL。项目锁依赖 `fcntl`。
- Dashboard 默认只应绑定 `127.0.0.1`。服务本身没有认证，不应直接暴露到公网。
- `.env`、`work/`、原始样本、页面快照和客户材料不得提交到公开仓库。
- API 仅允许公开 HTTP(S) 部署证据；本地文件证据只能通过本机 CLI 且必须位于项目目录内。
- 只测试你拥有或获得授权的网站、页面、账号和数据。

## 4. 数据与脱敏边界

客户账本的 `client_delivery_v1` 投影移除：

- API Key、Token、Cookie、认证头和其他凭证；
- 邮箱、本机绝对路径和内部说明；
- Prompt、AI 回答、页面正文、HTML 与抓取响应头。

脱敏是交付辅助，不替代发布前人工检查。资产目录可能包含客户主动要求交付的正文、草稿和品牌事实，
因此整个交付包仍应由项目负责人审核后再发送。

## 5. 示例边界

`examples/` 中所有公司、产品、人物、域名、事实、指标、哈希和事件均为虚构。
域名使用 RFC 2606 保留的 `.invalid` 后缀，示例不会访问真实服务。

示例用于：

- 查看 `geo.json` / `tasks.json` / baseline / events Schema；
- 体验看板和七阶段数据展示；
- 编写测试或二次开发 fixture。

示例不用于：真实抓取、部署验证、效果对标或商业宣传。

## 6. 对外发布白名单

允许公开：

```text
README.md
LICENSE
NOTICE.md
PUBLIC_ALPHA_RELEASE_CHECKLIST.md
KNOWN_LIMITATIONS.md
VALIDATION_LOG.md
docs/
examples/
scripts/delivery.py
patches/change-set-*.patch
tests/test_delivery*.py
```

禁止公开：

```text
.env*
work/
dist/ 中未经检查的压缩包
真实客户材料与截图
原始 AI 回答和手工采样表
本机绝对路径、日志、缓存和 __pycache__
```

## 7. 发布前检查

1. 从干净 GeoLook `HEAD` 顺序应用全部 Change Set。
2. 运行交付测试和 GeoLook 原生任务/交付包兼容测试。
3. 检查补丁空白、Python 编译和前端 JavaScript 语法。
4. 搜索凭证、邮箱、真实域名、本机路径、`.DS_Store` 和缓存。
5. 核对 `LICENSE` 与 `NOTICE.md` 保留上游 MIT 归属。
6. 仅按白名单创建公开发布包，不打包 `.git`、工作数据或临时目录。
7. 对每个待交付项目运行 `delivery-doctor`，任何 FAIL 都阻断发布。
