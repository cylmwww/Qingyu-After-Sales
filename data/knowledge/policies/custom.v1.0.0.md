<!-- policy-metadata
{
  "schema_version": "1.1.1",
  "id": "POL-CUSTOM",
  "version": "1.0.0",
  "title": "青屿示例店定制商品售后政策",
  "shop_id": "SHOP-DEMO-001",
  "category_ids": [
    "customized"
  ],
  "effective_from": "2026-01-01T00:00:00+08:00",
  "effective_until": null,
  "selection_event": "purchased_at",
  "review_status": "approved",
  "lifecycle": "draft",
  "source": {
    "kind": "fictional_shop_policy",
    "description": "虚构店铺青屿示例店的定制商品规则草稿，已由项目负责人依据代审结果确认通过，不代表真实商家政策或法律意见。"
  },
  "rules": [
    {
      "id": "RULE-CUS-001",
      "title": "定制商品不支持无理由退货",
      "conditions": [
        {
          "field": "return_reason",
          "op": "eq",
          "value": "no_reason"
        }
      ],
      "effects": {
        "eligibility": "ineligible"
      },
      "exceptions": [
        {
          "description": "已核实且在质量窗口内的质量退换按质量例外判断；尚未开始制作不构成取消订单承诺。",
          "evidence_refs": [
            {
              "kind": "policy_rule",
              "id": "RULE-CUS-002",
              "version": "1.0.0"
            },
            {
              "kind": "policy_rule",
              "id": "RULE-GEN-003",
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
        },
        {
          "kind": "policy_rule",
          "id": "RULE-GEN-003",
          "version": "1.0.1"
        }
      ]
    },
    {
      "id": "RULE-CUS-002",
      "title": "核实质量问题的定制商品退换例外",
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
          "description": "超过三十日质量窗口时交人工裁量，时间证据不足时不得直接确认可退。",
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
      "id": "RULE-CUS-003",
      "title": "定制商品质量报告待核验",
      "conditions": [
        {
          "field": "return_reason",
          "op": "eq",
          "value": "quality"
        },
        {
          "field": "quality_status",
          "op": "eq",
          "value": "reported"
        }
      ],
      "effects": {
        "eligibility": "need_verification"
      },
      "exceptions": [],
      "related_refs": [
        {
          "kind": "policy_rule",
          "id": "RULE-GEN-002",
          "version": "1.0.1"
        },
        {
          "kind": "policy_rule",
          "id": "RULE-CUS-002",
          "version": "1.0.0"
        }
      ]
    }
  ]
}
-->
# 青屿示例店定制商品售后政策

店铺：青屿示例店（`SHOP-DEMO-001`）。政策：`POL-CUSTOM`，版本：`1.0.0`，仅适用于 `customized`。虚构演示草稿，`review_status=approved`、`lifecycle=draft`，已由项目负责人依据代审结果确认通过（2026-10-09），不代表真实商家政策或法律判断。

以 `purchased_at` 选择政策版本，生效区间为 `[2026-01-01T00:00:00+08:00, 无截止)`。每条条件按 AND 匹配，无理由退货和质量退换分别判断；未知事实不能自行补造。

<a id="RULE-CUS-001"></a>
## RULE-CUS-001 定制商品不支持无理由退货

定制商品申请无理由退货（`return_reason=no_reason`）时，判定为 `eligibility=ineligible`。通用七日窗口不覆盖该品类限制。

制作尚未开始，也不能据此承诺可以取消订单，更不能宣称本系统已经取消。取消订单属于 `RULE-GEN-003` 所禁止的交易写操作；这里的退货限制不是额外的取消政策。

质量问题不适用本条无理由限制，应按 `RULE-CUS-002` 或 `RULE-CUS-003` 区分核实与报告状态。

<a id="RULE-CUS-002"></a>
## RULE-CUS-002 核实质量问题的定制商品退换例外

质量问题已经核实（`quality_status=confirmed`），且有证据确认 `within_quality_window=true` 的质量退换申请，品类判定为 `eligibility=eligible`，符合退换条件。

该窗口必须依 `RULE-GEN-002` 从签收时刻起算连续三十个二十四小时，申请时间不晚于截止时刻，包含边界。定制商品不支持无理由退货的规则不阻断这一质量例外。本条不能忽略通用质量核验和时间要求，也不是执行退款或确定金额的授权。超窗申请交人工裁量。

<a id="RULE-CUS-003"></a>
## RULE-CUS-003 定制商品质量报告待核验

质量退换申请中，质量状态只是 `quality_status=reported` 时，判定为 `eligibility=need_verification`，需要补充并核实质量证据。

用户说“有问题”、提交描述或照片，不自动等同于 `confirmed`。在取得核实结果和签收、申请时间证据之前，不能保证可退换或给出确定退款金额。经核实后再结合三十日窗口判断；超过窗口仍需人工裁量。
