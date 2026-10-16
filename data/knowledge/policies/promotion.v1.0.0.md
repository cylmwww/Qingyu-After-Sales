<!-- policy-metadata
{
  "schema_version": "1.1.1",
  "id": "POL-PROMOTION",
  "version": "1.0.0",
  "title": "青屿示例店优惠分摊与赠品政策",
  "shop_id": "SHOP-DEMO-001",
  "category_ids": [
    "apparel_footwear",
    "digital_accessories",
    "customized"
  ],
  "effective_from": "2026-01-01T00:00:00+08:00",
  "effective_until": null,
  "selection_event": "purchased_at",
  "review_status": "approved",
  "lifecycle": "draft",
  "source": {
    "kind": "fictional_shop_policy",
    "description": "虚构店铺青屿示例店的优惠和赠品规则草稿，已由项目负责人依据代审结果确认通过，不代表真实活动条款。"
  },
  "rules": [
    {
      "id": "RULE-PRO-001",
      "title": "整数分最大余数优惠分摊与冻结",
      "conditions": [],
      "effects": {
        "coupon_allocation": "largest_remainder"
      },
      "exceptions": [
        {
          "description": "缺少有效的商品级优惠、订单优惠、订单行或冻结支付份额证据时，不能猜测分摊结果或确定退款金额。",
          "evidence_refs": [
            {
              "kind": "policy_rule",
              "id": "RULE-REF-002",
              "version": "1.0.0"
            }
          ]
        }
      ],
      "related_refs": [
        {
          "kind": "policy_rule",
          "id": "RULE-REF-001",
          "version": "1.0.0"
        }
      ]
    },
    {
      "id": "RULE-PRO-002",
      "title": "部分退货不追扣剩余商品优惠",
      "conditions": [],
      "effects": {
        "coupon_clawback": false
      },
      "exceptions": [],
      "related_refs": [
        {
          "kind": "policy_rule",
          "id": "RULE-PRO-001",
          "version": "1.0.0"
        },
        {
          "kind": "policy_rule",
          "id": "RULE-REF-001",
          "version": "1.0.0"
        }
      ]
    },
    {
      "id": "RULE-PRO-003",
      "title": "关联赠品随主商品退回",
      "conditions": [],
      "effects": {
        "gift_return_required": true,
        "missing_gift_action": "manual_review"
      },
      "exceptions": [
        {
          "description": "没有关联赠品的商品不产生赠品退回要求；有关联赠品但未退或状态未知时，需要人工判断，不能自动扣宣传价或输出确定金额。",
          "evidence_refs": [
            {
              "kind": "policy_rule",
              "id": "RULE-REF-001",
              "version": "1.0.0"
            },
            {
              "kind": "policy_rule",
              "id": "RULE-REF-002",
              "version": "1.0.0"
            }
          ]
        }
      ],
      "related_refs": [
        {
          "kind": "policy_rule",
          "id": "RULE-REF-001",
          "version": "1.0.0"
        },
        {
          "kind": "policy_rule",
          "id": "RULE-REF-002",
          "version": "1.0.0"
        }
      ]
    }
  ]
}
-->
# 青屿示例店优惠分摊与赠品政策

店铺：青屿示例店（`SHOP-DEMO-001`）。政策：`POL-PROMOTION`，版本：`1.0.0`。适用于三个演示品类。虚构演示草稿，`review_status=approved`、`lifecycle=draft`，已由项目负责人依据代审结果确认通过（2026-10-09）。

以 `purchased_at` 选择版本，生效区间为 `[2026-01-01T00:00:00+08:00, 无截止)`。规则条件按 AND 匹配；空条件为本政策内通用，具体是否存在优惠或关联赠品必须由订单与活动证据判断，不得凭问题描述新增活动。

<a id="RULE-PRO-001"></a>
## RULE-PRO-001 整数分最大余数优惠分摊与冻结

订单级优惠采用 `coupon_allocation=largest_remainder`，即最大余数法。全部金额均使用整数分，不用浮点元金额四舍五入代替分摊。

1. 每个参与分摊的订单行权重为“单价分 × 数量 − 商品级优惠分”；使用商品级优惠后的行金额，不含原运费，零实付赠品不增加权重。
2. 订单优惠按各行权重占总权重的比例计算理想份额，先对每行份额向下取整到分。
3. 尚未分完的整数分按理想份额的小数余数从大到小逐一分配；余数相同则按订单行 ID 升序，每次分配一分。不得任意改变并列顺序。
4. 行份额总和应等于实际订单优惠总额；最终实际支付金额及订单行、商品单元的实际支付份额冻结保存。退款时读取已冻结的 `unit_paid_amounts_cents`，不能按退货后的剩余商品重新计算历史分摊。

缺少金额、订单行、支付冻结份额等必要证据，或者无法得到有效权重与分摊依据时，先补查，不猜测结果。本政策不额外规定历史商品单元份额的生成算法；退款依据是可核查的冻结单元实付份额。

<a id="RULE-PRO-002"></a>
## RULE-PRO-002 部分退货不追扣剩余商品优惠

`coupon_clawback=false`：部分退货后，即使剩余商品的金额不再达到原优惠门槛，也不对剩余商品追扣门槛优惠。

退货商品依冻结实付份额计算，剩余商品保留原冻结优惠。不能重算整单优惠再扣一次，也不能以“还没达到活动门槛”为由追加退款扣款。

<a id="RULE-PRO-003"></a>
## RULE-PRO-003 关联赠品随主商品退回

`gift_return_required=true`：订单中有关联赠品的主商品退货时，应同时退回该关联赠品。没有关联赠品的商品不额外产生赠品要求，关联关系必须有订单或活动依据。

赠品未退回或是否退回的资料缺失时，采取 `missing_gift_action=manual_review`。只能说明需要核查或人工裁量，不能自动按宣传价、标价或名义价值扣款，也不能直接给出确定退款金额。

赠品实际支付金额为零，商品退款应排除赠品，禁止把赠品的名义价值加入退款。赠品问题不能被“剩余商品不追扣优惠”规则自动解决，两条规则需分别判断。
