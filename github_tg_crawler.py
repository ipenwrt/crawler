# coding=utf-8
import requests
import re
import os
import time
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

# --- 1. 配置区 ---
GH_TOKEN = os.getenv("GH_TOKEN", "")

GH_QUERIES = [
    ' "sub?target=clash" extension:yaml ',
    ' "api/v1/client/subscribe?token=" extension:yaml ',
    ' path:sub filename:*.yaml "http" ',
    ' "nodes" AND "http" extension:yaml ',
    ' "订阅地址" extension:md ',
    ' filename:sub_all.yaml ',
    ' filename:sub.yaml "clash" ',
    ' filename:subscribe.md "http" "token" ',
    ' "clash订阅" extension:yaml ',
    ' filename:valid-domains.txt ',
    ' "api/v1/client/subscribe?token=" extension:txt ',
    ' "v2board" AND "sub=" extension:py ',
    ' "sspanel" AND "/link/" extension:md ',
    ' "机场" AND "订阅" extension:txt ',
    ' "nodes" AND "https" extension:yaml ',
    ' "clash" AND "proxies" AND "url" extension:yaml ',
    ' "v2ray" AND "vmess://" extension:txt ',
    ' filename:config.yaml "Proxy Group" ',
    ' "/api/v1/client/subscribe" ',
    ' "sub_url" AND "token" extension:json '
]

# 1.2 排除关键词列表
EXCLUDE_KEYWORDS = [
    "louwangzhiyu", "yywhale", "nxxbbf", "slianvpn", "cloudaddy", "quickbeevpn",
    "tianmiao", "cokecloud", "boluoidc", "gpket", "fast8888", "ykxqn",
    "127.0.0.1", "localhost", "github", "google", "tencent", "apple", "cloudfront",
    "w3.org", "telegram.org", "t.me", "wikipedia", "pypi", "docker", "example",
    "microsoft", "facebook", "twitter", "jsvini", "v2cross", "vercel.app", "netlify.app",
    "glpat-", "ghp_", "gho_", "ghu_", "ghs_", "ghr_", "rulesets", "subconverter",
    "baidu", "aliyun", "beian", "gov.cn", "crashlytics", "sentry.io", "umeng.com", 
    "ampproject", "schema.org", "wordpress.org", "gravatar.com", "jquery.com",
    "cloudfront.net", "akamaized.net", "azureedge.net", "fastly.net",
    "v2ray.com", "clash.wiki", "shadowsocks.org", "getbootstrap.com"
]

# 1.3 静态资源后缀
STATIC_SUFFIXES = (
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".css", ".js", ".ico", 
    ".woff", ".woff2", ".zip", ".rar", ".exe", ".dmg", ".mp4", ".pdf"
)

# 1.4 订阅链接特征
SUB_PATTERNS = [
    "token=", "sub=", "/sub?", "/sub/", "subscribe=", "/subscribe?", 
    "api/v1/client/subscribe", "api/v1/client/sub", "/s/",
    "/link/", "/api/v1/subscribe", "/getsub", "type=clash", "type=v2ray",
    "&flag=clash", "&flag=v2ray", "clash=1", "sub/subscribe"
]

# 1.5 TG 频道源
TG_CHANNELS = [
    "https://t.me/s/v2board_share", "https://t.me/s/free_v2board", "https://t.me/s/v2board_channel",
    "https://t.me/s/v2ray_free_dy", "https://t.me/s/v2board_dy", "https://t.me/s/v2ray_free_clash",
    "https://t.me/s/clash_v2board", "https://t.me/s/Jichang_Share", "https://t.me/s/vpn_free_link",
    "https://t.me/s/nodes_share", "https://t.me/s/v2ray_free_nodes", "https://t.me/s/free_jichang_share",
    "https://t.me/s/jichang_shiyong", "https://t.me/s/free_node_channel", "https://t.me/s/jichang_list"
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
}

URL_PATTERN = re.compile(r'https?://[a-zA-Z0-9\-\.]+(?::\d+)?/[^\s\'"<>\]\)\,}]+')
IP_PATTERN = re.compile(r'^(\d{1,3}\.){3}\d{1,3}(:\d+)?$')

# --- 2. 功能函数 ---

def is_valid_url(url):
    """校验 URL 是否合法，增加全局变量稳定性处理"""
    if not url: return False
    
    # 局部引用加速并防止并发下的 NameError
    _exclude = EXCLUDE_KEYWORDS
    _static = STATIC_SUFFIXES
    
    u_low = url.strip().rstrip('.,/').lower()
    if not u_low.startswith('http') or len(u_low) < 15 or len(u_low) > 500: 
        return False
    if any(k in u_low for k in _exclude): 
        return False
    if '{' in u_low or '$' in u_low or 'githubusercontent' in u_low: 
        return False
    
    # 检查静态后缀
    path_only = u_low.split('?')[0]
    if path_only.endswith(_static):
        return False
    return True

def http_get(url, timeout=15):
    """通用的 HTTP GET 请求"""
    try:
        headers = HEADERS.copy()
        if "api.github.com" in url:
            if GH_TOKEN:
                headers["Authorization"] = f"token {GH_TOKEN}"
            headers["Accept"] = "application/vnd.github.v3+json"
        
        res = requests.get(url, headers=headers, timeout=timeout)
        return res if res.status_code == 200 else None
    except:
        return None

def process_github_item(item):
    """处理 GitHub 搜索到的单个文件内容"""
    links = set()
    try:
        raw_url = item.get('html_url', '').replace('github.com', 'raw.githubusercontent.com').replace('/blob/', '/')
        file_res = http_get(raw_url, timeout=10)
        if file_res:
            found = URL_PATTERN.findall(file_res.text)
            for l in found:
                clean_l = l.rstrip('.,/;\'"')
                if is_valid_url(clean_l):
                    links.add(clean_l)
    except:
        pass
    return links

def fetch_github():
    """从 GitHub 采集"""
    all_links = set()
    for q in GH_QUERIES:
        api_url = f"https://api.github.com/search/code?q={q}&sort=indexed&per_page=100"
        res = http_get(api_url)
        if not res: 
            continue
        try:
            items = res.json().get('items', [])
            print(f" -> [GitHub] 搜索关键词 '{q[:15]}...' | 命中文件: {len(items)}")
            
            # 嵌套线程池加速文件下载
            with ThreadPoolExecutor(max_workers=10) as sub_executor:
                futures = [sub_executor.submit(process_github_item, item) for item in items]
                for f in as_completed(futures):
                    all_links.update(f.result())
        except Exception as e:
            print(f" [!] GitHub 解析异常: {e}")
            continue
        time.sleep(2) # 遵守速率限制
    return all_links

def fetch_tg():
    """从 TG 频道采集"""
    links = set()
    for url in TG_CHANNELS:
        res = http_get(url, timeout=15)
        if res:
            found = URL_PATTERN.findall(res.text)
            valid = [l.rstrip('.,/;\'"') for l in found if is_valid_url(l)]
            links.update(valid)
            if len(valid) > 0:
                print(f" -> [TG] 频道: {url.split('/')[-1]} | 获取有效 URL: {len(valid)}")
    return links

def load_existing(filename):
    """加载本地历史数据"""
    if os.path.exists(filename):
        try:
            with open(filename, "r", encoding="utf-8") as f:
                return set(line.strip() for line in f if line.strip())
        except:
            return set()
    return set()

# --- 3. 主程序 ---

if __name__ == "__main__":
    start_time = time.time()
    print(f"=== BOT 启动：开始深度采集 (时间: {time.strftime('%Y-%m-%d %H:%M:%S')}) ===")
    
    # 1. 抓取 (并行)
    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(fetch_github)
        f2 = executor.submit(fetch_tg)
        all_found = f1.result().union(f2.result())

    # 2. 增量去重逻辑
    old_domains = load_existing("domains.txt")
    old_subs = load_existing("subscribes.txt")

    final_domains = old_domains.copy()
    final_subs = old_subs.copy()

    for link in all_found:
        try:
            parsed = urlparse(link)
            if parsed.netloc and '.' in parsed.netloc:
                if IP_PATTERN.match(parsed.netloc): continue
                
                # 提取协议+域名
                final_domains.add(f"{parsed.scheme}://{parsed.netloc}")
                
                # 提取满足特征的订阅链接
                link_lower = link.lower()
                if any(p in link_lower for p in SUB_PATTERNS):
                    # 排除 GitLab 等生成的干扰 Token
                    if "glpat-" not in link_lower:
                        final_subs.add(link)
        except: 
            continue

    # 3. 数据持久化 (排序并写入)
    try:
        with open("urls.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(sorted(list(final_domains))))
        
        with open("subscribes.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(sorted(list(final_subs))))
    except Exception as e:
        print(f" [!] 文件写入失败: {e}")

    # 4. 统计汇报
    duration = int(time.time() - start_time)
    print("-" * 50)
    print(f"采集汇总报告 (耗时: {duration}s)")
    print(f" -> 原始 URL 发现: {len(all_found)} 条")
    print(f" -> 累计纯净域名: {len(final_domains)} (新增: {len(final_domains) - len(old_domains)})")
    print(f" -> 累计订阅链接: {len(final_subs)} (新增: {len(final_subs) - len(old_subs)})")
    print("-" * 50)
