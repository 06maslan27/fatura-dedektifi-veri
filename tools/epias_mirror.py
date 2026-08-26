#!/usr/bin/env python3
"""
EPİAŞ ŞEFFAFLIK PLATFORMU AYNASI

EPİAŞ'tan PTF (piyasa takas fiyatı) ve YEKDEM birim maliyet verisini çeker, uygulamanın
okuduğu ``PriceSeries`` biçiminde tek bir JSON dosyası üretir.

NEDEN AYNA: EPİAŞ Şeffaflık 2.0 web servisleri kayıtlı hesapla TGT (ticket) almayı
zorunlu tutuyor; anonim erişim yok. Bu kimlik bilgisi APK'ya gömülemez — herkes tarafından
çıkarılabilir ve hesap kapanır. Bu yüzden veriyi GitHub Actions çeker (kimlik bilgisi
GitHub secret'ında kalır), sonucu küçük bir JSON olarak yayımlar; uygulama yalnızca o
dosyayı indirir. Uygulamadan EPİAŞ'a hiçbir istek gitmez, EPİAŞ'a hiçbir kullanıcı verisi
gönderilmez.

Kullanım:
    export EPIAS_USERNAME="..." EPIAS_PASSWORD="..."
    python epias_mirror.py --days 45 --out ../../data/piyasa/ptf-yekdem.json

İlk kez çalıştırırken alan adlarını gözle doğrulamak için:
    python epias_mirror.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import getpass
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

CAS_URL = "https://giris.epias.com.tr/cas/v1/tickets"
BASE_URL = "https://seffaflik.epias.com.tr"

# Uçlar 401 (var, kimlik gerekiyor) / 404 (yok) farkıyla doğrulandı.
MCP_PATH = "/electricity-service/v1/markets/dam/data/mcp"            # PTF, saatlik
YEKDEM_UNIT_COST_PATH = "/electricity-service/v1/renewables/data/unit-cost"  # YEKDEM birim maliyet

# GÖP EŞLEŞME MİKTARI — günlük PTF'nin AĞIRLIĞI budur.
#
# Mevzuat "EPİAŞ tarafından Türkiye için günlük açıklanan AĞIRLIKLI ORTALAMA piyasa takas
# fiyatları" der (1 Nolu Açıklama md. 12). Saatlik PTF'lerin düz ortalaması ağırlıklı
# ortalama DEĞİLDİR: gecenin ucuz ve az işlem gören saati ile akşam puantının pahalı ve
# yoğun saati aynı ağırlığa sahip olamaz. Doğru ağırlık, o saatte gün öncesi piyasasında
# eşleşen miktardır:
#
#     PTF_gün = Σ(PTF_saat × Eşleşme_saat) / Σ(Eşleşme_saat)
#
# Uç adı sürümle değişebildiği için birkaç aday sırayla denenir; hiçbiri tutmazsa düz
# ortalamaya düşülür ve bu durum üretilen dosyanın not alanına YAZILIR — sessizce yanlış
# yöntem kullanılmasın.
MATCHING_QUANTITY_PATHS = (
    # Doğrulandı: date, hour, matchedBids, matchedOffers döner (saatlik).
    "/electricity-service/v1/markets/dam/data/clearing-quantity",
    # Yedekler — uç adı sürümle değişirse.
    "/electricity-service/v1/markets/dam/data/matching-quantity",
    "/electricity-service/v1/markets/dam/data/day-ahead-market-trade-volume",
)

# EPİAŞ'ın DOĞRUDAN yayımladığı günlük ağırlıklı ortalama PTF.
#
# 1 Nolu Açıklama md. 12 "EPİAŞ tarafından günlük AÇIKLANAN ağırlıklı ortalama piyasa
# takas fiyatları" der — yani ortalamayı biz hesaplamayız, EPİAŞ açıklar. Böyle bir uç
# varsa saatlikten türetmeye hiç gerek kalmaz ve tartışma biter.
DAILY_MCP_PATHS = (
    "/electricity-service/v1/markets/dam/data/mcp-daily",
    "/electricity-service/v1/markets/dam/data/daily-mcp",
    "/electricity-service/v1/markets/dam/data/mcp-daily-average",
    "/electricity-service/v1/markets/dam/data/weighted-average-mcp",
    "/electricity-service/v1/markets/dam/data/mcp-weighted-average",
    "/electricity-service/v1/markets/dam/data/mcp-summary",
)

# Bazı EPİAŞ uçları aynı yolda gövdeye konan bir alanla günlük toplulaştırma kabul ediyor.
DAILY_BODY_VARIANTS = (
    {"granularity": "DAILY"},
    {"period": "DAILY"},
    {"periodType": "DAILY"},
    {"aggregationType": "DAILY"},
)

# Günlük ağırlıklı ortalamanın hangi alandan geleceği de sürüme bağlı.
DAILY_PRICE_FIELD_CANDIDATES = (
    "weightedAverage", "weightedAveragePrice", "wap", "dailyAverage",
    "averagePrice", "price", "mcp",
)

TIMEZONE_SUFFIX = "+03:00"

# EPİAŞ yanıtlarındaki alan adları sürümle değişebiliyor. Tek yerden, sırayla deneniyor:
# ilk eşleşen kullanılır. --dry-run çıktısına bakıp buraya ekleme yapmak yeterli.
LIST_FIELD_CANDIDATES = ("items", "body", "content", "data")
DATE_FIELD_CANDIDATES = ("date", "effectiveDate", "period", "time", "dateTime")
PRICE_FIELD_CANDIDATES = ("price", "mcp", "priceTl", "unitCost", "cost", "amount", "value")
QUANTITY_FIELD_CANDIDATES = (
    # clearing-quantity ucunun gerçek alanları. Temizlenmiş bir gün öncesi piyasasında
    # eşleşen alış = eşleşen satış olduğu için ikisi de aynı ağırlığı verir.
    "matchedBids", "matchedOffers",
    "matchingQuantity", "quantity", "volume", "tradeVolume", "amount", "value",
)


class EpiasError(RuntimeError):
    pass


def yeniden_dene(islem, ad: str, deneme: int = 3, bekleme: int = 20):
    """
    Geçici ağ hatasında yeniden dener.

    EPİAŞ uçları zaman zaman zaman aşımına düşüyor. Günde bir çalışan bir işi tek bir
    takılmanın düşürmesi anlamsız; ama KALICI hata (yanlış parola, 404) hemen yukarı
    çıkmalı — o yüzden yalnızca ağ/zaman aşımı hataları tekrarlanıyor.
    """
    for sira in range(1, deneme + 1):
        try:
            return islem()
        except (urllib.error.URLError, TimeoutError, OSError) as hata:
            if isinstance(hata, urllib.error.HTTPError):
                raise
            if sira == deneme:
                raise
            print(
                f"{ad}: ağ hatası ({hata}), {bekleme} sn sonra {sira + 1}. deneme...",
                file=sys.stderr,
            )
            time.sleep(bekleme)


def login(username: str, password: str) -> str:
    """CAS'tan TGT alır. Ticket yanıt gövdesinde ya da Location başlığında gelir."""
    body = urllib.parse.urlencode({"username": username, "password": password}).encode()
    request = urllib.request.Request(
        CAS_URL,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/plain",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            location = response.headers.get("Location", "")
            payload = response.read().decode("utf-8", errors="replace").strip()
    except urllib.error.HTTPError as exc:
        raise EpiasError(
            f"EPİAŞ girişi başarısız (HTTP {exc.code}). Kullanıcı adı/parola doğru mu?"
        ) from exc

    for candidate in (payload, location.rsplit("/", 1)[-1] if location else ""):
        if candidate.startswith("TGT-"):
            return candidate
    raise EpiasError(f"TGT bulunamadı. Yanıt: {payload[:200]!r} Location: {location!r}")


def son_yayim_gunu() -> date:
    """
    İstenebilecek EN SON gün (hariç sınır).

    Gün öncesi piyasası sonuçları o gün saat 14:00'ten önce yayımlanmıyor; erken saatte
    yarını istemek isteğin TAMAMINI 400 ile düşürüyor ("... tarihli veri saat 14
    öncesinde mevcut değil"). Bu yüzden saat 14'ten önce yalnızca bugüne kadar
    (bugün hariç), sonrasında yarına kadar istiyoruz.
    """
    bugun = date.today()
    return bugun + timedelta(days=1) if datetime.now().hour >= 14 else bugun


def fetch(
    path: str,
    tgt: str,
    start: date,
    end: date,
    raw: bool = False,
    extra: dict | None = None,
):
    govde = {
        "startDate": f"{start.isoformat()}T00:00:00{TIMEZONE_SUFFIX}",
        "endDate": f"{end.isoformat()}T00:00:00{TIMEZONE_SUFFIX}",
    }
    if extra:
        govde.update(extra)
    payload = json.dumps(govde).encode()
    request = urllib.request.Request(
        BASE_URL + path,
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json", "TGT": tgt},
        method="POST",
    )
    try:
        def istek():
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.loads(response.read().decode("utf-8"))
        document = yeniden_dene(istek, path.rsplit("/", 1)[-1], deneme=2, bekleme=10)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        # Yayımlanmamış gün istendiyse aralığı bir gün kısaltıp bir kez daha dene.
        if exc.code == 400 and "mevcut de" in detail and end > start + timedelta(days=1):
            return fetch(path, tgt, start, end - timedelta(days=1), raw=raw, extra=extra)
        raise EpiasError(f"{path} çağrısı başarısız (HTTP {exc.code}): {detail}") from exc

    if raw:
        return document
    return extract_rows(document, path)


def extract_rows(document, path: str) -> list[dict]:
    """Yanıttaki satır listesini bulur; yapı sürümden sürüme değişebildiği için esnek."""
    if isinstance(document, list):
        return document
    for field in LIST_FIELD_CANDIDATES:
        value = document.get(field) if isinstance(document, dict) else None
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            for inner in value.values():
                if isinstance(inner, list):
                    return inner
    raise EpiasError(
        f"{path} yanıtında satır listesi bulunamadı. Alanlar: {list(document)[:10]}"
    )


def pick(row: dict, candidates: tuple[str, ...]):
    for field in candidates:
        if field in row and row[field] is not None:
            return row[field]
    return None


def to_day(value) -> str | None:
    """'2026-08-01T00:00:00+03:00' → '2026-08-01'."""
    if not isinstance(value, str) or len(value) < 10:
        return None
    return value[:10]


def to_hour_key(row: dict) -> str | None:
    """
    Bir satırdan saatlik eşleştirme anahtarı üretir: '2026-08-01T14'.

    EPİAŞ hem PTF hem eşleşme miktarı uçlarında tarihi ve saati AYRI alanlarda veriyor
    (`date` = günün başlangıcı, `hour` = "14:00"). Yalnızca `date`'e bakmak günün 24
    saatini tek anahtara toplar ve ağırlıklandırmayı bozar; bu yüzden ikisi birleştiriliyor.
    Saat alanı yoksa tarihin kendi saat kısmına düşülür.
    """
    ham = pick(row, DATE_FIELD_CANDIDATES)
    if not isinstance(ham, str) or len(ham) < 10:
        return None
    gun = ham[:10]

    saat = row.get("hour")
    if saat is not None:
        metin = str(saat).strip()
        rakam = re.match(r"^(\d{1,2})", metin)
        if rakam:
            return "{}T{:02d}".format(gun, int(rakam.group(1)))
    return ham[:13] if len(ham) >= 13 else None


def hourly_quantities(rows: list[dict]) -> dict[str, Decimal]:
    """Saat anahtarı → o saatte eşleşen miktar."""
    sonuc: dict[str, Decimal] = {}
    for row in rows:
        saat = to_hour_key(row)
        miktar = pick(row, QUANTITY_FIELD_CANDIDATES)
        if saat is None or miktar is None:
            continue
        try:
            sonuc[saat] = Decimal(str(miktar))
        except Exception:  # noqa: BLE001 - bozuk satırı atla
            continue
    return sonuc


def daily_weighted_average(
    rows: list[dict], quantities: dict[str, Decimal]
) -> tuple[dict[str, Decimal], bool]:
    """
    Saatlik PTF'yi güne indirger.

    Eşleşme miktarı verisi varsa AĞIRLIKLI ortalama (mevzuatın istediği), yoksa düz
    ortalama alınır. İkinci dönüş değeri hangisinin kullanıldığını söyler; çağıran bunu
    üretilen dosyaya not olarak yazar.
    """
    agirlikli = bool(quantities)
    paylar: dict[str, Decimal] = {}
    paydalar: dict[str, Decimal] = {}

    for row in rows:
        gun = to_day(pick(row, DATE_FIELD_CANDIDATES))
        fiyat = pick(row, PRICE_FIELD_CANDIDATES)
        if gun is None or fiyat is None:
            continue
        try:
            fiyat = Decimal(str(fiyat))
        except Exception:  # noqa: BLE001
            continue

        agirlik = Decimal(1)
        if agirlikli:
            saat = to_hour_key(row)
            agirlik = quantities.get(saat, Decimal(0)) if saat else Decimal(0)
            # O saatin miktarı yoksa saati düşürmek yerine ağırlığı 1 saymak, günün
            # tamamını kaybetmekten iyidir; ama bu artık saf ağırlıklı ortalama değil.
            if agirlik <= 0:
                agirlik = Decimal(1)

        paylar[gun] = paylar.get(gun, Decimal(0)) + fiyat * agirlik
        paydalar[gun] = paydalar.get(gun, Decimal(0)) + agirlik

    return (
        {
            gun: (paylar[gun] / paydalar[gun]) if paydalar[gun] > 0 else Decimal(0)
            for gun in paylar
        },
        agirlikli,
    )


def to_points(values: dict[str, Decimal], divisor: Decimal, scale: str) -> list[dict]:
    """MWh fiyatını kWh'e çevirip (bölen 1000) noktalara dönüştürür."""
    points = []
    for day in sorted(values):
        price = (values[day] / divisor).quantize(Decimal(scale), rounding=ROUND_HALF_UP)
        points.append({"date": day, "price": f"{price}"})
    return points


def monthly_yekdem(values: dict[str, Decimal]) -> list[dict]:
    """
    Günlük/aylık gelen YEKDEM birim maliyetini TAKVİM AYI kayıtlarına indirger.

    Mevzuat YEKDEM'i ay bazında tanımlar (Tebliğ md. 6/6): fatura dönemi bir aya kaç gün
    düşüyorsa o ayın bedeli o ağırlıkla girer. Bu yüzden ayna da ay bazında yazar; günlük
    bir seri olarak yazmak, motorun ağırlıklandırmasını bozardı.

    EPİAŞ'ın açıkladığı bu değerler KESİNLEŞMİŞ GERÇEKLEŞEN bedeldir (actual=true).

    DİKKAT: fatura hesabına giren bu değil, Kurul'un ÖNGÖRDÜĞÜ bedeldir — Tebliğ md. 6/6
    eşitlik (2)'nin değişkeni öngörüdür ve md. 6/7 uyarınca öngörü/gerçekleşen farkı
    tedarikçinin kendi tarife düzenlemesinde mahsuplaşır, tüketicinin faturasına yansımaz.
    Buradaki değerler uygulamada bilgi olarak tutulur ve yalnızca ilgili ayın Kurul
    öngörüsü hiç yoksa yedek olarak devreye girer.
    """
    buckets: dict[tuple[int, int], list[Decimal]] = {}
    for day, value in values.items():
        yil, ay = int(day[:4]), int(day[5:7])
        buckets.setdefault((yil, ay), []).append(value)
    kayitlar = []
    for (yil, ay) in sorted(buckets):
        degerler = buckets[(yil, ay)]
        ortalama = (sum(degerler) / Decimal(len(degerler))).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        kayitlar.append(
            {
                "year": yil,
                "month": ay,
                "unitCostPerMwh": f"{ortalama}",
                "actual": True,
                "source": "EPİAŞ Şeffaflık Platformu · YEKDEM birim maliyeti (gerçekleşen)",
            }
        )
    return kayitlar


YONTEM_ACIKLAMA = {
    "yayimlanan": "EPİAŞ'ın Türkiye için günlük açıkladığı ağırlıklı ortalama PTF olduğu "
                  "gibi alındı (1 Nolu Açıklama md. 12).",
    "agirlikli": "EPİAŞ günlük ortalama yayımlamadığı için saatlik PTF, o saatte gün "
                 "öncesi piyasasında eşleşen miktarla ağırlıklandırılarak güne indirgendi.",
    "duz": "UYARI: ne yayımlanan günlük ortalama ne de eşleşme miktarı alınabildi; "
           "saatlik PTF'lerin DÜZ ortalaması kullanıldı. Mevzuat ağırlıklı ortalama "
           "ister — bu değer YAKLAŞIKTIR.",
}


def gunluk_seri(satirlar: list) -> dict:
    """Günlük yanıttan gün → fiyat sözlüğü çıkarır."""
    sonuc: dict[str, Decimal] = {}
    for row in satirlar:
        gun = to_day(pick(row, DATE_FIELD_CANDIDATES))
        fiyat = pick(row, DAILY_PRICE_FIELD_CANDIDATES)
        if gun is None or fiyat is None:
            continue
        try:
            sonuc[gun] = Decimal(str(fiyat))
        except Exception:  # noqa: BLE001 - bozuk satırı atla
            continue
    return sonuc


def gunluk_ptf_bul(tgt: str, start: date, end: date) -> tuple:
    """
    Günlük PTF'yi bulur ve HANGİ YOLDAN bulunduğunu söyler.

    Öncelik sırası doğrudan mevzuattan (1 Nolu Açıklama md. 12: "EPİAŞ tarafından
    günlük AÇIKLANAN ağırlıklı ortalama"): ortalamayı biz hesaplamayız, EPİAŞ açıklar.

      1. EPİAŞ'ın yayımladığı günlük ağırlıklı ortalama  → olduğu gibi alınır
      2. Saatlik PTF + eşleşme miktarı                   → ağırlıklı ortalama türetilir
      3. Yalnızca saatlik PTF                            → düz ortalama (yaklaşık)

    Dönüş: (gün → TL/MWh sözlüğü, yöntem etiketi)
    """
    # 1) Yayımlanmış günlük değer — ayrı uçlar
    for path in DAILY_MCP_PATHS:
        try:
            degerler = gunluk_seri(fetch(path, tgt, start, end))
        except Exception:  # noqa: BLE001 - uç yoksa sıradakine geç
            continue
        if degerler:
            return degerler, "yayimlanan", path.rsplit("/", 1)[-1]

    # 1b) Aynı uç, gövdeye günlük toplulaştırma alanı koyarak. Saatlik yanıt gün başına
    #     24 kayıt verir; günlük yanıt gün sayısı kadar. Ayrımı buradan yapıyoruz.
    gun_sayisi = (end - start).days
    for ek in DAILY_BODY_VARIANTS:
        try:
            satirlar = fetch(MCP_PATH, tgt, start, end, extra=ek)
        except Exception:  # noqa: BLE001
            continue
        if satirlar and len(satirlar) <= gun_sayisi + 2:
            degerler = gunluk_seri(satirlar)
            if degerler:
                return degerler, "yayimlanan", "mcp+" + next(iter(ek))

    # 2/3) Türetme
    mcp_satirlari = fetch(MCP_PATH, tgt, start, end)
    miktarlar: dict[str, Decimal] = {}
    for path in MATCHING_QUANTITY_PATHS:
        try:
            miktarlar = hourly_quantities(fetch(path, tgt, start, end))
        except Exception:  # noqa: BLE001
            continue
        if miktarlar:
            break
    gunluk, agirlikli = daily_weighted_average(mcp_satirlari, miktarlar)
    return gunluk, ("agirlikli" if agirlikli else "duz"), ""


class OngoruHatasi(RuntimeError):
    """CSV bozuk. Yayındaki dosyaya dokunmadan işi durdurmak için."""


def ongorulen_yekdem_oku(yol: str) -> list[dict]:
    """
    Kurul'un öngördüğü aylık YEKDEM bedellerini CSV'den okur.

    Biçim bilerek CSV: JSON'da bir virgül ya da tırnak unutmak dosyanın tamamını
    bozuyor, CSV'de en fazla bir satır bozulur ve hangi satır olduğu söylenebilir.

        yil,ay,tl_mwh,kaynak
        2027,1,415.20,EPDK Kurul Kararı 15xxx / 12.2026
        2027,2,398.60,

    Kaynak boş bırakılırsa üstteki satırdan devralınır — bir kez yazmak yeterli.
    Aynı (yıl, ay) ikinci kez yazılırsa SONRAKİ kazanır; Kurul yıl ortasında revize
    ettiğinde eski satırı silmeye gerek kalmasın diye.

    Bozuk satırda [OngoruHatasi] fırlatır; çağıran hiçbir dosya yazmadan durur.
    """
    if not os.path.exists(yol):
        return []

    kayitlar: dict[tuple[int, int], dict] = {}
    son_kaynak = ""
    with open(yol, encoding="utf-8") as dosya:
        for satir_no, ham in enumerate(dosya, start=1):
            satir = ham.strip()
            if not satir or satir.startswith("#"):
                continue
            parcalar = [p.strip() for p in satir.split(",", 3)]
            if parcalar[0].lower() in ("yil", "yıl"):      # başlık satırı
                continue
            if len(parcalar) < 3:
                raise OngoruHatasi(
                    f"{yol}:{satir_no} — en az 3 sütun gerekli (yil,ay,tl_mwh): {satir!r}"
                )

            try:
                yil = int(parcalar[0])
                ay = int(parcalar[1])
                bedel = Decimal(parcalar[2].replace(",", "."))
            except Exception as hata:  # noqa: BLE001
                raise OngoruHatasi(f"{yol}:{satir_no} — sayı okunamadı: {satir!r}") from hata

            if not 1 <= ay <= 12:
                raise OngoruHatasi(f"{yol}:{satir_no} — ay 1–12 arasında olmalı, {ay} yazılmış.")
            if bedel <= 0:
                raise OngoruHatasi(f"{yol}:{satir_no} — bedel pozitif olmalı, {bedel} yazılmış.")
            if not 2000 <= yil <= 2100:
                raise OngoruHatasi(f"{yol}:{satir_no} — yıl makul değil: {yil}")

            kaynak = (parcalar[3].strip() if len(parcalar) > 3 else "") or son_kaynak
            if not kaynak:
                raise OngoruHatasi(
                    f"{yol}:{satir_no} — kaynak yazılmamış ve devralınacak üst satır yok."
                )
            son_kaynak = kaynak

            kayitlar[(yil, ay)] = {
                "year": yil,
                "month": ay,
                "unitCostPerMwh": f"{bedel}",
                "actual": False,
                "source": kaynak,
            }

    return [kayitlar[k] for k in sorted(kayitlar)]


def build_catalog(
    mcp_points: list[dict],
    yekdem_months: list[dict],
    generated_at: str,
    yekdem_forecast: list[dict] | None = None,
    method: str = "duz",
    method_detail: str = "",
) -> dict:
    series = []
    if mcp_points:
        weighted = method in ("yayimlanan", "agirlikli")
        yontem = YONTEM_ACIKLAMA.get(method, YONTEM_ACIKLAMA["duz"])
        if method_detail:
            yontem += " (uç: " + method_detail + ")"
        series.append(
            {
                "id": "epias-ptf-gunluk",
                "name": "PTF (günlük ağırlıklı ortalama)",
                "unit": "TL/kWh",
                "points": mcp_points,
                "source": "EPİAŞ Şeffaflık Platformu · gün öncesi piyasası takas fiyatı (MCP)",
                "placeholder": not weighted,
                "note": yontem + " MWh fiyatı kWh'e çevrildi. Son kaynak hesabında bu "
                "günlük değerlerin ARİTMETİK ortalaması alınır (1 Nolu Açıklama md. 12).",
            }
        )
    return {
        "schemaVersion": 1,
        "title": "EPİAŞ piyasa verisi",
        "generatedAt": generated_at,
        "disclaimer": "Bu dosya EPİAŞ Şeffaflık Platformu'ndan otomatik çekilen kamuya açık "
        "piyasa verisidir. Uygulama yalnızca bu dosyayı indirir; EPİAŞ'a kullanıcı verisi "
        "gönderilmez.",
        "priceSeries": series,
        # Önce Kurul öngörüleri (faturaya giren), sonra EPİAŞ gerçekleşenleri (bilgi).
        "yekdemUnitCosts": (yekdem_forecast or []) + yekdem_months,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="EPİAŞ PTF/YEKDEM aynası")
    parser.add_argument("--days", type=int, default=60, help="Kaç günlük geçmiş çekilsin")
    parser.add_argument("--out", default="data/piyasa/ptf-yekdem.json")
    parser.add_argument(
        "--ongoru",
        default="data/yekdem-ongoru.csv",
        help="Kurul'un öngördüğü aylık YEKDEM bedellerini içeren CSV.",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="Aday uçları tek tek yoklar; hangisinin var olduğunu HTTP koduyla söyler.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Ham yanıtı yazdırır; alan adlarını doğrulamak için ilk çalıştırmada kullan.",
    )
    args = parser.parse_args()

    # Kimlik önce ortam değişkeninden (GitHub Actions böyle çalışır), yoksa ekrandan
    # sorulur. Parolayı batch dosyasında okumak kırılgandı (for /f PowerShell'in çıktısını
    # yakalayınca soru ekrana hiç basılmıyordu); buraya alındı. getpass parolayı ekranda
    # GÖSTERMEZ ve hiçbir yere kaydetmez.
    username = os.environ.get("EPIAS_USERNAME")
    password = os.environ.get("EPIAS_PASSWORD")

    if not username or not password:
        if not sys.stdin.isatty():
            print(
                "EPIAS_USERNAME ve EPIAS_PASSWORD ortam degiskenleri gerekli.",
                file=sys.stderr,
            )
            return 2
        print("EPIAS Seffaflik Platformu girisi")
        print("(parola yazarken ekranda gorunmez, hicbir yere kaydedilmez)")
        print()
        if not username:
            username = input("  Kullanici adi (e-posta): ").strip()
        if not password:
            password = getpass.getpass("  Parola: ")
        print()
        if not username or not password:
            print("Kullanici adi ve parola bos olamaz.", file=sys.stderr)
            return 2

    # Öngörü CSV'si EN BAŞTA okunuyor: bozuksa EPİAŞ'a hiç gitmeden duruyoruz ve
    # yayındaki dosya olduğu gibi kalıyor. Yarım veri kimseye inmesin.
    try:
        ongoruler = ongorulen_yekdem_oku(args.ongoru)
    except OngoruHatasi as hata:
        print("ÖNGÖRÜ DOSYASI BOZUK — hiçbir şey yazılmadı.", file=sys.stderr)
        print("  " + str(hata), file=sys.stderr)
        return 4
    print(f"Kurul öngörüsü: {len(ongoruler)} ay ({args.ongoru})")

    end = son_yayim_gunu()
    start = end - timedelta(days=args.days)

    try:
        tgt = yeniden_dene(lambda: login(username, password), "EPİAŞ girişi")
    except EpiasError as hata:
        print()
        print("GIRIS BASARISIZ: " + str(hata), file=sys.stderr)
        print(
            "Not: EPIAS Seffaflik Platformu hesabi ile giris yapilir. Ayni parolayla "
            "tarayicidan seffaflik.epias.com.tr adresine girebiliyor musun? Giremiyorsan "
            "hesap henuz onaylanmamis olabilir.",
            file=sys.stderr,
        )
        return 3

    if args.probe:
        # HTTP 404 = uc yok · 400 = uc VAR ama istek/tarih yanlis · 200 = calisiyor.
        # Bu ayrim sayesinde dogru uc adini deneme yanilma ile bulabiliyoruz.
        print("Aday uclar yoklaniyor (404 = yok, 400 = var ama istek hatali, OK = calisiyor)")
        print()
        adaylar = list(DAILY_MCP_PATHS) + list(MATCHING_QUANTITY_PATHS)
        bulunanlar = []
        for path in adaylar:
            ad = path.rsplit("/", 1)[-1]
            try:
                satirlar = extract_rows(fetch(path, tgt, start, end, raw=True), path)
                print("  OK   {:<34} {} kayit".format(ad, len(satirlar)))
                if satirlar:
                    print("       alanlar: " + ", ".join(satirlar[0].keys()))
                    bulunanlar.append(path)
            except Exception as hata:  # noqa: BLE001
                metin = str(hata)
                kod = re.search(r"HTTP (\d+)", metin)
                kod = kod.group(1) if kod else "???"
                mesaj = re.search(r'"errorMessage"\s*:\s*"([^"]+)"', metin)
                print("  {:<4} {:<34} {}".format(kod, ad, (mesaj.group(1)[:60] if mesaj else "")))
                if kod == "400":
                    bulunanlar.append(path)
        print()
        print("VAR gibi gorunen uclar:")
        for p in bulunanlar:
            print("  " + p)
        if not bulunanlar:
            print("  (hicbiri)")
        return 0

    if args.dry_run:
        # Amaç ham JSON'u kusmak değil, tek bakışta "çalışıyor mu, hangi alanlar geldi,
        # tahminlerimiz tuttu mu" sorusunu cevaplamak.
        print("Giris basarili - TGT alindi.")
        print("Istenen aralik: " + start.isoformat() + " .. " + end.isoformat() + " (bitis haric)")
        print()
        tamam = True

        def hata_ozeti(hata: Exception) -> str:
            """Sadece errorCode + errorMessage; JSON gurultusu okumayi engelliyordu."""
            metin = str(hata)
            kod = re.search(r'"errorCode"\s*:\s*"([^"]+)"', metin)
            mesaj = re.search(r'"errorMessage"\s*:\s*"([^"]+)"', metin)
            http = re.search(r"HTTP (\d+)", metin)
            parcalar = []
            if http:
                parcalar.append("HTTP " + http.group(1))
            if kod:
                parcalar.append(kod.group(1))
            if mesaj:
                parcalar.append(mesaj.group(1))
            return " | ".join(parcalar) if parcalar else metin[:160]

        def incele(baslik: str, path: str, deger_adaylari) -> list:
            print("=== " + baslik + " ===")
            print("    uc: " + path)
            try:
                satirlar = extract_rows(fetch(path, tgt, start, end, raw=True), path)
            except Exception as hata:  # noqa: BLE001 - teshis ciktisi
                print("    HATA: " + hata_ozeti(hata))
                print()
                return []
            print("    gelen kayit: " + str(len(satirlar)))
            if not satirlar:
                print("    UYARI: kayit yok.")
                print()
                return []
            ornek = satirlar[0]
            print("    alanlar: " + ", ".join(ornek.keys()))
            tarih_alani = next((a for a in DATE_FIELD_CANDIDATES if a in ornek), None)
            print("    tarih alani : " + (tarih_alani or "BULUNAMADI"))
            # Sayisal alanlarin HEPSINI goster: hangisinin dogru oldugunu alan uzmani secer.
            for alan, deger in ornek.items():
                if alan != tarih_alani and isinstance(deger, (int, float)):
                    print("      {:<20} = {}".format(alan, deger))
            print()
            return satirlar

        mcp = incele("PTF (gun oncesi piyasasi takas fiyati)", MCP_PATH, PRICE_FIELD_CANDIDATES)
        if not mcp:
            tamam = False

        yekdem = incele("YEKDEM birim maliyeti", YEKDEM_UNIT_COST_PATH, PRICE_FIELD_CANDIDATES)
        if not yekdem:
            tamam = False
        elif len(yekdem) > 1:
            print("    YEKDEM son kayitlar:")
            for satir in yekdem[-6:]:
                donem = pick(satir, DATE_FIELD_CANDIDATES)
                print("      {}  unitCost={}  supplierUnitCost={}  ptf={}".format(
                    str(donem)[:10],
                    satir.get("unitCost"),
                    satir.get("supplierUnitCost"),
                    satir.get("ptf"),
                ))
            print()

        print("=== EPIAS'in DOGRUDAN yayimladigi gunluk agirlikli ortalama PTF ===")
        gunluk_uc = None
        for path in DAILY_MCP_PATHS:
            try:
                satirlar = extract_rows(fetch(path, tgt, start, end, raw=True), path)
            except Exception as hata:  # noqa: BLE001
                print("    {:<62} {}".format(path.rsplit("/", 1)[-1], hata_ozeti(hata)))
                continue
            if satirlar:
                gunluk_uc = path
                print("    BULUNDU: " + path)
                print("    alanlar: " + ", ".join(satirlar[0].keys()))
                break
        if gunluk_uc is None:
            gun_sayisi = (end - start).days
            for ek in DAILY_BODY_VARIANTS:
                anahtar = next(iter(ek))
                try:
                    satirlar = extract_rows(
                        fetch(MCP_PATH, tgt, start, end, raw=True, extra=ek), MCP_PATH
                    )
                except Exception as hata:  # noqa: BLE001
                    print("    {:<34} {}".format("mcp + " + anahtar, hata_ozeti(hata)))
                    continue
                if satirlar and len(satirlar) <= gun_sayisi + 2:
                    gunluk_uc = "mcp + " + anahtar
                    print("    BULUNDU: mcp ucu + " + anahtar + "=DAILY")
                    print("    alanlar: " + ", ".join(satirlar[0].keys()))
                    break
                print("    {:<34} {} kayit - saatlik, gunluk degil".format(
                    "mcp + " + anahtar, len(satirlar)))
        if gunluk_uc is None:
            print("    YOK - gunluk ortalamayi saatlikten turetmemiz gerekecek")
        print()

        print("=== GOP eslesme miktari (saatlikten turetirken agirlik) ===")
        miktar_uc = None
        for path in MATCHING_QUANTITY_PATHS:
            try:
                satirlar = extract_rows(fetch(path, tgt, start, end, raw=True), path)
            except Exception as hata:  # noqa: BLE001
                print("    {:<62} {}".format(path.rsplit("/", 1)[-1], hata_ozeti(hata)))
                continue
            if satirlar:
                miktar_uc = path
                print("    BULUNDU: " + path)
                print("    alanlar: " + ", ".join(satirlar[0].keys()))
                break
        if miktar_uc is None and gunluk_uc is None:
            print("    HICBIRI TUTMADI - duz ortalamaya dusulecek (mevzuat agirlikli ister).")
            tamam = False
        print()

        print("SONUC: " + ("her sey yerinde, ayna calismaya hazir."
                           if tamam else "eksik var - yukaridaki uyarilara bak."))
        return 0 if tamam else 1

    yekdem_rows = fetch(YEKDEM_UNIT_COST_PATH, tgt, start, end)

    # Günlük PTF: önce EPİAŞ'ın yayımladığı ağırlıklı ortalama, olmazsa türetme.
    gunluk, yontem, yontem_detay = gunluk_ptf_bul(tgt, start, end)
    print("PTF yöntemi: " + yontem + (" (" + yontem_detay + ")" if yontem_detay else ""))
    if yontem == "duz":
        print(
            "UYARI: ne yayımlanan günlük ortalama ne de eşleşme miktarı alınabildi; "
            "düz ortalama kullanıldı. --dry-run çıktısına bakıp uç adlarını güncelle.",
            file=sys.stderr,
        )

    # PTF MWh başına TL olarak yayımlanır; uygulama kWh ile çalışıyor.
    mcp_points = to_points(gunluk, Decimal(1000), "0.000001")

    yekdem_values: dict[str, Decimal] = {}
    for row in yekdem_rows:
        day = to_day(pick(row, DATE_FIELD_CANDIDATES))
        price = pick(row, PRICE_FIELD_CANDIDATES)
        if day and price is not None:
            yekdem_values[day] = Decimal(str(price))
    # YEKDEM ay bazında saklanır (TL/MWh olarak, mevzuatın yayımladığı birim).
    yekdem_months = monthly_yekdem(yekdem_values)

    document = build_catalog(
        mcp_points,
        yekdem_months,
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        yekdem_forecast=ongoruler,
        method=yontem,
        method_detail=yontem_detay,
    )

    out_path = args.out
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    print(
        f"Yazıldı: {out_path} · PTF {len(mcp_points)} gün ({yontem}) "
        f"· YEKDEM öngörü {len(ongoruler)} ay + gerçekleşen {len(yekdem_months)} ay"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except EpiasError as error:
        print(f"HATA: {error}", file=sys.stderr)
        sys.exit(1)
