"""SQLCipher storage. No password/key is written to files or command arguments."""
import ctypes as C
import ctypes.util
import json
import os
import threading

class VaultError(Exception):
    pass

class Database:
    def __init__(self, path, password, create=False):
        name = ctypes.util.find_library("sqlcipher")
        if not name:
            raise VaultError("SQLCipher is required. Install it with: omarchy pkg add sqlcipher")
        self.lib = C.CDLL(name)
        definitions = {
            "sqlite3_open_v2": ([C.c_char_p, C.POINTER(C.c_void_p), C.c_int, C.c_char_p], C.c_int),
            "sqlite3_key": ([C.c_void_p, C.c_void_p, C.c_int], C.c_int),
            "sqlite3_rekey": ([C.c_void_p, C.c_void_p, C.c_int], C.c_int),
            "sqlite3_close": ([C.c_void_p], C.c_int),
            "sqlite3_prepare_v2": ([C.c_void_p, C.c_char_p, C.c_int, C.POINTER(C.c_void_p), C.c_void_p], C.c_int),
            "sqlite3_bind_text": ([C.c_void_p, C.c_int, C.c_char_p, C.c_int, C.c_void_p], C.c_int),
            "sqlite3_step": ([C.c_void_p], C.c_int),
            "sqlite3_column_text": ([C.c_void_p, C.c_int], C.c_void_p),
            "sqlite3_column_bytes": ([C.c_void_p, C.c_int], C.c_int),
            "sqlite3_finalize": ([C.c_void_p], C.c_int),
        }
        for name, (args, result) in definitions.items():
            fn = getattr(self.lib, name); fn.argtypes = args; fn.restype = result
        self.mutex = threading.RLock()
        self.db = C.c_void_p()
        flags = 2 | 0x10000 | 0x1000000 | (4 if create else 0)  # READWRITE, FULLMUTEX, NOFOLLOW, CREATE
        try:
            self.check(self.lib.sqlite3_open_v2(os.fsencode(path), C.byref(self.db), flags, None))
            self.key(password)
            if not self.query("PRAGMA cipher_version"):
                raise VaultError("SQLCipher is unavailable.")
            self.query("SELECT count(*) FROM sqlite_master")
            self.query("PRAGMA cipher_memory_security = ON")
            self.query("PRAGMA temp_store = MEMORY")
            self.query("PRAGMA journal_mode = DELETE")
            self.query("PRAGMA synchronous = FULL")
            if create:
                self.query("CREATE TABLE IF NOT EXISTS documents (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
        except Exception:
            self.close()
            raise

    def check(self, code):
        if code != 0:
            raise VaultError("Cannot open or update the vault. Check your password and database file.")

    def key(self, password, rekey=False):
        raw = password.encode("utf-8")
        buffer = C.create_string_buffer(raw)
        try:
            self.check((self.lib.sqlite3_rekey if rekey else self.lib.sqlite3_key)(self.db, buffer, len(raw)))
        finally:
            C.memset(buffer, 0, len(buffer))

    def query(self, sql, params=()):
        with self.mutex:
            if not self.db:
                raise VaultError("Vault is locked.")
            statement = C.c_void_p()
            self.check(self.lib.sqlite3_prepare_v2(self.db, sql.encode(), -1, C.byref(statement), None))
            try:
                for i, value in enumerate(params, 1):
                    raw = str(value).encode()
                    self.check(self.lib.sqlite3_bind_text(statement, i, raw, len(raw), C.c_void_p(-1)))
                rows = []
                while True:
                    result = self.lib.sqlite3_step(statement)
                    if result == 101:
                        return rows
                    if result != 100:
                        self.check(result)
                    pointer = self.lib.sqlite3_column_text(statement, 0)
                    rows.append(C.string_at(pointer, self.lib.sqlite3_column_bytes(statement, 0)).decode() if pointer else None)
            finally:
                self.lib.sqlite3_finalize(statement)

    def get(self, name, fallback):
        rows = self.query("SELECT value FROM documents WHERE name = ?", (name,))
        return json.loads(rows[0]) if rows else fallback

    def put(self, name, value):
        self.query("INSERT INTO documents(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value", (name, json.dumps(value, ensure_ascii=False)))

    def delete(self, name):
        self.query("DELETE FROM documents WHERE name=?", (name,))

    def close(self):
        if getattr(self, "db", None):
            self.lib.sqlite3_close(self.db)
            self.db = C.c_void_p()
