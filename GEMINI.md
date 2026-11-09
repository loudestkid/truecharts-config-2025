# TrueCharts Config 2025 — Workspace Guide & Context

This file serves as the persistent project manual for AI coding assistants (Antigravity/Gemini via `GEMINI.md`, Claude Code via `CLAUDE.md`, and other agents via `AGENTS.md`).

---

## 1. Overview & Architecture

- **Repository Role:** This repository (`truecharts-config-2025`) manages the GitOps deployment for Andrew's Kubernetes cluster.
- **GitOps Engine:** Managed by **Flux v2** (`helm.toolkit.fluxcd.io/v2` HelmRelease resources and `kustomize.toolkit.fluxcd.io/v1`).
- **Cluster Deployment Mechanism:**
  - Flux periodically reconciles manifests against git.
  - Manifests contain environment substitutions (e.g. `${VOLSYNC_ACCESS_KEY}`, `${AUTHELIA_IP}`, `${DOMAIN_0}`) which are resolved cluster-side before Helm renders and deploys the charts.
- **Upstream Chart Catalog (`code/truecharts`):**
  - Path: `/home/andrew/code/truecharts`
  - Fork remote: `origin` (`git@github.com:loudestkid/truecharts.git`)
  - Upstream remote: `upstream` (`https://github.com/trueforge-org/truecharts.git`)
  - OCI Registry: `oci://oci.trueforge.org/truecharts`
  - When investigating chart templates, default values, or schemas, refer to `/home/andrew/code/truecharts`. Remember to keep `master` synced with `upstream/master` and pushed to `origin/master`.

---

## 2. Repository Structure

```
truecharts-config-2025/
├── clusters/main/kubernetes/    # Primary Kubernetes cluster manifests
│   ├── apps/                    # filebrowser, kubernetes-dashboard, truenas-access
│   ├── core/                    # blocky, clusterissuer, metallb-config
│   ├── kube-system/             # cilium, descheduler, kubelet-csr-approver, metrics-server, etc.
│   ├── media/                   # calibre, jackett, kavita, minio, plex, qbt, radarr, sonarr
│   ├── networking/              # authelia, cloudflared, lldap, nginx-external, nginx-internal
│   ├── sites/                   # andrew, recipes, wedding (Grav sites)
│   ├── system/                  # cert-manager, cloudnative-pg, longhorn, spegel, volsync, etc.
│   ├── flux-system/             # Flux controller configuration
│   └── flux-entry.yaml          # Root Flux kustomization entrypoint
├── repositories/                # HelmRepository, GitRepository, and OCIRepository definitions
├── scripts/                     # Local maintenance and validation scripts
│   └── validate-schemas.py      # Automated TrueCharts JSON schema validator
├── GEMINI.md                    # This persistent reference file
└── age.agekey                   # SOPS encryption age key (keep secure)
```

---

## 3. Schema Enforcement & The Common 30.4.x Upgrade

Starting with TrueCharts **Common Library 30.4.0+** (released Nov 2026), charts enforce **strict JSON Schema validation** on all `values.yaml` inputs. Previously ignored typos or obsolete parameters now cause hard Helm upgrade failures.

### Schema References
- **Hosted Flux HelmRelease Schema:**
  ```yaml
  # yaml-language-server: $schema=https://truecharts.org/schemas/helm-release/30.4.4.schema.json
  ```
  *(Add this at line 2 of `helm-release.yaml` files for in-editor autocompletion and live validation.)*
- **Raw Website Schemas:**
  `https://raw.githubusercontent.com/trueforge-org/websites/refs/heads/main/truecharts/public/schemas/helm-release/<version>.schema.json`
  `https://raw.githubusercontent.com/trueforge-org/websites/refs/heads/main/truecharts/public/schemas/common/<version>.schema.json`
- **Source Schemas in `code/truecharts`:**
  - Full schema: `charts/library/common/values.schema.json`
  - Modular definitions: `charts/library/common/schemas/` (`credentials.json`, `cnpg.json`, `persistence/`, etc.)

### Common Strict Schema Violations & Fixes

1. **`credentials.<target>.name`**:
   - **Error:** `Additional properties are not allowed ('name' was unexpected)`
   - **Cause:** Credentials map keys (`credentials.cloudflare:`) define the name. A nested `name: cloudflare` property is redundant and disallowed (`additionalProperties: false`).
   - **Fix:** Remove `name: <target>` from the credentials block.

2. **`cnpg.<target>.recovery.enabled`**:
   - **Error:** `Additional properties are not allowed ('enabled' was unexpected)`
   - **Cause:** CNPG operation mode is toggled by `mode: standalone | replica | recovery`. The `recovery:` block does NOT have an `enabled: false` flag.
   - **Fix:** When running in `mode: standalone`, remove the entire `recovery:` block (or remove `enabled: false`).

3. **`cnpg.<target>.backups.retentionPolicy`**:
   - **Error:** `'' does not match '^[1-9][0-9]*[dwm]$'`
   - **Cause:** An empty string `""` violates the regex format.
   - **Fix:** Remove `retentionPolicy` entirely or supply a valid duration (e.g., `30d`, `4w`).

4. **Empty `values:` block**:
   - **Fix:** If a chart needs no custom values, use `values: {}` or omit cleanly so it doesn't parse as `None`.

---

## 4. Key CLI Tools & Commands

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
   - Removed obsolete `name: cloudflare` from credentials in: `filebrowser`, `calibre`, `jackett`, `kavita`, `plex`, `qbittorrent-main`, `radarr`, `sonarr`, `recipes`, and `wedding`.
