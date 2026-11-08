# Network Flow Card — przykładowy dashboard

Przykłady współpracują z **Network Flow Card 4.x** i encjami wystawianymi przez **OpenWrt Network Monitor v0.4.0**. Żaden przykład **nie jest uniwersalną, automatycznie wygenerowaną topologią**: konfigurację trzeba dostosować do własnych węzłów, interfejsów i klientów. Nie wymaga SNMP ani karty `card-mod`.

## Pliki

- `network-flow-card.yaml` — **pojedyncza** karta do wklejenia przez **Dodaj kartę → Ręcznie** w edycji Lovelace.
- `network-dashboard.yaml` — **pełny dashboard** z widokiem sieci, podsumowaniem WAN, trzema wskaźnikami CPU, historią przepływności, statystykami AP i tabelą klientów Ethernet według sieci logicznych. Do edytora surowej konfiguracji dashboardu, nie do edytora pojedynczej karty.
- `template-helpers.yaml` — **opcjonalne** sumatory klientów Wi-Fi ze wszystkich BSS/SSID na każdym AP. Fragment `template:` do `configuration.yaml` lub pakietu HA (jeżeli masz już sekcję `template:`, **scal**, nie dodawaj drugiego klucza). Możesz też wskazać pojedynczy sensor BSS bez helpera, jeżeli AP udostępnia jedno SSID.

## Instalacja karty

Zainstaluj <https://github.com/benmac7/network-flow-card> przez **HACS → Custom repositories → Dashboard** (typ *Dashboard*). Po instalacji dodaj zasób JavaScript, jeśli HACS nie zrobi tego automatycznie, zgodnie z README karty. Następnie dodaj kartę ręcznie lub utwórz osobny dashboard i wklej `network-dashboard.yaml` w edytorze surowej konfiguracji.

## Co musisz poprawić

1. **Nazwy encji.** Przykłady używają `sensor.router_*`, `sensor.ap_alpha_*` i `sensor.ap_beta_*`. Prawdziwe identyfikatory tworzy Home Assistant na podstawie nazw węzłów podanych w Config Flow (np. `Router CPU usage` może dać `sensor.router_cpu_usage`). Sprawdź **Narzędzia deweloperskie → Stany**, filtrując `cpu_usage`, `load_1m`, `rx_bandwidth`, `wifi_clients`, `link`, `ethernet_clients`. Ustaw **jeden właściwy interfejs WAN** — NIE sumuj `wan`, `br-*`, VLAN i fizycznego uplinku, bo można policzyć ten sam ruch wielokrotnie.
2. **Łącza i węzły.** `binary_sensor.router_lan_port_link` w przykładzie to tylko stan portu prowadzącego do *niezarządzalnego* switcha — **nie jest** niezależnym pomiarem działania switcha. Jeśli go nie masz lub nie znasz uplinku, usuń cały węzeł `Switch LAN (uplink)` z `nodes`. Jeśli masz zarządzalny switch OpenWrt, można użyć jego prawdziwego sensora zamiast proxy portu.
3. **SSID i BSSID.** Każdy BSS/SSID ma osobny sensor klientów Wi-Fi, nawet gdy AP ma kilka SSID na jednym radiu. Zamień wpisy w `template-helpers.yaml` na wszystkie **rzeczywiste** sensory BSS danego AP. Jeśli nazwy SSID powtarzają się między AP, Home Assistant może nadać ID z sufiksami. W przykładach jest tylko symboliczny podział na `primary` i `secondary` — usuń lub dopisz dowolne SSID.
4. **Trackery.** Zamień `device_tracker.phone`, `device_tracker.workstation`, `device_tracker.printer` na trackery utworzone przez integrację. Nieistniejące encje będą pokazywane jako brakujące. Karta **nie ma jeszcze dedykowanego automatycznego Discover dla `openwrt_network_monitor`**; wpisz klientów ręcznie w `individual_devices` lub wybierz z edytora karty.
5. **Klienci Ethernet.** `sensor.ethernet_clients_all` pochodzi z integracji; sprawdź jego rzeczywiste ID. `by_network` jest atrybutem z mapą nazw sieci → liczba obecnych urządzeń. Nie trzeba wpisywać własnych VLAN-ów w dashboardzie.

## Hyperbolic, Flat i automatyczne przypinanie do AP

Domyślnie przykład ustawia `hyperbolic_client_layout: grouped`, żeby nie opierać prezentacji na nierozpoznanym automatycznie rodzicu. Możesz przestawić na `attached` po sprawdzeniu, czy Twoja wersja Network Flow Card dopasowuje klientów do węzłów. Integracja podaje `ap_mac` (BSSID), `ssid`, `vlan`, `ap_name`, `attached_to`, `connection_type`, ale **karta 4.x nie ogłasza natywnej obsługi `openwrt_network_monitor`**. Samo wystawienie tych atrybutów nie gwarantuje poprawnego dopięcia klientów do AP lub switcha. Jeśli pojawiają się przy „Unknown”, wróć do `grouped` lub dodaj ręczne reguły/obsługę integracji w karcie.

Możesz też zmienić `view_mode` na `flat` (wtedy nie działa wykres w dysku hiperbolicznym). W edytorze karty wybierz **Clients → Group By → VLAN** lub **SSID**, aby prezentować klientów w podgrupach. Nazwy ustawień i ich wartości najlepiej zweryfikować w aktualnym edytorze Network Flow Card.

## Sensory — ważne rozróżnienia

- `*_rx_bandwidth` / `*_tx_bandwidth`: **bieżący ruch Mbit/s**, a nie prędkość fizycznego portu; `*_negotiated_link_speed` pokazuje prędkość linku, jeżeli dostępna.
- `*_cpu_usage`: procent obciążenia CPU. `*_load_1m`: load average, **nie procent CPU**. W przykładzie `load_1m` jest także „sygnałem życia” routera i AP, bo sam sensor nadal jest dostępny tylko przy poprawnej telemetrii.
- `*_wifi_clients`: liczba klientów pojedynczego BSS. Sumatory per AP są tylko przykładami template sensorów.
- CPU/RAM i transfer mają własne sensory historyczne w HA. Nie musisz dodawać SNMP.

Przykład opiera się na strukturze YAML opublikowanej przez autorów karty: <https://github.com/benmac7/network-flow-card/blob/main/README.md> oraz <https://github.com/benmac7/network-flow-card/blob/main/HOWTO.md>. Kod niestandardowej karty może się zmieniać niezależnie od tej integracji.
