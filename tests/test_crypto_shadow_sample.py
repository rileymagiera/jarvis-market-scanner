import main


def test_crypto_shadow_sample_default_is_50():
    assert main.CRYPTO_SHADOW_MAX_SYMBOLS_PER_SCAN == 50


def test_crypto_shadow_sample_rotates_across_full_universe():
    symbols = [f"COIN{i}USD" for i in range(92)]

    first = main.select_crypto_shadow_symbols(symbols, run_number=1)
    second = main.select_crypto_shadow_symbols(symbols, run_number=2)

    assert len(first) == 50
    assert len(second) == 50
    assert len(set(first)) == 50
    assert len(set(second)) == 50
    assert set(first) | set(second) == set(symbols)


def test_crypto_shadow_sample_never_exceeds_configured_cap(monkeypatch):
    monkeypatch.setattr(main, "CRYPTO_SHADOW_MAX_SYMBOLS_PER_SCAN", 50)
    symbols = [f"COIN{i}USD" for i in range(120)]

    selected = main.select_crypto_shadow_symbols(symbols, run_number=1)

    assert len(selected) == 50
