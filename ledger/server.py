#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工程收支帳務系統 — 多專案多人連線版伺服器
只用 Python 標準函式庫（http.server + sqlite3），DSM 內建 Python3 即可執行。
用法：python3 server.py [port]   （預設 8123）
資料：data/projects.json 為專案清單；每個專案一個 data/<pid>.db。
b1 專案首次啟動自動從 seed.json 匯入；其他專案從空白開始。
帳號：data/users.json（密碼以 PBKDF2 雜湊儲存）；登入 session 存 data/sessions.json。
"""
import base64
import datetime
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
PROJECTS_PATH = os.path.join(DATA_DIR, "projects.json")
USERS_PATH = os.path.join(DATA_DIR, "users.json")
SESSIONS_PATH = os.path.join(DATA_DIR, "sessions.json")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
UPLOAD_ROOT = os.path.join(DATA_DIR, "uploads")
API_KEY_PATH = os.path.normpath(os.path.join(BASE, "..", "API.txt"))
SEED_PATH = os.path.join(BASE, "seed.json")
APP_PATH = os.path.join(BASE, "app.html")
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8123

TABLES = ("expense", "income", "deposits", "invoices")
PID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
UPLOAD_FILENAME_RE = re.compile(r"^[0-9a-f]{32}\.(jpg|jpeg|png|webp)$")
TAXID_RE = re.compile(r"^\d{8}$")
PBKDF2_ITERS = 200000
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 天
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_PROMPT = (
    "你是台灣發票／收據辨識助手。這張照片可能是手寫收據，也可能是印刷發票，請盡量辨認內容，"
    "看不清楚或無法判斷的欄位請填 null，不要瞎猜。只回傳 JSON，不要任何其他文字或說明，格式：\n"
    '{"issuer": "開票單位或店家名稱", "date": "YYYY-MM-DD，無法確定年份就用原文", '
    '"amount": 總金額數字或null, "taxId": "賣方統一編號8碼或null", '
    '"buyerTaxId": "買方統一編號8碼，若單據上沒有寫抬頭統編就填null", '
    '"items": "品項摘要文字", "confidence": "high/medium/low"}'
)
_lock = threading.RLock()
_sessions = {}


def now_iso():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------- 專案清單 ----------
def load_projects():
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(PROJECTS_PATH):
        with open(PROJECTS_PATH, encoding="utf-8") as f:
            return json.load(f)
    projects = {}
    save_projects(projects)
    return projects


def save_projects(projects):
    tmp = PROJECTS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(projects, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PROJECTS_PATH)


def new_pid(projects):
    n = 1
    for pid in projects:
        m = re.fullmatch(r"p(\d+)", pid)
        if m:
            n = max(n, int(m.group(1)) + 1)
    return f"p{n}"


# ---------- 使用者／登入 ----------
def load_users():
    if os.path.exists(USERS_PATH):
        with open(USERS_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_users(users):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = USERS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=1)
    os.replace(tmp, USERS_PATH)


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERS)
    return salt, dk.hex()


def verify_login(username, password):
    users = load_users()
    u = users.get(username)
    if not u:
        return None
    _, h = hash_password(password, u.get("salt", ""))
    if hmac.compare_digest(h, u.get("hash", "")):
        return {"name": u.get("name", username), "role": u.get("role", "user")}
    return None


def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_config(cfg):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CONFIG_PATH)


def load_api_key():
    try:
        with open(API_KEY_PATH, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def call_gemini_ocr(image_bytes, mime):
    """呼叫 Gemini Vision 辨識發票/收據內容，回傳 (fields_dict, error_message)。"""
    key = load_api_key()
    if not key:
        return None, "找不到 API 金鑰（" + API_KEY_PATH + "）"
    b64 = base64.b64encode(image_bytes).decode("ascii")
    body = json.dumps({
        "contents": [{"parts": [
            {"inline_data": {"mime_type": mime, "data": b64}},
            {"text": GEMINI_PROMPT},
        ]}],
        "generationConfig": {"response_mime_type": "application/json"},
    }).encode("utf-8")
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           + GEMINI_MODEL + ":generateContent?key=" + key)
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:200]
        except OSError:
            pass
        return None, f"AI 服務回應錯誤（{e.code}）{detail}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return None, f"連不到 AI 辨識服務：{e}"
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
            text = re.sub(r"```\s*$", "", text).strip()
        fields = json.loads(text)
    except (KeyError, IndexError, ValueError, json.JSONDecodeError):
        return None, "AI 回覆格式無法解析"
    return fields, None


def load_sessions():
    global _sessions
    if os.path.exists(SESSIONS_PATH):
        try:
            with open(SESSIONS_PATH, encoding="utf-8") as f:
                _sessions = json.load(f)
        except (OSError, json.JSONDecodeError):
            _sessions = {}


def save_sessions():
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = SESSIONS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_sessions, f, ensure_ascii=False)
    os.replace(tmp, SESSIONS_PATH)


# ---------- 資料庫 ----------
def db(pid):
    conn = sqlite3.connect(os.path.join(DATA_DIR, pid + ".db"))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS records(
        id INTEGER, tbl TEXT, data TEXT, PRIMARY KEY(tbl, id))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS meta(
        key TEXT PRIMARY KEY, value TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS history(
        hid INTEGER PRIMARY KEY AUTOINCREMENT,
        tbl TEXT, record_id INTEGER, action TEXT,
        before TEXT, after TEXT, user TEXT, ts TEXT, rev INTEGER)""")
    if conn.execute("SELECT value FROM meta WHERE key='rev'").fetchone() is None:
        if pid == "b1" and os.path.exists(SEED_PATH):
            load_seed(conn, pid)
        conn.execute("INSERT OR REPLACE INTO meta VALUES('rev','1')")
        conn.commit()
    return conn


def load_seed(conn, pid):
    conn.execute("DELETE FROM records")
    if pid == "b1" and os.path.exists(SEED_PATH):
        with open(SEED_PATH, encoding="utf-8") as f:
            seed = json.load(f)
        for tbl in TABLES:
            for i, rec in enumerate(seed.get(tbl, [])):
                rec = dict(rec)
                rid = rec.pop("id", None) or i + 1
                conn.execute("INSERT OR REPLACE INTO records VALUES(?,?,?)",
                             (rid, tbl, json.dumps(rec, ensure_ascii=False)))


def get_rev(conn):
    return int(conn.execute("SELECT value FROM meta WHERE key='rev'").fetchone()[0])


def bump_rev(conn):
    rev = get_rev(conn) + 1
    conn.execute("UPDATE meta SET value=? WHERE key='rev'", (str(rev),))
    return rev


def full_state(conn):
    state = {"rev": get_rev(conn)}
    for tbl in TABLES:
        rows = conn.execute(
            "SELECT id, data FROM records WHERE tbl=? ORDER BY id", (tbl,))
        state[tbl] = [{**json.loads(d), "id": i} for i, d in rows]
    return state


def log_history(conn, tbl, rid, action, before, after, user, rev):
    conn.execute(
        "INSERT INTO history(tbl,record_id,action,before,after,user,ts,rev) VALUES(?,?,?,?,?,?,?,?)",
        (tbl, rid, action, before, after, user["name"], now_iso(), rev))


class Handler(BaseHTTPRequestHandler):
    server_version = "LedgerServer/1.0"

    def _send(self, code, body, ctype="application/json; charset=utf-8", headers=None):
        raw = body if isinstance(body, bytes) else json.dumps(
            body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def _pid(self, seg):
        """驗證 /api/p/<pid>/... 的 pid，無效回 None 並回應錯誤。"""
        projects = load_projects()
        if PID_RE.match(seg) and seg in projects:
            return seg
        self._send(404, {"error": "專案不存在"})
        return None

    def get_cookie(self, name):
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            part = part.strip()
            if part.startswith(name + "="):
                return part[len(name) + 1:]
        return None

    def current_user(self):
        token = self.get_cookie("sid")
        if not token:
            return None
        with _lock:
            sess = _sessions.get(token)
        return dict(sess) if sess else None

    def require_login(self):
        user = self.current_user()
        if not user:
            self._send(401, {"error": "請先登入"})
            return None
        return user

    def require_admin(self):
        user = self.require_login()
        if user is None:
            return None
        if user.get("role") != "admin":
            self._send(403, {"error": "需要管理者權限"})
            return None
        return user

    # ---------- GET ----------
    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html", "/app.html"):
            try:
                with open(APP_PATH, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, {"error": "app.html not found"})
            return
        if path == "/api/me":
            user = self.current_user()
            self._send(200, {
                "user": {"name": user["name"], "role": user["role"]} if user else None,
                "needsSetup": not load_users(),
            })
            return
        if path == "/api/users":
            if self.require_admin() is None:
                return
            users = load_users()
            out = [{"username": k, "name": v.get("name", k), "role": v.get("role", "user")}
                   for k, v in users.items()]
            self._send(200, out)
            return
        if path == "/api/projects":
            projects = load_projects()
            self._send(200, [{"id": k, "name": v["name"]} for k, v in projects.items()])
            return
        if path == "/api/config":
            self._send(200, load_config())
            return
        m = re.fullmatch(r"/api/p/([^/]+)/uploads/([^/]+)", path)
        if m:
            pid = self._pid(m.group(1))
            if not pid:
                return
            fname = m.group(2)
            if not UPLOAD_FILENAME_RE.match(fname):
                self._send(404, {"error": "not found"})
                return
            fpath = os.path.join(UPLOAD_ROOT, pid, fname)
            if not os.path.isfile(fpath):
                self._send(404, {"error": "not found"})
                return
            ext = fname.rsplit(".", 1)[-1].lower()
            ctype = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                     "png": "image/png", "webp": "image/webp"}[ext]
            with open(fpath, "rb") as f:
                self._send(200, f.read(), ctype)
            return
        m = re.fullmatch(r"/api/p/([^/]+)/history", path)
        if m:
            user = self.require_login()
            if user is None:
                return
            qs = self.path.split("?")[1] if "?" in self.path else ""
            limit = 100
            for kv in qs.split("&"):
                if kv.startswith("limit="):
                    try:
                        limit = max(1, min(500, int(kv[6:])))
                    except ValueError:
                        pass
            with _lock:
                pid = self._pid(m.group(1))
                if not pid:
                    return
                conn = db(pid)
                try:
                    rows = conn.execute(
                        "SELECT hid, tbl, record_id, action, before, after, user, ts, rev "
                        "FROM history ORDER BY hid DESC LIMIT ?", (limit,)).fetchall()
                    out = [{
                        "hid": hid, "tbl": tbl, "record_id": rid, "action": action,
                        "before": json.loads(before) if before else None,
                        "after": json.loads(after) if after else None,
                        "user": uname, "ts": ts, "rev": rev,
                    } for hid, tbl, rid, action, before, after, uname, ts, rev in rows]
                    self._send(200, out)
                finally:
                    conn.close()
            return
        m = re.fullmatch(r"/api/p/([^/]+)/state", path)
        if m:
            qs = self.path.split("?")[1] if "?" in self.path else ""
            since = 0
            for kv in qs.split("&"):
                if kv.startswith("since="):
                    try:
                        since = int(kv[6:])
                    except ValueError:
                        pass
            with _lock:
                pid = self._pid(m.group(1))
                if not pid:
                    return
                conn = db(pid)
                try:
                    rev = get_rev(conn)
                    if since and since == rev:
                        self._send(200, {"rev": rev, "unchanged": True})
                    else:
                        self._send(200, full_state(conn))
                finally:
                    conn.close()
            return
        self._send(404, {"error": "not found"})

    # ---------- POST ----------
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._send(400, {"error": "bad json"})
            return
        path = self.path.split("?")[0]

        if path == "/api/setup":
            self._setup_admin(payload)
            return
        if path == "/api/login":
            self._login(payload)
            return
        if path == "/api/logout":
            self._logout()
            return
        if path == "/api/users":
            if self.require_admin() is None:
                return
            self._upsert_user(payload)
            return
        if path == "/api/users/delete":
            if self.require_admin() is None:
                return
            self._delete_user(payload)
            return
        if path == "/api/config":
            if self.require_admin() is None:
                return
            self._save_config(payload)
            return
        m = re.fullmatch(r"/api/p/([^/]+)/invoices/upload", path)
        if m:
            pid = self._pid(m.group(1))
            if not pid:
                return
            if self.require_login() is None:
                return
            self._upload_invoice(pid, payload)
            return

        with _lock:
            if path == "/api/projects":
                if self.require_login() is None:
                    return
                self._create_project(payload)
                return
            m = re.fullmatch(r"/api/p/([^/]+)/(save|replace|reset)", path)
            if m:
                pid = self._pid(m.group(1))
                if not pid:
                    return
                action = m.group(2)
                user = self.require_login()
                if user is None:
                    return
                if action in ("replace", "reset") and user.get("role") != "admin":
                    self._send(403, {"error": "需要管理者權限"})
                    return
                conn = db(pid)
                try:
                    if action == "save":
                        self._save(conn, payload, user)
                    elif action == "replace":
                        self._replace(conn, payload, user)
                    else:
                        self._reset(conn, pid, user)
                finally:
                    conn.close()
                return
            m = re.fullmatch(r"/api/p/([^/]+)/history/(\d+)/revert", path)
            if m:
                pid = self._pid(m.group(1))
                if not pid:
                    return
                user = self.require_admin()
                if user is None:
                    return
                conn = db(pid)
                try:
                    self._revert(conn, int(m.group(2)), user)
                finally:
                    conn.close()
                return
            self._send(404, {"error": "not found"})

    # ---------- 登入／使用者管理 ----------
    def _setup_admin(self, p):
        """第一次啟動、users.json 還沒有任何帳號時，讓訪客直接建立第一個管理者帳號。
        一旦有任何帳號存在就永久關閉這個入口，改用一般登入。"""
        username = str(p.get("username") or "").strip()
        if not USERNAME_RE.match(username):
            self._send(400, {"error": "帳號只能是英數字、_、-，1–32字"})
            return
        name = str(p.get("name") or username).strip()
        password = str(p.get("password") or "")
        if len(password) < 4:
            self._send(400, {"error": "密碼至少 4 碼"})
            return
        with _lock:
            if load_users():
                self._send(400, {"error": "系統已經設定過管理者帳號，請用登入功能"})
                return
            salt, h = hash_password(password)
            save_users({username: {"name": name, "role": "admin", "salt": salt, "hash": h}})
            token = secrets.token_hex(24)
            _sessions[token] = {"username": username, "name": name, "role": "admin"}
            save_sessions()
        self._send(200, {"user": {"name": name, "role": "admin"}}, headers={
            "Set-Cookie": f"sid={token}; Path=/; HttpOnly; Max-Age={SESSION_MAX_AGE}; SameSite=Lax"
        })

    def _login(self, p):
        username = str(p.get("username") or "").strip()
        password = str(p.get("password") or "")
        with _lock:
            info = verify_login(username, password)
            if not info:
                self._send(401, {"error": "帳號或密碼錯誤"})
                return
            token = secrets.token_hex(24)
            _sessions[token] = {"username": username, "name": info["name"], "role": info["role"]}
            save_sessions()
        self._send(200, {"user": {"name": info["name"], "role": info["role"]}}, headers={
            "Set-Cookie": f"sid={token}; Path=/; HttpOnly; Max-Age={SESSION_MAX_AGE}; SameSite=Lax"
        })

    def _logout(self):
        token = self.get_cookie("sid")
        with _lock:
            if token and token in _sessions:
                del _sessions[token]
                save_sessions()
        self._send(200, {"ok": True}, headers={"Set-Cookie": "sid=; Path=/; HttpOnly; Max-Age=0"})

    def _upsert_user(self, p):
        username = str(p.get("username") or "").strip()
        if not USERNAME_RE.match(username):
            self._send(400, {"error": "帳號只能是英數字、_、-，1–32字"})
            return
        name = str(p.get("name") or username).strip()
        role = p.get("role") if p.get("role") in ("admin", "user") else "user"
        password = p.get("password")
        with _lock:
            users = load_users()
            if password:
                salt, h = hash_password(str(password))
                users[username] = {"name": name, "role": role, "salt": salt, "hash": h}
            elif username in users:
                users[username]["name"] = name
                users[username]["role"] = role
            else:
                self._send(400, {"error": "新帳號必須設定密碼"})
                return
            save_users(users)
        self._send(200, {"username": username, "name": name, "role": role})

    def _delete_user(self, p):
        username = str(p.get("username") or "").strip()
        with _lock:
            users = load_users()
            if username in users:
                del users[username]
                save_users(users)
        self._send(200, {"ok": True})

    # ---------- 系統設定 ----------
    def _save_config(self, p):
        tax_id = str(p.get("companyTaxId") or "").strip()
        if tax_id and not TAXID_RE.match(tax_id):
            self._send(400, {"error": "統一編號需為 8 碼數字"})
            return
        with _lock:
            cfg = load_config()
            cfg["companyTaxId"] = tax_id
            save_config(cfg)
        self._send(200, cfg)

    # ---------- 發票上傳與辨識 ----------
    def _upload_invoice(self, pid, p):
        data_url = str(p.get("dataUrl") or "")
        m = re.match(r"^data:(image/(?:jpeg|png|webp));base64,(.+)$", data_url, re.S)
        if not m:
            self._send(400, {"error": "圖片格式不支援（僅接受 jpeg/png/webp）"})
            return
        mime, b64 = m.group(1), m.group(2)
        try:
            raw = base64.b64decode(b64, validate=True)
        except ValueError:
            self._send(400, {"error": "圖片資料無法解碼"})
            return
        if not raw or len(raw) > 12 * 1024 * 1024:
            self._send(400, {"error": "圖片太大或是空的（上限 12MB）"})
            return
        ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[mime]
        fname = f"{uuid.uuid4().hex}.{ext}"
        updir = os.path.join(UPLOAD_ROOT, pid)
        os.makedirs(updir, exist_ok=True)
        with open(os.path.join(updir, fname), "wb") as f:
            f.write(raw)
        fields, err = call_gemini_ocr(raw, mime)
        buyer_match = None
        if fields and fields.get("buyerTaxId"):
            company = (load_config().get("companyTaxId") or "").strip()
            buyer_match = bool(company) and str(fields["buyerTaxId"]).strip() == company
        self._send(200, {"file": fname, "ocr": fields, "ocrError": err, "buyerMatch": buyer_match})

    # ---------- 專案 ----------
    def _create_project(self, p):
        name = str(p.get("name") or "").strip()
        if not name or len(name) > 50:
            self._send(400, {"error": "請提供 1–50 字的專案名稱"})
            return
        projects = load_projects()
        if any(v["name"] == name for v in projects.values()):
            self._send(400, {"error": "已有同名專案"})
            return
        pid = new_pid(projects)
        projects[pid] = {"name": name}
        save_projects(projects)
        db(pid).close()   # 立即建立空資料庫
        self._send(200, {"id": pid, "name": name})

    # ---------- 記帳 ----------
    def _save(self, conn, p, user):
        tbl, action, rec = p.get("table"), p.get("action"), p.get("record") or {}
        if tbl not in TABLES or action not in ("add", "update", "delete"):
            self._send(400, {"error": "bad request"})
            return
        if action == "delete" and user.get("role") != "admin":
            self._send(403, {"error": "需要管理者權限才能刪除"})
            return
        rid = rec.get("id")
        before = None
        if action in ("update", "delete"):
            if not isinstance(rid, int):
                self._send(400, {"error": "missing id"})
                return
            row = conn.execute("SELECT data FROM records WHERE tbl=? AND id=?", (tbl, rid)).fetchone()
            before = row[0] if row else None
        elif action == "add":
            row = conn.execute(
                "SELECT COALESCE(MAX(id),0)+1 FROM records WHERE tbl=?", (tbl,)).fetchone()
            rid = row[0]
        if action == "delete":
            conn.execute("DELETE FROM records WHERE tbl=? AND id=?", (tbl, rid))
            after = None
        else:
            body = {k: v for k, v in rec.items() if k != "id"}
            after = json.dumps(body, ensure_ascii=False)
            conn.execute("INSERT OR REPLACE INTO records VALUES(?,?,?)", (rid, tbl, after))
        rev = bump_rev(conn)
        log_history(conn, tbl, rid, action, before, after, user, rev)
        conn.commit()
        self._send(200, {"rev": rev, "id": rid})

    def _replace(self, conn, p, user):
        if not isinstance(p.get("expense"), list) or not isinstance(p.get("income"), list):
            self._send(400, {"error": "格式不符"})
            return
        before = json.dumps(full_state(conn), ensure_ascii=False)
        conn.execute("DELETE FROM records")
        for tbl in TABLES:
            for i, rec in enumerate(p.get(tbl) or []):
                rec = dict(rec)
                rid = rec.pop("id", None) or i + 1
                conn.execute("INSERT OR REPLACE INTO records VALUES(?,?,?)",
                             (rid, tbl, json.dumps(rec, ensure_ascii=False)))
        rev = bump_rev(conn)
        after = json.dumps(full_state(conn), ensure_ascii=False)
        log_history(conn, "*", None, "replace", before, after, user, rev)
        conn.commit()
        self._send(200, full_state(conn))

    def _reset(self, conn, pid, user):
        before = json.dumps(full_state(conn), ensure_ascii=False)
        load_seed(conn, pid)
        rev = bump_rev(conn)
        after = json.dumps(full_state(conn), ensure_ascii=False)
        log_history(conn, "*", None, "reset", before, after, user, rev)
        conn.commit()
        self._send(200, full_state(conn))

    def _revert(self, conn, hid, user):
        row = conn.execute(
            "SELECT tbl, record_id, action, before, after FROM history WHERE hid=?", (hid,)).fetchone()
        if not row:
            self._send(404, {"error": "找不到這筆記錄"})
            return
        tbl, rid, _action, before, _after = row
        if tbl == "*":
            snapshot = json.loads(before) if before else {}
            cur_before = json.dumps(full_state(conn), ensure_ascii=False)
            conn.execute("DELETE FROM records")
            for t in TABLES:
                for i, rec in enumerate(snapshot.get(t) or []):
                    rec = dict(rec)
                    rrid = rec.pop("id", None) or i + 1
                    conn.execute("INSERT OR REPLACE INTO records VALUES(?,?,?)",
                                 (rrid, t, json.dumps(rec, ensure_ascii=False)))
            rev = bump_rev(conn)
            cur_after = json.dumps(full_state(conn), ensure_ascii=False)
            log_history(conn, "*", None, "revert", cur_before, cur_after, user, rev)
        else:
            cur_row = conn.execute("SELECT data FROM records WHERE tbl=? AND id=?", (tbl, rid)).fetchone()
            cur_data = cur_row[0] if cur_row else None
            if before is None:
                conn.execute("DELETE FROM records WHERE tbl=? AND id=?", (tbl, rid))
                new_after = None
            else:
                conn.execute("INSERT OR REPLACE INTO records VALUES(?,?,?)", (rid, tbl, before))
                new_after = before
            rev = bump_rev(conn)
            log_history(conn, tbl, rid, "revert", cur_data, new_after, user, rev)
        conn.commit()
        self._send(200, full_state(conn))

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


if __name__ == "__main__":
    load_projects()
    load_sessions()
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Ledger server on 0.0.0.0:{PORT}, data={DATA_DIR}")
    httpd.serve_forever()
