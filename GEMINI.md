# TrueCharts Config 2025 — Workspace Guide & Context

This file serves as the persistent project manual for AI coding assistants (Antigravity/Gemini via `GEMINI.md`, Claude Code via `CLAUDE.md`, and other agents via `AGENTS.md`).

---

## 1. Overview & Architecture

- **Repository Role:** This repository (`truecharts-config-2025`) manages the GitOps deployment for Andrew's Kubernetes cluster.
- **GitOps Engine:** Managed by **Flux v2** (`helm.toolkit.fluxcd.io/v2` HelmRelease resources and `kustomize.toolkit.fluxcd.io/v1`).
- **Cluster Deployment Mechanism:**
  - Flux periodically reconciles manifests against git.
  - Manifests contain environment substitutions (e.g. `${VOLSYNC_ACCESS_KEY}`, `${AUTHELIA_IP}`, `${DOMAIN_0}`) which are resolved cluster-side before Helm renders and deploys the charts.
- **Upstream Chart Catalog & Tooling (`code/truecharts-org`):**
  - TrueCharts Catalog Path: `/home/andrew/code/truecharts-org/truecharts`
  - ClusterTool Path: `/home/andrew/code/truecharts-org/clustertool`
  - Fork remote: `origin` (`git@github.com:loudestkid/truecharts.git`)
  - Upstream remote: `upstream` (`https://github.com/trueforge-org/truecharts.git`)
  - OCI Registry: `oci://oci.trueforge.org/truecharts`
  - When investigating chart templates, default values, or schemas, refer to `/home/andrew/code/truecharts-org/truecharts`. Remember to keep `master` synced with `upstream/master` and pushed to `origin/master`.

---

## 2. Repository Structure

```
truecharts-config-2025/
├── clusters/main/
│   ├── clusterenv.yaml          # Source environment/secrets file for ClusterTool (edit here!)
│   ├── talos/                   # Talos OS configuration (talconfig.yaml) and generated node configs
│   └── kubernetes/              # Primary Kubernetes cluster manifests
│       ├── apps/                # filebrowser, kubernetes-dashboard, truenas-access
│       ├── core/                # blocky, clusterissuer, metallb-config
│       ├── kube-system/         # cilium, descheduler, kubelet-csr-approver, metrics-server, etc.
│       ├── media/               # calibre, jackett, kavita, minio, plex, qbt, radarr, sonarr
│       ├── networking/          # authelia, cloudflared, lldap, nginx-external, nginx-internal
│       ├── sites/               # andrew, recipes, wedding (Grav sites)
│       ├── system/              # cert-manager, cloudnative-pg, longhorn, spegel, volsync, etc.
│       ├── flux-system/         # Flux controller configuration & generated clustersettings.secret.yaml
│       └── flux-entry.yaml      # Root Flux kustomization entrypoint
├── repositories/                # HelmRepository, GitRepository, and OCIRepository definitions
├── scripts/                     # Local maintenance and validation scripts
│   └── validate-schemas.py      # Automated TrueCharts JSON schema validator
├── clustertool                  # Local ClusterTool binary (genconfig, encrypt, decrypt, etc.)
├── GEMINI.md                    # This persistent reference file
└── age.agekey                   # SOPS encryption age key (keep secure)
```

---

## 3. Schema Enforcement & The Common 30.4.x / Common-2026 (32.x) Upgrades

Starting with TrueCharts **Common Library 30.4.0+** (released Nov 2026), charts enforce **strict JSON Schema validation** (`additionalProperties: false`) on all `values.yaml` inputs.

With **Common-2026 (`32.0.0+`, currently `32.3.0`)**, TrueCharts introduced its largest breaking rework yet. Stable and incubator charts migrate to Common-2026 one by one via **major chart version bumps**.

### Schema References
- **Hosted Flux HelmRelease Schema:**
  ```yaml
  # yaml-language-server: $schema=https://truecharts.org/schemas/helm-release/32.3.0.schema.json
  ```
  *(Add this at line 2 of `helm-release.yaml` files for in-editor autocompletion and live validation.)*
- **Raw Website Schemas:**
  `https://raw.githubusercontent.com/trueforge-org/websites/refs/heads/main/truecharts/public/schemas/helm-release/<version>.schema.json`
  `https://raw.githubusercontent.com/trueforge-org/websites/refs/heads/main/truecharts/public/schemas/common/<version>.schema.json`
- **Source Schemas in `code/truecharts-org/truecharts`:**
  - Full schema: `charts/library/common/values.schema.json`
  - Modular definitions: `charts/library/common/schemas/` (`credentials.json`, `dependencies.json`, `dependencies/postgresql/operator/`, `persistence/`, etc.)

### Common-2026 (`32.x`) Breaking Changes (Universal Dependencies & CNPG)

1. **`cnpg.main` → `dependencies.postgresql`:**
   - Top-level `cnpg`, `redis`, `valkey`, `mariadb`, `mongodb`, `clickhouse`, `memcached`, `solr`, and `volumeClaimTemplates` keys now **fail Helm rendering** (`tc.v1.common.values.deprecations`).
   - All CNPG configuration moves under `dependencies.postgresql` (default `mode: operator`; spawns the same `<release>-cnpg-main` `Cluster` name):
     - `enabled: true` → `dependencies.postgresql.enabled: true`
     - `user`, `database`, `password` → `dependencies.postgresql.depconfig.{postgresqlUsername, postgresqlDatabase, password}`
     - `pgVersion`, `mode: standalone | recovery`, `cluster`, `backups`, `recovery`, `monitoring`, `pooler` → `dependencies.postgresql.operator.*`
2. **PostgreSQL 18 Default & Debian 13 (Trixie) Image Migration:**
   - Common-2026 defaults to **PostgreSQL 18** (`-standard-trixie`). If `dependencies.postgresql.operator.pgVersion` is not set, CNPG automatically triggers an in-place `pg_upgrade` to PG 18.
   - Because CNPG only supports `pg_upgrade` across images on the **same OS distribution**, always pin `dependencies.postgresql.operator.pgVersion: 16` when first upgrading a chart to Common-2026 so it transitions cleanly to Debian 13 on PG 16 first.
3. **CNPG Barman Cloud Plugin Required (`plugin-barman-cloud`):**
   - In-tree Barman (`barmanObjectStore`) is removed. Backups and object store recovery now spawn `barmancloud.cnpg.io/v1` `ObjectStore` resources and require **`plugin-barman-cloud`** installed in the CNPG operator namespace (plus CNPG `>= 1.26` and `cert-manager`).
   - Offline `helm template` runs with backups/recovery enabled must pass `--api-versions barmancloud.cnpg.io/v1/ObjectStore`.
   - `destinationPath`, `serverName`, and `revision` remain identical (`s3://<bucket>/<release>/cnpg`), so existing R2 backups work seamlessly.
4. **Secret & Credentials Rename (`cnpg-main-urls` → `<release>-postgresqlcreds`):**
   - The `cnpg-main-urls` Secret (key `std`) and `.Values.cnpg.main.creds` are replaced by `<release>-postgresqlcreds` (key `url`, `urlnossl`, `jdbc`, `plainhost`, `postgresql-password`) and `.Values.dependencies.postgresql.depconfig.creds`.

### Common 30.4.x+ Strict Schema Violations & Fixes

1. **`credentials.<target>.name`**:
   - **Error:** `Additional properties are not allowed ('name' was unexpected)`
   - **Cause:** Credentials map keys (`credentials.cloudflare:`) define the name. A nested `name: cloudflare` property is redundant and disallowed (`additionalProperties: false`).
   - **Fix:** Remove `name: <target>` from the credentials block.

2. **`recovery.enabled`**:
   - **Error:** `Additional properties are not allowed ('enabled' was unexpected)`
   - **Cause:** CNPG operation mode is toggled by `mode: standalone | recovery`. The `recovery:` block does NOT have an `enabled: false` flag.
   - **Fix:** When running in `mode: standalone`, remove the entire `recovery:` block (or remove `enabled: false`).

3. **`backups.retentionPolicy`**:
   - **Error:** `'' does not match '^[1-9][0-9]*[dwm]$'`
   - **Cause:** An empty string `""` violates the regex format.
   - **Fix:** Remove `retentionPolicy` entirely or supply a valid duration (e.g., `30d`, `4w`).

4. **Empty `values:` block**:
   - **Fix:** If a chart needs no custom values, use `values: {}` or omit cleanly so it doesn't parse as `None`.

---

## 4. Key CLI Tools & Commands

### ClusterTool & Secrets Management (`clusterenv.yaml`)
- **IMPORTANT:** Do **NOT** manually edit `clusters/main/kubernetes/flux-system/flux/clustersettings.secret.yaml` or files in `clusters/main/talos/generated/`.
- Instead, edit variables/secrets in `clusters/main/clusterenv.yaml` and use `./clustertool` in the repository root:
  ```bash
  # Decrypt secrets
  ./clustertool decrypt

  # Regenerate clustersettings.secret.yaml and Talos configs from clusterenv.yaml & talconfig.yaml
  ./clustertool genconfig

  # Encrypt secrets before committing
  ./clustertool encrypt
  ```

### Investigating & Applying Renovate Major Upgrades
Use the upgrade investigator tool (or the `major-upgrade-investigator` skill):
```bash
# Analyze a Renovate PR/commit string
./scripts/investigate-upgrade.py "chore(flux): update image blocky 22.25.1 → 24.1.2"

# Automatically apply the version bump if safe
./scripts/investigate-upgrade.py "chore(flux): update image blocky 22.25.1 → 24.1.2" --apply
```

### Validating Schemas Locally
Use the built-in validator script:
```bash
# Validate all TrueCharts releases in clusters/main/kubernetes
./scripts/validate-schemas.py

# Validate a specific release directory
./scripts/validate-schemas.py clusters/main/kubernetes/networking/authelia

# Validate live HelmRelease objects directly from the running cluster
./scripts/validate-schemas.py --live

# Force refresh the cached schema from GitHub
./scripts/validate-schemas.py --refresh
```

### Inspecting Cluster State (Flux & Helm)
```bash
# Check status of all HelmReleases
kubectl get helmreleases -A

# View detailed release status and Helm controller failure logs
kubectl describe helmrelease authelia -n authelia

# View the actual substituted values applied to a release
kubectl get helmrelease authelia -n authelia -o jsonpath='{.spec.values}' | jq .
```

### Testing Helm Rendering Manually
```bash
# Pull and untar a chart from TrueCharts OCI
helm pull oci://oci.trueforge.org/truecharts/authelia --version 34.1.2 --untar

# Test template rendering against a values file (specify kube-version >=1.33.0)
helm template authelia ./authelia -f values.yaml --kube-version 1.33.0
```

---

## 5. Status & History

1. **Schema Validation Pass:**
   - All 26 TrueCharts `HelmRelease` manifests now pass validation against the published `30.4.5` schema (`./scripts/validate-schemas.py` reports 26/26 valid).
2. **Fixed Releases:**
   - `authelia`: Cleaned up schema header, removed `credentials.cloudflare.name`, removed redundant `recovery:` block. Successfully deployed and running.
   - `lldap`: Switched to `mode: standalone`, removed redundant `recovery:` block, removed empty `retentionPolicy: ""`, removed `credentials.cloudflare.name`, and removed redundant `LLDAP_JWT_SECRET` override so chart auto-binding takes over.
   - **Removed Unused Releases & Routes:**
   - Removed `sites/andrew`, `media/minio`, and `media/calibre` (and cleaned up their Cloudflare Zero Trust tunnel routes and DNS records).
   - Enabled VolSync `ReplicationSource` backups (`src.enabled: true`) for `filebrowser` (`config` and `database`) and disabled unused `persistence.manga` PVC on `kavita`.

---

## 6. Upcoming ClusterTool 5.0 Migration (`cluster-config-2027`)

- **Migration Plan Document:** See [`CLUSTERTOOL_MIGRATION.md`](./CLUSTERTOOL_MIGRATION.md) for the complete architecture comparison, secret mapping rules, CNPG recovery configuration, and step-by-step migration checklist.
- **Target Repository:** `/home/andrew/code/cluster-config-2027` (`git@github.com:loudestkid/cluster-config-2027.git`)
- **Instructions for AI Assistants:** When the user indicates that ClusterTool 5.0 stable is released and ready to initialize in `/home/andrew/code/cluster-config-2027`, **read [`CLUSTERTOOL_MIGRATION.md`](./CLUSTERTOOL_MIGRATION.md) first** before running any commands.
