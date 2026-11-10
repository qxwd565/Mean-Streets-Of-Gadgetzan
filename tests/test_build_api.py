import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

module_path = Path(__file__).resolve().parents[1] / "tools" / "build-api.py"
spec = importlib.util.spec_from_file_location("build_api", module_path)
build_api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_api)


class BuildApiTests(unittest.TestCase):
    def test_json_values_and_missing_are_distinct(self):
        initial = {
            "cost": 0,
            "text": "",
            "enabled": False,
            "optional": None,
            "nativeTags": [],
        }
        latest = {
            "cost": 1,
            "text": "new",
            "enabled": True,
            "optional": "set",
            "nativeTags": [],
        }
        patch = {
            "date": "2017-01-01",
            "changes": {
                "cost": 1,
                "text": "new",
                "enabled": True,
                "optional": "set",
            },
        }
        transitions = [{
            "from": "2016-12-02",
            "date": "2017-01-01",
            "before": initial,
            "changes": patch["changes"],
            "after": latest,
        }]
        entries = build_api.reverse_dated_history(
            "TEST_001",
            {"initialSpec": initial, "patches": [patch]},
            latest,
            transitions,
        )
        changes = entries[0]["changes"]
        self.assertEqual(changes["cost"], 0)
        self.assertEqual(changes["text"], "")
        self.assertIs(changes["enabled"], False)
        self.assertIsNone(changes["optional"])
        self.assertNotIn("absent", changes)

    def test_legacy_history_rejects_field_deletion(self):
        timeline = {
            "initialSpec": {"name": "Test", "nativeTags": []},
            "patches": [{"date": "2017-01-01", "changes": {"addedLater": "value"}}],
        }
        transition = [{
            "from": "2016-12-02",
            "date": "2017-01-01",
            "before": {"name": "Test", "nativeTags": []},
            "changes": {"addedLater": "value"},
            "after": {"name": "Test", "nativeTags": [], "addedLater": "value"},
        }]
        with self.assertRaisesRegex(ValueError, "cannot represent field absence"):
            build_api.reverse_dated_history(
                "TEST_001",
                timeline,
                {"name": "Test", "nativeTags": [], "addedLater": "value"},
                transition,
            )

    def test_pending_history_does_not_invent_from(self):
        timeline = {
            "knownEvents": [{
                "date": "2017-07-11",
                "before": {"races": [], "race": "", "nativeTags": []},
                "changes": {"races": ["ELEMENTAL"], "race": "정령", "nativeTags": [{"id": 1}]},
            }]
        }
        current = {"races": ["ELEMENTAL"], "race": "정령", "nativeTags": [{"id": 1}]}
        entries = build_api.reverse_pending_history("TOKEN_001", timeline, current)
        self.assertEqual(len(entries), 1)
        self.assertNotIn("from", entries[0])
        self.assertEqual(entries[0]["before"], "2017-07-11")

    def test_build_twice_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "data").mkdir()
            card = {
                "id": "TEST_001", "dbfId": 1, "name": "Test", "type": "MINION",
                "cardClass": "NEUTRAL", "rarity": "COMMON", "collectible": True,
                "cost": 1, "text": "", "attack": 1, "health": 1, "durability": 0,
                "armor": 0, "flavor": "", "spellSchool": "", "portrait": "test",
                "set": "GANGS", "nativeTags": [{"id": 48, "name": "마나 비용", "value": 1}],
                "races": [], "race": "", "classes": [], "art": "art/TEST_001.png",
                "releasedAt": "2016-12-02", "releaseStatus": "verified",
            }
            timeline = {
                "schema": 1, "releasedAt": "2016-12-02", "releaseStatus": "verified",
                "initialSpecStatus": "reconstructed",
                "initialSpec": {
                    "name": "Test", "type": "MINION", "cardClass": "NEUTRAL",
                    "classes": [], "rarity": "COMMON", "collectible": True,
                    "cost": 0, "text": "", "attack": 1, "health": 1,
                    "nativeTags": [{"id": 48, "name": "마나 비용", "value": 0}],
                },
                "patches": [{"date": "2017-01-01", "changes": {
                    "cost": 1,
                    "nativeTags": [{"id": 48, "name": "마나 비용", "value": 1}],
                }}],
                "knownEvents": [],
            }
            document = {"schemaVersion": 1, "mode": "constructed", "card": card, "timeline": timeline}
            (root / "data" / "TEST_001.json").write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
            policy = {
                "schema": 1, "timezone": "Asia/Seoul", "set": "Mean Streets Of Gadgetzan",
                "default": {"date": "2016-12-02", "status": "verified"}, "cards": {},
            }
            (root / "release-policy.json").write_text(json.dumps(policy), encoding="utf-8")
            index = {
                "schema": 1, "timezone": "Asia/Seoul", "set": "Mean Streets Of Gadgetzan",
                "cards": {"TEST_001": {"releasedAt": "2016-12-02", "releaseStatus": "verified",
                    "initialSpecStatus": "reconstructed", "timeline": {
                        "2016-12-02": {"name": "Test", "type": "MINION", "cardClass": "NEUTRAL",
                            "classes": [], "rarity": "COMMON", "collectible": True, "cost": 0,
                            "text": "", "attack": 1, "health": 1,
                            "ownTags": [{"id": 48, "name": "마나 비용", "value": 0}]},
                        "2017-01-01": {"cost": 1, "ownTags": [{"id": 48, "name": "마나 비용", "value": 1}]},
                    }}},
            }
            audit = {"events": [{
                "date": "2017-01-01", "cardId": "TEST_001",
                "before": {"cost": 0, "ownTags": [{"id": 48, "name": "마나 비용", "value": 0}]},
                "changes": {"cost": 1, "ownTags": [{"id": 48, "name": "마나 비용", "value": 1}]},
            }]}
            pending = {"firstPlayableDatesPending": [], "remainingUnverified": []}
            for name, value in (
                ("timeline-index.json", index),
                ("history-audit.json", audit),
                ("pending-history.json", pending),
            ):
                (root / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            first, _ = build_api.build_outputs(root, expected_count=1)
            build_api.write_outputs(root, first)
            second, _ = build_api.build_outputs(root, expected_count=1)
            self.assertEqual(first, second)
            self.assertEqual(build_api.write_outputs(root, second), [])


if __name__ == "__main__":
    unittest.main()
