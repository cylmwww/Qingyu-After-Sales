from app.core.enums import DocumentCategory
from app.retrieval.ingest import category_for_stem


def test_category_prefix_does_not_confuse_installment_with_install() -> None:
    assert category_for_stem("installment_huabei") == DocumentCategory.PAYMENT
    assert category_for_stem("install_appliance") == DocumentCategory.PRODUCT
    assert category_for_stem("return_policy") == DocumentCategory.RETURN_POLICY
    assert category_for_stem("coupons") == DocumentCategory.COUPON
    assert category_for_stem("privacy_cs") == DocumentCategory.ACCOUNT
    assert category_for_stem("unknown_topic") == DocumentCategory.FAQ
