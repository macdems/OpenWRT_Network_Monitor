"""Hostname lookup tests: IP/MAC metadata never changes presence decisions."""
import base64
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "openwrt_network_monitor"))
from identity import (parse_static_dhcp_hosts, decode_static_hosts,  # noqa: E402
                      merge_identity_sources, hostname_for_ip)
from telemetry import decode_leases  # noqa: E402

MAC = "02:00:00:00:00:aa"
MAC_OTHER = "02:00:00:00:00:bb"


class IdentityTests(unittest.TestCase):
    def test_dhcp_host_reservation_preferred_when_lease_name_missing(self):
        uci = """dhcp.cfg01=host
        dhcp.cfg01.name='Workstation'
        dhcp.cfg01.mac='02:00:00:00:00:aa'
        dhcp.cfg01.ip='192.0.2.32'
        dhcp.cfg02=dnsmasq
        dhcp.cfg02.server='example.net'
        """
        snapshot = {
            "dhcp_static_hosts_b64": base64.b64encode(uci.encode()).decode(),
            "dhcp_leases_b64": base64.b64encode(f"500 {MAC} 192.0.2.32 * *\n".encode()).decode(),
        }
        static, _ = decode_static_hosts(snapshot)
        identities = merge_identity_sources([
            ("dhcp_reservation", static),
            ("dnsmasq_lease", decode_leases(snapshot)),
        ])
        self.assertEqual(identities[MAC]["hostname"], "Workstation")
        self.assertEqual(identities[MAC]["name_source"], "dhcp_reservation")
        self.assertEqual(identities[MAC]["ip"], "192.0.2.32")

    def test_dynamic_dnsmasq_lease_can_supply_name(self):
        text = f"500 {MAC} 192.0.2.35 real-hostname *\n"
        static, _ = decode_static_hosts({})
        leases = decode_leases({"dhcp_leases_b64": base64.b64encode(text.encode()).decode()})
        names = merge_identity_sources([
            ("dhcp_reservation", static), ("dnsmasq_lease", leases),
        ])
        self.assertEqual(names[MAC]["hostname"], "real-hostname")
        self.assertEqual(names[MAC]["name_source"], "dnsmasq_lease")

    def test_secondary_blank_does_not_override_primary_name(self):
        merged = merge_identity_sources([
            ("main_dnsmasq", {MAC: {"hostname": "phone", "ip": "192.0.2.4"}}),
            ("secondary_odhcpd", {MAC: {"hostname": None, "ip": None}}),
            ("secondary_dnsmasq", {MAC: {"hostname": "*", "ip": None}}),
        ])
        self.assertEqual(merged[MAC]["hostname"], "phone")
        self.assertEqual(merged[MAC]["name_source"], "main_dnsmasq")

    def test_static_host_with_multiple_macs(self):
        cfg = """dhcp.@host[0]=host
        dhcp.@host[0].name='Lab device'
        dhcp.@host[0].mac='02:00:00:00:00:aa 02:00:00:00:00:bb'
        dhcp.@host[0].ip='192.0.2.44'
        """
        by_mac, by_ip = parse_static_dhcp_hosts(cfg)
        self.assertEqual(by_mac[MAC]["hostname"], "Lab device")
        self.assertEqual(by_mac[MAC_OTHER]["hostname"], "Lab device")
        self.assertEqual(by_ip["192.0.2.44"], "Lab device")

    def test_domain_only_name_can_be_matched_by_ip(self):
        cfg = """dhcp.domain_1=domain
        dhcp.domain_1.name='printer.example'
        dhcp.domain_1.ip='192.0.2.91'
        """
        by_mac, by_ip = parse_static_dhcp_hosts(cfg)
        self.assertEqual(by_mac, {})
        self.assertEqual(hostname_for_ip("192.0.2.91", [by_ip]), "printer.example")

    def test_unrelated_uci_config_is_not_parsed(self):
        cfg = """dhcp.cfgA=dnsmasq
        dhcp.cfgA.name='should-not-be-hostname'
        dhcp.cfgA.mac='02:00:00:00:00:aa'
        dhcp.cfgB=host
        dhcp.cfgB.mac='02:00:00:00:00:bb'
        dhcp.cfgB.name='*'
        """
        macs, _ = parse_static_dhcp_hosts(cfg)
        self.assertEqual(macs, {})

    def test_invalid_or_missing_snapshot_fields_are_safe(self):
        self.assertEqual(decode_static_hosts({}), ({}, {}))
        self.assertEqual(decode_static_hosts({"dhcp_static_hosts_b64": "@@@@"}), ({}, {}))
        self.assertEqual(merge_identity_sources([("dnsmasq_lease", {MAC: {"hostname": "*"}})])[MAC]["hostname"], None)


if __name__ == "__main__":
    unittest.main()
