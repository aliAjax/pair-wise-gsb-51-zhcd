import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, NotFound, PermissionDenied, ValidationError


CREATE_DATA = {'monthly_income': 18000.0, 'monthly_expenses': 9000.0, 'monthly_payment': 7000.0, 'arrears': 12000.0, 'hardship_factor': 0.5, 'program_type': 'reduction', 'requested_months': 9}


def admin(org):
    return Actor("admin-" + org, "admin", org)


def officer(org):
    return Actor("officer-" + org, "intake_officer", org)


class TransferTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def _create(self, org="branch-a", reference="MORT-27001"):
        return self.service.create(officer(org), reference, CREATE_DATA)

    def test_records_scoped_to_creator_branch(self):
        record = self._create()
        self.assertEqual(record["org"], "branch-a")
        self._create(org="branch-b", reference="MORT-27002")
        mine = self.service.list_records(officer("branch-a"))
        self.assertEqual([item["reference"] for item in mine], ["MORT-27001"])
        self.assertEqual(self.service.stats(officer("branch-a")), {"submitted": 1})
        with self.assertRaises(NotFound):
            self.service.get_record(officer("branch-b"), record["id"])
        with self.assertRaises(NotFound):
            self.service.timeline(officer("branch-b"), record["id"])
        with self.assertRaises(NotFound):
            self.service.act(officer("branch-b"), record["id"], record["version"], "assess", {"assessment_note": "跨分行"})

    def test_transfer_flow_switches_ownership_on_confirm(self):
        record = self._create()
        with self.assertRaises(PermissionDenied):
            self.service.initiate_transfer(officer("branch-a"), record["id"], "branch-b", "客户搬迁")
        with self.assertRaises(ValidationError):
            self.service.initiate_transfer(admin("branch-a"), record["id"], "branch-a", "目标相同")
        transfer = self.service.initiate_transfer(admin("branch-a"), record["id"], "branch-b", "客户搬迁")
        self.assertEqual(transfer["status"], "pending")
        self.assertEqual(transfer["from_org"], "branch-a")
        self.assertEqual(transfer["to_org"], "branch-b")
        self.assertEqual(transfer["reason"], "客户搬迁")
        self.assertEqual(transfer["initiated_by"], "admin-branch-a")
        with self.assertRaises(Conflict):
            self.service.initiate_transfer(admin("branch-a"), record["id"], "branch-c", "重复转办")
        # 确认前原分行继续办理，目标分行还看不到记录，只能看到转入待办
        record = self.service.act(officer("branch-a"), record["id"], record["version"], "assess", {"assessment_note": "继续办理"})
        self.assertEqual(record["state"], "assessed")
        self.assertEqual(self.service.list_records(officer("branch-b")), [])
        incoming = self.service.incoming_transfers(officer("branch-b"))
        self.assertEqual([item["id"] for item in incoming], [transfer["id"]])
        with self.assertRaises(PermissionDenied):
            self.service.confirm_transfer(officer("branch-b"), transfer["id"])
        with self.assertRaises(PermissionDenied):
            self.service.confirm_transfer(admin("branch-a"), transfer["id"])
        # 目标分行确认后归属与待办切换
        result = self.service.confirm_transfer(admin("branch-b"), transfer["id"])
        self.assertEqual(result["transfer"]["status"], "confirmed")
        self.assertEqual(result["transfer"]["confirmed_by"], "admin-branch-b")
        self.assertEqual(result["record"]["org"], "branch-b")
        with self.assertRaises(NotFound):
            self.service.get_record(officer("branch-a"), record["id"])
        moved = self.service.get_record(officer("branch-b"), record["id"])
        self.assertEqual(moved["org"], "branch-b")
        moved = self.service.act(Actor("uw-b", "underwriter", "branch-b"), moved["id"], moved["version"], "approve", {"exception_approved": False})
        self.assertEqual(moved["state"], "approved")
        # 转办记录保留新旧分行、原因和经办人
        history = self.service.record_transfers(officer("branch-b"), record["id"])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["from_org"], "branch-a")
        self.assertEqual(history[0]["to_org"], "branch-b")
        self.assertEqual(history[0]["reason"], "客户搬迁")
        self.assertEqual(history[0]["initiated_by"], "admin-branch-a")
        self.assertEqual(history[0]["confirmed_by"], "admin-branch-b")
        outgoing = self.service.outgoing_transfers(officer("branch-a"))
        self.assertEqual([item["id"] for item in outgoing], [transfer["id"]])
        actions = [event["action"] for event in self.service.timeline(officer("branch-b"), record["id"])]
        self.assertIn("transfer_initiated", actions)
        self.assertIn("transfer_confirmed", actions)

    def test_second_confirm_is_rejected(self):
        record = self._create()
        transfer = self.service.initiate_transfer(admin("branch-a"), record["id"], "branch-b", "并发确认")
        self.service.confirm_transfer(admin("branch-b"), transfer["id"])
        with self.assertRaises(Conflict):
            self.service.confirm_transfer(Actor("admin-b2", "admin", "branch-b"), transfer["id"])

    def test_concurrent_confirms_single_winner(self):
        record = self._create()
        transfer = self.service.initiate_transfer(admin("branch-a"), record["id"], "branch-b", "并发确认")
        wins = []
        losses = []

        def confirm(user):
            try:
                self.service.confirm_transfer(Actor(user, "admin", "branch-b"), transfer["id"])
                wins.append(user)
            except Conflict:
                losses.append(user)

        threads = [threading.Thread(target=confirm, args=("admin-b1",)), threading.Thread(target=confirm, args=("admin-b2",))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(wins), 1)
        self.assertEqual(len(losses), 1)
        transfer = self.service.repository.get_transfer(transfer["id"])
        self.assertEqual(transfer["status"], "confirmed")
        self.assertEqual(transfer["confirmed_by"], wins[0])
