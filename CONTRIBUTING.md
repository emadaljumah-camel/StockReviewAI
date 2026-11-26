# Contributing

Read [setup](README.md#setup), [architecture](docs/ARCHITECTURE.md), and
[security](SECURITY.md). Discuss substantial behavior changes first and keep
patches focused. Preserve unrelated changes and separate dependency updates.

## Tests

From the `server` directory in its existing virtual environment:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s . -p "test_*.py" -q
.\.venv\Scripts\python.exe -m pip check
```

From the repository root, run `git diff --check`.

Tests use synthetic data and mocked providers; they do not measure real model
quality or exercise Lightroom UI. For manual testing, use a backed-up disposable
catalog. Start with one-photo dry-run, then verify actual Pick/Rejected mapping,
existing-flag protection, cancellation, resume, and failure behavior. Record
Lightroom/Python/model versions and distinguish observations from assumptions.

The optional reasoning benchmark invokes real inference and can write filenames,
location, and diagnostics. It is not an automatic accuracy test. Read the
[tooling guide](server/README.md#benchmark-and-diagnostics) before using it.

## Pull requests

- Explain the problem, resulting behavior, and meaningful checks.
- Test policy changes and failed/retried jobs with focused cases.
- Preserve job identities, upload hashes, checkpoints, and result validation.
- Update docs when URLs, settings, scoring, retention, or application changes.
- Do not commit secrets, local config, catalogs, photos, model weights, job data,
  generated diagnostics, or private paths. Use synthetic fixtures.
- Check source/history for secrets before sharing branches or logs.

Original contributions use the [MIT license](LICENSE). Preserve notices and
compatible licensing for third-party material. Follow [SECURITY.md](SECURITY.md)
for vulnerability reports instead of publishing exploit details.
