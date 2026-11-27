# Project Covenant

**Đồ án Software Engineering · Python 3.12+ · LangGraph · PostgreSQL**

Covenant giúp người phân tích lập một hồ sơ doanh nghiệp, chọn nhóm công ty so sánh và duyệt báo cáo. Bài toán thiết kế nằm ở khoảng giữa các bước đó. Nếu người dùng đóng ứng dụng sau khi sửa hồ sơ, lần mở tiếp theo phải dùng đúng bản sửa. Nếu dữ liệu nguồn đổi ngày mai, người đọc vẫn phải giải thích được con số trong báo cáo hôm nay.

## Một hồ sơ đi qua hệ thống như thế nào?

~~~mermaid
flowchart LR
    A["Nhập hồ sơ"] --> B{"Đủ dữ liệu?"}
    B -- "Cần bổ sung" --> A
    B -- "Đủ" --> C["Xác nhận ngành"]
    C --> D["Xếp hạng và duyệt công ty so sánh"]
    D --> E{"Đủ công ty hợp lệ?"}
    E -- "Còn ứng viên" --> D
    E -- "Hết lựa chọn" --> F["Lưu lý do dừng"]
    E -- "Đủ" --> G["Tính và kiểm tra"]
    G --> H["Đóng băng bản nháp"]
    H --> I["Người phụ trách duyệt đúng phiên bản"]
~~~

### Các quyết định thiết kế

| Câu hỏi | Quyết định | Đánh đổi |
| --- | --- | --- |
| Một phiên làm việc bị ngắt rồi mở lại thì sao? | Mỗi hồ sơ chạy dưới một thread riêng. CLI giữ khóa theo thread. Checkpoint ghi bước tiếp theo, còn PostgreSQL lưu phiên bản hồ sơ và những quyết định đã chấp nhận. | Phải đối chiếu hai nơi sau sự cố. Các thao tác đồng bộ có thể chạy lại mà vẫn giữ nguyên bản kết quả đã chốt. |
| Khi nào được phép tính tiếp? | Lớp domain biểu diễn dữ liệu chưa biết bằng giá trị rỗng. Ví dụ, thiếu nợ hoặc tiền mặt vẫn tính được EV. Giá trị vốn chủ sở hữu chờ đủ cả hai trường. | Người dùng có thể nhận một kết quả hẹp hơn, với lý do được ghi trong báo cáo. |
| Làm sao giải thích kết quả cũ? | Một bản báo cáo đi cùng hồ sơ, đầu vào đã chọn, mã nguồn dữ liệu và phiên bản phương pháp. Đợt nạp dữ liệu mới có mã riêng. | Tốn thêm dung lượng để giữ lịch sử. Đổi lại, việc xem và duyệt lại báo cáo dùng bản đã đóng băng. |
| Phần nào cần phán đoán của con người? | Người dùng xác nhận ngành, rà danh sách so sánh và giải thích lựa chọn ngoài danh sách đề xuất. Phép tính dùng hàm Python và Decimal. | Việc duyệt nhóm so sánh và việc cho phép giao báo cáo là hai quyết định riêng, có thể xảy ra ở hai thời điểm khác nhau. |
| Dữ liệu doanh nghiệp có ra ngoài máy? | CLI dùng quy trình cục bộ cho hồ sơ và lời bình. Các adapter dữ liệu công khai tra cứu mã chứng khoán. Các bài test có thể thay nguồn và bộ sinh lời bình bằng đối tượng giả. | Chế độ nhập đoạn văn tự do cần một bộ trích xuất cục bộ được tích hợp. Bản cài mặc định hỗ trợ JSON và hỏi đáp. |

### Hai kiểu lưu trữ phục vụ hai câu hỏi

**Checkpoint trả lời “chạy tiếp từ đâu?”** Nó lưu trạng thái đồ thị và điểm đang đợi người dùng. Một phiên bị dừng sau bước tính vẫn phải hoàn tất bước kiểm tra trước khi chốt.

**Bản đóng băng trả lời “người duyệt đã xem gì?”** Kết quả đã chốt có danh sách nguồn, lý do loại công ty và mã SHA-256 của nội dung. Khi duyệt, chương trình đối chiếu mã này và tài khoản Windows đã đăng ký. Quyết định duyệt được lưu riêng. Có thể xuất lại báo cáo qua mã thread mà không gọi nguồn dữ liệu thị trường.

## Chạy thử trên Windows

Cần Python 3.12+, Docker và PowerShell. Từ thư mục gốc, sao chép cấu hình mẫu cho lần cài đầu tiên, đặt thông tin liên hệ ở `SEC_USER_AGENT` cho các lần lấy dữ liệu công khai, rồi khởi động PostgreSQL trên máy.

~~~powershell
Copy-Item .env.example .env
docker compose up -d postgres
cd ai
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install -e .
~~~

Để seed bộ dữ liệu, cần có giá đóng cửa kèm ngày và nguồn trong `ai/market_price_snapshots.csv`. Repo cung cấp mẫu cột trong `market_price_snapshots.example.csv`. Nếu chuẩn bị dữ liệu cho một bản sao mới, kiểm tra cấu hình SEC, rồi chạy công cụ discovery trong bản sao đó. Lệnh discovery có thể cập nhật danh sách công ty và file giá. Chạy seed sau khi đã rà bộ đầu vào.

~~~powershell
python -m covenant_ai.scripts.discover_peers --dry-run
# Khi đã có bộ giá và danh sách đã kiểm tra
python -m covenant_ai.scripts.seed_db
~~~

**Một kịch bản demo cho buổi bảo vệ.** Lưu ví dụ sau thành `intake.json` trong thư mục làm việc. Công ty trong ví dụ là giả.

~~~json
{
  "company_name": "Demo Controls",
  "business_description": "Industrial controls manufacturer",
  "industry": "industrial_machinery",
  "geography": "US",
  "ebitda_period": "FY",
  "revenue": "32000000",
  "ebitda": "4800000"
}
~~~

~~~powershell
covenant --input intake.json --thread-id swe-demo --pause-after-intake
covenant --resume swe-demo --export-md draft.md --export-json result.json
covenant --review-report swe-demo
~~~

Lần đầu, CLI dừng để xác nhận hồ sơ. Lần thứ hai, bạn có thể sửa ngành, xem danh sách đề xuất và mở bản nháp. Vì ví dụ bỏ trống nợ và tiền mặt, kết quả giải thích tại sao chưa có giá trị vốn chủ sở hữu. Để demo từ đầu lần nữa, dùng thread mới.

**Duyệt giao khách.** Đọc bản nháp và các phát hiện trước. Tài khoản Windows đã đăng ký có thể xác nhận mã của đúng bản vừa xem.

~~~powershell
$digest = (Get-Content result.json -Raw | ConvertFrom-Json).digest
covenant --review-report swe-demo --approve-digest $digest --export-md approved.md
~~~

Bản chạy từ source đọc mã định danh Windows (SID) ở `ai/.covenant-reviewer`. File này chỉ thuộc bản cài trên máy và được loại khỏi Git. Người quản lý bản cài mới phải đăng ký đúng SID và giới hạn quyền sửa file cùng quyền ghi PostgreSQL. Đây là cơ chế duyệt cho ứng dụng cục bộ một người phụ trách. Lệnh duyệt xuất file trên máy, việc giao khách do người phụ trách thực hiện.

## Bản đồ mã nguồn

| Đường dẫn | Trách nhiệm |
| --- | --- |
| [domain](ai/src/covenant_ai/domain/) | Mô hình dữ liệu, kiểm tra đầu vào, chọn nguồn và các hàm tính thuần. |
| [graph](ai/src/covenant_ai/graph/) | Thứ tự các bước, điều kiện rẽ nhánh và điểm dừng chờ xác nhận. |
| [providers](ai/src/covenant_ai/providers/) | Hợp đồng lấy dữ liệu, adapter SEC/PostgreSQL, giao dịch và snapshot. |
| [tools](ai/src/covenant_ai/tools/) và [llm](ai/src/covenant_ai/llm/) | Adapter gọi nghiệp vụ, trích xuất có bằng chứng, lời bình và bước kiểm tra. |
| [cli.py](ai/src/covenant_ai/cli.py) | Tạo phiên, tiếp tục phiên, xuất và duyệt kết quả. |
| [db/init.sql](db/init.sql) | Schema khởi tạo. Các thay đổi cho database sẵn có nằm trong [db_schema.py](ai/src/covenant_ai/providers/db_schema.py). |

## Kiểm thử

Từ `ai/`, có thể kiểm tra riêng quy tắc nghiệp vụ và luồng dừng/tiếp tục bằng dữ liệu giả trước khi bật database.

~~~powershell
python -m pytest tests/domain tests/tools tests/graph/test_workflow.py
~~~

Để chạy toàn bộ suite, trỏ `DATABASE_URL` tới **database kiểm thử riêng** đã có schema và baseline. Các test tích hợp tạo hồ sơ, checkpoint và audit event. Các test SEC dùng cache filing và giá đóng cửa. Một bản clone mới cần chuẩn bị những đầu vào này trước.

~~~powershell
python -m pytest
~~~

Khi đọc test, hãy theo ba tình huống: nhập lại cùng mã thread, khôi phục khi còn bước đang chờ, và xuất lại báo cáo sau khi dữ liệu nền đã đổi. Chúng cho thấy tại sao hệ thống cần đồng thời checkpoint, giao dịch và bản kết quả đóng băng.
