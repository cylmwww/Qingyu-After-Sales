<!-- policy-metadata
{
  "schema_version": "1.1.1",
  "id": "POL-APPAREL",
  "version": "1.0.0",
  "title": "青屿示例店服饰鞋靴售后政策",
  "shop_id": "SHOP-DEMO-001",
  "category_ids": [
    "apparel_footwear"
  ],
  "effective_from": "2026-01-01T00:00:00+08:00",
  "effective_until": null,
  "selection_event": "purchased_at",
  "review_status": "approved",
  "lifecycle": "draft",
  "source": {
    "kind": "fictional_shop_policy",
    "description": "虚构店铺青屿示例店的服饰鞋靴规则草稿，已由项目负责人依据代审结果确认通过，不代表真实商家政策。"
  },
  "rules": [
    {
      "id": "RULE-APP-001",
      "title": "状态完整的服饰鞋靴无理由退货",
      "conditions": [
        {
          "field": "return_reason",
          "op": "eq",
          "value": "no_reason"
        },
        {
          "field": "tags_intact",
          "op": "eq",
          "value": true
        },
        {
          "field": "product_intact",
          "op": "eq",
          "value": true
        },
        {
          "field": "used_outdoors",
          "op": "eq",
          "value": false
        },
        {
          "field": "packaging_intact",
          "op": "eq",
          "value": true
        }
      ],
      "effects": {
        "eligibility": "eligible"
      },
      "exceptions": [
        {
          "description": "本条仅判定品类状态条件，还必须满足通用七日窗口；缺少任何必要条件证据时不得据此直接保证可退。",
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
      "id": "RULE-APP-002",
      "title": "吊牌不完整的无理由退货限制",
      "conditions": [
        {
          "field": "return_reason",
          "op": "eq",
          "value": "no_reason"
        },
        {
          "field": "tags_intact",
          "op": "eq",
          "value": false
        }
      ],
      "effects": {
        "eligibility": "ineligible"
      },
      "exceptions": [
        {
          "description": "本条仅限无理由退货，不能阻断已核实且在质量窗口内的质量退换。",
          "evidence_refs": [
            {
              "kind": "policy_rule",
              "id": "RULE-APP-004",
              "version": "1.0.0"
            }
          ]
        }
      ],
      "related_refs": [
        {
          "kind": "policy_rule",
          "id": "RULE-APP-004",
          "version": "1.0.0"
        }
      ]
    },
    {
      "id": "RULE-APP-003",
      "title": "包装缺失的无理由退货人工裁量",
      "conditions": [
        {
          "field": "return_reason",
          "op": "eq",
          "value": "no_reason"
        },
        {
          "field": "packaging_intact",
          "op": "eq",
          "value": false
        },
        {
          "field": "tags_intact",
          "op": "eq",
          "value": true
        },
        {
          "field": "product_intact",
          "op": "eq",
          "value": true
        },
        {
          "field": "used_outdoors",
          "op": "eq",
          "value": false
        }
      ],
      "effects": {
        "eligibility": "manual_review"
      },
      "exceptions": [
        {
          "description": "已核实且在质量窗口内的质量问题依质量例外办理，不能因鞋盒或包装缺失直接拒绝。",
          "evidence_refs": [
            {
              "kind": "policy_rule",
              "id": "RULE-APP-004",
              "version": "1.0.0"
            }
          ]
        }
      ],
      "related_refs": [
        {
          "kind": "policy_rule",
          "id": "RULE-GEN-001",
          "version": "1.0.1"
        },
        {
          "kind": "policy_rule",
          "id": "RULE-APP-004",
          "version": "1.0.0"
        }
      ]
    },
    {
      "id": "RULE-APP-004",
      "title": "核实质量问题的服饰鞋靴退换例外",
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
          "description": "质量仅为reported不能套用本条；超过三十日窗口交人工裁量。",
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
    }
  ]
}
-->
# 青屿示例店服饰鞋靴售后政策

店铺：青屿示例店（`SHOP-DEMO-001`）。政策：`POL-APPAREL`，版本：`1.0.0`，仅适用于 `apparel_footwear`。虚构演示草稿，`review_status=approved`、`lifecycle=draft`，已由项目负责人依据代审结果确认通过（2026-10-09）。

以 `purchased_at` 选择政策版本，生效区间为 `[2026-01-01T00:00:00+08:00, 无截止)`。所有条件按 AND 匹配，未提供的商品状态不能猜测。无理由条件与质量条件分开匹配，规则中的可退结论不是执行退款的授权。

<a id="RULE-APP-001"></a>
## RULE-APP-001 状态完整的服饰鞋靴无理由退货

申请原因为无理由退货，且吊牌完整（`tags_intact=true`）、商品完整（`product_intact=true`）、未在室外使用（`used_outdoors=false`）、包装完整（`packaging_intact=true`）时，品类状态判定为 `eligibility=eligible`。

这是品类状态条件的结论，不是完整退货承诺。还必须同时满足 `RULE-GEN-001`：签收时刻起七个连续二十四小时内提出申请，包含截止边界。缺少签收、申请时刻或任何商品状态事实时，先澄清或查证，不得直接保证可退。运费、赠品与金额分别结合相应政策说明。

<a id="RULE-APP-002"></a>
## RULE-APP-002 吊牌不完整的无理由退货限制

申请原因为无理由退货且吊牌不完整（`tags_intact=false`）时，判定为 `eligibility=ineligible`。七日内申请本身不能覆盖吊牌条件。

本条明确仅限无理由退货；质量问题已核实并满足质量窗口时，应适用 `RULE-APP-004`，不得把缺少吊牌当成阻断质量退换的依据。

<a id="RULE-APP-003"></a>
## RULE-APP-003 包装缺失的无理由退货人工裁量

无理由退货时，包装不完整（`packaging_intact=false`），但吊牌和商品完整且未在室外使用的，判定为 `eligibility=manual_review`。鞋盒遗失属于此类包装问题。

应结合 `RULE-GEN-001` 查明时间条件，再说明需要人工判断包装缺失的处理。不能直接保证退货、不能因为缺鞋盒一律拒绝，也不能自动扣除包装费或给出自动扣款金额。这里只可整理交接摘要，不能声称人工已经接手。

<a id="RULE-APP-004"></a>
## RULE-APP-004 核实质量问题的服饰鞋靴退换例外

申请原因为质量问题，质量已经核实（`quality_status=confirmed`），并有证据确认 `within_quality_window=true` 时，品类判定为 `eligibility=eligible`，符合退换条件。

`within_quality_window` 必须依 `RULE-GEN-002` 的签收与申请时刻计算：三十个连续二十四小时，包含截止边界。本条质量例外不受缺鞋盒或吊牌不完整阻断，也不套用无理由退货的室外使用条件。仅有 `reported` 不能当作 `confirmed`；超过三十日窗口需人工裁量。
