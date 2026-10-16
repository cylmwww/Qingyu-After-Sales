<!-- policy-metadata
{
  "schema_version": "1.1.1",
  "id": "POL-SHIPPING",
  "version": "1.1.0",
  "title": "青屿示例店配送与退换运费政策（七月起版本）",
  "shop_id": "SHOP-DEMO-001",
  "category_ids": [
    "apparel_footwear",
    "digital_accessories",
    "customized"
  ],
  "effective_from": "2026-07-01T00:00:00+08:00",
  "effective_until": null,
  "selection_event": "purchased_at",
  "review_status": "approved",
  "lifecycle": "draft",
  "source": {
    "kind": "fictional_shop_policy",
    "description": "虚构店铺青屿示例店的七月起运费规则草稿，已由项目负责人依据代审结果确认通过；仅演示统一模拟标准配送，不代表真实配送收费。"
  },
  "rules": [
    {
      "id": "RULE-SHIP-001",
      "title": "标准配送门槛与费用",
      "conditions": [],
      "effects": {
        "free_shipping_threshold_cents": 15900,
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
          "version": "1.1.0"
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
          "version": "1.1.0"
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
# 青屿示例店配送与退换运费政策（七月起版本）

店铺：青屿示例店（`SHOP-DEMO-001`）。政策：`POL-SHIPPING`，版本：`1.1.0`。虚构演示草稿，`review_status=approved`、`lifecycle=draft`，已由项目负责人依据代审结果确认通过（2026-10-09）。

## 版本选择与适用范围

适用于三个演示品类，按 `purchased_at` 选择本版本，生效区间为 `[2026-07-01T00:00:00+08:00, 无截止)`。购买恰好位于七月一日零点时适用本版本；在此之前购买的订单仍适用 `1.0.0`，不能因之后签收、申请售后或查询而强行改用新门槛。退货窗口按签收和申请时刻另外判断。

条件按 AND 匹配。运费条款不独立决定商品能否退换，必须结合通用窗口、品类、赠品和金额政策。测试店仅统一模拟标准配送，不新增地域范围或偏远地区收费规则。

<a id="RULE-SHIP-001"></a>
## RULE-SHIP-001 标准配送门槛与费用

免标准配送费门槛为 `free_shipping_threshold_cents=15900`（159.00元），标准配送费为 `standard_shipping_cents=800`（8.00元）。相较 `1.0.0`，仅免标准配送费门槛由19900分调整为15900分。

所有金额以整数分处理。门槛按照折扣后实际支付的商品金额计算，即商品级优惠和订单优惠分摊后的商品实付总额，不按商品标价，也不把原出库运费加入门槛。商品实付达到或超过15900分时免标准配送费；低于时收800分。赠品实付为零，不按宣传价增加门槛。

<a id="RULE-SHIP-002"></a>
## RULE-SHIP-002 无理由退换的退件与换货运费

无理由退货的退件运费由客户承担（`return_shipping_payer=customer`）；无理由换货往返运费也由客户承担。费用承担不构成本系统执行退换的授权，不得因此从商品退款中自动扣除退件费。

<a id="RULE-SHIP-003"></a>
## RULE-SHIP-003 核实质量问题的退件运费

质量问题退换且 `quality_status=confirmed` 时，退件运费由店铺承担（`return_shipping_payer=shop`）。`quality_status=reported` 不能当成已核实，也不能据此直接承诺店铺承担。

退换资格仍需通用三十日窗口及品类质量条款；超窗需要人工裁量。退件运费与原订单出库运费是两项不同费用，不能互相替代解释。

<a id="RULE-SHIP-004"></a>
## RULE-SHIP-004 无理由退货不退原出库运费

无理由退货不退原出库运费（`outbound_shipping_refundable=false`）。原运费不计入商品退款，不能再从已冻结的商品实付份额中重复扣一次；也不自动扣退件费。

<a id="RULE-SHIP-005"></a>
## RULE-SHIP-005 核实质量问题且全单退货的原出库运费

质量问题已经核实且 `full_paid_order_return=true` 时，原出库运费可退（`outbound_shipping_refundable=true`），金额以实际已支付的原出库运费为准，单独列示。

`full_paid_order_return` 必须依据订单和所选退货范围证据，表示本次覆盖该订单全部付费商品单元；不是“退一件就是全单”，也不是模型自由猜测的事实。关联赠品另按活动政策判断。部分质量退货不退原出库运费；范围未知时不得保证退回原运费。原订单免配送费时该项为零，不凭标准配送价补出800分。
