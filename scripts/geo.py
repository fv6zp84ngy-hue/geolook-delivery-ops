#!/usr/bin/env python3
"""GEO 自动化管线 CLI。

  python3 scripts/geo.py init --url https://example.com --name 品牌名
  python3 scripts/geo.py crawl        --slug example
  python3 scripts/geo.py audit        --slug example
  python3 scripts/geo.py sample       --slug example
  python3 scripts/geo.py sample-sheet --slug example
  python3 scripts/geo.py sample-import --slug example --file work/example/samples/2026-07-26-manual.md
  python3 scripts/geo.py report       --slug example
  python3 scripts/geo.py cycle        --slug example      # 一条命令跑完整期
  python3 scripts/geo.py list
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import geolib as G  # noqa: E402
except ModuleNotFoundError as e:
    raise SystemExit(f"缺少依赖：{e.name}。请先 pip3 install requests beautifulsoup4 lxml") from e


DEFAULT_PLATFORMS = {
    "cn": ["glm", "doubao", "deepseek", "kimi", "minimax", "nano_ai", "baidu"],
    "global": ["gemini", "openai", "claude", "grok", "perplexity", "chatgpt"],
    "both": ["glm", "doubao", "deepseek", "kimi", "minimax", "nano_ai", "baidu",
             "gemini", "openai", "claude", "grok", "perplexity", "chatgpt"],
}


def cmd_init(a):
    url = a.url.rstrip("/")
    if not url.startswith("http"):
        url = "https://" + url
    host = urlparse(url).netloc.removeprefix("www.")
    slug = a.slug or G.slugify(host.split(".")[0])

    # 已存在的项目绝不覆盖：geo.json 里有问题库、竞品、事实口径，
    # 覆盖等于把一期的人工投入清零。要重建必须显式加 --force。
    existing = G.project_dir(slug) / "geo.json"
    if existing.exists() and not getattr(a, "force", False):
        cur = G.read_json(existing, {})
        G.die(f"项目 `{slug}` 已存在（问题 {len(cur.get('questions', []))} 题、"
              f"竞品 {len(cur.get('competitors', []))} 个）。换一个 --slug，"
              f"或确认要清空后加 --force")

    name = a.name
    if not name:
        res = G.fetch(url)
        if res["html"]:
            soup = G.parse_html(res["html"])
            title = soup.title.get_text(" ", strip=True) if soup.title else ""
            name = (title.split("|")[0].split("-")[0].split("_")[0].strip() or host)[:40]
        else:
            name = host

    from version import PRODUCT_STAGE, RELEASE_LABEL, SCHEMA_VERSION, get_distribution_version

    cfg = {
        "schema_version": SCHEMA_VERSION,
        "distribution_version": get_distribution_version(),
        "product_stage": PRODUCT_STAGE,
        "release_label": RELEASE_LABEL,
        "slug": slug,
        "created_at": G.now_iso(),
        "market": a.market,
        "brand": {
            "name": name,
            "aliases": [],
            "site": url,
            "products": [],
            "industry": "",
            "target_users": "",
            "business_goal": "",
        },
        "competitors": [],
        "platforms": DEFAULT_PLATFORMS[a.market],
        "pages": {"seed": [], "max": a.max_pages},
        "questions": [],
        "materials": [],
        "targets": {"mention_rate": 0.5, "top3_rate": 0.3, "avg_page_score": 75},
        "notes": "questions / competitors / aliases 由 Claude 按 SKILL.md 步骤 2 填充",
    }
    G.save_config(slug, cfg)
    for sub in ("evidence", "samples", "metrics", "reports", "history", "content"):
        (G.project_dir(slug) / sub).mkdir(parents=True, exist_ok=True)
    print(f"[geo] 项目已创建：{G.project_dir(slug)/'geo.json'}（品牌：{name}）")
    print("[geo] 下一步：让 Claude 补全 brand/competitors/questions，再跑 crawl")
    return cfg


def cmd_bootstrap(a):
    import bootstrap

    bootstrap.run(a.slug, skip_llm=a.skip_llm)


def cmd_deliverables(a):
    import deliverables

    deliverables.run(a.slug)


def cmd_new(a):
    """只给一个网址，跑完全流程出三份交付物。"""
    import audit as A
    import blueprint as BP
    import bootstrap
    import crawl as C
    import deliver
    import deliverables as DV
    import generate
    import report as Rp
    import sample as S
    import tasks
    import verify as V

    G.info("═══ 1/9 建项目 ═══")
    cfg = cmd_init(a)
    slug = cfg["slug"]
    G.info("═══ 2/9 抓取官网 ═══")
    C.run(slug, max_pages=a.max_pages)
    G.info("═══ 3/9 体检 ═══")
    A.run(slug)
    G.info("═══ 4/9 自动推导品牌事实、竞品与问题库 ═══")
    bootstrap.run(slug, skip_llm=a.skip_llm)
    G.info("═══ 5/9 重跑体检（问题库影响对题性评分）═══")
    A.run(slug)
    G.info("═══ 6/9 AI 答案采样 ═══")
    if a.no_sample:
        G.info("跳过：--no-sample")
    elif not G.load_config(slug).get("questions"):
        G.info("跳过：问题库为空")
    else:
        try:
            S.run(slug, limit=a.limit)
        except Exception as e:  # noqa: BLE001
            G.info(f"采样跳过：{type(e).__name__}: {e}")
    G.info("═══ 7/9 工单与建设蓝图 ═══")
    tasks.build(slug)
    BP.build(slug)
    G.info("═══ 8/9 资产与报告 ═══")
    generated = generate.run(slug, with_draft=a.draft, draft_limit=a.draft_limit)
    try:
        import delivery
        delivery.on_assets_generated(slug, generated)
    except Exception as e:  # noqa: BLE001
        G.info(f"资产交付绑定跳过：{type(e).__name__}: {e}")
    Rp.run(slug)
    G.info("═══ 9/9 三份交付物 + 交付包 ═══")
    DV.run(slug)
    try:
        V.run(slug, recrawl=False)
    except Exception as e:  # noqa: BLE001
        G.info(f"验收失败：{e}")
    deliver.run(slug)
    G.info("")
    G.info(f"完成。交付物在 work/{slug}/deliverables/：")
    G.info("  1-GEO诊断报告.html   现在什么样")
    G.info("  2-GEO优化方案.html   应该改成什么样")
    G.info("  3-GEO执行方案.html   谁在什么时候做什么")
    G.info("")
    G.info("下一步：打开工作台核对自动推导的品牌事实与问题库（标「待确认」的需人工补齐）")
    G.info("  python3 scripts/geo.py ui")


def cmd_autopilot(a):
    """对已建好的项目跑完整引导：推导底座 → 采样 → 工单 → 资产 → 三份交付物。"""
    import audit as A
    import blueprint as BP
    import bootstrap
    import crawl as C
    import deliver
    import deliverables as DV
    import generate
    import report as Rp
    import sample as S
    import tasks
    import verify as V

    cfg = G.load_config(a.slug)
    G.info("═══ 1/8 抓取官网 ═══")
    C.run(a.slug)
    G.info("═══ 2/8 体检 ═══")
    A.run(a.slug)
    if not cfg.get("questions"):
        G.info("═══ 3/8 自动推导品牌事实、竞品与问题库 ═══")
        bootstrap.run(a.slug, skip_llm=a.skip_llm)
        A.run(a.slug)
    else:
        G.info("═══ 3/8 已有问题库，跳过自动推导 ═══")
    G.info("═══ 4/8 AI 答案采样 ═══")
    if a.no_sample:
        G.info("跳过：--no-sample")
    elif G.load_config(a.slug).get("questions"):
        try:
            S.run(a.slug, limit=a.limit)
        except Exception as e:  # noqa: BLE001
            G.info(f"采样跳过：{type(e).__name__}: {e}")
    G.info("═══ 5/8 工单与建设蓝图 ═══")
    tasks.build(a.slug)
    BP.build(a.slug)
    G.info("═══ 6/8 资产与报告 ═══")
    generate.run(a.slug)
    Rp.run(a.slug)
    G.info("═══ 7/8 三份交付物 ═══")
    DV.run(a.slug)
    G.info("═══ 8/8 验收与打包 ═══")
    try:
        V.run(a.slug, recrawl=False)
    except Exception as e:  # noqa: BLE001
        G.info(f"验收失败：{e}")
    deliver.run(a.slug)
    G.info("完成。三份交付物在 deliverables/，标「待确认」的品牌事实需人工补齐。")


def cmd_crawl(a):
    import crawl

    crawl.run(a.slug, max_pages=a.max_pages)


def cmd_audit(a):
    import audit

    audit.run(a.slug)


def cmd_sample(a):
    import sample

    sample.run(a.slug, platforms=a.platforms.split(",") if a.platforms else None,
               repeat=a.repeat, limit=a.limit)


def cmd_sheet(a):
    import sample

    sample.sheet(a.slug)


def cmd_import(a):
    import sample

    sample.sample_import(a.slug, a.file)


def cmd_signal_import(a):
    import signals

    result = signals.import_csv(a.slug, a.file, source_type=a.source_type)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_delivery_export(a):
    import export_delivery

    result = export_delivery.write_project_export(a.slug, a.format, a.file)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_github_issue(a):
    import integrations

    result = integrations.sync_github_issue(
        a.slug, a.task_id,
        config={"repo": a.repo, "token": a.token, "timeout": a.timeout} if a.repo or a.token else None,
        dry_run=a.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_github_refresh(a):
    import integrations

    result = integrations.refresh_github_issue(a.slug, a.task_id, a.external_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_delivery_webhook(a):
    import integrations
    import tasks

    data = tasks.load(a.slug)
    task = next((row for row in data.get("tasks", []) if str(row.get("id")) == str(a.task_id)), None)
    if task is None:
        raise KeyError(f"找不到工单 {a.task_id}")
    result = integrations.send_webhook(a.slug, a.event, task, {"url": a.url, "timeout": a.timeout})
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_report(a):
    import report

    report.run(a.slug)


def cmd_version(a):
    import release
    result = release.version_info()
    print(json.dumps(result, ensure_ascii=False, indent=2) if getattr(a, "json", False) else f"GEO Delivery Ops {result['release_label']} {result['distribution_version']} ({result['product_stage']}) schema {result['schema_version']}")


def cmd_migrate(a):
    import release
    print(json.dumps(release.migrate_project(a.slug, dry_run=a.dry_run), ensure_ascii=False, indent=2))


def cmd_rollback(a):
    import release
    print(json.dumps(release.rollback_project(a.slug, a.backup_id), ensure_ascii=False, indent=2))


def cmd_doctor(a):
    import release
    result = release.doctor_all(slug=a.slug or None)
    print(json.dumps(result, ensure_ascii=False, indent=2) if a.json else release.format_doctor(result))
    if not result["ok"]:
        raise SystemExit(1)


def cmd_export_backup(a):
    import release
    print(json.dumps(release.export_backup(a.slug, a.file or None), ensure_ascii=False, indent=2))


def cmd_rc_init(a):
    import rc
    print(json.dumps(rc.init_project(a.root, a.slug, industry=a.industry, force=a.force), ensure_ascii=False, indent=2))


def cmd_rc_demo(a):
    import rc
    print(json.dumps(rc.demo_project(a.root, a.slug, industry=a.industry, force=a.force), ensure_ascii=False, indent=2))


def cmd_rc_wizard(a):
    import rc
    print(json.dumps(rc.wizard(a.root, a.slug, a.industry), ensure_ascii=False, indent=2))


def cmd_cycle(a):
    import audit
    import crawl
    import report
    import sample

    G.info("=== 1/4 抓取 ===")
    crawl.run(a.slug, max_pages=a.max_pages)
    G.info("=== 2/4 体检 ===")
    audit.run(a.slug)
    G.info("=== 3/4 采样 ===")
    # 采样失败不能把整期带崩：报告和待办比采样更重要
    if not G.load_config(a.slug).get("questions"):
        G.info("跳过采样：geo.json 里还没有问题库（见 SKILL.md 步骤 2）")
    else:
        try:
            sample.run(a.slug, limit=a.limit)
        except Exception as e:  # noqa: BLE001
            G.info(f"采样跳过：{type(e).__name__}: {e}")
    G.info("=== 4/4 报告 ===")
    report.run(a.slug)


def cmd_expand(a):
    import expand
    expand.run(a.slug, use_llm=not a.no_llm)


def cmd_plan(a):
    import tasks

    tasks.build(a.slug)


def cmd_delivery_sync(a):
    import delivery

    data = delivery.delivery_sync(a.slug)
    migration = data["delivery_migration"]
    G.info(f"交付 Schema 已同步：周期 {migration['cycle_id']}，"
           f"工单 {migration['normalized_tasks']} 条")
    print(json.dumps(migration, ensure_ascii=False, indent=2))


def cmd_delivery_show(a):
    import delivery

    print(json.dumps(delivery.delivery_view(a.slug), ensure_ascii=False, indent=2))


def cmd_delivery_events(a):
    import delivery

    cfg = delivery.normalize_project_config(G.load_config(a.slug))
    cycle_id = a.cycle_id or cfg["delivery"].get("current_cycle_id")
    if not cycle_id:
        raise ValueError("当前没有交付周期，请通过 --cycle-id 指定历史周期")
    result = delivery.read_cycle_event_log(G.project_dir(a.slug), cycle_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_delivery_doctor(a):
    import delivery

    result = delivery.delivery_doctor(a.slug)
    if a.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(delivery.format_doctor_report(result))
    if not result.get("ok"):
        raise SystemExit(1)


def cmd_feedback_benchmark(a):
    import internal_feedback_benchmark as B

    if a.feedback_command == "prepare":
        cases = []
        for value in a.case:
            if "=" not in value:
                raise ValueError("--case 格式必须是 case_id=/path/to/controlled-case")
            case_id, path = value.split("=", 1)
            cases.append((case_id, path))
        result = B.prepare(
            cases,
            a.output,
            cohort_id=a.cohort_id,
            persona_id=a.persona_id,
            persona_model=a.persona_model,
            seed=a.seed,
            dummy=not a.no_dummy,
            persona_profiles=a.persona_profiles,
        )
    elif a.feedback_command == "analyze":
        result = B.analyze(a.manifest, a.results, a.output)
    elif a.feedback_command == "run":
        result = B.run_trials(a.manifest, a.runner, a.output, timeout=a.timeout)
    else:
        result = B.publish(a.summary, a.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_delivery_baseline(a):
    import delivery

    confirmation = delivery.record_scope_confirmation(
        a.slug,
        selected_problem_ids=a.problem_ids,
        pending_fact_ids=a.pending_fact_ids,
        note=a.note,
    )
    snapshot = delivery.create_cycle(a.slug)
    result = {
        "schema_version": snapshot["schema_version"],
        "cycle_id": snapshot["cycle_id"],
        "baseline_ref": f"delivery/snapshots/{snapshot['cycle_id']}/baseline.json",
        "scope_sha256": confirmation["scope_sha256"],
        "created_at": snapshot["created_at"],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_delivery_cycle_end(a):
    import delivery

    result = delivery.end_cycle(
        a.slug, a.note, role="project_owner", allow_open=a.allow_open,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_delivery_cycle_start(a):
    import delivery

    snapshot = delivery.start_new_cycle(a.slug)
    print(json.dumps({
        "cycle_id": snapshot["cycle_id"],
        "baseline_ref": f"delivery/snapshots/{snapshot['cycle_id']}/baseline.json",
        "created_at": snapshot["created_at"],
    }, ensure_ascii=False, indent=2))


def cmd_delivery_diagnose(a):
    import delivery

    data = delivery.diagnose_tasks(a.slug)
    print(json.dumps(data["diagnosis"], ensure_ascii=False, indent=2))


def cmd_delivery_add_evidence(a):
    import delivery

    ref = delivery.add_manual_source_ref(
        a.slug, a.task_id, a.label, a.note,
        ref=a.ref, role=a.role, confidence=a.confidence,
    )
    print(json.dumps(ref, ensure_ascii=False, indent=2))


def cmd_delivery_review_evidence(a):
    import delivery

    task = delivery.review_source_ref(
        a.slug, a.task_id, a.evidence_id, a.status, a.note, role=a.role,
    )
    print(json.dumps({
        "task_id": task["id"],
        "stage": task["delivery"]["stage"],
        "diagnosis": task["delivery"]["diagnosis"],
    }, ensure_ascii=False, indent=2))


def cmd_delivery_scope_suggest(a):
    import delivery

    data = delivery.recommend_scope(a.slug)
    print(json.dumps(data["scope_recommendation"], ensure_ascii=False, indent=2))


def cmd_delivery_scope_decide(a):
    import delivery

    task = delivery.record_scope_decision(
        a.slug, a.task_id, a.status, a.reason,
    )
    print(json.dumps({
        "task_id": task["id"],
        "stage": task["delivery"]["stage"],
        "scope_decision": task["delivery"]["scope_decision"],
    }, ensure_ascii=False, indent=2))


def cmd_delivery_scope_status(a):
    import delivery

    print(json.dumps(delivery.scope_status(a.slug), ensure_ascii=False, indent=2))


def cmd_delivery_assign_prepare(a):
    import delivery

    data = delivery.prepare_assignments(a.slug)
    print(json.dumps(data["assignment_plan"], ensure_ascii=False, indent=2))


def cmd_delivery_assign_confirm(a):
    import delivery

    task = delivery.confirm_assignment(
        a.slug, a.task_id, a.status, a.note, role=a.role,
    )
    print(json.dumps({
        "task_id": task["id"],
        "status": task["status"],
        "stage": task["delivery"]["stage"],
        "assignment": task["delivery"]["assignment"],
    }, ensure_ascii=False, indent=2))


def cmd_delivery_assign_status(a):
    import delivery

    print(json.dumps(delivery.assignment_status(a.slug), ensure_ascii=False, indent=2))


def cmd_delivery_assets_prepare(a):
    import delivery

    data = delivery.scan_assets(a.slug)
    print(json.dumps(data["asset_review"], ensure_ascii=False, indent=2))


def cmd_delivery_asset_bind(a):
    import delivery

    task = delivery.bind_asset(a.slug, a.task_id, a.path, required=not a.optional)
    print(json.dumps({
        "task_id": task["id"],
        "stage": task["delivery"]["stage"],
        "assets": task["delivery"]["assets"],
    }, ensure_ascii=False, indent=2))


def cmd_delivery_asset_approve(a):
    import delivery

    result = delivery.add_asset_approval(
        a.slug, a.task_id, a.asset_id, a.status, a.role, a.note,
        requirement_id=a.requirement_id,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_delivery_assets_status(a):
    import delivery

    print(json.dumps(delivery.asset_status(a.slug), ensure_ascii=False, indent=2))


def cmd_delivery_deploy_prepare(a):
    import delivery

    data = delivery.prepare_deployments(a.slug)
    print(json.dumps(data["deployment_review"], ensure_ascii=False, indent=2))


def cmd_delivery_deploy_submit(a):
    import delivery

    result = delivery.add_deployment(
        a.slug, a.task_id, a.target_url, a.asset_ids, a.role, a.note,
        channel=a.channel,
        evidence_type=a.evidence_type,
        evidence_ref=a.evidence_ref,
        insertion_position=a.insertion_position,
        expected_snippets=a.expected_snippets,
        expected_jsonld_types=a.jsonld_types,
        request_id=a.request_id,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_delivery_deploy_check(a):
    import delivery

    result = delivery.recheck_deployment(a.slug, a.task_id, a.deployment_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_delivery_deploy_status(a):
    import delivery

    print(json.dumps(delivery.deployment_status(a.slug), ensure_ascii=False, indent=2))


def cmd_delivery_verify_review(a):
    import delivery

    result = delivery.record_verification_review(
        a.slug, a.task_id, a.status, a.note, role="reviewer",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_blueprint(a):
    import blueprint

    blueprint.build(a.slug)


def cmd_generate(a):
    import generate

    generated = generate.run(a.slug, which=a.asset.split(",") if a.asset else None,
                             with_draft=a.draft, draft_limit=a.draft_limit)
    try:
        import delivery
        delivery.on_assets_generated(a.slug, generated)
    except Exception as e:  # noqa: BLE001
        G.info(f"资产交付绑定跳过：{type(e).__name__}: {e}")


def cmd_lint(a):
    import generate

    rep = generate.lint_all(a.slug)
    if not rep["files"]:
        print("没有 AI 初稿可检查（用 generate --draft 生成）")
        return
    print(f"\n检查 {len(rep['files'])} 份初稿，共 {rep['total_issues']} 项待核实（高风险 {rep['high']} 项）")
    for fn, issues in rep["files"].items():
        if not issues:
            print(f"\n  {fn}：无风险")
            continue
        print(f"\n  {fn}")
        for i in issues:
            print(f"    [{i['level']}] {i['type']}：{i['detail']}")
            print(f"          …{i['excerpt'][:76]}")
    print("\n高风险项必须处理后才能发布；未核实数字需补来源与核验日期。\n")


def cmd_verify(a):
    import verify

    verify.run(a.slug, recrawl=not a.no_recrawl, task_id=a.task_id)


def cmd_deliver(a):
    import deliver

    deliver.run(a.slug)


def cmd_publish(a):
    import publish

    r = publish.publish(a.slug, a.platform, a.path, a.title or "")
    if r.get("ok"):
        G.info(f"已发布：{r.get('url') or r.get('note') or 'ok'}")
    else:
        G.die(f"发布失败：{r.get('error')}")


def cmd_task(a):
    import tasks

    if a.status:
        try:
            tasks.set_status(a.slug, a.id, a.status, a.note or "")
        except KeyError as e:
            G.die(e.args[0] if e.args else str(e))
    else:
        data = tasks.load(a.slug)
        t = next((x for x in data["tasks"] if x["id"] == a.id), None)
        if not t:
            G.die(f"找不到工单 {a.id}")
        print(json.dumps(t, ensure_ascii=False, indent=2))


def cmd_status(a):
    import tasks

    cfg = G.load_config(a.slug)
    audit = G.read_json(G.project_dir(a.slug) / "audit.json", {})
    data = tasks.load(a.slug)
    s = data.get("summary", {})
    print(f"\n{cfg['brand']['name']}  ({cfg.get('market')})  {cfg['brand']['site']}")
    print(f"  站点均分 {audit.get('avg_score', '—')}  页面 {audit.get('page_count', '—')}"
          f"  工单 {s.get('total', 0)} 条（可自动验收 {s.get('auto_verifiable', 0)}）")
    if not data.get("tasks"):
        print("  还没有工单，运行 plan 生成\n")
        return
    order = {"P0": 0, "P1": 1, "P2": 2}
    for pri in ("P0", "P1", "P2"):
        rows = [t for t in data["tasks"] if t["priority"] == pri]
        if not rows:
            continue
        done = sum(1 for t in rows if t["status"] == "done")
        print(f"\n  {pri}  {done}/{len(rows)} 完成")
        for t in sorted(rows, key=lambda x: (x["status"] != "todo", x["package"])):
            mark = {"done": "✓", "doing": "◐", "blocked": "✗", "wontfix": "—"}.get(t["status"], "·")
            print(f"    {mark} {t['id']} [{t['package']}/{t['owner']}/{t['market']}] {t['title']}")
    print()


def cmd_serve(a):
    """一条命令跑完整个服务周期：诊断 → 方案 → 资产 → 验收 → 交付。"""
    import audit as A
    import crawl as C
    import deliver
    import generate
    import report as Rp
    import sample as S
    import tasks
    import verify as V

    G.info("═══ 1/7 抓取 ═══")
    C.run(a.slug, max_pages=a.max_pages)
    G.info("═══ 2/7 体检 ═══")
    A.run(a.slug)
    G.info("═══ 3/7 AI 答案采样 ═══")
    if not G.load_config(a.slug).get("questions"):
        G.info("跳过：问题库为空（见 SKILL.md 步骤 2）")
    elif a.no_sample:
        G.info("跳过：--no-sample")
    else:
        try:
            S.run(a.slug, limit=a.limit)
        except Exception as e:  # noqa: BLE001
            G.info(f"采样跳过：{type(e).__name__}: {e}")
    try:
        import expand
        expand.run(a.slug)
    except Exception as e:  # noqa: BLE001
        G.info(f"拓词跳过：{type(e).__name__}: {e}")
    G.info("═══ 4/7 生成工单与建设蓝图 ═══")
    tasks.build(a.slug)
    import blueprint
    blueprint.build(a.slug)
    G.info("═══ 5/7 生成资产 ═══")
    generated = generate.run(a.slug, with_draft=a.draft, draft_limit=a.draft_limit)
    try:
        import delivery
        delivery.on_assets_generated(a.slug, generated)
    except Exception as e:  # noqa: BLE001
        G.info(f"资产交付绑定跳过：{type(e).__name__}: {e}")
    G.info("═══ 6/7 报告 ═══")
    Rp.run(a.slug)
    G.info("═══ 7/7 验收上期工单 ═══")
    V.run(a.slug, recrawl=False)
    G.info("═══ 打包交付 ═══")
    deliver.run(a.slug)


def cmd_ui(a):
    import dashboard

    dashboard.run(port=a.port, open_browser=not a.no_open)


def cmd_list(a):
    if not G.WORK.exists():
        print("还没有任何项目")
        return
    for d in sorted(G.WORK.iterdir()):
        cfg_path = d / "geo.json"
        if cfg_path.exists():
            cfg = G.read_json(cfg_path, {})
            reports = sorted((d / "reports").glob("2*")) if (d / "reports").exists() else []
            last = reports[-1].name if reports else "—"
            print(f"{d.name:20s} {cfg.get('brand', {}).get('name', ''):22s} 问题 {len(cfg.get('questions', [])):3d}  最近报告 {last}")


def main():
    p = argparse.ArgumentParser(prog="geo", description="GEO 自动化管线")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="新建项目")
    s.add_argument("--url", required=True)
    s.add_argument("--name")
    s.add_argument("--slug")
    s.add_argument("--market", choices=["cn", "global", "both"], default="cn")
    s.add_argument("--max-pages", type=int, default=25, dest="max_pages")
    s.add_argument("--force", action="store_true", help="项目已存在时清空重建（危险）")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("new", help="★ 只给一个网址，全自动出三份交付物")
    s.add_argument("--url", required=True)
    s.add_argument("--name")
    s.add_argument("--slug")
    s.add_argument("--market", choices=["cn", "global", "both"], default="both")
    s.add_argument("--max-pages", type=int, default=25, dest="max_pages")
    s.add_argument("--limit", type=int, default=None, help="采样只跑前 N 题")
    s.add_argument("--no-sample", action="store_true", dest="no_sample")
    s.add_argument("--skip-llm", action="store_true", dest="skip_llm", help="不用 LLM 推导底座")
    s.add_argument("--draft", action="store_true")
    s.add_argument("--draft-limit", type=int, default=3, dest="draft_limit")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_new)

    s = sub.add_parser("autopilot", help="对已有项目跑完整引导流程")
    s.add_argument("--slug", required=True)
    s.add_argument("--limit", type=int, default=None)
    s.add_argument("--no-sample", action="store_true", dest="no_sample")
    s.add_argument("--skip-llm", action="store_true", dest="skip_llm")
    s.set_defaults(func=cmd_autopilot)

    s = sub.add_parser("bootstrap", help="从官网正文自动推导品牌事实、竞品与问题库")
    s.add_argument("--slug", required=True)
    s.add_argument("--skip-llm", action="store_true", dest="skip_llm")
    s.set_defaults(func=cmd_bootstrap)

    s = sub.add_parser("deliverables", help="出三份正式交付物（诊断/优化/执行）")
    s.add_argument("--slug", required=True)
    s.set_defaults(func=cmd_deliverables)

    s = sub.add_parser("crawl", help="抓取官网")
    s.add_argument("--slug", required=True)
    s.add_argument("--max-pages", type=int, default=None, dest="max_pages")
    s.set_defaults(func=cmd_crawl)

    s = sub.add_parser("audit", help="页面 GEO 体检")
    s.add_argument("--slug", required=True)
    s.set_defaults(func=cmd_audit)

    s = sub.add_parser("sample", help="API 平台答案采样")
    s.add_argument("--slug", required=True)
    s.add_argument("--platforms", help="逗号分隔，默认取 geo.json 里有 Key 的")
    s.add_argument("--repeat", type=int, default=1, help="每题重复采样次数")
    s.add_argument("--limit", type=int, default=None, help="只跑前 N 个问题")
    s.set_defaults(func=cmd_sample)

    s = sub.add_parser("sample-sheet", help="导出人工/浏览器采样表")
    s.add_argument("--slug", required=True)
    s.set_defaults(func=cmd_sheet)

    s = sub.add_parser("sample-import", help="导入人工采样表")
    s.add_argument("--slug", required=True)
    s.add_argument("--file", required=True)
    s.set_defaults(func=cmd_import)

    s = sub.add_parser("signal-import", help="导入显式业务信号 CSV（仅观察，不做收入归因）")
    s.add_argument("--slug", required=True)
    s.add_argument("--type", dest="source_type", choices=["ga4", "gsc", "crm", "forms", "sales_manual", "sales_ai_report"])
    s.add_argument("--file", required=True)
    s.set_defaults(func=cmd_signal_import)

    s = sub.add_parser("delivery-export", help="导出当前工单执行副本（不改变内部真相）")
    s.add_argument("--slug", required=True)
    s.add_argument("--format", choices=["csv", "markdown", "json", "github", "notion_csv", "jira_csv"], default="csv")
    s.add_argument("--file", default="")
    s.set_defaults(func=cmd_delivery_export)

    s = sub.add_parser("github-issue", help="单向创建 GitHub Issue 并保存 external_ref")
    s.add_argument("--slug", required=True)
    s.add_argument("--task-id", required=True)
    s.add_argument("--repo", default="")
    s.add_argument("--token", default="")
    s.add_argument("--timeout", type=int, default=15)
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_github_issue)

    s = sub.add_parser("github-refresh", help="手动刷新 GitHub Issue 状态（不改内部状态）")
    s.add_argument("--slug", required=True)
    s.add_argument("--task-id", required=True)
    s.add_argument("--external-id", required=True)
    s.set_defaults(func=cmd_github_refresh)

    s = sub.add_parser("delivery-webhook", help="显式发送脱敏工单事件 Webhook")
    s.add_argument("--slug", required=True)
    s.add_argument("--task-id", required=True)
    s.add_argument("--event", choices=["task_approved", "task_assigned", "asset_pending_approval", "deployment_pending", "verification_failed", "regression"], required=True)
    s.add_argument("--url", required=True)
    s.add_argument("--timeout", type=int, default=10)
    s.set_defaults(func=cmd_delivery_webhook)

    s = sub.add_parser("report", help="生成报告")
    s.add_argument("--slug", required=True)
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("version", help="显示 Public Alpha RC 版本和 Schema")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_version)

    s = sub.add_parser("migrate", help="显式升级项目 Schema，并创建迁移前备份")
    s.add_argument("--slug", required=True)
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_migrate)

    s = sub.add_parser("rollback", help="从命名迁移备份恢复项目 JSON")
    s.add_argument("--slug", required=True)
    s.add_argument("--backup-id", required=True)
    s.set_defaults(func=cmd_rollback)

    s = sub.add_parser("doctor", help="检查一个或全部项目的发布阻断项")
    s.add_argument("--slug", default="")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("export-backup", help="导出不含凭证的可恢复项目 ZIP")
    s.add_argument("--slug", required=True)
    s.add_argument("--file", default="")
    s.set_defaults(func=cmd_export_backup)

    s = sub.add_parser("rc-init", help="初始化本地 Release Candidate 项目")
    s.add_argument("--root", default=".")
    s.add_argument("--slug", required=True)
    s.add_argument("--industry", choices=["saas", "ai", "smart_hardware", "other"], default="saas")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_rc_init)

    s = sub.add_parser("rc-demo", help="创建固定的离线 Release Candidate Demo")
    s.add_argument("--root", default=".")
    s.add_argument("--slug", default="geo-delivery-demo")
    s.add_argument("--industry", choices=["saas", "ai", "smart_hardware", "other"], default="saas")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_rc_demo)

    s = sub.add_parser("demo", help="创建固定的离线 Release Candidate Demo")
    s.add_argument("--root", default=".")
    s.add_argument("--slug", default="geo-delivery-demo")
    s.add_argument("--industry", choices=["saas", "ai", "smart_hardware", "other"], default="saas")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_rc_demo)

    s = sub.add_parser("rc-wizard", help="交互式初始化本地 Release Candidate 项目")
    s.add_argument("--root", default=".")
    s.add_argument("--slug", default="")
    s.add_argument("--industry", default="")
    s.set_defaults(func=cmd_rc_wizard)

    s = sub.add_parser("wizard", help="交互式初始化本地 Release Candidate 项目")
    s.add_argument("--root", default=".")
    s.add_argument("--slug", default="")
    s.add_argument("--industry", default="")
    s.set_defaults(func=cmd_rc_wizard)

    s = sub.add_parser("cycle", help="抓取→体检→采样→报告 一次跑完")
    s.add_argument("--slug", required=True)
    s.add_argument("--max-pages", type=int, default=None, dest="max_pages")
    s.add_argument("--limit", type=int, default=None)
    s.set_defaults(func=cmd_cycle)

    s = sub.add_parser("expand", help="拓词：百度下拉/Google suggest 扩出真实需求候选题")
    s.add_argument("--slug", required=True)
    s.add_argument("--no-llm", action="store_true", dest="no_llm",
                   help="不调 LLM 转写问句，用模板兜底")
    s.set_defaults(func=cmd_expand)

    s = sub.add_parser("plan", help="诊断结果 → 结构化工单（含验收标准）")
    s.add_argument("--slug", required=True)
    s.set_defaults(func=cmd_plan)

    s = sub.add_parser("delivery-sync", help="显式写回交付 Schema（旧项目懒迁移）")
    s.add_argument("--slug", required=True)
    s.set_defaults(func=cmd_delivery_sync)

    s = sub.add_parser("delivery-show", help="读取当前周期完整交付数据")
    s.add_argument("--slug", required=True)
    s.set_defaults(func=cmd_delivery_show)

    s = sub.add_parser("delivery-events", help="读取当前或历史周期 JSONL 事件")
    s.add_argument("--slug", required=True)
    s.add_argument("--cycle-id", default="")
    s.set_defaults(func=cmd_delivery_events)

    s = sub.add_parser("delivery-doctor", help="发布前检查配置、证据链、报告脱敏和路径安全")
    s.add_argument("--slug", required=True)
    s.add_argument("--json", action="store_true", help="输出结构化 JSON；存在 FAIL 时仍返回退出码 1")
    s.set_defaults(func=cmd_delivery_doctor)

    s = sub.add_parser("feedback-benchmark", help="准备、分析和发布内测用户反馈 benchmark")
    feedback = s.add_subparsers(dest="feedback_command", required=True)
    fb = feedback.add_parser("prepare", help="冻结案例、确定性真值和 paired stimuli")
    fb.add_argument("--case", action="append", required=True, help="case_id=/path/to/controlled-case")
    fb.add_argument("--output", required=True)
    fb.add_argument("--cohort-id", default="p0-local-smoke")
    fb.add_argument("--persona-id", default="operator-01")
    fb.add_argument("--persona-model", default="dummy-local")
    fb.add_argument("--persona-profile", action="append", dest="persona_profiles", help="固定 cohort persona id；可重复传入")
    fb.add_argument("--seed", type=int, default=0)
    fb.add_argument("--no-dummy", action="store_true")
    fb.set_defaults(func=cmd_feedback_benchmark)
    fb = feedback.add_parser("analyze", help="验证 paired trials 并计算确定性指标")
    fb.add_argument("--manifest", required=True)
    fb.add_argument("--results")
    fb.add_argument("--output")
    fb.set_defaults(func=cmd_feedback_benchmark)
    fb = feedback.add_parser("run", help="调用外部固定 persona runner 生成 baseline/treatment 结果")
    fb.add_argument("--manifest", required=True)
    fb.add_argument("--runner", required=True, help="JSON stdin/stdout runner 命令")
    fb.add_argument("--output")
    fb.add_argument("--timeout", type=float, default=120.0)
    fb.set_defaults(func=cmd_feedback_benchmark)
    fb = feedback.add_parser("publish", help="只发布已冻结的 summary 投影")
    fb.add_argument("--summary", required=True)
    fb.add_argument("--output")
    fb.set_defaults(func=cmd_feedback_benchmark)

    s = sub.add_parser("delivery-baseline", help="Project Owner 确认范围并锁定基线")
    s.add_argument("--slug", required=True)
    s.add_argument("--problem-id", action="append", required=True, dest="problem_ids")
    s.add_argument("--pending-fact-id", action="append", default=[], dest="pending_fact_ids")
    s.add_argument("--note", default="")
    s.set_defaults(func=cmd_delivery_baseline)

    s = sub.add_parser("delivery-cycle-end", help="Project Owner 显式结束当前交付周期")
    s.add_argument("--slug", required=True)
    s.add_argument("--note", required=True)
    s.add_argument("--allow-open", action="store_true", help="允许带未关闭工单结束并保留在原周期")
    s.set_defaults(func=cmd_delivery_cycle_end)

    s = sub.add_parser("delivery-cycle-start", help="基于当前已确认范围启动新的交付周期")
    s.add_argument("--slug", required=True)
    s.set_defaults(func=cmd_delivery_cycle_start)

    s = sub.add_parser("delivery-diagnose", help="给现有工单绑定诊断证据")
    s.add_argument("--slug", required=True)
    s.set_defaults(func=cmd_delivery_diagnose)

    s = sub.add_parser("delivery-add-evidence", help="给诊断补人工证据")
    s.add_argument("--slug", required=True)
    s.add_argument("--task-id", required=True)
    s.add_argument("--label", required=True)
    s.add_argument("--note", required=True)
    s.add_argument("--ref", default="")
    s.add_argument("--role", choices=["geo_operator", "reviewer"], default="geo_operator")
    s.add_argument("--confidence", choices=["high", "medium", "low"], default="medium")
    s.set_defaults(func=cmd_delivery_add_evidence)

    s = sub.add_parser("delivery-review-evidence", help="确认或排除诊断证据")
    s.add_argument("--slug", required=True)
    s.add_argument("--task-id", required=True)
    s.add_argument("--evidence-id", required=True)
    s.add_argument("--status", choices=["confirmed", "rejected"], required=True)
    s.add_argument("--role", choices=["geo_operator", "reviewer"], default="geo_operator")
    s.add_argument("--note", required=True)
    s.set_defaults(func=cmd_delivery_review_evidence)

    s = sub.add_parser("delivery-scope-suggest", help="生成有限且可解释的周期范围建议")
    s.add_argument("--slug", required=True)
    s.set_defaults(func=cmd_delivery_scope_suggest)

    s = sub.add_parser("delivery-scope-decide", help="Project Owner 对候选诊断作范围决策")
    s.add_argument("--slug", required=True)
    s.add_argument("--task-id", required=True)
    s.add_argument("--status", choices=["approved", "deferred", "rejected", "needs_evidence"], required=True)
    s.add_argument("--reason", required=True)
    s.set_defaults(func=cmd_delivery_scope_decide)

    s = sub.add_parser("delivery-scope-status", help="查看已批准范围和逐条决策完成度")
    s.add_argument("--slug", required=True)
    s.set_defaults(func=cmd_delivery_scope_status)

    s = sub.add_parser("delivery-assign-prepare", help="补齐已批准工单的执行规格")
    s.add_argument("--slug", required=True)
    s.set_defaults(func=cmd_delivery_assign_prepare)

    s = sub.add_parser("delivery-assign-confirm", help="负责人确认可执行或标记阻塞")
    s.add_argument("--slug", required=True)
    s.add_argument("--task-id", required=True)
    s.add_argument("--status", choices=["confirmed", "blocked"], required=True)
    s.add_argument("--role", default="", help="默认使用工单映射出的负责人角色")
    s.add_argument("--note", required=True)
    s.set_defaults(func=cmd_delivery_assign_confirm)

    s = sub.add_parser("delivery-assign-status", help="查看可分派、待确认和阻塞工单")
    s.add_argument("--slug", required=True)
    s.set_defaults(func=cmd_delivery_assign_status)

    s = sub.add_parser("delivery-assets-prepare", help="扫描并绑定 GeoLook 已生成资产")
    s.add_argument("--slug", required=True)
    s.set_defaults(func=cmd_delivery_assets_prepare)

    s = sub.add_parser("delivery-asset-bind", help="人工绑定 assets/ 下的现有文件")
    s.add_argument("--slug", required=True)
    s.add_argument("--task-id", required=True)
    s.add_argument("--path", required=True)
    s.add_argument("--optional", action="store_true")
    s.set_defaults(func=cmd_delivery_asset_bind)

    s = sub.add_parser("delivery-asset-approve", help="按角色批准或驳回当前资产版本")
    s.add_argument("--slug", required=True)
    s.add_argument("--task-id", required=True)
    s.add_argument("--asset-id", required=True)
    s.add_argument("--status", choices=["approved", "rejected"], required=True)
    s.add_argument("--role", choices=["fact_approver", "content_owner", "reviewer", "web_owner"], required=True)
    s.add_argument("--requirement-id", default="", choices=["", "fact", "content", "technical"])
    s.add_argument("--note", required=True)
    s.set_defaults(func=cmd_delivery_asset_approve)

    s = sub.add_parser("delivery-assets-status", help="查看资产存在性、预检和审批完成度")
    s.add_argument("--slug", required=True)
    s.set_defaults(func=cmd_delivery_assets_status)

    s = sub.add_parser("delivery-deploy-prepare", help="为已批准资产生成部署清单")
    s.add_argument("--slug", required=True)
    s.set_defaults(func=cmd_delivery_deploy_prepare)

    s = sub.add_parser("delivery-deploy-submit", help="Web Owner 提交部署 URL 并立即重抓")
    s.add_argument("--slug", required=True)
    s.add_argument("--task-id", required=True)
    s.add_argument("--target-url", required=True)
    s.add_argument("--asset-id", action="append", required=True, dest="asset_ids")
    s.add_argument("--role", required=True, choices=["web_owner"])
    s.add_argument("--channel", default="website", choices=["website", "github", "wordpress", "wechat", "external_platform", "other"])
    s.add_argument("--evidence-type", default="url", choices=["url", "file", "screenshot", "manual"])
    s.add_argument("--evidence-ref", default="")
    s.add_argument("--insertion-position", default="")
    s.add_argument("--expected-snippet", action="append", dest="expected_snippets")
    s.add_argument("--jsonld-type", action="append", dest="jsonld_types")
    s.add_argument("--request-id", default="", help="部署提交幂等键；重试时复用同一个值")
    s.add_argument("--note", required=True)
    s.set_defaults(func=cmd_delivery_deploy_submit)

    s = sub.add_parser("delivery-deploy-check", help="重抓已提交部署 URL")
    s.add_argument("--slug", required=True)
    s.add_argument("--task-id", required=True)
    s.add_argument("--deployment-id", required=True)
    s.set_defaults(func=cmd_delivery_deploy_check)

    s = sub.add_parser("delivery-deploy-status", help="查看部署提交、失败与人工证据状态")
    s.add_argument("--slug", required=True)
    s.set_defaults(func=cmd_delivery_deploy_status)

    s = sub.add_parser("delivery-verify-review", help="Reviewer 对当前验收版本给出明确结论")
    s.add_argument("--slug", required=True)
    s.add_argument("--task-id", required=True)
    s.add_argument("--status", choices=["approved", "rejected"], required=True)
    s.add_argument("--note", required=True)
    s.set_defaults(func=cmd_delivery_verify_review)

    s = sub.add_parser("blueprint", help="GEO 建设蓝图：在哪些平台建、建什么内容、覆盖度多少")
    s.add_argument("--slug", required=True)
    s.set_defaults(func=cmd_blueprint)

    s = sub.add_parser("generate", help="产出可直接部署的资产（llms.txt/JSON-LD/片段/大纲）")
    s.add_argument("--slug", required=True)
    s.add_argument("--asset", help="逗号分隔：llms,jsonld,snippets,outlines")
    s.add_argument("--draft", action="store_true", help="额外调用 LLM 出文章初稿")
    s.add_argument("--draft-limit", type=int, default=3, dest="draft_limit")
    s.set_defaults(func=cmd_generate)

    s = sub.add_parser("lint", help="检查 AI 初稿的编造风险（发布/交付前必跑）")
    s.add_argument("--slug", required=True)
    s.set_defaults(func=cmd_lint)

    s = sub.add_parser("verify", help="重抓并自动验收工单")
    s.add_argument("--slug", required=True)
    s.add_argument("--task-id", default=None, help="仅验收指定工单；默认全量")
    s.add_argument("--no-recrawl", action="store_true", dest="no_recrawl",
                   help="用现有 audit 结果验收，不重新抓站")
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("deliver", help="打包客户交付物")
    s.add_argument("--slug", required=True)
    s.set_defaults(func=cmd_deliver)

    s = sub.add_parser("publish", help="把成稿/资产发布到已配置的渠道（永远手动触发）")
    s.add_argument("--slug", required=True)
    s.add_argument("--path", required=True, help="content/ 或 assets/ 下的相对路径")
    s.add_argument("--platform", required=True, choices=["github", "wordpress", "wechat_draft", "webhook"])
    s.add_argument("--title")
    s.set_defaults(func=cmd_publish)

    s = sub.add_parser("task", help="查看或更新单条工单状态")
    s.add_argument("--slug", required=True)
    s.add_argument("--id", required=True)
    s.add_argument("--status", choices=["todo", "doing", "done", "blocked", "wontfix"])
    s.add_argument("--note")
    s.set_defaults(func=cmd_task)

    s = sub.add_parser("status", help="项目进度看板")
    s.add_argument("--slug", required=True)
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("serve", help="完整服务周期：抓取→体检→采样→工单→资产→报告→验收→交付")
    s.add_argument("--slug", required=True)
    s.add_argument("--max-pages", type=int, default=None, dest="max_pages")
    s.add_argument("--limit", type=int, default=None, help="采样只跑前 N 个问题")
    s.add_argument("--no-sample", action="store_true", dest="no_sample")
    s.add_argument("--draft", action="store_true", help="额外生成文章初稿")
    s.add_argument("--draft-limit", type=int, default=3, dest="draft_limit")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("ui", help="启动可观测看板（趋势、工单、信源、验收历史）")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-open", action="store_true", dest="no_open")
    s.set_defaults(func=cmd_ui)

    s = sub.add_parser("list", help="列出所有项目")
    s.set_defaults(func=cmd_list)

    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
