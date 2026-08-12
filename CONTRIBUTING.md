# Contributing

Run the checks before opening a pull request:

```bash
python -m pip install -e '.[dev]'
ruff check src tests
pytest -q
```

Keep operator commands deterministic, avoid shell interpolation, and add a
fixture whenever the topology schema changes. Deployment templates must never
contain real secrets or unpinned production images.
