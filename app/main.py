from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, HttpUrl
from urllib.parse import urlparse
from datetime import datetime, timezone
import sqlite3, json, re, requests, html as html_lib
from bs4 import BeautifulSoup

DB = "marketintel.db"
app = FastAPI(title="Pazaryeri İstihbarat", version="0.4.0")

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
        price REAL,
        list_price REAL,
        rating REAL,
        review_count INTEGER,
        commission_rate REAL DEFAULT 0,
        created_at TEXT
    )""")
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
    c.commit()
    c.close()

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
    if m:
        return m.group(1)
    return re.sub(r"[^a-zA-Z0-9]", "", str(url))[-48:]

def clean_text(v):
    if v is None:
        return None
    return re.sub(r"\s+", " ", html_lib.unescape(str(v))).strip()

def to_float(v):
    if v is None:
        return None
    s = str(v).strip()
    # Turkish decimal handling
    s = re.sub(r"[^\d,.\-]", "", s)
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except:
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
    except:
        return None

def first_jsonld(soup):
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except:
            continue
        candidates = obj if isinstance(obj, list) else [obj]
        for item in candidates:
            if isinstance(item, dict) and (
                item.get("@type") in ("Product", ["Product"]) or "offers" in item or "aggregateRating" in item
            ):
                return item
    return {}

def find_next_number(text, labels):
    for label in labels:
        m = re.search(re.escape(label) + r".{0,120}?(\d[\d\.,]*)", text, re.I | re.S)
        if m:
            return m.group(1)
    return None


def find_json_value(text, keys):
    for key in keys:
        # quoted JSON-ish key/value
        pats = [
            rf'"{re.escape(key)}"\s*:\s*"([^"]+)"',
            rf'"{re.escape(key)}"\s*:\s*([0-9]+(?:[.,][0-9]+)?)',
            rf"'{re.escape(key)}'\s*:\s*'([^']+)'",
        ]
        for pat in pats:
            m = re.search(pat, text, re.I)
            if m:
                return clean_text(m.group(1))
    return None

def detect_purchase_signal(text):
    patterns = [
        r'(?:son\s*24\s*saatte|son\s*24\s*saat içinde)\s*(\d+)\s*(?:kişi|adet)\s*(?:aldı|satın aldı)',
        r'(\d+)\s*(?:kişi|adet)\s*(?:bu ürünü\s*)?(?:aldı|satın aldı)\s*(?:son\s*24\s*saatte)?',
        r'(?:last\s*24\s*hours)\s*(\d+)\s*(?:people|orders|units)',
    ]
    for pat in patterns:
        m=re.search(pat,text,re.I)
        if m:
            return int(m.group(1))
    return None

def detect_badge(text):
    pats=[
        r'En\s*Çok\s*Satan\s*(\d+)?\.?\s*Ürün',
        r'En\s*çok\s*satan\s*(\d+)?\.?\s*ürün',
        r'Best\s*Seller\s*(\d+)?',
    ]
    for pat in pats:
        m=re.search(pat,text,re.I)
        if m:
            return ('En Çok Satan', int(m.group(1)) if m.group(1) else None)
    return (None,None)

def parse_public_product(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.7,en;q=0.5",
        "Cache-Control": "no-cache"
    }
    r = requests.get(str(url), headers=headers, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    text = clean_text(soup.get_text(" ", strip=True)) or ""
    ld = first_jsonld(soup)

    title = clean_text(ld.get("name"))
    if not title:
        meta = soup.find("meta", property="og:title")
        title = clean_text(meta.get("content")) if meta else None
    if not title and soup.title:
        title = clean_text(soup.title.get_text())

    brand = None
    b = ld.get("brand")
    if isinstance(b, dict):
        brand = clean_text(b.get("name"))
    elif b:
        brand = clean_text(b)

    offers = ld.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    price = to_float(offers.get("price"))
    list_price = None

    # Common public-page price signals
    if price is None:
        for pat in [
            r'"salePrice"\s*:\s*([0-9]+(?:[.,][0-9]+)?)',
            r'"discountedPrice"\s*:\s*([0-9]+(?:[.,][0-9]+)?)',
            r'"price"\s*:\s*([0-9]+(?:[.,][0-9]+)?)'
        ]:
            m = re.search(pat, r.text, re.I)
            if m:
                price = to_float(m.group(1))
                if price:
                    break

    # List price / crossed-out price when exposed
    for pat in [
        r'"listPrice"\s*:\s*([0-9]+(?:[.,][0-9]+)?)',
        r'"originalPrice"\s*:\s*([0-9]+(?:[.,][0-9]+)?)',
        r'"struckPrice"\s*:\s*([0-9]+(?:[.,][0-9]+)?)'
    ]:
        m = re.search(pat, r.text, re.I)
        if m:
            list_price = to_float(m.group(1))
            break

    rating = None
    review_count = None
    ar = ld.get("aggregateRating") or {}
    if isinstance(ar, dict):
        rating = to_float(ar.get("ratingValue"))
        review_count = to_int(ar.get("reviewCount") or ar.get("ratingCount"))

    if rating is None:
        v = find_next_number(text, ["Değerlendirme", "Puan", "rating"])
        rating = to_float(v)
    if review_count is None:
        v = find_next_number(text, ["Yorum", "Değerlendirme", "reviews"])
        review_count = to_int(v)

    seller = None
    if isinstance(offers, dict):
        s = offers.get("seller")
        if isinstance(s, dict):
            seller = clean_text(s.get("name"))
        elif s:
            seller = clean_text(s)
    if not seller:
        seller = find_json_value(r.text, ["merchantName","sellerName","merchantTitle","sellerTitle","merchant_name"])

    category = None
    cats = ld.get("category")
    if cats:
        category = clean_text(cats)
    if not category:
        # Prefer explicit breadcrumb/category JSON values if exposed.
        category = find_json_value(r.text, ["categoryName","categoryTitle","webCategory","categoryPath","category_name"])
    if not category:
        bc = soup.find_all(attrs={"itemprop":"name"})
        names=[clean_text(x.get_text(" ",strip=True)) for x in bc if clean_text(x.get_text(" ",strip=True))]
        if names:
            category = " > ".join(names[-4:])

    purchase_24h = detect_purchase_signal(text)
    badge, badge_rank = detect_badge(text)
    stock = None
    for pat in [
        r'"(?:stock|quantity|availableQuantity)"\s*:\s*(\d+)',
        r'"(?:stockCount|inventory)"\s*:\s*(\d+)'
    ]:
        m=re.search(pat,r.text,re.I)
        if m:
            stock=int(m.group(1)); break

    return {
        "title": title,
        "brand": brand or find_json_value(r.text,["brandName","brandTitle"]),
        "seller": seller,
        "category": category,
        "price": price,
        "list_price": list_price,
        "rating": rating,
        "review_count": review_count,
        "purchase_signal_24h": purchase_24h,
        "sales_badge": badge,
        "sales_badge_rank": badge_rank,
        "stock": stock,
        "http_status": r.status_code,
        "bytes": len(r.content)
    }

def estimate_from_current(data):
    # Tier 1: an explicit public 24h purchase signal is the strongest
    # instantaneous signal we can use.
    p24=data.get("purchase_signal_24h")
    if p24 is not None:
        return {"daily_low":p24,"daily_high":p24,"confidence":90,"basis":"public_24h_purchase_signal"}

    # Tier 2: a marketplace badge such as "En Çok Satan" is a useful
    # qualitative signal, but does not reveal a count. Keep the range broad.
    rank=data.get("sales_badge_rank")
    reviews=data.get("review_count")
    rating=data.get("rating")
    if rank is not None and reviews:
        # Rank is only used to adjust a broad review-based prior.
        base_low=max(1, round(reviews/90/30))
        base_high=max(base_low, round(reviews/30/10))
        factor=max(1.0, min(2.0, 10/max(rank,1)))
        lo=max(1, round(base_low*factor))
        hi=max(lo, round(base_high*factor))
        return {"daily_low":lo,"daily_high":hi,"confidence":50,"basis":"review_volume_plus_sales_badge"}

    # Tier 3: review-volume prior. This is deliberately low-confidence and
    # should never be presented as actual orders.
    if reviews:
        # Broad prior only; no invented exact daily sales.
        lo=max(1, round(reviews/365/30))
        hi=max(lo, round(reviews/90/10))
        confidence=30
        if rating: confidence += 5
        if data.get("price"): confidence += 5
        return {"daily_low":lo,"daily_high":hi,"confidence":min(confidence,40),"basis":"review_volume_prior"}

    return {"daily_low":None,"daily_high":None,"confidence":5,"basis":"public_data_insufficient"}

def periods(est, price):
    if est.get("daily_low") is None:
        return {
            k: {"sales_low": None, "sales_high": None, "revenue_low": None, "revenue_high": None}
            for k in ("daily","3_days","7_days","monthly")
        }
    out={}
    for key, days in (("daily",1),("3_days",3),("7_days",7),("monthly",30)):
        lo=est["daily_low"]*days
        hi=est["daily_high"]*days
        out[key]={
            "sales_low": lo, "sales_high": hi,
            "revenue_low": round(lo*price,2) if price else None,
            "revenue_high": round(hi*price,2) if price else None
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

    c=db()
    c.execute("""INSERT INTO products
        (id,marketplace,url,title,seller,brand,category,price,list_price,rating,review_count,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
        title=excluded.title,seller=excluded.seller,brand=excluded.brand,
        category=excluded.category,price=excluded.price,list_price=excluded.list_price,
        rating=excluded.rating,review_count=excluded.review_count""",
        (pid,mp,url,data["title"],data["seller"],data["brand"],data["category"],
         data["price"],data["list_price"],data["rating"],data["review_count"],
         datetime.now(timezone.utc).isoformat()))
    c.commit()

    # First snapshot is stored immediately, enabling later time-series tracking.
    c.execute("""INSERT INTO snapshots(product_id,captured_at,price,stock,review_count,rating,raw_json)
                 VALUES(?,?,?,?,?,?,?)""",
              (pid,datetime.now(timezone.utc).isoformat(),data["price"],data.get("stock"),
               data["review_count"],data["rating"],json.dumps(data,ensure_ascii=False)))
    c.commit()

    est=estimate_from_current(data)
    return {
        "product_id":pid,
        "marketplace":mp,
        "product":data,
        "estimate":est,
        "periods":periods(est,data["price"]),
        "commission_rate":None,
        "commission_note":"Kategori komisyonu henüz otomatik doğrulanmadı; yanlış oran göstermemek için boş bırakıldı.",
        "data_quality": {
            "purchase_signal_24h": data.get("purchase_signal_24h"),
            "sales_badge": data.get("sales_badge"),
            "sales_badge_rank": data.get("sales_badge_rank"),
            "stock": data.get("stock")
        },
        "snapshot_count":c.execute("SELECT COUNT(*) FROM snapshots WHERE product_id=?",(pid,)).fetchone()[0],
        "message":"Bu satış rakamları tahmindir. Gerçek rakip sipariş adedi pazaryerinin herkese açık verisi değildir. Ürün tekrar analiz edildikçe zaman serisi oluşur."
    }

@app.post("/api/snapshots")
def add_snapshot(req: SnapshotRequest):
    c=db()
    if not c.execute("SELECT 1 FROM products WHERE id=?",(req.product_id,)).fetchone():
        raise HTTPException(404,"Ürün bulunamadı. Önce /api/analyze çağırın.")
    now=datetime.now(timezone.utc).isoformat()
    c.execute("""INSERT INTO snapshots(product_id,captured_at,price,stock,review_count,rating,raw_json)
                 VALUES(?,?,?,?,?,?,?)""",
              (req.product_id,now,req.price,req.stock,req.review_count,req.rating,
               json.dumps(req.model_dump(),ensure_ascii=False)))
    c.commit()
    return {"ok":True,"captured_at":now}

@app.get("/api/products")
def products():
    c=db()
    return [dict(x) for x in c.execute("SELECT * FROM products ORDER BY created_at DESC").fetchall()]
