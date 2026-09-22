import sys

import linkling


def test_ci_pins_python_312():
    assert sys.version_info[:2] == (3, 12)


def test_linkling_package_installs():
    assert linkling.__name__ == "linkling"
