# Changelog

Alle nennenswerten Änderungen am Addon "Lüftungsgüte".

## 0.5.3

- **Mobile Optimierung der 3D-Ansicht**: Der Plotly-Plot im Tab "3D-Ansicht" war auf
  dem Handy (Ingress-WebView, ~1080px breit) praktisch unbenutzbar - Erklärungstext
  belegte fast einen halben Screen, Legende überlappte die Modebar, der Plot-
  Container war extrem hochkant gequetscht, Achsenbeschriftungen fehlten/waren
  abgeschnitten, Cone-Pfeilspitzen überdeckten die Kurven, und Drag im Plot blockierte
  das Scrollen der Seite. Behoben durch:
  - Viewport-Meta-Tag, `overflow-x: hidden`, responsive Tab-Buttons (44px Touch-Ziel).
  - Erklärungstext in ein `<details>`-Element eingeklappt, nur eine einzeilige
    Kurzfassung bleibt permanent sichtbar.
  - Plot-Container mit `clamp(320px, 55vh, 520px)` statt fester Pixelhöhe;
    `ResizeObserver` + `orientationchange`-Handler halten die Größe aktuell.
  - `aspectmode: 'cube'`, mobil-spezifische Startkamera, minimierte Ränder,
    lesbare Achsentitel/Ticks (weniger Ticks, keine Rotation).
  - Mindest-Achsenbereiche (2 °C / 10 %-Punkte / 20 Güte-Punkte), damit die enge
    Ist-Werte-Anpassung nicht zu einer entarteten Linie kollabiert.
  - Cone-Größe jetzt von der tatsächlichen Achsen-Spannweite abgeleitet
    (`CONE_SIZE_FACTOR`, mobil zusätzlich reduziert) statt fest codiert.
  - Legende auf Mobil horizontal unter dem Plot statt über der Modebar; Legenden-
    Kennfarbe pro Raum bewusst außerhalb des Grün/Rot-Farbbereichs der Kurven, um
    Verwechslungen mit der Verbesserung/Verschlechterung-Semantik zu vermeiden.
  - Reduzierte Modebar auf Mobil, eigener "Ansicht zurücksetzen"-Button.
  - Touch-Konflikt gelöst: Plot ignoriert auf Mobil Touch-Gesten standardmäßig
    (Seite scrollt normal), ein "Drehen aktivieren"-Toggle schaltet die Rotation
    bei Bedarf gezielt frei. Auf Desktop bleibt der Plot immer interaktiv.
  - Kompaktere, einzeilige Hover-Tooltips.
  - Reine Frontend-/Layout-Änderung - Güte-Berechnung, Blend-Kurven-Simulation,
    Entities und der Bad-/Schimmelrisiko-Pfad sind unverändert.

## 0.5.2

- Fix: `device_class` von `binary_sensor.luften_<raum>` entfernt (war einheitlich
  `"opening"` für alle Räume, was zu irreführenden Zustandstexten wie
  "Geöffnet"/"Geschlossen" statt "An"/"Aus" führte). Attribute jetzt zentral über
  `_luften_attributes()`. `binary_sensor.schimmelrisiko_*` unverändert.

## 0.5.1

- Push-Notifications für Lüftungsempfehlungen auf höchstens eine pro Raum pro
  `notify_cooldown_minutes` (Default 60) begrenzt.

## 0.5.0

- Idealwerte (`ideal_temp`, `ideal_humidity_rel`) lassen sich pro Raum zur Laufzeit
  über die Ingress-Tabelle oder `PUT/DELETE /api/rooms/{room}/ideal-override`
  anpassen, ohne die Addon-Konfiguration zu ändern.

## 0.4.0 - 0.4.3

- 3D-Visualisierung (Plotly.js) als zweiter Tab neben der Tabelle.
- Aktuelle Außenreferenz in Tabelle sichtbar; aus dem 3D-Plot wieder entfernt
  (Güte ist nur relativ zum Idealpunkt eines Raums definiert).
- Kräftigere Kurvenfarben, fokussiertere/enger angepasste Achsen.

## 0.3.0

- Güte-Score auf feste 0-100-Skala normiert (vorher unbegrenzt negativ).
- Der Bereich `outdoor_area_name` (Default "Aussen") wird nicht mehr als eigener
  Raum geführt, sondern liefert die Außenreferenz für alle Blend-Kurven.

## 0.2.x

- Bugfixes an der Auto-Discovery und am HA-Add-on-Schema (u.a. ungültiger
  `entity(...)`-Schema-Typ durch `str` ersetzt).

## 0.1.0

- Erste Version: Temperatur-/feuchtebasierte Lüftungsgüte-Bewertung mit
  Lüftungsempfehlung und Schimmelrisiko-Erkennung fürs Bad.
