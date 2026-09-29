"""UDPSEC V2 recovery baseline (MP0): scenario harness and acceptance tests.

See UDPSEC_V2_RECOVERY_BASELINE.md in this directory. This directory is a
package only so its test modules and `harness` subpackage import with the
parent `tests/` directory on `sys.path` (pytest's default prepend import
mode), exactly like the flat `tests/test_*.py` modules that import
`test_secure_udp_helpers`.
"""
