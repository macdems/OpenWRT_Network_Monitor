"""Static safety checks for the distributed ACL and rpcd plugin."""
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class SecurityTests(unittest.TestCase):
    def test_acl_only_explicit_read_methods(self):
        role = json.loads((ROOT / 'openwrt/ha-netmon-acl.json').read_text())['ha_netmon_read']
        self.assertEqual(role.get('write'), {})
        self.assertEqual(role['read']['ubus']['ha_netmon'], ['snapshot'])
        for dangerous in ('file', 'uci', 'network', 'service', 'system.signal'):
            self.assertNotIn(dangerous, role['read']['ubus'])
        self.assertNotIn('file', role['read'])
        self.assertEqual(role['read']['ubus']['system'], ['info','board'])

    def test_helper_does_not_parse_rpc_input_or_execute_dynamic_code(self):
        src = (ROOT / 'openwrt' / 'ha_netmon').read_text()
        self.assertIn('case "$1"', src)
        self.assertIn('[ "$2" = snapshot ]', src)
        self.assertNotIn('eval ', src)
        code = '\n'.join(line for line in src.splitlines() if not line.strip().startswith('#'))
        self.assertNotIn('sh -c', code)
        self.assertNotIn('source ', code)
        # Only a fixed UCI-generated list of lease paths is consumed via read.
        self.assertIn('while IFS= read -r lease_path', code)
        self.assertNotIn('uci set', code)

if __name__ == '__main__':
    unittest.main()
