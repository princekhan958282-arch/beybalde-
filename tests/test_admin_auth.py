"""Tests for multi-admin authentication and master owner privileges."""

import unittest
from types import SimpleNamespace

from cogs.admin import actions as A
from cogs.tournament import tournament as TM
from cogs.community import panel as CP
from cogs.updates import cog as UC
from cogs.updates import update_panel as UP
from cogs.codes import backup as BC
from cogs.core import onboarding as ONB


class TestAdminAuth(unittest.TestCase):
    def setUp(self):
        self.master_user = SimpleNamespace(id=956773141265391676, roles=[])
        self.new_admin_user = SimpleNamespace(id=1006545028614729808, roles=[])
        self.random_user = SimpleNamespace(id=123456789012, roles=[])

    def test_admin_ids_contains_both(self):
        self.assertIn(956773141265391676, A.ADMIN_IDS)
        self.assertIn(1006545028614729808, A.ADMIN_IDS)
        self.assertIn(956773141265391676, TM.ADMIN_IDS)
        self.assertIn(1006545028614729808, TM.ADMIN_IDS)
        self.assertIn(956773141265391676, BC.ADMIN_IDS)
        self.assertIn(1006545028614729808, BC.ADMIN_IDS)

    def test_is_admin_check(self):
        self.assertTrue(A.is_admin(self.master_user))
        self.assertTrue(A.is_admin(self.new_admin_user))
        self.assertFalse(A.is_admin(self.random_user))

    def test_may_run_owner_only_action(self):
        action = SimpleNamespace(owner_only=True)
        self.assertTrue(A.may_run(action, self.master_user))
        self.assertTrue(A.may_run(action, self.new_admin_user))
        self.assertFalse(A.may_run(action, self.random_user))

    def test_is_tournament_admin(self):
        self.assertTrue(TM.is_tournament_admin(self.master_user))
        self.assertTrue(TM.is_tournament_admin(self.new_admin_user))
        self.assertFalse(TM.is_tournament_admin(self.random_user))

    def test_community_and_updates_owner(self):
        self.assertTrue(CP.is_owner(self.master_user))
        self.assertTrue(CP.is_owner(self.new_admin_user))
        self.assertFalse(CP.is_owner(self.random_user))

        self.assertTrue(UC._is_owner(self.master_user))
        self.assertTrue(UC._is_owner(self.new_admin_user))
        self.assertFalse(UC._is_owner(self.random_user))

        self.assertTrue(UP._is_owner(self.master_user))
        self.assertTrue(UP._is_owner(self.new_admin_user))
        self.assertFalse(UP._is_owner(self.random_user))

    def test_maintenance_bypass_for_admins(self):
        # Admin is not blocked during maintenance
        A.set_maintenance(True, "Maintenance test", 956773141265391676)
        try:
            self.assertIsNone(ONB.blocked_reason(self.master_user))
            self.assertIsNone(ONB.blocked_reason(self.new_admin_user))
            # Normal user is blocked
            blocked = ONB.blocked_reason(self.random_user)
            self.assertIsNotNone(blocked)
            self.assertIsInstance(blocked, ONB.UnderMaintenance)
        finally:
            A.set_maintenance(False, "", 956773141265391676)


if __name__ == "__main__":
    unittest.main()
