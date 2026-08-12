# Pazaryeri İstihbarat V8

Trendyol / Hepsiburada ürün linkinden anlık ürün bilgileri ve kanıt-ağırlıklı satış tahmini üretmek için MVP.

## V8 değişiklikleri
- Trendyol ürününün kategori sayfası ayrıca okunarak `En Çok Satan #N` etiketi aranır.
- Satıcı için "Bu ürün X tarafından gönderilecektir" metni fallback olarak kullanılır.
- Kategori sırası, yorum hacmiyle birlikte tahmin öncülü olarak kullanılır; sıra doğrudan satış adedi kabul edilmez.
- Toplam stok hiçbir zaman satış adedi olarak kullanılmaz.
- Güçlü açık 24 saatlik satış sinyali varsa en yüksek ağırlığı alır.
