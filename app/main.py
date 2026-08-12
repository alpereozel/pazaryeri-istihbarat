from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, HttpUrl
from urllib.parse import urlparse, urljoin
from datetime import datetime, timezone
import sqlite3, json, re, requests, html as html_lib, math
from bs4 import BeautifulSoup

DB = "marketintel.db"
app = FastAPI(title="Pazaryeri İstihbarat", version="0.8.0")


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
    return rows


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


def estimate_from_current(data, history):
    """Evidence-weighted estimate. Never treats total stock as sales."""
    p24 = data.get("purchase_signal_24h")
    if p24 is not None:
        # Explicit public signal is the only case where we can give a tight estimate.
        lo = max(1, math.floor(p24 * 0.90))
        hi = max(lo, math.ceil(p24 * 1.10))
        return {
            "daily_estimate": p24, "daily_low": lo, "daily_high": hi,
            "confidence": 92, "basis": "Açık 24 saatlik satış sinyali", "score": min(100, 80 + min(20, p24 // 5)),
            "reasons": ["Sayfada açık 24 saatlik satın alma sinyali bulundu."],
            "evidence_level": "strong",
        }

    ts = time_series_signal(history)
    if ts and ts.get("review_velocity") is not None and ts["review_velocity"] > 0 and ts["days"] >= 1:
        # Conservative review-to-order conversion. It is a model assumption, not a fact.
        # 2% of daily reviews as an order proxy, bounded to avoid extreme estimates.
        daily = max(1, round(ts["review_velocity"] / 0.02))
        daily = min(daily, 250)
        lo = max(1, round(daily * 0.85))
        hi = max(lo, math.ceil(daily * 1.15))
        confidence = min(78, 45 + int(min(30, ts["days"] * 2)) + (8 if ts["review_delta"] >= 10 else 0))
        reasons = [f"İzleme süresinde günde yaklaşık {ts['review_velocity']:.2f} yeni yorum gözlendi.", "Yorum→sipariş dönüşümü model varsayımıdır; gerçek sipariş değildir."]
        if ts.get("stock_velocity"):
            reasons.append(f"Gözlenen stok azalışı yaklaşık {ts['stock_velocity']:.1f} adet/gün; yalnızca yardımcı sinyal olarak kullanıldı.")
        return {
            "daily_estimate": daily, "daily_low": lo, "daily_high": hi,
            "confidence": confidence, "basis": "Yorum hızı + zaman serisi", "score": min(100, 55 + confidence // 2),
            "reasons": reasons, "evidence_level": "medium",
        }

    rank = data.get("category_rank") or data.get("sales_badge_rank")
    reviews = data.get("review_count") or 0
    if rank is not None:
        # Rank is used to create a score and a prior, not a claimed exact sales number.
        # Rank supplies a strong popularity prior, while review volume scales it.
        # This is intentionally a model estimate, not a claimed order count.
        if rank <= 3:
            daily = max(8, round(reviews / 90))
            score = 88
        elif rank <= 10:
            daily = max(5, round(reviews / 110))
            score = 82
        elif rank <= 25:
            daily = max(3, round(reviews / 130))
            score = 76
        elif rank <= 50:
            daily = max(2, round(reviews / 150))
            score = 68
        elif rank <= 100:
            daily = max(1, round(reviews / 180))
            score = 60
        else:
            daily = max(1, round(reviews / 220))
            score = 52
        lo = max(1, round(daily * 0.9))
        hi = max(lo, math.ceil(daily * 1.1))
        return {
            "daily_estimate": daily, "daily_low": lo, "daily_high": hi,
            "confidence": max(30, score - 25), "basis": "Kategori satış sırası + yorum hacmi",
            "score": score, "reasons": [f"Kategori/satış sırası sinyali: #{rank}.", f"Toplam yorum: {reviews}.", "Kategori sırası satış adedini doğrudan göstermez; yorum hacmiyle birlikte model öncülü olarak kullanıldı."],
            "evidence_level": "medium-low",
        }

    if reviews:
        # No rank/no explicit purchase signal: narrow enough to be readable, but clearly low confidence.
        daily = max(1, min(50, round(reviews / 70)))
        lo = max(1, round(daily * 0.85))
        hi = max(lo, math.ceil(daily * 1.15))
        return {
            "daily_estimate": daily, "daily_low": lo, "daily_high": hi,
            "confidence": 25, "basis": "Yorum hacmi ön tahmini", "score": 30,
            "reasons": [f"Toplam yorum: {reviews}.", "Açık satış sinyali ve kategori sırası bulunamadı.", "Ürün yaşı bilinmediği için güven düşüktür."],
            "evidence_level": "weak",
        }

    return {
        "daily_estimate": None, "daily_low": None, "daily_high": None, "confidence": 5,
        "basis": "Yeterli açık veri yok", "score": 10,
        "reasons": ["Günlük satış için kullanılabilir açık sinyal bulunamadı."], "evidence_level": "none",
    }


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
    return {
        "product_id": pid, "marketplace": mp, "product": data,
        "estimate": est, "periods": make_periods(est, data.get("price")),
        "history": {"snapshots": len(history), "time_series": time_series_signal(history)},
        "commission_rate": None,
        "commission_note": "Kategori komisyonu henüz otomatik doğrulanmadı; yanlış oran göstermemek için boş bırakıldı.",
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


@app.get("/api/products")
def products():
    c = db()
    rows = [dict(x) for x in c.execute("SELECT * FROM products ORDER BY created_at DESC").fetchall()]
    c.close()
    return rows
