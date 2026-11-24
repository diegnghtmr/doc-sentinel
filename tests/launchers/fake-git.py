#!/usr/bin/python3
"""Deterministic git stand-in for launcher image-freshness checks."""
import os
import sys

args = sys.argv[1:]
repo = ''
if args[:1] == ['-C']:
    repo, args = args[1], args[2:]
head = os.environ.get('FAKE_GIT_HEAD')
if not head:
    sys.exit(128)
if args == ['rev-parse', '--show-toplevel']:
    print(os.environ.get('FAKE_GIT_TOPLEVEL', repo))
elif args == ['rev-parse', 'HEAD']:
    print(head)
elif args[:2] == ['status', '--porcelain']:
    sys.stdout.write(os.environ.get('FAKE_GIT_DIRTY', ''))
else:
    sys.exit(99)
