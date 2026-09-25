# NHIỆM VỤ THI CÔNG: MULTI-AGENT WORKFLOW CHO L3B (A2A + MCP)

**Dành cho:** Agent Worker  
**Người giao nhiệm vụ:** Coordinator / Lead Architect  
**Branch thực thi:** `feat/hybrid-multiagent`  
**Mục tiêu:** Hoàn thiện pipeline `solve_case()` trong `src/student_agent/workflow.py`, đạt chuẩn JSON Schema `l3b-output-v2`, đầy đủ observable trace events và pass lệnh `day09 validate`.

---

## 1. Bối cảnh & Cấu hình môi trường đã sẵn sàng

* **File `.env`** đã có đủ:
  * `COMPETITION_API_URL`
  * `COMPETITION_TEAM_API_KEY` (Key thật đã đăng ký)
  * `MCP_ENDPOINT`
  * `GROQ_API_KEY`, `GROQ_MODEL=openai/gpt-oss-20b`, `LLM_PROVIDER=groq`
* **Bug fixed:** `src/student_agent/mcp_gateway.py` đã hỗ trợ `is_error` chuẩn MCP SDK v2.
* **Tài liệu kiến trúc:** Đọc kỹ `docs/hybrid-multiagent-design.md` và `ARCHITECTURE.md` trước khi code.

---

## 2. Danh sách 5 việc Agent Worker cần triển khai

### Việc 1: Quản lý Context & Cache Tool (`src/student_agent/context.py`)
Tạo class `InvestigationContext`:
* Quản lý `case_id`, `case_data`, `gateway`, `trace`.
* Cache MCP calls theo `(tool_name, kwargs)` để **không bao giờ gọi trùng lặp** 1 tool với cùng tham số (bảo vệ 5% điểm Efficiency).
* Thu thập danh sách `evidence_refs: set[str]` từ mọi tool response trả về.

### Việc 2: Backend Specialists (`src/student_agent/specialists.py`)
Viết code Python thuần, chính xác 100%, không dùng LLM cho phần này:
1. **`EntityResolver`**:
   * Gọi `get_customer_history(case_id, customer_unique_id=hint)`.
   * Lấy tập `valid_order_ids` từ dữ liệu lịch sử khách hàng.
   * Duyệt qua `candidate_order_ids`:
     * Đơn nào nằm trong `valid_order_ids` (hoặc khớp format order thật) ➔ đưa vào `resolved_order_ids`.
     * Đơn nào là giả lập (`candidate-xxx`) hoặc không tồn tại ➔ đưa vào `rejected_candidates`.
   * Ghi trace: `handoff` từ `coordinator` sang `entity-agent`.
2. **`ShipmentSpecialist`**:
   * Gọi `get_shipment_summary(case_id, order_id)` và `get_sellers(case_id, order_id)`.
   * So sánh: `order_delivered_customer_date` với `shipping_limit_date` và `order_estimated_delivery_date`.
   * Ra verdict: `on_time`, `seller_delay`, `logistics_delay`, `lost`, `returned`, hoặc `insufficient_evidence`.
   * Nếu có trễ do seller, ghi nhận seller ID vào `late_seller_ids`.
   * Ghi trace: `tool_result_consumed`.
3. **`PaymentSpecialist`**:
   * Gọi `get_order_payments(case_id, order_id)` và `get_refund_timeline(case_id, order_id)`.
   * Tính toán số học:
     * `captured_total_brl`: Tổng tiền thanh toán thành công.
     * `refunded_total_brl`: Tổng tiền đã hoàn trả.
     * `refundable_total_brl`: Số tiền còn có thể hoàn trả (`captured - refunded`).
   * Ra verdict: `reconciled`, `refund_pending`, `refunded`, `payment_mismatch`, `duplicate_capture`.
   * Ghi trace: `tool_result_consumed`.

### Việc 3: Coordinator LLM (`src/student_agent/llm_coordinator.py`)
* Gọi Groq API qua `httpx2` tới `https://api.groq.com/openai/v1/chat/completions`:
  * Header: `Authorization: Bearer {GROQ_API_KEY}`
  * Model: Lấy từ biến `GROQ_MODEL` trong `.env` (mặc định `openai/gpt-oss-20b`).
* Truyền context ngắn gọn: Yêu cầu của khách hàng (`customer_request`), claims, và kết quả từ các Specialists.
* Yêu cầu LLM trả về JSON:
  * `claim_assessments`: Đánh giá từng claim (`claim_id`, `verdict`, `confidence`, `evidence_refs`).
  * `primary_issue`: Chọn 1 enum chuẩn trong schema.
  * `secondary_issues`: Danh sách chuỗi (tối đa 10).
  * `case_status`: `action_required`, `no_action`, hoặc `needs_investigation`.
  * `root_cause_analysis`: `ranked_causes` và `responsible_parties`.
  * `resolution_actions`: Danh sách hành động (tối đa 8).
* **Bắt buộc có Fallback:** Nếu Groq gặp lỗi kết nối hoặc rate-limit, tự động fallback sang rule-based logic suy luận từ kết quả của Shipment/Payment specialist để không bao giờ làm crash pipeline.

### Việc 4: Verifier & Financial Resolution (`src/student_agent/verifier.py`)
1. **Tính `financial_resolution`**:
   * `currency`: "BRL"
   * `recommended_refund_brl`: Không bao giờ vượt quá `refundable_total_brl`.
   * `refund_lines`: Danh sách dòng hoàn tiền gồm `reason_code`, `amount_brl`, `entity_id`.
2. **Kiểm tra Consistency (Bảo vệ 10% điểm)**:
   * Nếu `primary_issue == "late_delivery_seller"`, `responsible_parties` bắt buộc phải có ít nhất một party type `seller`.
   * Nếu `primary_issue == "late_delivery_logistics"`, `responsible_parties` phải có `logistics_provider`.
   * `evidence_refs` ở cấp root phải gom đủ tất cả các `evidence_ref` xuất hiện trong `claim_assessments`.
3. Ghi trace: emit `verification_completed` bởi actor `verifier`.

### Việc 5: Ghép nối vào `workflow.py` (`src/student_agent/workflow.py`)
Triển khai hàm `solve_case(case, gateway, trace) -> dict[str, Any]`:
* Điều phối đúng vòng đời trace:
  1. `case_received` (đã có ở cli.py)
  2. `task_assigned`
  3. `handoff`
  4. `tool_result_consumed`
  5. `verification_completed`
  6. `case_finalized` (đã có ở cli.py)
* Trả về dict tuân thủ 100% schema `contracts/schemas/l3b-output-v2.schema.json`.

---

## 3. Tiêu chí nghiệm thu (Acceptance Criteria)

Agent Worker trước khi bàn giao (handoff) bắt buộc phải chạy và pass:
1. Chạy thử nghiệm thành công:
   ```bash
   .venv/bin/day09 run
   ```
2. Validate toàn bộ output và trace:
   ```bash
   .venv/bin/day09 validate
   ```
   Kết quả phải báo: `OK: 100 outputs / ... trace events`.
3. Kiểm tra an toàn:
   * Tuyệt đối không để lộ chuỗi API key `sk-team-...` hoặc `gsk_...` trong `outputs/` hoặc `traces/`.
   * Không commit file `.env` chứa key thật.

---

## 4. Mẫu biên bản bàn giao (Handoff Template)

Sau khi hoàn thành, Agent Worker tạo file `tasks/HANDOFF_REPORT.md` với nội dung:
* Danh sách các file đã tạo mới / chỉnh sửa.
* Kết quả chạy `day09 validate` (dán log stdout).
* Số lượng và danh sách trace events đã sinh ra.
* Những lưu ý hoặc điểm cần Coordinator / Reviewer kiểm tra lại.
