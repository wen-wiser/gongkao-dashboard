# -*- coding: utf-8 -*-
"""
公考看板自动抓取脚本
由 GitHub Actions 每4小时运行一次；也可本地手动运行：python scripts/fetch_data.py
功能：巡检 sources.yaml 中的官网 → 发现新公告自动入库 → 更新“数据更新时间”
     → 有新公告或3天内有截止事项时，通过 PushPlus 推送微信提醒（可选）
"""
import json
import re
import html
import os
import yaml
import requests
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CN_TZ = timezone(timedelta(hours=8))

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
           "Accept-Language": "zh-CN,zh;q=0.9"}
TIMEOUT = 20
KEYWORDS = ["公告", "招考", "招聘", "职位表", "岗位表", "大纲", "成绩", "分数线",
            "报名", "笔试", "面试", "公示", "补录", "选调", "缴费", "准考证"]
KEEP_MAX = 400  # 公告流最多保留条数


def now_cn():
    return datetime.now(CN_TZ)


def fmt_display(dt):
    return dt.strftime("%Y年%m月%d日 %H:%M")


def load_json(name, default):
    p = DATA / name
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default


def save_json(name, obj):
    (DATA / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def abs_url(base, href):
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        p = urlparse(base)
        return f"{p.scheme}://{p.netloc}{href}"
    return None


def parse_date(text):
    m = re.search(r"(20\d{2})[-年/.](\d{1,2})[-月/.](\d{1,2})", text)
    if m:
        try:
            return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        except ValueError:
            return None
    return None


def fetch_source(src):
    """抓取单个数据源，返回 (src, items, error)"""
    try:
        r = requests.get(src["url"], headers=HEADERS, timeout=TIMEOUT)
        r.encoding = r.apparent_encoding or "utf-8"
        page = r.text
        items = []
        for m in re.finditer(r'<a[^>]+href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>', page, re.S | re.I):
            title = html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
            if not title or len(title) < 8 or len(title) > 130:
                continue
            if not any(k in title for k in KEYWORDS):
                continue
            url = abs_url(src["url"], m.group(1))
            if not url:
                continue
            # 取锚点附近文字里的日期（列表页日期一般贴着标题）
            seg = page[max(0, m.start() - 300): m.end() + 300]
            date = parse_date(seg)
            items.append({"title": title, "url": url, "date": date})
        # 去重
        seen, uniq = set(), []
        for it in items:
            if it["url"] not in seen:
                seen.add(it["url"])
                uniq.append(it)
        return src, uniq, None
    except Exception as e:
        return src, [], f"{type(e).__name__}: {e}"


def classify_owner(title, cfg):
    b_kw = cfg.get("owner_b_keywords", [])
    if any(k in title for k in b_kw):
        return ["B"]
    if any(k in title for k in cfg.get("owner_a_only_keywords", [])):
        return ["A"]
    return ["A", "B"]


def notify_pushplus(token, title, content):
    try:
        requests.post("http://www.pushplus.plus/send",
                      json={"token": token, "title": title, "content": content, "template": "html"},
                      timeout=15)
    except Exception:
        pass


def main():
    cfg = yaml.safe_load((ROOT / "scripts" / "sources.yaml").read_text(encoding="utf-8"))
    ts = now_cn()
    ts_display = fmt_display(ts)

    # 读取现有数据
    ann = load_json("announcements.json", {"note": "", "announcements": []})
    ev = load_json("events.json", {"note": "", "events": []})
    known_urls = {a.get("url") for a in ann.get("announcements", [])}
    known_titles = {a.get("title") for a in ann.get("announcements", [])}

    # 并行巡检所有源
    status, new_items = [], []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(fetch_source, s) for s in cfg.get("sources", [])]
        for f in as_completed(futs):
            src, items, err = f.result()
            status.append({"id": src["id"], "name": src["name"], "ok": err is None, "error": err})
            for it in items:
                if it["url"] in known_urls or it["title"] in known_titles:
                    continue
                known_urls.add(it["url"])
                new_items.append({
                    "id": f"auto-{src['id']}-{abs(hash(it['url'])) % 10**10}",
                    "title": it["title"],
                    "url": it["url"],
                    "date": it["date"] or ts.strftime("%Y-%m-%d"),
                    "region": src["region"],
                    "exam_type": src["exam"],
                    "owner": classify_owner(it["title"], cfg),
                    "discovered_display": f"{ts_display} 收录",
                    "source": src["name"],
                })

    if new_items:
        new_items.sort(key=lambda x: x["date"], reverse=True)
        ann["announcements"] = (new_items + ann["announcements"])[:KEEP_MAX]

    # 发现“XX省考公告/教师招聘公告”级别的标题时，自动生成确认事件进时间线
    existing_ev_titles = {e["title"] for e in ev.get("events", [])}
    new_events = []
    for it in new_items:
        t = it["title"]
        is_notice = ("公告" in t) and any(k in t for k in ["录用", "招录", "招聘", "考试", "公开"])
        if is_notice and t not in existing_ev_titles:
            existing_ev_titles.add(t)
            new_events.append({
                "id": it["id"] + "-ev",
                "title": f"【自动发现】{t[:60]}",
                "owner": it["owner"],
                "region": it["region"],
                "type": it["exam_type"],
                "action": "公告",
                "start": it["date"],
                "end": it["date"] + "T23:59",
                "status": "confirmed",
                "importance": "high",
                "link": it["url"],
                "note": f"来源：{it['source']}；报名/笔试时间以公告原文为准",
            })
    if new_events:
        ev["events"] = ev.get("events", []) + new_events

    # 3天内的截止事项（用于推送）
    urgent = []
    for e in ev.get("events", []):
        if e.get("status") in ("done",):
            continue
        try:
            end = datetime.strptime(str(e["end"])[:10], "%Y-%m-%d")
            days = (end.date() - ts.date()).days
            if 0 <= days <= 3:
                urgent.append(f"{e['title']}｜截止 {str(e['end'])[:10]}（还剩{days if days else '今天'}{'天' if days else ''}）")
        except Exception:
            pass

    # 微信推送（PushPlus，可选：在 GitHub Secrets 配置 PUSHPLUS_TOKEN）
    token = os.environ.get("PUSHPLUS_TOKEN", "").strip()
    today_key = ts.strftime("%Y-%m-%d")
    sent = load_json("notify_log.json", {})
    last_sent = sent.get("last_date", "")
    if token and (new_items or urgent) and last_sent != today_key:
        parts = []
        if new_items:
            rows = "".join(f"<p>🆕 <a href='{i['url']}'>{i['title']}</a></p>" for i in new_items[:8])
            parts.append(f"<h3>📢 新公告 {len(new_items)} 条</h3>{rows}")
        if urgent:
            rows = "".join(f"<p>⏰ {u}</p>" for u in urgent)
            parts.append(f"<h3>🔥 3天内截止</h3>{rows}")
        notify_pushplus(token, f"公考看板更新 {today_key}", "".join(parts))
        sent["last_date"] = today_key
        save_json("notify_log.json", sent)

    # 写回 meta（页面上展示的“更新时间”就来自这里）
    ok_n = sum(1 for s in status if s["ok"])
    save_json("meta.json", {
        "last_updated_display": ts_display,
        "last_updated_iso": ts.isoformat(timespec="minutes"),
        "next_check_hours": 4,
        "run_ok": True,
        "run_at_utc": datetime.utcnow().isoformat(timespec="minutes") + "Z",
        "sources_ok": ok_n,
        "sources_total": len(status),
        "sources_status": status,
    })
    save_json("announcements.json", ann)
    save_json("events.json", ev)

    print(f"[OK] 更新时间 {ts_display}")
    print(f"[OK] 巡检源 {ok_n}/{len(status)} 成功；新公告 {len(new_items)} 条；新事件 {len(new_events)} 条")
    for s in status:
        if not s["ok"]:
            print(f"  [跳过] {s['name']}: {s['error']}")


if __name__ == "__main__":
    main()
