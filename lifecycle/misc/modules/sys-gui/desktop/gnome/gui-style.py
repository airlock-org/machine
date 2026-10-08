"""Apply a narrow, durable GUI style through existing administrator transport."""

import fcntl
import json
import os
from pathlib import Path
import re
import signal
import sys
import time


STATE = Path('/rw/airlock')


def atomic(path, value, mode=0o600):
    temporary = path.with_name(path.name + '.new')
    with temporary.open('w') as stream:
        os.chmod(temporary, mode)
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def main():
    if os.geteuid() != 0:
        raise ValueError('GUI style requires the existing administrator transport')
    request = json.load(sys.stdin)
    if set(request) != {'schema', 'color_width', 'revision'}:
        raise ValueError('unexpected GUI style fields')
    if request['schema'] != 'airlock.gui-style.v1':
        raise ValueError('unsupported GUI style schema')
    width = request['color_width']
    if type(width) is not int or not 1 <= width <= 16:
        raise ValueError('colour width must be between 1 and 16')
    boot = sys.argv[1:] == ['boot']
    if not boot and (not isinstance(request['revision'], str)
                     or not re.fullmatch('[0-9a-f]{40}', request['revision'])):
        raise ValueError('a committed source revision is required')
    STATE.mkdir(mode=0o755, exist_ok=True)
    if STATE.stat().st_uid != 0 or STATE.stat().st_mode & 0o022:
        raise ValueError('GUI style state must be owned and controlled by root')
    with (STATE / 'gui-style.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        handles = []
        try:
            if not boot:
                for process in Path('/proc').iterdir():
                    if not process.name.isdecimal():
                        continue
                    try:
                        executable = (process / 'exe').resolve(strict=True)
                        if executable.name != 'qubes-guid':
                            continue
                        if not (executable.parent.parent / 'share/airlock/gui-border-reload-v1').is_file():
                            raise ValueError('running GUI daemon needs the one-time reload-support deployment')
                        handles.append(os.pidfd_open(int(process.name)))
                    except (FileNotFoundError, ProcessLookupError):
                        continue
                if not handles:
                    raise ValueError('no running GUI daemons; start a graphical session first')
            record = {**request, 'system': str(Path('/run/current-system').resolve()),
                      'time': time.time(), 'daemon_count': len(handles), 'status': 'prepared'}
            journal = STATE / 'gui-style-history'
            journal.mkdir(mode=0o700, exist_ok=True)
            receipt = journal / (str(time.time_ns()) + '.json')
            atomic(receipt, json.dumps(record) + '\n')
            atomic(STATE / 'gui-border-width', str(width) + '\n', mode=0o644)
            for handle in handles:
                signal.pidfd_send_signal(handle, signal.SIGHUP)
            record['status'] = 'reload-requested' if handles else 'boot-default'
            atomic(receipt, json.dumps(record) + '\n')
            atomic(STATE / 'gui-style-selection.json', json.dumps(record) + '\n')
            print(json.dumps(record))
        finally:
            for handle in handles:
                os.close(handle)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError) as exc:
        print('airlock-gui-style: ' + str(exc), file=sys.stderr)
        sys.exit(1)
