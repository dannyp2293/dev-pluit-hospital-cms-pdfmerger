# MCU PDF Merger — Tahap 1

Dashboard lokal berbasis Python + Streamlit untuk jaringan internal (LAN) rumah sakit.

## Isi tahap 1
- Dashboard ringkasan batch MCU.
- Pembuatan batch berdasarkan perusahaan/periode.
- Checklist pemeriksaan dinamis per batch.
- Penambahan jenis pemeriksaan baru.
- Pembuatan struktur folder sumber/output per batch.
- Penyimpanan metadata batch dan jenis pemeriksaan di SQLite.

**Belum termasuk:** import/ekstraksi PDF, OCR, pencocokan nama pasien, validasi dokumen, penggabungan PDF, dan laporan final. Fitur tersebut akan dibangun pada tahap berikutnya setelah alur dan keamanan tahap 1 diuji.

## 1. Persyaratan
- Windows 10/11.
- Python 3.10 atau lebih baru (disarankan Python 3.11/3.12).
- Komputer server dan komputer petugas berada pada jaringan LAN yang sama.
- Hak akses untuk membuat folder proyek dan aturan firewall Windows.

## 2. Instalasi di komputer server
Ekstrak folder proyek, lalu buka PowerShell di folder `MCU_PDF_MERGER`.

Buat virtual environment:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Jika PowerShell memblokir aktivasi, jalankan untuk sesi terminal saat ini:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

Pasang dependensi:

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## 3. Atur password aplikasi
Buat folder `.streamlit` jika belum ada. Salin:

`.streamlit/secrets.toml.example`

menjadi:

`.streamlit/secrets.toml`

Edit `secrets.toml`, ganti nilai password contoh dengan password kuat. Jangan bagikan file ini dan jangan masukkan ke Git.

## 4. Jalankan untuk uji lokal
```powershell
streamlit run app.py
```

Buka alamat yang ditampilkan, biasanya `http://localhost:8501`.

## 5. Jalankan agar dapat diakses melalui LAN
Di komputer server:

```powershell
streamlit run app.py --server.address 0.0.0.0 --server.port 8501
```

Cari IPv4 komputer server:

```powershell
ipconfig
```

Dari komputer petugas yang berada di LAN yang sama, buka browser:

`http://IP-KOMPUTER-SERVER:8501`

Contoh format: `http://192.168.1.20:8501` (gunakan IP server yang sebenarnya).

## 6. Firewall Windows
Minta bantuan administrator jaringan/IT RS untuk membuat inbound rule TCP port 8501 yang:
- hanya berlaku untuk profil jaringan Private/Domain yang sesuai;
- membatasi Remote IP address ke subnet LAN rumah sakit;
- tidak membuka akses dari jaringan publik/internet.

Jangan melakukan port forwarding pada router ke internet.

## 7. Cara menggunakan tahap 1
1. Masuk menggunakan password aplikasi.
2. Buka **Buat Batch MCU**.
3. Isi perusahaan dan periode, misalnya `JICT` dan `2026`.
4. Centang pemeriksaan yang termasuk paket perusahaan.
5. Klik **Buat batch**.
6. Aplikasi membuat folder `DATA_SUMBER/NAMA_BATCH/KODE_PEMERIKSAAN/`.

Jenis pemeriksaan baru dapat ditambahkan melalui menu **Paket Pemeriksaan**. Jenis baru berlaku untuk batch yang dibuat setelah penambahan.

## 8. Lokasi data
- `DATABASE/mcu_merger.db`: metadata batch dan jenis pemeriksaan.
- `DATA_SUMBER/`: PDF sumber per batch dan jenis pemeriksaan.
- `HASIL_PDF/`: lokasi output tahap berikutnya.
- `LAPORAN/`: lokasi laporan tahap berikutnya.

Jangan menghapus atau mengubah database saat aplikasi sedang berjalan.

## 9. Catatan keamanan penting
Tahap 1 menggunakan satu password bersama dan belum memiliki akun individual/role. Ini adalah prototipe internal, bukan kontrol akses final untuk data medis. Sebelum memasukkan PDF pasien asli atau digunakan operasional:
- tambahkan autentikasi individual dan role petugas;
- batasi akses jaringan dan folder dengan Windows permissions;
- siapkan backup terenkripsi dan uji pemulihan;
- aktifkan audit log yang mencatat pengguna dan aktivitas tanpa menyalin isi hasil medis;
- tetapkan kebijakan retensi dan penghapusan sesuai kebijakan RS;
- lakukan uji keamanan dan persetujuan penanggung jawab TI/keamanan informasi RS.

Untuk tahap pengembangan awal, gunakan data dummy.
