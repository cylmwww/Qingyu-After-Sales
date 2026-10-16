# 示例数据离线校验

校验器用于在入库或读取模拟业务种子前检查数据文件的结构、引用和关键一致性。它不启动数据库、检索、业务接口或模型。数据格式见[示例数据规范](data-spec.md)。

## 环境与入口

独立执行只需要 Python 3.10 或更新版本，无需安装第三方包、数据库或模型密钥：

```bash
python -B scripts/validate_stage1.py
```

脚本按自身位置定位项目根目录。JSON 输出包含 `result`、检查数量、错误列表、`human_review_status` 和数据包审核记录；通过时退出码为 0，发现问题时为 1。

保存结果可使用 `--report`。该参数只允许写入所校验项目的 `local/` 目录：

```bash
python -B scripts/validate_stage1.py --report local/data-validation.json
```

检查完整数据包副本时可指定实际目录：

```bash
python -B scripts/validate_stage1.py --root /path/to/package-copy
```

## 检查内容

校验包括文件清单、Schema 字段、稳定 ID 与精确版本、政策锚点和时间区间、部分结构化规则冲突、FAQ 参数、订单支付与优惠分摊、业务快照关联、案例证据时序及夹具隔离。

字段契约以仓库 Schema 为准。校验器使用标准库实现当前数据所需的明确子集，不是通用 JSON Schema 引擎；未知关键字或远程引用会失败，不自动下载或忽略。

## 常见错误

| 错误类别 | 核对内容 |
| --- | --- |
| `schema.invalid` | 必填字段、类型、枚举与格式；金额使用整数分。 |
| `reference.missing` | ID、类型和精确版本是否存在。 |
| `manifest.unlisted_file` | 文件是否属于数据包清单允许的范围。 |
| `policy.conflict` | 同场景结构化规则是否存在不一致参数。 |
| `money.*`、`business.*` | 支付分摊、商品单元、退款状态及快照关系。 |
| `development.*` | 案例结论、证据可用轮次、输入事实和依赖绑定。 |

正常知识与不可信夹具应分开声明。维护数据时先修复实际内容和引用，不能通过提前证据可用时间或改写预期结果来掩盖矛盾。

## 如何理解通过

`pass` 表示当前离线检查未发现错误；`human_review_status` 来自数据包记录，程序不会自动把待审核内容改为通过。内容审核、文件生命周期和数据库索引选择是不同状态。

校验通过不表示完成问答或真实模型验收。应用启动还需兼容索引和正确服务配置，见[运行说明](setup.md)。
