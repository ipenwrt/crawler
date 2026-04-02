# coding=utf-8
import requests
import random
import string
import os
import datetime
import base64
import json
import time
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

# 禁用 urllib3 的不安全请求警告
from requests.packages.urllib3.exceptions import InsecureRequestWarning
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

# --- 1. 变量配置 ---
REG_PATHS = [
    "/api/v1/passport/auth/register", 
    "/api/v1/guest/passport/auth/register",
    "/api/v1/client/register",
    "/auth/register",
    "/api/v1/passport/auth/subscribe",
    "/api/v1/passport/auth/v2boardRegister",
    "/register"
]
MAIL_PATHS = ["/api/v1/passport/comm/sendEmailVerify", "/api/v1/guest/passport/comm/sendEmailVerify"]
CAPTCHA_PATHS = ["/api/v1/passport/comm/captcha", "/api/v1/guest/passport/comm/captcha"]

INPUT_FILE = "urls.txt"
LOG_FILE = "trial.cache"
OUTPUT_SUB = "sub_links.txt"
OUTPUT_NODES = "nodes.txt"
BLACKLIST = ["github.com", "apple.com", "google.com", "pypi.org", "docker.com", "framer.com", "wiki.", "recipes"]

# --- 2. 性能与时区配置 ---
MAX_WORKERS = 180 
TIMEOUT = (10, 25) 
SH_TZ = datetime.timezone(datetime.timedelta(hours=8))

def log_flush(msg):
    now = datetime.datetime.now(SH_TZ).strftime('%H:%M:%S')
    print(f"[{now}] {msg}", flush=True)

def format_size(size):
    try:
        s = float(size)
        if s <= 0: return "0B"
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if s < 1024: return f"{s:.2f}{unit}"
            s /= 1024
        return f"{s:.2f}PB"
    except: return "0B"

def extract_nodes_strict(content):
    nodes = []
    uri_regex = r'(vmess|vless|ss|ssr|trojan|hysteria|hy2)://[^\s\'"<>]+'
    try:
        raw_b64 = re.sub(r'[^a-zA-Z0-9+/=]', '', content)
        missing_padding = len(raw_b64) % 4
        if missing_padding: raw_b64 += '=' * (4 - missing_padding)
        decoded = base64.b64decode(raw_b64).decode('utf-8', errors='ignore')
        if any(p in decoded for p in ["://", "proxies:", "server:"]):
            content = decoded
    except: pass
    
    matches = re.finditer(uri_regex, content, re.I)
    for m in matches: nodes.append(m.group())
    
    # 这一版新增：对 Clash 格式的兼容提取
    if "proxies:" in content or "server:" in content:
        servers = re.findall(r'server:\s*([^\s,]+)', content)
        if servers:
            for s in servers: nodes.append(f"ss://dummy_node_for_clash_{s}")
    return list(set(nodes))

def check_sub_status(url):
    uas = ['ClashforWindows/0.19.29', 'Shadowrocket/2.2.31 CFNetwork/1333.0.4 Darwin/21.5.0', 'v2rayN/6.23']
    target_urls = [url, url + "&flag=clash", url + "&flag=shadowrocket"]
    for test_url in target_urls:
        try:
            res = requests.get(test_url, headers={'User-Agent': random.choice(uas)}, timeout=TIMEOUT, verify=False)
            if res.status_code != 200: continue
            info_h = res.headers.get('subscription-userinfo', '')
            info_str = "0B  0B  永不过期  (剩余 0B)"
            has_traffic, is_expired = True, False
            if info_h:
                parts = {item.split('=')[0].strip(): item.split('=')[1].strip() for item in info_h.split(';') if '=' in item}
                total, up, down = int(parts.get('total', 0)), int(parts.get('upload', 0)), int(parts.get('download', 0))
                remains = total - (up + down)
                expire = int(parts.get('expire', 0))
                exp_date = datetime.datetime.fromtimestamp(expire, SH_TZ).strftime('%Y-%m-%d') if expire > 0 else "永久"
                info_str = f"{format_size(up+down)}  {format_size(total)}  {exp_date}  (剩余 {format_size(remains)})"
                if total > 0 and remains <= 0: has_traffic = False
                if 0 < expire < time.time(): is_expired = True
            nodes = extract_nodes_strict(res.text)
            if len(nodes) > 0:
                return info_str, len(nodes), (has_traffic and not is_expired), nodes
        except: continue
    return "订阅检测失败", 0, False, []

def load_cache():
    cache = {}
    if not os.path.exists(LOG_FILE): return cache
    try:
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            content = f.read()
        blocks = re.findall(r'\[(.*?)\]\n(.*?)(?=\n\n|\[|\Z)', content, re.DOTALL)
        for domain, info in blocks:
            sub_url = re.search(r'sub_url\s+(.*)', info)
            email = re.search(r'email\s+(.*)', info)
            if sub_url:
                cache[domain.strip()] = {
                    "sub_url": sub_url.group(1).strip(), 
                    "email": email.group(1).strip() if email else "unknown"
                }
    except: pass
    return cache

def process_domain(url, cached_info):
    original_domain = url.rstrip('/')
    domain = original_domain if original_domain.startswith('http') else 'https://' + original_domain
    clean_domain = domain.replace("https://", "").replace("http://", "").split('/')[0].rstrip(':')
    
    if any(b in clean_domain for b in BLACKLIST): return None, None, []

    # --- 逻辑 A: 命中缓存 (保持 7 参数) ---
    if cached_info:
        info_str, node_n, is_active, nodes = check_sub_status(cached_info['sub_url'])
        if is_active:
            log = (f"[{clean_domain}]\n"
                   f"buy  pass_cached\n"
                   f"email  {cached_info['email']}\n"
                   f"node_n  {node_n}\n"
                   f"sub_info  {info_str}\n"
                   f"sub_url  {cached_info['sub_url']}\n"
                   f"time  {datetime.datetime.now(SH_TZ).isoformat()}\n"
                   f"type  v2board\n")
            return log, cached_info['sub_url'], nodes

    session = requests.Session()
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)','Referer': domain,'Accept': 'application/json'}
    
    # 1. 尝试探测后端 (env.js)
    api_domain = domain
    try:
        env_res = session.get(f"{domain}/env.js", timeout=8, verify=False)
        if env_res.status_code == 200:
            match = re.search(r"host:\s*['\"](.*?)['\"]", env_res.text)
            if match:
                new_host = match.group(1).rstrip('/')
                if new_host and new_host.startswith('http') and new_host != domain:
                    api_domain = new_host
    except: pass

    # 2. 准备注册信息
    email = ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(12)) + random.choice(["@gmail.com", "@outlook.com", "@qq.com"])
    password = "ProxyPassword123"
    attempt_errors = []

    # 3. 循环接口注册
    for path in REG_PATHS:
        try:
            res = session.post(f"{api_domain}{path}", 
                               data={'email':email,'password':password,'invite_code':'','email_code':''}, 
                               headers=headers, timeout=12, verify=False)
            
            res_json = {}
            try: res_json = res.json()
            except: pass

            if res.status_code != 200:
                msg = res_json.get('message') or res_json.get('msg') or f"Status {res.status_code}"
                attempt_errors.append(f"{path} -> {msg}")
                continue

            if res_json.get("data"):
                data = res_json['data']
                auth_data = data.get('auth_data')
                token = data.get('token') if isinstance(data, dict) else data
                if not token: continue
                
                sub_url = f"{api_domain}/api/v1/client/subscribe?token={token}"
                
                # 尝试买套餐
                buy_status = "none"
                if auth_data:
                    for pid in range(1, 16):
                        try:
                            b_res = session.post(f"{api_domain}/api/v1/user/order/save", 
                                                headers={**headers, 'Authorization': auth_data}, 
                                                data={'period':'onetime_price','plan_id':str(pid)}, timeout=5, verify=False)
                            if b_res.status_code == 200: 
                                buy_status = f"pass(id:{pid})"
                                break
                        except: continue
                
                time.sleep(8) 
                info_str, node_n, is_active, nodes = check_sub_status(sub_url)
                
                # 日志格式整合 (7参数)
                log_entry = (f"[{clean_domain}]\n"
                             f"buy  {buy_status}\n"
                             f"email  {email}\n"
                             f"node_n  {node_n}\n"
                             f"sub_info  {info_str}\n"
                             f"sub_url  {sub_url}\n"
                             f"time  {datetime.datetime.now(SH_TZ).isoformat()}\n"
                             f"type  v2board\n")
                return log_entry, (sub_url if is_active else None), (nodes if is_active else [])
            else:
                attempt_errors.append(f"{path} -> 响应无Data: {res_json.get('message', '未知')}")
        except Exception as e:
            attempt_errors.append(f"{path} -> 异常: {type(e).__name__}")
            continue
    
    # 汇总报错
    error_report = " | ".join(attempt_errors)
    return f"[{clean_domain}]\n更新订阅失败原因: {error_report}\n\n", None, []

if __name__ == '__main__':
    log_flush("=== 启动整合版探测器 (7参数+详细错误+Clash提取) ===")
    if not os.path.exists(INPUT_FILE): sys.exit(1)

    old_cache = load_cache()
    with open(INPUT_FILE, 'r') as f:
        urls = list(set([line.strip() for line in f if line.strip()]))

    all_logs, active_subs, all_nodes = [], [], []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(process_domain, url, old_cache.get(url.replace("https://", "").replace("http://", "").split('/')[0].rstrip(':'))): url for url in urls}
        
        for future in as_completed(futures):
            url = futures[future]
            try:
                log_text, sub_link, nodes = future.result()
                if log_text:
                    all_logs.append(log_text)
                    if "buy  pass" in log_text or "buy  pass_cached" in log_text:
                        log_flush(f"Found Alive: {url}")
                if sub_link:
                    active_subs.append(sub_link)
                if nodes:
                    # 过滤掉 dummy 节点存入最终文件
                    real_nodes = [n for n in nodes if "dummy_node_for_clash" not in n]
                    all_nodes.extend(real_nodes)
            except Exception as e:
                log_flush(f"Critical Error ({url}): {e}")

    with open(LOG_FILE, 'w', encoding='utf-8') as f: f.write("\n\n".join(all_logs))
    with open(OUTPUT_SUB, 'a', encoding='utf-8') as f: f.write("\n".join(active_subs))
    unique_nodes = list(set(all_nodes))
    with open(OUTPUT_NODES, 'a', encoding='utf-8') as f: f.write("\n".join(unique_nodes))

    log_flush(f"=== 完工: 获得 {len(active_subs)} 个有效订阅, 共计 {len(unique_nodes)} 条节点 ===")
