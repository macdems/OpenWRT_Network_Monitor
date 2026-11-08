"""Run with: python -m unittest discover -s tests -v"""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "openwrt_network_monitor"))
from parser import (  # noqa: E402
    band_from_freq, normalize_nodes, parse_aggregated, parse_local_clients,
    parse_dnsmasq_leases, parse_dhcp_object, parse_hostapd_clients, connected_during_grace,
)


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.local = {"hostapd.phy0-ap0": {"bssid": "00:11:22:33:44:55", "ssid": "ssid-a", "freq": 5180}}
        self.remote = {"192.0.2.2#hostapd.phy1-ap0": {"bssid": "00:11:22:33:44:66", "ssid": "ssid-b", "freq": 2412}}
        self.nodes = normalize_nodes(self.local, self.remote, "192.0.2.1")

    def test_band(self):
        self.assertEqual(band_from_freq(2412), "2.4G")
        self.assertEqual(band_from_freq(5180), "5G")
        self.assertEqual(band_from_freq(5975), "6G")

    def test_aggregated_true_vs_false(self):
        clients = {
            "aa:bb:cc:dd:ee:01": {
                "hostapd.phy0-ap0": {"connected": False, "signal": -60},
                "192.0.2.2#hostapd.phy1-ap0": {"connected": True, "signal": -45},
            },
            "aa:bb:cc:dd:ee:02": {
                "hostapd.phy0-ap0": {"connected": False, "signal": -50}
            },
        }
        parsed = parse_aggregated(clients, self.nodes)
        self.assertEqual(set(parsed), {"aa:bb:cc:dd:ee:01"})
        self.assertEqual(parsed["aa:bb:cc:dd:ee:01"]["ssid"], "ssid-b")
        self.assertEqual(parsed["aa:bb:cc:dd:ee:01"]["ap_host"], "192.0.2.2")

    def test_direct_local(self):
        parsed = parse_local_clients({"hostapd.phy0-ap0": {
            "aa:bb:cc:dd:ee:03": {"signal": -73}}}, self.nodes, "192.0.2.1")
        self.assertTrue(parsed["aa:bb:cc:dd:ee:03"]["connected"])
        self.assertEqual(parsed["aa:bb:cc:dd:ee:03"]["band"], "5G")

    def test_no_stale_remote_node(self):
        parsed = parse_aggregated({"aa:bb:cc:dd:ee:03": {
            "192.0.2.9#hostapd.phy0-ap0": {"connected": True, "signal": -44}}}, self.nodes)
        self.assertEqual(parsed, {})

    def test_hostapd_ssids_not_in_usteer(self):
        node = normalize_nodes({}, {"192.0.2.2#hostapd.phy1-ap1": {
            "ssid": "ssid-b", "bssid": "00:11:22:33:44:77", "freq": 2412
        }}, "192.0.2.2")
        found = parse_hostapd_clients({"clients": {
            "aa:bb:cc:dd:ee:06": {"assoc": True, "authorized": True, "signal": -48},
            "aa:bb:cc:dd:ee:07": {"assoc": False, "signal": -30}
        }}, "192.0.2.2#hostapd.phy1-ap1", node)
        self.assertEqual(set(found), {"aa:bb:cc:dd:ee:06"})
        self.assertEqual(found["aa:bb:cc:dd:ee:06"]["ssid"], "ssid-b")

    def test_absence_grace(self):
        self.assertTrue(connected_during_grace(True, 100, 125, 30))
        self.assertFalse(connected_during_grace(True, 100, 130, 30))
        self.assertFalse(connected_during_grace(False, 100, 110, 30))

    def test_dhcp(self):
        self.assertEqual(parse_dnsmasq_leases("111 aa:bb:cc:dd:ee:01 192.0.2.2 phone *")
                         ["aa:bb:cc:dd:ee:01"]["hostname"], "phone")
        self.assertEqual(parse_dhcp_object({"device": {"leases": [{"mac": "aa:bb:cc:dd:ee:02", "ip": "192.0.2.4", "hostname": "laptop"}]}})
                         ["aa:bb:cc:dd:ee:02"]["hostname"], "laptop")


if __name__ == "__main__":
    unittest.main()

