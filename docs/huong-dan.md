# Hướng dẫn: unity-apk-re từng bước

Lệnh chạy là `scripts/uare` (Windows: `scripts\uare.cmd`). Chỉ cần Python 3.10 trở lên. adb đã có sẵn trong `vendor/`.

---

## Bước 0: Chuẩn bị điện thoại

1. Vào **Cài đặt → Thông tin điện thoại**, chạm 7 lần vào "Số bản dựng" để bật **Tùy chọn nhà phát triển**.
2. Trong Tùy chọn nhà phát triển, bật **Gỡ lỗi USB**. Trên Xiaomi, bật thêm "Gỡ lỗi USB (Cài đặt bảo mật)" nếu có.
3. Cắm cáp USB loại truyền được dữ liệu.
4. Khi điện thoại hỏi "Cho phép gỡ lỗi USB?", bấm **Cho phép**.

## Bước 1: `doctor`, kiểm tra môi trường

```bash
scripts/uare doctor
```

Lệnh này in ra:

- **adb nào đang được dùng**. Thứ tự ưu tiên:
  1. `--adb <đường dẫn>`
  2. biến môi trường `UARE_ADB`
  3. adb đóng gói sẵn trong `vendor/platform-tools/<hệ điều hành>/`
  4. `adb` có trong PATH
- **phiên bản adb**
- **danh sách thiết bị** và trạng thái của từng cái. Phải là `device` thì mới dùng được.

| Trạng thái        | Cách xử lý                                         |
| ----------------- | -------------------------------------------------- |
| không có thiết bị | Đổi cáp, kiểm tra Gỡ lỗi USB đã bật chưa           |
| `unauthorized`    | Mở khóa điện thoại, bấm "Cho phép" ở hộp thoại RSA |
| `offline`         | Rút ra cắm lại cáp                                 |

## Bước 2: `packages`, tìm tên package của game

```bash
scripts/uare packages --filter hero
```

Lệnh này liệt kê các app bạn tự cài (app hệ thống bị bỏ qua) có chứa chữ "hero", ví dụ `com.percent.aos.rollinghero`.

## Bước 3: `pull`, lấy APK về máy

```bash
scripts/uare pull com.percent.aos.rollinghero --out re/rollinghero/pull
```

Lệnh này làm ba việc:

1. Hỏi điện thoại (`pm path`) xem game gồm những file APK nào. Game trên Google Play thường bị chia thành nhiều phần:
   - `base.apk`: phần chính
   - `split_config.arm64_v8a.apk`: thư viện native cho CPU 64-bit, gồm `libil2cpp.so`, `libunity.so`
   - `split_*.apk` khác: gói asset (asset pack), ngôn ngữ, độ phân giải màn hình
2. Tìm file OBB (dữ liệu mở rộng) trong `/sdcard/Android/obb/<package>/`.
3. `adb pull` từng file, tính **sha256**, và ghi `manifest.json` gồm package, serial thiết bị, phiên bản game, kích thước và hash của từng file.

Kết quả: `pull/apks/*.apk`, `pull/obb/*.obb`, `pull/manifest.json`.

## Bước 4: `extract`, ghép các phần lại

```bash
scripts/uare extract re/rollinghero/pull --out re/rollinghero/extracted
```

APK thực chất là file zip. Lệnh này mở tất cả các APK/OBB cùng lúc và sắp xếp nội dung thành:

| Thư mục               | Nội dung                                                                                                               |
| --------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `Data/`               | Dữ liệu Unity (`assets/bin/Data`): scene `level0..N`, `sharedassets*.assets`, `globalgamemanagers`, metadata IL2CPP... |
| `lib/<abi>/`          | Thư viện native: `libil2cpp.so` (toàn bộ code C# đã biên dịch sang máy), `libunity.so` (engine)                        |
| `assets/`             | Asset nằm ngoài Data: Addressables (`assets/aa/`), StreamingAssets                                                     |
| `archives/<tên apk>/` | Phần còn lại của từng APK: `AndroidManifest.xml`, `classes.dex` (code Java/Kotlin), `res/`                             |

Những việc quan trọng lệnh này làm thêm:

- **Nối file `.splitN`**: Unity chia file lớn thành `level0.split0`, `level0.split1`, ... Công cụ nối chúng theo đúng thứ tự, và báo lỗi nếu thiếu mảnh.
- **Chặn file độc hại**: tên chứa `..`, đường dẫn tuyệt đối, ổ đĩa Windows (`C:`), tên chỉ khác hoa/thường, zip bomb (tổng dung lượng giải nén quá 64 GiB, hoặc lớn hơn dung lượng ổ còn trống).
- **Phát hiện xung đột**: hai APK chứa cùng một file nhưng nội dung khác nhau thì báo lỗi. Nếu nội dung giống hệt thì bỏ qua bản trùng.

## Bước 5: `analyze`, phân tích

```bash
scripts/uare analyze re/rollinghero/extracted --out re/rollinghero/analysis
```

Lệnh này đọc từng phần và ghi ra `report.json` cùng các file chi tiết:

1. **Kiểm kê `Data/`** (ghi vào `files.jsonl`). Với mỗi file, công cụ đọc phần header (vài KB đầu) để biết:
   - loại file: SerializedFile (scene, asset), `.resS`/`.resource` (dữ liệu texture/audio), AssetBundle (`UnityFS`)...
   - phiên bản Unity, nền tảng build (Android = 13), có type tree hay không, số loại object
   - kiểu nén của AssetBundle (lz4, lzma...)
2. **Kiểm kê `assets/`** (Addressables) theo cách tương tự, ghi vào `assets_files.jsonl`.
3. **Xác định scripting backend**:
   - `il2cpp`: code C# đã được biên dịch sang mã máy, nằm trong `libil2cpp.so`. Tên class/method nằm riêng trong `global-metadata.dat`.
   - `mono`: code C# vẫn là DLL .NET trong `Data/Managed/`, có thể decompile bằng công cụ .NET thông thường.
4. **Đọc metadata IL2CPP** (`Data/Managed/Metadata/global-metadata.dat`):
   - kiểm tra magic number `0xFAB11BAF`. Sai magic nghĩa là metadata bị mã hóa, và công cụ báo lỗi chứ không cố giải mã.
   - `il2cpp/identifiers.tsv`: **tất cả tên** class, method, field, namespace có trong game
   - `il2cpp/string_literals.jsonl`: **tất cả chuỗi** trong code C#: chữ trên UI, key, URL...
5. **Phân tích thư viện native** (`lib/*/*.so`):
   - đọc header ELF: kiến trúc CPU, các segment code
   - đếm các hàm `il2cpp_*` mà thư viện **export** (không tính hàm import)
   - **đo entropy** của vùng code theo từng cửa sổ 1 MB. Code bình thường có entropy khoảng 5,7–7,0 bit/byte. Code bị mã hóa hoặc nén thì gần 8. Vùng độn (filler) thì gần 0–1.
   - kết luận: `normal` (bình thường), `likely_packed_or_encrypted` (đã bị pack/mã hóa, không đọc tĩnh được nội dung hàm), hoặc `inconclusive` (chưa chắc).

   Vì sao phải đo theo cửa sổ: `libil2cpp.so` của Rolling Hero có khoảng 32 MB đầu bị mã hóa, phần còn lại là byte độn "AGAG". Entropy trung bình chỉ 3,82, trông có vẻ bình thường. Đếm theo cửa sổ (31 cửa sổ cao, 78 cửa sổ độn) mới lộ ra là đã bị pack.

Bước 3–5 có thể chạy gộp bằng một lệnh:

```bash
scripts/uare run com.percent.aos.rollinghero --workdir re/rollinghero
```

## Bước 6: `summary` và `find`, đọc kết quả

```bash
scripts/uare summary re/rollinghero/analysis
scripts/uare find re/rollinghero/analysis '(?i)shop|product' --context 8
scripts/uare find re/rollinghero/analysis '(?i)buy|price' --literals --context 2
```

- `summary` in lại bản tóm tắt.
- `find` tìm theo biểu thức chính quy:
  - `>` đánh dấu dòng khớp
  - `--context N` hiện thêm N dòng xung quanh. Tên trong metadata được lưu gần như theo thứ tự khai báo, nên các dòng lân cận thường là field và method của cùng class đó.
  - `--literals` tìm trong chuỗi thay vì tên.
  - mã thoát: 0 = có kết quả, 1 = không có, 2 = lỗi.

Ý nghĩa đầy đủ của từng trường trong `report.json`: [skills/explore-il2cpp/references/report-fields.md](../skills/explore-il2cpp/references/report-fields.md)

## Bước 7: `dump-il2cpp` và `export-assets`, lấy tên và dữ liệu

```bash
scripts/uare dump-il2cpp re/rollinghero/extracted --out re/rollinghero/il2cpp
scripts/uare export-assets re/rollinghero/extracted --out re/rollinghero/assets
```

**`dump-il2cpp`** đọc `global-metadata.dat` và phân loại tên thành từng bảng riêng:

| File | Nội dung |
|---|---|
| `types.tsv` | `chỉ_số <TAB> namespace <TAB> tên` cho mọi class |
| `methods.tsv`, `fields.tsv`, `parameters.tsv`, `properties.tsv`, `events.tsv` | `chỉ_số <TAB> tên` cho từng loại thành viên |
| `sections.json` | phiên bản metadata + offset/size của 31 section (để nạp vào công cụ khác) |

Đây là dạng có phân loại của đống tên mà `analyze` đổ ra phẳng. Ví dụ `events.tsv` của Rolling Hero có `OnGameEnd`, `FlightStarted`, `RewardMultiplierChanged`; `types.tsv` có `ShopPage`, `DeokjaShopView`, `SeasonEventLobbyView`. Số tên bị obfuscate được báo rõ và để trống, **không đoán**.

**`export-assets`** đọc mọi SerializedFile và xuất ra:

| File | Nội dung |
|---|---|
| `objects.jsonl` | mỗi object một dòng: file, pathID, class, tên, kích thước — bản kiểm kê asset đầy đủ |
| `textassets/` | byte của từng TextAsset, đặt tên theo m_Name — **dữ liệu cấu hình game** (CSV/JSON/Lua) |
| `monoscripts.tsv` | `file, pathID, tên, class, namespace, assembly` của mỗi MonoScript — nối asset với tên class IL2CPP |
| `assets_report.json` | số lượng object theo từng class |

Trên Rolling Hero, `textassets/` ra đúng các bảng cân bằng game: `StarChallenge`, `AfkConfig`, `ClawCraneMinigame`… dạng CSV đọc thẳng được.

### Giới hạn (nói thật)

- **Không gom thành viên theo class, không có chữ ký.** `dump-il2cpp` chỉ ra danh sách tên phẳng theo từng loại. Việc method/field nào thuộc class nào, kiểu tham số, kiểu trả về, địa chỉ hàm — cần Il2CppDumper/Il2CppInspector chạy trên binary **đã gỡ pack**. Với `libil2cpp.so` bị pack (như Rolling Hero), chính các công cụ đó cũng cần binary được gỡ pack trước, và plugin này không làm việc đó.
- **Không giải mã media.** `export-assets` lấy được byte TextAsset và kiểm kê mọi object, nhưng không giải mã texture/audio/mesh nén (cần codec ETC/ASTC/Vorbis). Dùng AssetRipper/UnityPy cho phần đó; bản kiểm kê đã cho biết tên và kích thước.

## Nguyên tắc an toàn của công cụ

- **Không ghi đè**: mỗi lệnh ghi vào một thư mục mới hoặc rỗng, thông qua thư mục tạm. Nếu lỗi giữa chừng thì không để lại gì, cứ chạy lại là được.
- **Báo lỗi rõ ràng**: lỗi in ra một dòng log JSON có loại lỗi và lý do, kèm mã thoát 2. Không bao giờ âm thầm bỏ qua.
- **An toàn với `adb shell`**: tên package và đường dẫn từ điện thoại đều được kiểm tra bằng regex trước khi dùng, để tránh chèn lệnh shell.
- **adb đóng gói sẵn**: lấy nguyên bản từ Google. Mỗi hệ điều hành có `FETCHED.json` ghi URL, revision và sha256. Muốn cập nhật thì xóa `vendor/platform-tools/` rồi chạy `scripts/uare fetch-adb --platform all`.

## Dùng như plugin Claude Code

Sau khi đưa repo lên GitHub:

```text
/plugin marketplace add <user>/<repo>
/plugin install unity-apk-re@unity-apk-re
```

Ba skill sẽ có sẵn:

| Skill                            | Dùng khi                                             |
| -------------------------------- | ---------------------------------------------------- |
| `/unity-apk-re:analyze-apk`      | Chạy cả quy trình và giải thích kết quả              |
| `/unity-apk-re:pull-from-device` | Lấy APK từ điện thoại, xử lý lỗi kết nối thiết bị    |
| `/unity-apk-re:explore-il2cpp`   | Tìm tên class/method/chuỗi, giải thích `report.json` |
