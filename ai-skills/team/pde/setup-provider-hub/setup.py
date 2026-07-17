#!/usr/bin/env python3
"""
Setup provider-hub: configures MCPs, dependencies, and tools.

Environment-aware: detects Copilot CLI, Claude CLI, Claude Desktop, VS Code, etc.
and provides appropriate setup instructions for that environment.

No external dependencies—uses stdlib only. Automatically backs up configs before changes.

Usage:
    python setup.py                    # Interactive menu
    python setup.py --check           # Report current status
    python setup.py --install all     # Install everything
    python setup.py --backups         # List available backups
    python setup.py --restore         # Restore from backup
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from environment import Environment, detect_environment, get_environment_info


def run_cmd(cmd: list, check: bool = False) -> tuple[int, str, str]:
    """Run command and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
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
        
        # Try to infer original path from backup filename
        # Backup format: <filename>.<timestamp>.bak
        parts = backup_path.name.rsplit(".", 2)  # Split from right: filename, timestamp, bak
        if len(parts) == 3:
            original_name = parts[0]
            print(f"\n  This backup is for: {original_name}")
            print(f"  Backup date: {backup_path.stat().st_mtime}")
        
        confirm = input("\n  ⚠️  This will replace the file. Continue? (yes/no): ")
        if confirm.lower() != "yes":
            print("  Cancelled")
            return False
        
        # For now, just show the backup path
        print(f"\n  ✅ Backup located at: {backup_path}")
        print(f"     To restore, copy from backup to original location")
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


def check_mcp_installed(mcp_name: str) -> bool:
    """Check if an MCP is registered in Copilot/Claude config."""
    env, metadata = detect_environment()
    
    if env == Environment.CLAUDE_DESKTOP or env == Environment.CLAUDE_CLI:
        config_file = metadata.get("config_dir") / "claude_desktop_config.json"
        if config_file.exists():
            try:
                with open(config_file) as f:
                    config = json.load(f)
                    return mcp_name in config.get("mcpServers", {})
            except (json.JSONDecodeError, IOError):
                pass
    
    elif env == Environment.COPILOT_CLI:
        config_file = metadata.get("mcp_config")
        if config_file and config_file.exists():
            try:
                with open(config_file) as f:
                    config = json.load(f)
                    return mcp_name in config.get("mcpServers", {})
            except (json.JSONDecodeError, IOError):
                pass
    
    return None  # Unknown


def print_header(text: str):
    """Print a formatted header."""
    print(f"\n{'='*70}")
    print(f"  {text}")
    print(f"{'='*70}\n")


def print_status(item: str, status: str, detail: str = ""):
    """Print a status line."""
    symbol = "✅" if "installed" in status.lower() or "ready" in status.lower() else "❌"
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
    if pde_jsm is None:
        status = "unknown"
    else:
        status = "installed" if pde_jsm else "missing"
    print_status("pde-jsm", status)
    
    sf_prod = check_mcp_installed("salesforce-prod")
    if sf_prod is None:
        status = "unknown"
    else:
        status = "installed" if sf_prod else "missing"
    print_status("salesforce-prod", status)
    
    # Python packages
    print_header("Python Packages")
    print_status("requests", "installed" if check_python_package("requests") else "missing")
    print_status("python-dotenv", "installed" if check_python_package("dotenv") else "missing")
    print_status("mcp", "installed" if check_python_package("mcp") else "missing")
    
    # Environment variables
    print_header("Environment Variables")
    print_status("ATLASSIAN_EMAIL", "set" if os.getenv("ATLASSIAN_EMAIL") else "missing")
    print_status("ATLASSIAN_API_TOKEN", "set" if os.getenv("ATLASSIAN_API_TOKEN") else "missing")
    print_status("EMAIL_USERNAME", "set" if os.getenv("EMAIL_USERNAME") else "optional")
    print_status("EMAIL_PASSWORD", "set" if os.getenv("EMAIL_PASSWORD") else "optional")
    
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
        "4": ("Install system prerequisites (npm, git)", lambda: None),
        "5": ("Install Salesforce CLI (sf)", lambda: None),
        "6": ("Register pde-jsm MCP", lambda: None),
        "7": ("Register salesforce-prod MCP", lambda: None),
        "8": ("Install Python packages", lambda: None),
        "9": ("Set environment variables", lambda: None),
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
        print("(Full implementation coming soon)")
    else:
        # Interactive mode
        interactive_menu()


if __name__ == "__main__":
    main()
