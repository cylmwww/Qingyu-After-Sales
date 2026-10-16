<!-- policy-metadata
{
  "schema_version": "1.1.1",
  "id": "POL-PROGRESS",
  "version": "1.0.0",
  "title": "青屿示例店售后退款进度解释政策",
  "shop_id": "SHOP-DEMO-001",
  "category_ids": [
    "apparel_footwear",
    "digital_accessories",
    "customized"
  ],
  "effective_from": "2026-01-01T00:00:00+08:00",
  "effective_until": null,
  "selection_event": "after_sales_applied_at",
  "review_status": "approved",
  "lifecycle": "draft",
  "source": {
    "kind": "fictional_shop_policy",
    "description": "虚构店铺青屿示例店的售后进度表述规则草稿，已由项目负责人依据代审结果确认通过，不代表真实处理进度或到账期限。"
  },
  "rules": [
    {
      "id": "RULE-PRG-001",
      "title": "退款记录与接口状态分别解释",
      "conditions": [],
      "effects": {
        "refund_state_distinction": true
      },
      "exceptions": [
        {
          "description": "退款记录没有完整的settled_at与settled_cents证据时，不得仅凭处理中、审批通过或金额预估宣布到账。",
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
          "id": "RULE-REF-002",
          "version": "1.0.0"
        },
        {
          "kind": "policy_rule",
          "id": "RULE-GEN-003",
          "version": "1.0.1"
        }
      ]
    },
    {
      "id": "RULE-PRG-002",
      "title": "再次询问动态进度必须刷新",
      "conditions": [],
      "effects": {
        "refresh_required": true
      },
      "exceptions": [
        {
          "description": "刷新失败时只能说明无法取得当前状态；旧结果可作为带查询时间的历史事实，不得冒充新的成功查询。",
          "evidence_refs": [
            {
              "kind": "policy_rule",
              "id": "RULE-PRG-001",
              "version": "1.0.0"
            }
          ]
        }
      ],
      "related_refs": [
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
# 青屿示例店售后退款进度解释政策

店铺：青屿示例店（`SHOP-DEMO-001`）。政策：`POL-PROGRESS`，版本：`1.0.0`，适用于三个演示品类。虚构演示草稿，`review_status=approved`、`lifecycle=draft`，已由项目负责人依据代审结果确认通过（2026-10-09）。

本政策以售后申请时刻 `after_sales_applied_at` 选择版本，生效区间为 `[2026-01-01T00:00:00+08:00, 无截止)`，不是按购买或查询时刻选择。申请时刻未知则不能猜测匹配。本政策解释状态，不执行退款、不代表真实处理进度。条件按 AND 匹配，空条件在适用范围内通用。

<a id="RULE-PRG-001"></a>
## RULE-PRG-001 退款记录与接口状态分别解释

`refund_state_distinction=true`：无退款记录、退款处理中、退款失败、已到账，以及查询接口失败必须分别说明。每次解释需要标注有效查询的 `observed_at`，表明“截至该查询时刻”的证据范围。

- 成功查询且无退款记录：说明截至查询时刻没有查到退款记录，不等同于审批拒绝、退款失败或已经到账。
- 退款处理中：仅说明记录显示处理中，不推断已到账，也不承诺到达日期。
- 退款失败：依失败记录说明，不将其解释为没有记录或仍正常处理中。
- 已到账：只有明确的到账记录并具有 `settled_at` 与 `settled_cents` 证据时，才说明记录中的到账时间和到账金额。不得以预估或审批金额补填实际到账金额；状态文字和到账证据矛盾时不得直接确认到账。
- 查询接口失败：说明当前查询未成功、状态无法确认，不能推断无退款记录、退款失败或到账，也不能把失败冒充有效业务查询。

预估、审批与到账依 `RULE-REF-002` 分开表述。只读系统不会发起支付或更改退款状态，不能声称已经退款。

<a id="RULE-PRG-002"></a>
## RULE-PRG-002 再次询问动态进度必须刷新

`refresh_required=true`：用户再次询问“现在到哪了”“刚才那笔到账了吗”等动态进度时，需要取得新的有效查询结果，并更新 `observed_at`，不能沿用旧状态宣称已经到账或仍然处于当前状态。

刷新失败时，应明确说明暂时无法确认当前状态。可引用有时间标记的旧结果作为历史事实，但不能冒充本次查询成功。只有新结果和到账证据均支持时才能确认新的到账事实；不作出确定的处理期限或到账期限承诺。
