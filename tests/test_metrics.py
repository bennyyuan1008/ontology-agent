# -*- coding: utf-8 -*-
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import query_engine as engine
from metric_service import MetricService


class MetricAndPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = engine.load_config(
            os.path.join(ROOT, "config", "ontology_models.example.yaml")
        )
        engine.DIALECT = "oracle"

    def test_metric_catalog_contains_first_phase_metrics(self):
        expected = {
            "sales_amount",
            "order_count",
            "average_order_value",
            "sku_quantity",
            "inventory_quantity",
            "available_inventory_quantity",
            "stockout_rate",
            "inventory_cover_days",
        }
        self.assertTrue(expected.issubset(self.cfg["metrics"]))
        self.assertEqual(self.cfg["metrics"]["sales_amount"]["status"], "implemented")

    def test_sales_metric_is_parameterized_and_signed(self):
        service = MetricService(self.cfg, dialect="oracle")
        plan, sql, params = service.compile(
            "sales_amount",
            current_window={"start": "2026-03-01", "end": "2026-03-31"},
        )
        self.assertEqual(plan["object"], "OrderItem")
        self.assertIn("CASE WHEN", sql)
        self.assertIn("IN (1, 2, 4)", sql)
        self.assertIn("IN (16)", sql)
        self.assertNotIn("'2026-03-01'", sql)
        self.assertEqual(params, {"p0": "2026-03-01", "p1": "2026-03-31"})
        engine.check_safety(sql)

    def test_invalid_operator_and_object_are_rejected(self):
        with self.assertRaises(ValueError):
            engine.validate_plan(self.cfg, {
                "object": "Order",
                "filters": [{"property": "type", "op": "OR 1=1", "value": 0}],
            })
        with self.assertRaises(ValueError):
            engine.validate_plan(self.cfg, {"object": "Unknown", "limit": 1})

    def test_filter_text_is_bound_instead_of_embedded(self):
        plan = {
            "object": "Product",
            "filters": [{"property": "brand", "op": "=", "value": "x' OR 1=1 --"}],
            "properties": ["plu"],
            "limit": 1,
        }
        with self.assertRaises(ValueError):
            engine.translate_bound(self.cfg, plan)
        safe_plan = {**plan, "filters": [{"property": "brand", "op": "=", "value": "x' OR 1=1"}]}
        sql, params = engine.translate_bound(self.cfg, safe_plan)
        self.assertNotIn("x' OR 1=1", sql)
        self.assertEqual(params["p0"], "x' OR 1=1")

    def test_unimplemented_metric_is_not_executable(self):
        service = MetricService(self.cfg, dialect="oracle")
        with self.assertRaises(ValueError):
            service.compile("stockout_rate")

    def test_breakdown_sql_has_stable_group_alias(self):
        service = MetricService(self.cfg, dialect="oracle")
        _, sql, _ = service.compile(
            "sales_amount",
            current_window={"start": "2026-03-01", "end": "2026-03-31"},
            dimension="product.product_name",
        )
        self.assertIn("AS group_0", sql)
        self.assertIn('GROUP BY "product"."SKU_ID"', sql)


if __name__ == "__main__":
    unittest.main()
