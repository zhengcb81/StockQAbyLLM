#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run llm_demo.py with encoding fixes."""

import sys
import io

# Set stdout to use UTF-8 encoding
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# Import and run the main function
from llm_demo import main

if __name__ == '__main__':
    exit_code = main()
    sys.exit(exit_code)
