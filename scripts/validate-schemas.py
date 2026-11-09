#!/usr/bin/env python3
"""
TrueCharts Schema Validator

Pulls the latest published TrueCharts common JSON schema (or a specified version)
and validates HelmRelease manifests (or live cluster releases) against it.
"""

import argparse
import glob
import json
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    import jsonschema
    import requests
    import yaml
    from packaging.version import parse as parse_version
except ImportError as err:
    sys.exit(
        f"Missing required Python dependency: {err}\n"
        f"Install via: pip install pyyaml jsonschema requests packaging"
    )

# ANSI color codes
RESET = "\033[0m"
BOLD = "\033[1m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
GRAY = "\033[90m"

CACHE_DIR = Path.home() / ".cache" / "truecharts-schemas"
GITHUB_API_SCHEMAS_URL = "https://api.github.com/repos/trueforge-org/websites/contents/truecharts/public/schemas/helm-release"
RAW_SCHEMA_BASE_URL = "https://raw.githubusercontent.com/trueforge-org/websites/refs/heads/main/truecharts/public/schemas/common"


def get_latest_schema_version(quiet=False):
    """Detects the latest published schema version from GitHub, local chart, or fallback."""
    # 1. Try GitHub API
    try:
        resp = requests.get(
            GITHUB_API_SCHEMAS_URL,
            headers={"User-Agent": "TrueChartsValidator/1.0"},
            timeout=5,
        )
        if resp.status_code == 200:
            files = resp.json()
            versions = []
            for item in files:
                m = re.match(r"^(\d+\.\d+\.\d+)\.schema\.json$", item.get("name", ""))
                if m:
                    versions.append(m.group(1))
            if versions:
                versions.sort(key=parse_version, reverse=True)
                return versions[0]
    except Exception as e:
        if not quiet:
            print(f"{YELLOW}Warning:{RESET} Could not query GitHub API ({e}), checking fallback...")

    # 2. Try adjacent truecharts repo Chart.yaml if available
    chart_candidates = [
        Path(__file__).resolve().parent.parent.parent / "truecharts" / "charts" / "library" / "common" / "Chart.yaml",
        Path("/home/andrew/code/truecharts/charts/library/common/Chart.yaml"),
    ]
    for candidate in chart_candidates:
        if candidate.is_file():
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                    if data and "version" in data:
                        return str(data["version"])
            except Exception:
                pass

    # 3. Default known fallback
    return "30.4.5"


def fetch_schema(version, refresh=False, quiet=False):
    """Fetches schema from cache or downloads from TrueCharts website repo."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / f"{version}.common.schema.json"

    if cache_file.exists() and not refresh:
        if not quiet:
            print(f"{GRAY}Using cached schema for version {version} ({cache_file}){RESET}")
        with open(cache_file, "r", encoding="utf-8") as f:
            return json.load(f), version

    schema_url = f"{RAW_SCHEMA_BASE_URL}/{version}.schema.json"
    if not quiet:
        print(f"{CYAN}Downloading schema for common {version} from:{RESET} {schema_url}")

    resp = requests.get(schema_url, headers={"User-Agent": "TrueChartsValidator/1.0"}, timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(f"Failed to download schema (HTTP {resp.status_code}): {schema_url}")

    schema = resp.json()
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(schema, f, indent=2)

    return schema, version


def validate_values(validator, values, name, location):
    """Validates values dict using Draft7Validator, returns list of formatted error dicts."""
    if values is None:
        values = {}

    errors = []
    for err in sorted(validator.iter_errors(values), key=lambda e: list(e.path)):
        path_str = "/" + "/".join(str(p) for p in err.path) if err.path else "/"
        hint = ""

        # Provide friendly hints for known common migration mistakes
        if "credentials" in path_str and "'name' was unexpected" in err.message:
            hint = f" ({YELLOW}Hint:{RESET} 'name' is not allowed in credential blocks; the block key is already the name)"
        elif "cnpg" in path_str and "'enabled' was unexpected" in err.message and "recovery" in path_str:
            hint = f" ({YELLOW}Hint:{RESET} recovery mode is toggled via cnpg.<name>.mode: 'recovery', not 'enabled: false')"
        elif "retentionPolicy" in path_str and err.message.endswith("does not match '^[1-9][0-9]*[dwm]$'"):
            hint = f" ({YELLOW}Hint:{RESET} retentionPolicy must be non-empty like '7d', '4w', '1m', or omitted)"

        errors.append({
            "path": path_str,
            "message": err.message,
            "hint": hint,
        })
    return errors


def collect_file_releases(paths, include_non_truecharts=False):
    """Finds all HelmRelease manifests in specified paths."""
    discovered = []
    files_to_scan = []

    for path_str in paths:
        p = Path(path_str)
        if p.is_file():
            files_to_scan.append(p)
        elif p.is_dir():
            files_to_scan.extend(p.rglob("*.yaml"))
            files_to_scan.extend(p.rglob("*.yml"))

    # Deduplicate
    files_to_scan = sorted(list(set(files_to_scan)))

    for fpath in files_to_scan:
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                docs = list(yaml.safe_load_all(f))
        except Exception:
            continue

        for doc in docs:
            if not isinstance(doc, dict):
                continue
            if doc.get("kind") != "HelmRelease":
                continue

            spec = doc.get("spec", {})
            chart_spec = spec.get("chart", {}).get("spec", {})
            source_name = chart_spec.get("sourceRef", {}).get("name", "")
            chart_name = chart_spec.get("chart", "")
            chart_version = chart_spec.get("version", "unknown")

            is_truecharts = (source_name == "truecharts")

            if not is_truecharts and not include_non_truecharts:
                continue

            metadata = doc.get("metadata", {})
            name = metadata.get("name", fpath.stem)
            namespace = metadata.get("namespace", "default")
            values = spec.get("values") or {}

            discovered.append({
                "name": name,
                "namespace": namespace,
                "chart": chart_name,
                "chart_version": chart_version,
                "source": source_name,
                "location": str(fpath),
                "values": values,
                "is_truecharts": is_truecharts,
            })

    return discovered


def collect_live_releases(include_non_truecharts=False):
    """Queries cluster via kubectl for HelmRelease resources."""
    cmd = ["kubectl", "get", "helmreleases", "-A", "-o", "json"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(res.stdout)
    except Exception as e:
        sys.exit(f"{RED}Error fetching HelmReleases from cluster:{RESET} {e}")

    items = data.get("items", [])
    discovered = []

    for item in items:
        spec = item.get("spec", {})
        chart_spec = spec.get("chart", {}).get("spec", {})
        source_name = chart_spec.get("sourceRef", {}).get("name", "")
        chart_name = chart_spec.get("chart", "")
        chart_version = chart_spec.get("version", "unknown")
        is_truecharts = (source_name == "truecharts")

        if not is_truecharts and not include_non_truecharts:
            continue

        metadata = item.get("metadata", {})
        name = metadata.get("name", "unnamed")
        namespace = metadata.get("namespace", "default")
        values = spec.get("values") or {}

        discovered.append({
            "name": name,
            "namespace": namespace,
            "chart": chart_name,
            "chart_version": chart_version,
            "source": source_name,
            "location": f"k8s://{namespace}/{name}",
            "values": values,
            "is_truecharts": is_truecharts,
        })

    return discovered


def main():
    parser = argparse.ArgumentParser(
        description="Validate HelmRelease values against published TrueCharts JSON Schema.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  %(prog)s
  %(prog)s clusters/main/kubernetes/networking/authelia
  %(prog)s --version 30.4.4
  %(prog)s --live
  %(prog)s --schema /path/to/custom/values.schema.json
""",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=[],
        help="Files or directories to scan (default: clusters/main/kubernetes or current directory)",
    )
    parser.add_argument(
        "--version",
        "-v",
        help="Schema version to validate against (default: auto-detected latest)",
    )
    parser.add_argument(
        "--schema",
        "-s",
        help="Path to a custom local schema file instead of downloading",
    )
    parser.add_argument(
        "--live",
        "-l",
        action="store_true",
        help="Validate live HelmReleases from current Kubernetes cluster context via kubectl",
    )
    parser.add_argument(
        "--all",
        "-a",
        action="store_true",
        help="Also validate non-TrueCharts HelmReleases",
    )
    parser.add_argument(
        "--refresh",
        "-r",
        action="store_true",
        help="Force re-downloading schema even if cached locally",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Only output failures and summary",
    )

    args = parser.parse_args()

    # Determine schema to use
    if args.schema:
        schema_path = Path(args.schema)
        if not schema_path.is_file():
            sys.exit(f"{RED}Error:{RESET} Schema file not found: {args.schema}")
        with open(schema_path, "r", encoding="utf-8") as f:
            schema = json.load(f)
        version_label = f"local file ({args.schema})"
    else:
        version = args.version or get_latest_schema_version(quiet=args.quiet)
        schema, version_label = fetch_schema(version, refresh=args.refresh, quiet=args.quiet)

    validator = jsonschema.Draft7Validator(schema)

    # Collect releases
    if args.live:
        if not args.quiet:
            print(f"{CYAN}Collecting live HelmReleases from Kubernetes cluster...{RESET}")
        releases = collect_live_releases(include_non_truecharts=args.all)
    else:
        paths = args.paths
        if not paths:
            # Check default locations
            repo_default = Path(__file__).resolve().parent.parent / "clusters" / "main" / "kubernetes"
            if repo_default.is_dir():
                paths = [str(repo_default)]
            else:
                paths = ["."]
        if not args.quiet:
            print(f"{CYAN}Scanning paths:{RESET} {', '.join(paths)}")
        releases = collect_file_releases(paths, include_non_truecharts=args.all)

    if not releases:
        print(f"{YELLOW}No matching HelmRelease documents found.{RESET}")
        return 0

    if not args.quiet:
        print(f"\n{BOLD}Validating {len(releases)} HelmRelease(s) against schema version {version_label}:{RESET}\n" + "-" * 70)

    passed_count = 0
    failed_count = 0

    for rel in releases:
        name = rel["name"]
        loc = rel["location"]
        chart_info = f"{rel['chart']}@{rel['chart_version']}" if rel["chart"] else "unknown"
        errors = validate_values(validator, rel["values"], name, loc)

        if errors:
            failed_count += 1
            print(f"\n{RED}✖ [{name}]{RESET} {GRAY}({chart_info}){RESET} in {loc}")
            for err in errors:
                print(f"   {RED}•{RESET} {BOLD}{err['path']}{RESET}: {err['message']}{err['hint']}")
        else:
            passed_count += 1
            if not args.quiet:
                print(f"{GREEN}✔ [{name}]{RESET} {GRAY}({chart_info}){RESET} - Valid")

    # Summary
    print("\n" + "=" * 70)
    print(f"{BOLD}Schema Validation Summary:{RESET}")
    print(f"  Schema:  {CYAN}{version_label}{RESET}")
    print(f"  Total:   {len(releases)}")
    print(f"  {GREEN}Passed:  {passed_count}{RESET}")
    if failed_count > 0:
        print(f"  {RED}Failed:  {failed_count}{RESET}")
    else:
        print(f"  Failed:  0")
    print("=" * 70)

    return 1 if failed_count > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
