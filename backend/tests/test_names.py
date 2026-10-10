from jobs.names import is_usable_company_name, normalize_company_name


def test_normalize_company_name_folds_case_and_suffix():
    assert normalize_company_name("  Acme, Inc. ") == "acme"
    assert normalize_company_name("JPMorgan Chase & Co.") == "jpmorgan chase"


def test_placeholder_names_are_not_usable():
    assert is_usable_company_name("Unknown") is False
    assert is_usable_company_name("Acme") is True
