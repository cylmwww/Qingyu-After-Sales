import pytest

from app.core.exceptions import LLMOutputError
from app.providers.json_parser import extract_json_object, parse_model
from app.schemas.retrieval import RetrievalRequest


@pytest.mark.parametrize(
    "text",
    [
        '{"query":"退货 {规则}","filters":{"tags":["中文", "售后"]}}',
        '  \n{"query":"退货 {规则}","filters":{"tags":["中文", "售后"]}}\n ',
        '```json\n{"query":"退货 {规则}","filters":{"tags":["中文", "售后"]}}\n```',
        '解析结果如下：\n```JSON\n{"query":"退货 {规则}","filters":{"tags":["中文", "售后"]}}\n```\n请查收。',
        '解析结果如下：{"query":"退货 {规则}","filters":{"tags":["中文", "售后"]}}。',
    ],
    ids=["纯对象", "前后空白", "代码围栏", "围栏加说明", "对象加说明"],
)
def test_对象及包装保留嵌套内容(text):
    assert extract_json_object(text) == {
        "query": "退货 {规则}",
        "filters": {"tags": ["中文", "售后"]},
    }


@pytest.mark.parametrize(
    "text",
    [
        "[]",
        '[{"query":"退货"}]',
        '[{"query":"退货"},{"query":"售后"}]',
        '```json\n[{"query":"退货"}]\n```',
        '"{}"',
        '"退货"',
        "42",
        "true",
        "null",
    ],
    ids=[
        "空数组",
        "单对象数组",
        "多对象数组",
        "围栏内数组",
        "含括号字符串",
        "字符串",
        "数字",
        "布尔值",
        "空值",
    ],
)
def test_合法非对象根不可静默抽取内部对象(text):
    with pytest.raises(LLMOutputError):
        extract_json_object(text)


@pytest.mark.parametrize(
    "text",
    ["", " \n\t", "没有结构化结果", '{"query":', '{"query":"退货",}', '{"query":undefined}'],
    ids=["空文本", "空白", "无对象说明", "截断对象", "尾逗号", "非法值"],
)
def test_空白及破损结果返回公开解析异常(text):
    with pytest.raises(LLMOutputError):
        extract_json_object(text)


def test_空对象也是合法对象():
    assert extract_json_object("{}") == {}


def test_解析模型返回通过字段约束的实例():
    result = parse_model('检索参数：```json\n{"query":"退货规则"}\n```', RetrievalRequest)

    assert isinstance(result, RetrievalRequest)
    assert result.query == "退货规则"


@pytest.mark.parametrize(
    "text",
    ["{}", '{"query":""}', '{"query":"退货","top_k":0}', '{"query":"退货","top_k":21}'],
    ids=["缺少必填字段", "空查询", "结果数过小", "结果数过大"],
)
def test_模型约束失败转换为公开解析异常(text):
    with pytest.raises(LLMOutputError):
        parse_model(text, RetrievalRequest)
