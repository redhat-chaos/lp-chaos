# Coverage catalog

`catalog.py` is the source of truth for Virt Chaos
**Coverage – What We Test**. Each scenario is
declared once. ci-operator `as:` names are aliases
in `testAs`.

`catalog.json` is a generated export for dashboards.
Do not edit it by hand. The dashboard reads this file
and does not hardcode topic names.

Topic values are the `TOPIC_*` constants:

- `TOPIC_MIGRATION` — Migration
- `TOPIC_SNAPSHOT` — Snapshot
- `TOPIC_STANDARD` — Standard
- `TOPIC_UPGRADE` — Upgrade

Do not invent extra topics, and do not use cloud or
layered-product names as topics.

Join is an allowlist, never job-name token parsing:

1. Exact `as:` name, or
2. Prow job name ending in `-{as:}`
3. Longest alias wins (`krkn-hub-node-tests` before
   `krkn-hub-tests`)

Unknown `as:` names stay unclassified until added
here.

## Validate

```bash
python3 test/metadata/coverage-catalog/catalog.py --write-json
python3 test/metadata/coverage-catalog/test_catalog.py
```
