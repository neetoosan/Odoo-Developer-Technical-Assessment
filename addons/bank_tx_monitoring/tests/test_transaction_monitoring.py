from datetime import datetime, timedelta
from unittest.mock import patch

from psycopg2 import IntegrityError

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger

from odoo.addons.bank_tx_monitoring.models.bank_transaction import BankTransaction


@tagged("post_install", "-at_install")
class TestTransactionMonitoring(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Transaction = cls.env["bank.transaction"]
        cls.ngn = cls.env.ref("base.NGN")
        cls.ada = cls.env["res.partner"].create({"name": "Ada Okafor"})
        cls.bayo = cls.env["res.partner"].create({"name": "Bayo Adeyemi"})
        cls.start = datetime(2026, 1, 15, 9, 0, 0)
        cls._counter = 0

    def _make_tx(self, amount=50_000, partner=None, minutes=0, **extra):
        type(self)._counter += 1
        return self.Transaction.create({
            "name": f"TX-{self._counter:05d}",
            "partner_id": (partner or self.ada).id,
            "amount": amount,
            "currency_id": self.ngn.id,
            "transaction_date": self.start + timedelta(minutes=minutes),
            **extra,
        })

    def _run(self):
        return self.Transaction._run_monitoring(auto_commit=False)

    def test_high_value_is_flagged(self):
        at_threshold = self._make_tx(amount=10_000_000)
        above = self._make_tx(amount=25_000_000, partner=self.bayo)
        just_below = self._make_tx(amount=9_999_999.99, minutes=30)
        self._run()

        self.assertEqual(at_threshold.state, "flagged")
        self.assertTrue(at_threshold.is_high_value)
        self.assertIn("High value", at_threshold.flag_reason)
        self.assertEqual(above.state, "flagged")
        self.assertEqual(just_below.state, "processed")
        self.assertFalse(just_below.is_high_value)

    def test_sixth_transaction_within_ten_minutes_is_flagged(self):
        txs = [self._make_tx(minutes=m) for m in (0, 1, 2, 3, 4, 9)]
        self._run()

        self.assertEqual({tx.state for tx in txs[:5]}, {"processed"})
        self.assertEqual(txs[5].state, "flagged")
        self.assertTrue(txs[5].is_high_frequency)
        self.assertIn("6 transactions within 10 minutes", txs[5].flag_reason)

    def test_six_transactions_spread_over_more_than_ten_minutes_are_normal(self):
        txs = [self._make_tx(minutes=m * 3) for m in range(6)]
        self._run()
        self.assertEqual({tx.state for tx in txs}, {"processed"})

    def test_frequency_is_counted_per_customer(self):
        txs = [self._make_tx(partner=self.ada if i % 2 else self.bayo, minutes=i) for i in range(10)]
        self._run()
        self.assertEqual({tx.state for tx in txs}, {"processed"})

    def test_frequency_counts_transactions_from_earlier_runs(self):
        first = [self._make_tx(minutes=m) for m in range(5)]
        self._run()
        late = self._make_tx(minutes=6)
        self._run()

        self.assertEqual({tx.state for tx in first}, {"processed"})
        self.assertEqual(late.state, "flagged")

    def test_both_rules_can_apply(self):
        for m in range(5):
            self._make_tx(minutes=m)
        tx = self._make_tx(amount=12_000_000, minutes=5)
        self._run()
        self.assertTrue(tx.is_high_value)
        self.assertTrue(tx.is_high_frequency)

    def test_normal_transaction_is_processed_once(self):
        tx = self._make_tx()
        self.assertEqual(self._run(), 1)
        self.assertEqual(tx.state, "processed")
        first_processed_on = tx.processed_on

        self.assertEqual(self._run(), 0, "A second run must not pick the transaction up again")
        self.assertEqual(tx.processed_on, first_processed_on)

    @mute_logger("odoo.sql_db")
    def test_duplicate_reference_is_rejected(self):
        self._make_tx(name="BANK-REF-1")
        with self.assertRaises(IntegrityError), self.cr.savepoint():
            self._make_tx(name="BANK-REF-1", partner=self.bayo)

    def test_large_volume_is_processed_in_batches(self):
        self.env["ir.config_parameter"].sudo().set_param("bank_tx_monitoring.batch_size", "7")
        partners = self.env["res.partner"].create([{"name": f"Customer {i}"} for i in range(50)])
        txs = self.Transaction.create([
            {
                "name": f"BULK-{i:04d}",
                "partner_id": partners[i % 50].id,
                "amount": 1_000 + i,
                "transaction_date": self.start + timedelta(seconds=i),
            }
            for i in range(100)
        ])
        self.assertEqual(self._run(), 100)
        self.assertEqual(set(txs.mapped("state")), {"processed"})

    @mute_logger("odoo.addons.bank_tx_monitoring.models.bank_transaction")
    def test_one_bad_transaction_does_not_block_the_others(self):
        good_before = self._make_tx(minutes=0)
        bad = self._make_tx(minutes=1, partner=self.bayo)
        good_after = self._make_tx(minutes=2, partner=self.env["res.partner"].create({"name": "Chi"}))

        original = BankTransaction._evaluate_transaction

        def broken_for_one(record, *args, **kwargs):
            if record == bad:
                raise ValueError("Simulated failure")
            return original(record, *args, **kwargs)

        with patch.object(BankTransaction, "_evaluate_transaction", broken_for_one):
            self._run()

        self.assertEqual(good_before.state, "processed")
        self.assertEqual(good_after.state, "processed")
        self.assertEqual(bad.state, "failed")
        self.assertIn("Simulated failure", bad.error_message)

        bad.action_retry()
        self._run()
        self.assertEqual(bad.state, "processed")
        self.assertFalse(bad.error_message)

    def test_invalid_setting_falls_back_to_default(self):
        self.env["ir.config_parameter"].sudo().set_param("bank_tx_monitoring.high_value_threshold", "ten million")
        with mute_logger("odoo.addons.bank_tx_monitoring.models.bank_transaction"):
            config = self.Transaction._get_monitoring_config()
        self.assertEqual(config["high_value_threshold"], 10_000_000)

    def test_checked_transaction_cannot_be_altered_or_deleted(self):
        tx = self._make_tx()
        self._run()
        with self.assertRaises(UserError):
            tx.amount = 1
        with self.assertRaises(UserError):
            tx.unlink()

    def test_reviewer_can_approve_flagged_transaction(self):
        tx = self._make_tx(amount=15_000_000)
        self._run()
        tx.review_note = "Customer confirmed by phone."
        tx.action_approve()

        self.assertEqual(tx.state, "approved")
        self.assertEqual(tx.reviewed_by_id, self.env.user)
        self.assertTrue(tx.reviewed_on)

    def test_only_flagged_transactions_can_be_reviewed(self):
        tx = self._make_tx()
        self._run()
        with self.assertRaises(UserError):
            tx.action_reject()
