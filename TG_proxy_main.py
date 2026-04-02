# coding=utf-8
import base64, re, time, random, string, os, json
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# 核心依赖库
from curl_cffi import requests as crequests
import ddddocr
import urllib3

# 禁用 SSL 警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- 文件配置 ---
URLS_FILE = "urls.txt"
CACHE_FILE = "tg.cache"

class AirportCommander:
    def __init__(self):
        self.old_cache = self.parse_existing_cache()
        # 初始化 OCR
        self.ocr = ddddocr.DdddOcr(show_ad=False)
        # 邮件 API 备选定义
        self.mail_api_list = ["mail.tm", "mail.gw"]
        self.mail_domain = "mail.tm"
        
        # 定义路径变种 - 严格保留，不准删除
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
                info = {l.split('  ')[0]: l.split('  ')[1:] if len(l.split('  ')) > 2 else l.split('  ')[1] for l in lines}
                data[url] = info
        except: pass
        return data

    def get_session(self, url=""):
        s = crequests.Session(impersonate="chrome120", verify=False)
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
        }
        if url:
            headers["Origin"] = url.rstrip('/')
            headers["Referer"] = f"{url.rstrip('/')}/"
        s.headers.update(headers)
        return s

    # --- 临时邮箱逻辑 (支持 API 自动切换) ---
    def create_temp_mail(self):
        random.shuffle(self.mail_api_list)
        for api in self.mail_api_list:
            try:
                s = crequests.Session(verify=False)
                domain_res = s.get(f"https://api.{api}/domains", timeout=8).json()
                domain = domain_res['hydra:member'][0]['domain']
                email = f"{''.join(random.choices(string.ascii_lowercase + string.digits, k=10))}@{domain}"
                password = "Pass" + ''.join(random.choices(string.digits, k=6))
                if s.post(f"https://api.{api}/accounts", json={"address": email, "password": password}, timeout=8).status_code == 201:
                    tk_res = s.post(f"https://api.{api}/token", json={"address": email, "password": password}, timeout=8).json()
                    self.mail_domain = api # 锁定当前成功的 API
                    return email, tk_res['token']
            except: continue
        return None, None

    def wait_for_code(self, mail_token, timeout=60):
        s = crequests.Session(verify=False)
        s.headers.update({"Authorization": f"Bearer {mail_token}"})
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                # 使用当前创建成功的 API 域名
                msg_res = s.get(f"https://api.{self.mail_domain}/messages", timeout=8).json()
                msgs = msg_res.get('hydra:member', [])
                if msgs:
                    msg_id = msgs[0]['id']
                    res = s.get(f"https://api.{self.mail_domain}/messages/{msg_id}", timeout=8).json()
                    txt = res.get('text', '') or res.get('intro', '')
                    code = re.search(r'(\d{6})', txt)
                    if code: return code.group(1)
            except: pass
            time.sleep(2) # 提速：缩短等待时间
        return None

    # --- 验证码识别 ---
    def get_captcha_code(self, session, base_url):
        for cp_path in self.CAPTCHA_PATHS:
            try:
                res = session.get(f"{base_url}{cp_path}", timeout=6)
                if res.status_code == 200:
                    img_data = res.content if "image" in res.headers.get("Content-Type", "").lower() else base64.b64decode(res.json().get('data', '').split(',')[-1])
                    return self.ocr.classification(img_data)
            except: continue
        return None

    # --- 流量与订阅获取 (Header + API 二合一) ---
    def get_info_from_sub_header(self, sub_url, session=None, base_url=None):
        try:
            s = session if session else self.get_session()
            res = s.get(sub_url, timeout=10, headers={"User-Agent": "Clash/1.0"})
            header = res.headers.get('subscription-userinfo', '')
            nodes_text = res.text
            u, t, e = 0, 0, 0
            if header:
                info = {k.strip(): int(v) for k, v in (item.split('=') for item in header.split(';') if '=' in item)}
                u, t, e = info.get('upload', 0) + info.get('download', 0), info.get('total', 0), info.get('expire', 0)
            if t == 0 and session and base_url:
                try:
                    d = session.get(f"{base_url}/api/v1/user/info", timeout=5).json().get('data', {})
                    u, t, e = (d.get('u', 0) + d.get('d', 0)), d.get('transfer_enable', 0), d.get('expired_at', 0)
                except: pass
            return u, t, e, nodes_text
        except: return 0, 0, 0, ""

    # --- 自动买包 ---
    def auto_buy_plan(self, url, session):
        for path in ["/api/v1/user/plan/fetch", "/api/v1/guest/plan/fetch"]:
            try:
                res = session.get(f"{url}{path}", timeout=8).json()
                for p in res.get("data", []):
                    free_cycles = [k for k, v in p.items() if '_price' in k and v == 0]
                    for cycle in free_cycles:
                        order = session.post(f"{url}/api/v1/user/order/save", json={'plan_id': p['id'], 'cycle': cycle}, timeout=8).json()
                        trade_no = order.get('data')
                        if trade_no:
                            session.post(f"{url}/api/v1/user/order/checkout", json={'trade_no': trade_no, 'method': 1}, timeout=8)
                            return True
            except: continue
        return False

    # --- 核心处理逻辑 ---
    def process_task(self, url):
        url = url.rstrip('/')
        session = self.get_session(url)
        
        # 1. 缓存复用逻辑
        if url in self.old_cache:
            info = self.old_cache[url]
            u_b, t_b, exp, sub_t = self.get_info_from_sub_header(info['sub_url'])
            nodes = self.extract_nodes(sub_t)
            # 即使没有节点也返回，只要缓存有记录
            print(f"[缓存复用] {url}")
            return nodes, self.format_log(url, info['email'], u_b, t_b, exp, info['sub_url'])

        # 2. 预访问首页
        try:
            session.get(url, timeout=8)
        except:
            return [], ""

        email_base = ''.join(random.choices(string.ascii_lowercase, k=9))
        pw = "Pass123456"
        
        for reg_path in self.REG_PATHS:
            # 定义三种注册策略
            strategies = [
                {'name': '小蜜蜂/Form模式', 'is_json': False, 'payload': {'email': f"{email_base}@gmail.com", 'password': pw, 'invite_code': '', 'email_code': ''}},
                {'name': '标准/JSON模式', 'is_json': True, 'payload': {'email': f"{email_base}@gmail.com", 'password': pw, 'repassword': pw, 'invite_code': ''}},
                {'name': '经典/Form模式', 'is_json': False, 'payload': {'email': f"{email_base}@gmail.com", 'password': pw, 'repassword': pw, 'invite_code': ''}},
            ]

            for st in strategies:
                # 增加邮箱重复自动重试
                for attempt in range(2):
                    try:
                        p = st['payload'].copy()
                        
                        def fire_request(data):
                            if st['is_json']:
                                return session.post(f"{url}{reg_path}", json=data, timeout=8)
                            else:
                                headers = {"Content-Type": "application/x-www-form-urlencoded"}
                                return session.post(f"{url}{reg_path}", data=data, headers=headers, timeout=8)

                        res_raw = fire_request(p)
                        if res_raw.status_code == 404: break # 路径不对，跳出
                        
                        try:
                            res_data = res_raw.json()
                        except:
                            continue

                        msg = str(res_data.get('message', '')).lower()
                        
                        # 邮箱重复：换个邮箱名重试当前策略
                        if "已在系统中存在" in msg or "exists" in msg:
                            email_base = ''.join(random.choices(string.ascii_lowercase, k=10))
                            st['payload']['email'] = f"{email_base}@gmail.com"
                            continue 

                        # 打印调试日志
                        if res_raw.status_code != 200 and "captcha" not in msg:
                            print(f"[{url}] {st['name']} 提示: {res_raw.status_code} - {msg}")

                        # 判定是否需要邮件验证码补漏
                        need_email = any(x in msg for x in ["邮箱验证码", "不能为空", "required", "email_code", "verification code", "验证码不能为空"])

                        # 2.1 处理图形验证码
                        if "captcha" in msg or res_data.get('data') == False:
                            for _ in range(2):
                                code = self.get_captcha_code(session, url)
                                if code:
                                    p['captcha_code'] = code
                                    res_data = fire_request(p).json()
                                    msg = str(res_data.get('message', '')).lower()
                                    if res_data.get("data", {}).get("token"): break
                                    # 再次确认是否触发了邮件验证
                                    need_email = any(x in msg for x in ["邮箱验证码", "不能为空", "required", "email_code"])
                                else: break

                        # 2.2 处理邮件验证码
                        if need_email:
                            t_email, t_token = self.create_temp_mail()
                            if t_email:
                                cap = self.get_captcha_code(session, url)
                                for m_path in self.MAIL_PATHS:
                                    if st['is_json']: session.post(f"{url}{m_path}", json={'email': t_email, 'captcha_code': cap}, timeout=8)
                                    else: session.post(f"{url}{m_path}", data={'email': t_email, 'captcha_code': cap}, timeout=8)
                                
                                ec = self.wait_for_code(t_token)
                                if ec:
                                    p.update({'email': t_email, 'email_code': ec})
                                    res_data = fire_request(p).json()

                        # 3. 注册结果判断
                        tk = res_data.get("data", {}).get("token")
                        if tk:
                            # --- 逻辑核心改进：只要 Token 拿到，必须立刻记录并返回 ---
                            print(f"[注册成功] {url} ({st['name']})")
                            session.headers.update({"Authorization": tk})
                            self.auto_buy_plan(url, session)
                            sub_url = f"{url}/api/v1/client/subscribe?token={tk}"
                            u_b, t_b, exp, sub_t = self.get_info_from_sub_header(sub_url, session, url)
                            nodes = self.extract_nodes(sub_t)
                            # 无论有没有节点，都生成日志返回，确保存入 cache
                            return nodes, self.format_log(url, p['email'], u_b, t_b, exp, sub_url)
                        
                        break # 非邮箱重复错误，跳出重试
                    except: break
        
        return [], ""

    def extract_nodes(self, text):
        if not text: return []
        if "://" not in text:
            try: text = base64.b64decode(text).decode('utf-8', errors='ignore')
            except: pass
        return [l.strip() for l in text.splitlines() if "://" in l]

    def format_log(self, url, email, u_b, t_b, exp, sub_url):
        exp_s = datetime.fromtimestamp(exp).strftime('%Y-%m-%d %H:%M:%S') if exp else "永久有效"
        rem_s = self.f_size(max(0, t_b - u_b))
        return (f"[{url}]\nbuy  pass\nemail  {email}\nname  Airport_Site\n"
                f"sub_info  {self.f_size(u_b)}  {self.f_size(t_b)}  {exp_s}  (剩余 {rem_s})\n"
                f"sub_url  {sub_url}\ntime  {datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f+08:00')}\ntype  v2board\n\n")

    def f_size(self, s):
        if not s: return "0B"
        for u in ['B', 'KB', 'MB', 'GB', 'TB']:
            if s < 1024: return f"{s:.0f}{u}"
            s /= 1024
        return f"{s:.1f}PB"

def main():
    if not os.path.exists(URLS_FILE): return
    with open(URLS_FILE, "r", encoding="utf-8") as f:
        urls = list(set([l.strip() for l in f if l.strip().startswith('http')]))

    commander = AirportCommander()
    all_n, all_l = [], []
    
    print(f"指挥官启动：正在多线程扫描 {len(urls)} 个目标...")
    # --- 提速：并发 workers 设置为 25 ---
    with ThreadPoolExecutor(max_workers=25) as exe:
        fut = {exe.submit(commander.process_task, u): u for u in urls}
        for f in as_completed(fut):
            try:
                res = f.result()
                # --- 核心改进：只要 res[1] (日志数据) 存在，就必须存入缓存 ---
                if res and res[1]:
                    if res[0]: all_n.extend(res[0])
                    all_l.append(res[1])
            except: pass

    unique_n = sorted(list(set(all_n)))
    with open("nodes_plain.txt", "a", encoding="utf-8") as f: f.write("\n".join(unique_n))
    with open("subscription", "a", encoding="utf-8") as f:
        f.write(base64.b64encode("\n".join(unique_n).encode()).decode())
    with open(CACHE_FILE, "w", encoding="utf-8") as f: f.writelines(all_l)
    print(f"\n任务结束！共抓取节点: {len(unique_n)} 个")

if __name__ == '__main__':
    main()
