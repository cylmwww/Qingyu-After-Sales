<!-- policy-metadata
{
  "schema_version": "1.1.1",
  "id": "POL-GENERAL",
  "version": "1.0.1",
  "title": "青屿示例店通用售后与只读边界",
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
    "description": "虚构店铺青屿示例店的演示政策草稿，已由项目负责人依据代审结果确认通过；不代表真实商家政策或法律意见。"
  },
  "rules": [
    {
      "id": "RULE-GEN-001",
      "title": "无理由退货窗口",
      "conditions": [
        {
          "field": "return_reason",
          "op": "eq",
          "value": "no_reason"
        }
      ],
      "effects": {
        "no_reason_window_days": 7,
        "late_application_action": "manual_review"
      },
      "exceptions": [
        {
          "description": "尚未签收或签收时刻缺失时，不得以购买时刻代替签收时刻计算窗口，也不得保证已经满足退货时间条件。",
          "evidence_refs": []
        },
        {
          "description": "超出七日窗口不满足正常无理由条件；可生成交接摘要供人工裁量，是否最终受理未知，不直接保证退款或把超期等同必然拒绝。",
          "evidence_refs": [
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
          "id": "RULE-APP-001",
          "version": "1.0.0"
        },
        {
          "kind": "policy_rule",
          "id": "RULE-DIG-001",
          "version": "1.0.0"
        },
        {
          "kind": "policy_rule",
          "id": "RULE-CUS-001",
          "version": "1.0.0"
        }
      ]
    },
    {
      "id": "RULE-GEN-002",
      "title": "质量问题窗口与核验要求",
      "conditions": [
        {
          "field": "return_reason",
          "op": "eq",
          "value": "quality"
        }
      ],
      "effects": {
        "quality_window_days": 30,
        "quality_confirmation_required": true
      },
      "exceptions": [
        {
          "description": "质量问题只有用户报告、尚未核实，或签收与申请时间证据不足时，不能直接认定满足质量退换条件；超过质量窗口的申请交由人工裁量，不自动接受或拒绝。",
          "evidence_refs": []
        }
      ],
      "related_refs": [
        {
          "kind": "policy_rule",
          "id": "RULE-APP-004",
          "version": "1.0.0"
        },
        {
          "kind": "policy_rule",
          "id": "RULE-DIG-003",
          "version": "1.0.0"
        },
        {
          "kind": "policy_rule",
          "id": "RULE-CUS-002",
          "version": "1.0.0"
        },
        {
          "kind": "policy_rule",
          "id": "RULE-CUS-003",
          "version": "1.0.0"
        }
      ]
    },
    {
      "id": "RULE-GEN-003",
      "title": "客服演示的只读边界",
      "conditions": [],
      "effects": {
        "scope": "read_only"
      },
      "exceptions": [],
      "related_refs": [
        {
          "kind": "policy_rule",
          "id": "RULE-REF-002",
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
# 青屿示例店通用售后与只读边界

店铺：青屿示例店（`SHOP-DEMO-001`）。政策：`POL-GENERAL`，版本：`1.0.1`。这是虚构店铺的演示草稿，状态为 `approved`、`draft`，已由项目负责人依据代审结果确认通过（2026-10-09），不代表真实商家承诺，不提供真实法律判断。

## 适用与条件约定

适用于服饰鞋靴、数码配件和定制商品。以订单购买时刻 `purchased_at` 选择政策版本，生效区间为 `[2026-01-01T00:00:00+08:00, 无截止)`；购买时刻不是退货窗口的起点。

每条规则的 `conditions` 按 AND 同时满足；空条件表示在本政策适用范围内通用。未知条件不得当成已满足或直接当成否定。`return_reason=no_reason` 表示无理由退货，`return_reason=quality` 表示质量问题退换；`quality_status` 仅使用 `none`、`reported`、`confirmed`，报告问题不等于已核实。窗口派生事实必须有签收时刻和售后申请时刻证据，不能由购买时间或问题描述猜测。

<a id="RULE-GEN-001"></a>
## RULE-GEN-001 无理由退货窗口

无理由退货窗口为 `no_reason_window_days=7`。从有证据的签收时刻 `signed_at` 起计算连续 `7 × 24` 小时，截止时刻为 `signed_at + 7 × 24小时`。售后申请时刻 `after_sales_applied_at` 不晚于截止时刻时满足时间条件，申请恰好位于截止时刻也包含在内。

尚未签收、签收时刻未知或申请时刻缺失时，不能确定窗口条件；尤其不得使用购买日期替代签收时刻，也不能把“购买未满七天”作为充分依据。签收前不能据此承诺可以退货或取消订单。

满足七日窗口只说明时间条件满足，不构成完整可退判定。必须同时结合对应品类政策：服饰鞋靴的吊牌、完整性、室外使用和包装条件，数码配件的激活、完整性和配件条件，或者定制商品的无理由退货限制。品类条款中 `eligibility=eligible` 也不能绕过本条窗口。

超过七日截止时刻的无理由申请不满足正常时间条件，处理路径为 `late_application_action=manual_review`。可以解释超期并生成交接摘要，供人工判断是否另行受理；最终受理和退款金额未知。不得直接保证可退，也不得仅因超期断言人工必然拒绝，更不能宣称人工已接手。

<a id="RULE-GEN-002"></a>
## RULE-GEN-002 质量问题窗口与核验要求

质量问题窗口为 `quality_window_days=30`，并要求核验质量事实（`quality_confirmation_required=true`）。以有证据的签收时刻起算连续 `30 × 24` 小时，申请时刻不晚于截止时刻时可记为 `within_quality_window=true`，含截止边界；缺少签收或申请时刻时不能生成这一肯定事实。

`quality_status=reported` 只表示有人报告问题，不能等同于 `quality_status=confirmed`。在质量窗口内申请且质量问题已经核实为 `confirmed` 的，结合对应品类质量条款可判定符合退换条件；本条仅规定质量窗口与核验要求，不单独输出覆盖所有品类的可退结论。

超过三十日窗口的质量申请需要人工裁量，不自动接受、不直接拒绝，也不承诺具体退款金额。未核实的问题需补充和核验证据。质量例外与无理由条件分开：不能用吊牌缺失、鞋盒缺失或数码配件激活状态直接阻断已经核实且在窗口内的质量退换。

以上均为虚构店铺的演示约定，不主张其为现实中的法定期限或法定义务。

<a id="RULE-GEN-003"></a>
## RULE-GEN-003 客服演示的只读边界

演示客服的操作范围为 `scope=read_only`：只查询、估算和解释，不执行退款、取消订单、改价等交易写操作。即使政策条件满足、客户要求立即执行或退款记录显示处理中，也不能宣称本系统已经执行上述操作。

需要人工处理时，只能生成包含诉求、已知事实、证据和未解决事项的交接摘要。没有接入真实人工工单平台，不得说“人工已接手”“已经转交成功”或作出处理期限承诺。金额预估、审批和实际到账依退款及进度政策分别说明。
