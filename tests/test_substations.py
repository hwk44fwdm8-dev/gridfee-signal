"""Reading substation names from transformer names."""

from gridfee.network import jao


def test_transformer_substation_names():
    assert jao.transformer_substation("TR Audorf/S T422") == "Audorf/S"
    assert jao.transformer_substation("TR Eula 220/400 401") == "Eula"
    assert jao.transformer_substation("TR / 380-220kV / Kuehmoos Trafo 401") == "Kuehmoos"
    assert jao.transformer_substation("PST Wuergau T442B") == "Wuergau"
    assert jao.voltage_level(231) == 220 and jao.voltage_level(410) == 380
