# Fatura Dedektifi — piyasa verisi

Bu depo tek bir iş yapar: **EPİAŞ Şeffaflık Platformu'ndan** günlük PTF (piyasa takas
fiyatı) ve aylık YEKDEM birim maliyetini çekip `data/piyasa/ptf-yekdem.json` dosyasına
yazar. Uygulama bu dosyayı indirir.

## Neden ayrı ve public bir depo?

Uygulama kodu **private** bir depoda. Ama bu dosyanın dağıtıldığı CDN (jsDelivr) yalnızca
public depolara hizmet veriyor. İçerik zaten kamuya açık piyasa verisi — gizli bir tarafı
yok. Bu yüzden kod private, veri public.

## Adres

```
https://cdn.jsdelivr.net/gh/06maslan27/fatura-dedektifi-veri@main/data/piyasa/ptf-yekdem.json
```

Uygulamaya gömülü olan adres budur; kullanıcı hiçbir kurulum yapmaz.

CDN arada olduğu için istekler bu depoya değil kenar sunuculara düşer — `raw.githubusercontent.com`
dosya dağıtımı için tasarlanmadığından kullanıcı sayısı büyüdüğünde kısıtlanır, jsDelivr ise
tam olarak bu iş için var. Uygulama günde bir kez ve ETag ile şartlı indirir; dosya
değişmediyse 304 döner ve hiç veri inmez.

## Kurulum (tek seferlik)

1. `Settings → Secrets and variables → Actions` altına ekle:
   - `EPIAS_USERNAME` — EPİAŞ Şeffaflık Platformu kullanıcı adın (e-posta)
   - `EPIAS_PASSWORD` — parolan
2. `Actions → Piyasa verisi → Run workflow` ile elle bir kez çalıştır.
3. Sonrası günlük otomatik.

Parola yalnızca GitHub secret olarak durur; ne uygulamaya ne de bu depodaki dosyalara girer.

## Elle çalıştırma / alan adı doğrulama

EPİAŞ yanıt şeması sürümle değişebiliyor. Değişip değişmediğini görmek için:

```bash
EPIAS_USERNAME=... EPIAS_PASSWORD=... python tools/epias_mirror.py --dry-run
```

`--dry-run` hiçbir dosya yazmaz; yalnızca hangi alanların geldiğini basar.
