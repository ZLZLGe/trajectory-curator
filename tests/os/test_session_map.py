import copy
import unittest

from trajectory_os import build_session_map, classify, refine, session_key

SESSION_ID = "12345678-1234-5678-1234-123456789abc"


def record(index, text="", *, harness="codex", date="2026-07-01", uuid=SESSION_ID):
    return {
        "metadata": {"session_id": uuid},
        "_basetraj": {
            "source_key": f"classified/dt={date}/{uuid}_sub_{index}.jsonl",
            "harness": harness,
        },
        "messages": [{"role": "system", "content": text}],
    }


class SessionMapTests(unittest.TestCase):
    def test_anchor_is_deterministic_and_prefers_direct_declarations(self):
        records = [
            record(0, "Working directory: /Users/alice/project"),
            record(2, "Platform: darwin"),
            record(1, "Platform: macOS"),
            record(3),
        ]
        original = copy.deepcopy(records)
        mapping = build_session_map(iter(records))
        self.assertEqual(mapping, build_session_map(reversed(records)))
        anchor = next(iter(mapping.values()))
        self.assertEqual(anchor["source_key"], records[2]["_basetraj"]["source_key"])
        self.assertEqual(anchor["supporting_records"], 3)
        result = refine(records[3], classify(records[3]), mapping)
        self.assertEqual(result["runtime_os"], "mac")
        self.assertEqual(result["runtime_os_method"], "session_context")
        self.assertEqual(
            result["runtime_os_evidence"][0]["source"],
            "same_session:" + anchor["source_key"],
        )
        self.assertEqual(records, original)

    def test_disagreeing_siblings_prevent_inheritance(self):
        records = [
            record(0, "Platform: linux"),
            record(1, "Platform: darwin"),
            record(2),
        ]
        mapping = build_session_map(records)
        self.assertEqual(mapping, {})
        self.assertEqual(
            refine(records[2], classify(records[2]), mapping)["runtime_os"], "unknown"
        )

    def test_conflicting_or_unsupported_sibling_blocks_the_session(self):
        for text in ("Platform: FreeBSD", "Platform: windows\nOS Version: Linux"):
            with self.subTest(text=text):
                records = [record(0, "Platform: linux"), record(1, text), record(2)]
                self.assertEqual(build_session_map(records), {})

    def test_session_metadata_must_match_for_anchors_and_targets(self):
        for index in (0, 1):
            for session_id in (None, "different-session"):
                with self.subTest(index=index, session_id=session_id):
                    records = [record(0, "Platform: linux"), record(1)]
                    records[index]["metadata"] = (
                        {} if session_id is None else {"session_id": session_id}
                    )
                    self.assertEqual(build_session_map(records), {})

    def test_harness_date_and_uuid_are_separate_scopes(self):
        target = record(1)
        for overrides in (
            {"harness": "claude-code"},
            {"date": "2026-07-02"},
            {"uuid": "87654321-4321-8765-4321-cba987654321"},
        ):
            with self.subTest(overrides=overrides):
                sibling = record(0, "Platform: linux", **overrides)
                self.assertEqual(build_session_map([sibling, target]), {})

    def test_stored_inherited_annotations_never_become_anchors(self):
        direct = record(0, "Platform: linux")
        inherited = record(1)
        mapping = build_session_map([direct, inherited])
        inherited["_basetraj"].update(refine(inherited, classify(inherited), mapping))
        self.assertEqual(build_session_map([inherited, record(2)]), {})
        rebuilt = build_session_map([direct, inherited, record(2)])
        self.assertEqual(next(iter(rebuilt.values()))["supporting_records"], 1)

    def test_only_supported_sessions_with_missing_evidence_need_anchors(self):
        self.assertEqual(build_session_map([record(0, "Platform: linux")]), {})
        records = [record(0, "Platform: linux"), record(1)]
        for item in records:
            item["_basetraj"]["source_key"] = item["_basetraj"]["source_key"].replace(
                SESSION_ID, "probe"
            )
        self.assertEqual(build_session_map(records), {})
        self.assertIsNone(session_key(records[0]["_basetraj"]["source_key"], "codex"))


if __name__ == "__main__":
    unittest.main()
