Equipping a Disk or Driver from another owned Bey now exchanges the two parts in that slot. Both Main Frames must accept their incoming part. The swap runs inside the existing locked profile mutation, preserves physical item IDs, and leaves the other slot and progression unchanged.

Use `;equippart <name or physical ID>` or the inventory equip control. Name lookup prefers the active part, then an available spare, then an attached copy. Physical IDs choose an exact copy. `;myparts` shows the holder's name and copy ID. Equipping an available spare returns the previous part to available inventory; re-equipping the same physical part does nothing.

Unequipping restores an available bundled stock part. If that stock part is on another Bey, use the equip command to swap it back first. This keeps selling and automatic removal from moving a sold part to another Bey.

Regression coverage checks exchanges between duplicate copies and different Beys, physical item conservation, other-slot preservation, repeated equips, spare replacement, blocked reverse compatibility, and unchanged loadout statistics after selection.
