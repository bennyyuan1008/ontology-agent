# -*- coding: utf-8 -*-
"""干跑：解析 langgraph 依赖树（只拉元数据，不下载），统计包数与体积。"""
import json, re, time, urllib.request

def _open(req, tries=4):
    for i in range(tries):
        try:
            return urllib.request.urlopen(req, timeout=30)
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(3 * (i + 1))

def fetch(name):
    req = urllib.request.Request(f"https://pypi.org/pypi/{name}/json", headers={"User-Agent": "curl/8.0"})
    with _open(req) as r:
        return json.load(r)

def parse_requires(rd):
    names = []
    for line in rd or []:
        if "extra ==" in line:
            continue
        m = re.match(r"^([A-Za-z0-9_.-]+)", line)
        if m:
            names.append(m.group(1).lower())
    return names

def wheel_size(meta):
    for f in meta.get("urls", []):
        fn = f["filename"]
        if fn.endswith(".whl"):
            if "cp310" in fn and "win_amd64" in fn:
                return f.get("size", 0)
            if "abi3" in fn and "win_amd64" in fn:
                return f.get("size", 0)
            if fn.startswith("py3-none-any"):
                return f.get("size", 0)
    return 0

resolved = {}
def resolve(name):
    name = name.lower()
    if name in resolved:
        return
    meta = fetch(name)
    resolved[name] = meta
    for dep in parse_requires(meta.get("info", {}).get("requires_dist")):
        resolve(dep)

resolve("langgraph")
total = 0
print(f"依赖包数: {len(resolved)}")
print(f"{'包名':<24}{'版本':<14}{'wheel大小(KB)'}")
for name, meta in sorted(resolved.items()):
    ver = meta["info"]["version"]
    sz = wheel_size(meta)
    total += sz
    print(f"{name:<24}{ver:<14}{sz//1024}")
print(f"估算下载总量: {total//1024//1024} MB")
