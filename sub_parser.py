import asyncio
import aiohttp
import base64
import re
import csv
import os
import socket
import json
import time
import ssl
import hashlib
from datetime import datetime
from urllib.parse import urlparse, quote, unquote
import geoip2.database

# --- 基础配置 ---
INPUT_FILE = "subscribes.txt"
GEOIP_DB = "GeoLite2-Country.mmdb" 
OUTPUT_TXT = "sub_parser.txt"
OUTPUT_B64 = "sub_parser_base64.txt"
OUTPUT_CSV = "sub_parser.csv"
OUTPUT_YAML = "sub_parser.yaml"

MAX_CONCURRENT_TASKS = 50 
MAX_RETRIES = 1

# --- 工具函数 ---
def decode_base64(data):
    if not data: return ""
    try:
        data = data.replace("-", "+").replace("_", "/")
        clean_data = re.sub(r'[^A-Za-z0-9+/=]', '', data.strip())
        missing_padding = len(clean_data) % 4
        if missing_padding: clean_data += '=' * (4 - missing_padding)
        return base64.b64decode(clean_data).decode('utf-8', errors='ignore')
    except: return ""

def encode_base64(data):
    try: return base64.b64encode(data.encode('utf-8')).decode('utf-8')
    except: return ""

def get_md5_short(text):
    return hashlib.md5(text.encode()).hexdigest()[:4]

def get_geo_info(host, reader):
    """适配 GeoLite2-Country.mmdb 的识别逻辑"""
    if not host or not reader: return "🌐", "未知地区"
    ip = host
    if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", host):
        try: ip = socket.gethostbyname(host)
        except: return "🌐", "未知地区"
    try:
        # 国家库使用 country 方法
        res = reader.country(ip)
        code = res.country.iso_code
        flag = "".join(chr(ord(c) + 127397) for c in code.upper()) if code else "🌐"
        # 获取中文国家名
        country_name = res.country.names.get('zh-CN') or res.country.name or "未知国家"
        return flag, country_name
    except:
        return "🌐", "未知地区"

def get_node_details(line, protocol):
    try:
        if protocol == 'vmess':
            v = json.loads(decode_base64(line.split("://")[1]))
            return {"server": v.get('add'), "port": int(v.get('port', 443)), "uuid": v.get('id'), "tls": v.get('tls') == "tls"}
        u = urlparse(line)
        return {"server": u.hostname, "port": int(u.port or 443)}
    except: return None

def parse_nodes(content, reader):
    if "://" not in content[:50] and len(content) > 20:
        content = decode_base64(content)
    protocols = ['vmess', 'vless', 'trojan', 'anytls', 'hysteria', 'hysteria2', 'hy2', 'tuic', 'ss', 'ssr']
    pattern = r'(?:' + '|'.join(protocols) + r')://[^\s\"\'<>#]+(?:#[^\s\"\'<>]*)?'
    found_links = re.findall(pattern, content, re.IGNORECASE)
    nodes = []
    for link in found_links:
        if link.lower().startswith(('http://', 'https://')): continue
        protocol = link.split("://")[0].lower()
        try:
            if protocol == 'vmess':
                host = json.loads(decode_base64(link.split("://")[1])).get('add')
            else:
                host = urlparse(link).hostname or re.search(r'@([^:/?#\s]+)', link).group(1).split(':')[0]
            flag, country = get_geo_info(host, reader)
            nodes.append({"protocol": protocol, "flag": flag, "country": country, "line": link})
        except: continue
    return nodes

async def fetch_with_retry(session, url, reader, semaphore):
    async with semaphore:
        for _ in range(MAX_RETRIES + 1):
            try:
                async with session.get(url, timeout=30, ssl=False) as res:
                    if res.status != 200: return url, [], 0
                    text = await res.text()
                    nodes = parse_nodes(text, reader)
                    if nodes:
                        print(f"[+] 成功 ({len(nodes)} 节点): {url}")
                        return url, nodes, len(nodes)
            except: pass
        return url, [], 0

async def main():
    all_urls = []
    if os.path.exists(INPUT_FILE):
        with open(INPUT_FILE, 'r', encoding='utf-8') as f:
            all_urls = re.findall(r'https?://[^\s<>\"\'\u4e00-\u9fa5]+', f.read())

    unique_urls = list(dict.fromkeys(all_urls))
    if not unique_urls: return
    if not os.path.exists(GEOIP_DB):
        print(f"缺失 {GEOIP_DB} 库文件"); return

    print(f"--- 正在处理 {len(unique_urls)} 个源 ---")
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
    
    with geoip2.database.Reader(GEOIP_DB) as reader:
        connector = aiohttp.TCPConnector(limit=50, ssl=False)
        async with aiohttp.ClientSession(headers={'User-Agent': 'v2rayN/6.23'}, connector=connector) as session:
            tasks = [fetch_with_retry(session, url, reader, semaphore) for url in unique_urls]
            results = await asyncio.gather(*tasks)
            raw_node_objs = []
            stats = []
            for url, nodes, count in results:
                raw_node_objs.extend(nodes); stats.append([url, count])

    # --- MD5 命名与 4 种文件输出 ---
    final_links = []
    yaml_proxies = []
    seen_lines = set()
    
    for obj in raw_node_objs:
        line, protocol, flag, country = obj["line"], obj["protocol"], obj["flag"], obj["country"]
        base_link = line.split('#')[0] if protocol != 'vmess' else line
        if base_link in seen_lines: continue
        seen_lines.add(base_link)

        short_id = get_md5_short(base_link)
        # 格式：[国旗] [国家名] 订阅节点聚合器_[MD5]
        new_name = f"{flag} {country} 订阅节点聚合器_{short_id}"
        
        try:
            if protocol == 'vmess':
                v_json = json.loads(decode_base64(line.split("://")[1]))
                v_json['ps'] = new_name
                final_links.append(f"vmess://{encode_base64(json.dumps(v_json))}")
            elif protocol == 'ssr':
                ssr_body = decode_base64(line.split("://")[1])
                main_part = ssr_body.split('&remarks=')[0]
                new_rem = encode_base64(new_name).replace('=', '').replace('+', '-').replace('/', '_')
                final_links.append(f"ssr://{encode_base64(main_part + '&remarks=' + new_rem)}")
            else:
                final_links.append(f"{base_link}#{quote(new_name)}")

            d = get_node_details(line, protocol)
            if d:
                p_type = "trojan" if protocol == 'anytls' else protocol
                proxy_item = f"  - {{ name: \"{new_name}\", type: {p_type}, server: {d['server']}, port: {d['port']}"
                if protocol == 'vmess': proxy_item += f", uuid: {d['uuid']}, cipher: auto, tls: {str(d['tls']).lower()}"
                proxy_item += ", udp: true }"
                yaml_proxies.append(proxy_item)
        except: continue

    # --- 写入 4 个文件 ---
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # 1. 文本明文
    with open(OUTPUT_TXT, "w", encoding="utf-8") as f: f.write("\n".join(final_links))
    
    # 2. Base64 订阅
    with open(OUTPUT_B64, "w", encoding="utf-8") as f: f.write(encode_base64("\n".join(final_links)))
    
    # 3. 统计报表
    success_stats = [item for item in stats if item[1] >= 1]

    with open(OUTPUT_CSV, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["订阅链接", "节点数量"])
        if success_stats:
            writer.writerows(success_stats)

    print(f"--- CSV 生成完成！仅保留成功源: {len(success_stats)} 个（已丢弃 {len(stats) - len(success_stats)} 个无效源） ---")

    # 4. Clash YAML
    yaml_header = f"""# 订阅节点聚合器
# Updated: {now_str}
# Total: {len(final_links)}

port: 7890
mode: Rule
dns:
  enable: true
  nameserver: [119.29.29.29, 223.5.5.5]

proxies:
"""
    with open(OUTPUT_YAML, "w", encoding="utf-8") as f:
        f.write(yaml_header + "\n".join(yaml_proxies))

    print(f"--- 任务完成！已生成 4 个文件，总计节点: {len(final_links)} ---")

if __name__ == "__main__":
    if os.name == 'nt': asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
