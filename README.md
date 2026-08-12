# Pazaryeri İstihbarat V5

Trendyol / Hepsiburada ürün URL'sinden herkese açık verileri okuyup satış ve ciro için tahmini aralık üreten FastAPI MVP.

## V5'teki yaklaşım
- Açık 24 saatlik satın alma sinyali varsa en güçlü sinyal olarak kullanılır.
- "En Çok Satan #X" etiketi varsa yorum hacmiyle birlikte geniş bir satış tahmini üretilir.
- Ürün tekrar analiz edildikçe snapshot geçmişi tutulur.
- Yorum hızı oluştuğunda geniş bir yorum→sipariş oranı heuristiği ile zaman serisi tahmini yapılır.
- Açık stok, tek başına satış kabul edilmez; yalnızca yardımcı sinyal olarak değerlendirilir.
- Günlük, 3 günlük, 7 günlük ve 30 günlük satış + ciro tahminleri gösterilir.
- Güven skoru ve tahminin nedenleri gösterilir.

## Çalıştırma
```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Render
Build:
```text
pip install -r requirements.txt
```
Start:
```text
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

> Not: Render Free üzerinde yerel SQLite kalıcı veri tabanı değildir; üretim sürümünde PostgreSQL veya başka kalıcı depolama kullanılmalıdır.
