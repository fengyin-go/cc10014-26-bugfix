"""缓存链路回归测试：直接用服务层复现并复核题目中的复测口径。

运行：backend/.venv/bin/python -m unittest tests.test_cache_flow -v
"""
from __future__ import annotations

import unittest

from app.services.cache import CacheService
from app.services.cache_contract import contract_status, dataset, hit_rate
from app.services.query import QueryService
from app.store import store


class CacheFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        store.reload()
        self.cache = CacheService()
        self.query = QueryService()
        self.entry = self.query.get_entry(1)  # QUER-0001，订单查询

    def context(self, **params: str) -> dict:
        context, state = self.query.resolve_context(self.entry, {"params": params})
        self.assertEqual(state, "ok")
        return context

    def test_registration_requires_ttl_and_max_entries(self) -> None:
        entry, missing = self.cache.create_entry(
            {"策略编号": "CACH-9101", "关联服务": "x", "缓存键模板": "q:{order_id}", "关联查询定义": "QUER-0001", "转换版本": "v2"}
        )
        self.assertIsNone(entry)
        self.assertEqual(missing, ["过期秒数", "最大条目"])

    def test_blank_template_rejected_on_create_and_enable(self) -> None:
        entry, missing = self.cache.create_entry(
            {"策略编号": "CACH-9102", "关联服务": "x", "缓存键模板": "   ", "过期秒数": 60, "最大条目": 10,
             "关联查询定义": "QUER-0001", "转换版本": "v2"}
        )
        self.assertIsNone(entry)
        self.assertEqual(missing, ["缓存键模板"])
        rejected, message = self.cache.run_action(2, "启用缓存")  # CACH-0002：空模板且缺过期/最大条目
        self.assertIsNone(rejected)
        self.assertIn("缓存键模板不能为空", message)
        self.assertIn("过期秒数", message)
        self.assertIn("最大条目", message)

    def test_cache_key_binds_template_params_and_transform_version(self) -> None:
        key_a = self.context(order_id="A1001")["cache_key"]
        key_b = self.context(order_id="B2002")["cache_key"]
        key_a_v1 = self.query.resolve_context(
            self.entry, {"params": {"order_id": "A1001"}, "transform_version": "v1"}
        )[0]["cache_key"]
        self.assertNotEqual(key_a, key_b)
        self.assertNotEqual(key_a, key_a_v1)
        self.assertIn('"order_id":"A1001"', key_a)
        self.assertTrue(key_a.endswith("|tpl=v2"))
        self.assertTrue(key_a_v1.endswith("|tpl=v1"))

    def test_different_params_never_share_cache_values(self) -> None:
        self.assertEqual(self.query.execute(1, {"operation": "write", "params": {"order_id": "A1001"}})["state"], "written")
        self.assertEqual(self.query.execute(1, {"operation": "write", "params": {"order_id": "B2002"}})["state"], "written")
        result_a = self.query.execute(1, {"params": {"order_id": "A1001"}})
        result_b = self.query.execute(1, {"params": {"order_id": "B2002"}})
        self.assertEqual(result_a["state"], "hit")
        self.assertEqual(result_b["state"], "hit")
        self.assertEqual({row["order_id"] for row in result_a["items"]}, {"A1001"})
        self.assertEqual({row["order_id"] for row in result_b["items"]}, {"B2002"})
        amounts_a = sorted(row["amount"] for row in result_a["items"])
        self.assertEqual(amounts_a, [9990, 50800, 129900])

    def test_old_transform_version_value_excluded_from_current_result(self) -> None:
        # 种子里已有一条 v1 旧值；默认当前版本为 v2，旧值不能被当成命中。
        result = self.query.execute(1, {"params": {"order_id": "A1001"}})
        self.assertEqual(result["state"], "stale")
        self.assertEqual(result["items"], [])
        self.assertTrue(result["cache_key"].endswith("|tpl=v2"))
        # 显式按 v1 读取时才允许读到 v1 的值。
        legacy = self.query.execute(1, {"params": {"order_id": "A1001"}, "transform_version": "v1"})
        self.assertEqual(legacy["state"], "hit")
        self.assertTrue(legacy["cache_key"].endswith("|tpl=v1"))

    def test_version_conflict_never_falls_back_to_lower_version(self) -> None:
        self.query.execute(1, {"operation": "write", "params": {"order_id": "A1001"}})
        self.cache.run_action(1, "停用缓存")  # 停用 v2 策略
        result = self.query.execute(1, {"params": {"order_id": "A1001"}})
        self.assertEqual(result["state"], "version_unbound")
        self.assertEqual(result["items"], [])
        # 即使再启用 v1 策略，默认仍停留在 v2 水位，不回落到 v1 旧值。
        self.cache.run_action(3, "启用缓存")
        self.assertEqual(self.query.execute(1, {"params": {"order_id": "A1001"}})["state"], "version_unbound")

    def test_params_and_version_change_together(self) -> None:
        self.query.execute(1, {"operation": "write", "params": {"order_id": "A1001"}, "transform_version": "v2"})
        self.query.execute(1, {"operation": "write", "params": {"order_id": "B2002"}, "transform_version": "v2"})
        result = self.query.execute(1, {"params": {"order_id": "B2002"}, "transform_version": "v1"})
        self.assertNotEqual(result["state"], "hit")
        self.assertTrue(result["cache_key"].endswith("|tpl=v1"))
        self.assertIn('"order_id":"B2002"', result["cache_key"])

    def test_timeout_and_retry_return_explicit_empty_state(self) -> None:
        self.query.execute(1, {"operation": "write", "params": {"order_id": "A1001"}})
        self.assertEqual(self.query.execute(1, {"params": {"order_id": "A1001"}})["state"], "hit")
        timeout = self.query.execute(1, {"params": {"order_id": "A1001"}, "timeout": True})
        self.assertEqual(timeout["state"], "timeout")
        self.assertEqual(timeout["items"], [])
        retry = self.query.execute(1, {"params": {"order_id": "A1001"}})
        self.assertEqual(retry["state"], "invalidated")
        self.assertEqual(retry["items"], [])
        # 失效后重新写入才能恢复命中，不会复用超时前的旧值。
        self.assertEqual(self.query.execute(1, {"operation": "write", "params": {"order_id": "A1001"}})["state"], "written")
        self.assertEqual(self.query.execute(1, {"params": {"order_id": "A1001"}})["state"], "hit")

    def test_empty_dataset_returns_empty_state_not_old_value(self) -> None:
        key = self.context(order_id="A1001")["cache_key"]
        self.query.execute(1, {"operation": "write", "params": {"order_id": "A1001"}})
        for row in store.rows("_cache_datasets"):
            if row.get("缓存键") == key and row.get("状态") != "无效":
                row["状态"] = "无效"
        result = self.query.execute(1, {"params": {"order_id": "A1001"}})
        self.assertEqual(result["state"], "empty")
        self.assertFalse(result["hit"])
        self.assertEqual(result["items"], [])

    def test_interrupted_batch_resumes_from_last_complete_batch(self) -> None:
        interrupted = self.query.execute(
            1, {"operation": "write", "params": {"order_id": "A1001"}, "batch_size": 2, "interrupt_after": 2}
        )
        self.assertEqual(interrupted["state"], "interrupted")
        self.assertEqual(interrupted["written"], 2)
        self.assertEqual(interrupted["resumable_from"], 2)
        self.assertEqual(self.query.execute(1, {"params": {"order_id": "A1001"}})["state"], "partial")
        resumed = self.query.execute(1, {"operation": "write", "params": {"order_id": "A1001"}, "batch_size": 2, "resume": True})
        self.assertEqual(resumed["state"], "written")
        self.assertEqual(resumed["resumed_from"], 2)
        self.assertEqual(resumed["batch"]["批次号"], interrupted["batch"]["批次号"])
        final = self.query.execute(1, {"params": {"order_id": "A1001"}})
        self.assertEqual(final["state"], "hit")
        self.assertEqual(len(final["items"]), 3)
        valid = [row for row in dataset(final["cache_key"]) if row.get("状态") == "有效"]
        self.assertEqual(len(valid), 3)
        self.assertEqual({row["批次号"] for row in valid}, {final["batch"]})

    def test_disable_policy_syncs_invalidation_to_contract(self) -> None:
        result = self.query.execute(1, {"operation": "write", "params": {"order_id": "A1001"}})
        key = result["cache_key"]
        self.assertIn(key, [item["缓存键"] for item in contract_status()["valid_keys"]])
        self.cache.run_action(1, "停用缓存")
        status = contract_status()
        self.assertNotIn(key, [item["缓存键"] for item in status["valid_keys"]])
        self.assertIn(key, [item["缓存键"] for item in status["invalid_keys"]])
        self.assertEqual(self.query.execute(1, {"params": {"order_id": "A1001"}})["state"], "version_unbound")

    def test_hit_rate_and_dataset_are_auditable(self) -> None:
        self.query.execute(1, {"operation": "write", "params": {"order_id": "A1001"}})
        result = self.query.execute(1, {"params": {"order_id": "A1001"}})
        self.query.execute(1, {"params": {"order_id": "A1001"}})
        self.assertEqual(hit_rate(result["cache_key"]), 1.0)
        rows = dataset(result["cache_key"])
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertEqual(row["查询参数"], '{"order_id":"A1001"}')
            self.assertEqual(row["转换版本"], "v2")
            self.assertEqual(row["批次号"], result["batch"])

    def test_detail_view_and_contract_agree(self) -> None:
        self.query.execute(1, {"operation": "write", "params": {"order_id": "A1001"}})
        detail = self.query.detail_view(self.query.get_entry(1))
        contract_keys = [item["缓存键"] for item in contract_status()["valid_keys"]]
        self.assertTrue(detail["valid_keys"])
        for item in detail["valid_keys"]:
            self.assertIn(item["缓存键"], contract_keys)
            self.assertEqual(item["命中率"], hit_rate(item["缓存键"]))
        self.assertTrue(all(binding["可求缓存键"] for binding in detail["bindings"] if binding["状态"] == "已启用"))


if __name__ == "__main__":
    unittest.main()
