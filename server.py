#!/usr/bin/env python3
# 家庭自驾记账 —— 后端服务器（纯标准库，无需联网、无需第三方账号）
# 账号和账本都保存在本文件同目录的 server_data.json 里；
# 同一网络下的手机和电脑都连它，即可共用一本账。
#
# 两种运行方式：
#   1) 本地 / Mac：  python server.py        （用内置 http.server 监听 PORT，默认 8765）
#   2) 托管平台(如 PythonAnywhere)：平台加载本文件的 wsgi_app 作为 WSGI 应用
import json
import os
import time
import hashlib
import secrets
import threading
import random
import string
import urllib.request
import urllib.error
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(ROOT, "server_data.json")
LOCK = threading.RLock()

def load():
    if os.path.exists(DB):
        try:
            return json.load(open(DB, encoding="utf-8"))
        except Exception:
            pass
    return {"users": {}, "households": {}, "txns": {}, "tokens": {}}

def save(d):
    tmp = DB + ".tmp"
    json.dump(d, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
    os.replace(tmp, DB)
    if GH_TOKEN:
        try:
            threading.Thread(target=sync_to_github, daemon=True).start()
        except Exception:
            pass

DATA = load()

def hashpw(p):
    return hashlib.sha256(("ftl_salt_" + p).encode("utf-8")).hexdigest()

def mkcode(n=6):
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=n))

def new_uid():
    return "u" + secrets.token_hex(8)

CT = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}

STATUS_TEXT = {
    200: "OK", 204: "No Content", 400: "Bad Request", 401: "Unauthorized",
    404: "Not Found", 500: "Internal Server Error",
}

def _resp(code, obj):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    h = {
        "Content-Type": "application/json; charset=utf-8",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Content-Type,Authorization",
        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        "Content-Length": str(len(body)),
    }
    return code, h, body

def _uid_from(headers):
    h = headers.get("Authorization", "") or headers.get("authorization", "")
    if h.startswith("Bearer "):
        return DATA["tokens"].get(h[7:])
    return None

def _parse_body(body):
    try:
        return json.loads(body or b"{}")
    except Exception:
        return {}

def serve_static(path):
    if path in ("/", ""):
        path = "/index.html"
    fp = os.path.normpath(os.path.join(ROOT, path.lstrip("/")))
    if not fp.startswith(ROOT) or not os.path.isfile(fp):
        return 404, {"Content-Type": "application/json"}, b'{"error":"not found"}'
    ext = os.path.splitext(fp)[1].lower()
    try:
        data = open(fp, "rb").read()
    except Exception:
        return 404, {"Content-Type": "application/json"}, b'{"error":"not found"}'
    return 200, {"Content-Type": CT.get(ext, "application/octet-stream"),
                 "Access-Control-Allow-Origin": "*",
                 "Content-Length": str(len(data))}, data

# ─────────────── GitHub 持久化（数据镜像到 GitHub 仓库文件，重启不丢） ───────────────
# 浏览器只与本服务器通信（同源，走 Render，国内可访问）；本服务器把账本实时同步到 GitHub 仓库文件。
# 需配置环境变量：GITHUB_TOKEN(有 repo 权限的 PAT)、GITHUB_REPO(owner/repo)、GITHUB_FILE(文件名)。
# 未配置时自动降级为仅内存+本地磁盘（重启会丢），不影响正常使用。
import base64
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GH_REPO = os.environ.get("GITHUB_REPO", "cngzwlm/family-ledger")
GH_PATH = os.environ.get("GITHUB_FILE", "ledger_data.json")

# 恢复完成标志：只有从 GitHub 成功恢复后，才允许向 GitHub 写，避免启动未完成时清掉已有数据
_restored = False

def _gh_headers(extra=None):
    h = {"Accept": "application/vnd.github+json", "User-Agent": "family-ledger",
         "X-GitHub-Api-Version": "2022-11-28"}
    if GH_TOKEN:
        h["Authorization"] = "Bearer " + GH_TOKEN
    if extra:
        h.update(extra)
    return h

def _gh_url():
    return "https://api.github.com/repos/%s/contents/%s" % (GH_REPO, GH_PATH)

def restore_from_github():
    global _restored
    if not GH_TOKEN:
        return False
    try:
        req = urllib.request.Request(_gh_url(), headers=_gh_headers())
        resp = urllib.request.urlopen(req, timeout=10)
        j = json.loads(resp.read())
        raw = base64.b64decode(j.get("content", "")).decode("utf-8")
        remote = json.loads(raw)
        with LOCK:
            for k in ("users", "households", "txns", "tokens"):
                if k in remote and isinstance(remote[k], dict):
                    DATA[k] = remote[k]
            save(DATA)
        _restored = True
        print("[github] 已从 GitHub 恢复数据")
        return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print("[github] 仓库中尚无数据文件，将在首次变更后创建")
            _restored = True  # 无远程文件即视为恢复阶段结束，允许首次创建
            return False
        print("[github] 恢复失败(忽略):", e)
        return False
    except Exception as e:
        print("[github] 恢复失败(忽略):", e)
        return False

def _restore_loop():
    """后台重试恢复，直到成功或明确无 token；不在主线程阻塞启动。"""
    global _restored
    for i in range(12):
        if _restored or not GH_TOKEN:
            return
        if restore_from_github():
            return
        time.sleep(20)
    _restored = True  # 重试耗尽（如持续网络错误）也视为恢复阶段结束
    # 恢复阶段结束：若已配置 token，立即创建初始文件（无需先发生交易）
    if GH_TOKEN:
        try:
            r = _do_github_push()
            _record_sync(r)
        except Exception:
            pass

_last_sync = {"attempts": 0, "last_ok": None, "last_error": None}

def _record_sync(r):
    global _last_sync
    _last_sync = {"attempts": _last_sync.get("attempts", 0) + 1,
                  "last_ok": r.get("ok"),
                  "last_error": None if r.get("ok") else r}

def _do_github_push():
    """真实向 GitHub 写入一次，返回 dict 结果（供后台同步与诊断共用）。"""
    url = _gh_url()
    sha = None
    try:
        req = urllib.request.Request(url, headers=_gh_headers())
        resp = urllib.request.urlopen(req, timeout=20)
        sha = json.loads(resp.read()).get("sha")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            return {"ok": False, "stage": "get", "code": e.code,
                    "detail": e.read().decode("utf-8", "ignore")[:500]}
    except Exception as e:
        return {"ok": False, "stage": "get", "code": 0, "detail": str(e)[:500]}
    content = base64.b64encode(json.dumps(DATA, ensure_ascii=False).encode("utf-8")).decode("utf-8")
    body = {"message": "ledger sync", "content": content}
    if sha:
        body["sha"] = sha
    try:
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                     headers=_gh_headers({"Content-Type": "application/json"}),
                                     method="PUT")
        urllib.request.urlopen(req, timeout=20)
        return {"ok": True}
    except urllib.error.HTTPError as e:
        return {"ok": False, "stage": "put", "code": e.code,
                "detail": e.read().decode("utf-8", "ignore")[:500]}
    except Exception as e:
        return {"ok": False, "stage": "put", "code": 0, "detail": str(e)[:500]}

def sync_to_github():
    global _restored
    if not GH_TOKEN:
        return
    # 等待启动恢复阶段结束（最多约 25s），避免覆盖远程已有数据
    for _ in range(10):
        if _restored:
            break
        time.sleep(2.5)
    if not _restored:
        return
    r = _do_github_push()
    _record_sync(r)
    if r.get("ok"):
        print("[github] 已同步到 GitHub")
    else:
        print("[github] 同步失败:", r)

# ─────────────── 核心分发（http.server 与 WSGI 共用） ───────────────
def dispatch(method, path, query, headers, body):
    method = (method or "GET").upper()
    p = path.split("?")[0]

    if method == "OPTIONS":
        return 204, {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        }, b""

    if p == "/api/ping":
        return _resp(200, {"ok": True})

    if p == "/api/debug":
        # 当场真实试写一次 GitHub（同时会创建初始文件），把原始错误直接返回
        test = _do_github_push() if GH_TOKEN else {"ok": False, "error": "no_token"}
        return _resp(200, {
            "version": "github-persist",
            "github_configured": bool(GH_TOKEN),
            "github_token_len": len(GH_TOKEN),
            "restored": _restored,
            "repo": GH_REPO,
            "file": GH_PATH,
            "sync_test": test,
            "last_sync": _last_sync,
        })

    if p == "/api/force_sync":
        if not GH_TOKEN:
            return _resp(200, {"ok": False, "error": "no_token"})
        try:
            try:
                req = urllib.request.Request(_gh_url(), headers=_gh_headers())
                resp = urllib.request.urlopen(req, timeout=20)
                sha = json.loads(resp.read()).get("sha")
                exists = True
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    sha = None; exists = False
                else:
                    return _resp(200, {"ok": False, "stage": "get", "code": e.code,
                                        "detail": e.read().decode("utf-8", "ignore")[:400]})
            content = base64.b64encode(json.dumps(DATA, ensure_ascii=False).encode("utf-8")).decode("utf-8")
            body = {"message": "force sync", "content": content}
            if sha:
                body["sha"] = sha
            req2 = urllib.request.Request(_gh_url(), data=json.dumps(body).encode("utf-8"),
                                          headers=_gh_headers({"Content-Type": "application/json"}),
                                          method="PUT")
            resp2 = urllib.request.urlopen(req2, timeout=20)
            return _resp(200, {"ok": True, "exists_before": exists, "http": resp2.getcode()})
        except urllib.error.HTTPError as e:
            return _resp(200, {"ok": False, "stage": "put", "code": e.code,
                               "detail": e.read().decode("utf-8", "ignore")[:400]})
        except Exception as e:
            return _resp(200, {"ok": False, "stage": "exception", "detail": str(e)[:400]})

    if method == "GET":
        if p == "/api/me":
            uid = _uid_from(headers)
            us = DATA["users"].get(uid) if uid else None
            if not us:
                return _resp(401, {"error": "未登录"})
            return _resp(200, {"uid": uid, "email": us["email"],
                               "displayName": us["displayName"],
                               "householdId": us.get("householdId", "")})
        if p == "/api/household":
            hid = (query.get("hid") or [""])[0]
            h = DATA["households"].get(hid)
            if not h:
                return _resp(404, {"error": "账本不存在"})
            return _resp(200, {"id": h["id"], "name": h["name"],
                               "inviteCode": h["inviteCode"],
                               "members": h["members"],
                               "memberProfiles": h.get("memberProfiles", {})})
        if p == "/api/tx":
            hid = (query.get("hid") or [""])[0]
            out = [t for t in DATA["txns"].get(hid, []) if not t.get("isDeleted")]
            return _resp(200, out)
        # 其余当静态文件
        code, h, data = serve_static(p)
        return code, h, data

    if method == "POST":
        b = _parse_body(body)
        if p == "/api/register":
            email = (b.get("email") or "").strip().lower()
            passw = b.get("pass") or ""
            name = (b.get("name") or "").strip() or email.split("@")[0]
            if not email or not passw:
                return _resp(400, {"error": "邮箱和密码都要填"})
            if len(passw) < 6:
                return _resp(400, {"error": "密码至少 6 位"})
            with LOCK:
                if any(v["email"] == email for v in DATA["users"].values()):
                    return _resp(400, {"error": "email-already-in-use"})
                uid = new_uid()
                DATA["users"][uid] = {"uid": uid, "email": email,
                                      "pass": hashpw(passw), "displayName": name,
                                      "householdId": ""}
                tok = secrets.token_hex(24)
                DATA["tokens"][tok] = uid
                save(DATA)
            return _resp(200, {"token": tok, "uid": uid, "email": email,
                               "displayName": name, "householdId": ""})
        if p == "/api/login":
            email = (b.get("email") or "").strip().lower()
            passw = b.get("pass") or ""
            with LOCK:
                us = next((v for v in DATA["users"].values() if v["email"] == email), None)
                if not us or us["pass"] != hashpw(passw):
                    return _resp(401, {"error": "invalid-credential"})
                tok = secrets.token_hex(24)
                DATA["tokens"][tok] = us["uid"]
                save(DATA)
            return _resp(200, {"token": tok, "uid": us["uid"], "email": us["email"],
                               "displayName": us["displayName"],
                               "householdId": us.get("householdId", "")})
        if p == "/api/logout":
            tok = headers.get("Authorization", "") or headers.get("authorization", "")
            if tok.startswith("Bearer "):
                with LOCK:
                    DATA["tokens"].pop(tok[7:], None)
                    save(DATA)
            return _resp(200, {"ok": True})

        uid = _uid_from(headers)
        if not uid or uid not in DATA["users"]:
            return _resp(401, {"error": "未登录"})

        if p == "/api/create":
            name = (b.get("name") or "").strip() or "我们的旅行账本"
            myName = (b.get("myName") or DATA["users"][uid]["displayName"] or "").strip()
            with LOCK:
                hid = "h" + secrets.token_hex(6)
                code = mkcode()
                while any(h.get("inviteCode") == code for h in DATA["households"].values()):
                    code = mkcode()
                DATA["households"][hid] = {
                    "id": hid, "name": name, "inviteCode": code,
                    "members": [uid], "memberProfiles": {uid: myName},
                }
                DATA["txns"][hid] = []
                DATA["users"][uid]["householdId"] = hid
                save(DATA)
            return _resp(200, {"id": hid, "name": name, "inviteCode": code,
                               "members": [uid], "memberProfiles": {uid: myName}})
        if p == "/api/join":
            code = (b.get("code") or "").strip().upper()
            myName = (b.get("myName") or DATA["users"][uid]["displayName"] or "").strip()
            with LOCK:
                h = next((v for v in DATA["households"].values() if v["inviteCode"] == code), None)
                if not h:
                    return _resp(400, {"error": "邀请码无效，请确认后重试"})
                if uid not in h["members"]:
                    h["members"].append(uid)
                h["memberProfiles"][uid] = myName
                DATA["users"][uid]["householdId"] = h["id"]
                save(DATA)
            return _resp(200, {"id": h["id"], "name": h["name"], "inviteCode": h["inviteCode"],
                               "members": h["members"], "memberProfiles": h["memberProfiles"]})
        if p == "/api/tx":
            hid = b.get("hid")
            tx = b.get("tx") or {}
            if hid not in DATA["households"]:
                return _resp(404, {"error": "账本不存在"})
            rec = {
                "id": "t" + secrets.token_hex(6),
                "amount": float(tx.get("amount") or 0),
                "category": tx.get("category") or "其他",
                "note": tx.get("note") or "",
                "paidBy": tx.get("paidBy") or "",
                "paidById": tx.get("paidById") or "",
                "date": tx.get("date") or "",
                "createdBy": tx.get("createdBy") or uid,
                "createdAt": 0,
                "isDeleted": False,
            }
            with LOCK:
                DATA["txns"].setdefault(hid, []).append(rec)
                save(DATA)
            return _resp(200, {"ok": True, "id": rec["id"]})
        if p == "/api/del":
            hid = b.get("hid")
            tid = b.get("id")
            found = False
            with LOCK:
                for t in DATA["txns"].get(hid, []):
                    if t["id"] == tid:
                        t["isDeleted"] = True
                        found = True
                        break
                if found:
                    save(DATA)
            return _resp(200, {"ok": found})

    return _resp(404, {"error": "not found"})

# ─────────────── 方式 1：本地 http.server（Mac / 家用） ───────────────
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _write(self, code, headers, body):
        self.send_response(code)
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_OPTIONS(self):
        code, h, b = dispatch("OPTIONS", self.path, {}, self.headers, b"")
        self._write(code, h, b)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        hdrs = {k: v for k, v in self.headers.items()}
        code, h, b = dispatch("GET", u.path, q, hdrs, b"")
        self._write(code, h, b)

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        hdrs = {k: v for k, v in self.headers.items()}
        try:
            n = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(n) if n else b"{}"
        except Exception:
            raw = b"{}"
        code, h, b = dispatch("POST", u.path, q, hdrs, raw)
        self._write(code, h, b)

# ─────────────── 方式 2：WSGI（PythonAnywhere 等托管平台） ───────────────
def wsgi_app(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO") or "/"
    qs = environ.get("QUERY_STRING", "")
    query = parse_qs(qs)
    headers = {}
    for k, v in environ.items():
        if k.startswith("HTTP_"):
            headers[k[5:].replace("_", "-").title()] = v
        elif k in ("CONTENT_TYPE", "CONTENT_LENGTH", "AUTHORIZATION"):
            headers[k.replace("_", "-").title()] = v
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except Exception:
        length = 0
    body = environ["wsgi.input"].read(length) if length else b"{}"
    code, resp_headers, body_bytes = dispatch(method, path, query, headers, body)
    status = "%d %s" % (code, STATUS_TEXT.get(code, ""))
    start_response(status, [(k, str(v)) for k, v in resp_headers.items()])
    return [body_bytes]

# 启动前：后台非阻塞地从 GitHub 恢复数据（恢复成功前不会向 GitHub 写，避免清掉已有数据）
try:
    threading.Thread(target=_restore_loop, daemon=True).start()
except Exception as e:
    print("[github] 启动恢复线程失败(忽略):", e)

if __name__ == "__main__":
    PORT = int(os.environ.get("PORT", "8765"))
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    print("家庭自驾记账服务器已启动： http://localhost:%d" % PORT)
    print("手机请访问： http://<本机局域网IP>:%d （手机与 Mac 需同一 WiFi）" % PORT)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
