# Release artifacts

The reproducible release check is run from a clean checkout:

```bash
python3 -m pip install -r requirements.txt
python3 scripts/release_validate.py --output release
```

The command creates a deterministic offline Tester Kit and writes the
following evidence under `release/`:

- `geo-delivery-ops-v0.1.0-rc.2-tester-kit.zip`
- `SHA256SUMS.txt`
- `BUILD_MANIFEST.json`
- `release-validation.log`
- `RELEASE_NOTES.md`

The package allow-list excludes the complete runtime, `.env`, `work/`,
`__pycache__`, bytecode, customer materials, and local absolute paths. The
GitHub Actions matrix repeats the same flow on macOS and Ubuntu with Python
3.11 and 3.12.
