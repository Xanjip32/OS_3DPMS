"""Start OS_3DPMS. Usage: py run.py [--open-setup]"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dashboard.web import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
