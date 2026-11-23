# ClusterTool 5.0 Migration Plan (`truecharts-config-2025` → `cluster-config-2027`)

This document records the architecture analysis, pre-migration audit, and step-by-step execution checklist for migrating Andrew's Kubernetes cluster from **ForgeTool 3.0 / ClusterTool 2.x** (`truecharts-config-2025`) to **ClusterTool 5.0 Stable** (`cluster-config-2027`).

---

## 1. Repository & Tooling Paths

- **Current Live Cluster Repo (v2/v3):** `/home/andrew/code/truecharts-config-2025` (`git@github.com:loudestkid/truecharts-config-2025.git`)
- **Target ClusterTool 5.0 Repo:** `/home/andrew/code/cluster-config-2027` (`git@github.com:loudestkid/cluster-config-2027.git`)
- **Upstream ClusterTool Source Repo:** `/home/andrew/code/truecharts-org/clustertool`
  - Contains official v5 documentation at `embed/generic/root/HOW_TO.md` and templates under `embed/generic/`.
  - *Note:* Analysis below is based on `v5.0.0-beta.3` (`e7842d0`). When stable `v5.0.0` is released, check `git -C /home/andrew/code/truecharts-org/clustertool log v5.0.0-beta.3..HEAD --oneline` for any last-minute template adjustments before running `clustertool init`.
- **Upstream TrueCharts Catalog Repo:** `/home/andrew/code/truecharts-org/truecharts`

---

## 2. Pre-Migration Cleanup & Backup Audit (Completed Nov 22, 2026)

1. **Pruned Unused Workloads & Cloudflare Routes:**
   - Removed `sites/andrew`, `media/minio`, and `media/calibre` from `truecharts-config-2025` (including their variables in `clusterenv.yaml` and `clustersettings.secret.yaml`).
   - Removed their public hostnames (`andrew.jewell.one`, `s3.jewell.one`, `api.s3.jewell.one`, `calibre.jewell.one`, plus legacy `etherpad.jewell.one` and `readarr.jewell.one`) from Cloudflare Zero Trust tunnel `7c39d6eb-5a0e-44f6-8a86-1c8e48b3ed5f` (now at version `95`) and deleted their `CNAME` DNS records in `jewell.one`.
2. **Fixed Backup Gaps:**
   - Enabled VolSync `ReplicationSource` backups (`src.enabled: true`) for both `filebrowser-config` and `filebrowser-database` in `clusters/main/kubernetes/apps/filebrowser/helm-release.yaml`.
   - Disabled the unused chart-default `persistence.manga` PVC on `kavita` in `clusters/main/kubernetes/media/kavita/helm-release.yaml`.
3. **Verified All Remaining Stateful Workloads:**
   - All active application PVCs have VolSync `ReplicationSource` (nightly at `00:00 UTC`) and `ReplicationDestination` (`dest.enabled: true`) backing up to Cloudflare R2 (`s3://truecharts-config` at `https://5f143f2d731cb90463d072a908fc0431.r2.cloudflarestorage.com`).
   - Both PostgreSQL clusters (`authelia-cnpg-main` and `lldap-cnpg-main`) have active daily CNPG `ScheduledBackup`s to Cloudflare R2 (`s3://truecharts-config/<app>/cnpg`).

---

## 3. Architectural Changes in ClusterTool 5.0

### A. Flattened Single-Cluster Directory Layout
ClusterTool 5.0 drops `clusters/main/` and places everything at the repository root:

| Component | Old Path (`truecharts-config-2025`) | New Path (`cluster-config-2027`) |
| :--- | :--- | :--- |
| **Talos Config & Patches** | `clusters/main/talos/talconfig.yaml` | `talos/clustertool.yaml` + `talos/patches/{all,control-plane,worker,nodes/control-1}/` |
| **Talos Secrets** | `clusters/main/talos/generated/talsecret.yaml` | `talos/secrets.sops.yaml` |
| **Cluster Secrets / Vars** | `clusters/main/clusterenv.yaml` + generated `clustersettings.secret.yaml` | `kubernetes/config/cluster-secrets.sops.yaml` (single source of truth; no generated copy) |
| **Helm / OCI Repositories** | `repositories/` | `kubernetes/repositories/` |
| **Flux Entrypoint** | `clusters/main/kubernetes/flux-entry.yaml` | `kubernetes/flux-entry/ks.yaml` |
| **All Workloads** | `clusters/main/kubernetes/{apps,core,kube-system,media,networking,sites,system}` | `kubernetes/apps/{flux-system,kube-system,networking,observability,other,system,...}` |

### B. Native Talos 1.14 Configuration Documents (No Talhelper)
- Talhelper is completely removed. `talos/clustertool.yaml` (`apiVersion: clustertool/v1`, `kind: ClusterConfig`) defines `clusterName`, `talosVersion` (`v1.14.x`), `kubernetesVersion` (`v1.37.x`), `nodes`, and the declarative `bootstrap` plan (`crd` and `steps`).
- Node IP variable `MASTER1IP` is renamed to **`CONTROL1IP`** and must be a **bare IP without CIDR** (`192.168.1.81`). The `/24` prefix is specified directly in `talos/patches/nodes/control-1/20-network.yaml` (`address: ${CONTROL1IP}/24`).
- ClusterTool 5.0 does **not** synthesize `_IP`, `_CIDR`, or `_NETMASK` suffixes and does **not** inject `CLUSTERNAME`.
- Network configuration uses a logical `mgmt` link alias (`LinkAliasConfig` + `LinkConfig` + `Layer2VIPConfig`) rather than hardcoding `ens3`.
- The default Image Factory schematic in `talos/patches/nodes/control-1/00-install.yaml` (`88d1f7a5c4f1d3aba7df787c448c1d3d008ed29cfb34af53fa0df4336a56040b`) already includes `siderolabs/iscsi-tools`, `siderolabs/qemu-guest-agent`, and `siderolabs/util-linux-tools`.

### C. Storage Mount Paths (`/var/mnt/longhorn` & `/var/mnt/openebs`)
- Talos `UserVolumeConfig` (`talos/patches/all/31-storage.yaml`) creates directory-type user volumes on the `EPHEMERAL` partition at `/var/mnt/longhorn` and `/var/mnt/openebs` (replacing the old `machine.kubelet.extraMounts` at `/var/lib/longhorn` and `/var/openebs/local`).
- ClusterTool 5.0's built-in `longhorn` and `openebs` HelmReleases are pre-configured for these paths.

### D. Flux Operator & Updated System Stack
- Raw Flux manifests (`flux-system/flux/flux.yaml`) are replaced by `flux-operator` and `flux-instance` HelmReleases.
- `kubernetes/flux-entry/ks.yaml` patches all child `HelmRelease` resources with default install/upgrade/rollback/drift-detection settings (`crds: CreateReplace`, `RetryOnFailure`, `RemediateOnFailure`, `driftDetection.mode: warn`).
- Built-in stack replacements:
  - `system-upgrade-controller` → **`tuppr`** (`kubernetes/apps/system/system-upgrade/tuppr/`)
  - `kubernetes-dashboard` (`DASHBOARD_IP`) → **`headlamp`** (`kubernetes/apps/observability/headlamp/`, using `HEADLAMP_IP`)
  - `cloudnative-pg` → moved to `kubernetes/apps/system/cnpg-system/` and paired with **`plugin-barman-cloud`**
  - `longhorn` → includes automated cleanup/trim CronJobs in `kubernetes/apps/system/longhorn/jobs/`
  - `spegel` and `descheduler` → removed from the default template
- `clustertool genks` scaffolds missing `ks.yaml` / `kustomization.yaml` files under `kubernetes/apps/` (`genconfig` no longer overwrites Kustomizations).

---

## 4. Secrets Mapping & Rotation Rules

### Secrets You MUST NOT Change Before Restore
Keep these identical to `truecharts-config-2025` so VolSync and CNPG can decrypt and restore existing backups from Cloudflare R2:
- **All VolSync Restic keys (`*_CONFIG_ENCRYPTION_KEY`)**: `FILES_`, `PLEX_`, `QBT_`, `JACKETT_`, `SONARR_`, `RADARR_`, `KAVITA_`, `LLDAP_`, `AUTHELIA_`, `RECIPES_`, `WEDDING_`
- **Authelia & LLDAP internal keys/passwords**: `AUTHELIA_ENCRYPTION_KEY` (encrypts 2FA/TOTP seeds in Postgres), `LLDAP_KEY_FILE`, `LLDAP_CNPG_PASSWORD`, `LLDAP_USER_PASSWORD`
- **Cloudflare R2 credentials**: `VOLSYNC_ACCESS_KEY`, `VOLSYNC_SECRET_KEY`, `VOLSYNC_S3_URL`, `VOLSYNC_BUCKET_NAME`

### Variable Renames & Formatting for `kubernetes/config/cluster-secrets.sops.yaml`
- `MASTER1IP: 192.168.1.81/24` → **`CONTROL1IP: 192.168.1.81`** (strip `/24`; drop unused `MASTER2IP` / `MASTER3IP`)
- `DASHBOARD_IP` → **`HEADLAMP_IP`** (and optionally `HEADLAMP_DNS_PREFIX` if exposing via ingress)
- `GITHUB_REPOSITORY` → **`ssh://git@github.com/loudestkid/cluster-config-2027.git`**
- **Quote all numeric/boolean values** under `stringData` (e.g., `JACKETT_PORT: "9117"`).
- **Drop unused legacy keys**: `DOMAIN_0_OLD_CLOUDFLARE_TOKEN`, `GITHUB_WEBHOOK_TOKEN`, `VPN_ENDPOINT_IP`, `VPN_ENDPOINT_PORT`, `VPN_WIREGUARD_ADDRESSES`, `VPN_WIREGUARD_PUBLIC_KEY`.

---

## 5. Step-by-Step Migration Checklist

### Phase 1: Initialize `cluster-config-2027` with Stable ClusterTool 5.0
1. Place the stable ClusterTool 5.0 binary in `/home/andrew/code/cluster-config-2027/clustertool`.
2. Run initial scaffold:
   ```bash
   cd /home/andrew/code/cluster-config-2027
   ./clustertool init
   ```
   *(This generates a fresh `age.agekey`, `talos/secrets.sops.yaml`, and the template tree with `RUNAGAIN` markers.)*
3. Decrypt `/home/andrew/code/truecharts-config-2025/clusters/main/clusterenv.yaml` (`./forgetool decrypt` in `truecharts-config-2025`) and copy your variables into `/home/andrew/code/cluster-config-2027/kubernetes/config/cluster-secrets.sops.yaml` under `stringData:` (applying the renames above), then re-encrypt `truecharts-config-2025`.
4. Run `./clustertool init` a second time in `cluster-config-2027` to complete setup and generate `ssh-public-key.txt`.
5. Add `ssh-public-key.txt` as a **read/write Deploy Key** on the `loudestkid/cluster-config-2027` GitHub repository.

### Phase 2: Port Custom Workloads (13 Releases)
All core/system charts are already provided by ClusterTool 5.0. Port only these 13 custom releases from `truecharts-config-2025/clusters/main/kubernetes/` into `cluster-config-2027/kubernetes/apps/`:

1. **`networking/`**:
   - `authelia`
   - `lldap`
   - `cloudflared`
2. **`media/`**:
   - `plex`
   - `qbt` (`qbittorrent-main`)
   - `jackett`
   - `sonarr`
   - `radarr`
   - `kavita`
3. **`sites/`**:
   - `recipes`
   - `wedding`
4. **`other/` (or `apps/`)**:
   - `filebrowser`
   - `truenas-access`
   - *(Optional)* `generic-device-plugin` if still needed

#### Critical Adjustment for CNPG Restore (`authelia` and `lldap`)
Before first bootstrap on the new cluster, check the current backup `revision` in `authelia/helm-release.yaml` (currently `"3"`) and `lldap/helm-release.yaml` (currently `"2"`), and configure **recovery mode** in `cluster-config-2027` so CNPG restores from Cloudflare R2 instead of initializing empty databases:

- **`authelia`**:
  ```yaml
  cnpg:
    main:
      mode: recovery
      recovery:
        method: object_store
        credentials: cloudflare
        revision: "3"
      backups:
        enabled: true
        credentials: cloudflare
        revision: "4"
  ```
- **`lldap`**:
  ```yaml
  cnpg:
    main:
      mode: recovery
      recovery:
        method: object_store
        credentials: cloudflare
        revision: "2"
      backups:
        enabled: true
        credentials: cloudflare
        revision: "3"
  ```

*(Note: Also verify whether TrueCharts Common has updated its CNPG templates for `plugin-barman-cloud` or still uses `barmanObjectStore` directly.)*

### Phase 3: Generate Kustomizations, Configs, Encrypt & Push
1. Check `talos/patches/nodes/control-1/` (hostname `k8s-control-1`, `/24` prefix, disk selector, and any custom node labels if desired).
2. Scaffold Kustomizations, generate Talos configs, and encrypt secrets:
   ```bash
   cd /home/andrew/code/cluster-config-2027
   ./clustertool genks
   ./clustertool genconfig
   ./clustertool encrypt
   ```
3. Review all generated `ks.yaml` and `kustomization.yaml` files (especially shared namespaces like `media` and `sites` where multiple `HelmRelease`s share one namespace).
4. Commit and push `cluster-config-2027` to `main`.

### Phase 4: Final Pre-Cutover Backups & TrueNAS VM Snapshot
1. **Trigger/Verify Final Backups on Current Cluster:**
   - Ensure VolSync `ReplicationSource`s have synced recent changes (`kubectl get replicationsources -A`).
   - Trigger or verify recent CNPG backups for `authelia` and `lldap` (`kubectl get backups.postgresql.cnpg.io -A`).
2. **Backup & Snapshot the TrueNAS Talos VM (`192.168.1.80`):**
   - Run `python3 ./scripts/truenas-vm-backup.py backup` in `truecharts-config-2025`.
   - Shut down the `talos` VM cleanly or take a ZFS snapshot of `main/talos-zvol`:
     ```bash
     ssh admin@192.168.1.80 "/usr/sbin/zfs snapshot main/talos-zvol@pre-ct5-migration-$(date +%Y%m%d)"
     ```
   *(If anything goes wrong during migration, rolling back this ZFS snapshot immediately restores the working v3 cluster!)*

### Phase 5: Bootstrap Fresh Talos 1.14 Cluster & Post-Restore Cleanup
1. Wipe / boot the `talos` VM on TrueNAS with the Talos `v1.14.x` ISO (or reset the node) so it awaits config at `192.168.1.81`.
2. From `/home/andrew/code/cluster-config-2027`, run:
   ```bash
   ./clustertool talos apply
   ```
3. Monitor bootstrap progress and Flux reconciliation:
   ```bash
   kubectl -n flux-system get fluxinstances
   kubectl get helmreleases -A
   kubectl get replicationsources,replicationdestinations -A
   kubectl get clusters.postgresql.cnpg.io -A
   ```
4. Once `authelia-cnpg-main` and `lldap-cnpg-main` are healthy and restored, switch `cnpg.main.mode` in their `helm-release.yaml` files from `recovery` back to `standalone` (removing the `recovery:` block while keeping the incremented `backups.revision`), commit, and push.
