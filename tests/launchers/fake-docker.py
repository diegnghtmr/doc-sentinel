#!/usr/bin/python3
"""Deterministic Docker stand-in; records only its test configuration, never the host env."""
import json
import os
import sys

args = sys.argv[1:]
config = {key: value for key, value in os.environ.items() if key.startswith('FAKE_')}
with open(os.environ['FAKE_LOG'], 'a', encoding='utf-8') as log:
    log.write(json.dumps({'args': args, 'env': config}, ensure_ascii=False) + '\n')

if args[:1] == ['info']:
    code = int(os.environ.get('FAKE_INFO_CODE', '0'))
    if code == 0 and '--format' in args:
        print(os.environ.get('FAKE_OSTYPE', 'linux'))
elif args[:2] == ['image', 'inspect']:
    code = int(os.environ.get('FAKE_IMAGE_CODE', '0'))
elif args[:1] == ['build']:
    print('build stdout', flush=True)
    print('build stderr', file=sys.stderr, flush=True)
    code = int(os.environ.get('FAKE_BUILD_CODE', '0'))
elif args[:1] == ['run']:
    sys.stdout.write(os.environ.get('FAKE_RUN_STDOUT', ''))
    sys.stderr.write(os.environ.get('FAKE_RUN_STDERR', ''))
    code = int(os.environ.get('FAKE_RUN_CODE', '0'))
else:
    print('unexpected fake docker call', file=sys.stderr)
    code = 99
sys.exit(code)
