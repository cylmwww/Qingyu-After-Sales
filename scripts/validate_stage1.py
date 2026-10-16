"""离线检查第一阶段数据；不运行客服、业务服务、检索或模型。"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta
from itertools import combinations
import json
from pathlib import Path
import re
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
GROUPS = {"standard_faq", "long_tail", "multi_document", "dependent_multihop", "multi_turn", "unanswerable"}
COLLECTIONS = {"customers": "customer", "products": "product", "campaigns": "campaign", "orders": "order", "order_lines": "order_line",
               "payments": "payment", "logistics": "logistics", "after_sales": "after_sale", "refunds": "refund", "snapshots": "snapshot"}
MONEY_KEYS = {"merchandise_refund_cents", "outbound_shipping_refund_cents", "total_estimated_cents", "approved_cents", "settled_cents"}
SCHEMA_KEYS = {"$schema", "$defs", "$ref", "title", "description", "type", "properties", "required", "additionalProperties", "items",
               "minItems", "maxItems", "uniqueItems", "enum", "const", "pattern", "format", "minLength", "minimum", "minProperties", "anyOf", "oneOf"}


def timestamp(value: str) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value):
        raise ValueError("时间必须是带时区的 RFC3339 时间戳")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("时间缺少时区")
    return parsed


def ref_key(ref: dict[str, Any]) -> tuple[str, str, str]:
    return ref["kind"], ref["id"], ref["version"]


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 对象含重复键：{key}")
        result[key] = value
    return result


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_object,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"非法数值：{value}")))


class SchemaChecker:
    """只支持仓库实际使用的 Schema 子集；未知关键字失败而非静默跳过。"""
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.cache: dict[Path, dict] = {}

    def load(self, path: Path) -> dict:
        path = path.resolve()
        if not path.is_relative_to(self.directory):
            raise ValueError("Schema 引用不能离开 data/schemas")
        if path not in self.cache:
            self.cache[path] = read_json(path)
        return self.cache[path]

    def resolve(self, ref: str, origin: Path) -> tuple[dict, Path]:
        file, _, fragment = ref.partition("#")
        if "://" in file:
            raise ValueError("离线校验禁止远程 Schema 引用")
        target = (origin.parent / file).resolve() if file else origin
        schema = self.load(target)
        for part in fragment.lstrip("/").split("/") if fragment else []:
            schema = schema[part.replace("~1", "/").replace("~0", "~")]
        return schema, target

    def check(self, value: Any, schema: dict, origin: Path, location: str = "$") -> list[str]:
        unknown = set(schema) - SCHEMA_KEYS
        if unknown:
            raise ValueError(f"不支持的 Schema 关键字：{sorted(unknown)}")
        if "$ref" in schema:
            resolved, target = self.resolve(schema["$ref"], origin)
            return self.check(value, resolved, target, location)
        for keyword in ("anyOf", "oneOf"):
            if keyword in schema:
                results = [self.check(value, branch, origin, location) for branch in schema[keyword]]
                matches = sum(not errors for errors in results)
                if (keyword == "anyOf" and matches == 0) or (keyword == "oneOf" and matches != 1):
                    detail = min(results, key=len) if results else []
                    return [f"{location}: 不满足 {keyword}；" + "; ".join(detail)]
        expected = schema.get("type")
        valid = {"object": isinstance(value, dict), "array": isinstance(value, list), "string": isinstance(value, str),
                 "integer": type(value) is int, "boolean": type(value) is bool, "null": value is None}
        if expected and not valid.get(expected, False):
            return [f"{location}: 期望 {expected}，实际 {type(value).__name__}"]
        errors = []
        if "const" in schema and (value != schema["const"] or type(value) is not type(schema["const"])):
            errors.append(f"{location}: 必须为 {schema['const']!r}")
        if "enum" in schema and not any(value == candidate and type(value) is type(candidate) for candidate in schema["enum"]):
            errors.append(f"{location}: 值不在枚举范围")
        if isinstance(value, dict):
            for key in schema.get("required", []):
                if key not in value:
                    errors.append(f"{location}.{key}: 缺少必填字段")
            props = schema.get("properties", {})
            for key, child in value.items():
                if key in props:
                    errors += self.check(child, props[key], origin, f"{location}.{key}")
                elif schema.get("additionalProperties") is False:
                    errors.append(f"{location}.{key}: 未定义字段")
            if len(value) < schema.get("minProperties", 0):
                errors.append(f"{location}: 对象字段数量不足")
        elif isinstance(value, list):
            if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", float("inf")):
                errors.append(f"{location}: 数组数量不符合约束")
            if schema.get("uniqueItems") and len({json.dumps(item, sort_keys=True, ensure_ascii=False) for item in value}) != len(value):
                errors.append(f"{location}: 数组包含重复项")
            for index, child in enumerate(value):
                if "items" in schema:
                    errors += self.check(child, schema["items"], origin, f"{location}[{index}]")
        elif isinstance(value, str):
            if len(value) < schema.get("minLength", 0):
                errors.append(f"{location}: 字符串不能为空")
            if "pattern" in schema and not re.search(schema["pattern"], value):
                errors.append(f"{location}: 不符合 ID 或版本格式")
            if schema.get("format") == "date-time":
                try:
                    timestamp(value)
                except ValueError as error:
                    errors.append(f"{location}: {error}")
        elif type(value) is int and value < schema.get("minimum", -float("inf")):
            errors.append(f"{location}: 数值低于下限")
        return errors


def condition_matches(condition: dict, value: Any) -> bool:
    op, expected = condition["op"], condition["value"]
    if op == "is_known":
        return (value is not None) == expected
    if value is None:
        return False
    if op == "eq":
        return type(value) is type(expected) and value == expected
    if op == "neq":
        return type(value) is not type(expected) or value != expected
    if op == "in":
        return any(type(value) is type(item) and value == item for item in expected)
    if op == "lte":
        return type(value) is int and type(expected) is int and value <= expected
    if op == "gte":
        return type(value) is int and type(expected) is int and value >= expected
    return False


def conditions_overlap(first: list[dict], second: list[dict]) -> bool:
    by_field: dict[str, list[dict]] = {}
    for condition in first + second:
        by_field.setdefault(condition["field"], []).append(condition)
    for conditions in by_field.values():
        candidates: list[Any] = [True, False, None, "__other__"]
        for condition in conditions:
            value = condition["value"]
            candidates.extend(value if isinstance(value, list) else [value])
            if type(value) is int:
                candidates.extend([value - 1, value + 1])
        try:
            if not any(all(condition_matches(condition, value) for condition in conditions) for value in candidates):
                return False
        except TypeError:
            numeric = [v for v in candidates if type(v) is int]
            if not any(all(condition_matches(condition, value) for condition in conditions) for value in numeric):
                return False
    return True


class Validation:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.errors: list[dict] = []
        self.checks: Counter = Counter()
        self.index: dict[tuple, dict] = {}
        self.identities: dict[str, str] = {}
        self.documents: list[tuple[str, Any]] = []
        self.policies: list[dict] = []
        self.rule_owners: dict[tuple, dict] = {}
        self.active_records: dict[tuple[str, str], dict] = {}
        self.fixtures: list[dict] = []
        self.stats: dict = {}
        self.schema = SchemaChecker(self.root / "data/schemas")

    def require(self, condition: bool, code: str, location: str, message: str):
        self.checks[code] += 1
        if not condition:
            self.errors.append({"code": code, "location": location, "message": message})

    def data_path(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root / "data"):
            raise ValueError(f"数据清单路径越界：{relative}")
        return path

    def load(self, relative: str, schema_name: str) -> Any:
        data = read_json(self.data_path(relative))
        self.documents.append((relative, data))
        schema_path = self.root / "data/schemas" / schema_name
        for error in self.schema.check(data, self.schema.load(schema_path), schema_path):
            self.require(False, "schema.invalid", relative, error)
        return data

    def add(self, kind: str, record: dict, version: str | None = None):
        key = (kind, record["id"], version or record["version"])
        self.require(key not in self.index, "id.duplicate", record["id"], f"重复记录键 {key}")
        old_kind = self.identities.setdefault(record["id"], kind)
        self.require(old_kind == kind, "id.kind_changed", record["id"], "稳定 ID 不得跨实体类型复用")
        self.index[key] = record

    def lookup(self, kind: str, id: str, version: str | None = None) -> dict:
        if version is not None:
            return self.index[(kind, id, version)]
        if self.active_records:
            return self.active_records[(kind, id)]
        matches = [record for key, record in self.index.items() if key[:2] == (kind, id)]
        if len(matches) != 1:
            raise ValueError(f"{kind}/{id} 有多个版本，必须通过快照选择")
        return matches[0]

    def walk_refs(self, value: Any, location: str):
        if isinstance(value, dict):
            if set(value) == {"kind", "id", "version"}:
                key = ref_key(value)
                self.require(key in self.index, "reference.missing", location, f"引用不存在：{key}")
                if key[0] == "user_turn" and key in self.index:
                    self.require(self.index[key]["role"] == "user", "reference.non_user_turn", location, "用户证据不能引用预期助手输出")
            for name, child in value.items():
                self.walk_refs(child, f"{location}.{name}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                self.walk_refs(child, f"{location}[{index}]")

    def setup(self):
        self.manifest = self.load("data/manifest.json", "manifest.schema.json")
        if self.errors:
            return
        files = self.manifest["files"]
        declared = set(files["policies"] + files["fixtures"] + [files["faq"], files["business"], files["development"], files["system_constraints"], files["authorization_results"]])
        self.require(len(declared) == len(files["policies"]) + len(files["fixtures"]) + 5, "manifest.duplicate_path", "manifest", "数据文件路径不能重复")
        discovered = {path.relative_to(self.root).as_posix() for folder in ("data/knowledge", "data/business", "data/evaluation", "data/fixtures", "data/system")
                      for path in (self.root / folder).rglob("*") if path.is_file() and path.suffix in (".json", ".md")}
        self.require(discovered == declared, "manifest.unlisted_file", "manifest", f"清单与正式数据不一致：{sorted(discovered ^ declared)}")
        for relative in files["policies"]:
            text = self.data_path(relative).read_text(encoding="utf-8-sig")
            match = re.match(r"\s*<!--\s*policy-metadata\s*\n(.*?)\n\s*-->\s*\n", text, re.S)
            if not match:
                self.require(False, "policy.metadata_missing", relative, "政策必须包含开头 JSON 元数据注释块")
                continue
            policy = json.loads(match.group(1), object_pairs_hook=unique_object)
            schema_path = self.root / "data/schemas/policy.schema.json"
            for error in self.schema.check(policy, self.schema.load(schema_path), schema_path):
                self.require(False, "schema.invalid", relative, error)
            self.policies.append(policy)
            self.documents.append((relative, policy))
            if self.errors:
                continue
            self.add("policy", policy)
            rule_ids = [rule["id"] for rule in policy["rules"]]
            anchors = re.findall(r'<a id="([A-Z0-9-]+)"></a>', text[match.end():])
            self.require(Counter(anchors) == Counter(rule_ids), "policy.anchor_mismatch", relative, "正文条款锚点须与元数据规则 ID 一一对应")
            for rule in policy["rules"]:
                self.add("policy_rule", rule, policy["version"])
                self.rule_owners[("policy_rule", rule["id"], policy["version"])] = policy
        self.faq = self.load(files["faq"], "faq.schema.json")
        self.business = self.load(files["business"], "business.schema.json")
        self.development = self.load(files["development"], "development.schema.json")
        self.constraints = self.load(files["system_constraints"], "system-constraints.schema.json")
        self.authorization = self.load(files["authorization_results"], "authorization-results.schema.json")
        for relative in files["fixtures"]:
            self.fixtures.append(self.load(relative, "fixture.schema.json"))
        if self.errors:
            return
        for item in self.constraints["items"]:
            self.add("system_constraint", item)
        for item in self.authorization["items"]:
            self.add("authorization_result", item)
        for item in self.faq["items"]:
            self.add("faq", item)
        for collection, kind in COLLECTIONS.items():
            for record in self.business[collection]:
                self.add(kind, record)
        for fixture in self.fixtures:
            self.add("fixture", fixture)
        cases = self.development["cases"]
        case_ids = [case["id"] for case in cases]
        self.require(len(case_ids) == len(set(case_ids)), "id.duplicate_case", "development", "开发案例 ID 重复")
        for case in cases:
            for turn in case["turns"]:
                self.add("user_turn", turn)
        self.stats = {"policy_documents": len(self.policies), "policy_stable_ids": len({policy['id'] for policy in self.policies}),
                      "policy_rule_versions": sum(len(policy["rules"]) for policy in self.policies), "faq_items": len(self.faq["items"]),
                      "orders": len(self.business["orders"]), "order_lines": len(self.business["order_lines"]),
                      "snapshots": len(self.business["snapshots"]), "development_cases": len(cases),
                      "cases_per_group": dict(Counter(case["group_id"] for case in cases)), "isolated_fixtures": len(self.fixtures)}

    def interval(self, record: dict):
        start = timestamp(record["effective_from"])
        end = timestamp(record["effective_until"]) if record["effective_until"] else None
        self.require(end is None or end > start, "time.invalid_interval", record["id"], "生效区间必须非空、左闭右开")
        return start, end

    @staticmethod
    def overlap(a: tuple, b: tuple) -> bool:
        return (a[1] is None or b[0] < a[1]) and (b[1] is None or a[0] < b[1])

    def knowledge_checks(self):
        expected_refs = {("policy", policy["id"], policy["version"]) for policy in self.policies}
        self.require({ref_key(ref) for ref in self.manifest["knowledge_refs"]} == expected_refs,
                     "manifest.knowledge_versions", "manifest", "知识清单必须精确覆盖所有政策修订版本")
        for policy in self.policies:
            self.interval(policy)
            self.require(policy["shop_id"] == self.manifest["shop_id"], "scope.shop_mismatch", policy["id"], "店铺 ID 不一致")
            for rule in policy["rules"]:
                for condition in rule["conditions"]:
                    op, value = condition["op"], condition["value"]
                    self.require(op != "in" or isinstance(value, list) and bool(value), "condition.invalid", rule["id"], "in 条件值必须是非空数组")
                    self.require(op not in {"gte", "lte"} or type(value) is int, "condition.invalid", rule["id"], "数值条件值必须为整数")
                    self.require(op != "is_known" or type(value) is bool, "condition.invalid", rule["id"], "is_known 条件值必须是布尔值")
        for first, second in combinations(self.policies, 2):
            if first["id"] == second["id"]:
                self.require(first["selection_event"] == second["selection_event"], "policy.event_changed", first["id"], "同政策不能暗中切换版本选择事件")
                self.require(not self.overlap(self.interval(first), self.interval(second)), "policy.interval_overlap", first["id"], "正常政策修订的生效区间不能重叠")
        rules = [(policy, rule) for policy in self.policies for rule in policy["rules"]]
        for (pa, a), (pb, b) in combinations(rules, 2):
            if pa["shop_id"] != pb["shop_id"] or not set(pa["category_ids"]) & set(pb["category_ids"]):
                continue
            if pa["selection_event"] != pb["selection_event"] or not self.overlap(self.interval(pa), self.interval(pb)):
                continue
            disagreements = [key for key in set(a["effects"]) & set(b["effects"]) if a["effects"][key] != b["effects"][key]]
            if disagreements:
                self.require(not conditions_overlap(a["conditions"], b["conditions"]), "policy.conflict", f"{a['id']} / {b['id']}",
                             f"可同时适用的结构化规则存在冲突：{disagreements}")
        for item in self.faq["items"]:
            start, end = self.interval({"id": item["id"], **item["applicability"]})
            refs = [ref for ref in item["evidence_refs"] if ref["kind"] == "policy_rule"]
            self.require(bool(refs), "faq.no_policy_evidence", item["id"], "FAQ 必须关联具体政策条款")
            effects: dict[str, list[Any]] = {}
            supported_categories = set()
            bindings = {ref_key(binding["evidence_ref"]): binding for binding in item.get("evidence_time_bindings", [])}
            self.require(len(bindings) == len(item.get("evidence_time_bindings", [])) and set(bindings) <= {ref_key(ref) for ref in refs}, "faq.time_binding_invalid", item["id"], "时间绑定须唯一且只能指向该 FAQ 引用的条款")
            for ref in refs:
                rule = self.index[ref_key(ref)]
                policy = self.rule_owners[ref_key(ref)]
                pstart, pend = self.interval(policy)
                binding = bindings.get(ref_key(ref))
                if binding:
                    bstart, bend = self.interval({"id": item["id"], **binding})
                    self.require(binding["selection_event"] == policy["selection_event"] and bstart >= pstart and (pend is None or bend is not None and bend <= pend), "faq.time_binding_invalid", item["id"], "证据时间绑定必须保留该条款的选择事件和有效区间")
                else:
                    self.require(start >= pstart and (pend is None or end is not None and end <= pend), "faq.interval_mismatch", item["id"], "FAQ 生效区间超出引用政策版本")
                    self.require(item["applicability"]["selection_event"] == policy["selection_event"], "faq.event_mismatch", item["id"], "跨时间事件引用必须显式保存证据时间绑定")
                supported_categories |= set(policy["category_ids"])
                self.require(bool(set(item["applicability"]["category_ids"]) & set(policy["category_ids"])), "faq.category_mismatch", item["id"], "FAQ 引用了完全不适用的品类条款")
                for key, value in rule["effects"].items():
                    effects.setdefault(key, []).append(value)
            self.require(set(item["applicability"]["category_ids"]) <= supported_categories, "faq.category_mismatch", item["id"], "FAQ 品类未被引用条款覆盖")
            for key, value in item["answer_parameters"].items():
                self.require(key in effects and value in effects[key], "faq.parameter_mismatch", item["id"], f"FAQ 参数 {key} 无相同条款依据")

    def applicable_rule(self, id: str, event_at: str) -> tuple[dict, dict]:
        instant = timestamp(event_at)
        matches = []
        for key, policy in self.rule_owners.items():
            if key[1] != id:
                continue
            start, end = self.interval(policy)
            if start <= instant and (end is None or instant < end):
                matches.append((self.index[key], policy))
        if len(matches) != 1:
            raise ValueError(f"{id} 在 {event_at} 必须恰有一个有效版本")
        return matches[0]

    def snapshot_records(self, snapshot_ref: dict) -> dict[tuple[str, str], dict]:
        snapshot = self.index[ref_key(snapshot_ref)]
        return {(ref["kind"], ref["id"]): self.index[ref_key(ref)] for ref in snapshot["record_refs"]}

    def activate_record(self, kind: str, record: dict):
        reference = {"kind": kind, "id": record["id"], "version": record["version"]}
        snapshot = next(item for item in self.business["snapshots"] if reference in item["record_refs"])
        self.active_records = self.snapshot_records({"kind": "snapshot", "id": snapshot["id"], "version": snapshot["version"]})

    def selection_total(self, order: dict, selections: list[dict]) -> int:
        total, selected = 0, set()
        for selection in selections:
            line = self.lookup("order_line", selection["line_id"])
            if line["order_id"] != order["id"]:
                raise ValueError("退货行不属于指定订单")
            product = self.lookup("product", line["product_id"])
            if product["is_gift"]:
                raise ValueError("赠品不能作为付费商品退款单元")
            for unit in selection["unit_indices"]:
                if type(unit) is not int or not 1 <= unit <= line["quantity"]:
                    raise ValueError("退货单元序号越界")
                if (line["id"], unit) in selected:
                    raise ValueError("同一退货单元重复选择")
                selected.add((line["id"], unit))
                total += line["unit_paid_amounts_cents"][unit - 1]
        return total

    def estimate_annotation(self, check: dict) -> dict:
        # 仅核对开发标注的静态金额；不判定客服请求是否可退、不生成业务响应。
        order = self.index[ref_key(check["order_ref"])]
        selected_ids = {selection["line_id"] for selection in check["selections"]}
        gifts = [self.lookup("order_line", lid) for lid in order["line_ids"] if self.lookup("order_line", lid)["gift_for_line_id"] in selected_ids]
        returned_ids = check["gift_returned_line_ids"]
        if len(returned_ids) != len(set(returned_ids)):
            raise ValueError("返还赠品 ID 重复")
        for gift_id in returned_ids:
            gift = self.lookup("order_line", gift_id)
            if gift["order_id"] != order["id"] or gift["gift_for_line_id"] not in selected_ids:
                raise ValueError("返还赠品须是本订单所退主商品的赠品")
        if not {gift["id"] for gift in gifts} <= set(returned_ids):
            raise ValueError("赠品未退，不能将确定退款估算标为预期值")
        merchandise = self.selection_total(order, check["selections"])
        paid_lines = [self.lookup("order_line", lid) for lid in order["line_ids"] if not self.lookup("product", self.lookup("order_line", lid)["product_id"])["is_gift"]]
        selected_units = {(selection["line_id"], unit) for selection in check["selections"] for unit in selection["unit_indices"]}
        full_return = selected_units == {(line["id"], unit) for line in paid_lines for unit in range(1, line["quantity"] + 1)}
        shipping = 0
        if check["return_reason"] == "quality":
            if not all(self.lookup("order_line", lid)["conditions"]["quality_status"] == "confirmed" for lid in selected_ids):
                raise ValueError("未核实质量问题，不能给出已确认质量退款估算")
            rule, _ = self.applicable_rule("RULE-SHIP-005", order["purchased_at"])
            if full_return and rule["effects"].get("outbound_shipping_refundable") is True:
                shipping = order["outbound_shipping_paid_cents"]
        return {"merchandise_refund_cents": merchandise, "outbound_shipping_refund_cents": shipping, "total_estimated_cents": merchandise + shipping}

    def business_checks(self):
        self.check_all_business_relations()
        if self.errors:
            return
        shop = self.business["shop"]
        self.require(shop["id"] == self.manifest["shop_id"] == self.faq["shop_id"], "scope.shop_mismatch", "shop", "知识、业务和清单必须使用同一店铺")
        for collection in ("customers", "products", "campaigns", "orders", "snapshots"):
            for record in self.business[collection]:
                self.require(record["shop_id"] == shop["id"], "scope.shop_mismatch", record["id"], "记录不属于该店铺")
        for line in self.business["order_lines"]:
            self.activate_record("order_line", line)
            self.require(any(key[:2] == ("order", line["order_id"]) for key in self.index), "business.relation_missing", line["id"], "订单行指向不存在的订单")
            self.require(any(key[:2] == ("product", line["product_id"]) for key in self.index), "business.relation_missing", line["id"], "订单行商品不存在")
            self.require(line["item_discount_cents"] <= line["unit_price_cents"] * line["quantity"], "money.discount_exceeds_gross", line["id"], "商品优惠超过原价")
            self.require(line["paid_cents"] == line["unit_price_cents"] * line["quantity"] - line["item_discount_cents"] - line["coupon_allocated_cents"], "money.line_total", line["id"], "订单行实付不等于原价减优惠")
            net, quantity = line["paid_cents"], line["quantity"]
            expected_units = [net // quantity + (index < net % quantity) for index in range(quantity)]
            self.require(line["unit_paid_amounts_cents"] == expected_units, "money.unit_split", line["id"], "单元份额必须前序单元优先分配余分并保持总额")
            if any(key[:2] == ("product", line["product_id"]) for key in self.index):
                product = self.lookup("product", line["product_id"])
                required = {"apparel_footwear": {"tags_intact", "product_intact", "used_outdoors", "packaging_intact", "quality_status"},
                            "digital_accessories": {"opened", "activated", "product_intact", "accessories_complete", "quality_status"},
                            "customized": {"production_started", "product_intact", "quality_status"}}[product["category_id"]]
                self.require(required <= set(line["conditions"]), "business.conditions_missing", line["id"], "该品类的关键条件缺失")
                if product["is_gift"]:
                    self.require(line["paid_cents"] == line["unit_price_cents"] == line["coupon_allocated_cents"] == line["item_discount_cents"] == 0,
                                 "money.gift_nonzero", line["id"], "赠品原价和冻结实付须为零，不能按宣传价退款")
                    target = self.active_records.get(("order_line", line["gift_for_line_id"]))
                    self.require(target is not None and target["order_id"] == line["order_id"] and target["gift_for_line_id"] is None,
                                 "business.gift_relation", line["id"], "赠品必须关联同订单的非赠品行")
                else:
                    self.require(line["gift_for_line_id"] is None, "business.gift_relation", line["id"], "非赠品不能标记赠品关联")
        if self.errors:
            return
        for order in self.business["orders"]:
            self.activate_record("order", order)
            self.require(any(key[:2] == ("customer", order["customer_id"]) for key in self.index), "business.relation_missing", order["id"], "客户不存在")
            attached = [record for (kind, _), record in self.active_records.items() if kind == "order_line" and record["order_id"] == order["id"]]
            self.require(set(order["line_ids"]) == {line["id"] for line in attached}, "business.line_relation", order["id"], "订单与订单行关联不完整")
            fields = {"merchandise_gross_cents": sum(line["unit_price_cents"] * line["quantity"] for line in attached),
                      "item_discount_total_cents": sum(line["item_discount_cents"] for line in attached),
                      "coupon_total_cents": sum(line["coupon_allocated_cents"] for line in attached), "merchandise_paid_cents": sum(line["paid_cents"] for line in attached)}
            for key, expected in fields.items():
                self.require(order[key] == expected, "money.order_total", order["id"], f"{key} 与订单行不一致")
            weights = {line["id"]: line["unit_price_cents"] * line["quantity"] - line["item_discount_cents"] for line in attached}
            denominator, coupon = sum(weights.values()), order["coupon_total_cents"]
            self.require(coupon <= denominator, "money.coupon_exceeds_weight", order["id"], "订单券超过可分摊金额")
            allocations = {id: coupon * weight // denominator if denominator else 0 for id, weight in weights.items()}
            sorted_ids = sorted(weights, key=lambda id: (-(coupon * weights[id] % denominator if denominator else 0), id))
            for id in sorted_ids[:coupon - sum(allocations.values())]:
                allocations[id] += 1
            self.require(all(line["coupon_allocated_cents"] == allocations[line["id"]] for line in attached), "money.coupon_allocation", order["id"], "优惠券分摊不符合最大余数法与稳定 ID 排序")
            rule, policy = self.applicable_rule("RULE-SHIP-001", order["purchased_at"])
            expected_shipping = 0 if order["merchandise_paid_cents"] >= rule["effects"]["free_shipping_threshold_cents"] else rule["effects"]["standard_shipping_cents"]
            self.require(order["outbound_shipping_paid_cents"] == expected_shipping, "money.shipping", order["id"], "购买时政策与折后包邮门槛不一致")
            shipping_ref = {"kind": "policy_rule", "id": "RULE-SHIP-001", "version": policy["version"]}
            self.require(shipping_ref in order["evidence_refs"], "business.policy_version", order["id"], "订单运费依据未固定到购买时有效版本")
            self.require(order["total_paid_cents"] == order["merchandise_paid_cents"] + order["outbound_shipping_paid_cents"], "money.order_total", order["id"], "订单总额不一致")
            payment = self.active_records.get(("payment", order["payment_id"]))
            self.require(payment is not None and payment["order_id"] == order["id"] and payment["amount_cents"] == order["total_paid_cents"], "money.payment", order["id"], "支付归属或金额不一致")
            for campaign_id in order["campaign_ids"]:
                campaign = self.active_records.get(("campaign", campaign_id))
                self.require(campaign is not None, "business.relation_missing", order["id"], "活动不存在")
                if campaign:
                    start, end = self.interval(campaign)
                    purchase = timestamp(order["purchased_at"])
                    self.require(start <= purchase and (end is None or purchase < end), "business.campaign_time", order["id"], "购买时活动未生效")
            self.require(timestamp(order["purchased_at"]) <= timestamp(order["observed_at"]), "time.event_after_observation", order["id"], "购买时间晚于观察时间")
        for item in self.business["logistics"]:
            self.activate_record("logistics", item)
            order = self.active_records.get(("order", item["order_id"]))
            self.require(order is not None, "business.relation_missing", item["id"], "物流订单不存在")
            signed = item["signed_at"]
            self.require((item["status"] == "signed") == (signed is not None), "business.logistics_state", item["id"], "物流状态与签收时间不一致")
            if order:
                self.require((order["status"] == "delivered") == (signed is not None), "business.logistics_state", item["id"], "订单状态与物流状态不一致")
                self.require(timestamp(order["purchased_at"]) <= timestamp(item["shipped_at"]) <= timestamp(item["observed_at"]), "time.logistics", item["id"], "物流时间顺序错误")
            if signed:
                self.require(timestamp(item["shipped_at"]) <= timestamp(signed) <= timestamp(item["observed_at"]), "time.logistics", item["id"], "签收时间顺序错误")
        for snapshot in self.business["snapshots"]:
            seen = set()
            for ref in snapshot["record_refs"]:
                identity = (ref["kind"], ref["id"])
                self.require(identity not in seen, "snapshot.multiple_versions", snapshot["id"], "一个快照内同实体只能有一个修订版本")
                seen.add(identity)
                record = self.index[ref_key(ref)]
                if "observed_at" in record:
                    self.require(timestamp(record["observed_at"]) <= timestamp(snapshot["captured_at"]), "snapshot.future_record", snapshot["id"], "快照含未来观察记录")
            self.require(snapshot["knowledge_version"] == self.manifest["knowledge_version"], "snapshot.knowledge_version", snapshot["id"], "快照知识版本不在本数据包")
            self.check_snapshot_relations(snapshot)
        self.active_records = {}
        expected_snapshot_refs = {("snapshot", item["id"], item["version"]) for item in self.business["snapshots"]}
        self.require({ref_key(ref) for ref in self.manifest["snapshot_refs"]} == expected_snapshot_refs, "manifest.snapshot_versions", "manifest", "清单快照不完整")

    def check_all_business_relations(self):
        identities = {(kind, id) for kind, id, _ in self.index}
        for collection, kind in COLLECTIONS.items():
            for record in self.business[collection]:
                relations = []
                if kind == "order":
                    relations += [("customer", record["customer_id"]), ("payment", record["payment_id"])]
                    relations += [("order_line", id) for id in record["line_ids"]] + [("campaign", id) for id in record["campaign_ids"]]
                elif kind == "order_line":
                    relations += [("order", record["order_id"]), ("product", record["product_id"])]
                    if record["gift_for_line_id"]:
                        relations += [("order_line", record["gift_for_line_id"])]
                elif kind in {"payment", "logistics"}:
                    relations += [("order", record["order_id"])]
                elif kind == "after_sale":
                    relations += [("order", record["order_id"])] + [("order_line", selection["line_id"]) for selection in record["selections"]]
                    relations += [("order_line", id) for id in record["gift_returned_line_ids"]] + [("refund", id) for id in record["refund_ids"]]
                elif kind == "refund":
                    relations += [("order", record["order_id"]), ("after_sale", record["after_sale_id"])]
                for relation in relations:
                    self.require(relation in identities, "business.relation_missing", record["id"], f"不存在的业务关联：{relation}")
        snapshot_keys = {ref_key(ref) for snapshot in self.business["snapshots"] for ref in snapshot["record_refs"]}
        for collection, kind in COLLECTIONS.items():
            if kind == "snapshot":
                continue
            for record in self.business[collection]:
                self.require((kind, record["id"], record["version"]) in snapshot_keys, "snapshot.unused_record", record["id"], "种子记录版本须属于至少一个完整快照，不能留下未校验的孤立修订")

    def check_snapshot_relations(self, snapshot: dict):
        records = self.snapshot_records({"kind": "snapshot", "id": snapshot["id"], "version": snapshot["version"]})
        def target(kind: str, id: str):
            result = records.get((kind, id))
            self.require(result is not None, "snapshot.relation_missing", snapshot["id"], f"快照缺少关联 {kind}/{id}")
            return result
        self.active_records = records
        selected_units = set()
        for (kind, _), record in records.items():
            for ref in record.get("evidence_refs", []):
                if ref["kind"] in COLLECTIONS.values() and ref["kind"] != "snapshot":
                    selected = records.get((ref["kind"], ref["id"]))
                    self.require(selected is not None and selected["version"] == ref["version"], "snapshot.evidence_version", record["id"], "业务证据引用必须指向本快照选择的记录版本")
            if kind == "order":
                target("customer", record["customer_id"])
                payment = target("payment", record["payment_id"])
                if payment:
                    self.require(payment["order_id"] == record["id"] and payment["amount_cents"] == record["total_paid_cents"], "money.payment", payment["id"], "快照内支付金额或订单归属不一致")
                for campaign_id in record["campaign_ids"]:
                    target("campaign", campaign_id)
                attached = [target("order_line", lid) for lid in record["line_ids"]]
                if all(attached):
                    self.require(all(line["order_id"] == record["id"] for line in attached), "business.line_relation", record["id"], "快照内订单行归属不一致")
                    self.require(record["merchandise_paid_cents"] == sum(line["paid_cents"] for line in attached)
                                 and record["coupon_total_cents"] == sum(line["coupon_allocated_cents"] for line in attached)
                                 and record["merchandise_gross_cents"] == sum(line["unit_price_cents"] * line["quantity"] for line in attached)
                                 and record["item_discount_total_cents"] == sum(line["item_discount_cents"] for line in attached)
                                 and record["total_paid_cents"] == record["merchandise_paid_cents"] + record["outbound_shipping_paid_cents"],
                                 "money.snapshot_order_total", record["id"], "快照所选订单行版本与订单冻结金额不一致")
            elif kind == "order_line":
                target("order", record["order_id"])
                target("product", record["product_id"])
                if record["gift_for_line_id"]:
                    target("order_line", record["gift_for_line_id"])
            elif kind in {"payment", "logistics"}:
                order = target("order", record["order_id"])
                if order and kind == "payment":
                    self.require(timestamp(order["purchased_at"]) <= timestamp(record["paid_at"]) <= timestamp(record["observed_at"]), "time.payment", record["id"], "支付事件时间必须在购买与观察时刻之间")
            elif kind == "after_sale":
                order = target("order", record["order_id"])
                if not order:
                    continue
                try:
                    goods = self.selection_total(order, record["selections"])
                except (KeyError, ValueError) as error:
                    self.require(False, "business.return_selection", record["id"], str(error))
                    continue
                for selection in record["selections"]:
                    for unit in selection["unit_indices"]:
                        key = (order["id"], selection["line_id"], unit)
                        self.require(key not in selected_units, "business.duplicate_return_unit", record["id"], "同快照退货单元重复占用")
                        selected_units.add(key)
                for gift_id in record["gift_returned_line_ids"]:
                    gift = target("order_line", gift_id)
                    self.require(gift is not None and gift["order_id"] == order["id"] and gift["gift_for_line_id"] is not None, "business.gift_relation", record["id"], "返还赠品关联不正确")
                    if gift and "gift_returned" in gift["conditions"]:
                        self.require(gift["conditions"]["gift_returned"] is True, "business.gift_return_state", record["id"], "售后返还列表与赠品状态不一致")
                linked_refunds = []
                for rid in record["refund_ids"]:
                    refund = target("refund", rid)
                    self.require(refund is not None and refund["after_sale_id"] == record["id"], "business.refund_relation", record["id"], "售后退款双向关联不一致")
                    if refund:
                        linked_refunds.append(refund)
                if record["approved_cents"] is not None:
                    self.require(sum(refund["approved_cents"] for refund in linked_refunds) <= record["approved_cents"]
                                 and sum(refund["settled_cents"] or 0 for refund in linked_refunds) <= record["approved_cents"],
                                 "money.refund_accumulated", record["id"], "同售后累计发起或到账金额不得超过审批金额")
                self.require(timestamp(order["purchased_at"]) <= timestamp(record["applied_at"]) <= timestamp(record["observed_at"]), "time.after_sale", record["id"], "售后申请时间顺序错误")
                approved = record["approved_cents"]
                if approved is not None:
                    self.require(record["status"] in {"refund_processing", "refund_completed"} and bool(record["refund_ids"]), "business.approved_state", record["id"], "本种子契约中已审批售后必须有对应退款记录，不能仍标待审批或凭空标完成")
                    selected_lines = [self.lookup("order_line", selection["line_id"]) for selection in record["selections"]]
                    if record["return_reason"] == "no_reason":
                        for line in selected_lines:
                            category = self.lookup("product", line["product_id"])["category_id"]
                            conditions = line["conditions"]
                            valid = category != "customized"
                            if category == "apparel_footwear":
                                valid = all(conditions.get(field) is value for field, value in {"tags_intact": True, "product_intact": True, "used_outdoors": False, "packaging_intact": True}.items())
                            elif category == "digital_accessories":
                                valid = all(conditions.get(field) is value for field, value in {"activated": False, "product_intact": True, "accessories_complete": True}.items())
                            self.require(valid, "business.approval_conditions", record["id"], "已审批的无理由售后与冻结品类条件不一致，裁量结果需另行建模")
                    else:
                        self.require(record["quality_status"] == "confirmed" and all(line["conditions"]["quality_status"] == "confirmed" for line in selected_lines), "business.approval_conditions", record["id"], "质量退款审批必须有已核实质量证据")
                    self.require(record["approved_at"] is not None and timestamp(record["applied_at"]) <= timestamp(record["approved_at"]) <= timestamp(record["observed_at"]), "time.approval", record["id"], "审批时间顺序错误")
                    check = {"order_ref": {"kind": "order", "id": order["id"], "version": order["version"]}, "return_reason": record["return_reason"],
                             "selections": record["selections"], "gift_returned_line_ids": record["gift_returned_line_ids"]}
                    try:
                        amounts = self.estimate_annotation(check)
                        self.require(goods == record["approved_merchandise_cents"] and amounts["outbound_shipping_refund_cents"] == record["approved_outbound_shipping_cents"]
                                     and approved == amounts["total_estimated_cents"], "money.approval", record["id"], "审批金额与冻结份额及原运费规则不一致")
                    except ValueError as error:
                        self.require(False, "money.approval", record["id"], str(error))
                    logistics = [item for (k, _), item in records.items() if k == "logistics" and item["order_id"] == order["id"]]
                    if logistics and logistics[0]["signed_at"]:
                        window_id = "RULE-GEN-001" if record["return_reason"] == "no_reason" else "RULE-GEN-002"
                        rule, _ = self.applicable_rule(window_id, order["purchased_at"])
                        days = rule["effects"]["no_reason_window_days" if record["return_reason"] == "no_reason" else "quality_window_days"]
                        self.require(timestamp(logistics[0]["signed_at"]) <= timestamp(record["applied_at"]) <= timestamp(logistics[0]["signed_at"]) + timedelta(days=days),
                                     "business.return_window", record["id"], "已审批售后不在规定的签收后申请窗口")
                    else:
                        self.require(False, "business.return_window", record["id"], "已审批售后缺少签收证据")
                else:
                    self.require(record["approved_at"] is None and record["approved_merchandise_cents"] is None and record["approved_outbound_shipping_cents"] is None
                                 and not record["refund_ids"] and record["status"] in {"pending_review", "pending_verification"}, "business.unapproved_state", record["id"], "待审批不能有已审批金额或退款记录")
            elif kind == "refund":
                sale = target("after_sale", record["after_sale_id"])
                order = target("order", record["order_id"])
                if not sale or not order:
                    continue
                self.require(record["order_id"] == sale["order_id"] and record["id"] in sale["refund_ids"] and record["approved_cents"] == sale["approved_cents"],
                             "business.refund_relation", record["id"], "退款归属或审批金额不一致")
                self.require(sale["approved_at"] is not None and timestamp(sale["approved_at"]) <= timestamp(record["initiated_at"]) <= timestamp(record["observed_at"]), "time.refund", record["id"], "退款发起时间顺序错误")
                if record["status"] == "settled":
                    self.require(record["settled_at"] is not None and record["settled_cents"] == record["approved_cents"] and sale["status"] == "refund_completed", "business.settled_state", record["id"], "到账状态、金额与售后状态不一致")
                    if record["settled_at"]:
                        self.require(timestamp(record["initiated_at"]) <= timestamp(record["settled_at"]) <= timestamp(record["observed_at"]), "time.refund", record["id"], "到账时间顺序错误")
                else:
                    self.require(record["settled_at"] is None and record["settled_cents"] is None and sale["status"] == "refund_processing", "business.processing_state", record["id"], "未到账不得填到账金额、时间或完成状态")

    def case_checks(self):
        cases = self.development["cases"]
        self.require(set(self.development["group_ids"]) == GROUPS, "development.groups", "development", "分组必须对应设计六组")
        self.require(Counter(case["group_id"] for case in cases) == Counter({group: 5 for group in GROUPS}), "development.balance", "development", "仅创建六组各 5 个，共 30 个开发案例")
        for case in cases:
            cid = case["id"]
            snapshot = self.index[ref_key(case["business_snapshot_ref"])]
            self.require(timestamp(snapshot["captured_at"]) <= timestamp(case["request_context"]["evaluated_at"]), "development.future_snapshot", cid, "案例初始快照不能晚于请求时间")
            self.require(case["request_context"]["shop_id"] == self.manifest["shop_id"], "scope.shop_mismatch", cid, "案例店铺不一致")
            self.require(("customer", case["request_context"]["customer_id"], "1.0.0") in self.index, "business.relation_missing", cid, "案例可信客户不存在")
            evidence_ids = [group["id"] for group in case["required_evidence"]]
            self.require(len(evidence_ids) == len(set(evidence_ids)), "development.local_id_duplicate", cid, "证据组 ID 重复")
            conclusions = [item["id"] for item in case["expected_conclusions"]]
            self.require(len(conclusions) == len(set(conclusions)), "development.local_id_duplicate", cid, "结论 ID 重复")
            seen_turns = set()
            last_at = None
            case_records = self.snapshot_records(case["business_snapshot_ref"])
            for turn in case["turns"]:
                at = timestamp(turn["at"])
                self.require(last_at is None or last_at <= at, "development.turn_time", cid, "对话时间逆序")
                last_at = at
                self.require((turn["role"] == "user" and turn["expected_action"] is None) or
                             (turn["role"] == "expected_assistant" and turn["expected_action"] in case["allowed_actions"]), "development.turn_action", turn["id"], "用户不能携带助手动作；标注动作须在允许列表")
                for ref in turn["invalidates"]:
                    self.require(ref_key(ref) in seen_turns, "development.invalidates", turn["id"], "修正只能使本案例先前的用户事实失效")
                    source = self.index.get(ref_key(ref), {})
                    fields = turn.get("invalidated_fields", [])
                    self.require(turn["role"] == "user" and bool(fields) and set(fields) <= set(source.get("facts", {}))
                                 and set(fields) <= set(turn["facts"]), "development.invalidated_fields", turn["id"], "修正必须明确先前已提供、当前重新提供的字段；其余事实不整轮删除")
                seen_turns.add(("user_turn", turn["id"], turn["version"]))
                if "business_snapshot_ref" in turn:
                    override = self.index[ref_key(turn["business_snapshot_ref"])]
                    self.require(timestamp(override["captured_at"]) <= at, "development.future_snapshot", turn["id"], "刷新快照不能晚于该轮对话")
                if turn["role"] == "user" and "order_id" in turn["facts"]:
                    order_id = turn["facts"]["order_id"]
                    self.require(("order", order_id) in case_records, "development.order_missing", turn["id"], "用户指向快照外不存在的订单")
                    if ("order", order_id) in case_records and case_records[("order", order_id)]["customer_id"] != case["request_context"]["customer_id"]:
                        self.require("refuse_unauthorized" in case["allowed_actions"], "development.authorization", cid, "跨客户订单案例必须标注拒绝越权动作")
            fixture_keys = {ref_key(ref) for ref in case["fixture_refs"]}
            for group in case["required_evidence"]:
                for ref in group["any_of"]:
                    key = ref_key(ref)
                    if ref["kind"] == "user_turn":
                        self.require(key in seen_turns, "development.evidence_turn", cid, "不能引用其他案例的用户事实")
                    if ref["kind"] == "fixture":
                        self.require(key in fixture_keys, "development.fixture_unlisted", cid, "必要夹具没有在 fixture_refs 声明")
            self.revision_case_checks(case)
            for conclusion in case["expected_conclusions"]:
                applied_index = next((i for i, turn in enumerate(case["turns"]) if turn["id"] == conclusion["applies_after_turn_id"]), None)
                if applied_index is None:
                    continue
                current_snapshot = self.case_snapshot_at(case, applied_index)
                available_refs = {ref_key(ref) for ref in current_snapshot["record_refs"]}
                self.require(set(conclusion["evidence_group_ids"]) <= set(evidence_ids), "development.conclusion_evidence", cid, "结论引用了不存在的证据组")
                checks = conclusion.get("value_checks", [])
                expected_values = conclusion["expected_values"]
                check_keys = [check["key"] for check in checks]
                self.require(len(check_keys) == len(set(check_keys)), "development.value_check_duplicate", cid, "同结论数值检查键重复")
                self.require(MONEY_KEYS & set(expected_values) <= set(check_keys), "development.money_without_check", cid, "金额标注缺少可核验源绑定")
                for check in checks:
                    self.require(check["key"] in expected_values, "development.value_key_missing", cid, "数值检查键不在预期值中")
                    if check["key"] not in expected_values:
                        continue
                    try:
                        if check["kind"] == "record_field":
                            key = ref_key(check["source_ref"])
                            self.require(key in available_refs or key[0] in {"policy_rule", "policy", "faq"}, "development.value_snapshot", cid, "数值核验源不在当前或刷新快照")
                            if check["key"] in MONEY_KEYS:
                                valid_sources = {"approved_cents": {("after_sale", "approved_cents"), ("refund", "approved_cents")},
                                                 "settled_cents": {("refund", "settled_cents")},
                                                 "merchandise_refund_cents": {("after_sale", "approved_merchandise_cents")},
                                                 "outbound_shipping_refund_cents": {("after_sale", "approved_outbound_shipping_cents")},
                                                 "total_estimated_cents": set()}
                                self.require((key[0], check["source_field"]) in valid_sources[check["key"]], "development.value_semantics", cid, "审批、到账和估算金额不可通过任意字段绑定互相替代")
                            actual = self.index[key][check["source_field"]]
                        else:
                            order_key = ref_key(check["order_ref"])
                            self.require(order_key[0] == "order" and order_key in available_refs, "development.value_snapshot", cid, "估算核验订单必须属于案例快照")
                            if order_key not in available_refs:
                                raise ValueError("找不到估算订单对应的案例快照")
                            self.active_records = self.snapshot_records({"kind": "snapshot", "id": current_snapshot["id"], "version": current_snapshot["version"]})
                            actual = self.estimate_annotation(check)[check["key"]]
                        self.require(actual == expected_values[check["key"]], "development.value_mismatch", cid, f"{check['key']} 标注与源数据不一致：期望 {expected_values[check['key']]!r}，源为 {actual!r}")
                    except (ValueError, KeyError, IndexError) as error:
                        self.require(False, "development.value_invalid", cid, str(error))
            steps = case["hop_dependencies"]
            seen_steps = {}
            for step in steps:
                self.require(step["id"] not in seen_steps, "development.local_id_duplicate", cid, "多跳步骤 ID 重复")
                self.require(set(step["depends_on"]) <= set(seen_steps), "development.hop_order", cid, "多跳依赖缺失或存在循环/逆序")
                for binding in step["input_bindings"]:
                    source = seen_steps.get(binding["source_step_id"])
                    self.require(source is not None and source["id"] in step["depends_on"] and binding["source_field"] in source["output_fields"],
                                 "development.hop_binding", cid, "后续输入必须来自声明依赖的前序输出字段")
                self.require(step["operation"] in case["allowed_actions"], "development.hop_action", cid, "步骤动作未获标注允许")
                seen_steps[step["id"]] = step
            if case["group_id"] == "dependent_multihop":
                self.require(len(steps) >= 2 and any(step["depends_on"] and step["input_bindings"] for step in steps), "development.not_multihop", cid, "依赖式多跳必须有先查业务再绑定后续输入的真实依赖")
            if case["group_id"] == "multi_document":
                self.require(not steps, "development.mislabeled_multidoc", cid, "多文档综合不标为业务依赖多跳")
            if case["group_id"] == "multi_turn":
                self.require(sum(turn["role"] == "user" for turn in case["turns"]) >= 2, "development.not_multiturn", cid, "多轮案例必须包含至少两次用户输入")

    def case_snapshot_at(self, case: dict, turn_index: int) -> dict:
        """只选择该轮之前显式启用的快照，不合并后续轮次记录。"""
        reference = case["business_snapshot_ref"]
        for turn in case["turns"][:turn_index + 1]:
            reference = turn.get("business_snapshot_ref", reference)
        return self.index[ref_key(reference)]

    def revision_case_checks(self, case: dict):
        cid = case["id"]
        positions = {turn["id"]: i for i, turn in enumerate(case["turns"])}
        users = {turn["id"] for turn in case["turns"] if turn["role"] == "user"}
        groups = {group["id"]: group for group in case["required_evidence"]}
        for group in groups.values():
            self.require(group["available_from_turn_id"] in users, "development.evidence_availability", cid, "证据可用时点必须是本案例用户轮")
            if group["available_from_turn_id"] not in users:
                continue
            available_index = positions[group["available_from_turn_id"]]
            available_at = timestamp(case["turns"][available_index]["at"])
            current_snapshot = self.case_snapshot_at(case, available_index)
            snapshot_keys = {ref_key(ref) for ref in current_snapshot["record_refs"]}
            self.require(timestamp(current_snapshot["captured_at"]) <= available_at,
                         "development.future_evidence", cid, "证据可用轮次不能使用尚未捕获的快照")
            for reference in group["any_of"]:
                record = self.index[ref_key(reference)]
                kind = reference["kind"]
                if kind == "user_turn":
                    source_index = positions.get(record["id"])
                    self.require(source_index is not None and record["role"] == "user"
                                 and source_index <= available_index and timestamp(record["at"]) <= available_at,
                                 "development.future_evidence", cid, "证据可用轮次不能早于实际用户输入")
                elif kind == "snapshot":
                    enabled_snapshots = {ref_key(case["business_snapshot_ref"])} | {
                        ref_key(turn["business_snapshot_ref"]) for turn in case["turns"][:available_index + 1]
                        if "business_snapshot_ref" in turn
                    }
                    self.require(ref_key(reference) in enabled_snapshots and timestamp(record["captured_at"]) <= available_at,
                                 "development.future_evidence", cid, "快照证据须已在该轮启用且完成捕获")
                elif kind in COLLECTIONS.values():
                    self.require(ref_key(reference) in snapshot_keys, "development.evidence_snapshot", cid,
                                 "业务证据精确版本须属于其可用轮次的快照")
                if "observed_at" in record:
                    self.require(timestamp(record["observed_at"]) <= available_at,
                                 "development.future_evidence", cid, "证据观察时间不能晚于可用轮次")
                if reference["kind"] in {"order", "order_line", "payment", "logistics", "after_sale", "refund"}:
                    order_id = record["id"] if reference["kind"] == "order" else record["order_id"]
                    order = self.snapshot_records({"kind": "snapshot", "id": current_snapshot["id"], "version": current_snapshot["version"]}).get(("order", order_id))
                    if order and order["customer_id"] != case["request_context"]["customer_id"]:
                        self.require(group["visibility"] == "evaluator_only", "development.unauthorized_evidence", cid, "他人业务证据只能由评测后台使用，不得向助手提供")
        merged = {}
        for turn in case["turns"]:
            if turn["role"] != "user":
                continue
            if turn["facts"].get("reset_order_scoped_facts"):
                merged = {}
            merged.update(turn["facts"])
            self.require(not (merged.get("gift_will_be_returned") is True and merged.get("hypothetical_gift_not_returned") is True), "development.stale_hypothesis", turn["id"], "返赠品更正后不能仍保留不返还假设")
            for field in ("purchased_at", "signed_at", "after_sales_applied_at", "comparison_applied_at"):
                if field in turn["facts"]:
                    self.require(turn["facts"][field] in turn["text"], "development.unsourced_time", turn["id"], "精确时间事实必须在用户文本中明确给出，不从日期猜时分秒")
        for conclusion in case["expected_conclusions"]:
            after = conclusion["applies_after_turn_id"]
            self.require(after in users, "development.conclusion_turn", cid, "结论必须绑定本案例用户轮")
            if after not in users:
                continue
            assistant_ids = set(conclusion["assistant_evidence_group_ids"])
            self.require(assistant_ids <= set(conclusion["evidence_group_ids"]) <= set(groups), "development.assistant_evidence", cid, "助手支持组必须属于结论支持组")
            for gid in conclusion["evidence_group_ids"]:
                if gid not in groups:
                    continue
                group = groups[gid]
                self.require(group["available_from_turn_id"] in positions and positions[group["available_from_turn_id"]] <= positions[after], "development.future_evidence", cid, "结论不能引用后续轮次才可用的证据")
                if gid in assistant_ids:
                    self.require(group["visibility"] == "assistant", "development.evaluator_leak", cid, "后台评分证据不可当作助手结论依据")
        current_user = None
        for turn in case["turns"]:
            if turn["role"] == "user":
                current_user = turn["id"]
            else:
                self.require(any(c["applies_after_turn_id"] == current_user for c in case["expected_conclusions"]), "development.unscored_turn", cid, "每个预期助手轮均需独立结论标注，不能只评分末轮")
        for group in groups.values():
            for reference in group["any_of"]:
                if reference["kind"] == "authorization_result":
                    result = self.index[ref_key(reference)]
                    order = self.snapshot_records(case["business_snapshot_ref"]).get(("order", result["requested_order_id"]))
                    self.require(result["customer_id"] == case["request_context"]["customer_id"] and result["shop_id"] == case["request_context"]["shop_id"]
                                 and order is not None and order["customer_id"] != result["customer_id"], "development.authorization_result", cid, "静态拒绝结果须与可信上下文和后台归属一致")
        if cid == "DEV-024":
            amounts = {(c["applies_after_turn_id"], c["expected_values"].get("total_estimated_cents")) for c in case["expected_conclusions"]}
            self.require({("DEV-024-U1", 15819), ("DEV-024-U2", 12900)} <= amounts, "development.round_amount_coverage", cid, "换单案例首末轮金额必须分别核验")

    def fixture_checks(self):
        conflicts = 0
        for fixture in self.fixtures:
            kind = fixture["fixture_kind"]
            operation = fixture["operation"]
            expected = fixture["expected_validation_errors"]
            if kind == "knowledge_conflict":
                conflicts += 1
                key = (operation.get("target_kind"), operation.get("id"), operation.get("version"))
                self.require(key in self.index and key[0] == "policy_rule", "fixture.invalid_target", fixture["id"], "冲突夹具目标规则不存在")
                changes = operation.get("changes", {})
                original = self.index.get(key, {}).get("effects", {})
                conflict = bool(changes) and all(field in original for field in changes) and any(original[field] != value for field, value in changes.items())
                self.require(conflict and expected == ["fixture.policy_conflict"], "fixture.no_expected_conflict", fixture["id"], "故意冲突必须改变同一条款参数并声明预期错误")
            elif kind == "prompt_injection":
                self.require(isinstance(operation.get("untrusted_text"), str) and bool(operation["untrusted_text"]) and not expected, "fixture.invalid_injection", fixture["id"], "注入必须是标记的不可信文本，不能假称结构无效")
            elif kind == "tool_timeout":
                self.require(operation.get("tool") in {"get_order", "get_after_sales", "get_logistics"} and operation.get("status") == "temporary_failure"
                             and operation.get("error_code") == "TIMEOUT" and not expected, "fixture.invalid_timeout", fixture["id"], "超时夹具必须保留失败而非业务数据")
                self.require(("order", operation.get("order_id"), "1.0.0") in self.index, "fixture.invalid_target", fixture["id"], "超时目标订单不存在")
        self.require(conflicts >= 1, "fixture.conflict_missing", "fixtures", "必须有独立标记的冲突夹具")
        normal_documents = [(location, value) for location, value in self.documents if not location.startswith("data/fixtures/") and location != "data/manifest.json"]
        def find_fixture_refs(value):
            if isinstance(value, dict):
                if value.get("kind") == "fixture" and set(value) == {"kind", "id", "version"}:
                    return [value]
                return [ref for child in value.values() for ref in find_fixture_refs(child)]
            if isinstance(value, list):
                return [ref for child in value for ref in find_fixture_refs(child)]
            return []
        for location, value in normal_documents:
            if location != self.manifest["files"]["development"]:
                self.require(not find_fixture_refs(value), "fixture.contaminated_normal_data", location, "正常政策、FAQ 和业务种子不得引用故障/冲突夹具")
        self.stats["expected_conflict_fixture_checks"] = conflicts

    def review_checks(self):
        """审核状态来自人工记录；程序通过不会自动改变审核状态。"""
        status = self.manifest["review_status"]
        record = self.manifest.get("review_record")
        if status == "approved":
            self.require(record is not None, "review.approval_record_missing", "manifest", "审核通过必须有人工确认记录")
        if record is not None:
            self.require(status == "approved" and record["knowledge_version"] == self.manifest["knowledge_version"],
                         "review.approval_record_mismatch", "manifest", "人工确认记录须对应已通过状态及当前知识集合")

        def check_status(value, location):
            if isinstance(value, dict):
                if "review_status" in value:
                    self.require(value["review_status"] == status, "review.status_mismatch", location,
                                 "数据包及其受审内容的审核状态须一致")
                for key, child in value.items():
                    check_status(child, f"{location}.{key}")
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    check_status(child, f"{location}[{index}]")

        check_status(self.manifest, "manifest")
        for location, value in self.documents:
            check_status(value, location)

    def run(self) -> dict:
        try:
            self.setup()
            if not self.errors:
                for location, value in self.documents:
                    self.walk_refs(value, location)
            if not self.errors:
                self.review_checks()
                self.knowledge_checks()
                self.business_checks()
                self.case_checks()
                self.fixture_checks()
        except (OSError, ValueError, KeyError, TypeError, IndexError, OverflowError, json.JSONDecodeError) as error:
            self.require(False, "validation.input_error", "input", str(error))
        return {"validator_version": "1.1.2", "schema_version": "1.1.1", "result": "pass" if not self.errors else "fail",
                "scope": "stage1_offline_data_validation", "human_review_status": getattr(self, "manifest", {}).get("review_status", "unknown"),
                "human_review_record": getattr(self, "manifest", {}).get("review_record"), "model_calls": 0,
                "counts": self.stats, "checks": dict(sorted(self.checks.items())), "errors": self.errors,
                "limitations": ["程序校验不自动作出人工确认；审核状态来自数据包的确认记录。", "审核通过仅覆盖第一阶段规则、数据规范及开发案例，不代表知识已发布。", "未运行检索、Agent、真实模型或业务服务。"]}


def validate(root: Path = ROOT) -> dict:
    return Validation(root).run()


def main():
    parser = argparse.ArgumentParser(description="离线校验第一阶段数据、开发标注和独立测试夹具")
    parser.add_argument("--root", type=Path, default=ROOT, help="数据包根目录；默认相对脚本定位项目")
    parser.add_argument("--report", type=Path, help="可选 JSON 报告路径，只允许放在项目 local 目录")
    args = parser.parse_args()
    report = validate(args.root)
    output = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        destination = args.report.resolve()
        if not destination.is_relative_to((args.root.resolve() / "local")):
            parser.error("运行结果须保存在当前数据包的 local 目录")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(output, encoding="utf-8")
    try:
        print(output)
    except UnicodeEncodeError:
        print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report["result"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
