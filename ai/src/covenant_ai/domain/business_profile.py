"""Business profile: tiêu chí ĐỊNH TÍNH để lọc peer.

Theo Rosenbaum & Pearl (Comparable Companies Analysis), chọn peer gồm hai tầng:

  1. Business profile  (file này)  -> loại hẳn công ty khác bản chất
  2. Financial profile (metrics.py, SCREENING_CATEGORIES) -> chấm điểm độ gần

Business profile gồm 5 chiều:
  sector | products & services | customers & end markets
  | distribution channels | geography

Mỗi chiều có `evidence`: trích dẫn/tham chiếu nơi thông tin đó xuất hiện
(vd "10-K FY2025, Item 1 Business"). Đây là thứ giúp defend: mỗi quyết định
loại/giữ peer đều truy được về nguồn.
"""

from dataclasses import dataclass, field
from enum import StrEnum


class BusinessDimension(StrEnum):
    SECTOR = "sector"
    PRODUCTS_SERVICES = "products_services"
    CUSTOMERS_END_MARKETS = "customers_end_markets"
    DISTRIBUTION_CHANNELS = "distribution_channels"
    GEOGRAPHY = "geography"


@dataclass(frozen=True)
class BusinessProfile:
    """Mô tả định tính của một công ty theo 5 chiều của sách."""

    ticker: str
    # Sector: dùng phân loại chuẩn (GICS / SIC) để so sánh được
    sector: str  # vd "Information Technology"
    sub_industry: str  # vd "Application Software"
    products_services: tuple[str, ...] = ()
    customers_end_markets: tuple[str, ...] = ()  # vd ("enterprise", "SMB")
    distribution_channels: tuple[str, ...] = ()  # vd ("direct sales", "resellers")
    geography: tuple[str, ...] = ()  # vd ("North America", "Europe")
    # chiều -> nơi thông tin xuất hiện (để truy nguồn khi bảo vệ)
    evidence: dict[BusinessDimension, str] = field(default_factory=dict)

    def missing_dimensions(self) -> list[BusinessDimension]:
        """Chiều còn trống, để agent/người duyệt biết cần điền gì."""
        values = {
            BusinessDimension.SECTOR: self.sector and self.sub_industry,
            BusinessDimension.PRODUCTS_SERVICES: self.products_services,
            BusinessDimension.CUSTOMERS_END_MARKETS: self.customers_end_markets,
            BusinessDimension.DISTRIBUTION_CHANNELS: self.distribution_channels,
            BusinessDimension.GEOGRAPHY: self.geography,
        }
        return [dim for dim, v in values.items() if not v]


def business_overlap(a: BusinessProfile, b: BusinessProfile) -> dict[BusinessDimension, float]:
    """Độ trùng 0..1 theo từng chiều (Jaccard cho tập, khớp chính xác cho sector).

    Chỉ là dữ kiện thô cho PeerCriteria dùng sau; chưa quyết định giữ/loại.
    """

    def jaccard(x: tuple[str, ...], y: tuple[str, ...]) -> float:
        sx, sy = {i.lower() for i in x}, {i.lower() for i in y}
        if not sx and not sy:
            return 0.0
        return len(sx & sy) / len(sx | sy)

    return {
        BusinessDimension.SECTOR: 1.0 if (a.sector, a.sub_industry) == (b.sector, b.sub_industry) else 0.0,
        BusinessDimension.PRODUCTS_SERVICES: jaccard(a.products_services, b.products_services),
        BusinessDimension.CUSTOMERS_END_MARKETS: jaccard(a.customers_end_markets, b.customers_end_markets),
        BusinessDimension.DISTRIBUTION_CHANNELS: jaccard(a.distribution_channels, b.distribution_channels),
        BusinessDimension.GEOGRAPHY: jaccard(a.geography, b.geography),
    }