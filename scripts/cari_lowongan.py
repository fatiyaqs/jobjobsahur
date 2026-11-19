"""
Pencari lowongan otomatis untuk Job Vacancy Tracker.

Dijalankan oleh GitHub Actions (lihat .github/workflows/cari-lowongan.yml) setiap hari.
Script ini:
  1. Mengambil lowongan terbaru dari beberapa sumber (Kalibrr, Jobstreet, Telegram GIS/RS Jobs, Disnakerja,
     Dealls, Glints, Talentics).
  2. Memberi skor kecocokan terhadap minat setiap orang di tracker (diatur dari tab
     Rekomendasi, tersimpan di koleksi "profil"; profil awal: 6 CV Fatiya).
  3. Menyimpan lowongan yang cocok ke Firestore (koleksi "rekomendasi"), lalu
     tracker menampilkannya di tab "Rekomendasi".

Ubah PROFIL, KATA_KUNCI_CARI, atau LOKASI_DISUKAI di bawah kalau kriteria berubah.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup

# ============================================================
# 1. KONFIGURASI
# ============================================================
FIREBASE_API_KEY = os.environ.get("FIREBASE_API_KEY", "AIzaSyCUPAlkUIyZilT98o2RsZP0DOdWtTCW8Dk")
FIREBASE_PROJECT = os.environ.get("FIREBASE_PROJECT", "jobjobsahur-job")
# Minat tiap orang diatur dari tracker (tab Rekomendasi → "Atur minat pencarian") dan
# tersimpan di Firestore koleksi "profil". PROFIL di bawah hanya dipakai sekali sebagai
# profil awal untuk PEMILIK_AWAL kalau belum ada profil sama sekali.
PEMILIK_AWAL = "Fatiya"
SKOR_MINIMUM = 6              # lowongan di bawah skor ini tidak disimpan
MAKS_HARI = 21                # abaikan lowongan yang diposting lebih lama dari ini

MAKS_KATA_CARI = 30          # batas jumlah kata kunci yang dikirim ke tiap situs per run

# Kata kunci pencarian tambahan (selain yang diambil dari minat tiap orang)
KATA_KUNCI_CARI = [
    "GIS", "geospatial", "remote sensing", "pemetaan", "geografi", "kartografi",
    "surveyor", "lingkungan", "environmental", "research assistant", "asisten peneliti",
    "admin", "administrasi", "data entry", "operations staff", "inventory",
    "human resources", "HR staff", "recruitment",
]

# Profil awal Fatiya (6 CV). Kata kunci judul bernilai 3, kata kunci deskripsi bernilai 1.
PROFIL = {
    "GIS": {
        "judul": ["gis", "geospatial", "geospasial", "spatial", "spasial", "remote sensing",
                  "penginderaan jauh", "mapping", "pemetaan", "cartograph", "kartograf",
                  "surveyor", "geomatic", "geodesi", "land use", "tata ruang"],
        "isi": ["arcgis", "qgis", "google earth engine", "landsat", "sentinel", "satellite",
                "citra", "shapefile", "postgis", "geografi", "geography", "survey lapangan",
                "field survey", "ground truth"],
    },
    "Research": {
        "judul": ["research", "riset", "peneliti", "researcher", "enumerator", "field officer",
                  "monitoring", "evaluation", "m&e", "analyst lingkungan", "environmental",
                  "lingkungan", "sustainability", "conservation", "konservasi"],
        "isi": ["penelitian", "survey", "survei", "data collection", "pengumpulan data",
                "report", "laporan", "amdal", "klhs", "spss", "geografi", "geography"],
    },
    "Operations": {
        "judul": ["operation", "operasional", "inventory", "inventori", "warehouse", "gudang",
                  "logistic", "logistik", "stock", "supply chain", "procurement"],
        "isi": ["stok", "stock opname", "inventory", "excel", "order", "pengiriman",
                "rekap", "data entry"],
    },
    "Administration": {
        "judul": ["admin", "administrasi", "administrative", "data entry", "secretary",
                  "sekretaris", "office", "document control", "clerk", "back office"],
        "isi": ["excel", "arsip", "filing", "dokumen", "surat", "input data",
                "microsoft office", "google workspace", "jadwal", "scheduling"],
    },
    "Human Resources": {
        "judul": ["hr", "human resource", "sdm", "recruit", "talent acquisition", "people",
                  "personalia", "general affair", "ga staff"],
        "isi": ["rekrutmen", "recruitment", "onboarding", "payroll", "absensi", "employee",
                "karyawan", "interview"],
    },
    "General": {
        "judul": ["management trainee", "graduate", "trainee", "staff", "officer",
                  "assistant", "asisten", "junior"],
        "isi": ["fresh graduate", "lulusan baru", "s1", "bachelor", "sarjana"],
    },
}

# Profil awal Tania (4 CV: Administration, Operations, GIS, HR & Admin).
PROFIL_TANIA = {
    "Administration": {
        "judul": ["admin", "administrasi", "administrative", "administration", "data entry", "secretary",
                  "sekretaris", "office", "document control", "project admin", "back office", "clerk"],
        "isi": ["excel", "google workspace", "arsip", "dokumen", "surat", "laporan", "notulen",
                "meeting minutes", "input data", "scheduling", "jadwal", "correspondence"],
    },
    "Operations": {
        "judul": ["operation", "operasional", "project coordinator", "project support", "project officer",
                  "facility", "asset", "inventory", "general affair", "business support", "event"],
        "isi": ["asset", "aset", "inventory", "data management", "reporting", "koordinasi",
                "process improvement", "stakeholder", "monitoring", "proposal"],
    },
    "GIS": {
        "judul": ["gis", "geospatial", "geospasial", "spatial", "spasial", "remote sensing",
                  "penginderaan jauh", "mapping", "pemetaan", "cartograph", "kartograf", "surveyor",
                  "land use", "tata ruang"],
        "isi": ["arcgis", "qgis", "google earth engine", "envi", "satellite", "citra", "raster",
                "land cover", "geografi", "geography", "field survey"],
    },
    "HR & Admin": {
        "judul": ["hr", "human resource", "sdm", "recruit", "talent acquisition", "personalia",
                  "hr admin", "people", "hrga"],
        "isi": ["rekrutmen", "recruitment", "interview", "onboarding", "payroll", "assessment",
                "candidate", "kandidat", "scheduling", "absensi"],
    },
}

# Profil yang dibuat otomatis kalau orangnya belum punya profil di tracker.
# Setelah dibuat, ubah dari tracker (tab Rekomendasi → Atur minat pencarian), bukan di sini.
PROFIL_AWAL = {"Fatiya": PROFIL, "Tania": PROFIL_TANIA}

# Lowongan yang terlalu senior atau tidak relevan
KATA_NEGATIF_JUDUL = ["senior", "sr.", "manager", "manajer", "head", "lead", "director",
                      "direktur", "principal", "vp ", "chief", "superintendent", "kepala",
                      "sales", "driver", "kurir", "teknisi", "mekanik", "developer",
                      "engineer software", "programmer", "akuntan", "accountant", "dokter",
                      "perawat", "nurse", "guru"]
KATA_NEGATIF_ISI = ["minimal 5 tahun", "minimum 5 years", "5+ years", "min. 5", "7+ years",
                    "10+ years", "minimal 4 tahun", "4+ years"]
KATA_POSITIF_ISI = ["fresh graduate", "lulusan baru", "fresh grad", "0-1", "0 - 1",
                    "1 tahun", "1 year", "entry level"]

# Hanya lowongan di Jabodetabek yang disimpan.
WILAYAH = ["jakarta", "dki", "jabodetabek", "depok", "bogor", "cibinong", "sentul", "cileungsi",
           "cibubur", "bekasi", "cikarang", "tangerang", "tangsel", "bsd", "serpong", "karawaci",
           "alam sutera", "ciputat", "pamulang", "bintaro", "cikupa", "balaraja"]
TERIMA_REMOTE = False   # ubah ke True kalau lowongan remote juga ingin ditampilkan

TELEGRAM_CHANNELS = ["gis_id_jobs"]
DISNAKERJA_HALAMAN = 6        # jumlah halaman daftar terbaru Disnakerja yang dibaca (±12 perusahaan/halaman)

# Situs yang dibaca dari halaman daftar/sitemap lalu halaman detail tiap lowongan.
# {q} = kata kunci (URL-encoded), {slug} = kata kunci format-url (mis. data-analyst).
SUMBER_HTML = {
    "Dealls": {
        "daftar": ["https://dealls.com/loker/posisi/loker-{slug}", "https://dealls.com/?searchJob={q}"],
        "pola": r"https?://(?:www\.)?dealls\.com/(?:en/)?loker/[a-z0-9-]+~[a-z0-9-]+",
        "sitemap": ["https://dealls.com/sitemap.xml"],
    },
    "Glints": {
        "daftar": ["https://glints.com/id/opportunities/jobs/explore?keyword={q}&country=ID&locationName=All+Cities%2FProvinces",
                   "https://glints.com/id/lowongan-kerja/{slug}"],
        "pola": r"https?://glints\.com/id/(?:en/)?opportunities/jobs/[a-z0-9-]+/[0-9a-f]{8}-[0-9a-f-]{27}",
        "sitemap": [],
    },
    "Talentics": {
        "daftar": ["https://jobs.talentics.id/jobs?keyword={q}", "https://jobs.talentics.id/jobs"],
        "pola": r"https?://jobs\.talentics\.id/(?!jobs/?$|login|register|faq|about|privacy|terms)[a-z0-9-]+/[a-z0-9][^\s\"'<>#?]*",
        "sitemap": ["https://jobs.talentics.id/sitemap.xml"],
        "wajib_jsonld": True,   # halaman daftarnya dimuat JavaScript, jadi hanya terima detail yang jelas lowongan
    },
}
MAKS_DETAIL_PER_SUMBER = 40   # batas halaman detail yang dibuka per situs per run
BATAS_MENIT = 25              # setelah ini pencarian dihentikan dan hasil yang ada langsung disimpan
GAGAL_BERUNTUN_MAKS = 2       # situs yang gagal 2x berturut-turut dilewati sisanya (biasanya karena memblokir)
_MULAI = time.monotonic()


def waktu_habis() -> bool:
    return time.monotonic() - _MULAI > BATAS_MENIT * 60

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Accept-Language": "id-ID,id;q=0.9,en;q=0.8"}


# ============================================================
# 2. UTILITAS
# ============================================================
def bersih(teks) -> str:
    if teks is None:
        return ""
    if not isinstance(teks, str):
        teks = str(teks)
    teks = html.unescape(re.sub(r"<[^>]+>", " ", teks))
    return re.sub(r"\s+", " ", teks).strip()


def tanggal_iso(nilai) -> str:
    """Ubah berbagai format tanggal ke YYYY-MM-DD; '' kalau gagal."""
    if not nilai:
        return ""
    s = str(nilai).strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    if m:
        return m.group(1)
    try:
        return datetime.fromtimestamp(float(s) / (1000 if float(s) > 1e11 else 1), tz=timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return ""


def ambil(d, *jalur, default=""):
    """Ambil nilai bertingkat: ambil(job, 'company', 'name')."""
    for kunci in jalur:
        if isinstance(d, dict):
            d = d.get(kunci)
        elif isinstance(d, list) and isinstance(kunci, int) and -len(d) <= kunci < len(d):
            d = d[kunci]
        else:
            return default
        if d is None:
            return default
    return d


def pertama(*nilai):
    for v in nilai:
        if v not in (None, "", [], {}):
            return v
    return ""


def deteksi_sumber(url: str) -> str:
    u = (url or "").lower()
    for k, v in [("linkedin", "LinkedIn"), ("jobstreet", "Jobstreet"), ("glints", "Glints"),
                 ("kalibrr", "Kalibrr"), ("instagram", "Instagram"), ("dealls", "Dealls")]:
        if k in u:
            return v
    return "Website Perusahaan" if u.startswith("http") else ""


# ============================================================
# 3. SUMBER LOWONGAN
# ============================================================
def sumber_kalibrr(kata: str) -> list[dict]:
    url = "https://www.kalibrr.com/kjs/job_board/search"
    r = requests.get(url, params={"limit": 40, "offset": 0, "text": kata, "country": "Indonesia"},
                     headers=UA, timeout=15)
    r.raise_for_status()
    data = r.json()
    jobs = data.get("jobs") if isinstance(data, dict) else data
    hasil = []
    for j in jobs or []:
        jid = pertama(j.get("id"), j.get("job_id"))
        kode = pertama(ambil(j, "company", "code"), j.get("company_code"), "company")
        nama = pertama(j.get("name"), j.get("title"))
        slug = re.sub(r"[^a-z0-9]+", "-", str(nama).lower()).strip("-")
        lokasi = pertama(ambil(j, "google_location", "address_components", "city"),
                         ambil(j, "location", "city"), j.get("city"),
                         ambil(j, "google_location", "formatted_address"))
        hasil.append({
            "sumber": "Kalibrr",
            "sid": f"kalibrr-{jid}",
            "posisi": bersih(nama),
            "perusahaan": bersih(pertama(j.get("company_name"), ambil(j, "company", "name"))),
            "lokasi": bersih(lokasi),
            "sistem": "Remote" if j.get("is_work_from_home") else "",
            "link": f"https://www.kalibrr.com/c/{kode}/jobs/{jid}/{slug}",
            "diposting": tanggal_iso(pertama(j.get("activation_date"), j.get("created_at"))),
            "deadline": tanggal_iso(j.get("application_end_date")),
            "isi": bersih(" ".join(str(x) for x in [j.get("description", ""), j.get("qualifications", ""),
                                                     j.get("function", ""), j.get("education_level", "")])),
        })
    return hasil


def sumber_jobstreet(kata: str) -> list[dict]:
    url = "https://id.jobstreet.com/api/jobsearch/v5/search"
    params = {"siteKey": "ID-Main", "sourcesystem": "houston", "keywords": kata,
              "page": 1, "pageSize": 40, "sortmode": "ListedDate", "locale": "id-ID",
              "daterange": MAKS_HARI}
    r = requests.get(url, params=params, headers={**UA, "Accept": "application/json"}, timeout=15)
    r.raise_for_status()
    data = r.json()
    hasil = []
    for j in data.get("data", []) or []:
        jid = j.get("id")
        sistem = ""
        wa = ambil(j, "workArrangements", "data", 0, "label", "text")
        if isinstance(wa, str):
            sistem = {"remote": "Remote", "hybrid": "Hybrid", "on-site": "On-site",
                      "di kantor": "On-site", "jarak jauh": "Remote"}.get(wa.lower(), wa)
        hasil.append({
            "sumber": "Jobstreet",
            "sid": f"jobstreet-{jid}",
            "posisi": bersih(j.get("title")),
            "perusahaan": bersih(pertama(ambil(j, "advertiser", "description"), j.get("companyName"))),
            "lokasi": bersih(pertama(ambil(j, "locations", 0, "label"), ambil(j, "jobLocation", "label"),
                                     j.get("location"))),
            "sistem": sistem,
            "link": f"https://id.jobstreet.com/id/job/{jid}",
            "diposting": tanggal_iso(j.get("listingDate")),
            "deadline": "",
            "gaji": bersih(j.get("salaryLabel")),
            "isi": bersih(" ".join([str(j.get("teaser", "")), " ".join(map(str, j.get("bulletPoints") or []))])),
        })
    return hasil


BUKAN_POSTING = {"kategori", "industri", "tipe", "pendidikan", "lokasi", "blog", "kontak", "terbaru",
                 "page", "berdasarkan-jurusan", "tag", "author", "about", "tentang", "privacy-policy",
                 "disclaimer", "wp-content", "wp-admin", "feed", "category", "search", "contact"}
LABEL_DISNAKERJA = ["Last Update", "Kategori", "Lokasi", "Tipe Pekerjaan", "Pendidikan", "Pengalaman",
                    "Jumlah Posisi", "Gaji", "Deskripsi Pekerjaan", "Persyaratan", "Kualifikasi"]


def _label(teks: str, label: str) -> str:
    """Ambil nilai setelah 'Label:' sampai label berikutnya atau akhir baris."""
    lain = "|".join(re.escape(x) for x in LABEL_DISNAKERJA if x != label)
    m = re.search(rf"{re.escape(label)}\s*:\s*(.+?)(?=\s*(?:{lain})\s*:|\n|$)", teks)
    return m.group(1).strip() if m else ""


def tautan_disnakerja(halaman: int) -> list[str]:
    url = "https://www.disnakerja.com/" if halaman == 1 else f"https://www.disnakerja.com/page/{halaman}/"
    r = requests.get(url, headers=UA, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    hasil = []
    for a in soup.select("a[href]"):
        m = re.match(r"https?://(?:www\.)?disnakerja\.com/([a-z0-9-]+)/?$", a["href"].split("#")[0].split("?")[0])
        if m and m.group(1) not in BUKAN_POSTING and m.group(1) not in hasil:
            hasil.append(m.group(1))
    return [f"https://www.disnakerja.com/{slug}/" for slug in hasil]


def sumber_disnakerja(halaman: int) -> list[dict]:
    hasil = []
    for url in tautan_disnakerja(halaman):
        if waktu_habis():
            break
        try:
            r = requests.get(url, headers=UA, timeout=15)
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"[Disnakerja] lewati {url}: {e}", file=sys.stderr)
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        h1 = soup.find("h1")
        judul = bersih(h1.get_text(" ")) if h1 else ""
        badan = soup.find("article") or soup.select_one(".entry-content, .post-content, main") or soup
        teks = badan.get_text("\n", strip=True)
        if not judul or "Persyaratan" not in teks and "Kualifikasi" not in teks:
            time.sleep(0.6)
            continue
        perusahaan = re.sub(r"(?i)^(lowongan kerja|loker|rekrutmen|recruitment)\s+", "", judul).strip()
        # Kandidat nama posisi: teks tebal pendek yang bukan label
        posisi = []
        for b in badan.find_all(["strong", "b", "h2", "h3", "h4"]):
            t = bersih(b.get_text(" "))
            if 3 <= len(t) <= 80 and not t.endswith(":") and t.lower() not in (x.lower() for x in LABEL_DISNAKERJA) \
                    and not re.search(r"(?i)perusahaan lainnya|pesan sponsor|share|bagikan|lowongan ini", t) and t not in posisi:
                posisi.append(t)
        lokasi = _label(teks, "Lokasi")
        pengalaman = _label(teks, "Pengalaman")
        update = _label(teks, "Last Update")
        try:
            diposting = datetime.strptime(update, "%B %d, %Y").strftime("%Y-%m-%d") if update else ""
        except ValueError:
            diposting = tanggal_iso(update)
        m_email = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", teks)
        m_dl = re.search(r"(?i)(?:batas[^:\n]{0,25}|deadline|ditutup|closing date)\s*:?\s*(\d{1,2}\s+[A-Za-z]+\s+20\d\d)", teks)
        isi = f"{teks[:3000]} pengalaman {pengalaman} pendidikan {_label(teks, 'Pendidikan')}"
        # Pilih posisi yang paling cocok dengan profil sebagai judul rekomendasi
        kandidat = [{"posisi": p, "isi": isi, "perusahaan": perusahaan} for p in posisi[:15]] or \
                   [{"posisi": perusahaan, "isi": isi, "perusahaan": perusahaan}]
        terbaik = max(kandidat, key=skor_terbaik)
        m_th = re.search(r"(\d+)", pengalaman)
        if m_th and int(m_th.group(1)) >= 3:
            isi += " minimal 5 tahun"  # dorong skor turun untuk lowongan berpengalaman
        hasil.append({
            "sumber": "Disnakerja",
            "sid": f"disnakerja-{url.rstrip('/').rsplit('/', 1)[-1]}",
            "posisi": terbaik["posisi"][:140] + (f" (+{len(posisi) - 1} posisi lain)" if len(posisi) > 1 else ""),
            "perusahaan": perusahaan[:120],
            "lokasi": lokasi[:120],
            "sistem": "",
            "link": url,
            "kontak": m_email.group(0) if m_email else "",
            "diposting": diposting,
            "deadline": "",
            "deadline_teks": m_dl.group(1) if m_dl else "",
            "isi": isi,
        })
        time.sleep(0.6)
    return hasil


TANDA_DITUTUP = re.compile(r"(?i)(lowongan ini (sudah|telah) ditutup|no longer accepting|job (is )?closed|"
                           r"lowongan (sudah )?(tidak tersedia|kedaluwarsa)|this job has expired)")


def _slug(k: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", k.lower()).strip("-")


def _cari_jobposting(soup: BeautifulSoup) -> dict | None:
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except (ValueError, TypeError):
            continue
        antrian = data if isinstance(data, list) else [data]
        while antrian:
            d = antrian.pop(0)
            if isinstance(d, list):
                antrian.extend(d); continue
            if not isinstance(d, dict):
                continue
            jenis = d.get("@type")
            if jenis == "JobPosting" or (isinstance(jenis, list) and "JobPosting" in jenis):
                return d
            antrian.extend(v for v in d.values() if isinstance(v, (dict, list)))
    return None


def _lokasi_jsonld(jp: dict) -> tuple[str, str]:
    tempat = jp.get("jobLocation") or []
    tempat = tempat if isinstance(tempat, list) else [tempat]
    nama = []
    for t in tempat:
        alamat = t.get("address", {}) if isinstance(t, dict) else {}
        if isinstance(alamat, str):
            nama.append(alamat); continue
        nama += [str(alamat.get(k)) for k in ("addressLocality", "addressRegion") if alamat.get(k)]
    sistem = "Remote" if "TELECOMMUTE" in str(jp.get("jobLocationType", "")).upper() else ""
    return ", ".join(dict.fromkeys(nama)), sistem


def _gaji_jsonld(jp: dict) -> str:
    v = ambil(jp, "baseSalary", "value")
    if isinstance(v, dict) and (v.get("minValue") or v.get("maxValue")):
        rp = lambda x: f"Rp{float(x)/1e6:.1f} jt".replace(".0 ", " ") if x else ""
        return " – ".join(x for x in [rp(v.get("minValue")), rp(v.get("maxValue"))] if x)
    return ""


def baca_detail(url: str, isi_html: str, nama: str, wajib_jsonld: bool = False) -> dict | None:
    soup = BeautifulSoup(isi_html, "html.parser")
    jp = _cari_jobposting(soup)
    for t in soup(["script", "style", "noscript", "nav", "header", "footer", "svg"]):
        t.decompose()
    utama = soup.find("main") or soup.find("article") or soup.body or soup
    teks = utama.get_text("\n", strip=True)
    if TANDA_DITUTUP.search(teks):
        return None
    if jp:
        lokasi, sistem = _lokasi_jsonld(jp)
        posisi = bersih(jp.get("title"))
        perusahaan = bersih(ambil(jp, "hiringOrganization", "name") or "")
        diposting, deadline = tanggal_iso(jp.get("datePosted")), tanggal_iso(jp.get("validThrough"))
        isi, gaji = bersih(jp.get("description"))[:3000], _gaji_jsonld(jp)
    else:
        if wajib_jsonld:
            return None
        meta = lambda prop: (soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop}) or {}).get("content", "")
        h1 = soup.find("h1")
        posisi = bersih(h1.get_text(" ") if h1 else meta("og:title"))
        posisi = re.split(r"\s+[|\-–]\s+(?:Glints|Dealls|Talentics)", posisi)[0]
        desk = meta("og:description") or meta("description")
        m = re.search(r"(?i)\b(?:at|di)\s+(.+?)\s+(?:in|di)\s+([A-Z][\w .,-]+)", desk or "")
        perusahaan = bersih(m.group(1)) if m else ""
        lokasi = bersih(m.group(2)) if m else next((w.title() for w in WILAYAH if w in (desk or "").lower()), "")
        sistem = "Remote" if re.search(r"(?i)\bremote\b|jarak jauh", desk or "") else ""
        diposting, deadline, gaji, isi = "", "", "", teks[:3000]
    if not posisi:
        return None
    m_email = re.search(r"[\w.+-]+@[\w-]+\.[a-z]{2,}(?:\.[a-z]{2,})?", teks)
    return {"sumber": nama, "sid": f"{_slug(nama)}-{hashlib.md5(url.split('?')[0].encode()).hexdigest()[:14]}",
            "posisi": posisi[:140], "perusahaan": perusahaan[:120], "lokasi": lokasi[:120], "sistem": sistem,
            "link": url, "kontak": m_email.group(0) if m_email else "", "diposting": diposting,
            "deadline": deadline, "gaji": gaji, "isi": isi}


def _tautan_sitemap(url: str, pola: re.Pattern, kedalaman: int = 0) -> list[str]:
    r = requests.get(url, headers=UA, timeout=15)
    r.raise_for_status()
    batas = (datetime.now(timezone.utc) - timedelta(days=MAKS_HARI)).strftime("%Y-%m-%d")
    hasil = []
    for blok in re.findall(r"<(?:url|sitemap)>(.*?)</(?:url|sitemap)>", r.text, re.S):
        loc = re.search(r"<loc>\s*(.*?)\s*</loc>", blok)
        mod = re.search(r"<lastmod>\s*(.*?)\s*</lastmod>", blok)
        if not loc:
            continue
        loc = html.unescape(loc.group(1))
        if loc.endswith(".xml") or "sitemap" in loc:
            if kedalaman < 1 and re.search(r"(?i)job|loker|lowongan|vacanc|career", loc):
                hasil += _tautan_sitemap(loc, pola, kedalaman + 1)
            continue
        if pola.match(loc) and (not mod or tanggal_iso(mod.group(1)) >= batas):
            hasil.append(loc)
    return hasil


def sumber_html(nama: str, kata_cari: list[str]) -> list[dict]:
    cfg = SUMBER_HTML[nama]
    pola = re.compile(cfg["pola"])
    tautan, catatan = [], []
    gagal = 0
    for kata in kata_cari:
        if waktu_habis() or gagal >= 3:
            break
        for templat in cfg["daftar"]:
            url = templat.format(q=quote_plus(kata), slug=_slug(kata))
            try:
                r = requests.get(url, headers=UA, timeout=15)
                if r.status_code == 404:
                    continue
                r.raise_for_status()
                gagal = 0
            except requests.RequestException as e:
                gagal += 1
                catatan.append(f"{url}: {e}"); continue
            for href in re.findall(r"""href=["']([^"'#]+)""", r.text) + re.findall(r"https?://[^\s\"'<>]+", r.text):
                u = urljoin(url, html.unescape(href)).split("?")[0].rstrip("/")
                if pola.match(u) and u not in tautan:
                    tautan.append(u)
            time.sleep(0.8)
    for sm in cfg.get("sitemap", []):
        try:
            tautan += [u for u in _tautan_sitemap(sm, pola) if u not in tautan]
        except requests.RequestException as e:
            catatan.append(f"sitemap: {e}")
    if not tautan:
        raise RuntimeError("Tidak ada tautan lowongan ditemukan (situs memblokir atau strukturnya berubah). "
                           + "; ".join(catatan[:2]))
    # Dahulukan tautan yang judulnya (slug) mengandung kata kunci minat
    kata_slug = {_slug(k) for k in kata_cari if len(k) >= 3}
    cocok = [u for u in tautan if any(k in u.lower() for k in kata_slug)]
    urut = (cocok + [u for u in tautan if u not in cocok])[:MAKS_DETAIL_PER_SUMBER]
    hasil = []
    for u in urut:
        if waktu_habis():
            break
        try:
            r = requests.get(u, headers=UA, timeout=15)
            r.raise_for_status()
            d = baca_detail(u, r.text, nama, cfg.get("wajib_jsonld", False))
            if d:
                hasil.append(d)
        except requests.RequestException as e:
            print(f"[{nama}] lewati {u}: {e}", file=sys.stderr)
        time.sleep(0.8)
    print(f"[{nama}] {len(tautan)} tautan, {len(urut)} dibuka, {len(hasil)} lowongan terbaca")
    return hasil


def sumber_telegram(channel: str) -> list[dict]:
    r = requests.get(f"https://t.me/s/{channel}", headers=UA, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    hasil = []
    for pesan in soup.select(".tgme_widget_message"):
        teks_el = pesan.select_one(".tgme_widget_message_text")
        if not teks_el:
            continue
        teks = teks_el.get_text("\n", strip=True)
        if len(teks) < 25:
            continue
        waktu = pesan.select_one("time[datetime]")
        diposting = tanggal_iso(waktu["datetime"]) if waktu else ""
        tautan = [a.get("href") for a in teks_el.select("a[href]")
                  if a.get("href", "").startswith("http") and "t.me/" not in a.get("href", "")]
        post = pesan.get("data-post", "")
        baris = [b.strip(" *•-–📢🔥✨") for b in teks.split("\n") if b.strip()]
        posisi = next((b for b in baris if re.search(r"(?i)gis|spatial|spasial|remote sensing|mapping|"
                                                        r"analyst|officer|specialist|staff|assistant|engineer|"
                                                        r"surveyor|research", b)), baris[0] if baris else "")
        m_email = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", teks)
        m_dl = re.search(r"(?i)(?:deadline|batas[^:\n]*|closing[^:\n]*)\s*:?\s*([0-9]{1,2}\s+\w+\s+20\d\d)", teks)
        hasil.append({
            "sumber": "Telegram GIS/RS Jobs",
            "sid": f"tg-{post or hashlib.md5(teks.encode()).hexdigest()[:12]}",
            "posisi": posisi[:140],
            "perusahaan": "",
            "lokasi": "",
            "sistem": "",
            "link": tautan[0] if tautan else (f"https://t.me/{post}" if post else f"https://t.me/s/{channel}"),
            "kontak": m_email.group(0) if m_email else "",
            "diposting": diposting,
            "deadline": "",
            "deadline_teks": m_dl.group(1) if m_dl else "",
            "isi": teks[:2000],
        })
    return hasil


# ============================================================
# 4. PENILAIAN KECOCOKAN
# ============================================================
def ada(kata: str, teks: str) -> bool:
    kata = kata.lower()
    if len(kata) <= 3:  # kata pendek (gis, hr, ga) harus berdiri sendiri
        return re.search(rf"(?<![a-z]){re.escape(kata)}(?![a-z])", teks) is not None
    return kata in teks


PROFIL_ORANG: dict[str, dict] = {}   # {nama: {label: {"judul": [...], "isi": [...]}}}, diisi di main()


def nilai(lowongan: dict, minat: dict | None = None) -> tuple[int, str, list[str]]:
    """Skor kecocokan lowongan terhadap satu set minat (default: profil awal)."""
    minat = PROFIL if minat is None else minat
    judul = lowongan.get("posisi", "").lower()
    isi = (lowongan.get("isi", "") + " " + lowongan.get("perusahaan", "")).lower()
    terbaik, cv_terbaik, alasan_terbaik = 0, next(iter(minat), "Umum"), []
    for cv, kk in minat.items():
        skor, alasan = 0, []
        for k in kk.get("judul", []):
            if ada(k, judul):
                skor += 3; alasan.append(k)
            elif ada(k, isi):
                skor += 1
        for k in kk.get("isi", []):
            if ada(k, judul) or ada(k, isi):
                skor += 1; alasan.append(k)
        if cv.lower() in ("general", "umum"):
            skor = min(skor, 4)  # profil umum hanya sebagai cadangan
        if skor > terbaik:
            terbaik, cv_terbaik, alasan_terbaik = skor, cv, alasan
    bonus = 0
    # Kata negatif diabaikan kalau orang ini memang mencari bidang tersebut (mis. "sales", "developer")
    milik = " ".join(j for kk in minat.values() for j in kk.get("judul", []))
    negatif = [k for k in KATA_NEGATIF_JUDUL if k.strip(" .") not in milik]
    if any(ada(k, judul) for k in negatif):
        bonus -= 8
    if any(k in isi for k in KATA_NEGATIF_ISI):
        bonus -= 4
    if any(k in isi or k in judul for k in KATA_POSITIF_ISI):
        bonus += 2; alasan_terbaik.append("cocok untuk fresh graduate")
    return terbaik + bonus, cv_terbaik, list(dict.fromkeys(alasan_terbaik))[:6]


def nilai_semua(lowongan: dict) -> dict:
    """{nama: {cv, skor, alasan}} untuk tiap orang yang skornya lolos SKOR_MINIMUM."""
    hasil = {}
    for nama, minat in (PROFIL_ORANG or {PEMILIK_AWAL: PROFIL}).items():
        if not minat:
            continue
        skor, cv, alasan = nilai(lowongan, minat)
        if skor >= SKOR_MINIMUM:
            hasil[nama] = {"cv": cv, "skor": skor, "alasan": alasan}
    return hasil


def skor_terbaik(lowongan: dict) -> int:
    return max((nilai(lowongan, m)[0] for m in (PROFIL_ORANG or {PEMILIK_AWAL: PROFIL}).values() if m), default=0)


# ============================================================
# 5. FIRESTORE (REST)
# ============================================================
BASE = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT}/databases/(default)/documents"


def ke_firestore(v):
    if isinstance(v, bool):
        return {"booleanValue": v}
    if isinstance(v, int):
        return {"integerValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, list):
        return {"arrayValue": {"values": [ke_firestore(x) for x in v]}}
    if isinstance(v, dict):
        return {"mapValue": {"fields": {k: ke_firestore(x) for k, x in v.items()}}}
    if v is None:
        return {"nullValue": None}
    return {"stringValue": str(v)}


def dari_firestore(v: dict):
    if not isinstance(v, dict) or not v:
        return None
    jenis, isi = next(iter(v.items()))
    if jenis == "arrayValue":
        return [dari_firestore(x) for x in (isi or {}).get("values", [])]
    if jenis == "mapValue":
        return {k: dari_firestore(x) for k, x in (isi or {}).get("fields", {}).items()}
    if jenis == "integerValue":
        return int(isi)
    return isi


def daftar_dokumen(koleksi: str) -> list[dict]:
    hasil, token = [], None
    while True:
        params = {"key": FIREBASE_API_KEY, "pageSize": 300}
        if token:
            params["pageToken"] = token
        r = requests.get(f"{BASE}/{koleksi}", params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        for d in data.get("documents", []):
            hasil.append({"_name": d["name"], **{k: dari_firestore(v) for k, v in d.get("fields", {}).items()}})
        token = data.get("nextPageToken")
        if not token:
            return hasil


def _daftar_kata(nilai_) -> list[str]:
    if isinstance(nilai_, str):
        nilai_ = nilai_.split(",")
    return [str(x).strip().lower() for x in (nilai_ or []) if str(x).strip()]


def muat_profil() -> dict[str, dict]:
    """Baca minat tiap orang dari koleksi 'profil'. Buat profil awal untuk orang di PROFIL_AWAL yang belum ada."""
    profil, sudah = {}, set()
    for d in daftar_dokumen("profil"):
        nama = (d.get("nama") or "").strip()
        sudah.add(nama.lower())
        minat = {}
        for m in d.get("minat") or []:
            if not isinstance(m, dict):
                continue
            label = (m.get("label") or "").strip() or "Minat"
            judul, isi = _daftar_kata(m.get("judul")), _daftar_kata(m.get("isi"))
            if judul or isi:
                minat[label] = {"judul": judul, "isi": isi}
        if nama and minat:
            profil[nama] = minat
    for nama, minat in PROFIL_AWAL.items():
        if nama.lower() in sudah:
            continue  # profil yang sudah ada (termasuk yang sudah diedit) tidak ditimpa
        awal = {"nama": nama, "minat": [{"label": k, "judul": v["judul"], "isi": v["isi"]} for k, v in minat.items()]}
        doc_id = re.sub(r"[^a-z0-9]+", "-", nama.lower()).strip("-") or "awal"
        requests.patch(f"{BASE}/profil/{doc_id}", params={"key": FIREBASE_API_KEY},
                       json={"fields": {k: ke_firestore(v) for k, v in awal.items()}}, timeout=20).raise_for_status()
        profil[nama] = minat
        print(f"Profil awal untuk {nama} dibuat.")
    return profil


def kata_cari_dari_profil(profil: dict[str, dict]) -> list[str]:
    kata = []
    for minat in profil.values():
        for kk in minat.values():
            kata += [k for k in kk.get("judul", []) if len(k) >= 3][:4]
    kata += [k.lower() for k in KATA_KUNCI_CARI]
    return list(dict.fromkeys(kata))[:MAKS_KATA_CARI]


def id_dokumen(sid: str) -> str:
    return hashlib.sha1(sid.encode()).hexdigest()[:20]


def sudah_ada(doc_id: str) -> bool:
    r = requests.get(f"{BASE}/rekomendasi/{doc_id}", params={"key": FIREBASE_API_KEY}, timeout=20)
    return r.status_code == 200


def perbarui_rekomendasi(doc_id: str, data: dict) -> None:
    """Perbarui data lowongan & kecocokan tanpa menyentuh status/abaikan yang diatur pengguna."""
    params = [("key", FIREBASE_API_KEY)] + [("updateMask.fieldPaths", k) for k in data]
    requests.patch(f"{BASE}/rekomendasi/{doc_id}", params=params,
                   json={"fields": {k: ke_firestore(v) for k, v in data.items()}}, timeout=20).raise_for_status()


def simpan_rekomendasi(doc_id: str, data: dict) -> str:
    r = requests.post(f"{BASE}/rekomendasi", params={"key": FIREBASE_API_KEY, "documentId": doc_id},
                      json={"fields": {k: ke_firestore(v) for k, v in data.items()}}, timeout=20)
    if r.status_code == 409:
        return "ada"
    r.raise_for_status()
    return "baru"


def rapikan_luar_wilayah() -> int:
    """Tandai rekomendasi lama (status 'baru') di luar Jabodetabek sebagai 'diabaikan'."""
    jumlah, token = 0, None
    while True:
        params = {"key": FIREBASE_API_KEY, "pageSize": 300}
        if token:
            params["pageToken"] = token
        r = requests.get(f"{BASE}/rekomendasi", params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        for d in data.get("documents", []):
            f = {k: next(iter(v.values())) for k, v in d.get("fields", {}).items()
                 if isinstance(v, dict) and v and k in ("lokasi", "sistem", "posisi", "ringkas", "status")}
            low = {"lokasi": f.get("lokasi") or "", "sistem": f.get("sistem") or "",
                   "posisi": f.get("posisi") or "", "isi": f.get("ringkas") or ""}
            if (f.get("status") or "baru") == "baru" and not di_jabodetabek(low):
                requests.patch(f"https://firestore.googleapis.com/v1/{d['name']}",
                               params={"key": FIREBASE_API_KEY, "updateMask.fieldPaths": "status"},
                               json={"fields": {"status": {"stringValue": "diabaikan"}}},
                               timeout=20).raise_for_status()
                jumlah += 1
        token = data.get("nextPageToken")
        if not token:
            return jumlah


def simpan_status(status: dict) -> None:
    r = requests.patch(f"{BASE}/config/scan", params={"key": FIREBASE_API_KEY},
                       json={"fields": {k: ke_firestore(v) for k, v in status.items()}}, timeout=20)
    r.raise_for_status()


# ============================================================
# 6. PROGRAM UTAMA
# ============================================================
def kumpulkan(kata_cari: list[str]) -> tuple[list[dict], dict]:
    semua, laporan = [], {}
    tugas = ([("Kalibrr", sumber_kalibrr, k) for k in kata_cari]
             + [("Jobstreet", sumber_jobstreet, k) for k in kata_cari]
             + [("Telegram", sumber_telegram, c) for c in TELEGRAM_CHANNELS]
             + [("Disnakerja", sumber_disnakerja, n) for n in range(1, DISNAKERJA_HALAMAN + 1)]
             + [(n, (lambda nama, k=kata_cari: sumber_html(nama, k[:12])), n) for n in SUMBER_HTML])
    beruntun: dict[str, int] = {}
    for nama, fungsi, arg in tugas:
        lap = laporan.setdefault(nama, {"ok": 0, "gagal": 0, "lowongan": 0, "error": ""})
        if waktu_habis():
            lap["error"] = lap["error"] or f"dilewati: batas waktu {BATAS_MENIT} menit tercapai"
            continue
        if beruntun.get(nama, 0) >= GAGAL_BERUNTUN_MAKS:
            continue  # situs ini sedang memblokir/bermasalah, jangan buang waktu
        try:
            hasil = fungsi(arg)
            lap["ok"] += 1
            lap["lowongan"] += len(hasil)
            semua.extend(hasil)
            beruntun[nama] = 0
        except Exception as e:  # satu sumber gagal tidak menghentikan yang lain
            lap["gagal"] += 1
            beruntun[nama] = beruntun.get(nama, 0) + 1
            lap["error"] = f"{type(e).__name__}: {str(e)[:160]}"
            if beruntun[nama] >= GAGAL_BERUNTUN_MAKS:
                lap["error"] += f" (dilewati setelah {GAGAL_BERUNTUN_MAKS}x gagal berturut-turut)"
            print(f"[{nama}] gagal untuk '{arg}': {e}", file=sys.stderr)
        time.sleep(1.2)
    print(f"Pengambilan selesai dalam {(time.monotonic() - _MULAI) / 60:.1f} menit")
    return semua, laporan


def di_jabodetabek(low: dict) -> bool:
    """Pakai kolom lokasi; kalau kosong (mis. Telegram), cari nama wilayah di teks lowongan."""
    lok = (low.get("lokasi", "") + " " + low.get("sistem", "")).lower()
    if not low.get("lokasi"):
        lok += " " + (low.get("posisi", "") + " " + low.get("isi", "")).lower()
    if any(k in lok for k in WILAYAH):
        return True
    return TERIMA_REMOTE and any(k in lok for k in ["remote", "wfh", "work from home", "jarak jauh"])


def saring(semua: list[dict]) -> list[dict]:
    batas = (datetime.now(timezone.utc) - timedelta(days=MAKS_HARI)).strftime("%Y-%m-%d")
    hari_ini = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    unik, cocok = {}, []
    for low in semua:
        if not low.get("posisi"):
            continue
        unik.setdefault(low["sid"], low)
    for low in unik.values():
        if low.get("diposting") and low["diposting"] < batas:
            continue
        if low.get("deadline") and low["deadline"] < hari_ini:
            continue
        if not di_jabodetabek(low):
            continue
        per_orang = nilai_semua(low)
        if not per_orang:
            continue
        nama, terbaik = max(per_orang.items(), key=lambda x: x[1]["skor"])
        low.update({"cocok": per_orang, "skor": terbaik["skor"], "cv": terbaik["cv"],
                    "alasan": terbaik["alasan"], "untuk": nama})
        cocok.append(low)
    cocok.sort(key=lambda x: -x["skor"])
    return cocok


def main() -> int:
    kering = "--dry-run" in sys.argv
    global PROFIL_ORANG
    try:
        PROFIL_ORANG = dict(PROFIL_AWAL) if kering else muat_profil()
    except requests.RequestException as e:  # database tidak terjangkau: tetap cari dengan profil awal
        print(f"Gagal membaca profil, memakai profil awal: {e}", file=sys.stderr)
        PROFIL_ORANG = dict(PROFIL_AWAL)
    print("Profil:", ", ".join(f"{n} ({len(m)} minat)" for n, m in PROFIL_ORANG.items()))
    semua, laporan = kumpulkan(kata_cari_dari_profil(PROFIL_ORANG))
    cocok = saring(semua)
    print(f"Total diambil: {len(semua)} | cocok: {len(cocok)}")
    baru = 0
    sekarang = datetime.now(timezone.utc).isoformat()
    for low in cocok:
        data = {
            "perusahaan": low.get("perusahaan", ""), "posisi": low["posisi"],
            "lokasi": low.get("lokasi", ""), "sistem": low.get("sistem", ""),
            "sumber": low.get("sumber", ""), "sumberLink": deteksi_sumber(low.get("link", "")),
            "link": low.get("link", ""), "kontak": low.get("kontak", ""),
            "diposting": low.get("diposting", ""), "deadline": low.get("deadline", ""),
            "deadlineTeks": low.get("deadline_teks", ""), "gaji": low.get("gaji", ""),
            "cv": low["cv"], "skor": low["skor"], "alasan": low["alasan"],
            "ringkas": low.get("isi", "")[:400], "untuk": low["untuk"], "cocok": low["cocok"],
        }
        siapa = ", ".join(f"{n}:{c['cv']}" for n, c in low["cocok"].items())
        print(f"  [{low['skor']:>2}] {siapa:<40} "
              f"{low['posisi'][:50]} — {low.get('perusahaan','')}")
        if kering:
            continue
        doc_id = id_dokumen(low["sid"])
        try:
            if sudah_ada(doc_id):
                perbarui_rekomendasi(doc_id, data)
            elif simpan_rekomendasi(doc_id, {**data, "status": "baru", "ditemukan": sekarang}) == "baru":
                baru += 1
        except requests.HTTPError as e:
            print(f"Gagal menyimpan {low['sid']}: {e}", file=sys.stderr)
    if not kering:
        try:
            print(f"Rekomendasi lama di luar Jabodetabek disembunyikan: {rapikan_luar_wilayah()}")
        except requests.HTTPError as e:
            print(f"Gagal merapikan rekomendasi lama: {e}", file=sys.stderr)
        simpan_status({"terakhir": sekarang, "diambil": len(semua), "cocok": len(cocok),
                       "baru": baru, "sumber": laporan})
    print(json.dumps(laporan, ensure_ascii=False, indent=1))
    print(f"Rekomendasi baru tersimpan: {baru}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
