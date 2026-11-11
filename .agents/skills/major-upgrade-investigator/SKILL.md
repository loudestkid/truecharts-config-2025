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
1. **Upstream Chart Inspection in `/home/andrew/code/truecharts`:**
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
   - Executes `helm template <chart> <dir> -f <values> --kube-version 1.33.0` to verify rendering without runtime errors.

---

### Step 2: Analyze Risk & Breaking Patterns

Check for known breaking change patterns in TrueCharts upgrades:

1. **Common Library 29.x → 30.x Major Bump:**
   - **Strict Schema Enforcement:** Unknown or misplaced keys that were previously ignored will now cause hard failures.
   - **`credentials.<name>.name`:** Disallowed redundant property (remove `name: <name>`).
   - **`cnpg.<name>.recovery.enabled`:** Disallowed property (mode is controlled via `mode: standalone | recovery`).
   - **`cnpg.<name>.backups.retentionPolicy: ""`:** Cannot be empty string (remove or supply valid duration like `30d`).
2. **Subchart Upgrades:**
   - e.g. `valkey` (Redis replacement) 2.x → 3.x (check persistence/password format).
   - `cnpg` PostgreSQL version bumps (check if major version migration is required).
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
