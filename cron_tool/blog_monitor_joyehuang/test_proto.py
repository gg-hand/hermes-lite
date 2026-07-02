#!/usr/bin/env python3
"""Test the cron_tool stdin/stdout protocol"""
import subprocess
import json
import sys

input_data = json.dumps({"input": {}, "context": {}})
p = subprocess.run(
    [sys.executable, "-X", "utf8", "run.py"],
    input=input_data,
    capture_output=True,
    text=True,
    timeout=30,
    cwd=r"cron_tool\.pending\blog_monitor_joyehuang"
)
print(f"RC: {p.returncode}")
print(f"OUT: {p.stdout}")
print(f"ERR: {p.stderr}")
