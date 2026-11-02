"""宣告利率抓取工具測試；以假的網路回應執行，不連線到保險公司網站。

回應格式比照 2026-11-02 實際抓取的 API 結構；數值為測試用，不代表真實利率。
執行：python -B -m unittest discover -s tests -p test_scrape_rates.py -v
外部套件：無。
"""

import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from tools import scrape_rates as sr

NANSHAN, TAIWANLIFE, FUBON = (next(t for t in sr.TARGETS if t["insurer"] == k)
                              for k in ("nanshan", "taiwanlife", "fubon"))


def ok(payload):
    return 200, json.dumps(payload, ensure_ascii=False).encode("utf-8")


class FakeSites:
    """依網址回傳假的 API 回應；可設定某家失敗或回傳特殊資料。"""

    def __init__(self, *, fail=(), fubon_rows=None, nanshan_options=None):
        self.fail = set(fail)
        self.calls = []
        self.fubon_rows = fubon_rows if fubon_rows is not None else [
            {"date": "115/11", "itemName": FUBON["name"], "item": "FBO", "rate": "4.10%"},
            {"date": "115/10", "itemName": FUBON["name"], "item": "FBO", "rate": "4.00%"},
            {"date": "115/11", "itemName": "其他商品", "item": "FBP", "rate": "9.99%"},
        ]
        self.nanshan_options = nanshan_options if nanshan_options is not None else [
            {"id": "389", "name": NANSHAN["name"]}, {"id": "1", "name": "其他商品"}]

    def __call__(self, method, url, body, headers):
        self.calls.append((method, url, body, headers))
        host = urlparse(url).netloc
        if any(name in host for name in self.fail):
            raise sr.ScrapeError(f"模擬連線失敗：{host}")
        if "fubon" in host:
            page = json.loads(body)["page"]
            rows = self.fubon_rows if page == 0 else []
            return ok({"success": True, "returnCode": "0000",
                       "data": {"totalPages": 1, "rateList": rows}})
        if "nanshanlife" in host:
            if url.endswith("getTypesProducts"):
                return ok({"header": {"code": "0"}, "datas": [
                    {"type_id": "-1", "option_list": self.nanshan_options},
                    {"type_id": "17", "option_list": self.nanshan_options}]})
            query = parse_qs(urlparse(url).query)
            assert query["productId"] == ["389"] and query["productType"] == ["17"]
            return ok({"header": {"code": "0"}, "page_Info": {"total_page_size": 1}, "datas": [
                {"name": NANSHAN["name"] + "【ACUPL】", "date": "115/11", "rate": "4.05%"}]})
        if "taiwanlife" in host:
            if "/Rate/Product/" in url:
                return ok({"header": {"code": "0"}, "datas": [
                    {"item_serno": 465, "item_name": TAIWANLIFE["name"]},
                    {"item_serno": 122, "item_name": "台灣人壽超美利外幣利率變動型保險"}]})
            page = json.loads(body)["page_no"]
            return ok({"header": {"code": "0"}, "page_info": {"total_page_size": 2}, "datas": [
                {"date": f"2026/{12 - page:02d}", "item_serno": 465,
                 "item_name": TAIWANLIFE["name"], "rate": "3.95"}]})
        return 404, b"{}"


class NormalizeTest(unittest.TestCase):
    def test_period_month(self):
        self.assertEqual(sr.to_period_month("115/11"), "2026-11")
        self.assertEqual(sr.to_period_month("115/6"), "2026-06")
        self.assertEqual(sr.to_period_month("2026/11"), "2026-11")
        for bad in ("", "115", "115/13", "abc"):
            with self.assertRaises(sr.ScrapeError):
                sr.to_period_month(bad)

    def test_rate_percent(self):
        self.assertEqual(sr.to_rate_percent("4.35%"), 4.35)
        self.assertEqual(sr.to_rate_percent("4.20"), 4.2)
        for bad in ("", None, "未揭露", "nan", "0", "35%"):
            with self.assertRaises(sr.ScrapeError):
                sr.to_rate_percent(bad)


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.raw, self.processed = root / "raw", root / "processed"

    def run_tool(self, fetch, targets=sr.TARGETS):
        return sr.run(targets, fetch=fetch, raw_dir=self.raw, processed_dir=self.processed, delay=0)

    def test_all_targets_parsed_and_snapshots_saved(self):
        sites = FakeSites()
        result = self.run_tool(sites)
        self.assertTrue(all(s["ok"] for s in result["status"].values()), result["status"])
        by_product = {}
        for r in result["records"]:
            by_product.setdefault(r["product_id"], []).append((r["period_month"], r["rate_percent"]))
        self.assertEqual(by_product["prd_fbo"], [("2026-11", 4.1), ("2026-10", 4.0)])  # 其他代碼不採計
        self.assertEqual(by_product["prd_acupl"], [("2026-11", 4.05)])
        self.assertEqual(by_product["prd_u09r0"], [("2026-11", 3.95), ("2026-10", 3.95)])  # 兩頁都讀到

        run_dir = self.raw / result["run_id"]
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["requests"]), len(sites.calls))
        for entry in manifest["requests"]:
            self.assertTrue((run_dir / entry["file"]).exists())
            self.assertEqual(entry["status"], 200)
            self.assertEqual(len(entry["sha256"]), 64)
        for record in result["records"]:
            self.assertTrue((run_dir / record["raw_file"]).exists())
            self.assertTrue(record["captured_at"].endswith("+08:00"))
        self.assertTrue((self.processed / "declared_rates.csv").exists())

    def test_failure_keeps_previous_records(self):
        self.run_tool(FakeSites())
        result = self.run_tool(FakeSites(fail={"fubon"}))
        self.assertFalse(result["status"]["prd_fbo"]["ok"])
        self.assertEqual(result["status"]["prd_fbo"]["kept_previous"], 2)
        kept = [r["period_month"] for r in result["records"] if r["product_id"] == "prd_fbo"]
        self.assertEqual(kept, ["2026-11", "2026-10"])
        self.assertTrue(result["status"]["prd_acupl"]["ok"])

    def test_no_rows_is_failure_not_empty_data(self):
        result = self.run_tool(FakeSites(fubon_rows=[]), targets=[FUBON])
        self.assertFalse(result["status"]["prd_fbo"]["ok"])
        self.assertEqual(result["records"], [])

    def test_ambiguous_product_name_is_rejected(self):
        twins = [{"id": "389", "name": NANSHAN["name"]}, {"id": "390", "name": NANSHAN["name"]}]
        result = self.run_tool(FakeSites(nanshan_options=twins), targets=[NANSHAN])
        self.assertFalse(result["status"]["prd_acupl"]["ok"])
        self.assertIn("無法確認", result["status"]["prd_acupl"]["error"])

    def test_conflicting_rates_same_month_rejected(self):
        rows = [{"date": "115/11", "itemName": FUBON["name"], "item": "FBO", "rate": "4.10%"},
                {"date": "115/11", "itemName": FUBON["name"], "item": "FBO", "rate": "4.20%"}]
        result = self.run_tool(FakeSites(fubon_rows=rows), targets=[FUBON])
        self.assertFalse(result["status"]["prd_fbo"]["ok"])
        self.assertIn("不同的宣告利率", result["status"]["prd_fbo"]["error"])

    def test_http_error_is_recorded(self):
        def not_found(method, url, body, headers):
            return 500, b"server error"
        result = self.run_tool(not_found, targets=[FUBON])
        self.assertIn("HTTP 500", result["status"]["prd_fbo"]["error"])
        manifest = json.loads((self.raw / result["run_id"] / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["requests"][0]["status"], 500)

    def test_compare_lists_differences_only(self):
        result = self.run_tool(FakeSites(), targets=[FUBON])
        dataset = {"rates": [{"product_id": "prd_fbo", "period_month": "2026-10", "rate_percent": 3.5}]}
        lines = sr.compare_with_dataset(result, dataset)
        self.assertIn("官網最新 2026-11 4.1%", lines[0])
        self.assertTrue(any("2026-10 快照 3.5% ≠ 官網 4.0%" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
