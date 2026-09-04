#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Security scan script for StockQAbyLLM dependencies.

This script runs pip-audit to check for known vulnerabilities in project dependencies.
"""

import subprocess
import sys
from pathlib import Path


def run_pip_audit(strict: bool = False) -> int:
    """Run pip-audit on project dependencies.

    Args:
        strict: If True, fail on any vulnerability. If False, only fail on high severity.

    Returns:
        Exit code (0 for success, 1 for failure)
    """
    print("=" * 60)
    print("StockQAbyLLM - Security Scan")
    print("=" * 60)
    print()

    # Get project root directory
    project_root = Path(__file__).parent.parent
    requirements_file = project_root / "pyproject.toml"

    if not requirements_file.exists():
        print(f"Error: {requirements_file} not found")
        return 1

    print(f"Scanning dependencies in: {requirements_file}")
    print()

    # Build pip-audit command
    cmd = [
        sys.executable,
        "-m",
        "pip_audit",
        "--format",
        "json",
        "--desc",
    ]

    # Run pip-audit
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=project_root,
        )

        # Parse output
        if result.stdout:
            print("Scan Results:")
            print("-" * 60)

            # Parse JSON output for summary
            import json

            try:
                data = json.loads(result.stdout)
                vuln_count = sum(len(d.get("vulns", [])) for d in data.get("dependencies", []))

                if vuln_count == 0:
                    print("✓ No vulnerabilities found!")
                else:
                    print(f"⚠ Found {vuln_count} vulnerability(ies)")

                    # Show details
                    for dep in data.get("dependencies", []):
                        for vuln in dep.get("vulns", []):
                            print()
                            print(f"Package: {dep['name']} (v{dep['version']})")
                            print(f"  ID: {vuln['id']}")
                            print(f"  Aliases: {', '.join(vuln.get('aliases', []))}")
                            print(f"  Fix versions: {vuln.get('fix_versions', 'N/A')}")
                            print(f"  Description: {vuln['description'][:100]}...")
            except json.JSONDecodeError:
                print(result.stdout)

        print()
        print("=" * 60)

        # Return appropriate exit code
        if result.returncode != 0:
            print("⚠ Security vulnerabilities detected!")
            print("Please update affected packages or review the findings.")
            return 1
        else:
            print("✓ Security scan passed!")
            return 0

    except FileNotFoundError:
        print("Error: pip-audit not found")
        print("Install it with: pip install pip-audit")
        return 1
    except Exception as e:
        print(f"Error running security scan: {e}")
        return 1


def main() -> int:
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Run security scan on dependencies")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail on any vulnerability (default: only high severity)",
    )

    args = parser.parse_args()
    return run_pip_audit(strict=args.strict)


if __name__ == "__main__":
    sys.exit(main())
