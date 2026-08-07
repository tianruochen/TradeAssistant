"""用户存储:注册/登录/会话(sqlite,全局 data/users.db)。自带Key SaaS——用户存自己的 LLM Key。

密码用 pbkdf2 加盐哈希(stdlib,无三方依赖)。会话用随机 token,存库,重启不失效。
新用户注册时初始化其数据目录(拷贝策略模板 + 空持仓/观察池/计划/告警)。
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_DB = ROOT / "data" / "users.db"


def _conn():
    _DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(_DB))
    c.execute("""CREATE TABLE IF NOT EXISTS users(
        uid TEXT PRIMARY KEY, username TEXT UNIQUE, pw_hash TEXT, salt TEXT,
        llm_key TEXT DEFAULT '', model TEXT DEFAULT '', created REAL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS sessions(
        token TEXT PRIMARY KEY, uid TEXT, created REAL)""")
    return c


def _hash(pw: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000).hex()


def _seed_user_dir(uid: str) -> None:
    """初始化用户数据目录:拷贝交易策略模板 + 建空持仓/观察池/计划/告警。"""
    udir = ROOT / "data" / "users" / uid
    udir.mkdir(parents=True, exist_ok=True)
    # 策略模板从全局 data/ 拷(作为出厂默认,用户可自行改);无真实文件则用示例模板
    tpl = ROOT / "data" / "交易策略.md"
    if not tpl.exists():
        tpl = ROOT / "data" / "交易策略.example.md"
    if tpl.exists() and not (udir / "交易策略.md").exists():
        (udir / "交易策略.md").write_text(tpl.read_text(encoding="utf-8"), encoding="utf-8")
    for name, content in [
        ("holdings.md", "# 持仓全景 · 唯一权威事实源\n\n> 数据来源：用户官方。回答持仓前必先读本文件。\n\n## 当前持仓\n（暂无，请发送你的持仓，或告诉我买卖操作）\n\n## 组合汇总\n| 指标 | 数值 |\n|:--|:--|\n| 初始本金 | 待设置 |\n| 当前总资产 | 待设置 |\n"),
        ("watchlist.md", "# 观察池 · Watchlist\n\n## 观察中\n（暂无）\n"),
        ("plan.md", "# 作战计划 · plan.md\n\n## OKR\n（请设置你的目标，如：初始本金 X → 目标 Y）\n\n## 循环记录\n"),
        ("alerts.md", "# 价格告警 · alerts.md\n> 格式：`代码 名称 类型 价格 说明`（类型 stop/buy/break）\n"),
    ]:
        p = udir / name
        if not p.exists():
            p.write_text(content, encoding="utf-8")


def register(username: str, password: str) -> tuple[bool, str]:
    username = (username or "").strip()
    if len(username) < 2 or len(password or "") < 6:
        return False, "用户名≥2位、密码≥6位"
    salt = secrets.token_hex(8)
    uid = secrets.token_hex(8)
    c = _conn()
    try:
        c.execute("INSERT INTO users(uid,username,pw_hash,salt,created) VALUES(?,?,?,?,?)",
                  (uid, username, _hash(password, salt), salt, time.time()))
        c.commit()
    except sqlite3.IntegrityError:
        return False, "用户名已存在"
    finally:
        c.close()
    _seed_user_dir(uid)
    return True, uid


def authenticate(username: str, password: str) -> str | None:
    c = _conn()
    try:
        row = c.execute("SELECT uid,pw_hash,salt FROM users WHERE username=?", (username,)).fetchone()
    finally:
        c.close()
    if not row:
        return None
    uid, pw_hash, salt = row
    return uid if secrets.compare_digest(pw_hash, _hash(password, salt)) else None


def create_session(uid: str) -> str:
    token = secrets.token_urlsafe(24)
    c = _conn()
    c.execute("INSERT INTO sessions(token,uid,created) VALUES(?,?,?)", (token, uid, time.time()))
    c.commit(); c.close()
    return token


def uid_for_token(token: str) -> str | None:
    if not token:
        return None
    c = _conn()
    try:
        row = c.execute("SELECT uid FROM sessions WHERE token=?", (token,)).fetchone()
    finally:
        c.close()
    return row[0] if row else None


def get_user(uid: str) -> dict | None:
    c = _conn()
    try:
        row = c.execute("SELECT uid,username,llm_key,model FROM users WHERE uid=?", (uid,)).fetchone()
    finally:
        c.close()
    if not row:
        return None
    return {"uid": row[0], "username": row[1], "llm_key": row[2] or "", "model": row[3] or ""}


def set_user_key(uid: str, llm_key: str, model: str = "") -> None:
    c = _conn()
    c.execute("UPDATE users SET llm_key=?, model=? WHERE uid=?", (llm_key, model, uid))
    c.commit(); c.close()
