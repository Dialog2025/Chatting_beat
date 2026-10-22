"""聊天客户端安装包下载器：无需账号即可从服务器拉取最新的 chat_client.zip。"""
import base64
import socket
import ssl
import sys

from chat_common import create_client_ssl_context, recv_msg, send_msg, DEFAULT_PORT


def main() -> None:
    host = input("服务器地址 [默认 127.0.0.1]: ").strip() or "127.0.0.1"
    port_text = input(f"端口 [默认 {DEFAULT_PORT}]: ").strip()
    port = int(port_text) if port_text else DEFAULT_PORT

    try:
        raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw_sock.settimeout(10)
        raw_sock.connect((host, port))
        sock = create_client_ssl_context().wrap_socket(raw_sock, server_hostname=host)
    except FileNotFoundError:
        print("缺少 server.crt 证书文件，请向服务器管理员索取后放在本脚本同目录下。")
        sys.exit(1)
    except ssl.SSLError as exc:
        print(f"TLS 握手失败: {exc}")
        sys.exit(1)
    except OSError as exc:
        print(f"连接失败: {exc}")
        sys.exit(1)

    send_msg(sock, {"type": "download_client"})
    reply = recv_msg(sock)
    sock.close()

    if reply is None:
        print("服务器无响应")
        sys.exit(1)
    if reply.get("type") == "download_fail":
        print(f"下载失败：{reply.get('reason', '未知错误')}")
        sys.exit(1)
    if reply.get("type") != "download_data":
        print("服务器响应异常")
        sys.exit(1)

    filename = reply.get("filename", "chat_client.zip")
    data = base64.b64decode(reply.get("data", ""))
    with open(filename, "wb") as f:
        f.write(data)
    print(f"下载完成：{filename}（{len(data)} 字节），解压后即可运行里面的 chat_client.pyw")


if __name__ == "__main__":
    main()
