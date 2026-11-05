"""tests/test_updates_and_parts.py — Unit tests for:
1. Update system rewards & feedback
2. Parts stacking & 54/53 inventory calculation
3. Clean trade/market parts detachment without ghosting
4. WhatsNew and update service creation
"""

import os
import tempfile
import pytest

from utils.character_registry import REGISTRY
from utils.userstore import UserStore
from utils import inventory as INV
from utils import bey_components as BC
from cogs.updates import service as SV
from cogs.updates import store as S


@pytest.fixture
def temp_db(tmp_path):
    db_file = str(tmp_path / "test_users.db")
    store = UserStore(db_file)
    store.ensure_ready()
    return store


def test_update_reward_claims(temp_db):
    upd_id = "upd_test_reward_1"
    user_id = "99887766"

    # First claim should succeed
    first = temp_db.update_claim_reward(upd_id, user_id)
    assert first is True

    # Has claimed should be true
    assert temp_db.update_reward_claimed(upd_id, user_id) is True
    assert temp_db.update_reward_claimed(upd_id, "other_user") is False

    # Second claim attempt for same user should fail (deduplication)
    second = temp_db.update_claim_reward(upd_id, user_id)
    assert second is False


def test_update_feedback_recording(temp_db):
    upd_id = "upd_test_feedback_1"
    temp_db.update_record_feedback(upd_id, "u1", "loved")
    temp_db.update_record_feedback(upd_id, "u2", "loved")
    temp_db.update_record_feedback(upd_id, "u3", "good")
    temp_db.update_record_feedback(upd_id, "u4", "issues")

    summary = temp_db.update_feedback_summary(upd_id)
    assert summary["loved"] == 2
    assert summary["good"] == 1
    assert summary["issues"] == 1
    assert summary["total"] == 4

    # Changing vote replaces previous vote
    temp_db.update_record_feedback(upd_id, "u4", "loved")
    summary2 = temp_db.update_feedback_summary(upd_id)
    assert summary2["loved"] == 3
    assert summary2.get("issues", 0) == 0
    assert summary2["total"] == 4


def test_parts_stacking_logic():
    # Simulate raw parts list with 45 copies of Disk "01" and 5 copies of Driver "Accel"
    raw_parts = []
    for i in range(45):
        raw_parts.append({
            "kind": "part",
            "name": "01",
            "instance_id": f"inst_disk_01_{i}",
            "ptype": "disk",
            "stats": {"hp": 0, "attack": 2, "defense": 5, "stamina": 1},
            "equipped": (i == 0),
            "equipped_on": "bey_inst_1" if i < 3 else None,
        })
    for i in range(10):
        raw_parts.append({
            "kind": "part",
            "name": "Accel",
            "instance_id": f"inst_drv_accel_{i}",
            "ptype": "driver",
            "stats": {"hp": 0, "attack": 3, "defense": 0, "stamina": 4},
            "equipped": False,
            "equipped_on": None,
        })

    stacked = {}
    for p in raw_parts:
        key = p["name"].casefold()
        if key not in stacked:
            stacked[key] = {
                "kind": "part",
                "name": p["name"],
                "instance_id": p.get("instance_id"),
                "ptype": p.get("ptype", "?"),
                "stat": p.get("stat"),
                "stats": p.get("stats"),
                "bonus": p.get("bonus", 0),
                "penalties": p.get("penalties") or {},
                "count": 0,
                "equipped_count": 0,
                "equipped": False,
                "instances": [],
                "available_instances": [],
            }
        item = stacked[key]
        item["count"] += 1
        if p.get("equipped"):
            item["equipped"] = True
        if p.get("equipped_on") or p.get("equipped"):
            item["equipped_count"] += 1
        else:
            if p.get("instance_id"):
                item["available_instances"].append(p["instance_id"])
        if p.get("instance_id"):
            item["instances"].append(p["instance_id"])

    parts = list(stacked.values())
    assert len(parts) == 2  # Only two entries instead of 55!

    disk_item = next(it for it in parts if it["name"] == "01")
    assert disk_item["count"] == 45
    assert disk_item["equipped"] is True
    assert disk_item["equipped_count"] == 3
    assert len(disk_item["available_instances"]) == 42

    drv_item = next(it for it in parts if it["name"] == "Accel")
    assert drv_item["count"] == 10
    assert drv_item["equipped"] is False
    assert drv_item["equipped_count"] == 0
    assert len(drv_item["available_instances"]) == 10


def test_inventory_equipped_54_53_overflow(monkeypatch):
    # Set BASE_INVENTORY_SLOTS to 53 for this test
    monkeypatch.setattr(INV, "BASE_INVENTORY_SLOTS", 53)

    profile = {
        "inventory": [f"Blade_{i}" for i in range(54)], # 54 total blades
        "active_beyblade": "Blade_0",
        "active_bey_instance": "inst_0",
        "marketplace_listings": [],
    }

    # Total used slots including equipped item
    assert INV.used(profile) == 54
    assert INV.capacity(profile) == 53
    # Used slots in storage (excluding the active equipped item)
    assert INV.stored_used(profile) == 53
    # Storage is full for adding new drops (since 53 stored == 53 capacity)
    assert INV.free(profile) == 0
    assert INV.can_add(profile) is False

    # Now if player has 53 items total (52 stored + 1 equipped)
    profile["inventory"].pop()
    assert INV.used(profile) == 53
    assert INV.stored_used(profile) == 52
    assert INV.free(profile) == 1
    assert INV.can_add(profile) is True


def test_clean_detach_bey_and_attach():
    # Setup sender profile with a Beyblade and parts
    blade_name = "Dynamite Belial"
    blade = REGISTRY.find_bey(blade_name)
    assert blade is not None

    sender = {
        "inventory": [blade_name],
        "active_beyblade": blade_name,
        "parts": ["Destroy Driver"], # purchased part
        "equipped_parts": ["Destroy Driver"],
    }
    BC.reconcile(sender)

    # Equip Destroy Driver on Storm Pegasus
    BC.equip(sender, "Destroy Driver")
    inst = sender["bey_instances"][0]
    assert "driver" in inst["parts"]
    attached_driver_id = inst["parts"]["driver"]
    assert any(i["instance_id"] == attached_driver_id for i in sender["part_instances"])

    # Detach the Beyblade (as happens in /trade and ;sellbey)
    bundle = BC.detach_bey(sender, blade_name)
    assert bundle["item"] == blade_name
    assert len(bundle["part_instances"]) == 2  # disk and driver

    # Verify detached parts are completely gone from sender
    sender_instances = [i["instance_id"] for i in sender.get("part_instances", [])]
    for p_inst in bundle["part_instances"]:
        assert p_inst["instance_id"] not in sender_instances

    # Verify purchased part is removed from sender["parts"] and sender["equipped_parts"]
    assert "Destroy Driver" not in sender.get("parts", [])
    assert "Destroy Driver" not in sender.get("equipped_parts", [])

    # Reconciling sender must NOT re-grant the detached part
    BC.reconcile(sender)
    assert not any(i["instance_id"] == attached_driver_id for i in sender.get("part_instances", []))

    # Recipient receives the Bey and parts
    recipient = {"inventory": [], "parts": []}
    BC.attach_bey(recipient, bundle)

    assert blade_name in recipient["inventory"]
    assert any(i["instance_id"] == attached_driver_id for i in recipient["part_instances"])
    assert "Destroy Driver" in recipient["parts"]


def test_service_create_and_metadata(monkeypatch, temp_db):
    from utils import database as DB
    monkeypatch.setattr(DB, "USER_STORE", temp_db)
    row = SV.create(
        event="UPDATE_RELEASED",
        title="v1.56 Mega Balance Patch",
        body="Major improvements to stadiums and abilities.",
        version="v1.56",
        reward={"coins": 1000, "exp": 100},
        broadcast_mode="both",
        fixed_reports=["rep_1234"]
    )
    assert row["update_id"].startswith("upd_")
    upd = S.get_update(row["update_id"])
    assert upd is not None
    assert upd["title"] == "v1.56 Mega Balance Patch"
    aud = upd.get("audience", {})
    assert aud.get("broadcast_mode") == "both"
    assert aud.get("reward") == {"coins": 1000, "exp": 100}
    assert aud.get("fixed_reports") == ["rep_1234"]
