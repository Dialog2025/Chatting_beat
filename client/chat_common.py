"""局域网聊天系统的共享模块：加密算法、通信协议分帧、TLS 辅助、公共工具函数。"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import socket
import ssl
import struct

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SECRETS_FILE = os.path.join(BASE_DIR, "secrets_config.json")
CERT_FILE = os.path.join(BASE_DIR, "server.crt")
KEY_FILE = os.path.join(BASE_DIR, "server.key")

DEFAULT_PORT = 8080


def _load_or_create_secrets() -> dict:
    """首次运行自动生成随机口令并落盘；多机部署时需要把同一份 secrets_config.json
    手动复制到所有客户端/管理端目录，否则各端口令不一致会导致加解密失败。"""
    if os.path.exists(SECRETS_FILE):
        with open(SECRETS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    data = {
        "chat_passphrase": secrets.token_urlsafe(32),
        "admin_passphrase": secrets.token_urlsafe(32),
    }
    with open(SECRETS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[首次运行] 已生成 {SECRETS_FILE}，请复制到其它客户端/管理端目录后再连接")
    return data


_secrets = _load_or_create_secrets()
# 所有客户端共用的消息加密口令，需与服务器/其它客户端保持一致（见 secrets_config.json）
CHAT_PASSPHRASE = _secrets["chat_passphrase"]
# 管理协议专用口令，仅服务器与管理端客户端知晓，用于加密整个管理报文
ADMIN_PASSPHRASE = _secrets["admin_passphrase"]

MAGIC = b"TXT1"
SALT_SIZE = 16
NONCE_SIZE = 16
TAG_SIZE = 32
PBKDF2_ROUNDS = 300_000


def derive_key(password: str, salt: bytes) -> bytes:
    if not password:
        raise ValueError("密码不能为空")
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt,
        PBKDF2_ROUNDS, dklen=32
    )


def make_keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    result = bytearray()
    counter = 0
    while len(result) < length:
        result.extend(hmac.new(
            key, nonce + struct.pack(">Q", counter), hashlib.sha256
        ).digest())
        counter += 1
    return bytes(result[:length])


def xor_bytes(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def encrypt_text(plaintext: str, password: str) -> str:
    data = plaintext.encode("utf-8")
    salt = secrets.token_bytes(SALT_SIZE)
    nonce = secrets.token_bytes(NONCE_SIZE)
    key = derive_key(password, salt)
    ciphertext = xor_bytes(data, make_keystream(key, nonce, len(data)))

    body = MAGIC + salt + nonce + ciphertext
    tag = hmac.new(key, body, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(body + tag).decode("ascii")


def decrypt_text(encoded: str, password: str) -> str:
    try:
        packed = base64.urlsafe_b64decode(encoded.encode("ascii"))
    except Exception as exc:
        raise ValueError("密文格式错误") from exc

    minimum = len(MAGIC) + SALT_SIZE + NONCE_SIZE + TAG_SIZE
    if len(packed) < minimum or packed[:4] != MAGIC:
        raise ValueError("不是本程序生成的有效密文")

    salt = packed[4:4 + SALT_SIZE]
    nonce_start = 4 + SALT_SIZE
    nonce = packed[nonce_start:nonce_start + NONCE_SIZE]
    tag_start = len(packed) - TAG_SIZE
    ciphertext = packed[nonce_start + NONCE_SIZE:tag_start]
    received_tag = packed[tag_start:]

    key = derive_key(password, salt)
    expected_tag = hmac.new(key, packed[:tag_start], hashlib.sha256).digest()

    if not hmac.compare_digest(received_tag, expected_tag):
        raise ValueError("密码错误，或密文已被修改")

    data = xor_bytes(ciphertext, make_keystream(key, nonce, len(ciphertext)))
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("解密结果不是有效的 UTF-8 文本") from exc


def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def create_server_ssl_context() -> ssl.SSLContext:
    """服务器端 TLS 上下文，需要 server.crt/server.key 已存在（由 chat_server.py 首次启动时自动生成）。"""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=CERT_FILE, keyfile=KEY_FILE)
    return context


def create_client_ssl_context() -> ssl.SSLContext:
    """客户端 TLS 上下文：对本地这份 server.crt 做证书锁定，不做基于主机名的校验。"""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=CERT_FILE)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    """循环读取直到凑满 size 字节，连接关闭时返回不足长度的数据。"""
    chunks = []
    remaining = size
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def send_msg(sock: socket.socket, obj: dict) -> None:
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    header = struct.pack(">I", len(body))
    sock.sendall(header + body)


def recv_msg(sock: socket.socket, max_size: int = 0):
    """读取一条分帧消息，连接关闭、数据不完整或超过 max_size（0 表示不限）时返回 None。"""
    header = _recv_exact(sock, 4)
    if len(header) < 4:
        return None
    (length,) = struct.unpack(">I", header)
    if max_size and length > max_size:
        return None
    body = _recv_exact(sock, length)
    if len(body) < length:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
