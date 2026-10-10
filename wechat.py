#!/usr/bin/env python3
"""微信公众号草稿工具；所有联网请求通过配置的 SSH 固定出口。"""
import argparse
from contextlib import contextmanager
import getpass
import hashlib
import http.client
import ipaddress
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
BASE = "https://api.weixin.qq.com/cgi-bin/"
KEYS = ("WECHAT_APP_ID", "WECHAT_APP_SECRET", "WECHAT_SSH_HOST",
        "WECHAT_SSH_KEY", "WECHAT_SSH_KNOWN_HOSTS")
ASSET = re.compile(r'asset://([^"\s<>]+)')


def config():
    values = {}
    for path in (ROOT / ".env.example", ROOT / ".env"):
        if path.exists():
            for line in path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    values[key.strip()] = value.strip().strip("\"'")
    return {key: os.environ.get(key, values.get(key, "")) for key in KEYS}


def validate(cfg):
    if any(not cfg[key] for key in KEYS):
        raise RuntimeError("配置不完整，请填写 wechat_api/.env。")
    if not re.fullmatch(r"[\w-]+@[A-Za-z0-9.-]+", cfg["WECHAT_SSH_HOST"]):
        raise RuntimeError("WECHAT_SSH_HOST 应为 用户名@主机。")
    for key in ("WECHAT_SSH_KEY", "WECHAT_SSH_KNOWN_HOSTS"):
        if not Path(cfg[key]).expanduser().is_file():
            raise RuntimeError(f"{key} 指定的文件不存在。")


def write_private(path, content):
    temporary = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def tunnel(cfg):
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    command = ["ssh", "-F", "/dev/null", "-N", "-T", "-i",
               str(Path(cfg["WECHAT_SSH_KEY"]).expanduser()),
               "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
               "-o", "StrictHostKeyChecking=yes", "-o",
               "UserKnownHostsFile=" + str(Path(cfg["WECHAT_SSH_KNOWN_HOSTS"]).expanduser()),
               "-o", "GlobalKnownHostsFile=/dev/null", "-o", "ForwardAgent=no",
               "-o", "ExitOnForwardFailure=yes", "-o", "ConnectTimeout=10",
               "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
               "-L", f"127.0.0.1:{port}:api.weixin.qq.com:443", cfg["WECHAT_SSH_HOST"]]
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 15
        while True:
            if process.poll() is not None:
                raise RuntimeError("SSH 隧道未建立：" + process.stderr.read().strip())
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("SSH 隧道连接超时。")
                time.sleep(0.1)
        yield port
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        process.stderr.close()


def api_opener(port):
    class Connection(http.client.HTTPSConnection):
        def connect(self):
            if self.host != "api.weixin.qq.com" or self.port != 443:
                raise RuntimeError("隧道仅允许连接微信官方 API。")
            raw = socket.create_connection(("127.0.0.1", port), self.timeout)
            try:
                self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
            except BaseException:
                raw.close()
                raise

    class Handler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(Connection, req, context=self._context)

    return urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                      Handler(context=ssl.create_default_context()))


class WeChat:
    def __init__(self, cfg, port):
        self.cfg = cfg
        self.opener = api_opener(port)
        self.access_token = None

    def request(self, endpoint, payload=None, authenticated=True, multipart=None, method="POST"):
        url = BASE + endpoint
        if authenticated:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode({"access_token": self.token()})
        content_type = "application/json; charset=utf-8"
        if method == "GET":
            data = None
        elif multipart:
            data, content_type = multipart
        else:
            data = json.dumps(payload or {}, ensure_ascii=False).encode()
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": content_type})
        try:
            with self.opener.open(req, timeout=45) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"微信接口 HTTP 错误：{exc.code}。") from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
            raise RuntimeError("微信接口连接失败，请检查服务器网络和 SSH 隧道。") from None
        code = result.get("errcode", 0)
        if code:
            if code == 40164:
                match = re.search(r"invalid ip\s+([0-9a-fA-F:.]+)", result.get("errmsg", ""))
                if match:
                    try:
                        address = ipaddress.ip_address(match.group(1).rstrip("."))
                    except ValueError:
                        pass
                    else:
                        raise RuntimeError(f"微信错误 40164：请在 API IP 白名单添加 {address}。")
            hints = {40164: "服务器出口 IP 不在白名单。", 40013: "AppID 不正确。",
                     40125: "AppSecret 不正确。", 48001: "账号缺少此接口权限。",
                     45009: "接口调用次数已达限制。"}
            raise RuntimeError(f"微信错误 {code}：{hints.get(code, '请查询官方错误码说明。')}")
        return result

    def token(self):
        if self.access_token is None:
            result = self.request("stable_token", {"grant_type": "client_credential",
                                  "appid": self.cfg["WECHAT_APP_ID"],
                                  "secret": self.cfg["WECHAT_APP_SECRET"],
                                  "force_refresh": False}, authenticated=False)
            self.access_token = result["access_token"]
        return self.access_token

    def upload(self, path, material=False):
        """正文图走 uploadimg（仅 jpg/png，≤1MB）；封面与 GIF 走永久素材（≤10MB），同样返回 url。"""
        mime = image_type(path)
        boundary = secrets.token_hex(24)
        data = (f'--{boundary}\r\nContent-Disposition: form-data; name="media"; '
                f'filename="{path.name}"\r\nContent-Type: {mime}\r\n\r\n').encode()
        data += path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        endpoint = "material/add_material?type=image" if material else "media/uploadimg"
        return self.request(endpoint, multipart=(data, f"multipart/form-data; boundary={boundary}"))


def image_type(path):
    if not path.is_file():
        raise RuntimeError(f"图片不存在：{path.name}")
    mime = mimetypes.guess_type(path.name)[0]
    if mime not in ("image/jpeg", "image/png", "image/gif"):
        raise RuntimeError(f"不支持的图片格式：{path.name}")
    limit = 10_000_000 if mime == "image/gif" else 1_000_000
    if path.stat().st_size > limit:
        raise RuntimeError(f"图片超过 {limit // 1_000_000}MB：{path.name}")
    return mime


def read_article(path):
    path = Path(path).resolve()
    article = json.loads(path.read_text())
    content = (path.parent / article.pop("content_file")).read_text()
    cover = path.parent / article.pop("cover_file")
    if not article.get("title") or not content.strip():
        raise RuntimeError("标题和正文不能为空。")
    article.setdefault("need_open_comment", 1)   # 微信默认关闭留言；本工具默认开启
    assets = {name: path.parent / name for name in ASSET.findall(content)}
    for image in (*assets.values(), cover):
        image_type(image)
    fingerprint = hashlib.sha256(json.dumps(article, sort_keys=True).encode() + content.encode())
    for image in (*assets.values(), cover):
        fingerprint.update(image.read_bytes())
    return article, content, cover, assets, fingerprint.hexdigest()


def save_draft(client, prepared):
    article, content, cover, assets, fingerprint = prepared
    receipt_path = ROOT / "draft-result.json"
    receipt = json.loads(receipt_path.read_text()) if receipt_path.exists() else {}
    if receipt.get("fingerprint") == fingerprint and receipt.get("media_id"):
        media_id = receipt["media_id"]
    else:
        urls = {name: client.upload(image, material=image.suffix.lower() == ".gif")["url"]
                for name, image in assets.items()}
        article["content"] = ASSET.sub(lambda match: urls[match[1]], content)
        article["thumb_media_id"] = client.upload(cover, material=True)["media_id"]
        media_id = client.request("draft/add", {"articles": [article]})["media_id"]
        receipt = {"media_id": media_id, "title": article["title"],
                   "fingerprint": fingerprint, "verified": False}
        write_private(receipt_path, json.dumps(receipt, ensure_ascii=False, indent=2))
    verified = client.request("draft/get", {"media_id": media_id})
    items = verified.get("news_item", [])
    if not items or items[0].get("title") != article["title"]:
        raise RuntimeError("草稿已创建，但读取验证未通过；ID 已保存，请勿重复创建。")
    receipt["verified"] = True
    write_private(receipt_path, json.dumps(receipt, ensure_ascii=False, indent=2))
    print("草稿已保存并读取验证：" + article["title"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="检查本地凭据与 SSH 文件，不联网")
    sub.add_parser("auth", help="一次检查固定出口、认证和草稿箱权限")
    sub.add_parser("configure", help="本地更新 AppID/AppSecret，隐藏密钥输入")
    draft = sub.add_parser("draft", help="上传图片、保存 HTML 草稿并读取验证")
    draft.add_argument("--article", required=True, help="文章 JSON 路径")
    args = parser.parse_args()
    cfg = config()
    if args.command == "configure":
        if not sys.stdin.isatty():
            raise RuntimeError("请在交互式终端运行 configure，以隐藏密钥输入。")
        cfg["WECHAT_APP_ID"] = input("公众号 AppID: ").strip()
        cfg["WECHAT_APP_SECRET"] = getpass.getpass("公众号 AppSecret（输入不显示）: ").strip()
        if any(not cfg[key] or any(c in cfg[key] for c in "\r\n") for key in KEYS):
            raise RuntimeError("配置不能为空，也不能包含换行。")
        write_private(ROOT / ".env", "".join(f"{key}={cfg[key]}\n" for key in KEYS))
        print("凭据已保存；SSH 出口配置已保留。")
        return 0
    validate(cfg)
    if args.command == "check":
        print("本地凭据与 SSH 文件齐全；固定出口：" + cfg["WECHAT_SSH_HOST"])
        return 0
    prepared = read_article(args.article) if args.command == "draft" else None
    with tunnel(cfg) as port:
        client = WeChat(cfg, port)
        if args.command == "auth":
            result = client.request("draft/count", method="GET")
            print(f"SSH 隧道、微信认证与草稿箱权限均通过；草稿数量：{result['total_count']}。")
        else:
            save_draft(client, prepared)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except (RuntimeError, OSError, ValueError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
