# coding=utf-8
import base64, re, time, random, string, os, json, logging, functools, socket
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# 核心依赖
from curl_cffi import requests as crequests
import ddddocr
import urllib3
import cv2
import numpy as np

# 禁用 SSL 警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- 核心配置 ---
URLS_FILE = "urls.txt"
CACHE_FILE = "tg.cache"
SUB_FILE = "subscription.txt"
MAX_WORKERS = 100               
DEFAULT_TIMEOUT = 5             
MAIL_WAIT_TIMEOUT = 35          

# ==================== 日志逻辑 ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(CACHE_FILE, mode='a', encoding='utf-8')
    ]
)

def request_with_retry(max_tries=1, backoff=1.0):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            tries, delay = 0, backoff
            while tries < max_tries:
                try:
                    resp = func(*args, **kwargs)
                    if resp and resp.status_code not in (429, 500, 502, 503, 504):
                        return resp
                except Exception: pass
                time.sleep(delay + random.uniform(0.1, 0.5))
                tries += 1
                delay *= 1.5
            return None
        return wrapper
    return decorator

class RateLimiter:
    def __init__(self, max_per_sec):
        self.interval = 1.0 / max_per_sec
        self._last = {}
    def wait(self, host):
        now = time.time()
        last = self._last.get(host, 0)
        if now - last < self.interval:
            time.sleep(self.interval - (now - last))
        self._last[host] = time.time()

def preprocess_captcha(img_bytes):
    try:
        nparr = np.frombuffer(img_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        bin_img = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 11, 2)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
        clean = cv2.morphologyEx(bin_img, cv2.MORPH_OPEN, kernel)
        _, buf = cv2.imencode('.png', clean)
        return buf.tobytes()
    except: return img_bytes

class AirportCommander:
    def __init__(self):
        self.old_cache = self.parse_existing_cache()
        self.ocr = ddddocr.DdddOcr(show_ad=False)
        self.mail_api_list = ["mail.tm", "mail.gw"]
        self.current_mail_api = "mail.tm"
        self.limiter = RateLimiter(1.0) 
        
        self.REG_PATHS = ["/api/v1/passport/auth/register", "/api/v1/guest/passport/auth/register"]
        self.MAIL_PATHS = ["/api/v1/passport/comm/sendEmailVerify", "/api/v1/guest/passport/comm/sendEmailVerify"]
        self.CAPTCHA_PATHS = ["/api/v1/passport/comm/captcha", "/api/v1/guest/passport/comm/captcha"]

    def parse_existing_cache(self):
        data = {}
        if not os.path.exists(CACHE_FILE): return data
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                content = f.read()
                blocks = re.findall(r'\[(https?://.*?)\]\n(.*?)\n\n', content, re.S)
            for url, body in blocks:
                lines = [l.strip() for l in body.strip().split('\n') if '  ' in l]
                info = {l.split('  ')[0]: l.split('  ')[1] for l in lines if len(l.split('  ')) >= 2}
                if 'sub_url' in info: data[url.rstrip('/')] = info
        except: pass
        return data

    def append_cache(self, log):
        with open(CACHE_FILE, "a", encoding="utf-8") as f:
            f.write(log + "\n")
            f.flush()

    @request_with_retry()
    def _get(self, session, url, **kwargs):
        host = url.split('/')[2] if '/' in url else "default"
        self.limiter.wait(host)
        return session.get(url, **kwargs)

    @request_with_retry()
    def _post(self, session, url, **kwargs):
        host = url.split('/')[2] if '/' in url else "default"
        self.limiter.wait(host)
        return session.post(url, **kwargs)

    def get_session(self, url=""):
        s = crequests.Session(impersonate="chrome120", verify=False)
        headers = {
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        }
        if url:
            headers["Origin"] = url.rstrip('/')
            headers["Referer"] = f"{url.rstrip('/')}/"
        s.headers.update(headers)
        return s

    def create_temp_mail(self):
        random.shuffle(self.mail_api_list)
        for api in self.mail_api_list:
            try:
                s = crequests.Session(verify=False)
                dom_res = s.get(f"https://api.{api}/domains", timeout=DEFAULT_TIMEOUT).json()
                domain = dom_res['hydra:member'][0]['domain']
                email = f"{''.join(random.choices(string.ascii_lowercase + string.digits, k=10))}@{domain}"
                pw = "Pass" + ''.join(random.choices(string.digits, k=6))
                if s.post(f"https://api.{api}/accounts", json={"address": email, "password": pw}, timeout=DEFAULT_TIMEOUT).status_code == 201:
                    tk = s.post(f"https://api.{api}/token", json={"address": email, "password": pw}, timeout=DEFAULT_TIMEOUT).json()['token']
                    self.current_mail_api = api
                    return email, tk
            except: continue
        return None, None

    def wait_for_code(self, mail_token, timeout=MAIL_WAIT_TIMEOUT):
        s = crequests.Session(verify=False)
        s.headers.update({"Authorization": f"Bearer {mail_token}"})
        start = time.time()
        while time.time() - start < timeout:
            try:
                msgs = s.get(f"https://api.{self.current_mail_api}/messages", timeout=DEFAULT_TIMEOUT).json().get('hydra:member', [])
                if msgs:
                    for m in msgs:
                        if any(k in m['subject'].lower() for k in ['code', '验证码', 'verification']):
                            res = s.get(f"https://api.{self.current_mail_api}/messages/{m['id']}", timeout=DEFAULT_TIMEOUT).json()
                            txt = res.get('text', '') or res.get('intro', '')
                            code = re.search(r'(\d{6})', txt)
                            if code: return code.group(1)
            except: pass
            time.sleep(2)
        return None

    def get_captcha_code(self, session, base_url):
        for cp_path in self.CAPTCHA_PATHS:
            try:
                res = self._get(session, f"{base_url}{cp_path}", timeout=DEFAULT_TIMEOUT)
                if not res or res.status_code != 200: continue
                img_data = res.content if "image" in res.headers.get("Content-Type", "").lower() else base64.b64decode(res.json().get('data', '').split(',')[-1])
                processed_img = preprocess_captcha(img_data)
                return self.ocr.classification(processed_img)
            except: continue
        return None

    def get_info_from_sub_header(self, sub_url, session=None, base_url=None):
        try:
            s = session if session else self.get_session()
            client_uas = ["ClashforWindows/0.19.29", "Shadowrocket/1054 CFNetwork/1333.0.4", "v2rayN/6.23"]
            res = s.get(sub_url, headers={"User-Agent": random.choice(client_uas)}, timeout=DEFAULT_TIMEOUT + 5)
            header = res.headers.get('subscription-userinfo', '')
            nodes_text = res.text
            u, t, e = 0, 0, 0
            if header:
                info = {k.strip(): int(v) for k, v in (item.split('=') for item in header.split(';') if '=' in item)}
                u, t, e = (info.get('upload', 0) + info.get('download', 0)), info.get('total', 0), info.get('expire', 0)
            
            if t == 0 and s and base_url:
                try:
                    d = s.get(f"{base_url.rstrip('/')}/api/v1/user/info", timeout=DEFAULT_TIMEOUT).json().get('data', {})
                    if d:
                        u, t, e = (d.get('u', 0) + d.get('d', 0)), d.get('transfer_enable', 0), d.get('expired_at', 0)
                except: pass
            return u, t, e, nodes_text
        except: return 0, 0, 0, ""

    def auto_buy_plan(self, url, session):
        """关键改进：增加 onetime_price 识别，确保买到试用流量"""
        for path in ["/api/v1/user/plan/fetch", "/api/v1/guest/plan/fetch"]:
            try:
                res = self._get(session, f"{url}{path}", timeout=DEFAULT_TIMEOUT).json()
                plans = res.get("data", [])
                best_plan = None
                max_transfer = -1
                for p in plans:
                    # 检查月付、年付或“一次性(onetime)”是否为 0
                    free_cycles = [k for k, v in p.items() if '_price' in k and v == 0 and k != 'reset_price']
                    if free_cycles:
                        if p.get('transfer_enable', 0) > max_transfer:
                            max_transfer = p.get('transfer_enable', 0)
                            best_plan = {'id': p['id'], 'cycle': free_cycles[0].replace('_price','')}
                if best_plan:
                    order = self._post(session, f"{url}/api/v1/user/order/save", json={'plan_id': best_plan['id'], 'cycle': best_plan['cycle']}, timeout=DEFAULT_TIMEOUT).json()
                    trade_no = order.get('data')
                    if trade_no:
                        self._post(session, f"{url}/api/v1/user/order/checkout", json={'trade_no': trade_no, 'method': 1}, timeout=DEFAULT_TIMEOUT)
                        return True
            except: continue
        return False

    def extract_nodes_strict(self, content):
        if not content: return []
        uri_regex = r'(?:vmess|vless|ss|ssr|trojan|hysteria|hy2|anytls|tuic)://[^\s\'"<>]+'
        results = re.findall(uri_regex, content, re.I)
        if not results:
            try:
                cleaned = re.sub(r'[^a-zA-Z0-9+/=]', '', content)
                missing_padding = len(cleaned) % 4
                if missing_padding: cleaned += '=' * (4 - missing_padding)
                decoded = base64.b64decode(cleaned).decode('utf-8', errors='ignore')
                if "://" in decoded: results = re.findall(uri_regex, decoded, re.I)
            except: pass
        return list(set([r.strip() for r in results]))

    def process_task(self, url):
        url = url.rstrip('/')
        last_err = "无法连接"
        try:
            host = url.split('//')[-1].split('/')[0].split(':')[0]
            socket.gethostbyname(host)
        except Exception as e: return [], self.format_error_log(url, f"DNS失败: {str(e)}"), ""

        sess = self.get_session(url)
        if url in self.old_cache:
            info = self.old_cache[url]
            sub_url = info.get('sub_url', '')
            if sub_url:
                u, t, exp, sub_txt = self.get_info_from_sub_header(sub_url, sess, url)
                if t > 0:
                    return self.extract_nodes_strict(sub_txt), self.format_log(url, info['email'], u, t, exp, sub_url), sub_url

        try:
            email_base = ''.join(random.choices(string.ascii_lowercase, k=9))
            pw = "Pass123456"
            for reg_path in self.REG_PATHS:
                strategies = [
                    {'name': 'JSON', 'is_json': True, 'payload': {'email': f"{email_base}@gmail.com", 'password': pw, 'repassword': pw}},
                    {'name': 'Form', 'is_json': False, 'payload': {'email': f"{email_base}@gmail.com", 'password': pw, 'repassword': pw}},
                ]
                for st in strategies:
                    try:
                        p = st['payload'].copy()
                        def fire(data):
                            if st['is_json']: return self._post(sess, f"{url}{reg_path}", json=data)
                            return self._post(sess, f"{url}{reg_path}", data=data)

                        res_raw = fire(p)
                        if not res_raw or res_raw.status_code == 404: continue
                        res_data = res_raw.json()
                        msg = str(res_data.get('message', '')).lower()
                        last_err = msg

                        # 验证码和邮箱逻辑保持不变
                        if "captcha" in msg:
                            code = self.get_captcha_code(sess, url)
                            if code: p['captcha_code'] = code; res_data = fire(p).json()
                        
                        if any(x in msg for x in ["邮箱", "email_code", "required"]):
                            t_email, t_token = self.create_temp_mail()
                            if t_email:
                                cap = self.get_captcha_code(sess, url)
                                self._post(sess, f"{url}/api/v1/passport/comm/sendEmailVerify", json={'email': t_email, 'captcha_code': cap})
                                ec = self.wait_for_code(t_token)
                                if ec: p.update({'email': t_email, 'email_code': ec}); res_data = fire(p).json()

                        tk = res_data.get("data", {}).get("token")
                        if tk:
                            sess.headers.update({"Authorization": tk})
                            self.auto_buy_plan(url, sess)
                            
                            # 关键改进：通过 API 获取官方订阅链接，解决 /s/ 链接问题
                            sub_url = f"{url}/api/v1/client/subscribe?token={tk}"
                            try:
                                # 尝试获取官方配置的短链接
                                sub_res = self._get(sess, f"{url}/api/v1/user/getSubscribe", timeout=DEFAULT_TIMEOUT).json()
                                if sub_res.get('data'): sub_url = sub_res['data']
                            except: pass

                            u, t, exp, sub_txt = self.get_info_from_sub_header(sub_url, sess, url)
                            log = self.format_log(url, p['email'], u, t, exp, sub_url)
                            self.append_cache(log)
                            return self.extract_nodes_strict(sub_txt), log, sub_url
                    except: continue
        except Exception as e: last_err = str(e)
        return [], self.format_error_log(url, last_err), ""

    def format_log(self, url, email, u, t, exp, sub_url):
        exp_s = datetime.fromtimestamp(exp).strftime('%Y-%m-%d %H:%M:%S') if exp and exp > 0 else "永久有效"
        return (f"[{url}]\nbuy  pass\nemail  {email}\n"
                f"sub_info  {self.f_size(u)}  {self.f_size(t)}  {exp_s}  (剩余 {self.f_size(max(0,t-u))})\n"
                f"sub_url  {sub_url}\ntime  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\ntype  v2board\n\n")

    def format_error_log(self, url, reason):
        return (f"[{url}]\nstatus  failed\nreason  {reason}\ntime  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

    def f_size(self, s):
        try:
            s = float(s)
            if s <= 0: return "0B"
            for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
                if s < 1024: return f"{s:.1f}{unit}"
                s /= 1024
            return f"{s:.1f}PB"
        except: return "0B"

def main():
    if not os.path.exists(URLS_FILE): return
    with open(URLS_FILE, "r", encoding="utf-8") as f:
        urls = list(set([l.strip() for l in f if l.strip().startswith('http')]))
    random.shuffle(urls) 
    commander = AirportCommander()
    all_nodes, all_logs, all_sub_urls = [], [], []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as exe:
        fut = {exe.submit(commander.process_task, u): u for u in urls}
        for f in as_completed(fut):
            try:
                res = f.result()
                if res and res[1]:
                    nodes, log, sub_url = res
                    if nodes: all_nodes.extend(nodes)
                    all_logs.append(log); 
                    if sub_url: all_sub_urls.append(sub_url)
                    tag = "成功" if "buy  pass" in log else "失败"
                    print(f"{tag}: {log.splitlines()[0]}")
            except: pass
    with open("nodes_plain.txt", "w", encoding="utf-8") as f: f.write("\n".join(list(set(all_nodes))))
    with open(SUB_FILE, "w", encoding="utf-8") as f: f.write("\n".join(list(set(all_sub_urls))))
    with open(CACHE_FILE, "w", encoding="utf-8") as f: f.writelines(all_logs)

if __name__ == '__main__':
    main()
