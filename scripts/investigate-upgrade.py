#!/usr/bin/env python3
"""
Major Version Upgrade Investigator for TrueCharts & Flux

Parses Renovate upgrade strings (e.g. "chore(flux): update image blocky 22.25.1 → 24.1.2"),
inspects git diffs in code/truecharts-org/truecharts between the two versions, validates local manifests
against the new schema, and tests helm template rendering.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import jsonschema
    import requests
    import yaml
except ImportError as err:
    sys.exit(f"Missing dependency: {err}\nInstall via: pip install pyyaml jsonschema requests")

# ANSI colors
RESET = "\033[0m"
BOLD = "\033[1m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
GRAY = "\033[90m"

REPO_ROOT = Path(__file__).resolve().parent.parent
TRUECHARTS_REPO = Path("/home/andrew/code/truecharts-org/truecharts")


def parse_upgrade_string(text):
    """
    Parses various upgrade string formats:
    - "chore(flux): update image blocky 22.25.1 → 24.1.2"
    - "update image blocky 22.25.1 -> 24.1.2"
    - "blocky 22.25.1 24.1.2"
    """
    pattern = r'(?:update\s+(?:image\s+|chart\s+)?|)([a-zA-Z0-9_\-]+)\s+v?([0-9\.]+)\s*(?:→|->|\sto\s|\s+)\s*v?([0-9\.]+)'
    m = re.search(pattern, text)
    if m:
        return m.group(1), m.group(2), m.group(3)
    return None, None, None


def find_chart_dir_in_truecharts(chart_name):
    """Finds the chart directory in truecharts repository."""
    for train in ["stable", "incubator", "dependency", "library"]:
        p = TRUECHARTS_REPO / "charts" / train / chart_name
        if p.is_dir():
            return p, train
    return None, None


def find_commits_for_versions(chart_rel_path, old_version, new_version):
    """Finds git commit hashes for old and new versions of the chart."""
    chart_yaml_path = f"{chart_rel_path}/Chart.yaml"

    cmd = ["git", "-C", str(TRUECHARTS_REPO), "log", "--format=%H %s", "--", chart_yaml_path]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    commits = res.stdout.strip().splitlines()

    old_commit = None
    new_commit = None

    for line in commits:
        if not line:
            continue
        h, subject = line.split(" ", 1)
        # Check version in this commit
        cmd_show = ["git", "-C", str(TRUECHARTS_REPO), "show", f"{h}:{chart_yaml_path}"]
        res_show = subprocess.run(cmd_show, capture_output=True, text=True)
        if res_show.returncode == 0:
            m = re.search(r'^version:\s*([0-9\.]+)', res_show.stdout, re.M)
            if m:
                v = m.group(1).strip()
                if v == old_version and not old_commit:
                    old_commit = h
                if v == new_version and not new_commit:
                    new_commit = h

    # If new_version matches HEAD, use HEAD
    if not new_commit:
        head_chart = TRUECHARTS_REPO / chart_yaml_path
        if head_chart.is_file():
            with open(head_chart, "r") as f:
                data = yaml.safe_load(f)
                if data and str(data.get("version")) == new_version:
                    new_commit = "HEAD"

    return old_commit, new_commit


def get_chart_info_at_commit(chart_rel_path, commit):
    """Returns parsed Chart.yaml at a given commit (or HEAD/working tree)."""
    chart_yaml_path = f"{chart_rel_path}/Chart.yaml"
    if not commit:
        head_chart = TRUECHARTS_REPO / chart_yaml_path
        if head_chart.is_file():
            with open(head_chart, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        return {}
    cmd_show = ["git", "-C", str(TRUECHARTS_REPO), "show", f"{commit}:{chart_yaml_path}"]
    res_show = subprocess.run(cmd_show, capture_output=True, text=True)
    if res_show.returncode == 0:
        try:
            return yaml.safe_load(res_show.stdout) or {}
        except Exception:
            return {}
    return {}


def get_common_version(chart_data):
    """Extracts the 'common' dependency version from Chart.yaml data."""
    for dep in chart_data.get("dependencies", []) or []:
        if isinstance(dep, dict) and dep.get("name") == "common":
            return str(dep.get("version", ""))
    return None


def analyze_common2026_risks(values, old_common_ver, new_common_ver):
    """
    Detects breaking changes when upgrading a chart to Common-2026 (common >= 32.0.0),
    specifically around CNPG -> dependencies.postgresql, Barman Cloud plugin, PG 18 default,
    and other dependency migrations.
    """
    warnings = []
    blockers = []

    def is_v32_or_newer(v):
        if not v:
            return False
        m = re.match(r"^(\d+)\.", str(v))
        return bool(m and int(m.group(1)) >= 32)

    moving_to_common2026 = is_v32_or_newer(new_common_ver) and not is_v32_or_newer(old_common_ver)
    on_common2026 = is_v32_or_newer(new_common_ver)

    if moving_to_common2026:
        warnings.append(
            f"{BOLD}Common-2026 (32.x) Major Rework Detected:{RESET} "
            f"Underlying common library jumps from {YELLOW}{old_common_ver or 'unknown'}{RESET} → {GREEN}{new_common_ver}{RESET}."
        )

    if not isinstance(values, dict):
        return warnings, blockers

    # 1. Check legacy top-level cnpg block
    cnpg = values.get("cnpg")
    if isinstance(cnpg, dict):
        for cname, ccfg in cnpg.items():
            if isinstance(ccfg, dict) and ccfg.get("enabled", True) is not False:
                if on_common2026:
                    blockers.append(
                        f"{BOLD}[CNPG → dependencies.postgresql Migration Required]{RESET}\n"
                        f"    • Top-level {RED}cnpg.{cname}{RESET} fails rendering in Common-2026 (32.0.0+).\n"
                        f"    • Rewrite under {GREEN}dependencies.postgresql{RESET}:\n"
                        f"      - Set {CYAN}dependencies.postgresql.enabled: true{RESET} (default mode is {CYAN}operator{RESET}).\n"
                        f"      - Move {CYAN}user{RESET}, {CYAN}database{RESET}, and {CYAN}password{RESET} to {CYAN}dependencies.postgresql.depconfig{RESET} as "
                        f"{CYAN}postgresqlUsername{RESET}, {CYAN}postgresqlDatabase{RESET}, and {CYAN}password{RESET} (or omit user/db if chart defaults apply).\n"
                        f"      - Move {CYAN}cluster{RESET}, {CYAN}backups{RESET}, {CYAN}recovery{RESET}, {CYAN}monitoring{RESET}, {CYAN}pooler{RESET}, and {CYAN}pgVersion{RESET} "
                        f"under {CYAN}dependencies.postgresql.operator{RESET}.\n"
                        f"    • {YELLOW}Pin PostgreSQL Version:{RESET} Common-2026 defaults to {BOLD}PostgreSQL 18{RESET} (Debian 13 / Trixie) and triggers an automatic in-place "
                        f"{CYAN}pg_upgrade{RESET} if unset! Explicitly set {CYAN}dependencies.postgresql.operator.pgVersion: {ccfg.get('pgVersion', 16)}{RESET} before upgrading to stay on your current version.\n"
                        f"    • {YELLOW}Barman Cloud Plugin Required:{RESET} In-tree Barman is removed. Backups/recovery now spawn {CYAN}barmancloud.cnpg.io/v1 ObjectStore{RESET} resources "
                        f"and require {BOLD}plugin-barman-cloud{RESET} (plus CNPG >= 1.26 and cert-manager) installed in the CNPG operator namespace."
                    )
                else:
                    warnings.append(
                        f"Chart uses legacy {CYAN}cnpg.{cname}{RESET}. Note that when this chart eventually moves to Common-2026 (32.x), "
                        f"it will require migrating to {CYAN}dependencies.postgresql{RESET} and the CNPG {CYAN}plugin-barman-cloud{RESET}."
                    )

    # 2. Check new dependencies.postgresql block on Common-2026
    deps = values.get("dependencies")
    if isinstance(deps, dict) and isinstance(deps.get("postgresql"), dict):
        pg_dep = deps["postgresql"]
        if pg_dep.get("enabled", True) is not False and pg_dep.get("mode", "operator") == "operator":
            op = pg_dep.get("operator") or {}
            if "pgVersion" not in op:
                warnings.append(
                    f"{BOLD}[PostgreSQL 18 Default Warning]{RESET} {CYAN}dependencies.postgresql.operator.pgVersion{RESET} is not explicitly set. "
                    f"Common-2026 defaults to {BOLD}PostgreSQL 18{RESET} and will run an in-place {CYAN}pg_upgrade{RESET} on existing clusters. "
                    f"Set {CYAN}pgVersion: 16{RESET} (or your current version) to prevent an unplanned major DB upgrade."
                )
            backups_enabled = isinstance(op.get("backups"), dict) and op["backups"].get("enabled")
            is_recovery = op.get("mode") == "recovery"
            if backups_enabled or is_recovery:
                warnings.append(
                    f"{BOLD}[CNPG Barman Cloud Plugin Prerequisite]{RESET} Backups/recovery are enabled under {CYAN}dependencies.postgresql.operator{RESET}. "
                    f"Ensure {BOLD}plugin-barman-cloud{RESET} ({CYAN}barmancloud.cnpg.io/v1/ObjectStore{RESET}) is deployed in the CNPG operator namespace before applying."
                )
            if isinstance(op.get("backups"), dict) and op["backups"].get("manualBackups"):
                warnings.append(
                    f"{BOLD}[Immutable Manual Backups]{RESET} Delete or rename existing CNPG {CYAN}Backup{RESET} resources created from {CYAN}manualBackups{RESET} before upgrading, as their spec is immutable."
                )

    # 3. Check legacy cnpg-main-urls secretKeyRef
    raw_yaml = yaml.dump(values)
    if "cnpg-main-urls" in raw_yaml:
        msg = (
            f"{BOLD}[Removed Secret 'cnpg-main-urls']{RESET} Local values reference {RED}cnpg-main-urls{RESET} (e.g. key {RED}std{RESET}). "
            f"In Common-2026 (32.x), this secret is replaced by {GREEN}<release>-postgresqlcreds{RESET} (keys: {CYAN}url{RESET}, {CYAN}urlnossl{RESET}, {CYAN}jdbc{RESET}, {CYAN}plainhost{RESET}, etc.) "
            f"or chart auto-binding via {CYAN}.Values.dependencies.postgresql.depconfig.creds{RESET}."
        )
        if on_common2026:
            blockers.append(msg)
        else:
            warnings.append(msg)

    # 4. Check other legacy top-level dependencies (redis, valkey, mariadb, mongodb, clickhouse, memcached)
    for old_dep, new_dep in [("redis", "valkey"), ("valkey", "valkey"), ("mariadb", "mariadb"), ("mongodb", "mongodb"), ("clickhouse", "clickhouse"), ("memcached", "memcached")]:
        dep_cfg = values.get(old_dep)
        if isinstance(dep_cfg, dict) and dep_cfg.get("enabled") is True:
            extra_note = ""
            if new_dep in ("mariadb", "mongodb", "clickhouse"):
                extra_note = f" {RED}WARNING:{RESET} {new_dep} uses a new containerforge image and volume layout—data is NOT automatically migrated (dump before upgrading and restore after)!"
            msg = (
                f"{BOLD}[Legacy '{old_dep}' Dependency Block]{RESET} Top-level {RED}{old_dep}{RESET} is replaced by {GREEN}dependencies.{new_dep}{RESET} in Common-2026.{extra_note}"
            )
            if on_common2026:
                blockers.append(msg)
            else:
                warnings.append(msg)

    return warnings, blockers
def load_clusterenv():
    """Loads clusters/main/clusterenv.yaml and enforces that secrets are decrypted."""
    env_file = REPO_ROOT / "clusters" / "main" / "clusterenv.yaml"
    if not env_file.exists():
        sys.exit(f"{RED}Error:{RESET} clusters/main/clusterenv.yaml not found at {env_file}")

    with open(env_file, "r", encoding="utf-8") as f:
        content = f.read()

    if "sops:" in content:
        sys.exit(
            f"{RED}Error:{RESET} clusters/main/clusterenv.yaml is encrypted with SOPS.\n"
            f"Please decrypt the secrets file first by running:\n"
            f"  {BOLD}{CYAN}./clustertool decrypt{RESET}\n"
            f"Then re-run this investigation tool."
        )

    try:
        parsed = yaml.safe_load(content)
        if not isinstance(parsed, dict):
            sys.exit(f"{RED}Error:{RESET} Failed to parse clusters/main/clusterenv.yaml as a dictionary.")
        return {str(k): str(v) for k, v in parsed.items() if v is not None}
    except Exception as e:
        sys.exit(f"{RED}Error:{RESET} Could not parse clusters/main/clusterenv.yaml: {e}")


def substitute_env_vars(data, env_vars):
    """Recursively replaces ${VAR} in values using clusterenv variables."""
    if isinstance(data, str):
        def repl(m):
            var_name = m.group(1)
            return env_vars.get(var_name, m.group(0))
        return re.sub(r"\$\{([^}]+)\}", repl, data)
    elif isinstance(data, dict):
        return {k: substitute_env_vars(v, env_vars) for k, v in data.items()}
    elif isinstance(data, list):
        return [substitute_env_vars(elem, env_vars) for elem in data]
    return data


def find_local_manifest(chart_name):
    """Finds the HelmRelease manifest in clusters/main/kubernetes for the chart."""
    manifests = list(REPO_ROOT.glob("clusters/main/kubernetes/**/helm-release.yaml"))
    for m in manifests:
        try:
            with open(m, "r", encoding="utf-8") as f:
                docs = list(yaml.safe_load_all(f))
            for doc in docs:
                if not isinstance(doc, dict):
                    continue
                if doc.get("kind") != "HelmRelease":
                    continue
                c_name = doc.get("spec", {}).get("chart", {}).get("spec", {}).get("chart")
                if c_name == chart_name:
                    return m, doc
        except Exception:
            continue
    return None, None


def main():
    parser = argparse.ArgumentParser(
        description="Investigate a Renovate major version upgrade for TrueCharts/Flux.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  %(prog)s "chore(flux): update image blocky 22.25.1 → 24.1.2"
  %(prog)s blocky 22.25.1 24.1.2
  %(prog)s "chore(flux): update image plex 23.0.9 → 23.1.2" --apply
""",
    )
    parser.add_argument("upgrade_input", nargs="*", help="Renovate commit/PR string or 'chart old new'")
    parser.add_argument("--chart", help="Chart name")
    parser.add_argument("--old", help="Old chart version")
    parser.add_argument("--new", help="New chart version")
    parser.add_argument("--apply", action="store_true", help="Apply the version bump and schema header if valid")

    args = parser.parse_args()

    full_input = " ".join(args.upgrade_input).strip()
    chart = args.chart
    old_ver = args.old
    new_ver = args.new

    if full_input:
        c, o, n = parse_upgrade_string(full_input)
        if c and o and n:
            chart, old_ver, new_ver = c, o, n

    if not (chart and old_ver and new_ver):
        sys.exit(f"{RED}Error:{RESET} Could not parse chart, old version, and new version from input.\nUsage: {parser.prog} 'chore(flux): update image <chart> <old> → <new>'")

    # Enforce decrypted clusterenv.yaml so substitutions succeed during Helm templating
    env_vars = load_clusterenv()

    print(f"\n{BOLD}{CYAN}=== Investigating Major Version Upgrade ==={RESET}")
    print(f"Chart:       {BOLD}{chart}{RESET}")
    print(f"Upgrade:     {YELLOW}{old_ver}{RESET} → {GREEN}{new_ver}{RESET}")
    print("=" * 60)

    # 1. Locate in code/truecharts-org/truecharts
    chart_dir, train = find_chart_dir_in_truecharts(chart)
    if not chart_dir:
        sys.exit(f"{RED}Error:{RESET} Chart '{chart}' not found in {TRUECHARTS_REPO}/charts/")

    rel_chart_path = f"charts/{train}/{chart}"
    print(f"{GRAY}Found chart in TrueCharts repo:{RESET} {rel_chart_path}")

    # 2. Find commits
    old_commit, new_commit = find_commits_for_versions(rel_chart_path, old_ver, new_ver)
    if not old_commit:
        print(f"{YELLOW}Warning:{RESET} Could not find exact commit for version {old_ver} (may be very old).")
    if not new_commit:
        new_commit = "HEAD"

    print(f"Commit range: {old_commit[:10] if old_commit else 'unknown'}..{new_commit[:10] if new_commit != 'HEAD' else 'HEAD'}")

    old_chart_data = get_chart_info_at_commit(rel_chart_path, old_commit) if old_commit else {}
    new_chart_data = get_chart_info_at_commit(rel_chart_path, new_commit)
    old_common_ver = get_common_version(old_chart_data)
    new_common_ver = get_common_version(new_chart_data)
    if old_common_ver or new_common_ver:
        print(f"Common Lib:  {YELLOW}{old_common_ver or 'unknown'}{RESET} → {GREEN}{new_common_ver or 'unknown'}{RESET}")

    # 3. Analyze differences in Chart.yaml and values.yaml
    if old_commit and new_commit:
        print(f"\n{BOLD}1. Dependency & App Changes (Chart.yaml):{RESET}")
        cmd_diff = ["git", "-C", str(TRUECHARTS_REPO), "diff", old_commit, new_commit, "--", f"{rel_chart_path}/Chart.yaml"]
        res_diff = subprocess.run(cmd_diff, capture_output=True, text=True)
        for line in res_diff.stdout.splitlines():
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
                color = GREEN if line.startswith("+") else RED
                print(f"  {color}{line}{RESET}")

        print(f"\n{BOLD}2. Configuration & Default Changes (values.yaml):{RESET}")
        cmd_vdiff = ["git", "-C", str(TRUECHARTS_REPO), "diff", old_commit, new_commit, "--", f"{rel_chart_path}/values.yaml"]
        res_vdiff = subprocess.run(cmd_vdiff, capture_output=True, text=True)
        vdiff_lines = [l for l in res_vdiff.stdout.splitlines() if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))]
        if vdiff_lines:
            for l in vdiff_lines[:25]:
                color = GREEN if l.startswith("+") else RED
                print(f"  {color}{l}{RESET}")
            if len(vdiff_lines) > 25:
                print(f"  {GRAY}... ({len(vdiff_lines) - 25} more lines modified){RESET}")
        else:
            print("  No direct changes in values.yaml defaults.")

        print(f"\n{BOLD}3. Git Commit History between {old_ver} and {new_ver}:{RESET}")
        cmd_log = ["git", "-C", str(TRUECHARTS_REPO), "log", "--oneline", f"{old_commit}..{new_commit}", "--", rel_chart_path]
        res_log = subprocess.run(cmd_log, capture_output=True, text=True)
        commits_in_range = res_log.stdout.strip().splitlines()
        for c in commits_in_range[:10]:
            print(f"  {GRAY}•{RESET} {c}")
        if len(commits_in_range) > 10:
            print(f"  {GRAY}... ({len(commits_in_range) - 10} more commits){RESET}")

    # 4. Check local deployment manifest
    print(f"\n{BOLD}4. Local Deployment Analysis:{RESET}")
    local_manifest, doc = find_local_manifest(chart)
    if not local_manifest:
        print(f"{YELLOW}Warning:{RESET} Could not find local HelmRelease for chart '{chart}'.")
        return 0

    print(f"  Manifest: {CYAN}{local_manifest.relative_to(REPO_ROOT)}{RESET}")
    values = doc.get("spec", {}).get("values") or {}

    # 4b. Common-2026 & CNPG Breaking Change Risk Analysis
    warnings, blockers = analyze_common2026_risks(values, old_common_ver, new_common_ver)
    if warnings or blockers:
        print(f"\n{BOLD}4b. Common-2026 & Dependency Migration Analysis:{RESET}")
        for b in blockers:
            print(f"  {RED}✖ BLOCKER:{RESET} {b}")
        for w in warnings:
            print(f"  {YELLOW}⚠ WARNING:{RESET} {w}")

    # 5. Schema Validation
    target_schema_ver = new_common_ver or "32.3.0"
    print(f"\n{BOLD}5. Schema Validation for New Version ({new_ver}, common {target_schema_ver}):{RESET}")
    # Run validator script against the target chart's common version
    val_script = REPO_ROOT / "scripts" / "validate-schemas.py"
    val_cmd = [sys.executable, str(val_script), str(local_manifest), "--version", target_schema_ver, "--quiet"]
    res_val = subprocess.run(val_cmd, capture_output=True, text=True)
    if res_val.returncode == 0 and not blockers:
        print(f"  {GREEN}✔ Schema validation passed!{RESET} Current values comply with common {target_schema_ver} schema.")
    else:
        print(f"  {RED}✖ Schema / Common-2026 validation failed on current values:{RESET}")
        if res_val.stdout.strip():
            print(res_val.stdout)
        print(f"  {YELLOW}Action required:{RESET} Fix the above schema/deprecation violations before updating.")

    # 6. Helm Template Rendering Verification
    print(f"\n{BOLD}6. Helm Template Rendering Verification:{RESET}")
    with tempfile.TemporaryDirectory() as tmpdir:
        chart_dest = Path(tmpdir) / chart
        cmd_pull = ["helm", "pull", f"oci://oci.trueforge.org/truecharts/{chart}", "--version", new_ver, "--untar", "--untardir", tmpdir]
        res_pull = subprocess.run(cmd_pull, capture_output=True, text=True)
        if res_pull.returncode != 0:
            print(f"  {YELLOW}Notice:{RESET} Could not pull chart from OCI ({res_pull.stderr.strip()}). Checking local chart...")
            chart_dest = chart_dir

        # Write values to temp file with clusterenv substitutions applied
        val_file = Path(tmpdir) / "values.yaml"
        substituted_values = substitute_env_vars(values, env_vars)
        with open(val_file, "w", encoding="utf-8") as f:
            yaml.dump(substituted_values, f)

        # Pass --api-versions barmancloud.cnpg.io/v1/ObjectStore because helm template runs offline without cluster API discovery,
        # and Common-2026 requires this capability check when CNPG backups or recovery are enabled.
        cmd_template = [
            "helm", "template", chart, str(chart_dest),
            "-f", str(val_file),
            "--kube-version", "1.33.0",
            "--api-versions", "barmancloud.cnpg.io/v1/ObjectStore",
        ]
        res_template = subprocess.run(cmd_template, capture_output=True, text=True)

        if res_template.returncode == 0:
            print(f"  {GREEN}✔ Helm template succeeded!{RESET} The chart renders successfully with your configuration.")
            safe_to_apply = (len(blockers) == 0)
        else:
            print(f"  {RED}✖ Helm template failed:{RESET}\n{res_template.stderr.strip()}")
            safe_to_apply = False

        # Check live cluster for Barman Cloud plugin CRD if this chart uses CNPG backups/recovery on Common-2026
        if new_common_ver and re.match(r"^(3[2-9]|[4-9]\d)\.", str(new_common_ver)):
            raw_vals = yaml.dump(values)
            if "postgresql" in raw_vals or "cnpg" in raw_vals:
                crd_check = subprocess.run(
                    ["kubectl", "get", "crd", "objectstores.barmancloud.cnpg.io"],
                    capture_output=True, text=True
                )
                if crd_check.returncode != 0:
                    print(
                        f"  {YELLOW}⚠ Live Cluster Check:{RESET} CRD {BOLD}objectstores.barmancloud.cnpg.io{RESET} was not detected on the current cluster.\n"
                        f"    Common-2026 requires {BOLD}plugin-barman-cloud{RESET} installed alongside the CloudNativePG operator when backups or recovery are enabled!"
                    )

    # 7. Summary & Recommendation
    print("\n" + "=" * 60)
    print(f"{BOLD}Upgrade Summary & Recommendation:{RESET}")
    if safe_to_apply and res_val.returncode == 0:
        print(f"  Status: {GREEN}{BOLD}SAFE TO UPGRADE{RESET}")
        print(f"  No breaking changes or schema conflicts detected for your current values.")

        if args.apply:
            print(f"\n{CYAN}Applying upgrade to {local_manifest.relative_to(REPO_ROOT)}...{RESET}")
            content = local_manifest.read_text(encoding="utf-8")
            # Update version
            content = re.sub(r'version:\s*' + re.escape(old_ver), f"version: {new_ver}", content)
            # Ensure schema header is present and up-to-date
            schema_header = f"# yaml-language-server: $schema=https://truecharts.org/schemas/helm-release/{target_schema_ver}.schema.json"
            if "yaml-language-server: $schema" not in content:
                content = f"---\n{schema_header}\n" + content.lstrip("-\n")
            else:
                content = re.sub(
                    r"#\s*yaml-language-server:\s*\$schema=https://truecharts\.org/schemas/helm-release/[0-9\.]+\.schema\.json",
                    schema_header,
                    content,
                )
            local_manifest.write_text(content, encoding="utf-8")
            print(f"{GREEN}✔ Successfully updated {local_manifest.relative_to(REPO_ROOT)} to version {new_ver}!{RESET}")
        else:
            print(f"\n  To apply this change automatically, run:")
            print(f"  {CYAN}{sys.argv[0]} \"{full_input}\" --apply{RESET}")
    else:
        print(f"  Status: {RED}{BOLD}MANUAL ADJUSTMENT REQUIRED{RESET}")
        print(f"  Please address the schema/template errors above before applying.")
    print("=" * 60 + "\n")

    return 0 if safe_to_apply else 1


if __name__ == "__main__":
    sys.exit(main())
