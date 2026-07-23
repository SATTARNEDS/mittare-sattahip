"""ถอดรหัสไฟล์ .mtbackup เป็น ZIP ด้วย BACKUP_ENCRYPTION_KEY"""

import argparse
import base64
import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


BACKUP_MAGIC = b"MITTARE-BACKUP-V1\0"


def decrypt_backup(source: Path, destination: Path, encoded_key: str) -> None:
    key = base64.urlsafe_b64decode(encoded_key.encode("ascii"))
    if len(key) != 32:
        raise ValueError("BACKUP_ENCRYPTION_KEY ต้องถอดรหัสได้ 32 bytes")
    payload = source.read_bytes()
    if not payload.startswith(BACKUP_MAGIC) or len(payload) <= len(BACKUP_MAGIC) + 12:
        raise ValueError("ไฟล์ไม่ใช่ MITTARE encrypted backup")
    nonce_start = len(BACKUP_MAGIC)
    nonce = payload[nonce_start:nonce_start + 12]
    ciphertext = payload[nonce_start + 12:]
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, BACKUP_MAGIC)
    except InvalidTag as error:
        raise ValueError("กุญแจไม่ถูกต้องหรือไฟล์ถูกแก้ไข") from error
    destination.write_bytes(plaintext)


def main() -> None:
    parser = argparse.ArgumentParser(description="ถอดรหัส MITTARE .mtbackup เป็น ZIP")
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    encoded_key = os.environ.get("BACKUP_ENCRYPTION_KEY", "")
    if not encoded_key:
        parser.error("กรุณาตั้ง BACKUP_ENCRYPTION_KEY ก่อน")
    decrypt_backup(args.source, args.destination, encoded_key)
    print(f"ถอดรหัสสำเร็จ: {args.destination}")


if __name__ == "__main__":
    main()
