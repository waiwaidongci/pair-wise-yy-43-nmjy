import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES
from src.verification import VerificationConflict


class VerificationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item({
            "title": "verification item", "description": "disposal close gate",
            "severity": "major", "quantity": 10, "threshold": 5,
            "external_ref": "V-1",
        }, "creator", "observer")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _reach_monitoring(self):
        current = self.item
        for target in STATES[1:-1]:
            current = self.service.transition(
                current["id"], target, current["version"], "reviewer",
                TRANSITION_ROLES[target][0])
        return current

    def _register_all(self, item_id, recovery=10):
        rec = self.service.register_verification(item_id, {
            "kind": "recovery", "recovery_quantity": recovery,
            "handler": "回收班组", "external_ref": "R-1"}, "ops", "operations")
        self.service.register_verification(item_id, {
            "kind": "shoreline", "shoreline_result": "正常",
            "handler": "岸线组", "external_ref": "S-1"}, "ops", "operations")
        self.service.register_verification(item_id, {
            "kind": "waste", "waste_destination": "危废中心", "handed_over": True,
            "handler": "清运班组", "external_ref": "W-1"}, "ops", "operations")
        return rec

    def test_close_returns_each_missing_category(self):
        current = self._reach_monitoring()
        # 三类都没有：返回全部三类
        with self.assertRaises(VerificationConflict) as ctx:
            self.service.transition(current["id"], "closed", current["version"],
                                    "reviewer", "response_commander")
        codes = [m["code"] for m in ctx.exception.missing]
        self.assertEqual(codes, ["recovery", "shoreline", "waste"])

        # 只有回收，且不够八成：返回回收+岸线+废弃物
        self.service.register_verification(current["id"], {
            "kind": "recovery", "recovery_quantity": 7, "handler": "回收班组"},
            "ops", "operations")
        with self.assertRaises(VerificationConflict) as ctx:
            self.service.transition(current["id"], "closed", current["version"],
                                    "reviewer", "response_commander")
        self.assertIn("recovery", [m["code"] for m in ctx.exception.missing])

        # 补齐回收、岸线异常、废弃物未交接：返回岸线+废弃物
        self.service.register_verification(current["id"], {
            "kind": "recovery", "recovery_quantity": 1, "handler": "回收班组",
            "external_ref": "R-2"}, "ops", "operations")
        self.service.register_verification(current["id"], {
            "kind": "shoreline", "shoreline_result": "异常", "handler": "岸线组"},
            "ops", "operations")
        self.service.register_verification(current["id"], {
            "kind": "waste", "waste_destination": "暂存点", "handed_over": False,
            "handler": "清运班组"}, "ops", "operations")
        with self.assertRaises(VerificationConflict) as ctx:
            self.service.transition(current["id"], "closed", current["version"],
                                    "reviewer", "response_commander")
        self.assertEqual([m["code"] for m in ctx.exception.missing],
                         ["shoreline", "waste"])

    def test_close_gate_boundary_and_latest_shoreline(self):
        current = self._reach_monitoring()
        self.service.register_verification(current["id"], {
            "kind": "recovery", "recovery_quantity": 7.999,
            "handler": "回收班组"}, "ops", "operations")
        self.service.register_verification(current["id"], {
            "kind": "shoreline", "shoreline_result": "正常", "handler": "岸线组"},
            "ops", "operations")
        self.service.register_verification(current["id"], {
            "kind": "waste", "waste_destination": "危废中心", "handed_over": True,
            "handler": "清运班组"}, "ops", "operations")
        # 7.999 < 八成 8
        with self.assertRaises(VerificationConflict):
            self.service.transition(current["id"], "closed", current["version"],
                                    "reviewer", "response_commander")
        self.service.register_verification(current["id"], {
            "kind": "recovery", "recovery_quantity": 0.001, "handler": "回收班组",
            "external_ref": "R-2"}, "ops", "operations")
        closed = self.service.transition(current["id"], "closed",
                                         current["version"], "reviewer",
                                         "response_commander")
        self.assertEqual(closed["status"], "closed")
        conclusion = self.service.verification_status(
            current["id"], "viewer")["latest_conclusion"]
        self.assertEqual(conclusion["outcome"], "closed")

    def test_correction_after_close_reopens_and_archives(self):
        current = self._reach_monitoring()
        rec = self._register_all(current["id"])
        closed = self.service.transition(current["id"], "closed",
                                         current["version"], "reviewer",
                                         "response_commander")
        self.assertEqual(closed["status"], "closed")

        # 原始回收记录更正为不足八成：事件回到待复核，旧记录留档
        corrected = self.service.correct_verification(rec["id"], {
            "recovery_quantity": 3, "reason": "计量口径更正"}, "ops", "operations")
        self.assertNotEqual(corrected["id"], rec["id"])
        self.assertEqual(corrected["supersedes_id"], rec["id"])
        self.assertEqual(corrected["status"], "active")

        item = self.service.get_item(current["id"], "viewer")
        self.assertEqual(item["status"], "monitoring")
        entries = self.service.list_verifications(current["id"], "viewer")
        old = next(e for e in entries if e["id"] == rec["id"])
        self.assertEqual(old["status"], "superseded")

        status = self.service.verification_status(current["id"], "viewer")
        self.assertFalse(status["ready"])
        self.assertEqual(status["missing"][0]["code"], "recovery")
        # 旧结论继续留档，新增reopened结论
        self.assertEqual(status["latest_conclusion"]["outcome"], "reopened")

        # 重开后仍需重新核验通过才能再关闭
        with self.assertRaises(VerificationConflict):
            self.service.transition(current["id"], "closed", item["version"],
                                    "reviewer", "response_commander")

    def test_waste_must_all_be_handed_over(self):
        current = self._reach_monitoring()
        self._register_all(current["id"])
        # 再登记一笔未交接废弃物
        self.service.register_verification(current["id"], {
            "kind": "waste", "waste_destination": "二号暂存点",
            "handed_over": False, "handler": "清运班组",
            "external_ref": "W-2"}, "ops", "operations")
        with self.assertRaises(VerificationConflict) as ctx:
            self.service.transition(current["id"], "closed", current["version"],
                                    "reviewer", "response_commander")
        self.assertEqual([m["code"] for m in ctx.exception.missing], ["waste"])

    def test_register_validation_and_permission(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_verification(self.item["id"], {
                "kind": "recovery", "recovery_quantity": 1}, "obs", "observer")
        with self.assertRaises(ValidationError):
            self.service.register_verification(self.item["id"], {
                "kind": "unknown", "handler": "h"}, "ops", "operations")
        with self.assertRaises(ValidationError):
            self.service.register_verification(self.item["id"], {
                "kind": "recovery", "recovery_quantity": -1, "handler": "h"},
                "ops", "operations")
        with self.assertRaises(ValidationError):
            self.service.register_verification(self.item["id"], {
                "kind": "waste", "waste_destination": "", "handed_over": True},
                "ops", "operations")

    def test_cannot_correct_superseded_record(self):
        first = self.service.register_verification(self.item["id"], {
            "kind": "recovery", "recovery_quantity": 9, "handler": "回收班组",
            "external_ref": "RX-1"}, "ops", "operations")
        second = self.service.correct_verification(first["id"], {
            "recovery_quantity": 8, "reason": "首次更正"}, "ops", "operations")
        with self.assertRaises(ConflictError):
            self.service.correct_verification(first["id"], {
                "recovery_quantity": 7, "reason": "再次更正"}, "ops", "operations")
        self.assertTrue(self.repo.verify_audit_chain())
        del second


if __name__ == "__main__":
    unittest.main()
