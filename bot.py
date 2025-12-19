#!/usr/bin/env python3
"""
fez_collector - Discord edition (Refactored)
Wrapper around src.main
"""
import sys
from pathlib import Path

# Ensure the current directory is in sys.path
sys.path.append(str(Path(__file__).parent))

from src.main import main

if __name__ == "__main__":
    main()