#!/usr/bin/env python3
"""
Setup provider-hub: configures MCPs, dependencies, and tools.

Environment-aware: detects Copilot CLI, Claude Code CLI, Claude Desktop, VS Code, etc.
and registers this repo's MCPs directly into the right config file for that environment.

No external dependencies—uses stdlib only. Automatically backs up configs before changes.

Usage:
    python setup.py                    # Interactive menu
    python setup.py --check           # Report current status
    python setup.py --install all     # Install everything
    python setup.py --backups         # List available backups
    python setup.py --restore         # Restore from backup
"""

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from environment import Environment, detect_environment, get_environment_info

REPO_ROOT = Path(__file__).resolve().parents[4]
# pde-jsm ships as part of the `pde` Claude Code plugin now (see plugins/pde/);
# this path is still used for the Copilot CLI / manual-registration fallback below.
PDE_JSM_DIR = REPO_ROOT / "plugins" / "pde" / "mcp-servers" / "pde-jsm"


def run_cmd(cmd: list, check: bool = False, timeout: int = 30) -> tuple[int, str, str]:
    """Run command and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return 1, "", "Command timed out"
    except Exception as e:
        return 1, "", str(e)


def get_backup_dir() -> Path:
    """Get or create backup directory in user's home."""
    backup_dir = Path.home() / ".provider-hub-backups"
    backup_dir.mkdir(exist_ok=True, parents=True)
    return backup_dir


def backup_file(file_path: Path, manifest_path: Path) -> Optional[Path]:
    """
    Backup a file with timestamp. Returns backup path or None if not found.
    Appends entry to manifest.
    """
    if not file_path.exists():
        return None

    backup_dir = get_backup_dir()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"{file_path.name}.{timestamp}.bak"
    backup_path = backup_dir / backup_name

    try:
        if file_path.is_file():
            shutil.copy2(file_path, backup_path)
        else:
            shutil.copytree(file_path, backup_path, dirs_exist_ok=True)

        # Append to manifest
        entry = {
            "timestamp": timestamp,
            "original_path": str(file_path.absolute()),
            "backup_path": str(backup_path.absolute()),
            "is_dir": file_path.is_dir(),
        }

        manifest_data = []
        if manifest_path.exists():
            try:
                with open(manifest_path) as f:
                    manifest_data = json.load(f)
            except json.JSONDecodeError:
                pass

        manifest_data.append(entry)
        with open(manifest_path, "w") as f:
            json.dump(manifest_data, f, indent=2)

        return backup_path
    except Exception as e:
        print(f"  ⚠️  Failed to backup {file_path}: {e}")
        return None


def restore_from_backup() -> bool:
    """Interactively restore a file from backup."""
    try:
        backup_dir = get_backup_dir()

        # Find all backup files
        backup_files = sorted(
            [f for f in backup_dir.glob("*") if f.suffix == ".bak"],
            key=lambda x: x.stat().st_mtime,
            reverse=True,
        )

        if not backup_files:
            print("  ❌ No backups found")
            return False

        # Show available backups and let user choose
        print("\n  Recent backups:\n")
        for i, backup in enumerate(backup_files[:10], 1):
            mtime = datetime.fromtimestamp(backup.stat().st_mtime)
            print(f"    {i}) {backup.name}")
            print(f"       {backup.parent.name}: {mtime.strftime('%Y-%m-%d %H:%M:%S')}")

        try:
            choice = int(input("\n  Select backup to restore (number): ")) - 1
            if choice < 0 or choice >= len(backup_files[:10]):
                print("  Invalid choice")
                return False
        except ValueError:
            print("  Invalid input")
            return False

        backup_path = backup_files[choice]

        # Infer original path from backup filename: <filename>.<timestamp>.bak
        parts = backup_path.name.rsplit(".", 2)
        original_name = parts[0] if len(parts) == 3 else backup_path.name

        manifest_path = get_backup_dir() / "manifest.json"
        original_path = None
        if manifest_path.exists():
            try:
                with open(manifest_path) as f:
                    manifest_data = json.load(f)
                for entry in manifest_data:
                    if entry.get("backup_path") == str(backup_path.absolute()):
                        original_path = Path(entry["original_path"])
                        break
            except (json.JSONDecodeError, IOError, KeyError):
                pass

        print(f"\n  This backup is for: {original_name}")
        if original_path:
            print(f"  Original location: {original_path}")
        mtime = datetime.fromtimestamp(backup_path.stat().st_mtime)
        print(f"  Backup date: {mtime.strftime('%Y-%m-%d %H:%M:%S')}")

        if not original_path:
            print("\n  ⚠️  Could not determine the original location from the manifest.")
            print(f"     To restore manually: cp {backup_path} <original location>")
            return False

        confirm = input(f"\n  ⚠️  This will overwrite {original_path}. Continue? (yes/no): ")
        if confirm.lower() != "yes":
            print("  Cancelled")
            return False

        if original_path.exists():
            # Back up the current file before overwriting it, so this is reversible too.
            backup_file(original_path, manifest_path)

        original_path.parent.mkdir(parents=True, exist_ok=True)
        if backup_path.is_dir():
            shutil.copytree(backup_path, original_path, dirs_exist_ok=True)
        else:
            shutil.copy2(backup_path, original_path)

        print(f"\n  ✅ Restored {original_path} from {backup_path.name}")
        return True
    except Exception as e:
        print(f"  ❌ Restore failed: {e}")
        return False


def list_backups():
    """List all available backups."""
    backup_dir = get_backup_dir()

    print_header("Available Backups")

    backups = sorted(
        [f for f in backup_dir.glob("*") if f.suffix == ".bak"],
        key=lambda x: x.stat().st_mtime,
        reverse=True,
    )

    if not backups:
        print("  No backups found")
        return

    print(f"  Backups stored in: {backup_dir}\n")
    for backup in backups[:20]:
        mtime = datetime.fromtimestamp(backup.stat().st_mtime)
        size = backup.stat().st_size / 1024  # KB
        print(f"  📦 {backup.name}")
        print(f"     Created: {mtime.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"     Size: {size:.1f} KB\n")


def check_command_exists(cmd: str) -> bool:
    """Check if a command is available in PATH."""
    rc, _, _ = run_cmd(["which", cmd] if sys.platform != "win32" else ["where", cmd])
    return rc == 0


def check_python_package(package: str) -> bool:
    """Check if a Python package is installed."""
    rc, _, _ = run_cmd([sys.executable, "-c", f"import {package}"])
    return rc == 0


def _mcp_config_path(env: Environment, metadata: dict) -> Optional[Path]:
    """Return the config file that holds mcpServers for the given environment."""
    if env == Environment.COPILOT_CLI:
        return metadata.get("mcp_config")
    return metadata.get("config_file")


def _read_mcp_servers(env: Environment, metadata: dict, project_root: Path) -> Optional[dict]:
    """Read the mcpServers dict for this environment. None if unknown/unreadable."""
    config_file = _mcp_config_path(env, metadata)
    if not config_file or not config_file.exists():
        return None

    try:
        with open(config_file) as f:
            config = json.load(f)
    except (json.JSONDecodeError, IOError):
        return None

    if env == Environment.CLAUDE_CODE_CLI or env == Environment.VSCODE_CLAUDE:
        return config.get("projects", {}).get(str(project_root), {}).get("mcpServers", {})
    return config.get("mcpServers", {})


def check_mcp_installed(mcp_name: str, project_root: Path = REPO_ROOT) -> Optional[bool]:
    """Check if an MCP is registered for the detected environment. None if unknown."""
    env, metadata = detect_environment()
    servers = _read_mcp_servers(env, metadata, project_root)
    if servers is None:
        return None
    return mcp_name in servers


def register_mcp(
    mcp_name: str,
    command: str,
    args: list,
    project_root: Path = REPO_ROOT,
    env_vars: Optional[dict] = None,
) -> bool:
    """Register an MCP server into the config file for the detected environment."""
    env, metadata = detect_environment()
    config_file = _mcp_config_path(env, metadata)

    if config_file is None:
        print(f"  ❌ Cannot register '{mcp_name}': unrecognized environment.")
        print("     Run with --check to see environment detection details.")
        return False

    manifest_path = get_backup_dir() / "manifest.json"
    if config_file.exists():
        backup_file(config_file, manifest_path)
        try:
            with open(config_file) as f:
                config = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"  ❌ Could not parse existing config at {config_file}: {e}")
            return False
    else:
        config = {}

    if env == Environment.CLAUDE_CODE_CLI or env == Environment.VSCODE_CLAUDE:
        servers = config.setdefault("projects", {}).setdefault(str(project_root), {}).setdefault("mcpServers", {})
    else:
        servers = config.setdefault("mcpServers", {})

    entry = {"command": command, "args": args}
    if env_vars:
        entry["env"] = env_vars
    servers[mcp_name] = entry

    config_file.parent.mkdir(parents=True, exist_ok=True)
    with open(config_file, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")

    print(f"  ✅ Registered '{mcp_name}' in {config_file}")
    return True


def register_pde_jsm(project_root: Path = REPO_ROOT) -> bool:
    """Register the in-repo pde-jsm MCP server for the detected environment."""
    app_path = PDE_JSM_DIR / "app.py"
    if not app_path.exists():
        print(f"  ❌ pde-jsm server not found at {app_path}")
        return False
    return register_mcp("pde-jsm", sys.executable, [str(app_path)], project_root)


def register_salesforce_prod(project_root: Path = REPO_ROOT) -> bool:
    """salesforce-prod is a Copilot CLI extension, not a config entry we can write."""
    env, _ = detect_environment()
    if env == Environment.COPILOT_CLI:
        print("  salesforce-prod is a built-in Copilot CLI extension, not a repo-managed MCP.")
        print("  Check if it's installed:  copilot config mcp list | grep salesforce-prod")
        print("  If missing, install it from the Copilot CLI extensions marketplace, then:")
        print("    sf org authenticate org_name:prod")
    else:
        print("  salesforce-prod is a Copilot CLI extension and has no equivalent in this")
        print("  environment. Skills that need Salesforce queries require Copilot CLI, or")
        print("  an equivalent MCP server wrapping the `sf` CLI.")
    return False


def install_python_packages() -> bool:
    """Install pde-jsm's Python dependencies."""
    req_file = PDE_JSM_DIR / "requirements.txt"
    if not req_file.exists():
        print(f"  ❌ requirements.txt not found at {req_file}")
        return False

    print(f"  Installing dependencies from {req_file}...")
    rc, out, err = run_cmd(
        [sys.executable, "-m", "pip", "install", "-r", str(req_file)],
        timeout=180,
    )
    if rc != 0:
        print(f"  ❌ pip install failed: {(err or out).strip()}")
        return False
    print("  ✅ Python packages installed")
    return True


def install_sf_cli() -> bool:
    """Install the Salesforce CLI via npm."""
    if check_command_exists("sf"):
        print("  ✅ Salesforce CLI (sf) already installed")
        return True
    if not check_command_exists("npm"):
        print("  ❌ npm not found; install Node.js/npm first")
        return False
    print("  Installing Salesforce CLI via npm (this can take a minute)...")
    rc, out, err = run_cmd(["npm", "install", "-g", "@salesforce/cli"], timeout=300)
    if rc != 0:
        print(f"  ❌ npm install failed: {(err or out).strip()}")
        return False
    print("  ✅ Salesforce CLI installed. Next: sf org authenticate org_name:prod")
    return True


def set_env_vars() -> bool:
    """Interactively write pde-jsm's .env file with Atlassian (and optional email) credentials."""
    env_path = PDE_JSM_DIR / ".env"
    print(f"  This writes credentials to {env_path} (gitignored, never committed).\n")

    existing = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, _, value = line.partition("=")
                existing[key.strip()] = value.strip()
        print("  A .env already exists; press Enter to keep the current value for a field.\n")

    def prompt(key: str, secret: bool = False, required: bool = True) -> Optional[str]:
        current = existing.get(key)
        label = f"  {key}" + (" (leave blank to keep current)" if current else "")
        value = getpass.getpass(f"{label}: ") if secret else input(f"{label}: ")
        value = value.strip()
        if not value:
            return current
        return value

    atlassian_email = prompt("ATLASSIAN_EMAIL")
    atlassian_token = prompt("ATLASSIAN_API_TOKEN", secret=True)

    if not atlassian_email or not atlassian_token:
        print("  ❌ ATLASSIAN_EMAIL and ATLASSIAN_API_TOKEN are required; aborting.")
        return False

    lines = [
        f"ATLASSIAN_EMAIL={atlassian_email}",
        f"ATLASSIAN_API_TOKEN={atlassian_token}",
    ]

    add_email = input("\n  Configure optional email credentials for find_emails? (yes/no): ").strip().lower()
    if add_email == "yes":
        email_user = prompt("EMAIL_USERNAME", required=False)
        email_pass = prompt("EMAIL_PASSWORD", secret=True, required=False)
        if email_user:
            lines.append(f"EMAIL_USERNAME={email_user}")
        if email_pass:
            lines.append(f"EMAIL_PASSWORD={email_pass}")

    env_path.write_text("\n".join(lines) + "\n")
    os.chmod(env_path, 0o600)
    print(f"\n  ✅ Wrote {env_path}")
    return True


def print_header(text: str):
    """Print a formatted header."""
    print(f"\n{'='*70}")
    print(f"  {text}")
    print(f"{'='*70}\n")


_POSITIVE_STATUSES = ("installed", "ready", "set")


def print_status(item: str, status: str, detail: str = ""):
    """Print a status line."""
    symbol = "✅" if any(s in status.lower() for s in _POSITIVE_STATUSES) else "❌"
    detail_str = f" ({detail})" if detail else ""
    print(f"  {symbol} {item:<30} {status}{detail_str}")


def report_status():
    """Report current setup status."""
    print_header("Provider-Hub Setup Status")

    # Environment
    env, metadata = detect_environment()
    print(f"Environment: {env.value}")
    env_info = get_environment_info(env)
    print(f"  Setup path: {env_info['setup_path']}")
    print(f"  MCP registration: {env_info['mcp_registration']}")
    if env_info['notes']:
        print("  Notes:")
        for note in env_info['notes']:
            print(f"    - {note}")

    print("\n" + "="*70 + "\n")

    # System prerequisites
    print_header("System Prerequisites")
    print_status("Python 3.9+", "installed" if sys.version_info >= (3, 9) else "missing",
                 f"{sys.version.split()[0]}")
    print_status("npm", "installed" if check_command_exists("npm") else "missing")
    print_status("git", "installed" if check_command_exists("git") else "missing")

    # CLI tools
    print_header("CLI Tools")
    sf_status = "installed" if check_command_exists("sf") else "missing"
    print_status("Salesforce CLI (sf)", sf_status,
                 "Run: npm install -g @salesforce/cli" if sf_status == "missing" else "")

    # MCPs
    print_header("MCPs")
    pde_jsm = check_mcp_installed("pde-jsm")
    status = "unknown" if pde_jsm is None else ("installed" if pde_jsm else "missing")
    print_status("pde-jsm", status)

    sf_prod = check_mcp_installed("salesforce-prod")
    status = "unknown" if sf_prod is None else ("installed" if sf_prod else "missing")
    print_status("salesforce-prod", status)

    # Python packages
    print_header("Python Packages")
    print_status("requests", "installed" if check_python_package("requests") else "missing")
    print_status("python-dotenv", "installed" if check_python_package("dotenv") else "missing")
    print_status("mcp", "installed" if check_python_package("mcp") else "missing")

    # Environment variables
    print_header("Environment Variables")
    env_path = PDE_JSM_DIR / ".env"
    env_file_vars = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, _, _ = line.partition("=")
                env_file_vars[key.strip()] = True
    print_status("ATLASSIAN_EMAIL", "set" if os.getenv("ATLASSIAN_EMAIL") or "ATLASSIAN_EMAIL" in env_file_vars else "missing")
    print_status("ATLASSIAN_API_TOKEN", "set" if os.getenv("ATLASSIAN_API_TOKEN") or "ATLASSIAN_API_TOKEN" in env_file_vars else "missing")
    print_status("EMAIL_USERNAME", "set" if os.getenv("EMAIL_USERNAME") or "EMAIL_USERNAME" in env_file_vars else "optional")
    print_status("EMAIL_PASSWORD", "set" if os.getenv("EMAIL_PASSWORD") or "EMAIL_PASSWORD" in env_file_vars else "optional")

    print()


def interactive_menu():
    """Show interactive setup menu."""
    print_header("Provider-Hub Setup")

    env, metadata = detect_environment()
    print(f"Detected environment: {env.value}\n")

    choices = {
        "1": ("Check status", report_status),
        "2": ("List backups", list_backups),
        "3": ("Restore from backup", restore_from_backup),
        "4": ("Install system prerequisites (npm, git)", lambda: print("  Install npm/git via your OS package manager, then re-run --check.")),
        "5": ("Install Salesforce CLI (sf)", install_sf_cli),
        "6": ("Register pde-jsm MCP", register_pde_jsm),
        "7": ("Register salesforce-prod MCP", register_salesforce_prod),
        "8": ("Install Python packages", install_python_packages),
        "9": ("Set environment variables", set_env_vars),
        "0": ("Exit", sys.exit),
    }

    print("What would you like to do?\n")
    for key, (label, _) in choices.items():
        print(f"  {key}) {label}")

    choice = input("\nEnter choice: ").strip()

    if choice in choices:
        label, func = choices[choice]
        if choice != "0":
            print_header(label)
            func()
    else:
        print("Invalid choice")


def install_target(target: str):
    """Run one or more install steps for --install TARGET."""
    valid = {"all", "mcps", "sf-cli", "python-packages", "env-vars"}
    if target not in valid:
        print(f"Unknown install target: {target}")
        print(f"Valid targets: {', '.join(sorted(valid))}")
        sys.exit(1)

    if target in ("all", "python-packages"):
        install_python_packages()
    if target in ("all", "sf-cli"):
        install_sf_cli()
    if target in ("all", "mcps"):
        register_pde_jsm()
        register_salesforce_prod()
    if target in ("all", "env-vars"):
        set_env_vars()


def main():
    parser = argparse.ArgumentParser(
        description="Setup provider-hub: configure MCPs, dependencies, and environment",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report current setup status",
    )
    parser.add_argument(
        "--backups",
        action="store_true",
        help="List available backups",
    )
    parser.add_argument(
        "--restore",
        action="store_true",
        help="Restore from backup",
    )
    parser.add_argument(
        "--install",
        metavar="TARGET",
        help="Install specific target: all, sf-cli, python-packages, mcps, env-vars",
    )

    args = parser.parse_args()

    if args.check:
        report_status()
    elif args.backups:
        list_backups()
    elif args.restore:
        restore_from_backup()
    elif args.install:
        print(f"Installation target: {args.install}")
        install_target(args.install)
    else:
        # Interactive mode
        interactive_menu()


if __name__ == "__main__":
    main()
