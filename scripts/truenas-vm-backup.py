#!/usr/bin/env python3
"""Backup and speculative (UNTESTED) restore helper for TrueNAS SCALE VMs.

Captures TrueNAS VM configuration (`midclt call vm.query`), system version,
update trains, and underlying ZFS zvol/snapshot state into a repo-tracked JSON
file (with display passwords redacted).

The `restore` subcommand is SPECULATIVE and UNTESTED against a live TrueNAS
25.04+/25.10+ API. Always run without `--apply` first to inspect the generated
`midclt` calls.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

DEFAULT_HOST = "192.168.1.80"
DEFAULT_USER = "admin"
DEFAULT_VM_NAME = "talos"
DEFAULT_OUTPUT = Path("clusters/main/talos/truenas-vm-backup.json")
REDACTED_SENTINEL = "REDACTED_ON_BACKUP"

# Fields returned by `vm.query` that are runtime/read-only and must not be sent to `vm.create`.
VM_READONLY_FIELDS = {"id", "uuid", "status", "display_available", "devices"}
DEVICE_READONLY_FIELDS = {"id", "vm"}


def run_ssh(target: str, remote_cmd: str) -> str:
    """Execute a command on TrueNAS over SSH and return stdout."""
    cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        target,
        remote_cmd,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(
            f"SSH command failed (exit {res.returncode}): {remote_cmd}\n"
            f"stderr: {res.stderr.strip()}"
        )
    return res.stdout.strip()


def redact_vm(vm: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of a VM dict with sensitive device fields redacted."""
    cleaned = copy.deepcopy(vm)
    for dev in cleaned.get("devices", []):
        attrs = dev.get("attributes", {})
        if isinstance(attrs, dict) and attrs.get("password"):
            attrs["password"] = REDACTED_SENTINEL
    return cleaned


def extract_zvol_datasets(vm: dict[str, Any]) -> list[str]:
    """Extract ZFS dataset names from DISK devices (e.g., /dev/zvol/main/talos-zvol -> main/talos-zvol)."""
    datasets: list[str] = []
    for dev in vm.get("devices", []):
        if dev.get("dtype") == "DISK":
            path = dev.get("attributes", {}).get("path", "")
            if path.startswith("/dev/zvol/"):
                datasets.append(path.removeprefix("/dev/zvol/"))
    return datasets


def cmd_backup(args: argparse.Namespace) -> int:
    target = f"{args.user}@{args.host}"
    print(f"Querying TrueNAS host {target}...")

    truenas_version = run_ssh(target, "midclt call system.version")
    trains_raw = run_ssh(target, "midclt call update.get_trains")
    vms_raw = run_ssh(target, "midclt call vm.query")
    pools_raw = run_ssh(
        target, "/usr/sbin/zpool list -H -o name,size,alloc,free,health"
    )

    vms: list[dict[str, Any]] = json.loads(vms_raw)
    if args.vm_name:
        matched = [vm for vm in vms if vm.get("name") == args.vm_name]
        if not matched:
            available = [vm.get("name") for vm in vms]
            raise RuntimeError(
                f"VM '{args.vm_name}' not found on {target}. Available VMs: {available}"
            )
        selected_vms = matched
    else:
        selected_vms = vms

    zvol_details: dict[str, Any] = {}
    for vm in selected_vms:
        for dataset in extract_zvol_datasets(vm):
            q_ds = shlex.quote(dataset)
            props = run_ssh(
                target,
                f"/usr/sbin/zfs list -H -o name,used,avail,refer,volsize {q_ds}",
            )
            snaps = run_ssh(
                target,
                f"/usr/sbin/zfs list -H -t snapshot -o name,creation {q_ds} || true",
            )
            zvol_details[dataset] = {
                "properties": props.split("\t") if props else [],
                "snapshots": [line for line in snaps.splitlines() if line.strip()],
            }

    pools = [line.split("\t") for line in pools_raw.splitlines() if line.strip()]

    payload = {
        "schema_version": 1,
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "truenas_host": args.host,
        "truenas_version": truenas_version,
        "update_trains": json.loads(trains_raw),
        "pools": [
            {
                "name": p[0],
                "size": p[1],
                "alloc": p[2],
                "free": p[3],
                "health": p[4],
            }
            for p in pools
            if len(p) >= 5
        ],
        "zvols": zvol_details,
        "vms": [redact_vm(vm) for vm in selected_vms],
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Saved VM backup state to {output_path}")
    return 0


def build_vm_create_payload(vm: dict[str, Any]) -> dict[str, Any]:
    """Strip read-only fields from a backed-up VM record for `vm.create`."""
    return {k: v for k, v in vm.items() if k not in VM_READONLY_FIELDS}


def build_device_create_payload(
    dev: dict[str, Any], vm_id: int | str, spice_password: str | None
) -> dict[str, Any]:
    """Strip read-only fields from a device record for `vm.device.create`."""
    payload = {k: copy.deepcopy(v) for k, v in dev.items() if k not in DEVICE_READONLY_FIELDS}
    payload["vm"] = vm_id
    attrs = payload.get("attributes")
    if isinstance(attrs, dict) and attrs.get("password") == REDACTED_SENTINEL:
        if spice_password:
            attrs["password"] = spice_password
        else:
            attrs.pop("password", None)
    return payload


def cmd_restore(args: argparse.Namespace) -> int:
    """[UNTESTED] Speculatively recreate a VM from a saved backup JSON."""
    print(
        "WARNING: `restore` is SPECULATIVE and UNTESTED against a live TrueNAS API.\n"
        "Review the generated payloads carefully before running with --apply.",
        file=sys.stderr,
    )

    backup_path = Path(args.input)
    data = json.loads(backup_path.read_text(encoding="utf-8"))
    vms: list[dict[str, Any]] = data.get("vms", [])
    if args.vm_name:
        vms = [vm for vm in vms if vm.get("name") == args.vm_name]
    if not vms:
        raise RuntimeError(f"No matching VM found in {backup_path}")
    if len(vms) > 1:
        raise RuntimeError(
            f"Multiple VMs in {backup_path}; pass --vm-name to select one."
        )

    vm = vms[0]
    vm_name = vm["name"]
    vm_payload = build_vm_create_payload(vm)
    device_payloads = [
        build_device_create_payload(dev, "<NEW_VM_ID>", args.spice_password)
        for dev in vm.get("devices", [])
    ]

    if not args.apply:
        print("\n[DRY-RUN] Would execute on", f"{args.user}@{args.host}:")
        print(f"1. Verify VM '{vm_name}' does not already exist (`midclt call vm.query`)")
        for ds in extract_zvol_datasets(vm):
            print(f"2. Verify zvol '/dev/zvol/{ds}' exists")
        print("3. midclt call vm.create", shlex.quote(json.dumps(vm_payload)))
        for idx, d_payload in enumerate(device_payloads, start=1):
            print(
                f"4.{idx}. midclt call vm.device.create",
                shlex.quote(json.dumps(d_payload)),
            )
        print("\nRe-run with --apply to execute (UNTESTED).")
        return 0

    # --- LIVE APPLY PATH (UNTESTED) ---
    target = f"{args.user}@{args.host}"
    existing = json.loads(run_ssh(target, "midclt call vm.query"))
    if any(item.get("name") == vm_name for item in existing):
        raise RuntimeError(
            f"A VM named '{vm_name}' already exists on {target}. Aborting."
        )

    for ds in extract_zvol_datasets(vm):
        run_ssh(target, f"test -e {shlex.quote('/dev/zvol/' + ds)}")

    created_raw = run_ssh(
        target, f"midclt call vm.create {shlex.quote(json.dumps(vm_payload))}"
    )
    created_vm = json.loads(created_raw)
    new_vm_id = created_vm["id"]
    print(f"Created VM '{vm_name}' with id={new_vm_id}")

    for dev in vm.get("devices", []):
        d_payload = build_device_create_payload(dev, new_vm_id, args.spice_password)
        run_ssh(
            target,
            f"midclt call vm.device.create {shlex.quote(json.dumps(d_payload))}",
        )
        print(f"Attached device dtype={d_payload.get('dtype')} to VM id={new_vm_id}")

    print("Restore complete. Verify devices, NIC bridge, and autostart in TrueNAS UI.")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backup and speculative (untested) restore for TrueNAS SCALE VMs."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_bak = sub.add_parser("backup", help="Query TrueNAS via SSH and save VM state.")
    p_bak.add_argument("--host", default=DEFAULT_HOST)
    p_bak.add_argument("--user", default=DEFAULT_USER)
    p_bak.add_argument("--vm-name", default=DEFAULT_VM_NAME)
    p_bak.add_argument("--output", default=str(DEFAULT_OUTPUT))

    p_res = sub.add_parser(
        "restore",
        help="[UNTESTED] Speculatively recreate a VM from a saved backup JSON.",
    )
    p_res.add_argument("--host", default=DEFAULT_HOST)
    p_res.add_argument("--user", default=DEFAULT_USER)
    p_res.add_argument("--vm-name", default=DEFAULT_VM_NAME)
    p_res.add_argument("--input", default=str(DEFAULT_OUTPUT))
    p_res.add_argument(
        "--spice-password",
        default=None,
        help="Optional SPICE display password to set if redacted in backup.",
    )
    p_res.add_argument(
        "--apply",
        action="store_true",
        help="Actually call midclt vm.create / vm.device.create (UNTESTED).",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "backup":
        return cmd_backup(args)
    if args.command == "restore":
        return cmd_restore(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
