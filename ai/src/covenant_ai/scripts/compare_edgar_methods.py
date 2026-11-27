"""Compare both EDGAR methods on identical local inputs without changing a case or seed."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from statistics import median

from covenant_ai.domain.evidence import MethodologyVersion
from covenant_ai.providers.edgar import EdgarMarketDataProvider
from covenant_ai.providers.edgar_manifest import atomic_write_bytes


METRICS = ("total_debt", "interest_expense_net", "ebit", "ebitda", "market_cap",
           "enterprise_value", "ev_to_ebitda")


def checksums(cache: Path, prices: Path, catalog: Path) -> dict[str, str]:
    paths = {f"cache/{p.name}": p for p in cache.glob("*.json") if p.is_file()}
    paths.update(prices=prices, catalog=catalog)
    package = Path(__file__).resolve().parents[1]
    for subdir in ("domain", "providers", "scripts"):
        for source in (package / subdir).glob("*.py"):
            paths[source.relative_to(package).as_posix()] = source
    digests = {}
    for name, path in sorted(paths.items()):
        with path.open("rb") as handle:
            digests[name] = hashlib.file_digest(handle, "sha256").hexdigest()
    return digests


def compare(cache: Path, prices: Path, catalog: Path) -> dict:
    manifest = checksums(cache, prices, catalog)
    provider = EdgarMarketDataProvider(
        cache_dir=cache, snapshot_csv=prices, catalog_path=catalog, mode="offline",
        clock=lambda: datetime(2000, 1, 1, tzinfo=UTC),
    )
    rows = []
    cohorts = []
    for industry in sorted(provider.catalog.ensure_loaded()):
        tickers = sorted(provider.get_candidate_peers(industry))
        for period in ("FY", "LTM"):
            multiples: dict[str, list[Decimal]] = {v.value: [] for v in MethodologyVersion}
            for ticker in tickers:
                results = {}
                for version in MethodologyVersion:
                    outcome = provider.extract_peer_outcome(ticker, period, method_version=version.value)
                    evidence = outcome.evidence_by_metric
                    metrics = {m: str(evidence[m].value) for m in METRICS
                               if m in evidence and evidence[m].is_usable}
                    refs = {dp.metric: dp.source_ref for dp in outcome.datapoints
                            if dp.metric in {"total_debt", "interest_expense_net", "market_cap"}}
                    if outcome.failure:
                        status = outcome.failure.reason_code.value
                        reason = outcome.failure.message
                    elif "ev_to_ebitda" not in metrics:
                        status, reason = "ineligible", evidence["ev_to_ebitda"].reason
                    elif Decimal(metrics["enterprise_value"]) < Decimal("100000000"):
                        status, reason = "ineligible", "EV below the FIN-08 floor"
                    else:
                        status, reason = "eligible", ""
                        multiples[version.value].append(Decimal(metrics["ev_to_ebitda"]))
                    results[version.value] = dict(status=status, reason=reason, metrics=metrics, sources=refs)
                rows.append(dict(industry=industry, ticker=ticker, period=period, methods=results))
            for version, values in multiples.items():
                cohorts.append(dict(industry=industry, period=period, method=version,
                                    requested=len(tickers), eligible=len(values),
                                    median=str(median(values)) if len(values) >= 3 else None))
    if manifest != checksums(cache, prices, catalog):
        raise RuntimeError("Comparison inputs changed during execution. Re-run on a stable copy.")
    return {"inputs_sha256": manifest, "cohorts": cohorts, "rows": rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("cache", "prices", "catalog", "output"):
        parser.add_argument(f"--{name}", required=True, type=Path)
    args = parser.parse_args()
    if args.output.resolve().is_relative_to(args.cache.resolve()) or args.output.exists():
        parser.error("Choose a new output file outside the input cache.")
    result = compare(args.cache, args.prices, args.catalog)
    atomic_write_bytes(args.output, json.dumps(result, indent=2).encode("utf-8"))
    print(f"Wrote {len(result['rows'])} peer-period comparisons to {args.output}")


if __name__ == "__main__":
    main()
