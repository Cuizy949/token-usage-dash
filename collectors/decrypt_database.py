"""decrypt_database.py — Trae SQLCipher 4 加密库页级解密（与 hexxyan/trae-db-recovery 同口径）。

用法:
  python3 decrypt_database.py <database.db 副本> <key.json> <decrypted.db>

key.json 需含 "enc_key"（64 位 hex，32 字节原始密钥）。只解主库文件，WAL 尾部不在内——
要最新数据先完全退出 Trae（触发 checkpoint）再拷贝副本。输出页数头可能截断，按需重写偏移 28
的页数（大端 uint32）后用 sqlite3 ".recover --ignore-freelist" 导出干净库。
依赖 pycryptodome。
"""
import hashlib
import json
import sys
from pathlib import Path

from Crypto.Cipher import AES

PAGE_SIZE = 4096
RESERVE_SIZE = 80
SALT_SIZE = 16
SQLITE_HEADER = b"SQLite format 3\x00"


def decrypt_page(key: bytes, page: bytes, page_number: int) -> bytes:
    iv_start = PAGE_SIZE - RESERVE_SIZE
    iv = page[iv_start : iv_start + 16]
    cipher = AES.new(key, AES.MODE_CBC, iv)
    if page_number == 1:
        plaintext = cipher.decrypt(page[SALT_SIZE:iv_start])
        return SQLITE_HEADER + plaintext + (b"\0" * RESERVE_SIZE)
    plaintext = cipher.decrypt(page[:iv_start])
    return plaintext + (b"\0" * RESERVE_SIZE)


def main() -> int:
    if len(sys.argv) != 4:
        print(f"usage: {sys.argv[0]} <database.db> <key.json> <decrypted.db>", file=sys.stderr)
        return 2

    source = Path(sys.argv[1])
    key_file = Path(sys.argv[2])
    destination = Path(sys.argv[3])
    key = bytes.fromhex(json.loads(key_file.read_text())["enc_key"])
    if len(key) != 32:
        print("key is not 256 bits", file=sys.stderr)
        return 2

    total_pages = source.stat().st_size // PAGE_SIZE
    with source.open("rb") as encrypted, destination.open("xb") as decrypted:
        for page_number in range(1, total_pages + 1):
            page = encrypted.read(PAGE_SIZE)
            if len(page) != PAGE_SIZE:
                print(f"short read at page {page_number}", file=sys.stderr)
                return 1
            decrypted.write(decrypt_page(key, page, page_number))
            if page_number % 25000 == 0 or page_number == total_pages:
                print(f"decrypted_pages={page_number}/{total_pages}", file=sys.stderr)

    with destination.open("rb") as decrypted:
        digest = hashlib.sha256(decrypted.read(PAGE_SIZE)).hexdigest()
    print(f"decrypted_database={destination}")
    print(f"page_one_sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
