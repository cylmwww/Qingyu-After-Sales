<!-- policy-metadata
{
  "schema_version": "1.1.1",
  "id": "POL-DIGITAL",
  "version": "1.0.0",
  "title": "青屿示例店数码配件售后政策",
  "shop_id": "SHOP-DEMO-001",
  "category_ids": [
    "digital_accessories"
  ],
  "effective_from": "2026-01-01T00:00:00+08:00",
  "effective_until": null,
  "selection_event": "purchased_at",
  "review_status": "approved",
  "lifecycle": "draft",
  "source": {
    "kind": "fictional_shop_policy",
    "description": "虚构店铺青屿示例店的数码配件规则草稿，已由项目负责人依据代审结果确认通过，不代表真实商家政策。"
  },
  "rules": [
    {
      "id": "RULE-DIG-001",
      "title": "未激活且完整的数码配件无理由退货",
      "conditions": [
        {
          "field": "return_reason",
          "op": "eq",
          "value": "no_reason"
        },
        {
          "field": "activated",
          "op": "eq",
          "value": false
        },
        {
          "field": "product_intact",
          "op": "eq",
          "value": true
        },
        {
          "field": "accessories_complete",
          "op": "eq",
          "value": true
        }
      ],
      "effects": {
        "eligibility": "eligible"
      },
      "exceptions": [
        {
          "description": "还需满足签收起七日的通用窗口；opened不是本条限制条件，拆封不等于激活。",
          "evidence_refs": [
            {
              "kind": "policy_rule",
              "id": "RULE-GEN-001",
              "version": "1.0.1"
            }
          ]
        }
      ],
      "related_refs": [
        {
          "kind": "policy_rule",
          "id": "RULE-GEN-001",
          "version": "1.0.1"
        }
      ]
    },
    {
      "id": "RULE-DIG-002",
      "title": "已激活数码配件的无理由退货限制",
      "conditions": [
        {
          "field": "return_reason",
          "op": "eq",
          "value": "no_reason"
        },
        {
          "field": "activated",
          "op": "eq",
          "value": true
        }
      ],
      "effects": {
        "eligibility": "ineligible"
      },
      "exceptions": [
        {
          "description": "本条不适用于已核实且在质量窗口内的质量退换，激活不能阻断该质量例外。",
          "evidence_refs": [
            {
              "kind": "policy_rule",
              "id": "RULE-DIG-003",
              "version": "1.0.0"
            }
          ]
        }
      ],
      "related_refs": [
        {
          "kind": "policy_rule",
          "id": "RULE-DIG-003",
          "version": "1.0.0"
        }
      ]
    },
    {
      "id": "RULE-DIG-003",
      "title": "核实质量问题的数码配件退换例外",
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
          "field": "within_quality_window",
          "op": "eq",
          "value": true
        }
      ],
      "effects": {
        "eligibility": "eligible"
      },
      "exceptions": [
        {
          "description": "质量仅被报告时需核验；超过三十日质量窗口时需人工裁量。",
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
      "id": "RULE-DIG-004",
      "title": "未激活但配件缺失的无理由退货人工裁量",
      "conditions": [
        {
          "field": "return_reason",
          "op": "eq",
          "value": "no_reason"
        },
        {
          "field": "accessories_complete",
          "op": "eq",
          "value": false
        },
        {
          "field": "activated",
          "op": "eq",
          "value": false
        }
      ],
      "effects": {
        "eligibility": "manual_review"
      },
      "exceptions": [],
      "related_refs": [
        {
          "kind": "policy_rule",
          "id": "RULE-GEN-001",
          "version": "1.0.1"
        }
      ]
    }
  ]
}
-->
# 青屿示例店数码配件售后政策

店铺：青屿示例店（`SHOP-DEMO-001`）。政策：`POL-DIGITAL`，版本：`1.0.0`，仅适用于 `digital_accessories`。虚构演示草稿，`review_status=approved`、`lifecycle=draft`，已由项目负责人依据代审结果确认通过（2026-10-09）。

以 `purchased_at` 选择政策版本，生效区间为 `[2026-01-01T00:00:00+08:00, 无截止)`。每条条件按 AND 匹配，激活与拆封是不同事实；`opened` 可以记录但不是无理由退货限制。未知激活状态不能当作未激活。

<a id="RULE-DIG-001"></a>
## RULE-DIG-001 未激活且完整的数码配件无理由退货

无理由退货且未激活（`activated=false`）、商品完整（`product_intact=true`）、配件齐全（`accessories_complete=true`）时，品类状态判定为 `eligibility=eligible`。拆封但未激活且完整、配件齐全的数码配件，也可满足本条；不对 `opened` 设置限制，不得增加“拆封一律不退”的条件。

还必须同时满足 `RULE-GEN-001`：签收时刻起七个连续二十四小时内申请，包含截止边界。本条 `eligible` 不是完整可退承诺；缺少时间、激活、完整性或配件事实时需先查明，不能仅凭“没用过”推断。

<a id="RULE-DIG-002"></a>
## RULE-DIG-002 已激活数码配件的无理由退货限制

无理由退货且已经激活（`activated=true`）时，判定为 `eligibility=ineligible`。即使在七日窗口内，也不能忽略激活限制。

本条只限无理由退货。已核实且在三十日质量窗口内的质量申请适用 `RULE-DIG-003`，不能因激活过而拒绝该质量退换。

<a id="RULE-DIG-003"></a>
## RULE-DIG-003 核实质量问题的数码配件退换例外

质量问题退换时，质量事实已经核实（`quality_status=confirmed`），并有证据确认 `within_quality_window=true` 的，品类判定为 `eligibility=eligible`。

质量窗口依 `RULE-GEN-002` 从签收时刻起算三十个连续二十四小时，申请恰在截止时刻也包含在内。激活状态不阻断这一质量例外，也不套用无理由退货的配件齐全条件。只有 `reported` 不足以适用本条；超过质量窗口交人工裁量，时间证据缺失时先查证。

<a id="RULE-DIG-004"></a>
## RULE-DIG-004 未激活但配件缺失的无理由退货人工裁量

无理由退货、未激活（`activated=false`）但配件不齐（`accessories_complete=false`）时，判定为 `eligibility=manual_review`。

先查明通用七日时间条件以及实际缺失情况，再整理人工交接摘要。不能自动保证可退、不能自动按配件宣传价扣款，也不能自行给出确定退款金额。若质量问题已核实且在质量窗口内，改按质量条款判断，不把无理由退货条件套入质量例外。
