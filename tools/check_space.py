#!/usr/bin/env python3
"""Fail before a download/build would cross the user's filesystem reserves."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import shutil
import sys

GIB = 1024 ** 3
RESERVES = {'/home': 5 * GIB, '/': 5 * GIB}


def check_space(write_path=None, expected_bytes=0):
    if expected_bytes < 0:
        raise ValueError('expected_bytes cannot be negative')
    destination = Path(write_path).resolve() if write_path else None
    if destination:
        while not destination.exists():
            destination = destination.parent
    destination_device = destination.stat().st_dev if destination else None
    rows = []
    for name, minimum in RESERVES.items():
        free = shutil.disk_usage(name).free
        planned = expected_bytes if Path(name).stat().st_dev == destination_device else 0
        rows.append({'path': name, 'free_bytes': free, 'reserve_bytes': minimum,
                     'planned_bytes': planned, 'ok': free - planned > minimum})
    if destination and destination_device not in {Path(p).stat().st_dev for p in RESERVES}:
        free = shutil.disk_usage(destination).free
        rows.append({'path': str(destination), 'free_bytes': free, 'reserve_bytes': GIB,
                     'planned_bytes': expected_bytes, 'ok': free - expected_bytes > GIB})
    return rows


def require_space(write_path=None, expected_bytes=0):
    rows = check_space(write_path, expected_bytes)
    if not all(row['ok'] for row in rows):
        raise RuntimeError('Disk reserve violated: /home > 5 GiB, / > 5 GiB; use the project volume on /mnt/docker_space')
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write-path')
    parser.add_argument('--expected-gib', type=float, default=0)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    rows = check_space(args.write_path, int(args.expected_gib * GIB))
    report = {'status': 'passed' if all(r['ok'] for r in rows) else 'blocked', 'filesystems': rows}
    text = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
    print(text, end='')
    if report['status'] != 'passed':
        sys.exit('Недостаточно места: /home > 5 GiB, / > 5 GiB. Используйте проектный том на /mnt/docker_space, не удаляйте чужие данные.')


if __name__ == '__main__':
    main()
