#!/usr/bin/env python3
"""Entry point: run an AI coding agent inside an unprivileged sandbox.
The implementation lives in the agtbox/ package."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from agtbox.cli import main

if __name__ == "__main__":
    main()
