#!/usr/bin/env python3
"""
Bootstrap provider-hub setup.

This script can be run before any other setup:
    python bootstrap.py              # Interactive menu
    python bootstrap.py --check      # Check status only
    python bootstrap.py --restore    # Restore from backup

It delegates to the main setup script in ai-skills/team/pde/setup-provider-hub/
"""

import subprocess
import sys
from pathlib import Path


def main():
    # Find the main setup script
    repo_root = Path(__file__).parent
    setup_script = (
        repo_root
        / "ai-skills"
        / "team"
        / "pde"
        / "setup-provider-hub"
        / "setup.py"
    )
    
    if not setup_script.exists():
        print(f"❌ Setup script not found: {setup_script}")
        sys.exit(1)
    
    # Run the main setup script, passing through all arguments
    result = subprocess.run(
        [sys.executable, str(setup_script)] + sys.argv[1:],
        cwd=repo_root,
    )
    
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
