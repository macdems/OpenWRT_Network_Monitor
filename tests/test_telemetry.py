"""Pure unit tests: no HA installation or live router needed."""
import base64
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "openwrt_network_monitor"))
from telemetry import (  # noqa
    parse_nodes_json, decode_leases, parse_fdb_json, parse_neigh_json,
    cpu_percent, system_stats, interfaces_from_snapshot,
    select_ethernet, wifi_network_name,
)
from wired import parse_vlan_mapping, parse_networks, network_for_ip

PRIMARY = {"host":"router.example", "port":443, "https":True, "username":"hass", "password":"password", "name":"Main"}
MAC_WIRED = "02:00:00:00:00:01"
MAC_WIFI = "02:00:00:00:00:02"


def snapshot(fdb=None, neigh=None, links=None):
    return {"fdb": fdb or [], "neighbours": neigh or [], "links": links or [], "cpu_ticks": [10,0,20,70,0,0,0,0], "speeds_mbps":{}, "dhcp_leases_b64":""}


class TelemetryTests(unittest.TestCase):
    def test_node_inheritance_and_topology(self):
        rows = parse_nodes_json(json.dumps([
            {"host":"node-a.example", "name":"Example AP", "role":"ap", "upstream":"router.example", "uplink_ports":["lan1"], "edge_ports":["lan2"]},
            {"host":"switch-b.example", "name":"Example switch", "role":"unmanaged_switch", "upstream":"node-a.example", "parent_port":"lan2"}]), PRIMARY)
        self.assertEqual(rows[1]["port"], 443)
        self.assertEqual(rows[1]["edge_ports"], ["lan2"])
        self.assertEqual(rows[2]["role"], "unmanaged_switch")

    def test_reject_topology_cycle_and_bad_uplink(self):
        with self.assertRaises(ValueError):
            parse_nodes_json('[{"host":"a","upstream":"b"},{"host":"b","upstream":"a"}]', PRIMARY)
        with self.assertRaises(ValueError):
            parse_nodes_json('[{"host":"a","uplink_ports":["lan1"],"edge_ports":["lan1"]}]', PRIMARY)
        with self.assertRaises(ValueError):
            parse_nodes_json('[{"host":"a","role":"unmanaged_switch","upstream":"router.example"}]', PRIMARY)

    def test_dhcp_not_presence(self):
        data = snapshot()
        data["dhcp_leases_b64"] = base64.b64encode(f"123 {MAC_WIRED} 10.0.10.3 host-a *".encode()).decode()
        self.assertEqual(decode_leases(data)[MAC_WIRED]["hostname"], "host-a")
        self.assertEqual(select_ethernet({"router.example":data}, parse_nodes_json("[]",PRIMARY), {}, {}, {}, [], {}), {})

    def test_fdb_filters_local_static_and_multicast(self):
        result = parse_fdb_json(snapshot(fdb=[
            {"mac":MAC_WIRED,"dev":"lan2","flags":["master"],"vlan":10},
            {"mac":MAC_WIRED,"dev":"phy0-ap0","flags":["master"]},
            {"mac":"01:00:5e:00:00:01","dev":"lan2"},
            {"mac":"02:00:00:00:00:04","dev":"lan3","flags":["permanent"]},
        ]), "router.example", {"phy0-ap0"})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["vlan"], 10)

    def test_neighbour_failed_ignored(self):
        self.assertEqual(parse_neigh_json(snapshot(neigh=[
            {"dst":"10.0.10.3", "lladdr":MAC_WIRED,"state":["REACHABLE"],"dev":"br-lan.10"},
            {"dst":"10.0.10.4", "lladdr":MAC_WIFI,"state":["FAILED"],"dev":"br-lan.10"},
        ]))[0]["ip"],"10.0.10.3")

    def test_cpu_delta(self):
        self.assertEqual(cpu_percent([10,0,10,80],[30,0,30,140]),40.0)
        self.assertIsNone(cpu_percent(None,[1,2,3,4]))
        self.assertIsNone(cpu_percent([10,0,10,80],[5,0,5,75]))

    def test_system_load_fixed_point_and_memory(self):
        info={"load":[65536,131072,32768],"memory":{"total":1000,"available":250},"uptime":12345}
        sys=system_stats(info, 52.5)
        self.assertEqual(sys['load_5'], 2)
        self.assertEqual(sys['memory_percent'],75)

    def test_interface_rates_after_two_snapshots(self):
        s1=snapshot(links=[{"ifname":"lan1","flags":["UP"],"operstate":"UP","stats64":{"rx":{"bytes":2000000},"tx":{"bytes":3000000}}}])
        s2=snapshot(links=[{"ifname":"lan1","flags":["UP"],"operstate":"UP","stats64":{"rx":{"bytes":7000000},"tx":{"bytes":13000000}}}],)
        self.assertIsNone(interfaces_from_snapshot(s1,None,None)['lan1']['rx_mbps'])
        result=interfaces_from_snapshot(s2,interfaces_from_snapshot(s1,None,None),10)['lan1']
        self.assertEqual(result['rx_mbps'],4.0)
        self.assertEqual(result['tx_mbps'],8.0)
        self.assertIsNone(interfaces_from_snapshot(s1,interfaces_from_snapshot(s2,None,None),10)['lan1']['rx_mbps'])

    def test_unknown_ssid_no_inferred_vlan(self):
        self.assertIsNone(wifi_network_name({},'phy0-ap0','home'))
        data={"radio1":{"interfaces":[{"ifname":"phy0-ap0","config":{"ssid":"foo","network":["private"]}}]}}
        self.assertEqual(wifi_network_name(data,'phy0-ap0','foo'),'private')

    def test_vlan_network_from_ip(self):
        nets=parse_networks({"interface":[{"interface":"lab","up":True,"l3_device":"br-lab","ipv4-address":[{"address":"10.0.10.1","mask":24}]}]},parse_vlan_mapping('lab=10'))
        self.assertEqual(network_for_ip('10.0.10.3',nets)['vlan'],10)

    def test_deepest_fdb_and_unknown_unmanaged_switch(self):
        nodes=parse_nodes_json(json.dumps([
          {"host":"node-a.example","upstream":"router.example","uplink_ports":["lan1"],"edge_ports":["lan2"]},
          {"host":"switch-b.example","name":"Example switch","role":"unmanaged_switch","upstream":"node-a.example","parent_port":"lan2"}]), PRIMARY)
        snaps={"router.example":snapshot(fdb=[{"mac":MAC_WIRED,"dev":"lan4","vlan":10}]),
               "node-a.example":snapshot(fdb=[{"mac":MAC_WIRED,"dev":"lan2","vlan":10}])}
        rec=select_ethernet(snaps,nodes,{}, {}, {}, [], {})[MAC_WIRED]
        self.assertEqual(rec['ap_host'],'node-a.example')
        self.assertEqual(rec['attached_to'],'Example switch')

    def test_port_down_discards_stale_fdb(self):
        nodes=parse_nodes_json("[]",PRIMARY)
        snapshots={"router.example":snapshot(
            fdb=[{"mac":MAC_WIRED,"dev":"lan2"}],
            links=[{"ifname":"lan2","operstate":"DOWN"}])}
        self.assertEqual(select_ethernet(snapshots,nodes,{}, {}, {}, [], {}), {})

    def test_stale_wireless_not_misclassified_as_wired(self):
        nodes=parse_nodes_json('[]',PRIMARY)
        snaps={"router.example":snapshot(fdb=[{"mac":MAC_WIFI,"dev":"lan1"}])}
        self.assertNotIn(MAC_WIFI,select_ethernet(snaps,nodes,{}, {}, {MAC_WIFI:{'ever_wifi':True}}, [], {}))
        self.assertNotIn(MAC_WIFI,select_ethernet(snaps,nodes,{MAC_WIFI:{}}, {}, {}, [], {}))

    def test_explicit_edge_allows_wifi_to_ethernet(self):
        nodes=parse_nodes_json('[]',{**PRIMARY, "edge_ports":["lan2"]})
        snaps={"router.example":snapshot(fdb=[{"mac":MAC_WIFI,"dev":"lan2"}])}
        self.assertIn(MAC_WIFI,select_ethernet(snaps,nodes,{}, {}, {MAC_WIFI:{'ever_wifi':True}}, [], {}))

if __name__=='__main__':
    unittest.main()
