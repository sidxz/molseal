from importlib.metadata import version

import molseal


def test_package_version_matches_the_installed_metadata():
    assert molseal.__version__ == version("molseal")


def test_rdkit_and_numpy_are_the_pinned_generation():
    import numpy as np
    import rdkit

    assert rdkit.__version__.startswith("2025.03")
    assert hasattr(np, "bitwise_count")
