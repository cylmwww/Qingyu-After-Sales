<!-- policy-metadata
{
  "schema_version": "1.1.1",
  "id": "POL-SHIPPING",
  "version": "1.0.0",
  "title": "青屿示例店配送与退换运费政策（历史版本）",
  "shop_id": "SHOP-DEMO-001",
  "category_ids": [
    "apparel_footwear",
    "digital_accessories",
    "customized"
  ],
  "effective_from": "2026-01-01T00:00:00+08:00",
  "effective_until": "2026-07-01T00:00:00+08:00",
  "selection_event": "purchased_at",
  "review_status": "approved",
  "lifecycle": "draft",
  "source": {
    "kind": "fictional_shop_policy",
    "description": "虚构店铺青屿示例店的历史运费规则草稿，已由项目负责人依据代审结果确认通过；仅演示统一模拟标准配送，不代表真实配送收费。"
  },
  "rules": [
    {
      "id": "RULE-SHIP-001",
      "title": "标准配送门槛与费用",
      "conditions": [],
      "effects": {
        "free_shipping_threshold_cents": 19900,
        "standard_shipping_cents": 800
      },
      "exceptions": [],
      "related_refs": [
        {
          "kind": "policy_rule",
          "id": "RULE-PRO-001",
          "version": "1.0.0"
        }
      ]
    },
    {
      "id": "RULE-SHIP-002",
      "title": "无理由退换的退件与换货运费",
      "conditions": [
        {
          "field": "return_reason",
          "op": "eq",
          "value": "no_reason"
        }
      ],
      "effects": {
        "return_shipping_payer": "customer"
      },
      "exceptions": [],
      "related_refs": [
        {
          "kind": "policy_rule",
          "id": "RULE-SHIP-004",
          "version": "1.0.0"
        }
      ]
    },
    {
      "id": "RULE-SHIP-003",
      "title": "核实质量问题的退件运费",
      "conditions": [
        {
          "field": "return_reason",
          "op": "eq",
          "value": "quality"
        },
        {
          "field": "quality_status",
          "op": "eq",
          "value": "confirmed"
        }
      ],
      "effects": {
        "return_shipping_payer": "shop"
      },
      "exceptions": [
        {
          "description": "质量仅被报告时不能直接按已核实质量问题承诺店铺承担；退换是否符合条件仍需结合通用窗口及品类政策。",
          "evidence_refs": [
            {
              "kind": "policy_rule",
              "id": "RULE-GEN-002",
              "version": "1.0.1"
            }
          ]
        }
      ],
      "related_refs": [
        {
          "kind": "policy_rule",
          "id": "RULE-GEN-002",
          "version": "1.0.1"
        }
      ]
    },
    {
      "id": "RULE-SHIP-004",
      "title": "无理由退货不退原出库运费",
      "conditions": [
        {
          "field": "return_reason",
          "op": "eq",
          "value": "no_reason"
        }
      ],
      "effects": {
        "outbound_shipping_refundable": false
      },
      "exceptions": [],
      "related_refs": [
        {
          "kind": "policy_rule",
          "id": "RULE-REF-001",
          "version": "1.0.0"
        }
      ]
    },
    {
      "id": "RULE-SHIP-005",
      "title": "核实质量问题且全单退货的原出库运费",
      "conditions": [
        {
          "field": "return_reason",
          "op": "eq",
          "value": "quality"
        },
        {
          "field": "quality_status",
          "op": "eq",
          "value": "confirmed"
        },
        {
          "field": "full_paid_order_return",
          "op": "eq",
          "value": true
        }
      ],
      "effects": {
        "outbound_shipping_refundable": true
      },
      "exceptions": [
        {
          "description": "部分质量退货不退原出库运费；全单退货事实未知时不能套用本条，原运费仅按实际已支付金额单列，不新增模拟标准运费。",
          "evidence_refs": [
            {
              "kind": "policy_rule",
              "id": "RULE-REF-001",
              "version": "1.0.0"
            }
          ]
        }
      ],
      "related_refs": [
        {
          "kind": "policy_rule",
          "id": "RULE-SHIP-003",
          "version": "1.0.0"
        },
        {
          "kind": "policy_rule",
          "id": "RULE-REF-001",
          "version": "1.0.0"
        }
      ]
    }
  ]
}
-->
# 青屿示例店配送与退换运费政策（历史版本）

店铺：青屿示例店（`SHOP-DEMO-001`）。政策：`POL-SHIPPING`，版本：`1.0.0`。虚构演示草稿，`review_status=approved`、`lifecycle=draft`，已由项目负责人依据代审结果确认通过（2026-10-09）。

## 历史版本选择与适用范围

适用于三个演示品类，按 `purchased_at` 选择本版本，生效区间为 `[2026-01-01T00:00:00+08:00, 2026-07-01T00:00:00+08:00)`，左闭右开。恰好在七月一日零点购买的订单不适用本版本，应选 `POL-SHIPPING` 的 `1.1.0`。之后才签收、申请售后或查询，不改变此前订单的运费政策版本；退货窗口仍按通用政策的签收和申请时刻单独判断。

条件按 AND 匹配。以下运费规则只解释收费及承担方式，不独立决定商品能否退换；需同时结合通用窗口、品类、赠品和金额政策。测试店仅统一模拟标准配送，不新增地域范围或偏远地区收费规则。

<a id="RULE-SHIP-001"></a>
## RULE-SHIP-001 标准配送门槛与费用

免标准配送费门槛为 `free_shipping_threshold_cents=19900`（199.00元），标准配送费为 `standard_shipping_cents=800`（8.00元）。所有金额使用整数分。

门槛依据是折扣后实际支付的商品金额，包含已分摊订单优惠后的商品实付，不使用商品标价，不把原出库运费加进门槛。商品实付达到门槛时免标准配送费，恰好达到也免；低于门槛时标准配送费为800分。赠品实付为零，不按宣传价增加门槛金额。

<a id="RULE-SHIP-002"></a>
## RULE-SHIP-002 无理由退换的退件与换货运费

无理由退货的退件运费由客户承担（`return_shipping_payer=customer`）；无理由换货的往返运费也由客户承担。费用承担不等于本系统能够执行退换，也不是自动从商品退款中扣除退件费的授权。

<a id="RULE-SHIP-003"></a>
## RULE-SHIP-003 核实质量问题的退件运费

质量问题退换且 `quality_status=confirmed` 时，退件运费由店铺承担（`return_shipping_payer=shop`）。只有用户报告的 `reported` 不能按已核实质量问题直接承诺店铺承担。

是否满足退换条件还需通用三十日窗口与品类质量条款；超窗需人工裁量。本条承担的是退件运费，不能与原订单出库运费混为同一项。

<a id="RULE-SHIP-004"></a>
## RULE-SHIP-004 无理由退货不退原出库运费

无理由退货不退原出库运费（`outbound_shipping_refundable=false`）。原出库运费不纳入商品退款，不在冻结实付商品份额中重复扣一次；退件费同样不由本条授权自动扣款。

<a id="RULE-SHIP-005"></a>
## RULE-SHIP-005 核实质量问题且全单退货的原出库运费

质量问题已经核实且 `full_paid_order_return=true` 时，原出库运费可退（`outbound_shipping_refundable=true`），以订单实际已支付的原出库运费为准，并与商品退款单列。

`full_paid_order_return` 是基于订单和退货范围证据判断的派生事实，表示本次退货覆盖该订单全部付费商品单元；不能因为退了一件主商品就假设全单退。关联赠品另按活动政策判断。部分质量退货不退原出库运费；全单范围未知时先查明，不能保证退回原运费。原订单免运费时此项为零，不补出800分或任何名义运费。
