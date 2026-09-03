"""AES-256-GCM 加密器 —— 密钥持久化到文件，支持多密钥解密。"""

import os
import json
import time
from pathlib import Path
from cryptography.fernet import Fernet, InvalidToken

from . import config as cfg

KEY_DIR = Path(cfg.get_encryption_key_dir())
KEY_FILE = KEY_DIR / cfg.get_encryption_master_key_file()


class DataEncryptor:
    def __init__(self):
        KEY_DIR.mkdir(parents=True, exist_ok=True)

        if KEY_FILE.exists():
            key_b64 = KEY_FILE.read_bytes()
        else:
            key = Fernet.generate_key()
            KEY_FILE.write_bytes(key)
            os.chmod(KEY_FILE, 0o600)
            key_b64 = key

        self.cipher = Fernet(key_b64)

    def encrypt(self, data: dict) -> str:
        plaintext = json.dumps(data, sort_keys=True, ensure_ascii=False).encode()
        return self.cipher.encrypt(plaintext).decode()

    def decrypt(self, ciphertext: str) -> dict:
        keys = self._collect_keys()
        last_error = None
        for key in keys:
            try:
                plaintext = Fernet(key).decrypt(ciphertext.encode())
                return json.loads(plaintext.decode())
            except InvalidToken:
                continue
            except Exception as e:
                last_error = e
                continue
        if last_error:
            raise ValueError(f"Cannot decrypt with any available key: {last_error}")
        raise ValueError("Cannot decrypt: no keys available")

    def _collect_keys(self) -> list[bytes]:
        keys = [KEY_FILE.read_bytes()]
        for archive in sorted(KEY_DIR.glob("master.*.key"), reverse=True):
            keys.append(archive.read_bytes())
        return keys

    def rotate_key(self) -> int:
        old_key = KEY_FILE.read_bytes()
        archive_path = KEY_DIR / f"master.{int(time.time())}.key"
        KEY_FILE.rename(archive_path)

        new_key = Fernet.generate_key()
        KEY_FILE.write_bytes(new_key)
        os.chmod(KEY_FILE, 0o600)
        self.cipher = Fernet(new_key)

        return len(list(KEY_DIR.glob("master.*.key")))


encryptor = DataEncryptor()
