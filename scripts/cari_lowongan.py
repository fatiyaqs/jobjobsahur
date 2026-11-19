"""
Pencari lowongan otomatis untuk Job Vacancy Tracker.

Dijalankan oleh GitHub Actions (lihat .github/workflows/cari-lowongan.yml) setiap hari.
Script ini:
  1. Mengambil lowongan terbaru dari beberapa sumber (Kalibrr, Jobstreet, Telegram GIS/RS Jobs).
  2. Memberi skor kecocokan terhadap 6 versi CV Fatiya (GIS, Research, Operations,
     Administration, Human Resources, General).
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

import requests
from bs4 import BeautifulSoup

# ============================================================
# 1. KONFIGURASI
# ============================================================
FIREBASE_API_KEY = os.environ.get("FIREBASE_API_KEY", "AIzaSyCUPAlkUIyZilT98o2RsZP0DOdWtTCW8Dk")
FIREBASE_PROJECT = os.environ.get("FIREBASE_PROJECT", "jobjobsahur-job")
PEMILIK = "Fatiya"           # nama orang di tracker yang menjadi sasaran rekomendasi
SKOR_MINIMUM = 6              # lowongan di bawah skor ini tidak disimpan
MAKS_HARI = 21                # abaikan lowongan yang diposting lebih lama dari ini

# Kata kunci pencarian yang dikirim ke tiap situs lowongan
KATA_KUNCI_CARI = [
    "GIS", "geospatial", "remote sensing", "pemetaan", "geografi", "kartografi",
    "surveyor", "lingkungan", "environmental", "research assistant", "asisten peneliti",
    "admin", "administrasi", "data entry", "operations staff", "inventory",
    "human resources", "HR staff", "recruitment",
]

# 6 profil CV. Kata kunci judul bernilai 3, kata kunci deskripsi bernilai 1.
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

# Lowongan yang terlalu senior atau tidak relevan
KATA_NEGATIF_JUDUL = ["senior", "sr.", "manager", "manajer", "head", "lead", "director",
                      "direktur", "principal", "vp ", "chief", "superintendent", "kepala",
                      "sales", "driver", "kurir", "teknisi", "mekanik", "developer",
                      "engineer software", "programmer", "akuntan", "accountant", "dokter",
                      "perawat", "nurse", "guru"]
KATA_NEGATIF_ISI = ["minimal 5 tahun", "minimum 5 years", "5+ years", "min. 5", "7+ years",
                    "10+ years", "minimal 4 tahun", "4+ years"]
KATA_POSITIF_ISI = ["fresh graduate", "lulusan baru", "fresh grad", "0-1", "0 - 1",
                    "1 tahun", "1 year", "entry level", "geografi", "geography"]

# Hanya lowongan di Jabodetabek yang disimpan.
WILAYAH = ["jakarta", "dki", "jabodetabek", "depok", "bogor", "cibinong", "sentul", "cileungsi",
           "cibubur", "bekasi", "cikarang", "tangerang", "tangsel", "bsd", "serpong", "karawaci",
           "alam sutera", "ciputat", "pamulang", "bintaro", "cikupa", "balaraja"]
TERIMA_REMOTE = False   # ubah ke True kalau lowongan remote juga ingin ditampilkan

TELEGRAM_CHANNELS = ["gis_id_jobs"]

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
                     headers=UA, timeout=30)
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
    r = requests.get(url, params=params, headers={**UA, "Accept": "application/json"}, timeout=30)
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


def sumber_telegram(channel: str) -> list[dict]:
    r = requests.get(f"https://t.me/s/{channel}", headers=UA, timeout=30)
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


def nilai(lowongan: dict) -> tuple[int, str, list[str]]:
    judul = lowongan.get("posisi", "").lower()
    isi = (lowongan.get("isi", "") + " " + lowongan.get("perusahaan", "")).lower()
    terbaik, cv_terbaik, alasan_terbaik = 0, "General", []
    for cv, kk in PROFIL.items():
        skor, alasan = 0, []
        for k in kk["judul"]:
            if ada(k, judul):
                skor += 3; alasan.append(k)
            elif ada(k, isi):
                skor += 1
        for k in kk["isi"]:
            if ada(k, judul) or ada(k, isi):
                skor += 1; alasan.append(k)
        if cv == "General":
            skor = min(skor, 4)  # profil umum hanya sebagai cadangan
        if skor > terbaik:
            terbaik, cv_terbaik, alasan_terbaik = skor, cv, alasan
    bonus = 0
    if any(ada(k, judul) for k in KATA_NEGATIF_JUDUL):
        bonus -= 8
    if any(k in isi for k in KATA_NEGATIF_ISI):
        bonus -= 4
    if any(k in isi or k in judul for k in KATA_POSITIF_ISI):
        bonus += 2; alasan_terbaik.append("cocok untuk fresh grad/geografi")
    return terbaik + bonus, cv_terbaik, list(dict.fromkeys(alasan_terbaik))[:6]


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


def id_dokumen(sid: str) -> str:
    return hashlib.sha1(sid.encode()).hexdigest()[:20]


def sudah_ada(doc_id: str) -> bool:
    r = requests.get(f"{BASE}/rekomendasi/{doc_id}", params={"key": FIREBASE_API_KEY}, timeout=20)
    return r.status_code == 200


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
        r = requests.get(f"{BASE}/rekomendasi", params=params, timeout=30)
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
def kumpulkan() -> tuple[list[dict], dict]:
    semua, laporan = [], {}
    tugas = ([("Kalibrr", sumber_kalibrr, k) for k in KATA_KUNCI_CARI]
             + [("Jobstreet", sumber_jobstreet, k) for k in KATA_KUNCI_CARI]
             + [("Telegram", sumber_telegram, c) for c in TELEGRAM_CHANNELS])
    for nama, fungsi, arg in tugas:
        lap = laporan.setdefault(nama, {"ok": 0, "gagal": 0, "lowongan": 0, "error": ""})
        try:
            hasil = fungsi(arg)
            lap["ok"] += 1
            lap["lowongan"] += len(hasil)
            semua.extend(hasil)
        except Exception as e:  # satu sumber gagal tidak menghentikan yang lain
            lap["gagal"] += 1
            lap["error"] = f"{type(e).__name__}: {str(e)[:160]}"
            print(f"[{nama}] gagal untuk '{arg}': {e}", file=sys.stderr)
        time.sleep(1.2)
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
        skor, cv, alasan = nilai(low)
        if skor < SKOR_MINIMUM:
            continue
        low.update({"skor": skor, "cv": cv, "alasan": alasan})
        cocok.append(low)
    cocok.sort(key=lambda x: -x["skor"])
    return cocok


def main() -> int:
    kering = "--dry-run" in sys.argv
    semua, laporan = kumpulkan()
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
            "ringkas": low.get("isi", "")[:400], "untuk": PEMILIK,
            "status": "baru", "ditemukan": sekarang,
        }
        print(f"  [{low['skor']:>2}] {low['cv']:<15} {low['posisi'][:60]} — {low.get('perusahaan','')}")
        if kering:
            continue
        doc_id = id_dokumen(low["sid"])
        try:
            if not sudah_ada(doc_id) and simpan_rekomendasi(doc_id, data) == "baru":
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
