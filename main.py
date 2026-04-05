"""
TVBox 纯净源自动筛选与深度测速脚本
功能：
1. 从多仓聚合链接中抓取所有子源
2. 通过关键词过滤掉需要网盘授权的源（饭太硬、肥猫等）以及播放卡顿的黑名单源（闪影等）
3. 深度测试：下载每个子源的配置，提取内部视频站API域名，测其HTTP延迟
4. 只保留内部API存活率>=40%且平均延迟<1500ms的源
5. 按内部API平均延迟排序，输出 tvbox_clean.json
"""

import json
import urllib.request
import base64
import re
import time
import socket
import ssl
import concurrent.futures
from urllib.parse import urlparse, quote

# ========== 配置区 ==========
SOURCE_URLS = [
    # 你的核心大仓库
    "https://gh-proxy.com/https://raw.githubusercontent.com/wzh15802/tvbox/main/tv.json",
    "https://gitlab.com/noimank/tvbox/-/raw/main/tvboxmuti.json",
    "https://freed.yuanhsing.cf/TVBox/meowcf.json",
    # 强制直接测试这些比较有名的单仓（以防大仓库没爬到或者剔除了）
    "https://xn--pppp-wn6lw489o.e.nxog.top/apib.php?id=5", # 欧歌
    "http://cdn.qiaoji8.com/tvbox.json", # 巧技
]

# 【用户绝对黑名单】在这里加入经常缓冲、加载不出来或者需要授权的源名字！
BANNED_KEYWORDS = [
     "盘", "阿里", "夸克", "网盘",  "测试", "广告","动漫","宝宝"  # <--- 将“闪影”等卡顿源永远打入冷宫
]

SKIP_DOMAINS = {
    "github.com", "raw.githubusercontent.com", "gitee.com", "gitlab.com",
    "jihulab.com", "bitbucket.org", "ghproxy.cxkpro.top", "gh-proxy.com",
    "ghfast.top", "raw.bgithub.xyz", "raw.gitmirror.com", "ghproxy.net",
    "cdn.jsdelivr.net", "pastebin.com", "gitcode.net",
    "bing.img.run", "pic.imgdb.cn", "i.imgur.com",
    "127.0.0.1", "localhost",
}

SKIP_EXTENSIONS = {
    ".jar", ".js", ".css", ".png", ".jpg", ".jpeg", ".gif",
    ".svg", ".woff", ".woff2", ".ttf", ".md", ".txt", ".apk"
}
# ========== 配置区结束 ==========

def safe_print(msg):
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode())

def get_context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx

def fetch_raw(url, timeout=8):
    headers = {'User-Agent': 'Mozilla/5.0'}
    safe_url = quote(url, safe=":/?&=")
    req = urllib.request.Request(safe_url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout, context=get_context()) as resp:
        return resp.read()

def try_decode(raw_bytes):
    text = raw_bytes.decode("utf-8", errors="ignore")
    stripped = text.strip()
    if len(stripped) > 100 and "\n" not in stripped[:200] and re.match(r'^[A-Za-z0-9+/=]+$', stripped[:200]):
        try:
            decoded = base64.b64decode(stripped).decode("utf-8", errors="ignore")
            if "sites" in decoded or "http" in decoded:
                return decoded
        except:
            pass
    return text

def fetch_json(url):
    try:
        raw = fetch_raw(url)
        text = try_decode(raw)
        clean = re.sub(r'(/\*[\w\'\s\r\n\*]*\*/)|(//[\w\s\']*)|(<![\-\-\s\w\>/]*>)', '', text)
        return json.loads(clean)
    except:
        return None

def extract_api_domains(text):
    url_pattern = r'https?://([a-zA-Z0-9\-\.]+\.[a-zA-Z]{2,})(?::\d+)?'
    found = re.findall(url_pattern, text)
    domains = set()
    for d in found:
        d_lower = d.lower()
        if d_lower in SKIP_DOMAINS:
            continue
        skip = any(ext in d_lower for ext in SKIP_EXTENSIONS)
        if not skip:
            domains.add(d_lower)
    return list(domains)

def http_latency(domain, timeout=3.0):
    url = f"http://{domain}"
    headers = {'User-Agent': 'Mozilla/5.0'}
    req = urllib.request.Request(url, headers=headers)
    try:
        start = time.time()
        urllib.request.urlopen(req, timeout=timeout, context=get_context())
        return round((time.time() - start) * 1000)
    except:
        try:
            start = time.time()
            socket.create_connection((domain, 80), timeout=timeout).close()
            return round((time.time() - start) * 1000)
        except:
            return 9999

def is_banned(name):
    return any(b in name for b in BANNED_KEYWORDS)

def deep_test_source(source):
    name = source["name"]
    url = source["url"]
    if not url.startswith("http"):
        return None

    safe_print(f"\n--- [{name}] ---")
    try:
        raw = fetch_raw(url)
    except Exception as e:
        safe_print(f"    SKIP: 无法获取配置 ({type(e).__name__})")
        return None

    text = try_decode(raw)
    domains = extract_api_domains(text)

    if not domains:
        safe_print(f"    SKIP: 配置中未找到可测试的API域名")
        return None

    test_domains = domains[:8]
    safe_print(f"    发现 {len(domains)} 个内部域名，测试前 {len(test_domains)} 个...")

    latencies = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        future_map = {executor.submit(http_latency, d): d for d in test_domains}
        for future in concurrent.futures.as_completed(future_map):
            d = future_map[future]
            latencies[d] = future.result()

    all_lats = list(latencies.values())
    alive = [l for l in all_lats if l < 3000]
    if not alive:
        safe_print(f"    REJECTED: 所有 {len(all_lats)} 个内部API全部超时")
        return None

    avg_alive = sum(alive) / len(alive)
    alive_rate = len(alive) / len(all_lats) * 100

    for d, lat in sorted(latencies.items(), key=lambda x: x[1]):
        status = f"{lat}ms" if lat < 3000 else "TIMEOUT"
        safe_print(f"      {status:>10s} | {d}")

    safe_print(f"    结果: {len(alive)}/{len(all_lats)} 存活, 平均={avg_alive:.0f}ms, 存活率={alive_rate:.0f}%")

    if alive_rate >= 40 and avg_alive < 1500:
        safe_print(f"    >>> 通过 <<<")
        return {
            "name": name, "url": url,
            "avg_latency": round(avg_alive),
            "alive_rate": round(alive_rate),
        }
    else:
        safe_print(f"    >>> 淘汰 <<<")
        return None

def main():
    safe_print("=" * 60)
    safe_print("TVBox 黑名单过滤 + 极速网络深度测速引擎 (稳定版)")
    safe_print("=" * 60)

    collected = []
    seen = set()

    for s_url in SOURCE_URLS:
        safe_print(f"\n正在抓取多仓: {s_url}")
        data = fetch_json(s_url)
        if data and "urls" in data:
            for item in data["urls"]:
                name = item.get("name", "Unknown")
                url = item.get("url", "")
                if url and url not in seen and not is_banned(name):
                    collected.append({"name": name, "url": url})
                    seen.add(url)

    safe_print(f"\n第一步完成: 收集到 {len(collected)} 个无黑名单候选源")
    safe_print("\n" + "=" * 60)
    safe_print("开始深度测试...")
    safe_print("=" * 60)

    results = []
    for s in collected:
        res = deep_test_source(s)
        if res:
            results.append(res)

    results.sort(key=lambda x: x["avg_latency"])

    safe_print("\n" + "=" * 60)
    safe_print(f"最终结果: {len(results)} 个源通过深度测试")
    safe_print("=" * 60)

    output = {"urls": [{"url": r["url"], "name": r["name"]} for r in results]}
    with open("tvbox_clean.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=4)
    safe_print(f"\n已保存极速源到 tvbox_clean.json")

if __name__ == "__main__":
    main()
