"""聊天服务器：TLS 加密传输、账号认证（新账号需邀请码）、好友、群聊、离线消息、客户端下载。"""
import argparse
import base64
import hmac
import json
import os
import secrets
import shutil
import socket
import ssl
import subprocess
import threading
import time
import uuid

from chat_common import (
    CERT_FILE, DEFAULT_PORT, KEY_FILE, create_server_ssl_context, derive_key, get_local_ip, recv_msg, send_msg,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "server_config.json")
ACCOUNTS_FILE = os.path.join(BASE_DIR, "accounts.json")
SOCIAL_FILE = os.path.join(BASE_DIR, "social.json")
MESSAGES_FILE = os.path.join(BASE_DIR, "messages.jsonl")
GROUP_MESSAGES_FILE = os.path.join(BASE_DIR, "group_messages.jsonl")
EMOJI_DIR = os.path.join(BASE_DIR, "emoji_pack")
CLIENT_ZIP = os.path.join(BASE_DIR, "chat_client.zip")

MAX_FRAME_SIZE = 32 * 1024 * 1024  # 客户端单文件上限 8MB，加密+base64 后远小于此值
LOGIN_TIMEOUT = 30
RECENT_LIMIT = 20
MAX_NAME_LEN = 32


def load_config() -> dict:
    """首次运行生成 server_config.json（含随机邀请码），之后直接读取。"""
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    config = {"host": "0.0.0.0", "port": DEFAULT_PORT, "invite_code": secrets.token_urlsafe(8)}
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    return config


def ensure_certificate() -> None:
    """缺证书时用系统 openssl 生成自签名证书；生成后必须把 server.crt 发给所有客户端。"""
    if os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE):
        return
    if shutil.which("openssl") is None:
        raise SystemExit("缺少 server.crt/server.key，且系统未安装 openssl，无法自动生成证书")
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256", "-days", "3650",
        "-subj", "/CN=chat-server", "-keyout", KEY_FILE, "-out", CERT_FILE,
    ], check=True, capture_output=True)
    os.chmod(KEY_FILE, 0o600)
    print(f"[首次运行] 已生成新证书 {CERT_FILE}，请把它复制到所有客户端目录（替换旧的 server.crt）")


def load_emoji_pack(emoji_dir: str) -> dict:
    """读取 emoji_pack 目录下所有 PNG，返回 {文件名: base64内容} 供客户端下载。"""
    files = {}
    if not os.path.isdir(emoji_dir):
        return files
    for name in os.listdir(emoji_dir):
        if not name.lower().endswith(".png"):
            continue
        with open(os.path.join(emoji_dir, name), "rb") as f:
            files[name] = base64.b64encode(f.read()).decode("ascii")
    return files


def valid_name(name) -> bool:
    return isinstance(name, str) and 0 < len(name) <= MAX_NAME_LEN and name.isprintable() and name.strip() == name


class AccountStore:
    """账号密码存储，PBKDF2 加盐哈希，线程安全读写。"""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        self.accounts = self._load()

    def _load(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save(self) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.accounts, f, ensure_ascii=False, indent=2)

    def exists(self, username: str) -> bool:
        with self.lock:
            return username in self.accounts

    def login(self, username: str, password: str, invite_code: str, expected_code: str):
        """已注册账号校验密码；未注册账号需邀请码正确才创建。返回 (是否通过, 失败原因)。"""
        with self.lock:
            entry = self.accounts.get(username)
            if entry is None:
                if not expected_code or not hmac.compare_digest(invite_code.encode(), expected_code.encode()):
                    return False, "新账号注册需要正确的邀请码"
                salt = secrets.token_hex(16)
                key = derive_key(password, bytes.fromhex(salt))
                self.accounts[username] = {"salt": salt, "hash": key.hex()}
                self._save()
                return True, ""

            key = derive_key(password, bytes.fromhex(entry["salt"]))
            if hmac.compare_digest(key.hex(), entry["hash"]):
                return True, ""
            return False, "密码错误"


class JsonlStore:
    """消息持久化（JSONL），追加写入；状态更新时整体重写。"""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        self.records = self._load()

    def _load(self) -> list:
        if not os.path.exists(self.path):
            return []
        records = []
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        return records

    def _save_all(self) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            for record in self.records:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def append(self, record: dict) -> None:
        with self.lock:
            self.records.append(record)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def query(self, predicate) -> list:
        with self.lock:
            return [dict(r) for r in self.records if predicate(r)]

    def update(self, ids, func) -> None:
        ids = set(ids)
        if not ids:
            return
        with self.lock:
            for record in self.records:
                if record["id"] in ids:
                    func(record)
            self._save_all()


class SocialStore:
    """好友关系、待处理好友申请、群组信息，统一存在 social.json。"""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        self.data = {"friends": {}, "requests": [], "groups": {}}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                self.data.update(json.load(f))

    def _save(self) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def friends_of(self, user: str) -> list:
        with self.lock:
            return list(self.data["friends"].get(user, []))

    def are_friends(self, a: str, b: str) -> bool:
        with self.lock:
            return b in self.data["friends"].get(a, [])

    def add_request(self, sender: str, target: str):
        """登记好友申请，返回失败原因；成功返回 None。"""
        with self.lock:
            if target in self.data["friends"].get(sender, []):
                return "对方已经是你的好友"
            for req in self.data["requests"]:
                if req["from"] == sender and req["to"] == target:
                    return "已经发送过申请，请等待对方处理"
            self.data["requests"].append({"from": sender, "to": target, "created_at": time.time()})
            self._save()
            return None

    def pop_request(self, sender: str, target: str) -> bool:
        with self.lock:
            before = len(self.data["requests"])
            self.data["requests"] = [
                r for r in self.data["requests"] if not (r["from"] == sender and r["to"] == target)
            ]
            changed = len(self.data["requests"]) != before
            if changed:
                self._save()
            return changed

    def requests_to(self, user: str) -> list:
        with self.lock:
            return [dict(r) for r in self.data["requests"] if r["to"] == user]

    def make_friends(self, a: str, b: str) -> None:
        with self.lock:
            for x, y in ((a, b), (b, a)):
                lst = self.data["friends"].setdefault(x, [])
                if y not in lst:
                    lst.append(y)
            self._save()

    def unfriend(self, a: str, b: str) -> None:
        with self.lock:
            for x, y in ((a, b), (b, a)):
                lst = self.data["friends"].get(x, [])
                if y in lst:
                    lst.remove(y)
            self._save()

    def groups_of(self, user: str) -> list:
        with self.lock:
            return [dict(g, members=list(g["members"])) for g in self.data["groups"].values() if user in g["members"]]

    def get_group(self, group_id: str):
        with self.lock:
            g = self.data["groups"].get(group_id)
            return dict(g, members=list(g["members"])) if g else None

    def create_group(self, owner: str, name: str, members: list) -> dict:
        with self.lock:
            group = {"id": str(uuid.uuid4()), "name": name, "owner": owner, "members": [owner] + members}
            self.data["groups"][group["id"]] = group
            self._save()
            return dict(group, members=list(group["members"]))

    def add_member(self, group_id: str, member: str):
        with self.lock:
            g = self.data["groups"].get(group_id)
            if g is None or member in g["members"]:
                return None
            g["members"].append(member)
            self._save()
            return dict(g, members=list(g["members"]))

    def remove_member(self, group_id: str, member: str):
        """移除成员；群主退出则转给下一位；群空则解散。返回更新后的群（解散/无变化返回 None）。"""
        with self.lock:
            g = self.data["groups"].get(group_id)
            if g is None or member not in g["members"]:
                return None
            g["members"].remove(member)
            if not g["members"]:
                del self.data["groups"][group_id]
                self._save()
                return None
            if g["owner"] == member:
                g["owner"] = g["members"][0]
            self._save()
            return dict(g, members=list(g["members"]))


class ChatServer:
    def __init__(self, host: str, port: int, invite_code: str):
        self.host = host
        self.port = port
        self.invite_code = invite_code
        self.ssl_context = create_server_ssl_context()
        self.accounts = AccountStore(ACCOUNTS_FILE)
        self.social = SocialStore(SOCIAL_FILE)
        self.messages = JsonlStore(MESSAGES_FILE)
        self.group_messages = JsonlStore(GROUP_MESSAGES_FILE)
        self.emoji_files = load_emoji_pack(EMOJI_DIR)
        self.online_clients = {}  # username -> (conn, send_lock)
        self.clients_lock = threading.Lock()
        self.handlers = {
            "msg": self._handle_chat_msg,
            "typing": self._relay_typing,
            "search_users": self._handle_search,
            "get_emoji_pack": self._handle_emoji_pack,
            "friend_request": self._handle_friend_request,
            "friend_response": self._handle_friend_response,
            "unfriend": self._handle_unfriend,
            "create_group": self._handle_create_group,
            "invite_group": self._handle_invite_group,
            "leave_group": self._handle_leave_group,
            "group_msg": self._handle_group_msg,
        }

    def start(self) -> None:
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind((self.host, self.port))
        server_socket.listen()

        print(f"聊天服务器已启动，监听 {self.host}:{self.port}")
        print(f"本机地址: {get_local_ip()}:{self.port}（云服务器请用公网 IP 连接）")
        print(f"新账号注册邀请码: {self.invite_code}")
        print("按 Ctrl+C 停止服务器", flush=True)

        try:
            while True:
                conn, addr = server_socket.accept()
                threading.Thread(target=self._handle_client, args=(conn, addr), daemon=True).start()
        except KeyboardInterrupt:
            print("\n服务器正在停止...")
        finally:
            with self.clients_lock:
                for sock, _ in self.online_clients.values():
                    try:
                        sock.close()
                    except OSError:
                        pass
            server_socket.close()

    def _send(self, username: str, obj: dict) -> bool:
        """给在线用户发消息；同一连接的多线程写入需串行化。返回是否发出。"""
        with self.clients_lock:
            client = self.online_clients.get(username)
        if client is None:
            return False
        sock, lock = client
        try:
            with lock:
                send_msg(sock, obj)
            return True
        except OSError:
            return False

    def _handle_client(self, raw_conn: socket.socket, addr) -> None:
        raw_conn.settimeout(LOGIN_TIMEOUT)
        try:
            conn = self.ssl_context.wrap_socket(raw_conn, server_side=True)
        except (ssl.SSLError, OSError):
            raw_conn.close()
            return

        username = None
        try:
            first = recv_msg(conn, MAX_FRAME_SIZE)
            if first and first.get("type") == "download_client":
                self._handle_download(conn)
                return
            if not first or first.get("type") != "login":
                send_msg(conn, {"type": "error", "reason": "需要先登录"})
                return

            name = first.get("username", "")
            password = first.get("password", "")
            invite_code = first.get("invite_code", "") or ""
            if not valid_name(name) or not isinstance(password, str) or not password:
                send_msg(conn, {"type": "login_fail", "reason": f"账号需为 1~{MAX_NAME_LEN} 个字符，密码不能为空"})
                return

            ok, reason = self.accounts.login(name, password, str(invite_code), self.invite_code)
            if not ok:
                time.sleep(1)  # 拖慢在线暴力猜密码
                send_msg(conn, {"type": "login_fail", "reason": reason})
                return

            # 客户端登录后会同步收 login_ok 和 emoji_pack，期间不能插入别的推送，所以完成后才登记在线
            send_msg(conn, self._build_login_ok(name))
            data = recv_msg(conn, MAX_FRAME_SIZE)
            if data is not None and data.get("type") == "get_emoji_pack":
                send_msg(conn, {"type": "emoji_pack", "files": self.emoji_files})
                data = None
            conn.settimeout(None)

            username = name
            with self.clients_lock:
                old = self.online_clients.get(username)
                self.online_clients[username] = (conn, threading.Lock())
            if old is not None:
                try:
                    old[0].shutdown(socket.SHUT_RDWR)  # 发 FIN 让旧客户端正常感知断开，而不是收到 RST
                    old[0].close()
                except OSError:
                    pass
            print(f"[登录] {username} 来自 {addr[0]}", flush=True)
            self._flush_pending(username)

            while True:
                if data is None:
                    data = recv_msg(conn, MAX_FRAME_SIZE)
                    if data is None:
                        break
                handler = self.handlers.get(data.get("type"))
                if handler is not None:
                    handler(username, data)
                data = None

        except (ConnectionError, OSError):
            pass
        finally:
            if username:
                with self.clients_lock:
                    current = self.online_clients.get(username)
                    if current is not None and current[0] is conn:
                        del self.online_clients[username]
                print(f"[离线] {username}", flush=True)
            conn.close()

    def _build_login_ok(self, username: str) -> dict:
        history = self.messages.query(lambda r: r["to"] == username and not r["delivered"])
        involved = self.messages.query(lambda r: r["to"] == username or r["from"] == username)
        involved.sort(key=lambda r: r["timestamp"])
        self.messages.update([r["id"] for r in history], lambda r: r.update(delivered=True))

        groups = self.social.groups_of(username)
        group_ids = {g["id"] for g in groups}
        group_history = self.group_messages.query(
            lambda r: r["group_id"] in group_ids and username not in r["delivered_to"]
        )
        group_recent = []
        for gid in group_ids:
            records = self.group_messages.query(lambda r, gid=gid: r["group_id"] == gid)
            records.sort(key=lambda r: r["timestamp"])
            group_recent.extend(records[-RECENT_LIMIT:])
        self.group_messages.update([r["id"] for r in group_history], lambda r: r["delivered_to"].append(username))

        return {
            "type": "login_ok",
            "history": history,
            "recent": involved[-RECENT_LIMIT:],
            "groups": groups,
            "group_history": group_history,
            "group_recent": group_recent,
            "broadcasts": [],
            "friends": self.social.friends_of(username),
            "friend_requests": self.social.requests_to(username),
        }

    def _flush_pending(self, username: str) -> None:
        """补发 login_ok 之后、登记在线之前到达的消息。"""
        for record in self.messages.query(lambda r: r["to"] == username and not r["delivered"]):
            incoming = {k: v for k, v in record.items() if k != "delivered"}
            incoming.update(type="incoming", msg_id=record["id"])
            if self._send(username, incoming):
                self.messages.update([record["id"]], lambda r: r.update(delivered=True))
        group_ids = {g["id"] for g in self.social.groups_of(username)}
        for record in self.group_messages.query(
            lambda r: r["group_id"] in group_ids and username not in r["delivered_to"]
        ):
            incoming = {k: v for k, v in record.items() if k != "delivered_to"}
            incoming["type"] = "group_incoming"
            if self._send(username, incoming):
                self.group_messages.update([record["id"]], lambda r: r["delivered_to"].append(username))

    def _handle_download(self, conn) -> None:
        if not os.path.exists(CLIENT_ZIP):
            send_msg(conn, {"type": "download_fail", "reason": "服务器上没有放置 chat_client.zip"})
            return
        with open(CLIENT_ZIP, "rb") as f:
            data = base64.b64encode(f.read()).decode("ascii")
        send_msg(conn, {"type": "download_data", "filename": "chat_client.zip", "data": data})

    def _handle_emoji_pack(self, username: str, data: dict) -> None:
        self._send(username, {"type": "emoji_pack", "files": self.emoji_files})

    def _handle_search(self, username: str, data: dict) -> None:
        """只做完整用户名精确匹配，避免陌生人枚举账号列表。"""
        query = str(data.get("query", "")).strip()
        found = [query] if query and query != username and self.accounts.exists(query) else []
        self._send(username, {"type": "search_result", "users": found})

    def _relay_typing(self, sender: str, data: dict) -> None:
        """输入状态是瞬时提示，不落盘；对方不在线直接丢弃。"""
        target = data.get("to", "")
        if self.social.are_friends(sender, target):
            self._send(target, {"type": "typing", "from": sender, "state": bool(data.get("state"))})

    def _handle_chat_msg(self, sender: str, data: dict) -> None:
        msg_id = data.get("msg_id")
        if not isinstance(msg_id, str) or not 0 < len(msg_id) <= 64:
            msg_id = str(uuid.uuid4())
        recipient = data.get("to", "")
        ciphertext = data.get("ciphertext", "")
        if not isinstance(ciphertext, str) or not self.social.are_friends(sender, recipient):
            self._send(sender, {"type": "msg_ack", "msg_id": msg_id, "status": "not_friend"})
            return

        record = {
            "id": msg_id,
            "from": sender,
            "to": recipient,
            "ciphertext": ciphertext,
            "encrypted": bool(data.get("encrypted", True)),
            "timestamp": time.time(),
            "delivered": False,
        }
        self.messages.append(record)

        incoming = {k: record[k] for k in ("id", "from", "to", "ciphertext", "encrypted", "timestamp")}
        incoming.update(type="incoming", msg_id=msg_id)
        if self._send(recipient, incoming):
            self.messages.update([msg_id], lambda r: r.update(delivered=True))
            status = "delivered"
        else:
            status = "stored"
        self._send(sender, {"type": "msg_ack", "msg_id": msg_id, "status": status})

    def _handle_friend_request(self, sender: str, data: dict) -> None:
        target = str(data.get("to", ""))
        if target == sender or not self.accounts.exists(target):
            self._send(sender, {"type": "friend_request_fail", "reason": "用户不存在"})
            return
        reason = self.social.add_request(sender, target)
        if reason:
            self._send(sender, {"type": "friend_request_fail", "reason": reason})
            return
        self._send(sender, {"type": "friend_request_sent", "target": target})
        self._send(target, {"type": "friend_request", "from": sender})

    def _handle_friend_response(self, responder: str, data: dict) -> None:
        requester = str(data.get("from", ""))
        if not self.social.pop_request(requester, responder):
            return
        if data.get("accept"):
            self.social.make_friends(requester, responder)
            self.social.pop_request(responder, requester)
            self._send(requester, {"type": "friend_added", "friend": responder})
        else:
            self._send(requester, {"type": "friend_request_rejected", "friend": responder})

    def _handle_unfriend(self, user: str, data: dict) -> None:
        target = str(data.get("target", ""))
        if self.social.are_friends(user, target):
            self.social.unfriend(user, target)
            self._send(target, {"type": "unfriended", "by": user})

    def _broadcast_group(self, group: dict, msg_type: str, exclude: str = None) -> list:
        """向群内在线成员推送，返回实际送达的成员名单。"""
        sent = []
        for member in group["members"]:
            if member != exclude and self._send(member, dict(group, type=msg_type)):
                sent.append(member)
        return sent

    def _handle_create_group(self, owner: str, data: dict) -> None:
        raw_members = data.get("members", [])
        if not isinstance(raw_members, list):
            return
        friends = set(self.social.friends_of(owner))
        members = list(dict.fromkeys(m for m in raw_members if m in friends))
        if not members:
            return
        name = str(data.get("name", "")).strip()[:MAX_NAME_LEN] or "、".join([owner] + members[:2]) + "的群聊"
        group = self.social.create_group(owner, name, members)
        self._broadcast_group(group, "group_created")

    def _handle_invite_group(self, user: str, data: dict) -> None:
        group_id = str(data.get("group_id", ""))
        member = str(data.get("member", ""))
        group = self.social.get_group(group_id)
        if group is None or user not in group["members"] or not self.social.are_friends(user, member):
            return
        group = self.social.add_member(group_id, member)
        if group is None:
            return
        self._broadcast_group(group, "group_update", exclude=member)
        self._send(member, dict(group, type="group_created"))

    def _handle_leave_group(self, user: str, data: dict) -> None:
        group = self.social.remove_member(str(data.get("group_id", "")), user)
        if group is not None:
            self._broadcast_group(group, "group_update")

    def _handle_group_msg(self, sender: str, data: dict) -> None:
        group = self.social.get_group(str(data.get("group_id", "")))
        ciphertext = data.get("ciphertext", "")
        if group is None or sender not in group["members"] or not isinstance(ciphertext, str):
            return
        msg_id = data.get("msg_id")
        if not isinstance(msg_id, str) or not 0 < len(msg_id) <= 64:
            msg_id = str(uuid.uuid4())

        record = {
            "id": msg_id,
            "group_id": group["id"],
            "from": sender,
            "ciphertext": ciphertext,
            "encrypted": bool(data.get("encrypted", True)),
            "timestamp": time.time(),
            "delivered_to": [sender],
        }
        self.group_messages.append(record)

        incoming = {k: v for k, v in record.items() if k != "delivered_to"}
        incoming["type"] = "group_incoming"
        sent = [m for m in group["members"] if m != sender and self._send(m, incoming)]
        if sent:
            self.group_messages.update([msg_id], lambda r: r["delivered_to"].extend(sent))


def main() -> None:
    config = load_config()
    parser = argparse.ArgumentParser(description="聊天服务器")
    parser.add_argument("--host", default=config.get("host", "0.0.0.0"), help="监听地址，默认 0.0.0.0")
    parser.add_argument("--port", type=int, default=config.get("port", DEFAULT_PORT), help="监听端口")
    args = parser.parse_args()

    ensure_certificate()
    ChatServer(args.host, args.port, config.get("invite_code", "")).start()


if __name__ == "__main__":
    main()
