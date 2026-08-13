# Upstream and merge record

## Upstream repository

- Repository: `https://github.com/aigclink/geolook.git`
- Fixed upstream commit: `f8bd3656a1b38c4fcba30b5cd46de4b61b8e9796`
- Branch: `delivery-ops/v0.1.0-rc.2`
- Clone path: local checkout (not part of the distribution)

## Applied Change Sets

- Change Set 1: applied
- Change Set 2: applied
- Change Set 3: applied
- Change Set 4: applied
- Change Set 5: applied
- Change Set 6: applied
- Change Set 7: applied
- Change Set 8: applied
- Change Set 9: applied
- Change Set 10: applied
- Change Set 11: applied
- Change Set 12: manually merged; source patch was malformed at line 70
- Change Set 13: manually merged; source patch was malformed at line 48

## Modified host files

```text
scripts/geo.py
scripts/tasks.py
scripts/verify.py
scripts/dashboard.py
scripts/ui.html
scripts/generate.py
scripts/deliver.py
scripts/deliverables.py
scripts/bootstrap.py
```

## Added Delivery Ops files

```text
scripts/delivery.py
scripts/export_delivery.py
scripts/industry_templates.py
scripts/integrations.py
scripts/prioritize.py
scripts/release.py
scripts/rc.py
scripts/signals.py
```

## Known upstream conflicts and limitations

- Change Sets 2–11 applied cleanly after Change Set 1.
- Change Sets 12–13 were not valid unified patches and were merged manually from their
  intended additions; the original patch files were not copied into this Runtime repo.
- No upstream source files were deleted.
- Runtime CLI help works after installing the upstream dependencies `requests`,
  `beautifulsoup4` and `lxml`.
- Change Sets 12–13 were manually merged because the supplied patch files were
  malformed; `RUNTIME_VALIDATION.md` records the exact checks run after the merge.
- Showcase/portfolio tests remain under `showcase-tests/` and are intentionally not
  part of the source-runtime test discovery. The source repository keeps the upstream
  product identity and attribution; the separate Showcase artifact has its own
  public-brand boundary.
- No upstream source files were deleted. The only new runtime behavior outside the
  Delivery Ops schema is CSV formula neutralization and webhook SSRF validation.

## License and attribution

The upstream project is MIT licensed. The original `LICENSE` is retained, and `NOTICE.md`
records the required third-party copyright attribution. Delivery Ops additions remain
under the same MIT license unless a file states otherwise.
