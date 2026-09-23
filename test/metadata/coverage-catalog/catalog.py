"""Coverage catalog keyed by ci-operator ``as:`` names."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

CATALOG_PATH = Path(__file__).with_name("catalog.json")

TOPIC_MIGRATION = "Migration"
TOPIC_SNAPSHOT = "Snapshot"
TOPIC_STANDARD = "Standard"
TOPIC_UPGRADE = "Upgrade"
ALLOWED_TOPICS = frozenset(
    {
        TOPIC_MIGRATION,
        TOPIC_SNAPSHOT,
        TOPIC_STANDARD,
        TOPIC_UPGRADE,
    }
)


def _format_allowed(values: frozenset[str]) -> str:
    return " or ".join(sorted(values))


def coverage_scenario(
    *,
    scenario_id: str,
    scenario_name: str,
    topic: str,
    test_as: list[str],
) -> dict[str, Any]:
    """One catalog row for the Virt Chaos Coverage table."""
    if topic not in ALLOWED_TOPICS:
        raise ValueError(
            f"unsupported coverage topic {topic!r}; "
            f"expected {_format_allowed(ALLOWED_TOPICS)}"
        )
    return {
        "scenarioId": scenario_id,
        "scenarioName": scenario_name,
        "topic": topic,
        "testAs": test_as,
    }


SCENARIOS: list[dict[str, Any]] = [
    coverage_scenario(
        scenario_id="cnv-outage-kubevirt",
        scenario_name="KubeVirt outage",
        topic=TOPIC_STANDARD,
        test_as=[
            "cr--cnv--outage-kubevirt--aws",
            "kubevirt-outage",
        ],
    ),
    coverage_scenario(
        scenario_id="cnv-outage-node",
        scenario_name="Node outage",
        topic=TOPIC_STANDARD,
        test_as=[
            "cr--cnv--outage-node--aws",
            "lp-chaos--cnv--node-outage",
        ],
    ),
    coverage_scenario(
        scenario_id="cnv-outage-pod",
        scenario_name="Pod outage",
        topic=TOPIC_STANDARD,
        test_as=["cr--cnv--outage-pod--aws"],
    ),
    coverage_scenario(
        scenario_id="cnv-snapshot-kill-apiserver",
        scenario_name="Snapshot kill API server",
        topic=TOPIC_SNAPSHOT,
        test_as=["cr--cnv--snapshot--kill--apiserver--aws"],
    ),
    coverage_scenario(
        scenario_id="cnv-snapshot-kill-controller",
        scenario_name="Snapshot kill controller",
        topic=TOPIC_SNAPSHOT,
        test_as=["cr--cnv--snapshot--kill--controller--aws"],
    ),
    coverage_scenario(
        scenario_id="cnv-snapshot-kill-csi-driver",
        scenario_name="Snapshot kill CSI driver",
        topic=TOPIC_SNAPSHOT,
        test_as=["cr--cnv--snapshot--kill--csi-driver--aws"],
    ),
    coverage_scenario(
        scenario_id="cnv-snapshot-kill-virt-api",
        scenario_name="Snapshot kill virt-api",
        topic=TOPIC_SNAPSHOT,
        test_as=["cr--cnv--snapshot--kill--virt-api--aws"],
    ),
    coverage_scenario(
        scenario_id="cnv-krkn-hub",
        scenario_name="Krkn hub tests",
        topic=TOPIC_STANDARD,
        test_as=["krkn-hub-tests"],
    ),
    coverage_scenario(
        scenario_id="cnv-krkn-hub-node",
        scenario_name="Krkn hub node tests",
        topic=TOPIC_STANDARD,
        test_as=["krkn-hub-node-tests"],
    ),
]


def catalog_dict() -> dict[str, Any]:
    return {
        "topics": sorted(ALLOWED_TOPICS),
        "scenarios": SCENARIOS,
    }


def load_catalog(path: Path = CATALOG_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def dump_catalog_json(path: Path = CATALOG_PATH) -> None:
    path.write_text(
        json.dumps(catalog_dict(), indent=2) + "\n",
        encoding="utf-8",
    )


def classification_of(scenario: dict[str, Any]) -> dict[str, str]:
    return {
        "scenarioId": scenario["scenarioId"],
        "scenarioName": scenario["scenarioName"],
        "topic": scenario["topic"],
    }


def index_by_test_as(
    scenarios: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, str]], list[str]]:
    by_test_as: dict[str, dict[str, str]] = {}
    scenario_ids: set[str] = set()
    for scenario in scenarios:
        scenario_id = scenario["scenarioId"]
        if scenario_id in scenario_ids:
            raise ValueError(f"duplicate coverage scenario id {scenario_id!r}")
        scenario_ids.add(scenario_id)
        topic = scenario.get("topic")
        if topic not in ALLOWED_TOPICS:
            raise ValueError(
                f"unsupported coverage topic {topic!r} on {scenario_id!r}; "
                f"expected {_format_allowed(ALLOWED_TOPICS)}"
            )
        classification = classification_of(scenario)
        aliases = scenario.get("testAs") or []
        if not aliases:
            raise ValueError(
                f"coverage scenario {scenario_id!r} has no test as aliases"
            )
        for raw_test_as in aliases:
            test_as = str(raw_test_as).strip()
            if not test_as:
                raise ValueError(
                    f"coverage scenario {scenario_id!r} has an empty test as alias"
                )
            previous = by_test_as.get(test_as)
            if previous:
                raise ValueError(
                    f"duplicate coverage test as {test_as!r} "
                    f"({previous['scenarioId']} and {scenario_id})"
                )
            by_test_as[test_as] = classification
    test_as_keys = sorted(by_test_as, key=len, reverse=True)
    return by_test_as, test_as_keys


def classify_job_name(
    job_name: str,
    by_test_as: dict[str, dict[str, str]],
    test_as_keys: list[str],
) -> dict[str, str] | None:
    name = job_name.strip()
    if not name:
        return None
    for test_as in test_as_keys:
        if name == test_as or name.endswith(f"-{test_as}"):
            return by_test_as[test_as]
    return None


def validate_catalog(path: Path = CATALOG_PATH) -> None:
    scenarios = catalog_dict()["scenarios"]
    if not scenarios:
        raise ValueError("coverage catalog scenarios must be a non-empty list")
    for scenario in scenarios:
        for field in ("scenarioId", "scenarioName", "topic"):
            if not str(scenario.get(field) or "").strip():
                raise ValueError(f"coverage scenario missing {field}")
    index_by_test_as(scenarios)
    exported = load_catalog(path)
    if exported != catalog_dict():
        raise ValueError(
            f"{path.name} is stale; run python3 "
            "test/metadata/coverage-catalog/catalog.py --write-json"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write-json",
        action="store_true",
        help="rewrite catalog.json from the Python catalog",
    )
    args = parser.parse_args()
    if args.write_json:
        dump_catalog_json()
    validate_catalog()
    print(f"ok {CATALOG_PATH}")


if __name__ == "__main__":
    main()
