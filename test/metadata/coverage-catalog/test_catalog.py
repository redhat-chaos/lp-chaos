import unittest

from catalog import (
    ALLOWED_TOPICS,
    TOPIC_SNAPSHOT,
    TOPIC_STANDARD,
    classify_job_name,
    coverage_scenario,
    index_by_test_as,
    load_catalog,
    validate_catalog,
)

KUBEVIRT_LPGA = (
    "periodic-ci-redhat-chaos-lp-chaos-main-ocp-4.22-lpGA-lp-chaos-"
    "cr--cnv--outage-kubevirt--aws"
)
NODE_LPGA = (
    "periodic-ci-redhat-chaos-lp-chaos-main-ocp-4.22-lpGA-lp-chaos-"
    "cr--cnv--outage-node--aws"
)
POD_LPGA = (
    "periodic-ci-redhat-chaos-lp-chaos-main-ocp-4.22-lpGA-lp-chaos-"
    "cr--cnv--outage-pod--aws"
)
SNAPSHOT_APISERVER = (
    "periodic-ci-redhat-chaos-lp-chaos-main-ocp-4.22-lpGA-lp-chaos-"
    "cr--cnv--snapshot--kill--apiserver--aws"
)
SNAPSHOT_CONTROLLER = (
    "periodic-ci-redhat-chaos-lp-chaos-main-ocp-4.22-lpGA-lp-chaos-"
    "cr--cnv--snapshot--kill--controller--aws"
)
SNAPSHOT_CSI = (
    "periodic-ci-redhat-chaos-lp-chaos-main-ocp-4.22-lpGA-lp-chaos-"
    "cr--cnv--snapshot--kill--csi-driver--aws"
)
SNAPSHOT_VIRT_API = (
    "periodic-ci-redhat-chaos-lp-chaos-main-ocp-4.22-lpGA-lp-chaos-"
    "cr--cnv--snapshot--kill--virt-api--aws"
)
KRKN_HUB_NODE_421 = (
    "periodic-ci-redhat-chaos-lp-chaos-main-ocp4.21-nightly--"
    "cnv-4.21-stable--aws-krkn-hub-node-tests"
)
KRKN_HUB_421 = (
    "periodic-ci-redhat-chaos-lp-chaos-main-ocp4.21-nightly--"
    "cnv-4.21-stable--aws-krkn-hub-tests"
)
KUBEVIRT_421 = (
    "periodic-ci-redhat-chaos-lp-chaos-main-ocp4.21-nightly--"
    "cnv-4.21-stable--aws-kubevirt-outage"
)
NODE_CNV_CASES_421 = (
    "periodic-ci-redhat-chaos-lp-chaos-main-ocp4.21-nightly--"
    "cnv-4.21-stable-cnvcases--aws-lp-chaos--cnv--node-outage"
)
OCP_CHAOS_POD = (
    "periodic-ci-redhat-chaos-lp-chaos-main-ocp-4.22-ocp-chaos-"
    "cr--outage-pod--aws"
)
OCP_CHAOS_NODE = (
    "periodic-ci-redhat-chaos-lp-chaos-main-ocp-4.22-ocp-chaos-"
    "cr--outage-node--aws"
)

LIVE_JOBS = (
    KUBEVIRT_LPGA,
    NODE_LPGA,
    POD_LPGA,
    SNAPSHOT_APISERVER,
    SNAPSHOT_CONTROLLER,
    SNAPSHOT_CSI,
    SNAPSHOT_VIRT_API,
    KRKN_HUB_NODE_421,
    KRKN_HUB_421,
    KUBEVIRT_421,
    NODE_CNV_CASES_421,
)


class CatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        catalog = load_catalog()
        cls.by_test_as, cls.test_as_keys = index_by_test_as(catalog["scenarios"])

    def classify(self, job_name: str):
        return classify_job_name(job_name, self.by_test_as, self.test_as_keys)

    def test_catalog_validates(self) -> None:
        validate_catalog()

    def test_exported_topics_match_allowlist(self) -> None:
        catalog = load_catalog()
        self.assertEqual(set(catalog["topics"]), ALLOWED_TOPICS)

    def test_live_jobs_classify(self) -> None:
        for job_name in LIVE_JOBS:
            classified = self.classify(job_name)
            self.assertIsNotNone(classified, job_name)
            self.assertIn(classified["topic"], ALLOWED_TOPICS)

    def test_equivalent_ocp_versions_share_scenario(self) -> None:
        kubevirt_lpga = self.classify(KUBEVIRT_LPGA)
        kubevirt_421 = self.classify(KUBEVIRT_421)
        node_lpga = self.classify(NODE_LPGA)
        node_cnv_cases = self.classify(NODE_CNV_CASES_421)
        self.assertEqual(kubevirt_lpga["scenarioId"], "cnv-outage-kubevirt")
        self.assertEqual(kubevirt_421["scenarioId"], "cnv-outage-kubevirt")
        self.assertEqual(node_lpga["scenarioId"], "cnv-outage-node")
        self.assertEqual(node_cnv_cases["scenarioId"], "cnv-outage-node")

    def test_cnv_outage_jobs_are_standard(self) -> None:
        pod = self.classify(POD_LPGA)
        node = self.classify(NODE_LPGA)
        self.assertEqual(pod["scenarioId"], "cnv-outage-pod")
        self.assertEqual(pod["topic"], TOPIC_STANDARD)
        self.assertEqual(node["scenarioId"], "cnv-outage-node")
        self.assertEqual(node["topic"], TOPIC_STANDARD)

    def test_ocp_chaos_jobs_are_not_classified(self) -> None:
        self.assertIsNone(self.classify(OCP_CHAOS_POD))
        self.assertIsNone(self.classify(OCP_CHAOS_NODE))

    def test_job_name_tokens_do_not_classify(self) -> None:
        fake = (
            "periodic-ci-redhat-chaos-lp-chaos-main-ocp-4.22-"
            "cnv-kubevirt-outage-node-pod-aws"
        )
        self.assertIsNone(self.classify(fake))

    def test_krkn_hub_node_does_not_match_shorter_hub_key(self) -> None:
        node = self.classify(KRKN_HUB_NODE_421)
        hub = self.classify(KRKN_HUB_421)
        self.assertEqual(node["scenarioId"], "cnv-krkn-hub-node")
        self.assertEqual(hub["scenarioId"], "cnv-krkn-hub")

    def test_snapshot_kills_are_snapshots(self) -> None:
        self.assertEqual(self.classify(KUBEVIRT_LPGA)["topic"], TOPIC_STANDARD)
        self.assertEqual(self.classify(SNAPSHOT_APISERVER)["topic"], TOPIC_SNAPSHOT)
        self.assertEqual(self.classify(KUBEVIRT_421)["topic"], TOPIC_STANDARD)

    def test_unknown_topic_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported coverage topic"):
            coverage_scenario(
                scenario_id="bad",
                scenario_name="Bad",
                topic="AWS",
                test_as=["nope"],
            )

    def test_duplicate_scenario_id_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate coverage scenario id"):
            index_by_test_as(
                [
                    {
                        "scenarioId": "dup",
                        "scenarioName": "A",
                        "topic": TOPIC_STANDARD,
                        "testAs": ["alpha"],
                    },
                    {
                        "scenarioId": "dup",
                        "scenarioName": "B",
                        "topic": TOPIC_SNAPSHOT,
                        "testAs": ["beta"],
                    },
                ]
            )

    def test_duplicate_test_as_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate coverage test as"):
            index_by_test_as(
                [
                    {
                        "scenarioId": "one",
                        "scenarioName": "One",
                        "topic": TOPIC_STANDARD,
                        "testAs": ["shared"],
                    },
                    {
                        "scenarioId": "two",
                        "scenarioName": "Two",
                        "topic": TOPIC_SNAPSHOT,
                        "testAs": ["shared"],
                    },
                ]
            )


if __name__ == "__main__":
    unittest.main()
