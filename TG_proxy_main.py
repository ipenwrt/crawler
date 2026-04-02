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
SUB_FILE = "subscription.txt"  # 保存注册成功的订阅链接
MAX_WORKERS = 100               # 保持 100 并发
DEFAULT_TIMEOUT = 5             # 5秒超时过滤死链
MAIL_WAIT_TIMEOUT = 35          # 邮件等待上限

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
                except Exception:
                    pass
                time.sleep(delay + random.uniform(0.1, 0.5))
                tries += 1
                delay *= 1.5
            return None
        return wrapper
    return decorator

# ==================== 速率限制器 ====================
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

# ==================== OCR 预处理 ====================
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
    except:
        return img_bytes

# ==================== 机场指挥官主类 ====================
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
        if 'timeout' not in kwargs: kwargs['timeout'] = DEFAULT_TIMEOUT
        return session.get(url, **kwargs)

    @request_with_retry()
    def _post(self, session, url, **kwargs):
        host = url.split('/')[2] if '/' in url else "default"
        self.limiter.wait(host)
        if 'timeout' not in kwargs: kwargs['timeout'] = DEFAULT_TIMEOUT
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
            
            # 修正：移除强制 flag=clash，以直接获取包含 anytls 等协议的 Base64 订阅内容
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
        for path in ["/api/v1/user/plan/fetch", "/api/v1/guest/plan/fetch"]:
            try:
                res = self._get(session, f"{url}{path}", timeout=DEFAULT_TIMEOUT).json()
                for p in res.get("data", []):
                    free_cycles = [k for k, v in p.items() if '_price' in k and v == 0]
                    for cycle in free_cycles:
                        order = self._post(session, f"{url}/api/v1/user/order/save", json={'plan_id': p['id'], 'cycle': cycle.replace('_price','')}, timeout=DEFAULT_TIMEOUT).json()
                        trade_no = order.get('data')
                        if trade_no:
                            self._post(session, f"{url}/api/v1/user/order/checkout", json={'trade_no': trade_no, 'method': 1}, timeout=DEFAULT_TIMEOUT)
                            return True
            except: continue
        return False

    # ==================== 核心增强：精准提取节点 ====================
    def extract_nodes_strict(self, content):
        """精准提取真实 URI 节点，支持 Base64 混合格式，移除伪节点"""
        if not content: return []
        
        # 协议正则表达式
        uri_regex = r'(?:vmess|vless|ss|ssr|trojan|hysteria|hy2|anytls|tuic)://[^\s\'"<>]+'
        
        def find_uris(text):
            return re.findall(uri_regex, text, re.I)

        # 1. 直接搜寻
        results = find_uris(content)

        # 2. 如果没搜到，尝试对整个内容进行 Base64 解码后再匹配
        if not results:
            try:
                # 预清洗：移除所有非 Base64 字符
                cleaned = re.sub(r'[^a-zA-Z0-9+/=]', '', content)
                # 自动补全 Padding
                missing_padding = len(cleaned) % 4
                if missing_padding: cleaned += '=' * (4 - missing_padding)
                
                decoded = base64.b64decode(cleaned).decode('utf-8', errors='ignore')
                if "://" in decoded:
                    results = find_uris(decoded)
            except: pass
            
        return list(set([r.strip() for r in results]))

    def process_task(self, url):
        url = url.rstrip('/')
        try:
            host = url.split('//')[-1].split('/')[0].split(':')[0]
            socket.gethostbyname(host)
        except: return [], "", ""

        sess = self.get_session(url)
        if url in self.old_cache:
            info = self.old_cache[url]
            sub_url = info.get('sub_url', '')
            if sub_url:
                u, t, exp, sub_txt = self.get_info_from_sub_header(sub_url, sess, url)
                log = self.format_log(url, info['email'], u, t, exp, sub_url)
                return self.extract_nodes_strict(sub_txt), log, sub_url

        try:
            self._get(sess, url, timeout=DEFAULT_TIMEOUT)
            email_base = ''.join(random.choices(string.ascii_lowercase, k=9))
            pw = "Pass123456"
            
            for reg_path in self.REG_PATHS:
                strategies = [
                    {'name': '小蜜蜂/Form', 'is_json': False, 'payload': {'email': f"{email_base}@gmail.com", 'password': pw, 'invite_code': '', 'email_code': ''}},
                    {'name': '标准/JSON', 'is_json': True, 'payload': {'email': f"{email_base}@gmail.com", 'password': pw, 'repassword': pw, 'invite_code': ''}},
                    {'name': '经典/Form', 'is_json': False, 'payload': {'email': f"{email_base}@gmail.com", 'password': pw, 'repassword': pw, 'invite_code': ''}},
                ]

                for st in strategies:
                    try:
                        p = st['payload'].copy()
                        def fire(data):
                            if st['is_json']: return self._post(sess, f"{url}{reg_path}", json=data)
                            return self._post(sess, f"{url}{reg_path}", data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})

                        res_raw = fire(p)
                        if not res_raw or res_raw.status_code == 404: break
                        res_data = res_raw.json()
                        msg = str(res_data.get('message', '')).lower()

                        if "captcha" in msg or res_data.get('data') is False:
                            for _ in range(2):
                                code = self.get_captcha_code(sess, url)
                                if code:
                                    p['captcha_code'] = code
                                    res_data = fire(p).json()
                                    if res_data.get("data", {}).get("token"): break
                                else: break

                        if any(x in msg for x in ["邮箱验证码", "不能为空", "required", "email_code", "verification code"]):
                            t_email, t_token = self.create_temp_mail()
                            if t_email:
                                cap = self.get_captcha_code(sess, url)
                                for m_path in self.MAIL_PATHS:
                                    if st['is_json']: self._post(sess, f"{url}{m_path}", json={'email': t_email, 'captcha_code': cap})
                                    else: self._post(sess, f"{url}{m_path}", data={'email': t_email, 'captcha_code': cap})
                                ec = self.wait_for_code(t_token)
                                if ec:
                                    p.update({'email': t_email, 'email_code': ec})
                                    res_data = fire(p).json()

                        tk = res_data.get("data", {}).get("token")
                        if tk:
                            sess.headers.update({"Authorization": tk})
                            self.auto_buy_plan(url, sess)
                            sub_url = f"{url}/api/v1/client/subscribe?token={tk}"
                            u, t, exp, sub_txt = self.get_info_from_sub_header(sub_url, sess, url)
                            log = self.format_log(url, p['email'], u, t, exp, sub_url)
                            self.append_cache(log)
                            return self.extract_nodes_strict(sub_txt), log, sub_url
                    except: continue
        except: pass
        return [], "", ""

    def format_log(self, url, email, u, t, exp, sub_url):
        exp_s = datetime.fromtimestamp(exp).strftime('%Y-%m-%d %H:%M:%S') if exp and exp > 0 else "永久有效"
        rem_s = self.f_size(max(0, t - u))
        return (f"[{url}]\nbuy  pass\nemail  {email}\n"
                f"sub_info  {self.f_size(u)}  {self.f_size(t)}  {exp_s}  (剩余 {rem_s})\n"
                f"sub_url  {sub_url}\ntime  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\ntype  v2board\n\n")

    def f_size(self, s):
        try:
            s = float(s)
            if s <= 0: return "0B"
            for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
                if s < 1024: return f"{s:.1f}{unit}"
                s /= 1024
            return f"{s:.1f}PB"
        except: return "0B"

# ==================== 主函数 ====================
def main():
    if not os.path.exists(URLS_FILE):
        print(f"找不到 {URLS_FILE}")
        return
    
    with open(URLS_FILE, "r", encoding="utf-8") as f:
        urls = list(set([l.strip() for l in f if l.strip().startswith('http')]))
    
    random.shuffle(urls) 
    commander = AirportCommander()
    all_nodes, all_logs, all_sub_urls = [], [], []
    
    print(f"任务启动：共 {len(urls)} 条网址，并发数: {MAX_WORKERS}")
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as exe:
        fut = {exe.submit(commander.process_task, u): u for u in urls}
        processed_count = 0
        for f in as_completed(fut):
            processed_count += 1
            try:
                res = f.result()
                if res and res[1]:
                    nodes, log, sub_url = res
                    if nodes: all_nodes.extend(nodes)
                    all_logs.append(log)
                    if sub_url: all_sub_urls.append(sub_url)
                    print(f"[{processed_count}/{len(urls)}] 成功: {log.splitlines()[0]} 节点数: {len(nodes)}")
            except Exception: pass

    # 1. 保存纯文本节点到 nodes_plain.txt
    unique_nodes = list(set(all_nodes))
    with open("nodes_plain.txt", "w", encoding="utf-8") as f: 
        f.write("\n".join(unique_nodes))
    
    # 2. 保存注册成功的订阅链接到 subscription.txt
    unique_subs = list(set(all_sub_urls))
    with open(SUB_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(unique_subs))
    
    # 3. 最终更新 CACHE_FILE
    with open(CACHE_FILE, "w", encoding="utf-8") as f: 
        f.writelines(all_logs)
        
    print(f"任务结束！共提取订阅链接: {len(unique_subs)} 个，有效节点: {len(unique_nodes)} 个")

if __name__ == '__main__':
    main()
