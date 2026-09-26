import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, NotFound, PermissionDenied, ValidationError


CREATE_DATA = {'monthly_income': 18000.0, 'monthly_expenses': 9000.0, 'monthly_payment': 7000.0, 'arrears': 12000.0, 'hardship_factor': 0.5, 'program_type': 'reduction', 'requested_months': 9}


class BranchScopeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def _create(self, org="branch-a", reference="MORT-27001"):
        return self.service.create(Actor("creator", "intake_officer", org), reference, CREATE_DATA)

    def test_records_scoped_to_own_branch(self):
        record = self._create()
        self.assertEqual(record["branch"], "branch-a")
        outsider = Actor("outsider", "intake_officer", "branch-b")
        self.assertEqual(len(self.service.list_records(Actor("staff", "intake_officer", "branch-a"))), 1)
        self.assertEqual(self.service.list_records(outsider), [])
        with self.assertRaises(NotFound):
            self.service.get_record(outsider, record["id"])
        with self.assertRaises(NotFound):
            self.service.timeline(outsider, record["id"])
        with self.assertRaises(NotFound):
            self.service.act(outsider, record["id"], record["version"], "assess", {"assessment_note": "越权"})
        self.assertEqual(self.service.stats(Actor("admin-b", "admin", "branch-b")), {})
        self.assertEqual(self.service.stats(Actor("admin-a", "admin", "branch-a")), {"submitted": 1})

    def test_transfer_flow_moves_ownership_after_confirm(self):
        record = self._create()
        admin_a = Actor("admin-a", "admin", "branch-a")
        admin_b = Actor("admin-b", "admin", "branch-b")
        with self.assertRaises(PermissionDenied):
            self.service.initiate_transfer(Actor("staff", "intake_officer", "branch-a"), record["id"], {"to_branch": "branch-b", "reason": "借款人搬迁"})
        transfer = self.service.initiate_transfer(admin_a, record["id"], {"to_branch": "branch-b", "reason": "借款人搬迁"})
        self.assertEqual(transfer["status"], "pending")
        self.assertEqual(transfer["from_branch"], "branch-a")
        self.assertEqual(transfer["to_branch"], "branch-b")
        self.assertEqual(transfer["reason"], "借款人搬迁")
        self.assertEqual(transfer["initiated_by"], "admin-a")
        with self.assertRaises(Conflict):
            self.service.initiate_transfer(admin_a, record["id"], {"to_branch": "branch-c", "reason": "重复发起"})
        # 确认前原分行继续办理
        record = self.service.act(Actor("operator", "intake_officer", "branch-a"), record["id"], record["version"], "assess", {"assessment_note": "收入波动"})
        self.assertEqual(record["state"], "assessed")
        incoming = self.service.incoming_transfers(admin_b)
        self.assertEqual([item["id"] for item in incoming], [transfer["id"]])
        self.assertEqual(incoming[0]["reference"], "MORT-27001")
        with self.assertRaises(PermissionDenied):
            self.service.incoming_transfers(Actor("staff-b", "servicer", "branch-b"))
        with self.assertRaises(PermissionDenied):
            self.service.confirm_transfer(Actor("admin-c", "admin", "branch-c"), transfer["id"])
        with self.assertRaises(PermissionDenied):
            self.service.confirm_transfer(Actor("staff-b", "servicer", "branch-b"), transfer["id"])
        # 目标分行确认后归属与待办切换
        confirmed = self.service.confirm_transfer(admin_b, transfer["id"])
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(confirmed["confirmed_by"], "admin-b")
        with self.assertRaises(Conflict):
            self.service.confirm_transfer(admin_b, transfer["id"])
        with self.assertRaises(NotFound):
            self.service.get_record(admin_a, record["id"])
        self.assertEqual(self.service.list_records(admin_a), [])
        moved = self.service.get_record(admin_b, record["id"])
        self.assertEqual(moved["branch"], "branch-b")
        moved = self.service.act(Actor("uw", "underwriter", "branch-b"), record["id"], moved["version"], "approve", {"exception_approved": False})
        self.assertEqual(moved["state"], "approved")
        # 转办记录保留新旧分行、原因和经办人
        history = self.service.list_transfers(admin_b, record["id"])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["from_branch"], "branch-a")
        self.assertEqual(history[0]["to_branch"], "branch-b")
        self.assertEqual(history[0]["reason"], "借款人搬迁")
        self.assertEqual(history[0]["initiated_by"], "admin-a")
        self.assertEqual(history[0]["confirmed_by"], "admin-b")
        actions = [event["action"] for event in self.service.timeline(admin_b, record["id"])]
        self.assertIn("transfer_initiated", actions)
        self.assertIn("transfer_confirmed", actions)

    def test_transfer_validation(self):
        record = self._create()
        admin_a = Actor("admin-a", "admin", "branch-a")
        with self.assertRaises(ValidationError):
            self.service.initiate_transfer(admin_a, record["id"], {"to_branch": "branch-a", "reason": "同分行"})
        with self.assertRaises(ValidationError):
            self.service.initiate_transfer(admin_a, record["id"], {"to_branch": "branch-b", "reason": ""})
        with self.assertRaises(NotFound):
            self.service.initiate_transfer(Actor("admin-b", "admin", "branch-b"), record["id"], {"to_branch": "branch-c", "reason": "跨分行发起"})

    def test_concurrent_confirm_first_wins(self):
        record = self._create()
        transfer = self.service.initiate_transfer(Actor("admin-a", "admin", "branch-a"), record["id"], {"to_branch": "branch-b", "reason": "辖区调整"})
        results = []

        def confirm(user_id):
            try:
                self.service.confirm_transfer(Actor(user_id, "admin", "branch-b"), transfer["id"])
                results.append("ok")
            except Conflict:
                results.append("conflict")

        threads = [threading.Thread(target=confirm, args=("admin-b%d" % i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results), ["conflict", "ok"])
        history = self.service.list_transfers(Actor("admin-b", "admin", "branch-b"), record["id"])
        self.assertEqual(history[0]["status"], "confirmed")


if __name__ == "__main__":
    unittest.main()
