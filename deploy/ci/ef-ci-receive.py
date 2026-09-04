#!/usr/bin/python3
"""Forced SSH command; never accepts an interactive shell or arbitrary commands."""
import os
import re
import sys

command = os.environ.get("SSH_ORIGINAL_COMMAND", "")
match = re.fullmatch(r"deploy ([0-9a-f]{40})", command)
if not match:
    sys.exit("Only 'deploy <40-character commit SHA>' is permitted")
os.execv("/usr/bin/sudo", ["sudo", "-n", "/usr/local/sbin/ef-ci-deploy", match.group(1)])
