import unittest
from contextlib import nullcontext
from unittest.mock import patch

import mongomock

from mongo_cursor import MongoCursor
from mongo_repository import MongoRepository


class MongoRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.client = mongomock.MongoClient()
        self.repository = MongoRepository(client=self.client, database_name="runtime_test")
        self.repository.ensure_indexes()

    def test_user_balance_and_restart_persistence(self):
        self.repository.ensure_user(101)
        self.repository.update_balance(101, 250)
        self.assertEqual(self.repository.get_user(101)["balance"], 250)

        restarted = MongoRepository(client=self.client, database_name="runtime_test")
        self.assertEqual(restarted.get_user(101)["balance"], 250)

    def test_stock_filters_and_atomic_claim(self):
        self.repository.db.stock.insert_many([
            {"_id": "good", "phone": "good", "country_name": "India", "account_year": 2026,
             "category": "Good", "available": 1, "twofa": "None"},
            {"_id": "spam", "phone": "spam", "country_name": "India", "account_year": 2025,
             "category": "spam", "available": 1, "twofa": "pass"},
        ])
        claimed = self.repository.claim_stock_account("nonspam", country="India")
        self.assertEqual(claimed["phone"], "good")
        self.assertIsNone(self.repository.claim_stock_account("nonspam", country="India"))
        self.assertEqual(self.repository.claim_stock_account("spam", country="India")["phone"], "spam")

    def test_manual_stock_survives_repository_restart(self):
        self.repository.upsert_stock_account({
            "phone": "restart-stock",
            "session_file": "sessions/restart-stock.session",
            "country_name": "India",
            "country_icon": "",
            "account_year": 2026,
            "category": "Good",
            "price": 100,
            "available": 1,
            "twofa": "None",
        })

        restarted = MongoRepository(client=self.client, database_name="runtime_test")
        account = restarted.db.stock.find_one({"phone": "restart-stock"})
        self.assertIsNotNone(account)
        self.assertEqual(account["category"], "Good")
        self.assertEqual(account["country_name"], "India")
        self.assertEqual(account["account_year"], 2026)
        self.assertEqual(account["available"], 1)
        self.assertIn("added_date", account)

    def test_stock_category_filters_preserve_country_year_and_availability(self):
        self.repository.db.stock.insert_many([
            {"_id": "clean-india", "phone": "clean-india", "country_name": "India", "account_year": 2026,
             "category": "Good", "available": 1, "twofa": "None"},
            {"_id": "clean-brazil", "phone": "clean-brazil", "country_name": "Brazil", "account_year": 2025,
             "category": "Good", "available": 1, "twofa": "None"},
            {"_id": "spam-india", "phone": "spam-india", "country_name": "India", "account_year": 2026,
             "category": "spam", "available": 1, "twofa": "None"},
            {"_id": "spam-brazil", "phone": "spam-brazil", "country_name": "Brazil", "account_year": 2025,
             "category": "spam", "available": 1, "twofa": "None"},
            {"_id": "unavailable-clean", "phone": "unavailable-clean", "country_name": "India", "account_year": 2026,
             "category": "Good", "available": 0, "twofa": "None"},
        ])

        cursor = MongoCursor(self.repository)
        nonspam_where = "available=1 AND category IS NOT NULL AND LOWER(category) != 'spam'"
        spam_where = "available=1 AND LOWER(category) = 'spam'"
        self.assertEqual(
            {row[0] for row in cursor.execute(f"SELECT phone FROM stock WHERE {nonspam_where}").fetchall()},
            {"clean-india", "clean-brazil"},
        )
        self.assertEqual(
            {row[0] for row in cursor.execute(f"SELECT phone FROM stock WHERE {spam_where}").fetchall()},
            {"spam-india", "spam-brazil"},
        )
        self.assertEqual(
            {row[0] for row in cursor.execute(f"SELECT phone FROM stock WHERE {nonspam_where} AND country_name=? AND account_year=?", ("India", 2026)).fetchall()},
            {"clean-india"},
        )
        self.assertEqual(
            {row[0] for row in cursor.execute(f"SELECT phone FROM stock WHERE {spam_where} AND country_name=? AND account_year=?", ("Brazil", 2025)).fetchall()},
            {"spam-brazil"},
        )

    def test_settings_and_lzt_settings_persist(self):
        self.repository.set_setting("bot_mode", "hybrid")
        self.repository.set_setting("lzt_api_key", "stored-key")
        restarted = MongoRepository(client=self.client, database_name="runtime_test")
        self.assertEqual(restarted.get_setting("bot_mode"), "hybrid")
        self.assertEqual(restarted.get_setting("lzt_api_key"), "stored-key")

    def test_manual_deposit_is_pending_and_callback_id_is_normalized(self):
        self.repository.ensure_user(101)
        deposit, created = self.repository.create_manual_deposit(
            101, 250, "ManualUPI", "file-1", 101, 9001,
        )

        self.assertTrue(created)
        self.assertEqual(deposit["status"], "pending")
        self.assertEqual(deposit["_id"], deposit["id"])
        self.assertEqual(deposit["source_chat_id"], 101)
        self.assertEqual(deposit["source_message_id"], 9001)
        self.assertIsNone(deposit.get("processed"))
        stored = self.repository.get_deposit(str(deposit["id"]))
        self.assertEqual(stored["_id"], deposit["id"])
        self.assertEqual(stored["user_id"], 101)
        self.assertEqual(stored["amount"], 250)
        self.assertEqual(stored["status"], "pending")

    def test_auto_upi_order_contains_verification_contract(self):
        from datetime import timedelta

        self.repository.ensure_user(101)
        expires_at = self.repository._now() + timedelta(minutes=10)
        order = self.repository.create_auto_upi_order(
            101, 100, 100, "ORD20260924ABC12345", expires_at,
        )

        self.assertEqual(order["user_id"], 101)
        self.assertEqual(order["base_amount"], 100)
        self.assertEqual(order["payable_amount"], 100)
        self.assertEqual(order["purpose"], "ORD20260924ABC12345")
        self.assertEqual(order["status"], "pending")
        self.assertIsNotNone(order["created_at"])
        self.assertEqual(order["expires_at"], expires_at)
        stored = self.repository.db.upi_orders.find_one({"_id": "ORD20260924ABC12345"})
        self.assertEqual(stored["user_id"], order["user_id"])
        self.assertEqual(stored["base_amount"], order["base_amount"])
        self.assertEqual(stored["payable_amount"], order["payable_amount"])
        self.assertEqual(stored["purpose"], order["purpose"])
        self.assertEqual(stored["status"], order["status"])

    def test_auto_upi_completion_requires_exact_amount_and_credits_once(self):
        from datetime import timedelta

        self.repository.ensure_user(505)
        order = self.repository.create_auto_upi_order(
            505, 75, 75, "ORD20260924EXACT001", self.repository._now() + timedelta(minutes=10),
        )

        wrong_amount = self.repository.complete_auto_upi_order(
            order["_id"], {"amount": 74, "email_msg_id": "<wrong@example.com>"},
        )
        self.assertTrue(wrong_amount["already_processed"])
        self.assertEqual(self.repository.get_user(505)["balance"], 0)

        completed = self.repository.complete_auto_upi_order(
            order["_id"], {"amount": 75, "email_msg_id": "<payment@example.com>"},
        )
        repeated = self.repository.complete_auto_upi_order(
            order["_id"], {"amount": 75, "email_msg_id": "<payment@example.com>"},
        )
        self.assertTrue(completed["credited"])
        self.assertTrue(repeated["already_processed"])
        self.assertEqual(completed["previous_balance"], 0)
        self.assertEqual(completed["balance"], 75)
        self.assertEqual(self.repository.get_user(505)["balance"], 75)
        stored = self.repository.db.upi_orders.find_one({"_id": order["_id"]})
        self.assertEqual(stored["verification_status"], "AUTO VERIFIED")
        self.assertEqual(stored["previous_balance"], 0)
        self.assertEqual(stored["balance"], 75)

    def test_auto_upi_expired_order_cannot_be_completed(self):
        from datetime import timedelta

        self.repository.ensure_user(606)
        order = self.repository.create_auto_upi_order(
            606, 80, 80, "ORD20260924EXPIRED1", self.repository._now() - timedelta(seconds=1),
        )

        expired = self.repository.expire_auto_upi_order(order["_id"])
        result = self.repository.complete_auto_upi_order(
            order["_id"], {"amount": 80, "email_msg_id": "<late@example.com>"},
        )
        self.assertEqual(expired["status"], "expired")
        self.assertTrue(result["already_processed"])
        self.assertEqual(self.repository.get_user(606)["balance"], 0)

    def test_manual_deposit_accept_and_custom_amount_are_one_time(self):
        self.repository.ensure_user(101)
        exact, _ = self.repository.create_manual_deposit(101, 250, "ManualUPI", "file-1", 101, 9001)
        custom, _ = self.repository.create_manual_deposit(101, 300, "ManualUPI", "file-2", 101, 9002)

        with patch.object(self.repository, "transaction", return_value=nullcontext(None)):
            accepted = self.repository.approve_deposit(str(exact["id"]), 250)
            duplicate_accept = self.repository.approve_deposit(exact["id"], 250)
            custom_approved = self.repository.approve_deposit(custom["id"], 275)

        self.assertTrue(accepted["approved"])
        self.assertTrue(duplicate_accept["already_processed"])
        self.assertTrue(custom_approved["approved"])
        self.assertEqual(self.repository.db.deposits.find_one({"_id": exact["id"]})["status"], "approved")
        self.assertEqual(self.repository.db.deposits.find_one({"_id": custom["id"]})["amount"], 275)
        self.assertEqual(self.repository.get_user(101)["balance"], 525)

    def test_pending_deposit_accept_credits_balance_and_approves(self):
        self.repository.ensure_user(303)
        self.repository.update_balance(303, 100)
        deposit, _ = self.repository.create_manual_deposit(
            303, 50, "ManualUPI", "file-3", 303, 9003,
        )

        accepted = self.repository.approve_deposit(deposit["id"], deposit["amount"])
        duplicate = self.repository.approve_deposit(deposit["id"], deposit["amount"])

        self.assertTrue(accepted["approved"])
        self.assertEqual(self.repository.get_user(303)["balance"], 150)
        self.assertEqual(self.repository.get_deposit(deposit["id"])["status"], "approved")
        self.assertTrue(duplicate["already_processed"])
        self.assertEqual(self.repository.get_user(303)["balance"], 150)

    def test_failed_deposit_credit_stays_pending(self):
        deposit, _ = self.repository.create_manual_deposit(
            404, 50, "ManualUPI", "file-4", 404, 9004,
        )

        with self.assertRaisesRegex(ValueError, "does not exist"):
            self.repository.approve_deposit(deposit["id"], deposit["amount"])

        self.assertEqual(self.repository.get_deposit(deposit["id"])["status"], "pending")

    def test_manual_deposit_reject_is_one_time_and_isolated(self):
        self.repository.ensure_user(101)
        self.repository.ensure_user(202)
        rejected, _ = self.repository.create_manual_deposit(101, 250, "ManualUPI", "file-1", 101, 9001)
        other, _ = self.repository.create_manual_deposit(202, 400, "ManualUPI", "file-2", 202, 9002)

        with patch.object(self.repository, "transaction", return_value=nullcontext(None)):
            rejection = self.repository.reject_deposit(str(rejected["id"]))
            duplicate_rejection = self.repository.reject_deposit(rejected["id"])

        self.assertTrue(rejection["rejected"])
        self.assertTrue(duplicate_rejection["already_processed"])
        self.assertEqual(self.repository.get_deposit(rejected["id"])["status"], "rejected")
        self.assertEqual(self.repository.get_deposit(other["id"])["status"], "pending")
        self.assertEqual(self.repository.get_user(101)["balance"], 0)
        self.assertEqual(self.repository.get_user(202)["balance"], 0)


if __name__ == "__main__":
    unittest.main()
