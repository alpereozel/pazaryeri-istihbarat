from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, HttpUrl
from urllib.parse import urlparse, urljoin
from datetime import datetime, timezone
import sqlite3, json, re, requests, html as html_lib, math, os, base64
from bs4 import BeautifulSoup

DB = "marketintel.db"
app = FastAPI(title="Pazaryeri İstihbarat", version="1.0.0")


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    c = db()
    c.execute("""CREATE TABLE IF NOT EXISTS products(
        id TEXT PRIMARY KEY,
        marketplace TEXT,
        url TEXT,
        title TEXT,
        seller TEXT,
        brand TEXT,
        category TEXT,
        category_url TEXT,
        price REAL,
        list_price REAL,
        rating REAL,
        review_count INTEGER,
        commission_rate REAL DEFAULT 0,
        created_at TEXT
    )""")
    # Existing V6 DBs may not have category_url; migrate safely.
    cols = {r[1] for r in c.execute("PRAGMA table_info(products)").fetchall()}
    if "category_url" not in cols:
        c.execute("ALTER TABLE products ADD COLUMN category_url TEXT")
    c.execute("""CREATE TABLE IF NOT EXISTS snapshots(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id TEXT,
        captured_at TEXT,
        price REAL,
        stock INTEGER,
        review_count INTEGER,
        rating REAL,
        raw_json TEXT
    )""")
    c.commit(); c.close()


init_db()


class AnalyzeRequest(BaseModel):
    url: HttpUrl


class SnapshotRequest(BaseModel):
    product_id: str
    price: float | None = None
    stock: int | None = None
    review_count: int | None = None
    rating: float | None = None


def marketplace(url):
    host = urlparse(str(url)).netloc.lower()
    if "trendyol.com" in host:
        return "trendyol"
    if "hepsiburada.com" in host:
        return "hepsiburada"
    return "unknown"


def product_id_from_url(url):
    m = re.search(r"-p-(\d+)", str(url))
    return m.group(1) if m else re.sub(r"[^a-zA-Z0-9]", "", str(url))[-48:]


def clean_text(v):
    if v is None:
        return None
    return re.sub(r"\s+", " ", html_lib.unescape(str(v))).strip()


def to_float(v):
    if v is None:
        return None
    s = re.sub(r"[^\d,\.\-]", "", str(v).strip())
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except Exception:
        return None


def to_int(v):
    if v is None:
        return None
    m = re.search(r"\d[\d\.\s,]*", str(v))
    if not m:
        return None
    s = m.group(0).replace(".", "").replace(" ", "").replace(",", "")
    try:
        return int(s)
    except Exception:
        return None


def first_jsonld(soup):
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        candidates = []
        if isinstance(obj, list):
            candidates = obj
        elif isinstance(obj, dict):
            graph = obj.get("@graph")
            candidates = graph if isinstance(graph, list) else [obj]
        for item in candidates:
            if isinstance(item, dict):
                typ = item.get("@type")
                if typ == "Product" or (isinstance(typ, list) and "Product" in typ) or "offers" in item or "aggregateRating" in item:
                    return item
    return {}


def find_json_value(text, keys):
    for key in keys:
        patterns = [
            rf'"{re.escape(key)}"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"',
            rf'"{re.escape(key)}"\s*:\s*([0-9]+(?:[.,][0-9]+)?)',
            rf"'{re.escape(key)}'\s*:\s*'([^']+)'",
        ]
        for pat in patterns:
            m = re.search(pat, text, re.I)
            if m:
                return clean_text(m.group(1))
    return None


def find_label_number(text, labels):
    for label in labels:
        m = re.search(re.escape(label) + r".{0,120}?(\d[\d\.,]*)", text, re.I | re.S)
        if m:
            return m.group(1)
    return None


def detect_purchase_signal(text):
    patterns = [
        r'(?:son\s*24\s*saatte|son\s*24\s*saat içinde)\s*(\d+)\s*(?:kişi|adet)\s*(?:aldı|satın aldı)',
        r'(\d+)\s*(?:kişi|adet)\s*(?:bu ürünü\s*)?(?:aldı|satın aldı)\s*(?:son\s*24\s*saatte)?',
        r'(?:last\s*24\s*hours)\s*(\d+)\s*(?:people|orders|units)',
    ]
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            return int(m.group(1))
    return None


def detect_badge(text):
    patterns = [
        r'En\s*Çok\s*Satan\s*(\d+)?\.?\s*Ürün',
        r'En\s*çok\s*satan\s*(\d+)?\.?\s*ürün',
        r'Best\s*Seller\s*(\d+)?',
    ]
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            return "En Çok Satan", int(m.group(1)) if m.group(1) else None
    return None, None


def detect_sold_signal(text):
    patterns = [
        r'(\d[\d\.,]*)\s*\+?\s*(?:adet|ürün)\s*(?:satıldı|satıldı\b)',
        r'(\d[\d\.,]*)\s*\+?\s*(?:satış|satıldı)',
        r'(?:sold|orders)\s*[:]?\s*(\d[\d\.,]*)\+?',
    ]
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            return to_int(m.group(1))
    return None


def breadcrumb_category(soup):
    # Try schema.org breadcrumbs first.
    crumbs = []
    for item in soup.select('[itemtype*="BreadcrumbList"] [itemprop="itemListElement"]'):
        name = item.select_one('[itemprop="name"]')
        if name:
            txt = clean_text(name.get_text(" ", strip=True))
            if txt and txt.lower() not in {"anasayfa", "home"} and txt not in crumbs:
                crumbs.append(txt)
    if len(crumbs) >= 2:
        return " > ".join(crumbs[-4:])

    # Common breadcrumb classes/ARIA labels.
    candidates = soup.select('nav[aria-label*="breadcrumb" i], [class*="breadcrumb" i], [data-testid*="breadcrumb" i]')
    for box in candidates:
        parts = []
        for a in box.find_all(["a", "span"]):
            txt = clean_text(a.get_text(" ", strip=True))
            if txt and len(txt) < 100 and txt.lower() not in {"anasayfa", "home"} and txt not in parts:
                parts.append(txt)
        if len(parts) >= 2:
            return " > ".join(parts[-4:])
    return None


def breadcrumb_url(soup, base_url):
    for item in soup.select('[itemtype*="BreadcrumbList"] [itemprop="itemListElement"]'):
        a = item.select_one('a[itemprop="item"]') or item.select_one("a[href]")
        if a and a.get("href"):
            href = urljoin(base_url, a.get("href"))
            if "/" in urlparse(href).path and "trendyol.com" in urlparse(href).netloc:
                return href
    for box in soup.select('nav[aria-label*="breadcrumb" i], [class*="breadcrumb" i], [data-testid*="breadcrumb" i]'):
        links = box.find_all("a", href=True)
        for a in reversed(links):
            href = urljoin(base_url, a.get("href"))
            if "trendyol.com" in urlparse(href).netloc and "/" in urlparse(href).path:
                return href
    return None


def detect_category_rank(text):
    # Product pages sometimes expose a rank label. Keep this separate from sales estimate.
    patterns = [
        r'(?:En\s*Çok\s*Satan|En\s*çok\s*satan)\s*(\d+)\.?\s*Ürün',
        r'(?:En\s*Çok\s*Satan|En\s*çok\s*satan)\s*#\s*(\d+)',
    ]
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            return int(m.group(1))
    return None



def normalize_title(s):
    s=clean_text(s or '').lower()
    s=re.sub(r'[^a-z0-9çğıöşü ]+',' ',s)
    return re.sub(r'\s+',' ',s).strip()



COMMISSION_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "commission_rules.json")
DEFAULT_REPO = os.getenv("GITHUB_REPO", "alperoezel/pazaryeri-istihbarat")


def commission_for_category(category):
    if not category:
        return {"rate": None, "label": None, "source": None, "source_url": None, "note": "Kategori komisyonu otomatik eşleştirilemedi."}
    try:
        rules = json.loads(open(COMMISSION_FILE, encoding="utf-8").read()).get("rules", [])
    except Exception:
        rules = []
    text = clean_text(category).lower()
    for rule in rules:
        if any(k.lower() in text for k in rule.get("keywords", [])):
            return {
                "rate": rule.get("rate"),
                "label": rule.get("label"),
                "source": rule.get("source"),
                "source_url": rule.get("source_url"),
                "note": "Referans orandır; satıcı panelindeki sözleşmeli oran kampanya/alt kategori nedeniyle farklı olabilir."
            }
    return {"rate": None, "label": None, "source": None, "source_url": None, "note": "Kategori komisyonu otomatik eşleştirilemedi."}


def github_headers():
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        return None
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


def github_file(path):
    headers = github_headers()
    if not headers:
        return None, None
    repo = os.getenv("GITHUB_REPO", DEFAULT_REPO)
    r = requests.get(f"https://api.github.com/repos/{repo}/contents/{path}", headers=headers, timeout=15)
    if r.status_code != 200:
        return None, None
    obj = r.json()
    try:
        raw = base64.b64decode(obj["content"]).decode("utf-8")
        return json.loads(raw), obj.get("sha")
    except Exception:
        return None, obj.get("sha")


def github_put_json(path, payload, sha=None, message="Pazaryeri takip listesi güncellendi"):
    headers = github_headers()
    if not headers:
        raise HTTPException(503, "Takip özelliği için Render Environment Variables bölümüne GITHUB_TOKEN eklenmeli.")
    repo = os.getenv("GITHUB_REPO", DEFAULT_REPO)
    content = base64.b64encode(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")).decode("ascii")
    body = {"message": message, "content": content, "branch": os.getenv("GITHUB_BRANCH", "main")}
    if sha:
        body["sha"] = sha
    r = requests.put(f"https://api.github.com/repos/{repo}/contents/{path}", headers=headers, json=body, timeout=20)
    if r.status_code not in (200, 201):
        raise HTTPException(502, f"GitHub veri kaydı başarısız: {r.text[:300]}")
    return r.json()


def tracked_urls_remote():
    data, _ = github_file("data/tracked_urls.json")
    return data if isinstance(data, list) else []


def remote_history_for(pid):
    repo = os.getenv("GITHUB_REPO", DEFAULT_REPO)
    url = f"https://raw.githubusercontent.com/{repo}/{os.getenv('TRACKING_BRANCH','tracker-data')}/data/tracking_history.json"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return []
        obj = r.json()
        return obj.get(pid, []) if isinstance(obj, dict) else []
    except Exception:
        return []

def category_page_rank(category_url, product_title):
    """Try to recover the marketplace's explicit 'En Çok Satan #N' label from the category page."""
    if not category_url or 'trendyol.com' not in urlparse(category_url).netloc:
        return None, None
    try:
        headers={
            'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36',
            'Accept-Language':'tr-TR,tr;q=0.9,en;q=0.8'
        }
        rr=requests.get(category_url,headers=headers,timeout=15)
        if rr.status_code!=200:
            return None,None
        soup=BeautifulSoup(rr.text,'html.parser')
        page_text=clean_text(soup.get_text(' ',strip=True)) or ''
        # First, use DOM cards where the product title and badge live close together.
        target=normalize_title(product_title)
        best=None
        for node in soup.find_all(['a','div','li','article']):
            txt=clean_text(node.get_text(' ',strip=True)) or ''
            nt=normalize_title(txt)
            if target and len(target)>20 and target[:70] in nt:
                m=re.search(r'En\s*Çok\s*Satan\s*(\d+)?\.?\s*Ürün',txt,re.I)
                if m:
                    return int(m.group(1)) if m.group(1) else 1, 'En Çok Satan'
                # Some category cards have the badge and title separated in parent text.
                parent=clean_text(node.parent.get_text(' ',strip=True)) if node.parent else ''
                m=re.search(r'En\s*Çok\s*Satan\s*(\d+)?\.?\s*Ürün',parent,re.I)
                if m:
                    return int(m.group(1)) if m.group(1) else 1, 'En Çok Satan'
        # Fallback: search snippets around a distinctive product-title fragment.
        frag=target[:80] if target else ''
        if frag:
            pos=normalize_title(page_text).find(frag)
            if pos>=0:
                window=page_text[max(0,pos-300):pos+300]
                m=re.search(r'En\s*Çok\s*Satan\s*(\d+)?\.?\s*Ürün',window,re.I)
                if m:
                    return int(m.group(1)) if m.group(1) else 1, 'En Çok Satan'
        return None,None
    except Exception:
        return None,None

def parse_public_product(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.7,en;q=0.5",
        "Cache-Control": "no-cache",
    }
    r = requests.get(str(url), headers=headers, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    text = clean_text(soup.get_text(" ", strip=True)) or ""
    ld = first_jsonld(soup)

    title = clean_text(ld.get("name"))
    og = soup.find("meta", property="og:title")
    if not title and og:
        title = clean_text(og.get("content"))
    if not title and soup.title:
        title = clean_text(soup.title.get_text())

    brand = ld.get("brand")
    brand = clean_text(brand.get("name")) if isinstance(brand, dict) else clean_text(brand)
    brand = brand or find_json_value(r.text, ["brandName", "brandTitle", "brand"])

    offers = ld.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    price = to_float(offers.get("price")) if isinstance(offers, dict) else None
    list_price = None
    if price is None:
        for pat in [r'"salePrice"\s*:\s*([0-9]+(?:[.,][0-9]+)?)', r'"discountedPrice"\s*:\s*([0-9]+(?:[.,][0-9]+)?)']:
            m = re.search(pat, r.text, re.I)
            if m:
                price = to_float(m.group(1)); break
    for pat in [r'"listPrice"\s*:\s*([0-9]+(?:[.,][0-9]+)?)', r'"originalPrice"\s*:\s*([0-9]+(?:[.,][0-9]+)?)', r'"struckPrice"\s*:\s*([0-9]+(?:[.,][0-9]+)?)']:
        m = re.search(pat, r.text, re.I)
        if m:
            list_price = to_float(m.group(1)); break

    ar = ld.get("aggregateRating") or {}
    rating = to_float(ar.get("ratingValue")) if isinstance(ar, dict) else None
    review_count = to_int(ar.get("reviewCount") or ar.get("ratingCount")) if isinstance(ar, dict) else None
    if rating is None:
        rating = to_float(find_label_number(text, ["Değerlendirme", "Puan", "rating"]))
    if review_count is None:
        review_count = to_int(find_label_number(text, ["Yorum", "Değerlendirme", "reviews"]))

    seller = None
    if isinstance(offers, dict):
        s = offers.get("seller")
        seller = clean_text(s.get("name")) if isinstance(s, dict) else clean_text(s)
    seller = seller or find_json_value(r.text, ["merchantName", "sellerName", "merchantTitle", "sellerTitle", "merchant_name"])
    if not seller:
        m = re.search(r'Bu ürün\s+([A-Za-z0-9ÇĞİÖŞÜçğıöşü .&_-]{2,80})\s+tarafından gönderilecektir', text, re.I)
        if m:
            seller = clean_text(m.group(1))

    category = clean_text(ld.get("category"))
    category = category or find_json_value(r.text, ["categoryName", "categoryTitle", "webCategory", "categoryPath", "category_name"])
    category_url = None
    if not category:
        category = breadcrumb_category(soup)
    category_url = breadcrumb_url(soup, str(url))

    # A stronger fallback: inspect anchors whose href looks like a category page.
    if not category_url and "trendyol.com" in urlparse(str(url)).netloc:
        for a in soup.find_all("a", href=True):
            txt = clean_text(a.get_text(" ", strip=True))
            href = urljoin(str(url), a.get("href"))
            if txt and 2 <= len(txt) <= 80 and "trendyol.com" in urlparse(href).netloc:
                path = urlparse(href).path.lower()
                if "/sr" in path or (path.count("/") >= 1 and "-y-" in path):
                    category_url = href
                    if not category:
                        category = txt
                    break

    purchase_24h = detect_purchase_signal(text)
    badge, badge_rank = detect_badge(text)
    category_rank = detect_category_rank(text) or badge_rank
    category_rank_source = 'ürün sayfası' if category_rank else None
    if category_rank is None and category_url and category:
        page_rank, page_badge = category_page_rank(category_url, title)
        if page_rank is not None:
            category_rank = page_rank
            category_rank_source = 'kategori sayfası'
            if not badge:
                badge = page_badge
                badge_rank = page_rank
    sold_total = detect_sold_signal(text)
    stock = None
    for pat in [r'"(?:stock|quantity|availableQuantity)"\s*:\s*(\d+)', r'"(?:stockCount|inventory)"\s*:\s*(\d+)']:
        m = re.search(pat, r.text, re.I)
        if m:
            stock = int(m.group(1)); break

    return {
        "title": title, "brand": brand, "seller": seller, "category": category, "category_url": category_url,
        "price": price, "list_price": list_price, "rating": rating, "review_count": review_count,
        "purchase_signal_24h": purchase_24h, "sales_badge": badge, "sales_badge_rank": badge_rank,
        "category_rank": category_rank, "category_rank_source": category_rank_source, "sold_total_signal": sold_total, "stock": stock,
        "http_status": r.status_code, "bytes": len(r.content),
    }


def get_history(product_id):
    c = db()
    rows = [dict(x) for x in c.execute("SELECT * FROM snapshots WHERE product_id=? ORDER BY captured_at", (product_id,)).fetchall()]
    c.close()
    remote = remote_history_for(product_id)
    merged = rows + remote
    merged.sort(key=lambda x: x.get("captured_at", ""))
    # Deduplicate by timestamp.
    seen = set(); out = []
    for row in merged:
        key = row.get("captured_at")
        if key in seen: continue
        seen.add(key); out.append(row)
    return out


def time_series_signal(history):
    if len(history) < 2:
        return None
    first, last = history[0], history[-1]
    try:
        t0 = datetime.fromisoformat(first["captured_at"])
        t1 = datetime.fromisoformat(last["captured_at"])
        days = (t1 - t0).total_seconds() / 86400
    except Exception:
        return None
    if days < 0.5:
        return None

    review_delta = None
    if first.get("review_count") is not None and last.get("review_count") is not None:
        review_delta = max(0, last["review_count"] - first["review_count"])

    stock_delta = None
    if first.get("stock") is not None and last.get("stock") is not None:
        stock_delta = max(0, first["stock"] - last["stock"])

    return {
        "days": days,
        "review_delta": review_delta,
        "review_velocity": (review_delta / days) if review_delta is not None else None,
        "observed_stock_decrease": stock_delta,
        "stock_velocity": (stock_delta / days) if stock_delta is not None else None,
    }


def trend_from_history(history):
    ts = time_series_signal(history)
    if not ts:
        return {"label": "Yeterli zaman serisi yok", "direction": "unknown", "detail": "En az iki ölçüm ve anlamlı zaman aralığı gerekir."}
    rv = ts.get("review_velocity")
    sv = ts.get("stock_velocity")
    if rv is not None and rv > 0:
        return {"label": "Yükselen", "direction": "up", "detail": f"Günde yaklaşık {rv:.2f} yeni yorum gözlendi."}
    if sv is not None and sv > 0:
        return {"label": "Hareket var", "direction": "up", "detail": f"Gözlenen stok azalışı yaklaşık {sv:.1f}/gün; satış olarak kabul edilmedi."}
    return {"label": "Yatay / belirsiz", "direction": "flat", "detail": "Son ölçümlerde güçlü yön sinyali yok."}


def estimate_from_current(data, history):
    """Separate popularity potential from sales quantity. Sales quantity is always an estimate unless an explicit public signal exists."""
    p24 = data.get("purchase_signal_24h")
    rank = data.get("category_rank") or data.get("sales_badge_rank")
    reviews = data.get("review_count") or 0
    ts = time_series_signal(history)

    if p24 is not None:
        lo = max(1, math.floor(p24 * 0.90))
        hi = max(lo, math.ceil(p24 * 1.10))
        return {
            "daily_estimate": p24, "daily_low": lo, "daily_high": hi,
            "confidence": 92, "basis": "Açık 24 saatlik satış sinyali", "score": min(100, 85 + min(15, p24 // 10)),
            "reasons": ["Sayfada açık 24 saatlik satın alma sinyali bulundu.", "Bu sinyal herkese açık olduğu için satış miktarında en güçlü kanıt olarak kullanıldı."],
            "evidence_level": "strong", "trend": trend_from_history(history)
        }

    # If we have measured review velocity over time, use it as the strongest indirect signal.
    if ts and ts.get("review_velocity") is not None and ts["days"] >= 1 and ts["review_velocity"] > 0:
        # Review -> order conversion is uncertain, so use a deliberately broad prior.
        # Approx. 1 review per 20-60 orders, bounded to prevent runaway estimates.
        center = max(1, round(ts["review_velocity"] * 40))
        center = min(center, 300)
        lo = max(1, math.floor(center * 0.55))
        hi = max(lo, math.ceil(center * 1.45))
        confidence = min(65, 35 + int(min(20, ts["days"] * 2)) + (5 if ts["review_delta"] and ts["review_delta"] >= 10 else 0))
        reasons = [
            f"İzleme süresinde günde yaklaşık {ts['review_velocity']:.2f} yeni yorum gözlendi.",
            "Yorum→sipariş dönüşümü bir model varsayımıdır; gerçek sipariş değildir."
        ]
        if ts.get("stock_velocity"):
            reasons.append(f"Gözlenen stok azalışı yaklaşık {ts['stock_velocity']:.1f}/gün; yalnızca yardımcı sinyal olarak kullanıldı.")
        return {
            "daily_estimate": center, "daily_low": lo, "daily_high": hi,
            "confidence": confidence, "basis": "Yorum hızı + zaman serisi", "score": popularity_score(data),
            "reasons": reasons, "evidence_level": "medium", "trend": trend_from_history(history)
        }

    if rank is not None:
        # Rank is a popularity signal, not a direct order count. Review volume is used only to scale a prior.
        rank_factor = 1.0 / math.sqrt(max(1, rank))
        base = max(3, reviews / 100.0) if reviews else 8
        center = max(3, round(base * (1.15 if rank <= 3 else 1.0 if rank <= 10 else 0.85 if rank <= 25 else 0.70 if rank <= 50 else 0.55)))
        center = min(center, 250)
        # Keep the band useful but honest while evidence is indirect.
        band = 0.30 if rank <= 10 else 0.35
        lo = max(1, math.floor(center * (1 - band)))
        hi = max(lo, math.ceil(center * (1 + band)))
        return {
            "daily_estimate": center, "daily_low": lo, "daily_high": hi,
            "confidence": 32 if rank <= 10 else 28,
            "basis": "Kategori satış sırası + yorum hacmi", "score": popularity_score(data),
            "reasons": [
                f"Kategori/satış sırası sinyali: #{rank}.",
                f"Toplam yorum: {reviews}.",
                "Kategori sırası satış adedini doğrudan göstermez; yorum hacmiyle birlikte satış öncülü olarak kullanıldı.",
                "Gerçek sipariş verisi olmadığı için güven skoru sınırlı tutuldu."
            ],
            "evidence_level": "medium-low", "trend": trend_from_history(history)
        }

    if reviews:
        center = max(1, min(80, round(reviews / 120)))
        lo = max(1, math.floor(center * 0.65))
        hi = max(lo, math.ceil(center * 1.35))
        return {
            "daily_estimate": center, "daily_low": lo, "daily_high": hi,
            "confidence": 18, "basis": "Yorum hacmi ön tahmini", "score": popularity_score(data),
            "reasons": [f"Toplam yorum: {reviews}.", "Açık satış sinyali ve kategori sırası bulunamadı.", "Ürün yaşı ve yorum dönüşüm oranı bilinmediği için güven düşüktür."],
            "evidence_level": "weak", "trend": trend_from_history(history)
        }

    return {
        "daily_estimate": None, "daily_low": None, "daily_high": None, "confidence": 5,
        "basis": "Yeterli açık veri yok", "score": 10,
        "reasons": ["Günlük satış için kullanılabilir açık sinyal bulunamadı."], "evidence_level": "none",
        "trend": trend_from_history(history)
    }


def popularity_score(data):
    """0-100 popularity potential. This is intentionally separate from estimated unit sales."""
    rank = data.get("category_rank") or data.get("sales_badge_rank")
    reviews = data.get("review_count") or 0
    rating = data.get("rating") or 0
    score = 20
    if rank is not None:
        if rank == 1: score += 60
        elif rank <= 3: score += 55
        elif rank <= 10: score += 48
        elif rank <= 25: score += 38
        elif rank <= 50: score += 30
        elif rank <= 100: score += 22
        else: score += 12
    elif reviews:
        score += min(35, round(math.log10(max(10, reviews)) * 9))
    if rating >= 4.5: score += 8
    elif rating >= 4.0: score += 5
    return min(100, score)


def make_periods(est, price):
    out = {}
    for key, days in (("daily", 1), ("3_days", 3), ("7_days", 7), ("monthly", 30)):
        if est.get("daily_low") is None or not price:
            out[key] = {"sales_low": None, "sales_high": None, "revenue_low": None, "revenue_high": None}
            continue
        lo = est["daily_low"] * days
        hi = est["daily_high"] * days
        out[key] = {
            "sales_low": lo, "sales_high": hi,
            "revenue_low": round(lo * price, 2), "revenue_high": round(hi * price, 2),
        }
    return out


@app.get("/", response_class=HTMLResponse)
def home():
    with open("app/static/index.html", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest):
    url = str(req.url)
    mp = marketplace(url)
    if mp == "unknown":
        raise HTTPException(400, "Şimdilik sadece Trendyol ve Hepsiburada URL'leri destekleniyor.")
    pid = f"{mp}:{product_id_from_url(url)}"
    try:
        data = parse_public_product(url)
    except Exception as e:
        raise HTTPException(502, f"Ürün sayfası okunamadı: {e}")

    c = db()
    c.execute("""INSERT INTO products(id,marketplace,url,title,seller,brand,category,category_url,price,list_price,rating,review_count,created_at)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                 title=excluded.title,seller=excluded.seller,brand=excluded.brand,category=excluded.category,
                 category_url=excluded.category_url,price=excluded.price,list_price=excluded.list_price,
                 rating=excluded.rating,review_count=excluded.review_count""",
              (pid, mp, url, data["title"], data["seller"], data["brand"], data["category"], data.get("category_url"),
               data["price"], data["list_price"], data["rating"], data["review_count"], datetime.now(timezone.utc).isoformat()))
    now = datetime.now(timezone.utc).isoformat()
    c.execute("""INSERT INTO snapshots(product_id,captured_at,price,stock,review_count,rating,raw_json)
                 VALUES(?,?,?,?,?,?,?)""",
              (pid, now, data["price"], data.get("stock"), data["review_count"], data["rating"], json.dumps(data, ensure_ascii=False)))
    c.commit(); c.close()

    history = get_history(pid)
    est = estimate_from_current(data, history)
    commission = commission_for_category(data.get("category"))
    tracked = url in tracked_urls_remote()
    rate = commission.get("rate")
    gross = data.get("price")
    commission_amount = round(gross * rate / 100, 2) if gross is not None and rate is not None else None
    return {
        "product_id": pid, "marketplace": mp, "product": data,
        "estimate": est, "periods": make_periods(est, data.get("price")),
        "history": {"snapshots": len(history), "time_series": time_series_signal(history)},
        "trend": est.get("trend"),
        "commission": {**commission, "amount_on_current_price": commission_amount},
        "tracked": tracked,
        "message": "Tahmin gerçek sipariş verisi değildir. Güçlü açık satış sinyali varsa önceliklidir; aksi halde kategori sırası, yorum hızı ve diğer açık sinyaller birlikte değerlendirilir.",
    }


@app.post("/api/snapshots")
def add_snapshot(req: SnapshotRequest):
    c = db()
    if not c.execute("SELECT 1 FROM products WHERE id=?", (req.product_id,)).fetchone():
        raise HTTPException(404, "Ürün bulunamadı. Önce /api/analyze çağırın.")
    now = datetime.now(timezone.utc).isoformat()
    c.execute("""INSERT INTO snapshots(product_id,captured_at,price,stock,review_count,rating,raw_json)
                 VALUES(?,?,?,?,?,?,?)""",
              (req.product_id, now, req.price, req.stock, req.review_count, req.rating, json.dumps(req.model_dump(), ensure_ascii=False)))
    c.commit(); c.close()
    return {"ok": True, "captured_at": now}



@app.post("/api/track")
def track(req: AnalyzeRequest):
    url = str(req.url)
    if marketplace(url) == "unknown":
        raise HTTPException(400, "Sadece Trendyol ve Hepsiburada URL'leri destekleniyor.")
    urls = tracked_urls_remote()
    if url not in urls:
        urls.append(url)
        github_put_json("data/tracked_urls.json", sorted(set(urls)), message="Ürün takibe alındı")
    return {"ok": True, "tracked": True, "count": len(urls), "message": "Ürün 2 saatlik otomatik takip listesine eklendi."}


@app.delete("/api/track")
def untrack(req: AnalyzeRequest):
    url = str(req.url)
    urls = [x for x in tracked_urls_remote() if x != url]
    github_put_json("data/tracked_urls.json", urls, message="Ürün takipten çıkarıldı")
    return {"ok": True, "tracked": False, "count": len(urls)}


@app.get("/api/tracked")
def tracked():
    return {"urls": tracked_urls_remote()}


@app.get("/api/products")
def products():
    c = db()
    rows = [dict(x) for x in c.execute("SELECT * FROM products ORDER BY created_at DESC").fetchall()]
    c.close()
    return rows
