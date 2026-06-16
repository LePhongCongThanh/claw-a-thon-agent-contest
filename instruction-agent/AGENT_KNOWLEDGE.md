# Zalopay Merchant Analytics — Agent Knowledge Base

## 1. Mục tiêu hệ thống

Phân tích hiệu suất thanh toán của từng merchant qua cổng Zalopay.
Sau khi ký kết, mọi giao dịch của merchant đều đi qua Zalopay.
Khi TPV (Total Payment Volume) của một segment giảm, hệ thống cần tìm ra nguyên nhân và đề xuất hành động.

---

## 2. Cấu trúc dữ liệu đầu vào

| Cột | Mô tả |
|-----|-------|
| `Date` | Ngày giao dịch (YYYY-MM-DD) |
| `Merchant` | Tên / ID merchant |
| `SOF_Type` | Nguồn thanh toán: App Payment, Web Payment, BNPL, Card, E-Wallet, VietQR |
| `Acquisition_Type` | Kênh: Organic, Paid, QR |
| `TPV` | Tổng giá trị giao dịch |

File Excel có thể chứa thêm:
- **MoM_Analysis**: Prev_Month_TPV làm baseline lịch sử
- **Detail_Analysis**: Ghi chú phân tích sẵn, recommended action
- **Voucher_Breakdown**: Breakdown theo từng voucher (dùng cho Paid channel)

---

## 3. Metrics hệ thống tính toán

- **MTD_TPV**: Tổng TPV từ ngày 1 đến ngày hiện tại trong tháng
- **Prev_Month_TPV**: TPV tháng trước (lấy từ MoM_Analysis sheet hoặc tính từ data)
- **MoM_Growth_%** = (MTD_TPV - Prev_Month_TPV) / Prev_Month_TPV × 100
- **MoM_Status**: High growth (≥20%) | Stable growth (0-20%) | Underperforming (<0%) | New segment | Dropped

---

## 4. Logic phân tích theo 4 Scenarios

### Scenario 1 — ORGANIC CHANNEL ↓
```
Organic TPV giảm?
  → So YoY (cùng kỳ năm ngoái)
      ├── Seasonal (bình thường): Monitor, không cần action ngay
      └── Bất thường:
            ├── Competitor lấy thị phần → Phân tích chiến lược đối thủ, chạy counter-campaign
            ├── Internal campaign kết thúc → Relaunch hoặc optimize chiến dịch mới
            └── Feedback tiêu cực (social) → Fix UX/issues, chạy retention campaign
```

### Scenario 2 — PAID CHANNEL ↓
```
Paid TPV giảm?
  → Breakdown theo voucher (xem Voucher_Breakdown)
      ├── Budget bị cắt giảm: BÌNH THƯỜNG → Đánh giá ROI, cân nhắc tái đầu tư
      ├── Budget giữ nguyên: CHẤT LƯỢNG KÉM → Test creative mới, optimize targeting, A/B test
      └── Budget tăng mà vẫn giảm: MARKET SATURATION → Giảm budget, thử voucher thay thế
```

### Scenario 3 — QR CHANNEL ↓
```
QR TPV giảm?
  → KHÔNG tự chẩn đoán được (QR không có voucher/budget structure rõ ràng)
  → Escalate lên BIZ team / Area Manager với:
      - QR TPV MTD vs Previous
      - YoY comparison (seasonality)
      - Vùng / địa điểm bị ảnh hưởng (nếu có)
  → Ghi lại findings sau khi BIZ điều tra xong
```

### Scenario 4 — GROWTH ↑
```
TPV tăng?
  → Xác định driver:
      ├── Organic growth: PR coverage, viral moment, competitor decline, word-of-mouth
      │     → Amplify (thêm PR/social), replicate (lên kế hoạch kỳ tới), tốn ít budget
      └── Paid growth: Voucher mới hiệu quả, campaign timing tốt, targeting cải thiện
            → Kiểm tra ROI trước → nếu dương: scale budget
            → Monitor ad fatigue / saturation
```

---

## 5. Nguyên nhân thường gặp theo SOF_Type

| SOF_Type | Nguyên nhân drop phổ biến | Cách điều tra |
|----------|---------------------------|---------------|
| **BNPL / Buy Now Pay Later** | Merchant tự ra dịch vụ trả góp riêng (vd: Thế Giới Di Động treo banner trả góp 0%) | Crawl website merchant tìm từ khóa trả góp, Home Credit, Kredivo... |
| **VietQR** | Lỗi kỹ thuật, campaign offline kết thúc, traffic giảm theo mùa | Escalate BIZ, kiểm tra khu vực |
| **Organic** | Phốt trên mạng xã hội (Threads, Facebook), đối thủ cạnh tranh | Search social media, news |
| **Paid** | Budget cắt, chất lượng voucher giảm, saturation | Xem Voucher_Breakdown |

---

## 6. Web Research Guidelines

Khi không tìm thấy nguyên nhân từ internal data, Agent phải nghiên cứu:
- Website chính thức của merchant: tìm banner BNPL/trả góp cạnh tranh, đối tác thanh toán khác
- Facebook, Threads, TikTok (public): tìm phốt, khiếu nại, viral content
- Tin tức: campaign mới, ra mắt sản phẩm, sự kiện ảnh hưởng
- Đối thủ: pricing, promotion, market share

Phân loại độ tin cậy: **Cao** (website merchant) | **Trung bình** (news) | **Thấp** (social snippet)

---

## 7. Output Report Format

Mỗi lần phân tích xuất:
1. **Executive Summary**: Tổng MTD TPV, MoM growth toàn hàng
2. **Top High Growth Segments**: Top 5 segment tăng mạnh nhất
3. **Top Underperforming Segments**: Top 5 segment giảm, kèm chẩn đoán nguyên nhân
4. **New Segments**: Segment mới xuất hiện tháng này
5. **Recommended Actions**: Hành động cụ thể theo từng scenario
6. **Web Research Findings**: Bằng chứng từ internet (nếu có), kèm link và confidence level
