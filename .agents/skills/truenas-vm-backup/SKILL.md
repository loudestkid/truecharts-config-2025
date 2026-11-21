---
name: truenas-vm-backup
description: >-
  Backs up TrueNAS SCALE VM configuration, ZFS zvol state, and update train metadata
  over SSH into the repository before TrueNAS OS upgrades, and documents a speculative
  (untested) restore workflow if a VM definition is lost during an upgrade.
---

# TrueNAS VM Backup & Recovery Skill

This skill captures the exact configuration of the TrueNAS SCALE VM hosting the Kubernetes cluster (`talos` on `192.168.1.80`) before an OS upgrade, and provides a speculative (untested) recovery procedure if an upgrade drops or corrupts the VM definition.

---

## When to Activate

Activate this skill when:
- Preparing for a TrueNAS SCALE OS upgrade (e.g., `24.10` → `25.04.2+` → `25.10`).
- Refreshing the stored VM configuration snapshot in `clusters/main/talos/truenas-vm-backup.json`.
- Recovering a lost or unmigrated VM definition after a TrueNAS upgrade while keeping its existing ZFS `zvol`.

---

## Step 1: Capture Current VM & Pool State (Tested)

Run the backup script (requires SSH access to `admin@192.168.1.80`; in sandboxed agent environments, SSH requires `BypassSandbox: true`):

```bash
python3 ./scripts/truenas-vm-backup.py backup
```

This queries TrueNAS via `midclt` and `/usr/sbin/zfs` / `/usr/sbin/zpool` and writes a redacted snapshot to:
- `clusters/main/talos/truenas-vm-backup.json`

What it records:
- TrueNAS version (`system.version`) and available/selected update trains (`update.get_trains`).
- ZFS pool health and free space (`boot-pool`, `main`).
- `main/talos-zvol` size, usage, and existing ZFS snapshots.
- Full VM definition (`midclt call vm.query`): CPU, memory, UEFI/OVMF, autostart, `VIRTIO` disk path, `VIRTIO` NIC MAC & bridge (`br001`), and display settings (with SPICE password replaced by `REDACTED_ON_BACKUP`).

---

## Step 2: Pre-Upgrade Host Preparation (Manual / Operator-Confirmed)

Before initiating a TrueNAS OS upgrade in the Web UI:
1. **Export TrueNAS Config:** Download the full TrueNAS configuration + secret seed from **System → General Settings → Manage Configuration**.
2. **Shut Down the VM Cleanly:** Stop the `talos` VM before taking the ZFS snapshot and upgrading.
3. **Create a ZFS Snapshot of the VM Disk:**
   ```bash
   ssh admin@192.168.1.80 "/usr/sbin/zfs snapshot main/talos-zvol@pre-upgrade-$(date +%Y%m%d)"
   ```
4. **Follow Safe TrueNAS Upgrade Path:**
   - If on `24.10`, upgrade directly to **`25.04.2` or later** (never stop on `25.04.0`/`25.04.1`) so classic VMs auto-migrate.
   - Verify the VM on `25.04.x`, then upgrade to **`25.10.x` (Goldeye)**.

---

## Step 3: Speculative VM Recreation (`restore`) — **UNTESTED**

> [!WARNING]
> **UNTESTED WORKFLOW:** The `restore` subcommand in `scripts/truenas-vm-backup.py` and the `midclt` calls below have **NOT** been tested against a live TrueNAS `25.04+` or `25.10+` system. TrueNAS API schemas for `vm.create` and `vm.device.create` may reject or require different fields across major versions. Always run in dry-run mode first and verify against `midclt call vm.create --help` or the TrueNAS Web UI.

If a TrueNAS upgrade loses the VM definition while leaving `/dev/zvol/main/talos-zvol` intact:

1. **Preview the generated `midclt` commands (safe dry-run):**
   ```bash
   python3 ./scripts/truenas-vm-backup.py restore
   ```
2. **Verify prerequisites on TrueNAS:**
   - Confirm no conflicting VM named `talos` exists (`midclt call vm.query`).
   - Confirm `/dev/zvol/main/talos-zvol` is present.
   - Confirm bridge `br001` exists.
3. **Attempt automated recreation (UNTESTED — ask user before running):**
   ```bash
   python3 ./scripts/truenas-vm-backup.py restore --apply --spice-password "<new-password>"
   ```
   If `midclt` rejects any field due to `25.04`/`25.10` schema changes, fall back to creating the VM manually in **Virtual Machines → Add** using the exact values recorded in `clusters/main/talos/truenas-vm-backup.json` (UEFI, 1 socket / 5 cores / 2 threads, 65536 MiB RAM, existing disk `/dev/zvol/main/talos-zvol` as `VIRTIO`, NIC `VIRTIO` with MAC `00:a0:98:41:4f:cf` on `br001`).
