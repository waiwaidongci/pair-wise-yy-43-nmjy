import tempfile, unittest
from pathlib import Path
from src import disposal_api
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
class DisposalTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.repo=Repository(str(Path(self.tmp.name)/"test.db")); self.service=Service(self.repo)
        self.item=self.service.create_item({"title":"disposal item","description":"disposal verification","severity":"major","quantity":100,"threshold":50,"external_ref":"DSP-1"},"creator",'observer')
    def tearDown(self): self.repo.close(); self.tmp.cleanup()
    def _to_monitoring(self):
        current=self.item
        for target,role in [("assessing","response_commander"),("containing","response_commander"),("recovering","operations"),("monitoring","operations")]:
            current=self.service.transition(current["id"],target,current["version"],"reviewer",role)
        return current
    def _close(self,current):
        return self.service.transition(current["id"],"closed",current["version"],"commander","response_commander")
    def _active(self,kind):
        return [r for r in self.service.list_disposal_records(self.item["id"],"viewer") if r["kind"]==kind and r["status"]=="active"]
    def test_close_blocked_until_three_categories_satisfied(self):
        current=self._to_monitoring()
        with self.assertRaises(ConflictError) as ctx: self._close(current)
        message=str(ctx.exception)
        self.assertIn("回收数量",message); self.assertIn("岸线复查",message); self.assertIn("废弃物去向",message)
        self.service.add_disposal_record(self.item["id"],{"kind":"recovery","quantity":80,"handler":"张三"},"operator",'operations')
        self.service.add_disposal_record(self.item["id"],{"kind":"shoreline","result":"normal","handler":"李四"},"operator",'operations')
        status=self.service.disposal_verification(self.item["id"],"viewer")
        self.assertEqual(status["missing"],["waste"])
        with self.assertRaises(ConflictError) as ctx: self._close(current)
        message=str(ctx.exception)
        self.assertIn("废弃物去向",message); self.assertNotIn("回收数量",message); self.assertNotIn("岸线复查",message)
        self.service.add_disposal_record(self.item["id"],{"kind":"waste","destination":"危废处置中心","transfer_status":"pending","handler":"王五"},"operator",'operations')
        self.assertEqual(self.service.disposal_verification(self.item["id"],"viewer")["missing"],["waste"])
        waste=self._active("waste")[0]
        self.service.correct_disposal_record(waste["id"],{"transfer_status":"completed"},"operator",'operations')
        self.assertEqual(self._close(current)["status"],"closed")
    def test_recovery_ratio_boundary_and_shoreline_latest(self):
        self.service.add_disposal_record(self.item["id"],{"kind":"recovery","quantity":79.9,"handler":"张三"},"operator",'operations')
        self.service.add_disposal_record(self.item["id"],{"kind":"shoreline","result":"abnormal","handler":"李四"},"operator",'operations')
        self.service.add_disposal_record(self.item["id"],{"kind":"waste","destination":"危废处置中心","transfer_status":"completed","handler":"王五"},"operator",'operations')
        status=self.service.disposal_verification(self.item["id"],"viewer")
        self.assertEqual(status["missing"],["recovery","shoreline"])
        self.service.correct_disposal_record(self._active("recovery")[0]["id"],{"quantity":80},"operator",'operations')
        self.service.add_disposal_record(self.item["id"],{"kind":"shoreline","result":"normal","handler":"李四"},"operator",'operations')
        status=self.service.disposal_verification(self.item["id"],"viewer")
        self.assertTrue(status["ok"]); self.assertEqual(status["missing"],[])
        self.assertEqual(status["recovery"]["total"],80); self.assertEqual(status["required_quantity"],80)
    def test_correction_reopens_closed_event_and_keeps_conclusions(self):
        self.service.add_disposal_record(self.item["id"],{"kind":"recovery","quantity":90,"handler":"张三"},"operator",'operations')
        self.service.add_disposal_record(self.item["id"],{"kind":"shoreline","result":"normal","handler":"李四"},"operator",'operations')
        self.service.add_disposal_record(self.item["id"],{"kind":"waste","destination":"危废处置中心","transfer_status":"completed","handler":"王五"},"operator",'operations')
        closed=self._close(self._to_monitoring())
        self.assertEqual(closed["status"],"closed")
        waste=self._active("waste")[0]
        result=self.service.correct_disposal_record(waste["id"],{"destination":"资源化利用厂"},"operator",'operations')
        self.assertTrue(result["reopened"]); self.assertEqual(result["item_status"],"pending_review")
        self.assertEqual(self.service.get_item(self.item["id"],"viewer")["status"],"pending_review")
        outcomes=[c["outcome"] for c in self.service.list_disposal_conclusions(self.item["id"],"viewer")]
        self.assertIn("passed",outcomes); self.assertIn("reopened",outcomes)
        records=self.service.list_disposal_records(self.item["id"],"viewer")
        self.assertTrue(any(r["status"]=="superseded" for r in records))
        self.assertEqual(records[-1]["corrects_id"],waste["id"])
        item=self.service.get_item(self.item["id"],"viewer")
        self.assertEqual(self._close(item)["status"],"closed")
    def test_correction_can_invalidate_closure_conditions(self):
        self.service.add_disposal_record(self.item["id"],{"kind":"recovery","quantity":90,"handler":"张三"},"operator",'operations')
        self.service.add_disposal_record(self.item["id"],{"kind":"shoreline","result":"normal","handler":"李四"},"operator",'operations')
        self.service.add_disposal_record(self.item["id"],{"kind":"waste","destination":"危废处置中心","transfer_status":"completed","handler":"王五"},"operator",'operations')
        self._close(self._to_monitoring())
        self.service.correct_disposal_record(self._active("recovery")[0]["id"],{"quantity":10},"operator",'operations')
        item=self.service.get_item(self.item["id"],"viewer")
        self.assertEqual(item["status"],"pending_review")
        with self.assertRaises(ConflictError) as ctx: self._close(item)
        message=str(ctx.exception)
        self.assertIn("回收数量",message); self.assertNotIn("岸线复查",message)
    def test_validation_and_permissions(self):
        with self.assertRaises(PermissionDenied): self.service.add_disposal_record(self.item["id"],{"kind":"recovery","quantity":1,"handler":"张三"},"viewer",'viewer')
        with self.assertRaises(ValidationError): self.service.add_disposal_record(self.item["id"],{"kind":"recovery","handler":"张三"},"operator",'operations')
        with self.assertRaises(ValidationError): self.service.add_disposal_record(self.item["id"],{"kind":"recovery","quantity":1},"operator",'operations')
        with self.assertRaises(ValidationError): self.service.add_disposal_record(self.item["id"],{"kind":"shoreline","result":"unknown","handler":"李四"},"operator",'operations')
        with self.assertRaises(ValidationError): self.service.add_disposal_record(self.item["id"],{"kind":"waste","transfer_status":"completed","handler":"王五"},"operator",'operations')
        with self.assertRaises(ValidationError): self.service.add_disposal_record(self.item["id"],{"kind":"waste","destination":"危废处置中心","transfer_status":"done","handler":"王五"},"operator",'operations')
        with self.assertRaises(ValidationError): self.service.add_disposal_record(self.item["id"],{"kind":"other","handler":"王五"},"operator",'operations')
        record=self.service.add_disposal_record(self.item["id"],{"kind":"recovery","quantity":50,"handler":"张三"},"operator",'operations')
        self.service.correct_disposal_record(record["id"],{"quantity":60},"operator",'operations')
        with self.assertRaises(ConflictError): self.service.correct_disposal_record(record["id"],{"quantity":70},"operator",'operations')
        with self.assertRaises(PermissionDenied): self.service.correct_disposal_record(self._active("recovery")[0]["id"],{"quantity":70},"viewer",'viewer')
    def test_disposal_api_routes(self):
        self.service.add_disposal_record(self.item["id"],{"kind":"recovery","quantity":80,"handler":"张三"},"operator",'operations')
        status,payload=disposal_api.handle(self.service,"GET",f"/api/items/{self.item['id']}/disposal-records",{},"demo","viewer")
        self.assertEqual(status,200); self.assertEqual(len(payload["records"]),1)
        status,payload=disposal_api.handle(self.service,"GET",f"/api/items/{self.item['id']}/disposal-verification",{},"demo","viewer")
        self.assertEqual(status,200); self.assertEqual(payload["missing"],["shoreline","waste"])
        status,payload=disposal_api.handle(self.service,"GET",f"/api/items/{self.item['id']}/disposal-conclusions",{},"demo","viewer")
        self.assertEqual(status,200); self.assertEqual(payload["conclusions"],[])
        record_id=payload and self._active("recovery")[0]["id"]
        status,payload=disposal_api.handle(self.service,"POST",f"/api/disposal-records/{record_id}/correct",{"quantity":55},"operator","operations")
        self.assertEqual(status,200); self.assertFalse(payload["reopened"]); self.assertEqual(payload["record"]["quantity"],55)
        self.assertIsNone(disposal_api.handle(self.service,"GET","/api/items",{},"demo","viewer"))
        self.assertIsNone(disposal_api.handle(self.service,"DELETE",f"/api/items/{self.item['id']}/disposal-records",{},"demo","viewer"))
if __name__=="__main__": unittest.main()
