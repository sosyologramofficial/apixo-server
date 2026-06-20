import random
import time
import datetime
import requests
import string
import re
import hashlib
import os
import secrets
import tempfile
import threading
import psycopg2
import uuid as _uuid
from functools import wraps
from flask import (
    Flask, render_template, render_template_string,
    request, jsonify, session, redirect, url_for
)

# ════════════════════════════════════════════════════════════════════════════
#  VERİTABANI VE LOG SİSTEMİ
# ════════════════════════════════════════════════════════════════════════════

DB_CONN_STR = "postgresql://neondb_owner:npg_JtUVxc7vWjw0@ep-rapid-voice-ahentndm-pooler.c-3.us-east-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require"

def get_db_conn():
    return psycopg2.connect(DB_CONN_STR)

def init_db():
    conn = get_db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS accounts (
                        id SERIAL PRIMARY KEY,
                        email VARCHAR(255) UNIQUE NOT NULL,
                        is_used BOOLEAN DEFAULT FALSE
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS logs (
                        id SERIAL PRIMARY KEY,
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        message TEXT NOT NULL
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS jobs (
                        job_id VARCHAR(255) PRIMARY KEY,
                        status VARCHAR(50) NOT NULL,
                        task_type VARCHAR(50) NOT NULL,
                        model VARCHAR(100) NOT NULL,
                        mode VARCHAR(100) NOT NULL,
                        prompt TEXT,
                        created_at DOUBLE PRECISION NOT NULL,
                        logs TEXT[] DEFAULT '{}',
                        result_urls TEXT[] DEFAULT '{}'
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS prompts (
                        id VARCHAR(255) PRIMARY KEY,
                        title VARCHAR(255),
                        text TEXT NOT NULL,
                        timestamp BIGINT NOT NULL
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS gallery (
                        id VARCHAR(255) PRIMARY KEY,
                        prompt TEXT NOT NULL,
                        model VARCHAR(100),
                        task_type VARCHAR(50),
                        url TEXT NOT NULL,
                        timestamp BIGINT NOT NULL
                    );
                """)
    finally:
        conn.close()

def write_log(message):
    """Veritabanındaki logs tablosuna yazar ve konsola basar."""
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] {message}"
    print(line)
    try:
        conn = get_db_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO logs (message) VALUES (%s)", (line,))
        conn.close()
    except Exception as e:
        print(f"[-] Log yazma hatası: {e}")

def write_log_separator(title, details=None):
    """Veritabanındaki logs tablosuna ayraçlı blok yazar ve konsola basar."""
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    lines = []
    lines.append("=" * 50)
    lines.append(f"[{ts}] {title}")
    if details:
        for k, v in details.items():
            lines.append(f"  {k}: {v}")
    lines.append("=" * 50)
    block = "\n".join(lines)
    print(block)
    try:
        conn = get_db_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO logs (message) VALUES (%s)", (block,))
        conn.close()
    except Exception as e:
        print(f"[-] Log ayraç yazma hatası: {e}")

# ════════════════════════════════════════════════════════════════════════════
#  ACCOUNTS SİSTEMİ (PostgreSQL)
# ════════════════════════════════════════════════════════════════════════════

_in_progress_accounts = set()
_in_progress_lock = threading.Lock()

def read_accounts():
    """Veritabanındaki tüm hesapları okur (email:X veya email formatında döner)."""
    conn = get_db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT email, is_used FROM accounts ORDER BY id ASC")
                rows = cur.fetchall()
                return [f"{r[0]}:X" if r[1] else r[0] for r in rows]
    except Exception as e:
        write_log(f"read_accounts veritabanı hatası: {e}")
        return []
    finally:
        conn.close()

def get_available_accounts():
    """Kullanılmamış hesapları döndürür."""
    conn = get_db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT email FROM accounts WHERE is_used = FALSE ORDER BY id ASC")
                rows = cur.fetchall()
                return [r[0] for r in rows]
    except Exception as e:
        write_log(f"get_available_accounts veritabanı hatası: {e}")
        return []
    finally:
        conn.close()

def get_used_accounts():
    """Kullanılmış hesapları döndürür."""
    conn = get_db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT email FROM accounts WHERE is_used = TRUE ORDER BY id ASC")
                rows = cur.fetchall()
                return [r[0] for r in rows]
    except Exception as e:
        write_log(f"get_used_accounts veritabanı hatası: {e}")
        return []
    finally:
        conn.close()

def get_available_count():
    """Kullanılabilir hesap sayısını döndürür."""
    return len(get_available_accounts())

def mark_account_used(email):
    """Başarılı üretim sonrası hesabı veritabanında kullanıldı (is_used=TRUE) olarak işaretler."""
    conn = get_db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE accounts SET is_used = TRUE WHERE email = %s", (email,))
        write_log(f"Hesap kullanıldı olarak işaretlendi (DB): {email}:X")
    except Exception as e:
        write_log(f"mark_account_used veritabanı hatası: {e}")
    finally:
        conn.close()

# ════════════════════════════════════════════════════════════════════════════
#  MODEL TANIMLAMALARI (sadece video)
# ════════════════════════════════════════════════════════════════════════════

MODEL_CONFIGS = {
    "wan-2-5-video": {
        "display_name": "Wan 2.5 Video",
        "type": "video",
        "modes": {
            "text-to-video": {
                "display_name": "Text → Video",
                "default_params": {
                    "request_type": "async", "mode": "text-to-video",
                    "prompt": "", "resolution": "480p", "duration": 5,
                    "aspect_ratio": "16:9", "watermark": False,
                    "enable_prompt_expansion": False, "negative_prompt": "",
                    "seed": "", "image_urls": [], "audio_urls": [],
                },
                "cost_table": {
                    "480p":  {5: 0.2, 10: 0.4},
                    "720p":  {5: 0.4, 10: 0.8},
                    "1080p": {5: 0.7, 10: 1.4},
                },
                "supported_resolutions": ["480p", "720p", "1080p"],
                "supported_durations":   [5, 10],
                "supported_ratios":      ["16:9", "9:16", "1:1", "4:3", "3:4"],
                "needs_image": False,
                "supports_audio": True,
            },
            "image-to-video": {
                "display_name": "Image → Video",
                "default_params": {
                    "request_type": "async", "mode": "image-to-video",
                    "prompt": "", "resolution": "480p", "duration": 5,
                    "aspect_ratio": "16:9", "watermark": False,
                    "enable_prompt_expansion": False, "negative_prompt": "",
                    "seed": "", "image_urls": [], "audio_urls": [],
                },
                "cost_table": {
                    "480p":  {5: 0.2, 10: 0.4},
                    "720p":  {5: 0.4, 10: 0.8},
                    "1080p": {5: 0.7, 10: 1.4},
                },
                "supported_resolutions": ["480p", "720p", "1080p"],
                "supported_durations":   [5, 10],
                "supported_ratios":      ["16:9", "9:16", "1:1", "4:3", "3:4"],
                "needs_image": True,
                "max_ref_images": 1,
                "supports_audio": True,
            },
        },
    },
    "veo-3-1": {
        "display_name": "Veo 3.1",
        "type": "video",
        "modes": {
            "text2vid": {
                "display_name": "Text → Video",
                "api_mode": "fast",
                "generation_type": "TEXT_2_VIDEO",
                "default_params": {
                    "request_type": "async", "mode": "fast",
                    "prompt": "", "generationType": "TEXT_2_VIDEO",
                    "duration": 8, "resolution": "1080p",
                    "aspect_ratio": "16:9", "watermark": "",
                    "seed": "", "image_urls": [],
                },
                "cost_table": {
                    "720p":  {4: 0.375, 6: 0.375, 8: 0.375},
                    "1080p": {4: 0.375, 6: 0.375, 8: 0.375},
                    "4k":    {4: 0.375, 6: 0.375, 8: 0.375},
                },
                "supported_resolutions": ["720p", "1080p", "4k"],
                "supported_durations":   [4, 6, 8],
                "supported_ratios":      ["16:9", "9:16", "auto"],
                "needs_image": False,
                "supports_audio": False,
            },
            "start_end_frame": {
                "display_name": "Start / End Frame",
                "api_mode": "fast",
                "generation_type": "FIRST_AND_LAST_FRAMES_2_VIDEO",
                "default_params": {
                    "request_type": "async", "mode": "fast",
                    "prompt": "", "generationType": "FIRST_AND_LAST_FRAMES_2_VIDEO",
                    "duration": 8, "resolution": "1080p",
                    "aspect_ratio": "16:9", "watermark": "",
                    "seed": "", "image_urls": [],
                },
                "cost_table": {
                    "720p":  {4: 0.375, 6: 0.375, 8: 0.375},
                    "1080p": {4: 0.375, 6: 0.375, 8: 0.375},
                    "4k":    {4: 0.375, 6: 0.375, 8: 0.375},
                },
                "supported_resolutions": ["720p", "1080p", "4k"],
                "supported_durations":   [4, 6, 8],
                "supported_ratios":      ["16:9", "9:16", "auto"],
                "needs_image": True,
                "allow_single_image": True,
                "max_ref_images": 2,
                "supports_audio": False,
            },
            "reference": {
                "display_name": "Reference → Video",
                "api_mode": "fast",
                "generation_type": "REFERENCE_2_VIDEO",
                "default_params": {
                    "request_type": "async", "mode": "fast",
                    "prompt": "", "generationType": "REFERENCE_2_VIDEO",
                    "duration": 8, "resolution": "1080p",
                    "aspect_ratio": "16:9", "watermark": "",
                    "seed": "", "image_urls": [],
                },
                "cost_table": {
                    "720p":  {4: 0.375, 6: 0.375, 8: 0.375},
                    "1080p": {4: 0.375, 6: 0.375, 8: 0.375},
                    "4k":    {4: 0.375, 6: 0.375, 8: 0.375},
                },
                "supported_resolutions": ["720p", "1080p", "4k"],
                "supported_durations":   [4, 6, 8],
                "supported_ratios":      ["16:9", "9:16", "auto"],
                "needs_image": True,
                "max_ref_images": 3,
                "supports_audio": False,
            },
        },
    },
    "grok-video": {
        "display_name": "Grok Video",
        "type": "video",
        "modes": {
            "txt2vid": {
                "display_name": "TXT → Video",
                "api_mode": "text-to-video",
                "default_params": {
                    "request_type": "async",
                    "mode": "text-to-video",
                    "prompt": "",
                    "duration": 14,
                    "resolution": "720p",
                    "aspect_ratio": "16:9",
                    "style": "normal",
                    "image_urls": [],
                },
                "estimated_credits": 0.42,
                "supported_resolutions": ["480p", "720p"],
                "supported_durations": list(range(6, 31)),
                "supported_ratios": ["1:1", "3:2", "2:3", "16:9", "9:16"],
                "needs_image": False,
                "supports_audio": False,
            },
            "img2vid": {
                "display_name": "IMG → Video",
                "api_mode": "image-to-video",
                "default_params": {
                    "request_type": "async",
                    "mode": "image-to-video",
                    "prompt": "",
                    "duration": 15,
                    "resolution": "720p",
                    "aspect_ratio": "16:9",
                    "style": "normal",
                    "image_urls": [],
                },
                "estimated_credits": 0.45,
                "supported_resolutions": ["480p", "720p"],
                "supported_durations": list(range(6, 31)),
                "supported_ratios": ["1:1", "3:2", "2:3", "16:9", "9:16"],
                "needs_image": True,
                "max_ref_images": 1,
                "supports_audio": False,
            },
        },
    },
}

# ════════════════════════════════════════════════════════════════════════════
#  PROXY SİSTEMİ KALDIRILDI
# ════════════════════════════════════════════════════════════════════════════

# ════════════════════════════════════════════════════════════════════════════
#  SPAMOK + OTP
# ════════════════════════════════════════════════════════════════════════════

# ════════════════════════════════════════════════════════════════════════════
#  SPAMOK + OTP
# ════════════════════════════════════════════════════════════════════════════

class ApixoTemp:
    def random_email(self, length=15) -> str:
        return ''.join(
            random.SystemRandom().choice(string.ascii_lowercase + string.digits)
            for _ in range(length)
        ) + '@spamok.com'

    def generate_fingerprint(self, email: str) -> str:
        raw = f"{email}{time.time()}"
        return hashlib.md5(raw.encode()).hexdigest()

    def get_existing_mail_ids(self, email: str) -> set:
        address = email.replace('@spamok.com', '')
        try:
            r = requests.get(f'https://api.spamok.com/v2/EmailBox/{address}', timeout=10)
            return {mail['id'] for mail in r.json().get('mails', [])}
        except Exception:
            return set()

    def get_otp(self, email: str, existing_ids: set = None, timeout=60) -> str | None:
        address = email.replace('@spamok.com', '')
        if existing_ids is None:
            existing_ids = set()
        for i in range(timeout):
            try:
                r = requests.get(f'https://api.spamok.com/v2/EmailBox/{address}', timeout=10)
                data = r.json()
                for mail in data.get('mails', []):
                    mail_id = mail['id']
                    if mail_id in existing_ids:
                        continue
                    subject = mail.get('subject', '')
                    from_display = mail.get('fromDisplay', '')
                    if 'APIXO' in from_display or 'verification' in subject.lower():
                        email_r = requests.get(
                            f'https://api.spamok.com/v2/Email/{address}/{mail_id}', timeout=10
                        )
                        body = email_r.json()
                        plain = body.get('messagePlain', '')
                        match = re.search(r'\b(\d{6})\b', plain)
                        if match: return match.group(1)
                        html = body.get('messageHtml', '')
                        match = re.search(r'letter-spacing:8px[^>]*>(\d{6})<', html)
                        if match: return match.group(1)
            except Exception:
                pass
            time.sleep(2)
        return None

_active_sessions = {}
_sessions_lock = threading.Lock()

def get_session_for_email(email):
    with _sessions_lock:
        return _active_sessions.get(email)

def set_session_for_email(email, session):
    with _sessions_lock:
        _active_sessions[email] = session

def apixo_auto_login_with_email(email):
    """Verilen e-posta ile giriş yapar (OTP tabanlı)."""
    # Check session cache
    cached_sess = get_session_for_email(email)
    if cached_sess:
        try:
            r = cached_sess.get("https://apixo.ai/api/auth/session", timeout=5)
            if r.status_code == 200 and r.json().get("user"):
                write_log(f"Aktif session cache'ten başarıyla alındı (doğrulandı) — {email}")
                return cached_sess, r.json(), None
            else:
                write_log(f"Cached session geçersiz, yeniden login olunuyor — {email}")
        except Exception as e:
            write_log(f"Cached session doğrulama hatası ({e}), yeniden login olunuyor — {email}")
    temp = ApixoTemp()
    existing_ids = temp.get_existing_mail_ids(email)
    fingerprint = temp.generate_fingerprint(email)
    base_url = "https://apixo.ai"

    write_log(f"Login başlatılıyor: {email}")

    s = requests.Session()
    s.headers.update({
        "Origin": base_url,
        "Referer": f"{base_url}/models/image",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
        "sec-ch-ua": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    })

    # OTP GÖNDERİMİ İÇİN TEKRAR DENEME MEKANİZMASI (PROXY OLMADAN DIRECT BAĞLANTI)
    max_retries = 3
    otp_sent_successfully = False

    for attempt in range(max_retries):
        write_log(f"OTP gönderim denemesi {attempt + 1}/{max_retries} — {email}")

        try:
            r1 = s.post(
                f"{base_url}/api/auth/otp/send",
                json={"email": email, "fingerprint": fingerprint},
                timeout=15
            )

            if r1.json().get("success"):
                write_log(f"OTP başarıyla gönderildi — {email}")
                otp_sent_successfully = True
                break
            else:
                write_log(f"OTP gönderilemedi (API hatası): {r1.text}")

        except Exception as e:
            write_log(f"OTP istek hatası: {e}")

        time.sleep(1)

    if not otp_sent_successfully:
        msg = "OTP gönderilemedi."
        write_log(f"HATA: {msg} — {email}")
        return None, None, msg

    # OTP Bekle
    write_log(f"OTP kodu bekleniyor... — {email}")
    otp = temp.get_otp(email, existing_ids=existing_ids)
    if not otp:
        write_log(f"HATA: OTP timeout — {email}")
        return None, None, "OTP timeout."
    write_log(f"OTP yakalandı: {otp} — {email}")

    # OTP Doğrula
    r2 = s.post(f"{base_url}/api/auth/otp/verify", json={"email": email, "otp": otp})
    d2 = r2.json()
    if not d2.get("success"):
        write_log(f"HATA: OTP doğrulanamadı — {email}")
        return None, None, "OTP doğrulanamadı."
    temp_token = d2["tempToken"]
    write_log(f"OTP doğrulandı — {email}")

    # CSRF Al
    r3 = s.get(f"{base_url}/api/auth/csrf")
    csrf_token = r3.json()["csrfToken"]

    # Callback (Kayıt tamamlama)
    try:
        s.post(
            f"{base_url}/api/auth/callback/email-otp",
            headers={**dict(s.headers),
                     "Content-Type": "application/x-www-form-urlencoded",
                     "x-auth-return-redirect": "1"},
            data={
                "email": email, "token": temp_token,
                "callbackUrl": f"{base_url}/models/image",
                "redirect": "false", "csrfToken": csrf_token,
            },
            allow_redirects=False,
            timeout=15
        )
    except Exception as e:
        write_log(f"HATA: Callback hatası — {email}: {e}")
        return None, None, f"Callback isteği sırasında hata: {e}"

    # Session Al
    r5 = s.get(f"{base_url}/api/auth/session")
    write_log(f"Session alındı, login başarılı — {email}")
    set_session_for_email(email, s)
    return s, r5.json(), None

# ════════════════════════════════════════════════════════════════════════════
#  UPLOAD
# ════════════════════════════════════════════════════════════════════════════

MIME_TYPES = {
    "jpg":  "image/jpeg", "jpeg": "image/jpeg",
    "png":  "image/png",  "webp": "image/webp",
    "mp3":  "audio/mpeg", "wav":  "audio/wav", "m4a": "audio/mp4",
}

def upload_file(sess: requests.Session, file_path: str) -> str:
    file_name = os.path.basename(file_path)
    file_size = os.path.getsize(file_path)
    ext = file_name.rsplit(".", 1)[-1].lower()
    file_type = MIME_TYPES.get(ext, "application/octet-stream")
    name_hash = hashlib.md5(file_name.encode()).hexdigest()
    upload_name = f"{name_hash}-{int(time.time() * 1000)}.{ext}"

    r = sess.post("https://apixo.ai/api/upload-presigned-url", json={
        "fileName": upload_name, "fileType": file_type, "fileSize": file_size
    })
    if r.status_code != 200:
        raise Exception(f"Presigned URL alınamadı: {r.text}")
    presigned = r.json()

    with open(file_path, "rb") as f:
        file_data = f.read()

    r2 = requests.put(
        presigned["uploadUrl"], data=file_data,
        headers={
            "Content-Type":  presigned["contentType"],
            "Cache-Control": presigned["cacheControl"],
            "Origin":  "https://apixo.ai",
            "Referer": "https://apixo.ai/",
        },
        timeout=60
    )
    if r2.status_code != 200:
        raise Exception(f"R2 upload başarısız: {r2.text}")
    return presigned["publicUrl"]

# ════════════════════════════════════════════════════════════════════════════
#  GENERATE (sadece video)
# ════════════════════════════════════════════════════════════════════════════

def generate_video(sess, prompt, mode, resolution, duration, aspect_ratio,
                   enable_prompt_expansion, image_url=None, image_urls=None,
                   negative_prompt="", seed="", audio_url=None, model="wan-2-5-video",
                   watermark=False, style=None):
    mode_cfg = MODEL_CONFIGS[model]["modes"][mode]
    params = dict(mode_cfg["default_params"])
    provided_image_urls = image_urls if image_urls is not None else ([image_url] if image_url else [])
    params.update({
        "mode": mode_cfg.get("api_mode", mode),
        "prompt": prompt,
        "resolution": resolution,
        "duration": duration,
        "aspect_ratio": aspect_ratio,
        "enable_prompt_expansion": enable_prompt_expansion,
        "negative_prompt": negative_prompt,
        "seed": seed,
        "watermark": watermark,
        "image_urls": provided_image_urls,
    })
    if mode_cfg.get("supports_audio"):
        params["audio_urls"] = [audio_url] if audio_url else []
    default_style = mode_cfg.get("default_params", {}).get("style")
    if style is None:
        style = default_style
    if style is not None:
        params["style"] = style
    if mode_cfg.get("generation_type"):
        params["generationType"] = mode_cfg["generation_type"]
    cost = mode_cfg.get("estimated_credits")
    if cost is None:
        cost = mode_cfg["cost_table"].get(resolution, {}).get(duration, 0.4)
    r = sess.post(
        f"https://apixo.ai/api/playground/models/{model}/generate",
        json={"model": model, "parameters": params, "estimatedCredits": cost}
    )
    j = r.json()
    if not j.get("success"):
        raise Exception(f"Üretim başlatılamadı: {r.text}")
    return j["taskId"]

# ════════════════════════════════════════════════════════════════════════════
#  FLASK APP
# ════════════════════════════════════════════════════════════════════════════

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)
APP_PASSWORD = "123"

# ── Storage (Tamamen veritabanına taşındı) ──────────────────────────────────
import uuid as _uuid

# ── Upload Session (Önbellekten active email'i kullanır) ───────────────────
def get_or_create_upload_session():
    """Upload işlemleri için session döndürür (önbellek kontrollü)."""
    accounts = get_available_accounts()
    if not accounts:
        all_accts = read_accounts()
        accounts = [a.replace(":X", "") for a in all_accts if a.strip()]
    if not accounts:
        return None
    email = accounts[0]
    s, u, err = apixo_auto_login_with_email(email)
    if s:
        return s
    return None


def _run_job(job_id, data):
    """Background worker: hesap seç → login → generate → poll, update jobs_store."""
    def log(msg):
        try:
            conn = get_db_conn()
            with conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE jobs 
                        SET logs = array_append(logs, %s) 
                        WHERE job_id = %s
                    """, (msg, job_id))
            conn.close()
        except Exception as e:
            print(f"[-] Job log append hatası: {e}")

    def update(upd):
        if not upd:
            return
        valid_columns = {'status', 'result_urls'}
        fields = []
        params = []
        for k, v in upd.items():
            if k in valid_columns:
                fields.append(f"{k} = %s")
                params.append(v)
        if not fields:
            return
        params.append(job_id)
        query = f"UPDATE jobs SET {', '.join(fields)} WHERE job_id = %s"
        try:
            conn = get_db_conn()
            with conn:
                with conn.cursor() as cur:
                    cur.execute(query, params)
            conn.close()
        except Exception as e:
            print(f"[-] Job update hatası: {e}")

    email = None
    try:
        # Hesap seç (in-progress olanlar hariç)
        with _in_progress_lock:
            available = [a for a in get_available_accounts() if a not in _in_progress_accounts]
            if not available:
                update({'status': 'error'})
                log('Kullanılabilir hesap yok!')
                write_log(f"HATA: Kullanılabilir hesap yok (tümü kullanımda) - Job: {job_id}")
                return
            email = available[0]
            _in_progress_accounts.add(email)

        prompt_text = data.get('prompt', '')[:80]
        model = data.get('model', 'wan-2-5-video')
        mode = data.get('mode', 'text-to-video')

        write_log_separator("YENİ ÜRETİM BAŞLADI", {
            "Job ID": job_id,
            "Hesap": email,
            "Prompt": prompt_text,
            "Model": f"{model} | Mod: {mode}",
            "Çözünürlük": data.get('resolution', '?'),
            "Süre": f"{data.get('duration', '?')}s",
        })

        update({'status': 'logging_in'})
        log(f'Hesap ile giriş yapılıyor: {email}')
        write_log(f"Hesap ile login yapılıyor: {email}")

        s, u, err = apixo_auto_login_with_email(email)
        if not s:
            update({'status': 'error'})
            log(f'Login hatası: {err}')
            write_log(f"HATA: Login başarısız — {email}: {err}")
            return

        write_log(f"Login başarılı — {email}")
        log('Giriş başarılı, üretim başlatılıyor...')

        update({'status': 'generating'})

        task_id = generate_video(
            s,
            prompt=data.get('prompt', ''),
            mode=mode,
            resolution=data.get('resolution', '480p'),
            duration=int(data.get('duration', 5)),
            aspect_ratio=data.get('aspect_ratio', '16:9'),
            enable_prompt_expansion=bool(data.get('enable_prompt_expansion', False)),
            image_url=data.get('image_url') or None,
            image_urls=data.get('image_urls') or None,
            negative_prompt=data.get('negative_prompt', ''),
            seed=data.get('seed', ''),
            audio_url=data.get('audio_url') or None,
            model=model,
            watermark=bool(data.get('watermark', False)),
            style=data.get('style'),
        )

        log(f'Task ID: {task_id}')
        write_log(f"Üretim başlatıldı — Task ID: {task_id} — Hesap: {email}")
        update({'status': 'polling', 'apixo_task_id': task_id})

        for i in range(150):
            time.sleep(4)
            try:
                r = s.get(
                    f'https://apixo.ai/api/playground/models/{model}/status',
                    params={'taskId': task_id}
                )
                d = r.json()
                st = d.get('state')
                elapsed = (i + 1) * 4
                log(f'State: {st} ({elapsed}s)')
                write_log(f"Polling: state={st} ({elapsed}s) — Task: {task_id}")

                if st == 'success':
                    outputs = d.get('resultUrls') or ([d.get('resultUrl')] if d.get('resultUrl') else [])
                    outputs = [u for u in outputs if u]
                    update({'status': 'done', 'result_urls': outputs})
                    log(f'Tamamlandı! {len(outputs)} çıktı')
                    write_log(f"✅ BAŞARILI — {len(outputs)} çıktı — Task: {task_id}")

                    # Başarılı → hesabı :X ile işaretle
                    mark_account_used(email)
                    write_log(f"Hesap :X ile işaretlendi — {email}")
                    return

                elif st == 'failed':
                    update({'status': 'error'})
                    err_msg = d.get("error", "bilinmeyen")
                    log(f'Başarısız: {err_msg}')
                    write_log(f"❌ BAŞARISIZ — {err_msg} — Task: {task_id} — Hesap: {email} (işaretlenmedi)")
                    return

            except Exception as e:
                log(f'Poll hatası: {e}')
                write_log(f"Poll hatası: {e} — Task: {task_id}")

        update({'status': 'error'})
        log('Zaman aşımı (10 dk)')
        write_log(f"❌ ZAMAN AŞIMI — Task: {task_id} — Hesap: {email} (işaretlenmedi)")

    except Exception as e:
        update({'status': 'error'})
        log(f'Hata: {e}')
        write_log(f"❌ GENEL HATA — {e} — Job: {job_id} — Hesap: {email or 'yok'}")
    finally:
        if email:
            with _in_progress_lock:
                _in_progress_accounts.discard(email)


def get_sid():
    if 'sid' not in session:
        session['sid'] = secrets.token_hex(16)
    return session['sid']


def require_app_login(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not session.get('logged_in'):
            if request.path.startswith('/api/'):
                return jsonify({"error": "Yetkisiz"}), 401
            return redirect(url_for('login'))
        return f(*a, **kw)
    return wrapper


# ─── Login sayfası (inline) ─────────────────────────────────────────────────

LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="tr"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI STUDIO · Giriş</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Space+Grotesk:wght@600;700&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{
  font-family:'Inter',sans-serif;background:#06070d;color:#e8eaf0;
  display:flex;align-items:center;justify-content:center;
  min-height:100vh;overflow:hidden;position:relative;
}
body::before{
  content:'';position:fixed;inset:0;pointer-events:none;
  background:
    radial-gradient(ellipse at 20% 10%, rgba(124,92,255,.30) 0%, transparent 50%),
    radial-gradient(ellipse at 80% 90%, rgba(0,212,255,.20) 0%, transparent 50%),
    radial-gradient(ellipse at 50% 50%, rgba(255,92,242,.08) 0%, transparent 70%);
}
body::after{
  content:'';position:fixed;inset:0;pointer-events:none;opacity:.04;
  background-image:linear-gradient(rgba(255,255,255,.5) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.5) 1px,transparent 1px);
  background-size:42px 42px;
}
.card{
  position:relative;z-index:1;width:min(420px,92vw);padding:42px 36px;
  background:rgba(20,22,35,.55);backdrop-filter:blur(20px);
  border:1px solid rgba(120,130,200,.18);border-radius:24px;
  box-shadow:0 30px 80px rgba(0,0,0,.5),inset 0 0 0 1px rgba(255,255,255,.03);
  animation:rise .6s cubic-bezier(.2,.8,.2,1);
}
@keyframes rise{from{opacity:0;transform:translateY(20px)}to{opacity:1;transform:none}}
.brand{
  font-family:'Space Grotesk',sans-serif;font-weight:700;font-size:28px;letter-spacing:.5px;
  background:linear-gradient(135deg,#7c5cff 0%,#00d4ff 60%,#ff5cf2 100%);
  -webkit-background-clip:text;background-clip:text;color:transparent;
  margin-bottom:6px;
}
.sub{color:#8a8fa3;font-size:14px;margin-bottom:28px}
label{display:block;font-size:12px;letter-spacing:.6px;color:#8a8fa3;text-transform:uppercase;margin-bottom:8px}
input[type=password]{
  width:100%;padding:14px 16px;font-size:15px;font-family:inherit;
  background:rgba(10,11,18,.7);border:1px solid rgba(120,130,200,.2);
  border-radius:12px;color:#e8eaf0;outline:none;transition:.2s;
}
input[type=password]:focus{border-color:#7c5cff;box-shadow:0 0 0 4px rgba(124,92,255,.15)}
button{
  width:100%;margin-top:18px;padding:14px;font-size:15px;font-weight:600;font-family:inherit;
  background:linear-gradient(135deg,#7c5cff 0%,#00d4ff 100%);
  color:#fff;border:none;border-radius:12px;cursor:pointer;letter-spacing:.3px;
  transition:.2s;
}
button:hover{transform:translateY(-1px);box-shadow:0 10px 30px rgba(124,92,255,.4)}
.error{
  margin-top:14px;padding:10px 14px;background:rgba(255,85,119,.12);
  border:1px solid rgba(255,85,119,.3);border-radius:10px;
  color:#ff8aa3;font-size:13px;
}
.dot{display:inline-block;width:6px;height:6px;border-radius:50%;background:#7c5cff;margin-right:6px;animation:pulse 1.8s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
</style></head><body>
<div class="card">
  <div class="brand">AI STUDIO</div>
  <div class="sub"><span class="dot"></span>Cinematic AI Studio · Erişim için şifre gerekli</div>
  <form method="POST" action="/login">
    <label for="pwd">Şifre</label>
    <input id="pwd" type="password" name="password" autofocus required placeholder="••••••">
    <button type="submit">Giriş Yap</button>
    {% if error %}<div class="error">{{ error }}</div>{% endif %}
  </form>
</div>
</body></html>"""


@app.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('logged_in') and request.method == 'GET':
        return redirect(url_for('index'))
    error = None
    if request.method == 'POST':
        pwd = request.form.get('password', '')
        if pwd == APP_PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('index'))
        error = "Hatalı şifre."
    return render_template_string(LOGIN_HTML, error=error)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/')
@require_app_login
def index():
    return render_template('index.html')


# ─── API ────────────────────────────────────────────────────────────────────

@app.route('/api/models')
@require_app_login
def api_models():
    return jsonify(MODEL_CONFIGS)


@app.route('/api/account-info')
@require_app_login
def api_account_info():
    total_avail = get_available_count()
    with _in_progress_lock:
        active_count = len(_in_progress_accounts)
    available = max(0, total_avail - active_count)
    all_accts = read_accounts()
    total = len(all_accts)
    used = total - total_avail
    return jsonify({'available': available, 'total': total, 'used': used})


@app.route('/api/upload', methods=['POST'])
@require_app_login
def api_upload():
    f = request.files.get('file')
    if not f:
        return jsonify({"error": "Dosya bulunamadı."}), 400
    suffix = '_' + os.path.basename(f.filename or 'file')
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    f.save(tmp.name)
    tmp.close()
    try:
        sess = get_or_create_upload_session()
        if not sess:
            return jsonify({"error": "Upload için oturum oluşturulamadı. accounts.txt kontrol edin."}), 500
        url = upload_file(sess, tmp.name)
        return jsonify({"url": url, "name": f.filename})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        try: os.unlink(tmp.name)
        except: pass


@app.route('/api/generate', methods=['POST'])
@require_app_login
def api_generate():
    data = request.get_json(force=True)
    task_type = data.get('task_type')
    if task_type != 'video':
        return jsonify({"error": "Sadece video üretimi destekleniyor."}), 400

    # Hesap kontrolü
    if get_available_count() <= 0:
        return jsonify({"error": "Kullanılabilir hesap yok. accounts.txt dosyasını kontrol edin."}), 400

    model = data.get('model', 'wan-2-5-video')
    return jsonify({"task_type": "video", "model": model})


@app.route('/api/task-status')
@require_app_login
def api_task_status():
    task_id = request.args.get('task_id')
    model   = request.args.get('model')
    if not task_id or not model:
        return jsonify({"error": "task_id ve model zorunlu."}), 400
    # Bu endpoint artık doğrudan kullanılmıyor (job system kullanılıyor)
    return jsonify({"error": "Bu endpoint kullanım dışı. Job system kullanın."}), 400


# ── JOB SYSTEM ─────────────────────────────────────────────────────────────

@app.route('/api/start-job', methods=['POST'])
@require_app_login
def api_start_job():
    data = request.get_json(force=True)

    # Hesap kontrolü
    if get_available_count() <= 0:
        return jsonify({"error": "Kullanılabilir hesap yok. Lütfen sisteme hesap yükleyin."}), 400

    job_id = str(_uuid.uuid4())
    
    conn = get_db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO jobs (job_id, status, task_type, model, mode, prompt, created_at, logs, result_urls)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    job_id, 'starting', data.get('task_type', 'video'),
                    data.get('model', ''), data.get('mode', ''),
                    data.get('prompt', ''), time.time(),
                    ['Hesap seçiliyor...'], []
                ))
    except Exception as e:
        return jsonify({"error": f"Görev veritabanına eklenemedi: {e}"}), 500
    finally:
        conn.close()

    t = threading.Thread(target=_run_job, args=(job_id, data), daemon=True)
    t.start()
    return jsonify({"job_id": job_id})


@app.route('/api/job-status/<job_id>')
@require_app_login
def api_job_status(job_id):
    conn = get_db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status, logs, result_urls, task_type, model, mode, prompt, created_at FROM jobs WHERE job_id = %s", (job_id,))
                row = cur.fetchone()
                if row:
                    return jsonify({
                        'status': row[0],
                        'logs': row[1],
                        'result_urls': row[2],
                        'task_type': row[3],
                        'model': row[4],
                        'mode': row[5],
                        'prompt': row[6],
                        'created_at': row[7]
                    })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()
    return jsonify({"error": "Job bulunamadı"}), 404


@app.route('/api/jobs')
@require_app_login
def api_jobs():
    conn = get_db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT job_id, status, logs, result_urls, task_type, model, mode, prompt, created_at FROM jobs ORDER BY created_at DESC")
                rows = cur.fetchall()
                jobs_dict = {}
                for row in rows:
                    jobs_dict[row[0]] = {
                        'status': row[1],
                        'logs': row[2],
                        'result_urls': row[3],
                        'task_type': row[4],
                        'model': row[5],
                        'mode': row[6],
                        'prompt': row[7],
                        'created_at': row[8]
                    }
                return jsonify(jobs_dict)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route('/api/delete-job/<job_id>', methods=['DELETE'])
@require_app_login
def api_delete_job(job_id):
    conn = get_db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM jobs WHERE job_id = %s", (job_id,))
                return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


# ── PROMPT LIBRARY ─────────────────────────────────────────────────────────

@app.route('/api/prompts', methods=['GET'])
@require_app_login
def api_get_prompts():
    conn = get_db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, title, text, timestamp FROM prompts ORDER BY timestamp DESC")
                rows = cur.fetchall()
                prompts_list = []
                for row in rows:
                    prompts_list.append({
                        "id": row[0],
                        "title": row[1],
                        "text": row[2],
                        "timestamp": row[3]
                    })
                return jsonify(prompts_list)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route('/api/prompts', methods=['POST'])
@require_app_login
def api_save_prompt():
    data = request.get_json(force=True)
    text = (data.get('text') or '').strip()
    if not text:
        return jsonify({"error": "Prompt boş olamaz"}), 400
    pid = data.get('id') or str(_uuid.uuid4())
    title = (data.get('title') or '').strip()
    timestamp = int(time.time() * 1000)
    entry = {"id": pid, "title": title, "text": text, "timestamp": timestamp}
    
    conn = get_db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO prompts (id, title, text, timestamp)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE 
                    SET title = EXCLUDED.title, text = EXCLUDED.text, timestamp = EXCLUDED.timestamp
                """, (pid, title, text, timestamp))
        return jsonify(entry)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route('/api/prompts/<pid>', methods=['DELETE'])
@require_app_login
def api_delete_prompt(pid):
    conn = get_db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM prompts WHERE id = %s", (pid,))
                return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


# ── GALLERY ────────────────────────────────────────────────────────────────

@app.route('/api/gallery', methods=['GET'])
@require_app_login
def api_get_gallery():
    conn = get_db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, prompt, model, task_type, url, timestamp FROM gallery ORDER BY timestamp DESC LIMIT 200")
                rows = cur.fetchall()
                gallery_list = []
                for row in rows:
                    gallery_list.append({
                        "id": row[0],
                        "prompt": row[1],
                        "model": row[2],
                        "taskType": row[3],
                        "url": row[4],
                        "timestamp": row[5]
                    })
                return jsonify(gallery_list)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route('/api/gallery', methods=['POST'])
@require_app_login
def api_add_gallery():
    data = request.get_json(force=True)
    if not data:
        return jsonify({"error": "Veri eksik"}), 400
    
    conn = get_db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                # Delete existing item with same ID if any
                cur.execute("DELETE FROM gallery WHERE id = %s", (data.get('id'),))
                # Insert new item
                cur.execute("""
                    INSERT INTO gallery (id, prompt, model, task_type, url, timestamp)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (
                    data.get('id'), data.get('prompt'), data.get('model'),
                    data.get('taskType'), data.get('url'), data.get('timestamp')
                ))
                # Enforce limit of 200 items in gallery
                cur.execute("SELECT count(*) FROM gallery")
                count = cur.fetchone()[0]
                if count > 200:
                    # Delete oldest items beyond 200
                    cur.execute("""
                        DELETE FROM gallery 
                        WHERE id NOT IN (
                            SELECT id FROM gallery 
                            ORDER BY timestamp DESC 
                            LIMIT 200
                        )
                    """)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route('/api/gallery/<item_id>', methods=['DELETE'])
@require_app_login
def api_delete_gallery(item_id):
    conn = get_db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM gallery WHERE id = %s", (item_id,))
                return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route('/api/gallery/clear', methods=['DELETE'])
@require_app_login
def api_clear_gallery():
    conn = get_db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE TABLE gallery")
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


# ── MEDIA PROXY ────────────────────────────────────────────────────────────

@app.route('/api/proxy-media')
@require_app_login
def api_proxy_media():
    url = request.args.get('url', '')
    dl = request.args.get('dl', '0') == '1'
    if not url:
        return jsonify({"error": "URL gerekli"}), 400
    range_header = request.headers.get('Range', None)
    req_headers = {}
    if range_header:
        req_headers['Range'] = range_header
    try:
        from flask import Response
        resp = requests.get(url, headers=req_headers, stream=True, timeout=60)
        response_headers = {
            'Content-Type': resp.headers.get('content-type', 'application/octet-stream'),
            'Accept-Ranges': 'bytes',
        }
        if 'Content-Length' in resp.headers:
            response_headers['Content-Length'] = resp.headers['Content-Length']
        if 'Content-Range' in resp.headers:
            response_headers['Content-Range'] = resp.headers['Content-Range']
        if dl:
            ext = ""
            if url:
                path_part = url.split('?')[0].split('/')[-1]
                if '.' in path_part:
                    ext = "." + path_part.split('.')[-1]
            if not ext:
                content_type = resp.headers.get('content-type', '')
                if 'video' in content_type:
                    ext = '.mp4'
                elif 'image' in content_type:
                    ext = '.jpg'
                elif 'audio' in content_type:
                    ext = '.mp3'
            filename = f"ai_studio_media{ext}"
            response_headers['Content-Disposition'] = f'attachment; filename="{filename}"'

        def gen():
            for chunk in resp.iter_content(chunk_size=65536):
                yield chunk

        return Response(gen(), status=resp.status_code, headers=response_headers)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


init_db()

if __name__ == "__main__":
    os.makedirs("templates", exist_ok=True)
    write_log("Sunucu başlatılıyor...")
    app.run(host="0.0.0.0", port=5000, debug=True)
