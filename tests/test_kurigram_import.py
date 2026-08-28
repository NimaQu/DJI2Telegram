from qdc507_gateway.telegram import kurigram


def test_kurigram_namespace_provider_check_is_explicit():
    assert isinstance(kurigram._installed_telegram_distributions(), list)
