<!-- policy-metadata
{
  "schema_version": "1.1.1",
  "id": "POL-REFUND",
  "version": "1.0.0",
  "title": "青屿示例店退款金额与表述政策",
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
    "description": "虚构店铺青屿示例店的退款计算与表述规则草稿，已由项目负责人依据代审结果确认通过，不代表真实退款承诺。"
  },
  "rules": [
    {
      "id": "RULE-REF-001",
      "title": "按冻结商品单元实付份额解释退款",
      "conditions": [],
      "effects": {
        "refund_basis": "unit_paid_amounts_cents"
      },
      "exceptions": [
        {
          "description": "缺少退货范围、冻结实付份额、适用运费规则或关联赠品状态时，不能输出确定退款金额。",
          "evidence_refs": [
            {
              "kind": "policy_rule",
              "id": "RULE-REF-002",
              "version": "1.0.0"
            },
            {
              "kind": "policy_rule",
              "id": "RULE-PRO-003",
              "version": "1.0.0"
            }
          ]
        }
      ],
      "related_refs": [
        {
          "kind": "policy_rule",
          "id": "RULE-PRO-001",
          "version": "1.0.0"
        },
        {
          "kind": "policy_rule",
          "id": "RULE-PRO-002",
          "version": "1.0.0"
        },
        {
          "kind": "policy_rule",
          "id": "RULE-PRO-003",
          "version": "1.0.0"
        },
        {
          "kind": "policy_rule",
          "id": "RULE-SHIP-004",
          "version": "1.0.0"
        },
        {
          "kind": "policy_rule",
          "id": "RULE-SHIP-005",
          "version": "1.0.0"
        },
        {
          "kind": "policy_rule",
          "id": "RULE-SHIP-004",
          "version": "1.1.0"
        },
        {
          "kind": "policy_rule",
          "id": "RULE-SHIP-005",
          "version": "1.1.0"
        }
      ]
    },
    {
      "id": "RULE-REF-002",
      "title": "预估、审批与到账金额分开表述",
      "conditions": [],
      "effects": {
        "amount_semantics": "estimate_approved_settled_separate"
      },
      "exceptions": [],
      "related_refs": [
        {
          "kind": "policy_rule",
          "id": "RULE-GEN-003",
          "version": "1.0.1"
        },
        {
          "kind": "policy_rule",
          "id": "RULE-PRO-003",
          "version": "1.0.0"
        },
        {
          "kind": "policy_rule",
          "id": "RULE-PRG-001",
          "version": "1.0.0"
        }
      ]
    }
  ]
}
-->
# 青屿示例店退款金额与表述政策

店铺：青屿示例店（`SHOP-DEMO-001`）。政策：`POL-REFUND`，版本：`1.0.0`，适用于三个演示品类。虚构演示草稿，`review_status=approved`、`lifecycle=draft`，已由项目负责人依据代审结果确认通过（2026-10-09）。

以 `purchased_at` 选择政策版本，生效区间为 `[2026-01-01T00:00:00+08:00, 无截止)`。空条件规则在本政策范围内通用，但不意味着无需退货资格、订单或支付证据。运费关联条款的两个版本仅用于上下文回查，同一订单应按购买时刻选择唯一适用版本，不能把两个版本叠加。

<a id="RULE-REF-001"></a>
## RULE-REF-001 按冻结商品单元实付份额解释退款

退款依据为 `refund_basis=unit_paid_amounts_cents`。商品退款金额等于所选退货商品单元的已冻结实际支付份额之和，使用整数分；不以标价或当前活动价重算。所选单元必须有可核查的订单行、数量和冻结支付份额依据，不得仅凭行平均价猜测具体单元份额。

赠品实付为零，排除在商品退款之外；禁止增加其宣传价或名义价值。商品级优惠和订单优惠已经反映在冻结份额中，不重复扣优惠，不对剩余商品追扣门槛优惠，也不自动减去退件费。

原出库运费依购买时刻对应的 `RULE-SHIP-004`、`RULE-SHIP-005` 单列：无理由退货不退；已核实质量问题且退回全单付费商品时可按实际已付原运费列入；部分质量退货不退原运费。原运费不能从商品份额中重复扣除，也不能把模拟标准配送价当成已支付证据。原运费是否可退不改变商品退款的冻结份额基准。

仅在退货条件、退货范围、冻结支付份额、适用运费规则及关联赠品状态证据完整时，才可解释计算出的预估金额；不执行退款。资料缺失、赠品未退或待人工裁量时，不输出确定金额。

<a id="RULE-REF-002"></a>
## RULE-REF-002 预估、审批与到账金额分开表述

采用 `amount_semantics=estimate_approved_settled_separate`：预估金额、实际审批金额和实际到账金额必须分开，三者不能互相代替。

- 预估只表示按已查事实和适用政策进行的解释性计算，不是审批通过或支付承诺。
- 审批金额必须来自明确的审批记录；即使审批通过也不表示已经到账。
- 到账金额与时间必须来自退款进度证据，依 `RULE-PRG-001` 核实 `settled_cents` 和 `settled_at`；处理中不能声称已到账。

缺少必要资料、赠品状态未知、关联赠品未退或需要人工裁量时，应说明缺口，不得输出确定退款金额。可以解释计算口径或已核实的部分事实，但不能将假设变成确定金额。系统只读，不宣称执行退款，也不作出到账期限承诺。
