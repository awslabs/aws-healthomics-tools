"""Tests for the PricingCache class."""

import json
import unittest
from unittest.mock import MagicMock

from omics.cli.run_analyzer.pricing import PricingCache


class TestPricingCacheNoClient(unittest.TestCase):
    """Tests for PricingCache when no client is provided."""

    def test_no_client_returns_none(self):
        cache = PricingCache()
        result = cache.get_price("omics.c.xlarge", "us-east-1", 1.0)
        self.assertIsNone(result)

    def test_no_client_with_zero_hours_returns_none(self):
        cache = PricingCache()
        result = cache.get_price("omics.c.xlarge", "us-east-1", 0.0)
        self.assertIsNone(result)


class TestPricingCacheHit(unittest.TestCase):
    """Tests for PricingCache when a price is already cached."""

    def test_cache_hit_returns_price_times_hours(self):
        cache = PricingCache()
        # Manually populate the cache
        cache._cache["omics.c.xlarge:us-east-1"] = 0.10

        result = cache.get_price("omics.c.xlarge", "us-east-1", 2.0)
        self.assertAlmostEqual(result, 0.20)

    def test_cache_hit_does_not_call_api(self):
        client = MagicMock()
        cache = PricingCache(client)
        cache._cache["omics.c.xlarge:us-east-1"] = 0.10

        result = cache.get_price("omics.c.xlarge", "us-east-1", 1.5)
        self.assertAlmostEqual(result, 0.15)
        client.get_paginator.assert_not_called()


class TestPricingCacheMiss(unittest.TestCase):
    """Tests for PricingCache when a price needs to be fetched from the API."""

    def _make_mock_client(self, usd_price="0.05"):
        price_entry = {
            "terms": {
                "OnDemand": {
                    "offer1": {"priceDimensions": {"dim1": {"pricePerUnit": {"USD": usd_price}}}}
                }
            }
        }
        client = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [{"PriceList": [json.dumps(price_entry)]}]
        client.get_paginator.return_value = paginator
        return client

    def test_cache_miss_fetches_from_api(self):
        client = self._make_mock_client("0.05")
        cache = PricingCache(client)

        result = cache.get_price("omics.c.xlarge", "us-east-1", 2.0)
        self.assertAlmostEqual(result, 0.10)
        client.get_paginator.assert_called_once_with("get_products")

    def test_cache_miss_populates_cache(self):
        client = self._make_mock_client("0.08")
        cache = PricingCache(client)

        cache.get_price("omics.m.xlarge", "us-west-2", 1.0)
        # Second call should hit cache
        client.get_paginator.reset_mock()
        result = cache.get_price("omics.m.xlarge", "us-west-2", 3.0)
        self.assertAlmostEqual(result, 0.24)
        client.get_paginator.assert_not_called()

    def test_cache_miss_empty_price_list_returns_none(self):
        client = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [{"PriceList": []}]
        client.get_paginator.return_value = paginator
        cache = PricingCache(client)

        result = cache.get_price("nonexistent", "us-east-1", 1.0)
        self.assertIsNone(result)
