# L3B Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Vẽ hoặc mô tả luồng từ input/candidate resolution đến MCP investigation, specialist agents, conflict resolver, verifier, output và trace.

```text
Input → Entity Resolver → Coordinator → Specialists → Conflict Resolver → Verifier → Output
            │                              │                  │             │
            └──────────────────────────── MCP ────────────────┴──────────── Trace
```

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Entity/customer | Case và candidate | Resolve customer/order | `get_customer_history` | Resolved/rejected IDs |
| Coordinator | Case và specialist results | Điều phối, tổng hợp Groq hoặc fallback | Không gọi MCP | Decision cho verifier |
| Shipment | Resolved order IDs | Phân tích timeline/seller | `get_shipment_summary`, `get_sellers` | Shipment verdict |
| Payment/refund | Resolved order IDs | Đối soát capture/refund | `get_order_payments`, `get_refund_timeline` | Payment verdict/totals |
| Verifier | Toàn bộ kết quả | Enforce schema-facing invariants và refund cap | Không gọi MCP | Final output |

Áp dụng least privilege; tool discovery không đồng nghĩa mọi actor đều được gọi mọi tool.

## 3. Entity resolution và A2A protocol

`EntityResolver` đối chiếu candidate với customer history; ID giả `candidate-*` bị reject,
còn order ID hex hợp lệ được giữ làm fallback khi history thiếu. Mọi context gắn cố định với một
`case_id`; handoff chỉ đi theo luồng coordinator → entity → specialist → coordinator → verifier,
không có vòng lặp hay retry không giới hạn.

## 4. Evidence và conflict lifecycle

Gateway validate MCP envelope trước khi `InvestigationContext` cache response. Context gom
`evidence_ref` theo case và emit `tool_result_consumed` khi specialist dùng response. Event shipment
đã confirmed được ưu tiên hơn timeline suy ra; bất đồng được ghi vào `data_conflicts`. Verifier loại
evidence ref không thuộc context hiện tại khỏi claim và xuất toàn bộ ref đã dùng ở root.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout | 0 | Specialist trả insufficient evidence từ phần dữ liệu còn lại | Không emit consumption cho call lỗi |
| Entity not found/ambiguous | 0 | `not_found`/`ambiguous`, không gọi tool theo order | `ENTITIES_RESOLVED` |
| Source conflict | 0 | Ưu tiên confirmed event, ghi `data_conflicts` | `AUTHORITATIVE_EVENT` |
| Groq lỗi/rate limit | 0 | Rule-based coordinator | `SPECIALIST_RESULTS_READY` |
| Invalid specialist/LLM result | 0 | Verifier normalize về enum/limit công khai | `CONSISTENCY_CHECKED` |

Context cache theo `(tool_name, kwargs)`; happy path dùng đúng 5 MCP calls/case. Không retry MCP và
không biến missing evidence thành dữ liệu nghiệp vụ.

## 6. Verification invariants

Verifier giới hạn confidence, enum, collection sizes, claim evidence ownership và refund không vượt
`captured - refunded`. Late seller luôn có seller party; late logistics luôn có logistics provider.
Root evidence chứa mọi claim evidence. CLI thực hiện JSON Schema validation trước khi ghi output.

## 7. Reproducibility

Coordinator dùng `GROQ_MODEL` (mặc định `openai/gpt-oss-20b`) qua `httpx2`, temperature 0 và JSON
mode. Pipeline xử lý case tuần tự; logic specialist không ngẫu nhiên. Chạy bằng `.venv/bin/day09 run`
và kiểm tra bằng `.venv/bin/day09 validate`; secret chỉ được đọc từ `.env`, không ghi vào artifact.
