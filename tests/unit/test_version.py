from importlib.metadata import PackageNotFoundError, version

import corvus


def test_v2_version_is_exposed_by_module_and_package_metadata() -> None:
    assert corvus.__version__ == "0.2.0a1"
    try:
        installed = version("corvus")
    except PackageNotFoundError:
        installed = None
    assert installed == "0.2.0a1"
