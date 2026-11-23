---
name: major-upgrade-investigator
description: >-
  Investigates and safely applies Renovate-suggested major chart upgrades for TrueCharts and Flux.
  Use when given a Renovate upgrade string (e.g. "chore(flux): update image <chart> <old> → <new>")
  or when planning major version upgrades to inspect upstream git diffs, schema changes, and validate rendering before applying.
---

# Major Upgrade Investigator Skill

This skill guides the inspection, schema validation, and application of major chart version upgrades recommended by Mend Renovate for this repository (`truecharts-config-2025`).

---

## When to Activate

Activate this skill when:
- The user provides a Renovate commit message or PR title (e.g. `chore(flux): update image blocky 22.25.1 → 24.1.2`).
- The user asks whether a major version upgrade for a chart is safe to apply.
- An upgrade failed and needs investigation against upstream chart commits and schemas.

---

## The Investigation Workflow

When given an upgrade string (e.g. `chore(flux): update image <chart> <old> → <new>`):

### Step 0: Ensure Secrets Are Decrypted (Prerequisite)
The investigation script renders Helm templates against local values, which require real cluster substitutions (e.g. S3 endpoints, IPs, passwords) from `clusters/main/clusterenv.yaml`.
- The script strictly enforces that `clusters/main/clusterenv.yaml` is decrypted.
- If `clusters/main/clusterenv.yaml` is encrypted with SOPS, stop and prompt the user to decrypt first:
  ```bash
  ./clustertool decrypt
  ```
- Only after the user confirms decryption (or if already decrypted), proceed to run the tool.

### Step 1: Run the Automated Investigation Tool
Execute the built-in investigation script:
```bash
./scripts/investigate-upgrade.py "chore(flux): update image <chart> <old> → <new>"
```

This automated script performs:
1. **Upstream Chart Inspection in `/home/andrew/code/truecharts-org/truecharts`:**
   - Finds the commit range between `<old>` and `<new>`.
   - Diffs `Chart.yaml` to detect underlying library bumps (e.g. Common `29.x` → `30.x`, Valkey `2.x` → `3.x`, or upstream app version jumps).
   - Diffs `values.yaml` to see default value changes or removed configuration keys.
   - Summarizes upstream git commit logs for context.
2. **Local Deployment Matching:**
   - Finds the matching `helm-release.yaml` in `clusters/main/kubernetes/**/<chart>/`.
3. **Strict Schema Validation:**
   - Validates the current manifest's `spec.values` against the new version's JSON schema using `./scripts/validate-schemas.py`.
4. **Helm Template Verification:**
   - Pulls the new chart from `oci://oci.trueforge.org/truecharts/<chart>:<new>` into a temporary directory.
   - Executes `helm template <chart> <dir> -f <values> --kube-version 1.33.0 --api-versions barmancloud.cnpg.io/v1/ObjectStore` to verify rendering without runtime errors.
   - Checks the live cluster for the `objectstores.barmancloud.cnpg.io` CRD if the chart uses PostgreSQL/CNPG on Common-2026 (`32.x`).

---

### Step 2: Analyze Risk & Breaking Patterns

Check for known breaking change patterns in TrueCharts upgrades:

1. **Common-2026 (`30.4.x` → `32.x`) Major Rework (Universal Dependency System & CNPG):**
   - **When it triggers:** Releasing `common 32.0.0+` does not change existing pinned charts immediately; each stable/incubator chart moves to Common-2026 via its own **major chart version bump**.
   - **Universal Dependency System (`dependencies.<name>`):**
     - Top-level `cnpg`, `redis`, `valkey`, `mariadb`, `mongodb`, `clickhouse`, `memcached`, and `solr` blocks are **removed** and now fail Helm rendering immediately (`tc.v1.common.values.deprecations`).
     - All database/cache dependencies move under `dependencies.<name>` (`postgresql`, `valkey`, `mariadb`, `mongodb`, `clickhouse`, `memcached`), supporting `mode: standalone | operator`.
     - Dependency images move from `dependencies.<name>.image` to root keys (`valkeyImage`, `mariadbImage`, `mongodbImage`, `clickhouseImage`, `memcachedImage`).
     - **MariaDB, MongoDB, and ClickHouse Data Warning:** These now use TrueForge's own `containerforge` images (UID/GID 568, read-only rootfs) and a new volume/data layout. Data is **NOT** automatically migrated—you MUST dump the database before upgrading and restore after!
   - **PostgreSQL (CNPG) Major Changes (`cnpg.main` → `dependencies.postgresql`):**
     - **Structural Rewrite:** `cnpg.main` moves to `dependencies.postgresql` (which defaults to `mode: operator`, one CNPG cluster per release; the generated CNPG `Cluster` keeps the name `<release>-cnpg-main`).
       - Move `enabled: true` to `dependencies.postgresql.enabled: true`.
       - Move `user`, `database`, and `password` to `dependencies.postgresql.depconfig` as `postgresqlUsername`, `postgresqlDatabase`, and `password` (note: charts migrating to `dependencies.postgresql` default both `postgresqlUsername` and `postgresqlDatabase` to `app` unless overridden).
       - Move all CNPG operator settings (`pgVersion`, `mode: standalone | recovery`, `cluster`, `backups`, `recovery`, `monitoring`, `pooler`, `hibernate`) under `dependencies.postgresql.operator`.
     - **PostgreSQL 18 is the New Default (Automatic In-Place `pg_upgrade` Danger):**
       - Without an explicit version set, Common-2026 defaults to **PostgreSQL 18** (`ghcr.io/cloudnative-pg/postgresql:18.x-standard-trixie` on Debian 13) and triggers an in-place `pg_upgrade`.
       - **CRITICAL:** CloudNativePG only supports in-place `pg_upgrade` between images on the **same OS distribution**. Because Common-2026 also moves images from Debian 11/12 to **Debian 13 (Trixie)**, an existing cluster on an older image MUST stay on its current major version (`dependencies.postgresql.operator.pgVersion: 16`) during the chart upgrade to Debian 13 first, and only upgrade to `18` afterwards!
     - **CNPG Barman Cloud Plugin Required (`plugin-barman-cloud`):**
       - In-tree Barman (`Cluster.spec.backup.barmanObjectStore`) is **removed** in favor of the **CNPG Barman Cloud plugin** (`barman-cloud.cloudnative-pg.io`), spawning `ObjectStore` (`barmancloud.cnpg.io/v1`) resources (`<release>-cnpg-main-backup` and `<release>-cnpg-main-recovery`).
       - **Cluster Prerequisite:** Requires CloudNativePG `>= 1.26` (`0.26+` chart), `cert-manager`, and the `plugin-barman-cloud` Helm chart installed in the CNPG operator namespace (`objectstores.barmancloud.cnpg.io` CRD). Rendering fails if backups or recovery are enabled and the `barmancloud.cnpg.io/v1/ObjectStore` API is missing (offline `helm template` must pass `--api-versions barmancloud.cnpg.io/v1/ObjectStore`).
       - **Archive Compatibility:** `destinationPath`, `serverName`, and `revision` work identically (`s3://<bucket>/<release>/cnpg`, `<clusterName>-r<revision>`), so existing Cloudflare R2 backups remain usable without path changes.
       - **Removed CNPG Keys:** `barmanMode`, `recovery.backupName`, `recovery.method: backup` (use `object_store`), `operator.verify`, and `type: timescaledb` are removed. Also, delete or rename any existing `Backup` resources created from `manualBackups` before upgrading because their spec is immutable.
     - **Credentials & Secret Rename (`cnpg-main-urls` → `<release>-postgresqlcreds`):**
       - The `cnpg-main-urls` Secret (key `std`) and `.Values.cnpg.main.creds` are **removed**!
       - Replaced by Secret `<release>-postgresqlcreds` (keys: `url`, `urlnossl`, `jdbc`, `plainhost`, `plainporthost`, `postgresql-password`, etc.) and `.Values.dependencies.postgresql.depconfig.creds`.
       - Check local `helm-release.yaml` `workload.main.podSpec.containers.main.env` (such as `LLDAP_database_url` in `lldap`) for any custom `secretKeyRef` pointing to `cnpg-main-urls` and update or remove it so chart defaults bind to `<release>-postgresqlcreds`.
   - **Before vs. After CNPG Values Example:**
     ```yaml
     # BEFORE (Common 30.4.x):
     cnpg:
       main:
         enabled: true
         mode: standalone
         pgVersion: 16
         user: lldap
         database: lldap
         password: ${LLDAP_CNPG_PASSWORD}
         cluster:
           instances: 1
           singleNode: true
         backups:
           enabled: true
           credentials: cloudflare
           revision: "2"

     # AFTER (Common-2026 / 32.x):
     dependencies:
       postgresql:
         enabled: true
         depconfig:
           postgresqlUsername: lldap
           postgresqlDatabase: lldap
           password: ${LLDAP_CNPG_PASSWORD}
         operator:
           mode: standalone
           pgVersion: 16 # Pin to 16 first during Debian 13 image transition!
           cluster:
             instances: 1
             singleNode: true
           backups:
             enabled: true
             credentials: cloudflare
             revision: "2"
     ```

2. **Common Library 29.x → 30.4.x Strict Schema Rules (Still Enforced in 32.x):**
   - **Closed JSON Schema (`additionalProperties: false`):** Unknown or removed keys (legacy probes, `ingress.autoLink`, `global.ignoreIsStopped`, `securityContext.container.fsGroup`, env-lists, top-level `volumeClaimTemplates`) fail rendering.
   - **Strict Boolean Typing:** Quoted strings like `"false"` on boolean keys that do not support `tpl` now fail validation.
   - **`credentials.<name>.name`:** Disallowed redundant property (remove `name: <name>`).
   - **`recovery.enabled`:** Disallowed property (mode is controlled via `operator.mode: standalone | recovery`).
   - **`backups.retentionPolicy: ""`:** Cannot be empty string (remove or supply valid duration like `30d`).

3. **Upstream Application Binary Jump:**
   - If `appVersion` changed (e.g. major release of Plex, Blocky, Authelia), review if the app itself has migration steps or deprecations.

---

### Step 3: Present the Findings to the User

Present a clear summary covering:
- **Application Version:** Did the app binary version change, or is it purely a Helm chart/library bump?
- **Library Dependencies:** Which underlying libraries changed (e.g. Common, Valkey)?
- **Validation Status:** Schema validation result and `helm template` status.
- **Breaking Changes:** Any adjustments required in the local `helm-release.yaml`.
- **Recommendation:** `SAFE TO UPGRADE` or `MANUAL ADJUSTMENT REQUIRED`.

---

### Step 4: Applying the Upgrade

If verified safe:
1. Apply the version change and schema header:
   ```bash
   ./scripts/investigate-upgrade.py "chore(flux): update image <chart> <old> → <new>" --apply
   ```
2. Verify again using `./scripts/validate-schemas.py <manifest-path>`.
3. Stage and commit:
   ```bash
   git add <manifest-path>
   git commit -m "chore(flux): update chart <chart> <old> → <new>"
   git push origin main
   ```
4. If the user requests immediate cluster rollout:
   ```bash
   flux reconcile kustomization flux-entry
   flux reconcile helmrelease <chart> -n <namespace>
   ```
