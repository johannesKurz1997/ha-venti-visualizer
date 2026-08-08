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

## Konfiguration im Dashboard

Alle Entity-Felder (`temp_entity`, `humidity_entity`, `outdoor_weather_entity`,
`person_entity`) sind als `entity(...)`-Schema-Typ definiert. Im UI-Modus des
Konfigurations-Tabs zeigt Home Assistant dafür automatisch ein
Entity-Picker-Dropdown, vorgefiltert nach Domain (z.B. nur `weather.*`) bzw.
Domain + Device-Class (z.B. nur `sensor.*` mit `device_class: temperature`).
Es müssen also keine Entity-IDs mehr abgetippt werden.

Die `rooms`-Liste ist ein Listen-Schema – im UI-Modus erscheint dafür pro
Eintrag ein Karten-Block mit „Hinzufügen"/„Entfernen"-Buttons. Neue Räume
(z.B. ein zukünftiges Gästezimmer) lassen sich also direkt im Dashboard
anlegen, ohne `config.yaml` zu ändern oder das Addon neu zu bauen – nur ein
Neustart des Addons ist danach nötig, damit die neuen Optionen geladen
werden.

**Einschränkung:** Der `entity(sensor|temperature)`-Filter zeigt nur
Sensoren, die HA tatsächlich mit `device_class: temperature` (bzw.
`humidity`) kennt. Fehlt einem Sensor diese Device-Class, taucht er im
Dropdown nicht auf – dann kurz in den YAML-Modus wechseln und die Entity-ID
manuell eintragen (das Addon selbst validiert nur, dass es ein String ist,
keine Domain/Device-Class-Prüfung).

Das `service`-Feld unter `notify_targets` bleibt bewusst ein Freitextfeld
(kein Picker): Companion-App-Notify-Ziele sind historisch Services
(`notify.mobile_app_xxx`), kein einheitlicher Entity-Picker-Typ dafür
existiert in der Add-on-Schema-DSL zuverlässig über alle HA-Versionen
hinweg. Wert entspricht dem Service-Namen aus Entwicklertools → Aktionen.

## Was das Addon tut

Alle `poll_interval_seconds` (Default 60s):

1. Liest Temperatur/Feuchte für jeden konfigurierten Raum sowie Außentemperatur/-feuchte aus dem konfigurierten `weather.*`-Entity.
2. Berechnet absolute Feuchte (Magnus-Formel) für Innen- und Außenluft.
3. Simuliert für jeden Raum die Mischung Innen-/Außenluft in `blend_steps`-Schritten von 0–100 % und berechnet den Güte-Score an jedem Punkt (siehe unten).
4. Bildet `ΔGüte = Güte(100%) - Güte(0%)`. Ist `ΔGüte` seit `stability_minutes` Minuten ununterbrochen über `delta_guete_threshold`, wird Lüften empfohlen.
5. Setzt `binary_sensor.luften_<raum>` entsprechend und schickt beim Wechsel auf „empfohlen" eine Push-Notification an alle in `notify_targets` konfigurierten Ziele, deren zugehörige Person gerade `home` ist.
6. Bewertet das Bad separat (kein Fenster, keine Güte/Lüftungslogik) rein über absolute Feuchte mit Schwelle + Hysterese und setzt `binary_sensor.schimmelrisiko_bad`.

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

- `GET /api/rooms` – aktuelle Werte + Güte aller Lüftungsräume
- `GET /api/rooms/{room}/blend-curve` – die Blend-Punkte für einen Raum (Name oder Slug, z.B. `wohnzimmer`)
- `GET /api/bad` – aktuelle Bad-Werte + Schimmelrisiko
- `GET /health` – Health-Check

## Optionen

```yaml
rooms:
  - name: "Wohnzimmer"
    temp_entity: "sensor.temphum_wohnzimmer_temperatur"
    humidity_entity: "sensor.temphum_wohnzimmer_luftfeuchtigkeit"
    ideal_temp: 21.0
    ideal_humidity_rel: 45.0
    weight_temp: 1.0
    weight_humidity: 1.0
    binary_sensor_suffix: null   # optional: überschreibt den Auto-Slug (z.B. bei Büro/Umlaut-Problemen)
bad:
  temp_entity: "sensor.temphum_bad_temperatur"
  humidity_entity: "sensor.temphum_bad_luftfeuchtigkeit"
  mold_risk_threshold: 12.0      # g/m³ absolute Feuchte, ab der Schimmelrisiko = an
  mold_risk_hysteresis: 1.5      # g/m³ Abstand, um wieder auf "aus" zu schalten
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

Der Raum-Slug für `binary_sensor.luften_<slug>` wird automatisch aus `name`
abgeleitet (Umlaute wie bei HA üblich: ü→u, ö→o, ä→a, ß→ss, z.B. "Büro" →
`buro`). Falls das nicht zur echten Entity-ID passt, `binary_sensor_suffix`
explizit setzen.

## Architekturentscheidungen

- **Bad als eigene Konfigurationssektion (`bad:`)**, nicht als Raum mit
  `has_window: false` in der `rooms`-Liste: Bad nimmt an Güte-Score,
  Blend-Curve und Lüftungsentscheidung überhaupt nicht teil, sondern läuft
  fachlich komplett getrennt (nur Schimmelrisiko-Schwelle). Eine gemeinsame
  Liste mit Sonderfall-Flag hätte in `scoring.py`/`main.py` an mehreren
  Stellen Verzweigungen nötig gemacht; die getrennte Sektion macht das im
  Code und im REST-API (`/api/rooms` vs. `/api/bad`) explizit.
- **States-API statt MQTT** für `binary_sensor.*`: Das Addon hat über
  `homeassistant_api: true` ohnehin Zugriff auf die Core-API: kein
  zusätzlicher Broker-Kram nötig. Nachteil: die States gelten als manuell
  gesetzt (keine Integrations-Verfügbarkeits-Semantik). Für mehr Robustheit
  ließe sich später auf MQTT-Discovery umstellen.
- **Persistenz als JSON unter `/data`**, kein SQLite: Datenmenge (Stabilitäts-
  Zustand pro Raum + max. 500 Historieneinträge) ist klein genug, dass eine
  Datenbank keinen Mehrwert bringt.

## Offene Punkte / Annahmen (bitte gegenchecken)

- **Entity-ID-Defaults sind nur Startwerte**: Dank der Entity-Picker im
  Konfigurations-UI (siehe [Konfiguration im Dashboard](#konfiguration-im-dashboard))
  müssen sie nicht mehr exakt stimmen – beim ersten Einrichten im Dashboard
  einfach pro Raum den richtigen Sensor auswählen. Einzig `weather.forecast_home`
  sollte `temperature`/`humidity` als Attribute liefern (bei met.no sollte
  das der Fall sein) – das lässt sich in Entwicklertools → Zustände
  gegenchecken.
- **Schimmelrisiko-Schwelle fürs Bad**: Ohne den Original-Code von
  `luftungsueberwachung.py` wurde ein plausibler Default gewählt
  (`mold_risk_threshold: 12.0 g/m³`, `mold_risk_hysteresis: 1.5 g/m³`).
  Diesen Wert bitte mit der bisherigen Logik abgleichen und anpassen.
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
