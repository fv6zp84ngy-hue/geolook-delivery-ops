# Runtime validation — v0.1.0-rc.2

This file records the release-candidate checks for the complete merged source
repository. It is deliberately separate from the offline Tester Kit and the
static Showcase site.

## Source identity

- Upstream repository: `https://github.com/aigclink/geolook.git`
- Upstream commit: `f8bd3656a1b38c4fcba30b5cd46de4b61b8e9796`
- Runtime branch: `delivery-ops/v0.1.0-rc.2`
- Distribution version: `0.1.0-rc.2`
- Schema version: `2.0`

## Required source files

```text
PASS scripts/geo.py
PASS scripts/delivery.py
PASS scripts/tasks.py
PASS scripts/verify.py
PASS scripts/dashboard.py
PASS scripts/ui.html
PASS scripts/generate.py
PASS scripts/deliver.py
```

## Commands

The following commands completed successfully in the repository virtualenv:

```bash
python3 scripts/geo.py --help
python3 scripts/geo.py ui --help
python3 scripts/geo.py delivery-doctor --help
python3 scripts/geo.py version --json
```

The CLI is the merged source runtime. The separate Tester Kit does not contain
these host files and must not be described as a production Agent installer.

## Automated tests

```bash
PYTHONPATH=scripts .venv/bin/python -m unittest discover -s tests
```

Result on 2026-08-06:

```text
Ran 335 tests in 1.112s
OK
```

The suite includes the upstream GeoLook tests and Delivery Ops tests for schema
compatibility, event logs, recovery, verification gates, industry templates,
priority scoring, signal imports, exports, and external adapters.

## P0-4 real UI/API acceptance

The controlled SaaS case uses a real local Dashboard process and a separately
served English Product Labs website. The browser creates the project, locks the
baseline through the visible modal, confirms evidence and scope, assigns the
owner, generates and approves an asset, submits a deployment URL, runs the
required verification, refreshes the page to prove persistence, removes the
definition sentence to trigger an automatic reopen, and restores it for a final
Reviewer-backed close.

```bash
PYTHONPATH=scripts .venv/bin/pytest tests/e2e/test_delivery_flow.py -q -s
```

Result on 2026-08-07:

```text
2 passed in 15.83s
```

The browser suite does not edit `tasks.json`, `delivery/events/*.jsonl`,
`manifest.json`, or verification snapshots. It writes only to the controlled
fixture website when simulating deployment, deletion, and recovery. The
runtime's private/loopback URL guard remains enabled by default; the E2E child
process opts into the paired `GEO_E2E=1` and `GEO_ALLOW_LOCAL_DEPLOYMENT=1`
switches solely for this isolated site.

Evidence artifacts:

```text
tests/e2e/artifacts/saas-verified.png
tests/e2e/artifacts/saas-regressed-reopened.png
tests/e2e/artifacts/saas-recovered-verified.png
tests/e2e/artifacts/saas-delivery-flow.webm
```

The E2E test also checks that an unapproved asset cannot be deployed, a
malformed deployment submitted through the UI produces an error toast, and a
manual definition-consistency gate cannot be bypassed by automatic checks.

## Security checks included

- CSV exports prefix formula-like cells with an apostrophe so spreadsheet
  applications do not execute them.
- Webhook validation rejects credentials in URLs and local/private/link-local,
  loopback, reserved, multicast, and unspecified targets.
- Existing path, report-leak, backup, JSONL corruption, and external URL tests
  remain enabled.

## Showcase-only tests

The previous standalone extension repository also contains portfolio, landing
page, and real-case artifact tests. They are retained in `showcase-tests/` for
the Showcase artifact, but are not Runtime blockers because the complete source
repository intentionally retains the upstream product identity and runtime
documentation.

## Remaining release boundary

This validation proves the local merged source runtime and its deterministic
tests. It does not claim that an external GitHub repository has been pushed, nor
does it claim that a real customer website has been deployed from this checkout.
