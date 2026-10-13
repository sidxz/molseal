import molseal


def test_package_imports_and_has_version():
    assert molseal.__version__ == "0.0.1"


def test_rdkit_and_numpy_are_the_pinned_generation():
    import numpy as np
    import rdkit

    assert rdkit.__version__.startswith("2025.03")
    assert hasattr(np, "bitwise_count")
