"""Durable backup inventory and progress. No Fusion API calls belong here."""
import hashlib
import json
import os
import sqlite3
import subprocess
from contextlib import contextmanager
from pathlib import Path


def fingerprint(value):
    encoded = json.dumps(value, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def process_session_id():
    """PID plus process creation time, so restarting really resets the allowance."""
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        creation, exit_time, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(kernel32.GetCurrentProcess(), ctypes.byref(creation),
                                        ctypes.byref(exit_time), ctypes.byref(kernel), ctypes.byref(user)):
            raise ctypes.WinError(ctypes.get_last_error())
        started = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
    else:
        started = subprocess.check_output(
            ['ps', '-o', 'lstart=', '-p', str(os.getpid())], text=True, timeout=5,
        ).strip()
        if not started:
            raise RuntimeError('Could not identify the Fusion process start time')
    return f'{os.getpid()}:{started}'


def output_exists(root, relative, allow_empty=False):
    path = Path(root) / relative
    if path.is_file() and (allow_empty or path.stat().st_size > 0):
        return True
    return any(
        path.with_name(path.name + suffix).is_file()
        and path.with_name(path.name + suffix).stat().st_size > 0
        for suffix in ('.zip', '.rar', '.gz', '.tar.gz', '.tar.bz2', '.tar.xz')
    )


class BackupCatalog:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / '.fusion-exporter.sqlite3'
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        try:
            version = self.db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError(f'Unsupported backup catalog version {version}: {self.path}')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS jobs (
                    key TEXT PRIMARY KEY, payload TEXT NOT NULL,
                    scan TEXT NOT NULL, position INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    outputs TEXT NOT NULL DEFAULT '[]',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS jobs_scan ON jobs(scan);
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, opens INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                PRAGMA user_version=1;
            ''')
            # A running job was never committed as completed. Existing output
            # files are retained; a later attempt will skip those individually.
            with self.db:
                self.db.execute("UPDATE jobs SET status='interrupted', last_error="
                                "'Previous run ended before this document completed' WHERE status='running'")
        except Exception:
            self.db.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    @contextmanager
    def scan(self):
        # Inventory changes commit before any document is opened. An interrupted
        # discovery rolls back without destroying the previous inventory.
        with self.db:
            yield

    def discover(self, payload, scan, position):
        key = fingerprint(payload)
        self.db.execute('''
            INSERT INTO jobs(key, payload, scan, position) VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET scan=excluded.scan, position=excluded.position
        ''', (key, json.dumps(payload), scan, position))
        row = self.db.execute('SELECT * FROM jobs WHERE key=?', (key,)).fetchone()
        outputs = json.loads(row['outputs'])
        if row['status'] == 'done':
            if not self.outputs_present(payload, outputs):
                self.db.execute("UPDATE jobs SET status='pending', last_error='' WHERE key=?", (key,))
        elif not payload['sketches'] and self.outputs_present(payload, payload['expected_outputs']):
            # Adopt exports created before the catalog existed. Sketches require
            # one successful enumeration before their inventory is known.
            self.db.execute("UPDATE jobs SET status='done', outputs=?, last_error='' WHERE key=?",
                            (json.dumps(payload['expected_outputs']), key))
        return key

    def outputs_present(self, payload, outputs):
        return all(output_exists(self.root, path, payload['extension'] not in ('f3d', 'f2d'))
                   for path in outputs)

    def queue(self, scan):
        rows = self.db.execute('''
            SELECT * FROM jobs WHERE scan=? AND status!='done'
            ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'interrupted' THEN 1 ELSE 2 END,
                     position
        ''', (scan,)).fetchall()
        return [dict(row, payload=json.loads(row['payload'])) for row in rows]

    def counts(self, scan):
        result = dict(total=0, done=0, pending=0, running=0, interrupted=0, error=0)
        for row in self.db.execute('SELECT status, count(*) FROM jobs WHERE scan=? GROUP BY status', (scan,)):
            result[row[0]] = row[1]
            result['total'] += row[1]
        return result

    def save_scan(self, scan, options, errors=0):
        self.db.execute('INSERT OR REPLACE INTO metadata VALUES (?, ?)', ('scan', scan))
        self.db.execute('INSERT OR REPLACE INTO metadata VALUES (?, ?)', ('options', fingerprint(options)))
        self.db.execute('INSERT OR REPLACE INTO metadata VALUES (?, ?)', ('scan_errors', str(errors)))

    def preview(self, options):
        metadata = dict(self.db.execute('SELECT key, value FROM metadata'))
        if metadata.get('options') != fingerprint(options) or 'scan' not in metadata:
            return None
        result = self.counts(metadata['scan'])
        result['scan_errors'] = int(metadata.get('scan_errors', '0'))
        return result

    def completed_outputs(self, scan):
        return sum(len(json.loads(row[0])) for row in self.db.execute(
            "SELECT outputs FROM jobs WHERE scan=? AND status='done'", (scan,)))

    def start(self, key):
        with self.db:
            self.db.execute("UPDATE jobs SET status='running', attempts=attempts+1, last_error='' WHERE key=?", (key,))

    def finish(self, key, outputs):
        with self.db:
            self.db.execute("UPDATE jobs SET status='done', outputs=?, last_error='' WHERE key=?",
                            (json.dumps(sorted(set(outputs))), key))

    def fail(self, key, error):
        with self.db:
            self.db.execute("UPDATE jobs SET status='error', last_error=? WHERE key=?", (str(error), key))

    def defer(self, key):
        with self.db:
            self.db.execute("UPDATE jobs SET status='pending' WHERE key=?", (key,))

    def session_opens(self, session):
        row = self.db.execute('SELECT opens FROM sessions WHERE id=?', (session,)).fetchone()
        return row[0] if row else 0

    def reserve_open(self, session, limit):
        # Reserve durably before entering Fusion's native open call, including an
        # attempt that crashes or returns null. Repeated script calls share this.
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO sessions(id) VALUES (?)', (session,))
            changed = self.db.execute('UPDATE sessions SET opens=opens+1 WHERE id=? AND opens<?',
                                      (session, limit)).rowcount
        return bool(changed)
