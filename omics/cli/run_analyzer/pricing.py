"""Pricing lookup and caching for HealthOmics resources."""

from __future__ import annotations

import json
from typing import Any, Optional

OMICS_SERVICE_CODE = "AmazonOmics"
PRICING_AWS_REGION = "us-east-1"  # Pricing service endpoint


class PricingCache:
    """Cache for HealthOmics pricing lookups.

    Wraps the AWS Pricing API client and caches per-resource/region prices
    for the lifetime of the instance.
    """

    def __init__(self, pricing_client=None):
        """Initialize with an optional AWS Pricing API client."""
        self._client = pricing_client
        self._cache: dict[str, float] = {}

    def get_price(self, resource: str, region: str, hours: float) -> Optional[float]:
        """Return the price (USD) for *hours* of *resource* in *region*, or None."""
        key = f"{resource}:{region}"
        price = self._cache.get(key)
        if price is not None:
            return price * hours
        if not self._client:
            return None

        filters = [
            {"Type": "TERM_MATCH", "Field": "resourceType", "Value": resource},
            {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
        ]
        rqst = {"ServiceCode": OMICS_SERVICE_CODE, "Filters": filters}
        for page in self._client.get_paginator("get_products").paginate(**rqst):
            for item in page["PriceList"]:
                entry: dict[str, Any] = json.loads(item)
                terms: dict[str, Any] = entry.get("terms", {})
                on_demand: dict[str, Any] = terms.get("OnDemand", {})
                price_dim: dict[str, Any] = next(iter(on_demand.values()), {})
                dimensions: dict[str, Any] = price_dim.get("priceDimensions", {})
                per_unit_wrapper: dict[str, Any] = next(iter(dimensions.values()), {})
                per_unit: dict[str, Any] = per_unit_wrapper.get("pricePerUnit", {})
                usd = per_unit.get("USD")
                if usd is None:
                    continue
                usd = float(usd)
                self._cache[key] = usd
                return usd * hours
        return None
