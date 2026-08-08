# Lüftungsgüte

Home-Assistant-Addon, das pro Raum eine kombinierte Temperatur-/Feuchte-"Güte"
berechnet und daraus eine Lüftungsempfehlung ableitet. Ersetzt die alte
`luftungsueberwachung.py` pyscript-Logik (reine Hysterese auf absoluter
Feuchte) vollständig.

## Installation

1. In Home Assistant: **Einstellungen → Add-ons → Add-on Store → ⋮ (oben rechts) → Repositories**.
2. URL dieses Repositories eintragen: `https://github.com/johannesKurz1997/ha-venti-visualizer`
3. Der Store zeigt danach das Addon **„Lüftungsgüte"** an → installieren (Build läuft auf dem Pi 5, Ziel-Architektur `aarch64`).
4. Vor dem ersten Start: Tab **Konfiguration** öffnen. Dort **nicht** in den YAML-Modus wechseln, sondern im UI-Modus bleiben (⋮-Menü → „In UI bearbeiten", falls YAML aktiv ist) – dann lassen sich Sensoren per Dropdown auswählen und Räume per „+" hinzufügen/entfernen (siehe [Konfiguration im Dashboard](#konfiguration-im-dashboard)).
5. Addon starten. Die Oberfläche erscheint über **Ingress** direkt in der Sidebar (kein separater Port, keine eigene Anmeldung nötig).

## Automatische Raumerkennung

Standardmäßig (`auto_discover_rooms: true`) muss **kein einziger Raum manuell
angelegt werden**. Das Addon fragt bei jedem Start und danach alle
`area_discovery_interval_minutes` (Default 30 Min.) über die HA-Bereichs-
Registry ab, welche **Bereiche** ("Areas") in deiner Instanz existieren, und
sucht darin jeweils einen Sensor mit `device_class: temperature` und einen
mit `device_class: humidity`:

- Bereich hat beide Sensoren gefunden und ist **nicht** in `no_window_areas`
  gelistet → wird automatisch als Lüftungs-Raum behandelt, mit
  `default_ideal_temp` / `default_ideal_humidity_rel` / `default_weight_*`
  als Idealwerten.
- Bereich steht in `no_window_areas` (Default: `["Bad"]`) → wird automatisch
  als fensterloser Raum (nur Schimmelrisiko) behandelt, mit
  `mold_risk_threshold` / `mold_risk_hysteresis` als Schwellwerten.
- Bereich hat keinen passenden Sensor (device_class fehlt oder Sensor fehlt
  ganz) → wird stillschweigend übersprungen, kein Fehler.
- Hat ein Bereich **mehrere** Sensoren mit passender device_class (z.B. zwei
  Thermometer im selben Raum), wird deterministisch der alphabetisch erste
  genommen und eine Warnung geloggt (`docker logs` bzw. Addon-Log-Tab). Für
  volle Kontrolle in diesem Fall: manueller Eintrag mit gleichem Namen (siehe
  unten).

**Manuelle Einträge haben immer Vorrang**: Ein Eintrag in `rooms` oder
`no_window_rooms`, dessen `name` exakt (case-insensitive) zu einem
HA-Bereichsnamen passt, ersetzt den automatisch gefundenen Raum vollständig
(z.B. um andere Idealwerte zu setzen oder einen bestimmten von mehreren
Sensoren zu erzwingen). Räume ganz ohne passenden HA-Bereich lassen sich
weiterhin frei per manuellem Eintrag definieren. Mit
`auto_discover_rooms: false` verhält sich das Addon wie eine rein manuell
konfigurierte Liste (kein WebSocket-Zugriff auf die Registry nötig).

**Voraussetzung:** Sensoren müssen in HA einem **Bereich** zugeordnet sein
(direkt am Entity oder über das zugehörige Gerät) und eine `device_class`
von `temperature` bzw. `humidity` haben – beides ist bei den meisten
Integrationen (Zigbee2MQTT, ESPHome, Bluetooth-Thermometer, ...) automatisch
der Fall.

## Konfiguration im Dashboard

Alle Entity-Felder (`temp_entity`, `humidity_entity`, `outdoor_weather_entity`,
`person_entity`) sind als `entity(...)`-Schema-Typ definiert. Im UI-Modus des
Konfigurations-Tabs zeigt Home Assistant dafür automatisch ein
Entity-Picker-Dropdown, vorgefiltert nach Domain (z.B. nur `weather.*`) bzw.
Domain + Device-Class (z.B. nur `sensor.*` mit `device_class: temperature`).
Diese Picker sind vor allem für manuelle Einträge in `rooms`/`no_window_rooms`
relevant – bei aktiver Auto-Discovery brauchst du sie im Regelfall gar nicht.

Die `rooms`- und `no_window_rooms`-Listen sind Listen-Schemas – im UI-Modus
erscheint dafür pro Eintrag ein Karten-Block mit „Hinzufügen"/„Entfernen"-
Buttons, falls doch ein manueller Override nötig ist.

**Einschränkung:** Der `entity(sensor|temperature)`-Filter (Picker **und**
Auto-Discovery) zeigt nur Sensoren, die HA tatsächlich mit
`device_class: temperature` (bzw. `humidity`) kennt. Fehlt einem Sensor diese
Device-Class, taucht er weder im Dropdown noch in der Auto-Discovery auf –
dann kurz in den YAML-Modus wechseln und die Entity-ID manuell in einem
`rooms`/`no_window_rooms`-Eintrag setzen (das Addon selbst validiert nur,
dass es ein String ist, keine Domain/Device-Class-Prüfung).

Das `service`-Feld unter `notify_targets` bleibt bewusst ein Freitextfeld
(kein Picker): Companion-App-Notify-Ziele sind historisch Services
(`notify.mobile_app_xxx`), kein einheitlicher Entity-Picker-Typ dafür
existiert in der Add-on-Schema-DSL zuverlässig über alle HA-Versionen
hinweg. Wert entspricht dem Service-Namen aus Entwicklertools → Aktionen.

## Was das Addon tut

Alle `poll_interval_seconds` (Default 60s):

0. Falls `auto_discover_rooms: true`: alle `area_discovery_interval_minutes`
   die HA-Bereichs-Registry über die Core-WebSocket-API abfragen und die
   Raumliste aktualisieren (siehe [Automatische Raumerkennung](#automatische-raumerkennung)).
1. Liest Temperatur/Feuchte für jeden (manuellen + auto-erkannten) Raum sowie Außentemperatur/-feuchte aus dem konfigurierten `weather.*`-Entity.
2. Berechnet absolute Feuchte (Magnus-Formel) für Innen- und Außenluft.
3. Simuliert für jeden Lüftungs-Raum die Mischung Innen-/Außenluft in `blend_steps`-Schritten von 0–100 % und berechnet den Güte-Score an jedem Punkt (siehe unten).
4. Bildet `ΔGüte = Güte(100%) - Güte(0%)`. Ist `ΔGüte` seit `stability_minutes` Minuten ununterbrochen über `delta_guete_threshold`, wird Lüften empfohlen.
5. Setzt `binary_sensor.luften_<raum>` entsprechend und schickt beim Wechsel auf „empfohlen" eine Push-Notification an alle in `notify_targets` konfigurierten Ziele, deren zugehörige Person gerade `home` ist.
6. Bewertet jeden fensterlosen Raum (z.B. Bad) separat (kein Fenster, keine Güte/Lüftungslogik) rein über absolute Feuchte mit Schwelle + Hysterese und setzt `binary_sensor.schimmelrisiko_<raum>` (für den Bereich "Bad" also exakt `binary_sensor.schimmelrisiko_bad`, kompatibel zur bisherigen Entity-ID).

Stabilitäts-Zustand und eine begrenzte Score-Historie werden unter `/data`
persistiert und überstehen damit Addon-Neustarts.

## Güte-Score

```
Güte(Raum) = -( w_temp * (T_ist - T_ideal)² + w_feuchte * (F_abs_ist - F_abs_ideal)² )
```

- Höher (näher an 0) ist besser; 0 exakt am Idealpunkt.
- `F_abs_ideal` wird einmal pro Raum aus `ideal_temp` + `ideal_humidity_rel` (Magnus-Formel) berechnet und bleibt danach als **fester** Zielwert für die absolute Feuchte bestehen – er skaliert nicht mit der tatsächlichen Temperatur. Das entspricht der bauphysikalischen Logik: es gibt eine feste, schimmelunkritische Feuchtemenge in der Luft, unabhängig davon, wie warm der Raum gerade tatsächlich ist.
- `w_temp` / `w_feuchte` gewichten die beiden Anteile pro Raum.

### Blend-Curve

`GET /api/rooms/{room}/blend-curve` liefert 11 Punkte (bei `blend_steps: 10`)
von 0 % (aktueller Innenraumzustand) bis 100 % (vollständig Außenluft), je
mit `{blend, temp_c, abs_humidity_gm3, guete}`. Diese Kurve ist die
Grundlage für die geplante spätere 3D-Visualisierung (Achsen: Feuchte,
Temperatur, Güte + Pfeil 0→100 %) sowie für die Lüftungsentscheidung selbst.

## REST-Endpunkte (über Ingress erreichbar)

- `GET /api/rooms` – aktuelle Werte + Güte aller Lüftungsräume (manuell + auto-erkannt)
- `GET /api/rooms/{room}/blend-curve` – die Blend-Punkte für einen Raum (Name oder Slug, z.B. `wohnzimmer`)
- `GET /api/no-window-rooms` – aktuelle Werte + Schimmelrisiko aller fensterlosen Räume
- `GET /health` – Health-Check

## Optionen

```yaml
auto_discover_rooms: true
area_discovery_interval_minutes: 30
no_window_areas:
  - "Bad"
default_ideal_temp: 21.0
default_ideal_humidity_rel: 45.0
default_weight_temp: 1.0
default_weight_humidity: 1.0

# Optional: manuelle Overrides. Name == HA-Bereichsname => ersetzt Auto-Discovery
# für genau diesen Raum. Name ohne passenden Bereich => komplett manueller Raum.
rooms:
  - name: "Schlafzimmer"
    temp_entity: "sensor.temphum_schlafzimmer_temperatur"
    humidity_entity: "sensor.temphum_schlafzimmer_luftfeuchtigkeit"
    ideal_temp: 18.0             # z.B. bewusst kühler als der globale Default
    ideal_humidity_rel: 50.0
    weight_temp: 1.0
    weight_humidity: 1.0
    binary_sensor_suffix: null   # optional: überschreibt den Auto-Slug (z.B. bei Umlaut-Problemen)
no_window_rooms: []              # normalerweise leer, "Bad" kommt über no_window_areas automatisch
mold_risk_threshold: 12.0        # g/m³ absolute Feuchte, ab der Schimmelrisiko = an
mold_risk_hysteresis: 1.5        # g/m³ Abstand, um wieder auf "aus" zu schalten

outdoor_weather_entity: "weather.forecast_home"
blend_steps: 10                  # muss 100 ohne Rest teilen
delta_guete_threshold: 0.5
stability_minutes: 10
poll_interval_seconds: 60
notify_targets:
  - service: "notify.mobile_app_johannes_handy"
    person_entity: "person.johannes_kurz"
  - service: "notify.mobile_app_linas_handy"
    person_entity: "person.lina_kollmann"
```

Der Raum-Slug für `binary_sensor.luften_<slug>` (bzw.
`binary_sensor.schimmelrisiko_<slug>` für fensterlose Räume) wird automatisch
aus `name` abgeleitet (Umlaute wie bei HA üblich: ü→u, ö→o, ä→a, ß→ss, z.B.
"Büro" → `buro`) – gilt für auto-erkannte Räume genauso wie für manuelle
Einträge, da beide denselben Bereichs-/Raumnamen verwenden. Falls das nicht
zur gewünschten Entity-ID passt, `binary_sensor_suffix` in einem manuellen
Eintrag explizit setzen.

## Architekturentscheidungen

- **`no_window_rooms` als eigene, generalisierte Liste** (statt `has_window: false`
  in der `rooms`-Liste): fensterlose Räume nehmen an Güte-Score, Blend-Curve
  und Lüftungsentscheidung überhaupt nicht teil, sondern laufen fachlich
  komplett getrennt (nur Schimmelrisiko-Schwelle). Eine gemeinsame Liste mit
  Sonderfall-Flag hätte in `scoring.py`/`main.py` an mehreren Stellen
  Verzweigungen nötig gemacht; die getrennte Liste macht das im Code und im
  REST-API (`/api/rooms` vs. `/api/no-window-rooms`) explizit. Ursprünglich
  gab es dafür eine singuläre `bad:`-Sektion; mit der automatischen
  Raumerkennung wurde daraus eine Liste, damit potenziell auch andere
  fensterlose Räume (Abstellkammer, Keller, …) ohne Codeänderung dazukommen.
- **Registry-Abfrage nur über eine kleine `no_window_areas`-Namensliste**,
  nicht über HA-Labels: Labels wären eleganter (semantisch am Bereich/Gerät
  selbst hinterlegt), setzen aber voraus, dass in HA ein entsprechendes Label
  existiert und gepflegt wird. Eine Config-Option mit Bereichsnamen ist ein
  einzelner String pro fensterlosem Raum, unmittelbar verständlich und ohne
  zusätzliche HA-seitige Vorbereitung nutzbar.
- **Registry-Zugriff nur per WebSocket, mit langem Intervall**: Home
  Assistant exponiert die Area-/Device-/Entity-Registry nicht über die
  REST-States-API, nur über `config/area_registry/list` etc. auf der
  Core-WebSocket-API (`ws://supervisor/core/websocket`, gleicher
  `SUPERVISOR_TOKEN`). Da sich Bereiche/Zuordnungen selten ändern, wird das
  nicht bei jedem 60s-Poll, sondern nur alle `area_discovery_interval_minutes`
  (Default 30 Min.) abgefragt – eine kurzlebige WS-Verbindung pro Abfrage
  statt eines dauerhaft offenen Sockets, um die Fehlerfläche klein zu halten.
- **Mehrdeutige Sensoren: erster Treffer + Log-Warnung**, kein automatisches
  Überspringen: einfacher und der Raum ist sofort nutzbar; falls der falsche
  Sensor gewählt wurde, per manuellem `rooms`/`no_window_rooms`-Eintrag mit
  gleichem Namen gezielt überschreiben.
- **States-API statt MQTT** für `binary_sensor.*`: Das Addon hat über
  `homeassistant_api: true` ohnehin Zugriff auf die Core-API: kein
  zusätzlicher Broker-Kram nötig. Nachteil: die States gelten als manuell
  gesetzt (keine Integrations-Verfügbarkeits-Semantik). Für mehr Robustheit
  ließe sich später auf MQTT-Discovery umstellen.
- **Persistenz als JSON unter `/data`**, kein SQLite: Datenmenge (Stabilitäts-
  Zustand pro Raum + max. 500 Historieneinträge) ist klein genug, dass eine
  Datenbank keinen Mehrwert bringt.

## Offene Punkte / Annahmen (bitte gegenchecken)

- **Entity-ID-Defaults spielen dank Auto-Discovery praktisch keine Rolle
  mehr**: Solange deine Sensoren HA-Bereichen zugeordnet sind und eine
  `device_class` haben, findet das Addon sie von selbst (siehe
  [Automatische Raumerkennung](#automatische-raumerkennung)). Bitte einmal
  nach der Installation die Addon-Logs prüfen, ob die erwarteten Bereiche
  gefunden wurden ("Area-Discovery: N Bereich(e) ... gefunden"). Einzig
  `weather.forecast_home` sollte `temperature`/`humidity` als Attribute
  liefern (bei met.no sollte das der Fall sein) – das lässt sich in
  Entwicklertools → Zustände gegenchecken.
- **WebSocket-Endpunkt `ws://supervisor/core/websocket` nicht am echten
  Supervisor getestet**: Die REST-States-API (`http://supervisor/core/api`)
  wurde in dieser Session gegen einen gemockten Client verifiziert, die
  WebSocket-Registry-Abfrage nur gegen die reine Nachrichten-Logik (Auth-Flow,
  Command/Response-Matching), nicht gegen einen echten Supervisor-Proxy –
  dafür stand kein Zugriff zur Verfügung. Falls sich beim ersten Start
  "Area-Discovery fehlgeschlagen" im Log zeigt, greift das Addon automatisch
  auf reine manuelle Konfiguration zurück (Fehler wird geloggt, Discovery
  einfach beim nächsten Intervall erneut versucht) – funktioniert also auch
  dann, es fehlt dann nur die Automatik.
- **Schimmelrisiko-Schwelle fürs Bad**: Ohne den Original-Code von
  `luftungsueberwachung.py` wurde ein plausibler globaler Default gewählt
  (`mold_risk_threshold: 12.0 g/m³`, `mold_risk_hysteresis: 1.5 g/m³`, gilt
  für alle `no_window_areas`/`no_window_rooms`, sofern nicht pro Raum
  überschrieben). Diesen Wert bitte mit der bisherigen Logik abgleichen und
  anpassen.
- **`delta_guete_threshold` Default (0.5)**: mangels Referenzdaten aus der
  bestehenden Installation ein plausibler, aber nicht empirisch
  hergeleiteter Wert. Nach ein paar Tagen Betrieb über `/api/rooms`
  (`delta_guete`-Werte) beobachten und ggf. nachjustieren.
- **Keine Notification beim Bad-Schimmelrisiko**: Der Auftrag verlangt
  Push-Notifications nur für die Lüftungsempfehlung, nicht fürs Bad. Falls
  gewünscht, ließe sich das analog ergänzen.
- **Keine Falling-Edge-Notification**: Es wird nur benachrichtigt, wenn eine
  Empfehlung neu entsteht (Rising Edge), nicht wenn sie wieder endet, um
  Spam zu vermeiden.
- **Base-Image-Tag** (`ghcr.io/home-assistant/aarch64-base-python:3.12-alpine3.20`)
  ist zum Zeitpunkt der Erstellung ein aktueller, aber nicht auf Verfügbarkeit
  geprüfter Tag – falls der Build fehlschlägt, in `build.yaml` ggf. auf den
  aktuell in `home-assistant/addons-example` verwendeten Tag anpassen.
- **Icon/Logo**: Es liegt aktuell kein `icon.png`/`logo.png` bei; das Addon
  erscheint im Store mit Default-Icon. Optional ergänzbar.
