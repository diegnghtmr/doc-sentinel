#!/usr/bin/python3
"""Host Python stand-in: records review-run launches like the fake docker does."""
import json
import os
import sys

with open(os.environ['FAKE_LOG'], 'a', encoding='utf-8') as log:
    log.write(json.dumps({'args': ['python', *sys.argv[1:]], 'env': {}}, ensure_ascii=False) + '\n')
if '-c' in sys.argv[1:]:
    sys.exit(0)  # the launcher's interpreter probe
sys.stdout.write(os.environ.get('FAKE_PYTHON_STDOUT', ''))
sys.exit(int(os.environ.get('FAKE_PYTHON_CODE', '0')))
