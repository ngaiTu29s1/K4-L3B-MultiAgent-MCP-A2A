# L3B Architecture Record

Tài liệu này chỉ mô tả các quyết định có thể quan sát được. Không ghi prompt,
chain-of-thought, API key hoặc dữ liệu case riêng tư.

## 1. Tổng quan hệ thống

```text
Input case
    -> Entity/Customer Agent
    -> Coordinator / Router
    -> Order/Product + Shipment + Payment/Refund specialists
    -> Policy / Conflict Resolver
    -> Verifier
    -> L3B output

MCP Gateway: chỉ được gọi với case_id hiện tại và đúng quyền của actor.
Trace: ghi assignment, evidence consumption, handoff, verification, finalize.
```

Coordinator tạo state cục bộ cho đúng một case, điều phối các specialist khi
entity đã được xác minh, rồi ghép các finding để Policy/Conflict Resolver và
Verifier xử lý. Specialist độc lập có thể chạy song song sau khi nhận handoff
hợp lệ; không có vòng lặp handoff vô hạn.

## 2. Public contract lock

Các file phát hành trong `contracts/schemas/` là public contract: **không sửa**
chúng và không thêm field vào JSON output/trace ngoài field đã định nghĩa.

| Contract | Cách dùng bắt buộc |
| --- | --- |
| `l3b-output-v2.schema.json` và `l3a-output-v2.schema.json` được tham chiếu | Validate output cuối của từng case. |
| `trace-event-v1.schema.json` | Validate từng dòng trace observable. |
| `mcp-evidence-response-v1.schema.json` | Validate response MCP trước khi dùng data hoặc reference. |
| `submission-manifest-v2.schema.json` | Validate `manifest.json` khi đóng gói. |

`evidence_ref` được MCP cấp. Agent lưu nguyên giá trị này; không tự tạo, chỉnh
sửa hay dùng lại reference của case khác.

## 3. Agent ownership và least privilege

| Actor | Input | Trách nhiệm | Quyền tool | Handoff |
| --- | --- | --- | --- | --- |
| Entity/customer | Candidate và customer clue | Resolve order/customer, reject candidate không được chứng minh, tải customer context | Customer và entity/order lookup đã discover | Status, IDs, confidence, evidence refs |
| Coordinator | Case và entity handoff | Route task, áp case scope/query budget, thu finding | Discovery một lần mỗi run; không quét dữ liệu rộng | Assignment và state đã ghép |
| Order/product | Resolved order IDs | Kiểm order, item, seller, product, cancellation | Order, item, product, seller | Order facts và evidence refs |
| Shipment | Resolved order/item IDs | Dựng timeline, phân loại delay/lost/returned | Shipment | Verdict, seller IDs, timeline status, refs |
| Payment/refund | Resolved order/payment IDs | Reconcile capture, duplicate, refund, refundable amount | Payment và refund | Totals, verdict và refs |
| Policy | Finding và conflict | Áp policy/source precedence khi thực sự cần | Policy tool | Decision và resolution code |
| Conflict resolver | Specialist handoffs | Ghi conflict, chọn source hoặc giữ unresolved | Không query rộng; tối đa một targeted lookup | Conflict record |
| Verifier | State hoàn chỉnh | Check schema, scope, evidence, consistency, confidence | Không gọi MCP mặc định | Verification event và output đã duyệt |

Discovery không cấp quyền dùng mọi tool. Tool names thật phải lấy từ
`day09 mcp-tools`, không đoán tên tool trong code.

## 4. A2A handoff protocol

Handoff là internal typed record (không phải public output) và luôn được liên
kết bằng `case_id`:

```json
{
  "case_id": "L3B_CASE_001",
  "source_agent": "entity_customer",
  "target_agent": "shipment",
  "task_type": "analyze_shipment",
  "status": "resolved",
  "decision_code": "ENTITY_CONFIRMED",
  "entity_ids": {"order_ids": ["..."]},
  "confidence": 0.9,
  "evidence_refs": ["ev_..."]
}
```

Mọi handoff cần `case_id`, actor nguồn/đích, task type, status, decision code,
facts/IDs, confidence từ 0 đến 1 và evidence refs liên quan. Coordinator không
giao task phụ thuộc ID chưa resolve, trừ task có thể kết luận
`insufficient_evidence`.

Candidate chỉ được `resolved` khi MCP chứng minh relationship phù hợp. Candidate
mâu thuẫn evidence phải vào `rejected_candidates`; nếu chưa đủ chứng cứ thì trả
`ambiguous` hoặc `not_found`, không phát minh ID. Mỗi assignment và handoff có
trace event `task_assigned` hoặc `handoff`; không trace nội dung suy luận riêng.

## 5. Evidence và conflict lifecycle

1. Discover tool MCP một lần cho mỗi run.
2. Gọi tool đã discover với `case_id` hiện tại và tham số hẹp nhất có thể.
3. Validate toàn bộ MCP response theo `mcp-evidence-response-v1`.
4. Cache response theo `(tool, arguments)` chỉ trong state của case hiện tại.
5. Khi actor dùng response, emit `tool_result_consumed` cùng evidence ref.
6. Map evidence refs vào finding, claim và output section liên quan.
7. Áp source precedence từ policy hoặc ghi conflict chưa giải quyết.

Một `data_conflict` phải có field, ít nhất hai sources, selected source (hoặc
`null`) và resolution code. Missing evidence vẫn là missing; không thay bằng
dữ liệu đoán.

## 6. Failure và efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout/transient error | 1 retry | Mark analysis insufficient, tiếp tục với evidence sẵn có | `policy_decided` / `mcp_unavailable` |
| Entity ambiguous/not found | Không broad retry | Trả `ambiguous`/`not_found`, không invent ID | `handoff` / `entity_unresolved` |
| Source conflict | 1 targeted lookup | Áp policy hoặc giữ `selected_source: null` | `policy_decided` / `source_conflict` |
| Specialist result invalid | 1 local repair | Reject finding, chỉ finalize sau verifier-safe reconstruction | `verification_completed` / `specialist_invalid` |

Mọi retry phải idempotent. Không scan unrelated customers, orders hoặc global
history. Các MCP call, kể cả call không dùng ở output, đều ảnh hưởng efficiency.

## 7. Verification invariants

Trước `case_finalized`, Verifier kiểm tra:

- output match `l3b-output-v2`, không có extra fields;
- `case_id` và entities thuộc đúng case;
- `resolved_order_ids` và `rejected_candidates` không giao nhau;
- mọi evidence ref là original, case-scoped và được link tới claim/analysis;
- shipment timeline phù hợp verdict;
- captured, refunded, refundable và recommended refund nhất quán;
- source precedence phù hợp `data_conflicts`;
- responsible parties, root cause, case status và actions không mâu thuẫn;
- mọi confidence nằm trong `[0, 1]`;
- trace theo thứ tự có `case_received`, `task_assigned`, `handoff`,
  `verification_completed`, `case_finalized`.

## 8. Implementation boundary và reproducibility

Điểm tích hợp là `src/student_agent/workflow.py`:

```python
async def solve_case(case, gateway, trace) -> dict:
    ...
```

Pha 3 sẽ hiện thực coordinator và specialist functions theo thiết kế này, bằng
Python async thuần hoặc framework tùy chọn, nhưng không thay đổi public
contracts. Hệ thống dùng Python 3.11+, dependencies trong `pyproject.toml`,
MCP tool set đã discover và query policy có giới hạn. Với MCP responses giống
nhau, quyết định nghiệp vụ phải deterministic. Credentials chỉ ở `.env`, không
được ghi vào output, trace hay submission archive.
