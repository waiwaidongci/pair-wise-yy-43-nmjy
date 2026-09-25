import tempfile, unittest
from pathlib import Path
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES
class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.repo=Repository(str(Path(self.tmp.name)/"test.db")); self.service=Service(self.repo)
    def tearDown(self): self.repo.close(); self.tmp.cleanup()
    def test_complete_workflow_and_audit(self):
        item=self.service.create_item({"title":"workflow item","description":"complete business flow","severity":'major',"quantity":12,"threshold":6,"external_ref":"WF-1"},"creator",'observer')
        self.assertEqual(item["status"],STATES[0])
        self.service.add_record(item["id"],{"kind":"evidence","detail":"evidence registered","status":"closed","external_ref":"EV-1"},"recorder",'response_commander')
        current=item
        for target in ["assessing","containing","recovering","monitoring"]:
            current=self.service.transition(current["id"],target,current["version"],"reviewer",TRANSITION_ROLES[target][0])
        self.service.add_disposal_record(item["id"],{"kind":"recovery","quantity":10,"handler":"张三"},"operator",'operations')
        self.service.add_disposal_record(item["id"],{"kind":"shoreline","result":"normal","handler":"李四"},"operator",'operations')
        self.service.add_disposal_record(item["id"],{"kind":"waste","destination":"危废处置中心","transfer_status":"completed","handler":"王五"},"operator",'operations')
        current=self.service.transition(current["id"],"closed",current["version"],"commander",TRANSITION_ROLES["closed"][0])
        self.assertEqual(current["status"],"closed")
        recovery=[r for r in self.service.list_disposal_records(item["id"],"viewer") if r["kind"]=="recovery"][0]
        result=self.service.correct_disposal_record(recovery["id"],{"quantity":11},"operator",'operations')
        self.assertTrue(result["reopened"])
        current=self.service.get_item(item["id"],"viewer")
        self.assertEqual(current["status"],"pending_review")
        outcomes=[c["outcome"] for c in self.service.list_disposal_conclusions(item["id"],"viewer")]
        self.assertIn("passed",outcomes); self.assertIn("reopened",outcomes)
        current=self.service.transition(current["id"],"closed",current["version"],"commander",TRANSITION_ROLES["closed"][0])
        self.assertEqual(current["status"],"closed")
        self.assertEqual(len(self.service.list_records(current["id"],"viewer")),1)
        events=self.service.audit("viewer",current["id"]); self.assertGreaterEqual(len(events),len(STATES)+1); self.assertTrue(self.repo.verify_audit_chain())
if __name__=="__main__": unittest.main()
