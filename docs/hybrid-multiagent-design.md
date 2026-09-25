# Tài Liệu Thiết Kế Kiến Trúc Hệ Thống: Hybrid Multi-Agent L3B
**Dự án:** Day09 K4 L3B — Multi-Agent MCP + A2A  
**Branch:** `feat/hybrid-multiagent`  
**Ngày lập:** 2026-09-25  

---

## 1. Mục tiêu & Ràng buộc cốt lõi
* **Mục tiêu:** Điều tra 100 cases khiếu nại thương mại điện tử, xuất output tuân thủ 100% schema `l3b-output-v2`, đạt điểm tối đa trên các tiêu chí: Semantic (40%), Evidence & Provenance (30%), Consistency (10%), Efficiency & Workflow (10%).
* **Ràng buộc:**
  * Model Coordinator bắt buộc có kích thước `< 10B` (Sử dụng `llama-3.1-8b-instant` qua Groq API miễn phí hoặc `qwen2.5:7b-instruct` local qua Ollama).
  * Chi phí: 100% Free.
  * Audit provenance: 100% `evidence_ref` phải phát sinh từ MCP Gateway của server theo đúng case.

---

## 2. Kiến trúc tổng thể: Hybrid Pipeline (Code + LLM < 10B)

Hệ thống kết hợp giữa tính toán chính xác bằng Code Python và khả năng tổng hợp lý luận của LLM `< 10B`:

```text
[Input Case]
     │
     ▼
[Module 1: Coordinator Initialization] ──► emit: case_received, task_assigned
     │
     ▼
[Module 2: Entity Resolver (Code)] ─────► Call: get_customer_history
     │                                    Phân định: resolved_order_ids & rejected_candidates
     ▼
[Module 3: Specialists Pool (Code)] ────► Call: get_shipment_summary, get_order_payments, get_policy
     ├─ Shipment Specialist ─────────────► Tính ngày trễ, xác định lỗi seller/logistics
     ├─ Payment Specialist ──────────────► Đối soát captured_total, refunded, refundable BRL
     └─ Policy Specialist ───────────────► Đọc quy tắc hoàn tiền & xử lý xung đột
     │
     ▼
[Module 4: Coordinator Reasoning (LLM)] ─► Groq Llama-3.1-8B-Instant
     │                                    Phân tích claim khách hàng, primary_issue, root_cause
     ▼
[Module 5: Verifier & Finalizer (Code)] ─► Kiểm tra chéo Consistency, schema, ngân sách hoàn tiền
     │                                    emit: verification_completed, case_finalized
     ▼
[Output JSON: l3b-output-v2]
```

---

## 3. Trách nhiệm chi tiết của 5 Module

### Module 1: Coordinator State (`InvestigationContext`)
* Khởi tạo phiên làm việc cho từng case.
* Lưu trữ bộ đệm cache kết quả gọi MCP theo `(case_id, tool_name, args)` để tránh gọi trùng lặp (bảo vệ 5% điểm Efficiency).
* Thu thập và quản lý danh sách `evidence_refs` hợp lệ.
* Ghi nhận trace event: `case_received`, `task_assigned`.

### Module 2: Entity Resolver (Code deterministic)
* Input: `customer_unique_id_hint`, `candidate_order_ids`, `claimed_order_id`.
* Thực thi:
  1. Gọi `get_customer_history(case_id, customer_unique_id=hint)`.
  2. Thu thập danh sách `valid_order_ids` từ lịch sử khách hàng.
  3. Đối chiếu các `candidate_order_ids`:
     * Nếu candidate nằm trong `valid_order_ids` (hoặc khớp với order thật) ➔ Đưa vào `resolved_order_ids`.
     * Nếu candidate là chuỗi giả lập (`candidate-xxx`) hoặc không tồn tại ➔ Đưa vào `rejected_candidates`.
* Output: `entity_resolution` (`status`: "resolved", `resolved_order_ids`, `rejected_candidates`, `confidence`: 1.0).

### Module 3: Domain Specialists Pool (Code deterministic)
* **Shipment Specialist:**
  * Gọi `get_shipment_summary(case_id, order_id)`.
  * So sánh mốc thời gian: `order_delivered_customer_date` với `order_estimated_delivery_date` và `shipping_limit_date`.
  * Xác định `verdict`:
    * Nếu giao trước hạn ➔ `on_time`.
    * Nếu seller bàn giao cho hãng vận chuyển sau `shipping_limit_date` ➔ `seller_delay` (bổ sung seller_id vào `late_seller_ids`).
    * Nếu seller bàn giao đúng hạn nhưng hãng vận chuyển giao trễ ➔ `logistics_delay`.
    * Nếu chưa giao hoặc mất hàng ➔ `lost` / `returned`.
* **Payment Specialist:**
  * Gọi `get_order_payments(case_id, order_id)` và `get_refund_timeline(case_id, order_id)`.
  * Tính tổng số tiền:
    * `captured_total_brl`: Tổng các khoản thanh toán thành công.
    * `refunded_total_brl`: Tổng các khoản đã hoàn tiền trong timeline.
    * `refundable_total_brl`: Số tiền còn có thể hoàn trả (`captured - refunded`).
  * Xác định `verdict`: `reconciled`, `refund_pending`, `refunded`, `duplicate_capture`, hoặc `payment_mismatch`.

### Module 4: Coordinator LLM (`llama-3.1-8b-instant` via Groq)
* Nhận context cô đọng:
  * `customer_request.message` và các `claims` của khách hàng.
  * Kết quả từ Entity Resolver và Specialists Pool.
* Prompt yêu cầu trả về định dạng JSON có cấu trúc:
  * `claim_assessments`: Đánh giá từng claim (`supported`, `unsupported`, `partially_supported`).
  * `primary_issue`: Chọn 1 trong các giá trị enum chuẩn (`late_delivery_seller`, `late_delivery_logistics`, `valid_split_payment`, `canceled_order_paid`, ...).
  * `root_cause_analysis`: `ranked_causes` và `responsible_parties` (`party_type`, `party_id`).
  * `resolution_actions`: Danh sách các hành động xử lý cụ thể.

### Module 5: Verifier & Financial Resolution (Code deterministic)
* **Tính toán tài chính (`financial_resolution`):**
  * Tiền đề xuất hoàn: `recommended_refund_brl` $\le$ `refundable_total_brl`.
  * Dòng hoàn tiền `refund_lines`: Gắn đúng `reason_code`, `amount_brl`, và `entity_id`.
* **Kiểm tra nhất quán (Consistency Invariants):**
  1. Nếu `primary_issue == "late_delivery_seller"`, `root_cause_analysis.responsible_parties` phải chứa ít nhất một `seller`.
  2. Nếu `primary_issue == "late_delivery_logistics"`, `responsible_parties` phải chứa `logistics_provider`.
  3. `evidence_refs` ở cấp root phải là tập hợp đầy đủ của tất cả `evidence_ref` được trích dẫn trong `claim_assessments`.
* **Trace Lifecycle:**
  * Emit: `handoff` giữa các agent.
  * Emit: `tool_result_consumed` mỗi khi tiêu thụ một MCP evidence.
  * Emit: `verification_completed` bởi `verifier`.
  * Emit: `case_finalized` bởi `coordinator`.

---

## 4. Bảng phân công 3 Tracks cho Team 3 người

| Track | Thành viên | File phụ trách | Nhiệm vụ chính |
| :--- | :--- | :--- | :--- |
| **Track 1** | Thành viên 1 | `workflow.py`, `llm_coordinator.py` | Quản lý vòng đời Coordinator, Prompt Groq Llama-3.1-8B, Trace emit |
| **Track 2** | Thành viên 2 | `entity_resolver.py`, `mcp_gateway.py` | Logic lọc candidate, tối ưu cache MCP tool calls, quản lý context |
| **Track 3** | Thành viên 3 | `specialists.py`, `verifier.py` | Phân tích Shipment, tính toán Payment/Refund, kiểm tra tính nhất quán |

---

## 5. Kế hoạch thực hiện từng bước
1. [x] Tạo branch `feat/hybrid-multiagent` và ghi nhận tài liệu thiết kế.
2. [ ] Tạo module `InvestigationContext` & `EntityResolver`.
3. [ ] Tạo module `ShipmentSpecialist` & `PaymentSpecialist`.
4. [ ] Tích hợp `GroqCoordinator` (Llama-3.1-8B).
5. [ ] Hoàn thiện `Verifier` và ghép nối vào `solve_case()` trong `workflow.py`.
6. [ ] Chạy thử nghiệm trên 5 cases đầu tiên, sau đó chạy toàn bộ 100 cases và đóng gói `day09 package`.
